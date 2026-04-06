"""Knowledge Graph Night Synthesis — extract entities, build wiki from pending memory chunks.

Called by scheduler cron (default 03:00). Processes pending synthesis queue:
1. Collect pending chunks from Qdrant (synthesis_status=pending)
2. For each group: LLM extracts entities + relations + summary
3. Create/update entity nodes in Qdrant (tag=entity)
4. Save wiki chunks to Qdrant (tag=wiki) + markdown to disk
5. Mark originals as done
6. Log to wiki/log.md
"""

import json
import re
import time
import os
from pathlib import Path
import config
import memory
import providers
import logger

_log = logger.get("synthesis")

WIKI_DIR = config.DATA_DIR / "wiki"
WIKI_DIR.mkdir(exist_ok=True)

_EXTRACT_PROMPT = """Extract entities, relations, and a summary from this text.
Reply ONLY with valid JSON (no markdown, no explanation):
{
  "entities": [
    {"name": "EntityName", "type": "technology|person|project|concept|place|event", "description": "one line description"}
  ],
  "relations": [
    {"from": "Entity1", "to": "Entity2", "rel": "uses|built_on|part_of|works_on|related|depends_on|prefers|alternative|language|instance_of"}
  ],
  "summary": "2-3 sentence synthesis of the key information in this text"
}

Rules:
- Extract 2-8 entities (most important ones)
- Relations connect entities FROM this text
- Summary should be a standalone paragraph
- Entity names should be normalized (e.g. "FastAPI" not "fastapi framework")
"""


def run_synthesis() -> str:
    """Main entry point. Process all pending synthesis items.

    Returns summary string of what was done.
    """
    if not config.get("synthesis_enabled"):
        return "Synthesis disabled"

    max_items = config.get("synthesis_max_per_run")
    pending = memory.get_pending_synthesis(limit=max_items)

    if not pending:
        _log.info("synthesis: no pending items")
        return "No pending items"

    _log.info(f"synthesis: processing {len(pending)} groups, {sum(len(v) for v in pending.values())} chunks")

    client = providers.get_client()
    model = providers.get_model()
    results = []

    for group_name, chunks in pending.items():
        try:
            result = _process_group(client, model, group_name, chunks)
            results.append(result)
        except Exception as e:
            _log.error(f"synthesis: group {group_name} failed: {e}")
            results.append(f"FAILED: {group_name} — {e}")

    # Log summary
    summary = _append_log(results)
    _log.info(f"synthesis complete: {summary}")
    return summary


def _process_group(client, model: str, group_name: str, chunks: list[dict]) -> str:
    """Process a single synthesis group (set of related chunks)."""
    # 1. Combine chunk texts
    full_text = "\n\n".join(c["text"] for c in chunks)
    if len(full_text) > 4000:
        full_text = full_text[:4000]  # limit for LLM context

    source = chunks[0].get("source", group_name)
    _log.info(f"synthesis: processing group '{group_name}' ({len(chunks)} chunks, {len(full_text)} chars)")

    # 2. LLM: extract entities + relations + summary
    extraction = _extract_entities(client, model, full_text)
    if not extraction:
        _log.warning(f"synthesis: extraction failed for {group_name}, skipping")
        return f"SKIP: {group_name} — extraction failed"

    entities = extraction.get("entities", [])
    relations = extraction.get("relations", [])
    summary = extraction.get("summary", "")

    _log.info(f"synthesis: extracted {len(entities)} entities, {len(relations)} relations")

    # 3. Save/update entity nodes
    for entity in entities:
        try:
            _upsert_entity(entity, relations)
        except Exception as e:
            _log.warning(f"synthesis: entity '{entity.get('name')}' failed: {e}")

    # 4. Save wiki chunks + disk
    if summary:
        _save_wiki(entities, relations, summary, source, chunks)

    # 5. Mark originals as done
    point_ids = [c["id"] for c in chunks if "id" in c]
    memory.mark_synthesized(point_ids)

    # 6. Update entity references in original chunks
    entity_names = [e["name"] for e in entities]
    _update_chunk_entities(point_ids, entity_names)

    return f"OK: {group_name} — {len(entities)} entities, {len(relations)} relations"


