# v0.18.6 — Hardware support: serial / USB-COM (scales, scanners, GPS, label printers, PLCs)

The headline of this release is **first-class hardware integration** via the new built-in **`serial_port`** skill. qwe-qwe runs on a real machine — and your machine is plugged into real hardware. Cloud agents can't see your warehouse floor; qwe-qwe can. This release closes that gap.

Cross-platform via `pyserial`: same API on **Windows** (`COM3`), **macOS** (`/dev/tty.usbserial-…`), and **Linux** (`/dev/ttyUSB0`). Auto-active on every install — `tool_search("serial")` (or `"scale"`, `"modbus"`, `"rfid"`, `"barcode"`, `"gps"`, `"plc"`, `"hardware"`) unlocks the tools without any manual setup.

## 🔌 What this means in practice

The universe of devices that talk USB-serial / RS-232 / RS-485 is exactly the universe of hardware sitting in real businesses:

| Category | Examples | What qwe-qwe can now do |
|---|---|---|
| **Weighing scales** | Mettler Toledo, CAS, Ohaus | Read weight, log it, route to ERP / Telegram, trigger reorder thresholds |
| **Barcode / RFID readers** | Datalogic, Honeywell (RS-232), UHF inventory readers | Receiving, stocktake, asset tracking, real-time inventory |
| **GPS modules** | u-blox NEO-6M / 9M | Fleet tracking, geofence triggers, route logging |
| **Label printers** | Zebra (ZPL), TSC, Godex (ESC/P) | "Print this shipping label for the order I just packed" |
| **Receipt printers** | Epson / Star / Bixolon (ESC/POS) | POS, kitchen tickets, queue numbers |
| **Industrial PLCs** | Anything Modbus RTU / RS-485 | Read sensors, command motors, monitor production lines |
| **VFDs / inverters** | Modbus-RTU drives | Pump and motor control with safety gating |
| **Environmental sensors** | pH, conductivity, gas, humidity, temperature | Greenhouses, labs, cold-chain monitoring |
| **Energy meters** | RS-485 with DLMS/COSEM | Plant-floor power monitoring, billing |
| **Cash drawers / fingerprint scanners / SCUD turnstiles** | Standard serial-trigger devices | POS, time tracking, access control |

### Three concrete scenarios

**1. Weighing flow — warehouse digitization in 30 seconds of conversation:**

```
You:    Scales on COM3, baud 9600. When something is weighed, ask me what
        it is on Telegram and remember it.
Agent:  [tool_search("scale")] → 3 serial tools activated
        [serial_list_ports] → "COM3 — USB-Serial CH340 (wch.cn)"
        [serial_read_once port=COM3 until="newline"] → "ST,GS,+00012.345,kg"
        [telegram_notify_owner "Weighed 12.345 kg — what was it?"]
        [memory_save "Weighed 12.345 kg of pears, 2026-05-09 14:32"]
```

A week later you ask "сколько яблок я завесил в среду?" and the answer comes from memory. No ERP integration, no custom dashboard — just chat + scales.

**2. Modbus RTU on a VFD with safety gate:**

```
You:    Read frequency setpoint from the VFD on COM5 (Modbus addr 1, reg 0x2000).
Agent:  [serial_read_once] constructs "01 03 20 00 00 01 8E 0B" frame,
        decodes response → "Setpoint: 50.0 Hz"

You:    Set it to 30 Hz.
Agent:  [serial_write data="01 06 20 00 01 2C ..." confirm=false]
        DRY RUN — pass confirm=true to actually send.
          port: COM5  bytes: 8  hex: 010620000 12C...
        This will write 30.0 Hz (0x012C) to register 0x2000.
        Confirm to proceed.
You:    yes
Agent:  [serial_write ... confirm=true] → "Wrote 8 bytes to COM5."
```

**Every actuator write is gated by `confirm=true`.** Default is dry-run that shows a hex preview without opening the port. Read-only on dry-run, ack-then-act on live. Set wrong, a Modbus write can damage motors or open valves — the gate is mandatory.

**3. GPS-triggered scheduled routine:**

```
You:    Every 5 min read GPS on COM4. If I'm within 200m of the warehouse
        coordinates (52.0123, 4.5678) — ping me on Telegram.
Agent:  [tool_search("schedule")] [tool_search("gps")]
        [schedule_task every="5m"] →
          serial_read_once port=COM4 until="newline"
          parse $GPRMC, haversine, telegram_notify_owner if <200m
        Routine #4 scheduled.
```

These three patterns — read sensors → route signals, command actuators with safety, schedule polling — cover the majority of real shop-floor automation.

## ⚙️ How it ships

### Three tools, gated by `tool_search`

```python
serial_list_ports                                 # discovery
serial_read_once(port, baud, until, format)       # text or hex frames
serial_write(port, data, format, confirm=True)    # safety-gated writes
```

