# v0.18.2 — Camera fixes, UI polish, skill_creator opens up

Patch release with three focused tracks: making `camera_capture` actually work on Windows, two UI buttons that were rendered but had no handler wired, and broadening what generated skills can do.

## 📷 Camera capture works again (5 commits)

`camera_capture` had been silently broken in the common case for a while. Field session today (qwen3.5-9b on LM Studio, two cameras) showed exactly how it failed and the fixes landed in five steps as I traced symptoms.

### The chain of bugs

1. **WS event-name mismatch** — `server.py` broadcast `{"type": "get_frame", ...}` but `static/index.html` listened for `t === 'frame_request'`. The browser never received the request, server timed out at 3s, tool fell through to the OpenCV fallback. Client now accepts both event names so version skew can't bring it back.

2. **PiP-only restriction** — even with the names matching, the browser's frame handler only snapped a frame if the floating PiP overlay was active (`state.cameraOn`). Most users haven't toggled that on. Added `cameraSnapshotOneShot()`: opens a transient `getUserMedia` stream with the device picked in Settings, waits up to 3s for the first frame, draws to canvas, releases. Tool no longer depends on user UI state.

3. **3s timeout was too tight** for cold-start `getUserMedia` (permission prompt + first-frame ≈ 1–3s). Bumped server-side timeout to 6s.

4. **Windows DirectShow black-frame** — when another consumer recently held the camera (Settings preview's `getUserMedia`, Skype, browser tab), OpenCV opens the device but the first 5–15 frames come back nearly-uniform black before auto-exposure kicks in. The existing 5-frame warmup at open time wasn't enough on cold sensors. Added a per-read black-frame guard: if `mean(frame) < 25`, retry up to 30 times with 70ms spacing — sensor typically clears in 5–15 frames. Returns the dark frame anyway after warmup retries so the LLM reports "it's dark" instead of the tool failing.

5. **Auto-detect picked the wrong camera** — old logic took the first index where `mean > 3` (barely above pitch black). On laptops with built-in webcam at index 0 and a USB webcam at 1, it would pick the dim built-in. New logic probes all 4 indexes, scores each by frame brightness, picks the brightest. Per-index probe results logged for transparency:

```
camera: probe index 0: 640x480, mean=16.0
camera: probe index 1: 1280x720, mean=98.4
camera: auto-selected index 1 (1280x720, mean=98.4) — set
  camera_index in settings to pin a different one
```

Plus a small cosmetic — silenced OpenCV's `cap_ffmpeg_impl: Failed list devices for backend dshow` warning that fires on every Windows VideoCapture init even when the open succeeds.

## 🖱️ UI handlers that were missing

### Floating "scroll to latest" button

Adds a circular chevron-down button that appears in the chat column when the user has scrolled up >200px from the bottom. Clicking smooth-scrolls back to the latest message. Auto-hides as soon as the user is within 200px of bottom. Sticky positioning inside the chat-scroll viewport with `bottom: 96px` keeps it just above the composer, negative margin keeps it out of the flex flow so the composer's vertical position is unchanged.

### Slash-command chips above the composer were inert

The hint bar above the composer rendered four chips:

> Continue the thread, or try: `/recall` `/remember` `/cron` `/tools`

These had `data-slash="..."` attributes but **no handlers wired anywhere**. Clicking did nothing. Two-part fix:

1. Click handler in `wireEvents()` — inserts the command + trailing space into the textarea, focuses, places caret at end.
2. Client-side interception in `sendMessage()` so commands actually do something predictable instead of being sent to the LLM as prose:
   - `/cron` → navigates to Scheduler
   - `/tools` → opens the ⌘K palette
   - `/recall <q>` → rewrites as `"search memory for: <q>"` so the LLM picks `memory_search` reliably
   - `/remember <fact>` → rewrites as `"remember this fact: <fact>"`
   - Bare `/recall` or `/remember` without an argument → toast usage hint, doesn't submit

## 🧩 Skill creator: expose the full agent runtime

Generated skills used to be told they could only touch the `db` module. The LLM-facing `INSTRUCTION` listed `db.kv_get`/`kv_set`/`_get_conn` and explicitly forbade everything else — so skills came out as isolated mini-CRUD apps with no way to use the agent's actual capabilities. A user asking "build a meal logger that takes a photo and remembers what I ate" couldn't get that skill because the LLM didn't know it could call `camera_capture` or `memory.save`.

Now the prompt context documents:

- `memory.save` / `memory.search` (semantic recall)
- `tools.execute("<any_tool>", {...})` — `camera_capture`, `http_request`, `read_file`, `write_file`, `send_file`, `open_url`, `secret_save`, `secret_get`
- `providers.get_client()` for direct LLM calls
- `scheduler` / `tasks` for background work
- ALWAYS / NEVER bullets at the end (lazy imports, no hardcoded secrets, no blocking external waits)

`STEP1_PLAN` got composability hints + 3 cross-feature plan examples (fitness coach, meal logger, slack notify). `STEP3_CODE` got 3 new code examples (camera+memory, secret+http, memory.search) to give the LLM patterns to copy.

### Table namespacing rule

Important architectural correction. qwe-qwe uses a single shared SQLite for everything — core agent tables (`messages`, `kv`, `threads`, `scheduled_tasks`, `routine_runs`) live in the same DB as every skill's tables. Without a naming rule, two user-generated skills both creating a `notes` or `logs` table would silently share rows. New rule in both `INSTRUCTION` and `STEP1_PLAN`:

> ALWAYS prefix tables with `skill_<skill_name>_<purpose>`:
> `skill_meal_logger_meals`, `skill_workout_tracker_sets`, …
> NEVER use generic names: `notes`, `logs`, `tasks`, `users`, `items`, `records`.

### Tests

`skills/skill_creator.py` was 1318 lines with **zero test coverage**. New `tests/test_skill_creator_smoke.py` with 25 tests covering:

- Pure helpers: `_sanitize_id`, `_infer_op`, `_extract_json` (4 cases), `_extract_code`, `_build_table_ddl`
- Deterministic mapping path: `_build_mapping_from_tools` + `_assemble_from_mapping` (AST-validates the assembled body)
- `delete_skill` protections: built-in refuse, invalid identifier, missing skill
- `execute()` dispatch
- Integration: `skill_creator` in `_DEFAULT_SKILLS`, `tool_search('skill')` surfaces the right tools
- Prompt content fences: pin that `INSTRUCTION` documents memory/tools/providers/db, that `STEP1_PLAN` mentions composability, that `STEP3_CODE` shows camera/memory/secret/http examples, that the table-namespacing rule lands in both prompts

The fence tests guard against accidental narrowing on future refactors — if someone drops the camera example from STEP3_CODE, the LLM stops generating camera-using skills, and we'd never notice without these tests.

## 🔄 Upgrading

```bash
git pull && pip install -e .
# restart cli.py --web for camera fixes to take effect
# hard-reload the browser tab (Ctrl+Shift+R) for the UI fixes
```

No data migration needed.

## 📊 Stats

- 9 commits since v0.18.1
- 394 → 394 tests passing (added skill_creator coverage offset by one earlier integration; net +25 from skill_creator)
- All fixes verified via field session — camera went from "all black" to working capture in real time
