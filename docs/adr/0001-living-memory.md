# ADR-0001: Living Memory — mutating memory architecture for qwe-qwe

**Status:** Proposed
**Date:** 2026-04-24
**Deciders:** kir + claude

## Context

qwe-qwe's current memory layer is read-only after save. We have 3-way hybrid search (dense + sparse + BM25 → RRF fusion) on a Qdrant disk collection with three tag families: `knowledge/fact/user/...` (raw), `entity` (graph nodes), `wiki` (synthesized summaries). A nightly `synthesis.py` job promotes raw into wiki. No recall mutates anything. No salience decay. No typed relations. Entity tag has `relations` field but traversal is ad-hoc.

The `living-memory-architecture.md` proposal wants memory to behave more biologically: mutating at recall, typed connections, hierarchical abstractions, salience decay, crystallization of cold paths, archive-not-delete. User has refined 5 key decisions after design debate:

1. **Mutation happens nightly (batch), not on recall.** Online mutation doubles LLM cost and risks drift on short timescales.
2. **Drift monitoring kept.** Semantic distance warnings when a memory's current version has diverged >threshold from its original.
3. **Conflicts preserved, never auto-resolved.** When new info contradicts existing, both stay. At recall time, presence of conflict is surfaced in the returned context.
4. **Hierarchy pruning rules:** meta-level 3+ expires if unused for **months**; level 2 expires if unused for **years**. Atoms and level 1 chains follow regular salience decay.
5. **Cross-domain isolation.** Memories are scoped by top-level tag (`work/personal/project-X/…`). Cross-domain association disabled by default.

The unresolved decision: **should we extend Qdrant in-place OR build markdown-first layer that eventually replaces it?** Our existing 333 tests, secret scrubbing, hybrid RRF, and session isolation all live inside the Qdrant-based memory module.

**Constraints:**

- Local-first: must run offline on the user's machine. No cloud dependency.
- Computational cost matters: users run on 8GB RAM laptops. Embedding compute + LLM reflect calls is the ceiling.
- Must not break `memory_search`, `memory_save`, `memory_delete` tool contracts (agent relies on these).
- Must preserve secret scrubbing (`_scrub_secrets` in `memory.py`) — living markdown files can't leak API keys.
- Incremental delivery: can't afford a 2-month rewrite.

## Decision

**Option C — Hybrid: markdown as source of truth, Qdrant as derived search index.**

Memories live as `.md` files with YAML frontmatter under `~/.qwe-qwe/memories/{atoms,chains,meta}/`. Qdrant is rebuilt from the markdown and treated as a cache; editing markdown directly (or by the night job) triggers re-embed and payload update for that file's vector. All Living Memory semantics — mutation, salience, connections, crystallization — live in the markdown's frontmatter. Qdrant just stores the vector + a subset of fields for fast filtered search.

Existing tools (`memory_save`, `memory_search`, `memory_delete`) stay on their current contract — they now write/read through the markdown layer with auto-reindex. The 333 existing tests continue to pass because the public API is unchanged.

## Options Considered

### Option A: Extend Qdrant in-place

Add Living Memory fields to Qdrant payload (`salience`, `anchor`, `access_count`, `connections: [{id, relation}]`, `crystallization`). Night synthesis expands to cover decay / chain formation / meta promotion. Mutation writes back to the payload; vector re-embedded only if content changes.

| Dimension | Assessment |
|-----------|------------|
| Complexity | Low (incremental on existing code) |
| Cost | Zero new infrastructure |
| Scalability | Fine (Qdrant handles millions of vectors on disk) |
| Team familiarity | High (existing module) |
| Inspectability | **Poor** — payload is JSON inside Qdrant's opaque collection |
| Human editability | **None** — no way to hand-edit a memory |
| Version history | None native; bolt-on git for DB files useless (binary) |

**Pros:**
- Every feature (anchors, salience, chains) is independently shippable
- Reuse hybrid RRF, secret scrubbing, session isolation unchanged
- Fast recall stays at Qdrant latency (<10ms)
- No migration — existing user data just grows new fields