`format="hex"` works for any binary protocol (Modbus RTU, vendor frames). Whitespace, colons, commas, semicolons are stripped automatically — paste a copied hex stream from a manual and it parses.

### Cross-platform error handling

`Permission denied` and `Could not open port` get translated into platform-aware advice:

- **Linux**: "Run `sudo usermod -aG dialout $USER` and re-login."
- **macOS**: "Apple Silicon CH340 cables need the WCH driver: <link>."
- **Windows**: "Check Device Manager — yellow triangle means missing driver."

### Doctor check

`qwe-qwe --doctor` (or auto-run on startup) now reports:

```
Serial: + 2 port(s): COM3, COM7
```

or, on a fresh Linux install where the user isn't in `dialout`:

```
Serial: + 1 port(s): /dev/ttyUSB0  [note: 'kir' not in 'dialout' group —
                                    serial reads may need: sudo usermod -aG dialout kir]
```

The agent itself reads this diagnostic on startup. **In one user's actual install, the agent saw "pyserial not installed" and ran `pip install pyserial` itself before the user had a chance to** — exactly the closed-loop self-repair the diagnostic is designed for.

### Mocked tests, real CI

24 new unit tests in `tests/test_serial_port_skill.py` that mock pyserial entirely — CI runs identically on Linux runners with no hardware. Coverage:

- Discovery: empty list with platform hints, multi-device render, handles None metadata from cheap clones
- Reads: text and hex output, all `until` modes (`newline` / `bytes:N` / `timeout`), parity validation, port-not-found / permission-error translation
- Writes: dry-run NEVER opens the port (Serial() raises if called), `confirm=true` actually writes, hex payload decoding with separator stripping, odd-length / non-hex rejection
- Wiring: skill is in `_DEFAULT_SKILLS`, all 16 tool_search keywords resolve to the right tool subset

### `docs/HARDWARE.md`

New pattern doc — serial_port as the reference, then a checklist for adding new hardware skills:

1. Closed parameter schemas
2. Safety gates on actuators (`confirm=true` mandatory)
3. Lazy imports of heavy hardware libraries
4. Doctor check with platform-aware hints
5. Mocked tests (no real hardware in CI)
6. Tight `tool_search` keyword wiring
7. Honest platform support declaration
8. Bridge to Home Assistant for the 2000+ devices that don't speak serial

## 🐛 Other fixes shipped this cycle

### `splitFiles()` in the chat reload path (#26)

Reported: an image the agent sent via `send_file` rendered inline during the live turn, then flipped to a download chip after the user left the thread and came back. Root cause: the live WS path ran `msg.files` through `splitFiles()` (which routes image-extensions to `_images` for inline render), but the reload path treated all of `meta.files` as plain attachments. Same data, different bucket. Fix mirrors the live path so live + reload render identically. Pinned by `tests/test_ws_attachments.py::test_reload_path_runs_meta_files_through_splitfiles`.

### `CLAUDE.md` refresh for v0.18.x

Surgical update keeping the existing structure: test count refresh, telemetry section bumped to 6 events with `thread_created` documented, new "Project blog feed" subsection covering `/api/feed/blog`, two cache-related Web UI contracts pinned (`api()` no-store + `splitFiles` symmetry).

## 🚀 Upgrade

```bash
pip install -e . --upgrade        # from a checkout
# or
pip install --upgrade qwe-qwe     # if installed as a package
```

`pyserial` is now a hard dependency (~100 KB pure Python, no native compile). It auto-installs on upgrade. If somehow you end up without it, the doctor check flags the gap and the skill returns a friendly install hint.

## 🔢 Stats

- **546 tests pass**, 3 skipped, 0 failing (was 522)
- **24 new unit tests** for the serial_port skill
- **+8 built-in skill** (`serial_port` joins browser, mcp_manager, skill_creator, soul_editor, notes, timer, weather)
- **+3 always-discoverable tools** via `tool_search`
- New 16 keywords in `_TOOL_SEARCH_INDEX` mapping hardware vocabulary to the same three tools

## What's NOT in scope yet

- **Listener mode** (`serial_listen(port, on_frame=...)` — sit on a port and trigger turns when frames arrive). Requires async event-driven turns, which is an architecture change. Tracked.
- **Hardware abstraction layer** in core. Premature until ~5 hardware skills exist and we see real shared patterns.
- **Auto-discovery dialog** in Settings → Hardware tab. For now, call `serial_list_ports` from chat.

For non-serial hardware, the practical bridge today is **Home Assistant via MCP**: add an HA MCP server through `mcp_manager` and expose all 2000+ integrations to the agent. A reference `home_assistant` skill is on the roadmap.

— Full pattern docs: [`docs/HARDWARE.md`](docs/HARDWARE.md). Privacy: [`docs/PRIVACY.md`](docs/PRIVACY.md). Architecture: [`ARCHITECTURE.md`](ARCHITECTURE.md). Contributor flow: [`CONTRIBUTING.md`](CONTRIBUTING.md).
