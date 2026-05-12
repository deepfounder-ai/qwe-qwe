# Importing skills from skills.sh / GitHub

qwe-qwe supports importing community skills from **[skills.sh](https://skills.sh)** and any compatible GitHub repository that follows the [agentskills.io SKILL.md spec](https://agentskills.io/specification). The same skills used by Claude Code / Claude.ai work in qwe-qwe via a thin adapter layer.

## How to import

### Web UI

Settings → Tools & skills → **Import skill** button. Paste a URL, click Import.

Recognised URL shapes:

- `https://skills.sh/<owner>/<repo>/<skill-name>`
- `https://github.com/<owner>/<repo>/tree/<ref>/<path-to-skill>`
- `https://raw.githubusercontent.com/<owner>/<repo>/<ref>/<path-to-skill>/SKILL.md`

### REST

```bash
curl -X POST http://localhost:7861/api/skills/import \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://skills.sh/anthropics/skills/pdf"}'
```

Response on success:

```json
{
  "name": "pdf",
  "description": "Read, write, edit, and extract content from PDF files.",
  "license": "Complete terms in LICENSE.txt",
  "source_url": "https://skills.sh/anthropics/skills/pdf",
  "py_path": "~/.qwe-qwe/skills/pdf.py",
  "assets_dir": "~/.qwe-qwe/skills_imported/pdf",
  "files_imported": ["SKILL.md", "scripts/extract.py", "references/api.md"],
  "tools_count": 1,
  "hash": "sha256-..."
}
```

## What gets installed

For each imported skill, two paths land on disk:

| Path | Contents |
|---|---|
| `~/.qwe-qwe/skills/<name>.py` | A generated **adapter module** that qwe-qwe's skill loader picks up. Exposes ONE tool: `<name>_help` returning the SKILL.md body verbatim. |
| `~/.qwe-qwe/skills_imported/<name>/` | The original SKILL.md plus any whitelisted assets (`.md` / `.py` / `.sh` / `.js` / `.json` / `.yaml` / `.html` / `.css` / `.csv` / `.sql`). Binaries and images are skipped. |

Provenance is recorded in the `skill_imports` SQLite table (source URL, hash at import time, license, timestamp) so a future "check for upstream updates" workflow can compare.

## What the adapter does — and what it doesn't

skills.sh skills are **markdown instructions for an LLM**, plus optional executable scripts. qwe-qwe skills are **single Python modules with `TOOLS` + `execute()`**. The bridge:

1. The adapter's `INSTRUCTION` (injected into the system prompt when the skill is active) is a short pointer: "call `<name>_help` for the full body."
2. The `<name>_help` tool returns the full SKILL.md markdown.
3. **The agent follows the SKILL.md instructions using its existing tools**. If SKILL.md says "use `scripts/extract.py`", the agent calls `read_file` to view it, then `shell` to run it. qwe-qwe doesn't have a sub-runtime that auto-executes imported scripts — the agent treats them as files like any other.

This means imported skills work best for **knowledge / procedure-heavy** capabilities. Pure-code tools (e.g. an HTTP wrapper around a specific API) are still better written as native qwe-qwe skills via `create_skill`.

## Security

### Domain allowlist

URLs must point at one of: `skills.sh`, `github.com`, `raw.githubusercontent.com`, `api.github.com`. Arbitrary hosts are rejected with `host_not_allowed` (HTTP 403).

### SSRF guard

URLs that resolve to private / loopback / link-local IPs (DNS rebinding caught via `socket.getaddrinfo`) are rejected with `private_ip` (HTTP 403). Override with `QWE_ALLOW_PRIVATE_URLS=1` for self-hosted GitHub Enterprise / mirrors — same env var as `/api/knowledge/url`.

### Name validation

The skill `name` from frontmatter must match `^[a-z0-9]+(-[a-z0-9]+)*$` (the agentskills.io spec) and be ≤64 characters. Names with uppercase, underscores, leading/trailing/consecutive hyphens are rejected.

### Collision protection

- **Built-in skills are NOT overridable.** `browser`, `canvas`, `skill_creator`, `serial_port`, `soul_editor`, `mcp_manager`, `timer`, `notes`, `weather`, `spicy_duck` cannot be replaced by import — this is the **typosquatting defense**. Even `overwrite=true` won't bypass.
- **User skills require explicit overwrite.** If you've already installed `pdf-helper`, re-importing requires `overwrite: true`. The UI shows a confirmation checkbox.

### License surfacing

If the upstream skill's `license` field doesn't match a known OSS marker (Apache, MIT, BSD-*, GPL-*, LGPL, MPL-2, ISC, Unlicense, CC0), the import returns HTTP **451 with `license_confirm_required`** and the license text in `details.license`. The Web UI shows a confirmation panel; CLI users re-POST with `accept_license: true`.

This matters because several skills.sh skills (notably Anthropic's `docx`, `pdf`, `pptx`, `xlsx`) are **source-available, not open-source** — their LICENSE.txt restricts commercial use. qwe-qwe doesn't enforce a policy on top; it just makes sure you saw the license before installing.

### Size + budget caps

- SKILL.md body: 100 KB max.
- Total bytes fetched per skill (SKILL.md + assets): 1 MB max.
- Files per skill: 50 max.
- Excluded asset extensions: binaries, images, fonts, executables.

### Audit trail

The `skill_imports` table records `source_url`, `hash` (SHA-256 of SKILL.md at import time), `license`, `imported_at` for every install. List via `GET /api/skills/imports` or query the DB directly.

## Removing an imported skill

Web UI: same delete-button affordance as native skills (planned). REST:

```bash
curl -X DELETE http://localhost:7861/api/skills/imports/<name>
```

Removes the adapter `.py`, the staged `skills_imported/<name>/` directory, and the provenance record.

## What's NOT supported (yet)

- **Auto-update from upstream.** The `hash` is recorded but no `qwe-qwe --update-imported-skills` workflow exists. Re-import with `overwrite: true` to pull a fresh copy.
- **Direct `npx skills add` cli compatibility.** That CLI writes to the same `~/.qwe-qwe/skills/` paths but doesn't go through our validation / collision / SSRF layers. Stick to the REST endpoint or Web UI button.
- **Skills that ship binary assets** (fonts, images, model weights). Those are filtered out by the asset whitelist — only text / code is staged. If you need binary assets, fetch them manually via the agent's `http_request` tool after import.
- **Private GitHub repos.** No token auth on the fetch path. PRs welcome.

## Reference implementations

| Source | Notes |
|---|---|
| `anthropics/skills` | The reference repo. Skills under `skills/document/` (pdf, docx, pptx, xlsx) are **source-available**, the rest are Apache-2.0. |
| `vercel-labs/agent-skills` | Vercel-curated mirror — same SKILL.md format. |
| Your own GitHub repo | Drop a `SKILL.md` + optional `scripts/` directory and import via the GitHub URL. |

Pattern docs for skills.sh authors: <https://agentskills.io/specification>.
