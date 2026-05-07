# v0.18.3 — Skill creator actually works for engineering tasks + camera tuning

Patch release driven by a live field session that took the skill_creator from "generates broken stubs that fail validation" to "generates working multi-tool engineering skills with raw OpenCV capture, statistics math, and time-series SQLite tables — from a single chat prompt". Plus two fixes for chat UI annoyances and new resolution/quality controls in Settings → Camera.

## 🤖 Skill creator now generates working engineering skills

Three coupled fixes that landed in sequence as the field session exposed each layer of the breakage.

### Soul rule 14 — agent must STOP after `create_skill`

Symptom: agent invoked `create_skill` (correct) but in the same turn ran 20+ shell commands and `write_file` to manually build the skill in parallel. The pipeline message says "started in background, will notify in 2-5 min" but the agent didn't trust it. Result: a half-baked manually-written `.py` clobbered or raced the pipeline's output, and the file didn't satisfy the skill loader contract (no `TOOLS` list, no `execute()` function, hardcoded paths, ungrouped table names).

Rule 14 now contains four critical sub-rules:
- After `create_skill` → END the turn. Don't run more tools that turn. Notification arrives later.
- NEVER `write_file` in `~/.qwe-qwe/skills/`. Manual writes don't satisfy the loader.
- Skills are SINGLE `.py` files at `~/.qwe-qwe/skills/<name>.py` — not directories. Don't `mkdir` or `ls` a skill subdir.
- "Run `<skill_name>`" = call one of the skill's tools directly. If unsure of tool names → `list_skills`.

### Pipeline elif→if when all tools are custom

Symptom: every attempt failed with `syntax error on attempt N: invalid syntax (<unknown>, line 70)` — same line across attempts. Field session gave 6 attempts, all the same. Generated `.py` had `elif name == "..."` as the first branch, no preceding `if`.

Root cause: when `_assemble_from_mapping()` finds no recognisable CRUD ops in the tools list (snapshot, trend, recommend, benchmark, etc. are all "custom"), the deterministic execute_body comes out EMPTY. The LLM was then asked to "Generate ONLY elif blocks for these tools… Start each with 'elif name == ...'". With no preceding `if`, that's a `SyntaxError`.

Two-part fix in `_run_pipeline`:
- **Prompt-level**: detect whether `execute_body` is empty BEFORE calling the LLM. If empty, tell it to start the FIRST tool with `if name == "..."` and the rest with `elif`. Also stricter wording: "FULL implementation (no stub `pass`)" and "All code for a tool MUST be indented under its branch".
- **Defensive post-process**: regex-rewrite the first occurrence of `elif name ==` to `if name ==` whenever `execute_body` was empty. Idempotent on already-correct output.

### End-to-end pipeline test

`test_skill_creator_pipeline.py` (NEW) — three tests with mocked LLM that drive `_run_pipeline` end-to-end:
- happy-path camera-using skill — asserts the produced file calls `tools.execute("camera_capture", ...)` + `memory.save()` + uses `skill_<name>_` table prefix + parses cleanly + passes smoke test
- 3-attempt retry: first plan call returns garbage, retry, succeed
- elif-first regression shield — feeds the exact buggy LLM output from the field session, asserts post-process rewrites to `if`

Field result after these fixes: created `camera_diagnostics` from a chat prompt — 5580-byte skill with 3 tools (`camera_benchmark`, `camera_health`, `camera_baseline_reset`), direct `cv2.VideoCapture(0)` for raw frame grab, `statistics.stdev()` for FPS variance, properly-namespaced `skill_camera_diagnostics_benchmarks` table, baseline comparison logic. Worked first try (190s pipeline). Real engineering skill generated from a paragraph of natural-language description.

## 🖱 Chat UI: task_update no longer creates ghost streaming messages

Symptom: after invoking `create_skill`, the chat indicator stayed in "generating" state with the typing dot blinking forever, even after the original turn completed.

Root cause: `skill_creator._notify()` broadcasts pipeline progress as `{type: "task_update", name, text}` over WS. The client's `handleWsMessage` had a fall-through branch that fired for ANY non-status event, including `task_update`, treating each "Step 2/5: generating tools" as the start of a new agent turn. Since `task_update` has no follow-up `done` event, `state.streaming.streaming` stayed `true` forever.

Fix: handle `task_update` explicitly at the top of `handleWsMessage` — surface as a toast (✅/❌ styling based on the message text), return before the streaming-message-creation gate. Side benefit: user now actually SEES skill creation progress as toasts (`camera_diagnostics: Step 2/5: generating tools` → `camera_diagnostics: ✅ Created and enabled!`) instead of getting zero UI feedback during the 2-5 minute background pipeline. Plus auto-refresh of the skills list on success so new tools appear without manual reload.

## 📷 Camera: configurable resolution + JPEG quality

Two new settings in Settings → Camera → Capture quality:

**`camera_resolution`**: `auto` / `480p` / `720p` / `1080p`. Applied to `cv2.VideoCapture` via `CAP_PROP_FRAME_WIDTH`/`CAP_PROP_FRAME_HEIGHT` on device open. Each preset also carries a max-pixels cap for the resize step before JPEG encoding — so users picking 1080p don't end up with the legacy 256×192 default cap:

| Preset | Capture | Sent to LLM |
|---|---|---|
| auto | camera default | up to 256×192 (49K pixels) |
| 480p | 640×480 | up to 256×192 (49K pixels) |
| 720p | 1280×720 | up to 512×384 (196K pixels) |
| 1080p | 1920×1080 | up to 1024×768 (786K pixels) |

**`camera_quality`**: int 1-100, default 70. Replaces the hardcoded `cv2.IMWRITE_JPEG_QUALITY=70`. Read on every encode (no restart needed); resolution requires a camera reset to re-open with the new size.

Trade-off the user controls now: 1080p + quality 90 sends a sharp ~1MB base64 to the vision LLM (slow, expensive, but readable text + tiny details). auto + quality 50 sends a cheap 256×192 thumbnail (fast, cheap, blurry). 9 unit tests pin the preset table, helper behaviour, exception handling, and config metadata.

## 🔄 Upgrading

```bash
git pull && pip install -e .
# restart cli.py --web for camera + skill_creator fixes
# hard-reload the browser tab (Ctrl+Shift+R) for the UI fix
```

No data migration needed.

## 📊 Stats

- 5 commits since v0.18.2
- 394 → 409 tests passing (+15: 4 pipeline e2e tests, 9 camera-settings tests, +2 minor)
- All fixes verified live — `camera_diagnostics` skill generation went from 100% failure rate to first-try success after the elif→if fix landed

## 🐞 Filed as follow-up issues

Tech debt surfaced during the session — open for community contributions:

- [#13](https://github.com/deepfounder-ai/qwe-qwe/issues/13) (good first issue): `smoke_test` param-usage check looks at whole source instead of `execute()` body — let a real `num_samples` vs `samples` mismatch slip through
- [#14](https://github.com/deepfounder-ai/qwe-qwe/issues/14) (help wanted): AST-level fix for code outside `if`/`elif` branches — the second half of the LLM-codegen failure mode, prompt-only mitigated for now
- [#15](https://github.com/deepfounder-ai/qwe-qwe/issues/15) (help wanted): `delete_skill` leaves orphan tables in shared SQLite