**Cons:**
- **Loses the UX win Living Memory promises** — you can't open `~/.qwe-qwe/memory/fact_foo.md` in an editor, because it doesn't exist
- Graph traversal (chain BFS) is awkward — N queries per hop
- No "git diff of memory drift over a week" because there are no files to diff
- Schema-less JSON in payload will calcify; no enforcement

### Option B: Markdown-first, replace Qdrant entirely

Greenfield rewrite: markdown files are canonical, recall uses a new search engine (grep + custom FTS + optional lightweight vector store). Qdrant removed.

| Dimension | Assessment |
|-----------|------------|
| Complexity | Very high (reimplement hybrid search, migration tooling, re-test) |
| Cost | 6-8 weeks of work minimum |
| Scalability | Risky (grep degrades past ~10k memories without a custom index) |
| Team familiarity | Low (new search layer from scratch) |
| Inspectability | **Perfect** — every memory is a markdown file |
| Human editability | **Perfect** — users edit with any editor |
| Version history | Free via git |

**Pros:**
- Architectural purity — aligns with the philosophical goal of Living Memory
- No stale "cache vs source" duality
- Maximum transparency and user agency

**Cons:**
- **Months of rework.** Secret scrubbing, hybrid RRF, session isolation, tests all need reimplementation.
- Losing the proven hybrid search path is a real quality regression
- User's existing memory data requires a migration step that may lose vectors
- Re-embedding entire corpus on first boot after migration is slow
- Higher operational risk for a feature whose value is unproven at this scale

### Option C: Hybrid — markdown canonical, Qdrant derived (RECOMMENDED)

Memories physically live as `.md` under `~/.qwe-qwe/memories/`. Qdrant collection rebuilt from markdown; each file's frontmatter `id` is the Qdrant point id. Content hash in frontmatter lets the indexer skip unchanged files on rebuild.

Mutation path (nightly):
```
read .md → compute new content via LLM reflect →
write new .md (preserving id, updating last_accessed, salience, connections) →
re-embed if content changed → upsert Qdrant point
```

Old version archived via git commit (automatic on every write).

Public tool contracts unchanged — `memory_save(text)` now writes a `.md` file AND upserts Qdrant; `memory_search(query)` hits Qdrant hybrid RRF as before; `memory_delete` removes both.

| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium (new storage layer behind existing API) |
| Cost | 3-4 weeks for full Living Memory feature set |
| Scalability | Good (Qdrant handles hot path; markdown pagination for cold browse) |
| Team familiarity | Partial — reuses Qdrant, introduces file-watching |
| Inspectability | **Good** — any editor works on `.md` |
| Human editability | **Good** — edit file → file watcher re-embeds on next sleep pass |
| Version history | **Good** — git commits on every nightly write |

**Pros:**
- Preserves fast recall (Qdrant stays) AND inspectability (markdown canonical)
- Existing `_scrub_secrets` runs on `.md` content before save — same guarantee
- Session-isolation remains Qdrant filter-based
- 333 existing tests stay passing because API contracts unchanged
- Incremental delivery: add living features one at a time on top of existing storage
- Qdrant becomes rebuildable from source of truth → deleting `~/.qwe-qwe/memory/` (Qdrant dir) is no longer a disaster
- Native git integration (we already integrity-block `.git` writes; memory dir stays outside)

**Cons:**
- Two places to keep in sync (markdown + Qdrant)
- Re-embedding on edit adds ~100ms per file (CPU FastEmbed) — fine for nightly batch, not for per-keystroke
- Storage doubles (markdown text + Qdrant vectors) — for typical usage (~10k memories × 1KB md + vectors) well under 100MB

## Trade-off Analysis

**A vs C — why not stay with Qdrant-only?**
The strongest user-facing claim in Living Memory is *"your memory is inspectable, editable, diffable"*. Option A can't deliver that — the "memory" exists only as opaque Qdrant payload entries. Users can't even see what they remembered without writing code. The whole thing becomes yet another internal database, and the architectural ambition collapses into "Qdrant with decay."