def _extract_entities(client, model: str, text: str) -> dict | None:
    """Call LLM to extract entities + relations + summary from text."""
    try:
        # Use high max_tokens — model may spend tokens on thinking before JSON
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a data extraction assistant. Output ONLY valid JSON. No markdown fences, no explanation."},
                {"role": "user", "content": _EXTRACT_PROMPT + "\n\nText:\n" + text},
            ],
            temperature=0.1,
            max_tokens=4096,
            stream=False,
        )
        msg = resp.choices[0].message
        content = msg.content or ""
        # Some models put everything in reasoning_content (Qwen 3.5 with thinking enabled)
        if not content.strip():
            reasoning = getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None) or ""
            if reasoning:
                content = reasoning
        _log.info(f"synthesis LLM response: {len(content)} chars, finish={resp.choices[0].finish_reason}")

        # Strip thinking tags if present
        content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL)
        content = re.sub(r"<\|channel\>thought\b.*?(?=<\|channel\>|$)", "", content, flags=re.DOTALL)
        content = re.sub(r"<\|[^>]+\>", "", content)

        # Strip markdown code fences
        content = content.strip()
        if content.startswith("```"):
            content = "\n".join(content.split("\n")[1:])
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        return json.loads(content)
    except json.JSONDecodeError as e:
        _log.warning(f"synthesis: JSON parse failed: {e}, content: {content[:200]}")
        # Try to find JSON in response
        m = re.search(r'\{.*\}', content, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
        return None
    except Exception as e:
        _log.error(f"synthesis: LLM extraction failed: {e}")
        return None


def _upsert_entity(entity: dict, all_relations: list[dict]):
    """Create or update an entity node in Qdrant."""
    name = entity.get("name", "").strip()
    if not name:
        return

    entity_type = entity.get("type", "concept")
    description = entity.get("description", "")

    # Find relations for this entity
    entity_relations = []
    for rel in all_relations:
        if rel.get("from") == name:
            entity_relations.append({"to": rel["to"], "rel": rel["rel"]})
        elif rel.get("to") == name:
            entity_relations.append({"to": rel["from"], "rel": f"inv_{rel['rel']}"})

    # Search for existing entity (RRF scores are ~0.01-0.06, not cosine 0-1)
    existing = memory.search(name, limit=1, tag="entity")

    if existing and existing[0]["score"] > 0.02 and existing[0]["text"].lower() == name.lower():
        # Update existing entity
        point = existing[0]
        old_relations = point.get("relations", [])
        # Merge relations (avoid duplicates)
        rel_set = {(r["to"], r["rel"]) for r in old_relations}
        for r in entity_relations:
            if (r["to"], r["rel"]) not in rel_set:
                old_relations.append(r)
        obs_count = point.get("observation_count", 0) + 1

        memory._save_single(
            text=name,
            tag="entity",
            dedup=True,
            meta={
                "entity_type": entity_type,
                "description": description or point.get("description", ""),
                "relations": old_relations,
                "observation_count": obs_count,
                "last_synthesized": time.time(),
                "synthesis_status": "done",
            },
        )
        _log.info(f"synthesis: updated entity '{name}' (obs={obs_count}, rels={len(old_relations)})")
    else:
        # Create new entity
        memory._save_single(
            text=name,
            tag="entity",
            dedup=False,
            meta={
                "entity_type": entity_type,
                "description": description,
                "relations": entity_relations,
                "observation_count": 1,
                "last_synthesized": time.time(),
                "synthesis_status": "done",
            },
        )
        _log.info(f"synthesis: created entity '{name}' ({entity_type})")


def _save_wiki(entities: list[dict], relations: list[dict],
               summary: str, source: str, chunks: list[dict]):
    """Save wiki chunk to Qdrant + markdown to disk."""
    entity_names = [e["name"] for e in entities]

    # Save wiki chunk to Qdrant (searchable)
    wiki_text = summary
    memory._save_single(
        text=wiki_text,
        tag="wiki",
        dedup=True,
        meta={
            "wiki_page": source,
            "wiki_section": "summary",
            "synthesis_sources": [source],
            "entities": entity_names,
            "synthesis_status": "done",
        },
    )

    # Write markdown to disk for each main entity
    for entity in entities[:3]:  # top 3 entities get wiki pages
        name = entity["name"]
        slug = name.lower().replace(" ", "_").replace("/", "_")
        filepath = WIKI_DIR / f"{slug}.md"

        # Build relations section
        entity_rels = [r for r in relations if r.get("from") == name or r.get("to") == name]
        rels_text = "\n".join(f"- {r['from']} --{r['rel']}--> {r['to']}" for r in entity_rels)

        # Build wiki page
        page = f"# {name}\n\n"
        page += f"**Type:** {entity.get('type', 'concept')}\n"
        if entity.get("description"):
            page += f"**Description:** {entity['description']}\n"
        page += f"\n## Summary\n\n{summary}\n"
        if rels_text:
            page += f"\n## Relations\n\n{rels_text}\n"
        page += f"\n## Sources\n\n- {source} ({len(chunks)} chunks, synthesized {time.strftime('%Y-%m-%d %H:%M')})\n"

        # Append if exists, create if not
        if filepath.exists():
            existing = filepath.read_text(encoding="utf-8")
            # Append new source section
            page = existing + f"\n\n---\n\n## Update ({time.strftime('%Y-%m-%d')})\n\n{summary}\n\n### Sources\n- {source}\n"

        filepath.write_text(page, encoding="utf-8")
        _log.info(f"synthesis: wrote wiki page {filepath}")

    # Update index.md
    _update_index()


def _update_index():
    """Rebuild wiki/index.md from existing wiki files."""
    index_path = WIKI_DIR / "index.md"
    lines = ["# Knowledge Wiki Index\n"]
    lines.append(f"Last updated: {time.strftime('%Y-%m-%d %H:%M')}\n")

    for f in sorted(WIKI_DIR.glob("*.md")):
        if f.name in ("index.md", "log.md"):
            continue
        name = f.stem.replace("_", " ").title()
        lines.append(f"- [{name}]({f.name})")

    index_path.write_text("\n".join(lines), encoding="utf-8")


def _update_chunk_entities(point_ids: list[str], entity_names: list[str]):
    """Update original chunks with extracted entity names."""
    qc = memory._get_qdrant()
    for pid in point_ids:
        try:
            qc.set_payload(
                config.QDRANT_COLLECTION,
                payload={"entities": entity_names},
                points=[pid],
            )
        except Exception:
            pass


def _append_log(results: list[str]) -> str:
    """Append synthesis run summary to wiki/log.md."""
    log_path = WIKI_DIR / "log.md"
    timestamp = time.strftime("%Y-%m-%d %H:%M")

    ok_count = sum(1 for r in results if r.startswith("OK"))
    fail_count = sum(1 for r in results if r.startswith("FAILED"))
    skip_count = sum(1 for r in results if r.startswith("SKIP"))

    summary = f"{ok_count} processed, {fail_count} failed, {skip_count} skipped"

    entry = f"\n## {timestamp}\n\n{summary}\n\n"
    for r in results:
        entry += f"- {r}\n"

    if log_path.exists():
        existing = log_path.read_text(encoding="utf-8")
        log_path.write_text(existing + entry, encoding="utf-8")
    else:
        header = "# Synthesis Log\n\nChronological record of knowledge graph synthesis runs.\n"
        log_path.write_text(header + entry, encoding="utf-8")

    return summary
