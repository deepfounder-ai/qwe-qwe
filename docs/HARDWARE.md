# Hardware integration

qwe-qwe runs on a real machine, talking to real hardware. This is one of the few honest advantages of self-hosting an agent — your scales, scanners, sensors, and PLCs are reachable as plain USB devices that the agent can pick up locally.

This doc is the contract for adding hardware support. The first reference implementation is the cross-platform **`serial_port`** skill — start by reading it, then follow the same pattern for new device classes.

## The `serial_port` skill (Windows / macOS / Linux)

Built in. Auto-active on every install (no manual enable required).

Surfaces three tools, gated by `tool_search`:

| Tool | Purpose |
|---|---|
| `serial_list_ports` | Enumerate plugged-in USB-COM devices. Returns port name, description, manufacturer, VID:PID. |
| `serial_read_once` | Open → read one frame (line / N bytes / timeout) → close. |
| `serial_write` | Open → write → close. Behind a `confirm=true` gate because writes to PLCs / VFDs / actuators can physically damage equipment. |

Ask the agent to `tool_search("serial")` (or `"scale"`, `"modbus"`, `"rfid"`, `"barcode"`, `"gps"`, `"plc"`, `"hardware"`, etc.) — they all unlock the same three tools.

### What this lets you talk to

| Category | Examples | Notes |
|---|---|---|
| **Weighing scales** | Mettler Toledo, CAS, Ohaus | Most output a continuous text stream like `ST,GS,+00012.345,kg\r\n`. `until="newline"` works. |
| **Barcode scanners** | Datalogic, Honeywell in RS-232 mode | Same — line-terminated text. |
| **RFID / NFC** | UHF Inventory, MAGIC, ZKTeco | Often binary. Use `format="hex"`. |
| **GPS modules** | u-blox NEO-6M / 9M | NMEA over UART, line-terminated. |
| **Label printers** | Zebra (ZPL), TSC, Godex (ESC/P) | Write-only. Always pass `confirm=true`. |
| **Receipt printers** | Epson / Star / Bixolon (ESC/POS) | Same. |
| **Industrial PLCs** | Modbus RTU over RS-485 | Binary. `format="hex"`, watch CRC. |
| **VFDs / inverters** | Anything Modbus-RTU | **Read carefully** before writes — wrong register can damage motors. |
| **Sensors** | pH, conductivity, humidity, gas, temp | Mix of text + binary, vendor-dependent. |
| **Energy meters** | Меркурий, Энергомера | DLMS / COSEM via RS-485. |

### Common scenario

> Tell qwe-qwe: "I have Mettler Toledo scales on COM3, baud 9600. Read what's on the scales right now."

Agent flow:
1. `tool_search("scale")` → activates the three serial tools
2. `serial_list_ports()` → confirms COM3 exists with description `"USB-Serial CH340"`
3. `serial_read_once(port="COM3", baud=9600, until="newline", timeout_s=2)` → returns `ST,GS,+00012.345,kg`
4. Reply: "12.345 kg"

Adding scheduling (`tool_search("schedule")`) on top of this lets you run "log the scale weight every 10 minutes" routines without writing any code.

## Adding a new hardware skill

The serial_port skill is the reference implementation. Mirror it.

### 1. Define `TOOLS` with closed parameter shapes

LLMs work better with tight schemas. Each parameter gets a clear `description` (the agent reads these), a constrained type, and an optional set of valid values.

### 2. Write a safety gate for any actuator

Reads are usually safe. Writes can damage equipment. **Every actuator tool MUST require `confirm=true`** as an explicit boolean parameter, defaulting to `false` (dry-run preview):

```python
if not confirm:
    return f"DRY RUN — pass confirm=true to actually send.\n  port: {port}\n  bytes: {len(payload)}"
```

The agent reads the user's intent, builds a dry-run, shows the user, **stops the turn**, and only on next user confirmation issues the real call. This mirrors the shell-safety pattern.

### 3. Make platform errors actionable

`Permission denied` is useless to a non-Linux user. Translate it:

```python
if "permission" in str(exc).lower() and sys.platform.startswith("linux"):
    return f"{base}\nLinux: 'usermod -aG dialout $USER' then re-login."
```

The serial_port skill has full coverage for Windows / macOS / Linux — copy that pattern.

### 4. Use lazy imports for hardware libraries

Import the hardware library **inside `execute()`**, not at module top:

```python
def execute(name, args):
    try:
        import bleak  # imported only when the skill is actually called
    except ImportError:
        return "bleak not installed. pip install bleak"
```

This way users without the hardware never pay the import cost (some hardware libraries are heavy). For pyserial we made an exception because it's tiny (~100 KB) and ships as a hard dependency.

### 5. Add a `doctor()` check

In `cli.py::doctor()`, add a `_check_<your_hardware>` block after the existing ones. Report:
- Missing library → `"- libname not installed"`
- Successful enumeration → `"+ N device(s): ..."`
- Permission gotchas (group membership, driver missing) → `"+ ... [note: usermod ...]"`

Use ASCII markers (`+` / `-` / `~`), NOT Unicode emojis — cp1251 terminals on Windows truncate them silently.

### 6. Wire `tool_search` keywords

In `tools.py::_TOOL_SEARCH_INDEX`, add every word a user might use to describe their hardware:

```python
"bluetooth": ["bt_scan", "bt_connect", "bt_read"],
"ble": ["bt_scan", "bt_connect", "bt_read"],
"heart": ["bt_scan", "bt_connect", "bt_read"],   # heart-rate sensors
"thermo": ["bt_scan", "bt_connect", "bt_read"],  # BLE thermometers
```

The fallback (substring match against descriptions) covers a lot, but explicit keywords are more reliable for non-obvious mappings.

### 7. Document platform support honestly

In the skill's `DESCRIPTION` + at the top of the file, state which platforms work. Don't over-promise. GPIO-style libraries are Linux-only; BLE on Windows works but flakily; serial works everywhere.

### 8. Test with mocks, not real hardware

CI doesn't have your scale plugged in. Mock the hardware library at the module attribute level:

```python
def test_read(monkeypatch):
    fake = _FakeSerial()
    monkeypatch.setattr("serial.Serial", lambda **kw: fake)
    out = sp.execute("serial_read_once", {"port": "COM3"})
    assert "12.345" in out
```

`tests/test_serial_port_skill.py` has 24 tests covering happy path, error paths, safety gate, all `until` modes — copy that structure.

## What's NOT in scope here (yet)

- **Listener mode** (`serial_listen(port, callback_skill)` — sit on the port and trigger turns when frames arrive). Requires async event-driven turns, which is an architecture change beyond a single skill. Tracked separately.
- **Auto-discovery dialog** in the web UI — currently you call `serial_list_ports` from chat. A Settings → Hardware tab with live-detected device list is on the roadmap.
- **Hardware abstraction layer** in core. Premature until ~5 hardware skills exist and we see real shared patterns.

## Bridge to Home Assistant (for everything else)

If your device speaks Zigbee, Z-Wave, Wi-Fi, MQTT, IP — you probably want a **Home Assistant bridge** rather than a native qwe-qwe skill. HA has 2000+ integrations already. A small `home_assistant` skill that calls HA's REST + MQTT gives the agent access to all of them with zero per-device code.

A reference `home_assistant` skill is on the roadmap. Until then: use the existing `mcp_manager` to add an HA MCP server, or write a custom skill via `create_skill`.