**B vs C — why not go full markdown?**
Option B throws out the 3-way hybrid search we already have working, including the recent (v0.17.28) fix that made auto-recall thresholds meaningful. Rebuilding that from scratch in 4 weeks is risky; doing it in 8 weeks is a distraction from the actual value proposition (mutation, drift-aware recall). Better to keep proven infrastructure and add the new semantic layer on top.

**Computational budget.**
User's key refinement #1 (mutation at night, not on recall) is the crucial cost-safeguard. With Option C, nightly reflection loops through N `.md` files, calls the LLM per memory batch (grouped), writes back. Cost: O(memories × reflect_tokens_per_memory × nightly_freq). If a user has 500 active memories and we reflect on 50 per night (the ones accessed that day), that's 50 × ~500 tokens = 25k tokens per night. On Claude Sonnet: ~$0.15/night. On local Llama/GLM: free. Acceptable either way.

**Drift risk (refinement #2).**
Drift is only dangerous in aggregate. Option C makes drift observable: git-diff `~/.qwe-qwe/memories/fact_foo.md` over a week shows the mutation trajectory. Options A and B obscure this in different ways (A: no files; B: files but no Qdrant sanity-check layer). Markdown + git + drift semantic-distance alarm = the right combination.

**Conflict preservation (refinement #3).**
Natural fit for Option C: a conflict is just TWO `.md` files, one with `connections: [{id: other-mem, relation: contradicts}]`. Recall returns both; injection layer adds a *"⚠ two conflicting memories on this topic, reconcile in your answer"* prefix. No auto-merge ever.

**Hierarchy pruning (refinement #4).**
Markdown frontmatter tracks `last_accessed` and `level`. Nightly sleep job runs:
```
if level >= 3 and months_since(last_accessed) > N_months_threshold: archive
if level == 2 and years_since(last_accessed) > N_years_threshold: archive
```
Archive = move `.md` to `archive/` subdir (no deletion, git retains history). Same semantics as the existing `_pending_files` sweep for uploads/.

**Cross-domain isolation (refinement #5).**
Add `domain: work | personal | project-X` to frontmatter. Qdrant filter on the domain when querying. Sleep job operates per-domain so consolidations stay in-domain. This is a 1-line Qdrant filter change once the domain field exists.

## Consequences

**What becomes easier:**
- User inspection — `ls ~/.qwe-qwe/memories/atoms/` is "what I remember"
- Debugging drift — git log on any memory file
- Manual override — user edits a `.md` directly, gets respected on next embed
- Testing — drop-in sample `.md` files in test fixtures
- Export / backup — just copy the directory

**What becomes harder:**
- Schema evolution — YAML frontmatter needs a validator to catch malformed memories after user edits. Propose: Pydantic model, warn-not-crash on invalid
- Atomicity — writing `.md` + re-embedding + Qdrant upsert is 3 steps. Use a `.md.tmp` → rename pattern, reconcile Qdrant on boot (any .md with no Qdrant point → re-embed)
- File-system limits — 10k+ markdown files in one directory is slow on Windows. Sharding: `atoms/aa/mem_aa_...md` by hash prefix

**What we'll need to revisit:**
- File watcher vs polling — real-time reindex on user edit vs "next sleep cycle picks it up." Polling is simpler; file-watching nicer but adds OS-specific deps (watchdog).
- Embedding cache invalidation — if user edits the BODY of a `.md`, re-embed. If they only change `last_accessed` metadata, don't. Content hash comparison handles this.
- Cold start / migration — existing Qdrant data needs to be exported to `.md` files on first boot after the feature ships. Scripted one-time job.

## Action Items

Phased delivery, each phase independently shippable and testable. Suggested order:

**Phase 1 — Storage foundation (1 week)**
1. [ ] Migration `006_memory_markdown.sql`: add `markdown_path TEXT` to any schema that needs file back-reference (may be none)
2. [ ] Create `memory_store.py` — thin wrapper over Qdrant that also reads/writes markdown canonically. `save(text, meta) → id, file_path`
3. [ ] Migrate existing `memory.save/search/delete` to go through `memory_store`. Existing tests must pass unchanged
4. [ ] Scripted one-time migration: export every Qdrant point to `.md` under `atoms/` with frontmatter; skip if already migrated (idempotent)
5. [ ] Integrity block extension: `~/.qwe-qwe/memories/` joins the protected-write list (only memory tools can write there, not `write_file`)

**Phase 2 — Living Memory frontmatter (3-4 days)**
6. [ ] Frontmatter schema: `id, type, level, created, last_accessed, access_count, salience, tags, domain, connections, trigger_pattern, crystallization, anchor`. Pydantic model for validation
7. [ ] `salience` decay formula (nightly); `reinforcement` on recall (in-memory counter, flushed nightly — not online writes)
8. [ ] `anchor: true` memories skip decay, skip mutation. 2 tests
9. [ ] Cross-domain filter in recall — Qdrant filter by `domain` field

**Phase 3 — Nightly mutation + consolidation (1-1.5 weeks)**
10. [ ] Extend `synthesis.py` → now `consolidation.py` with distinct phases: co-occurrence detection, reflect-mutation, chain formation, meta promotion, merge (for cosine > 0.9), decay-and-archive, index rebuild
11. [ ] Drift monitor: track `cosine(original_embedding, current_embedding)`. Warn at 0.3, alarm at 0.5
12. [ ] Git auto-commit on every nightly write (one commit per phase: `memory: nightly mutation 2026-04-25`)
13. [ ] `~/.qwe-qwe/memories/.git/` initialized on first run

**Phase 4 — Conflict preservation + hierarchy pruning (1 week)**
14. [ ] When new memory contradicts existing (detected during save via cosine + LLM judgment), write both; add `contradicts` relation
15. [ ] Recall surfaces conflicts: if returned set contains `contradicts` pair, prepend `⚠ Conflicting memories present` note
16. [ ] Pruning rules: level ≥ 3 + `last_accessed` > `months_threshold` → archive; level 2 + > `years_threshold` → archive. Archive = move to `archive/`, keep git history
17. [ ] Tests: construct dummy mem tree with dates in the past, assert archival

**Phase 5 — UI surface (3-4 days)**
18. [ ] Memory page (`/memory` or extend existing) — file tree of atoms/chains/meta with salience + last_accessed shown
19. [ ] Memory detail view — rendered markdown + connections graph view + drift plot (if we have it)
20. [ ] Inline edit — user can tweak a memory's body, save re-embeds on next sleep
21. [ ] Git log per memory — scrollable diff history

**Phase 6 — Drift dashboard + test harness (3 days)**
22. [ ] Periodic `tests/test_drift_corpus.py` — synthetic corpus, simulate 30 nights of mutation, assert drift stays under threshold on known-truth facts
23. [ ] Memory-health panel: conflict count, drift distribution, hierarchy depth, archive queue size

**Total:** ~3-4 weeks of focused work with a lot of room to iterate based on real usage.

## Rollback plan

If Living Memory misbehaves (runaway drift, chain explosion, cost spike):
- Anchor everything on: `find ~/.qwe-qwe/memories -name '*.md' -exec sed -i '/^salience:/d' {} \;` followed by `anchor: true` auto-add. Freezes the corpus.
- Disable nightly consolidation: `config.set("consolidation_enabled", False)`. No more mutations; recall falls back to today's behavior.
- Full revert: delete the `memories/` directory, `memory_store.py` returns to a thin Qdrant wrapper, system is back at v0.17.33 behavior.

## Open questions (deferred, not blockers)

- **Embedding provider lock-in** — we use FastEmbed (local). If we want to switch to OpenAI embeddings later, re-embedding is O(N memories). Mitigated by: store embedding provider + version in frontmatter, re-embed selectively on version mismatch.
- **File-watcher vs cron** — Phase 2-3 starts with "nightly re-scan" (cron). Only upgrade to file-watching if users complain about edit-lag.
- **Archive pruning** — once a memory is in `archive/` for N years unchanged, can it be garbage-collected? Probably yes, but requires explicit user command. Not part of Phase 1-6.
- **Multi-user** — deferred. Single-user cwd assumptions are baked in. If we go multi-user later, `memories/{user_id}/atoms/...` is the obvious shape.

---

*This ADR records the decision at one point in time; revisit after Phase 1-2 ships and we have real data on drift, cost, and user inspection patterns.*
