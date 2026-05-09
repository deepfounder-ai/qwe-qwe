"""Serial port (USB-COM / RS-232 / RS-485) skill.

Cross-platform driver for the universe of business hardware that talks
serial: weighing scales, barcode scanners, RFID readers, GPS modules,
label / receipt printers (ESC/P, ZPL, ESC/POS), industrial PLCs over
RS-485 (Modbus RTU), pH meters, gas detectors, energy meters, cash
drawers, fingerprint scanners, etc.

Powered by `pyserial` — works identically on Windows (`COM3`), Linux
(`/dev/ttyUSB0`, `/dev/ttyACM0`), and macOS (`/dev/tty.usbserial-…`).

This skill exposes three tools:

  * ``serial_list_ports``  — discover currently-plugged serial devices.
  * ``serial_read_once``   — open / read once (line or N bytes) / close.
  * ``serial_write``       — open / write / close. Behind a ``confirm``
                             gate because writes to PLCs / actuators
                             can physically damage equipment.

A long-running listener mode (subscribe to a port and trigger turns on
each frame) is intentionally out of scope here — it requires async
event-driven turns, which is a separate architectural change.
"""
from __future__ import annotations

import binascii
import sys
import threading

DESCRIPTION = (
    "Talk to USB-serial / RS-232 / RS-485 devices: weighing scales, "
    "barcode scanners, RFID readers, GPS modules, label printers, "
    "industrial PLCs (Modbus RTU), sensors. Cross-platform "
    "(Windows / macOS / Linux) via pyserial."
)

# Surface platform-specific guidance to the agent on first use. The
# `INSTRUCTION` is injected into the system prompt when any tool from
# this skill is active (see `skills.get_instruction`).
INSTRUCTION = (
    "When the user asks about hardware connected via USB or COM port "
    "(scales, barcode scanners, RFID readers, GPS, label printers, "
    "PLCs, sensors), call `serial_list_ports` first to discover what's "
    "actually plugged in. Match the user's description to the port's "
    "manufacturer / description fields rather than guessing the port "
    "name. Common baud rates: 9600 (most legacy), 19200 / 38400 (newer "
    "scales / GPS), 115200 (modern modules). When in doubt, try 9600 "
    "first.\n\n"
    "WRITES are gated by `confirm=true` — writes to PLCs / VFDs / "
    "actuators can physically damage equipment. Always read the user's "
    "intent twice before passing confirm=true to `serial_write`. Reads "
    "are always safe."
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "serial_list_ports",
            "description": (
                "List all currently-plugged serial / USB-COM devices on this "
                "machine. Returns port name (COM3 / /dev/ttyUSB0 / "
                "/dev/tty.usbserial-…), human-readable description, vendor / "
                "product info. Use this BEFORE any read/write to identify "
                "the right port."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "serial_read_once",
            "description": (
                "Open a serial port, read one frame (line OR fixed byte "
                "count), and close. Returns the data as text (default) or "
                "hex (for binary protocols). Use this for one-shot reads "
                "from scales, GPS, sensors, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "port": {
                        "type": "string",
                        "description": "Port name. Windows: 'COM3'. Linux: '/dev/ttyUSB0'. macOS: '/dev/tty.usbserial-1410'. Get from serial_list_ports.",
                    },
                    "baud": {
                        "type": "integer",
                        "description": "Baud rate. Common: 9600, 19200, 38400, 57600, 115200. Default 9600.",
                    },
                    "until": {
                        "type": "string",
                        "description": "Frame end. 'newline' = read until \\n (text protocols, NMEA, scales). 'bytes:N' = read exactly N bytes (binary protocols). 'timeout' = read whatever arrives within the timeout window. Default 'newline'.",
                    },
                    "timeout_s": {
                        "type": "number",
                        "description": "Max seconds to wait for data. Default 2.0.",
                    },
                    "format": {
                        "type": "string",
                        "description": "Output format. 'text' = utf-8 decoded, errors replaced (default). 'hex' = uppercase hex (binary protocols, Modbus RTU).",
                    },
                    "parity": {
                        "type": "string",
                        "description": "'N' (none, default), 'E' (even), 'O' (odd). Modbus typically 'N' or 'E'.",
                    },
                    "stopbits": {
                        "type": "integer",
                        "description": "1 (default) or 2.",
                    },
                    "bytesize": {
                        "type": "integer",
                        "description": "5, 6, 7, or 8 (default).",
                    },
                },
                "required": ["port"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "serial_write",
            "description": (
                "Open a serial port, write data, and close. SAFETY-GATED: "
                "set confirm=true to actually send. Writes to PLCs / VFDs / "
                "actuators can physically damage equipment — read the user's "
                "intent carefully before confirming."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "port": {"type": "string", "description": "Port name (see serial_list_ports)."},
                    "baud": {"type": "integer", "description": "Baud rate. Default 9600."},
                    "data": {
                        "type": "string",
                        "description": "Data to send. If format='text', sent as UTF-8 bytes (newline NOT auto-appended — include \\n explicitly if needed). If format='hex', interpreted as a hex string ('01 03 00 00' or '01030000'), spaces ignored.",
                    },
                    "format": {
                        "type": "string",
                        "description": "'text' (default) or 'hex' (for Modbus RTU and other binary protocols).",
                    },
                    "confirm": {
                        "type": "boolean",
                        "description": "MUST be true to actually write. False (default) returns a dry-run preview without opening the port.",
                    },
                    "parity": {"type": "string", "description": "'N' / 'E' / 'O'. Default 'N'."},
                    "stopbits": {"type": "integer", "description": "1 or 2. Default 1."},
                    "bytesize": {"type": "integer", "description": "5–8. Default 8."},
                    "timeout_s": {
                        "type": "number",
                        "description": "Open / write timeout in seconds. Default 2.0.",
                    },
                },
                "required": ["port", "data"],
            },
        },
    },
]


# Module-level lock so a misbehaving model can't race two concurrent
# opens on the same physical device. pyserial's exclusive=True flag
# also helps on POSIX, but Windows ignores it.
_lock = threading.Lock()


def execute(name: str, args: dict) -> str:
    if name == "serial_list_ports":
        return _list_ports()
    elif name == "serial_read_once":
        return _read_once(args)
    elif name == "serial_write":
        return _write(args)
    return f"Unknown tool: {name}"


# ── List ports ─────────────────────────────────────────────────────


def _list_ports() -> str:
    """Enumerate plugged-in serial devices.

    Backed by `serial.tools.list_ports.comports()` which abstracts the
    platform mess: Windows reads SetupDi*, Linux walks /sys/class/tty
    + /dev/serial/by-id, macOS uses IOKit. Output is identical shape
    on all three.
    """
    try:
        import serial.tools.list_ports as lp
    except ImportError:
        return _pyserial_missing_msg()

    ports = list(lp.comports())
    if not ports:
        return (
            "No serial ports detected. "
            f"(Platform: {sys.platform}.)\n"
            "Plug in your USB device and try again. If it's still not "
            "showing up:\n"
            + _platform_troubleshooting()
        )
    lines = [f"**{len(ports)} serial port(s) detected:**"]
    for p in ports:
        # The `description` is the human-readable label ("USB-Serial CH340").
        # `hwid` includes VID/PID for unique identification.
        # `manufacturer` / `product` may be None on cheap clones.
        desc = p.description or "(no description)"
        hwid = p.hwid or ""
        line = f"  • `{p.device}` — {desc}"
        if p.manufacturer:
            line += f" [{p.manufacturer}]"
        if hwid and "VID:PID" in hwid.upper():
            # extract VID:PID for the agent (helps disambiguate)
            line += f"  ({hwid})"
        lines.append(line)
    return "\n".join(lines)


# ── Read ───────────────────────────────────────────────────────────


def _read_once(args: dict) -> str:
    """One-shot read: open → read frame → close."""
    try:
        import serial as pyserial
    except ImportError:
        return _pyserial_missing_msg()

    port = args.get("port", "")
    if not port:
        return "Error: 'port' required. Call serial_list_ports to discover."
    baud = int(args.get("baud") or 9600)
    until = (args.get("until") or "newline").lower()
    timeout_s = float(args.get("timeout_s") or 2.0)
    fmt = (args.get("format") or "text").lower()
    parity_raw = (args.get("parity") or "N").upper()
    stopbits = int(args.get("stopbits") or 1)
    bytesize = int(args.get("bytesize") or 8)

    parity = _coerce_parity(parity_raw, pyserial)
    if parity is None:
        return f"Error: invalid parity {parity_raw!r}. Use 'N', 'E', or 'O'."

    with _lock:
        try:
            ser = pyserial.Serial(
                port=port, baudrate=baud, parity=parity,
                stopbits=stopbits, bytesize=bytesize, timeout=timeout_s,
            )
        except Exception as e:
            return _open_error_msg(port, e)
        try:
            data = _do_read(ser, until, timeout_s)
        finally:
            try:
                ser.close()
            except Exception:
                pass

    if not data:
        return f"(no data within {timeout_s}s on {port} @ {baud} baud)"
    if fmt == "hex":
        return f"{port} @ {baud} ({len(data)} bytes hex):\n{binascii.hexlify(data).decode('ascii').upper()}"
    # Default: utf-8 with errors replaced. Strip trailing whitespace
    # (most line-based protocols include \r\n which renders ugly).
    text = data.decode("utf-8", errors="replace").rstrip()
    return f"{port} @ {baud}:\n{text}"


def _do_read(ser, until: str, timeout_s: float) -> bytes:
    """Read one frame from an open Serial object.

    `until`:
      - 'newline'   — readline()
      - 'bytes:N'   — read(N)
      - 'timeout'   — read everything that arrives within timeout_s,
                      then return whatever's been buffered
    """
    if until.startswith("bytes:"):
        try:
            n = int(until.split(":", 1)[1])
        except (ValueError, IndexError):
            n = 0
        if n <= 0:
            return b""
        return ser.read(n)
    if until == "timeout":
        # `timeout` has already been set on the Serial. Just read whatever
        # is pending; pyserial returns when the inter-byte gap exceeds
        # the timeout.
        import time
        end = time.time() + timeout_s
        chunks: list[bytes] = []
        while time.time() < end:
            in_waiting = getattr(ser, "in_waiting", 0) or 0
            if in_waiting:
                chunks.append(ser.read(in_waiting))
            else:
                # short blocking read so we don't busy-loop
                got = ser.read(1)
                if got:
                    chunks.append(got)
        return b"".join(chunks)
    # Default 'newline'
    return ser.readline()


# ── Write ──────────────────────────────────────────────────────────


def _write(args: dict) -> str:
    """One-shot write with safety gate.

    `confirm=true` is REQUIRED to actually open the port + write.
    Default (confirm=false) returns a preview of what would be sent.
    """
    try:
        import serial as pyserial
    except ImportError:
        return _pyserial_missing_msg()

    port = args.get("port", "")
    if not port:
        return "Error: 'port' required."
    data_raw = args.get("data")
    if data_raw is None or data_raw == "":
        return "Error: 'data' required."
    fmt = (args.get("format") or "text").lower()
    confirm = bool(args.get("confirm"))

    # Encode payload up-front so a malformed hex string fails before
    # we touch the port (clearer error, no half-open device).
    try:
        payload = _encode_payload(data_raw, fmt)
    except ValueError as e:
        return f"Error: {e}"

    if not confirm:
        # Dry-run preview. Don't open the port. Show what WOULD go on
        # the wire so the user can confirm safety.
        hex_preview = binascii.hexlify(payload).decode("ascii").upper()
        return (
            f"DRY RUN — pass confirm=true to actually send.\n"
            f"  port:  {port}\n"
            f"  baud:  {int(args.get('baud') or 9600)}\n"
            f"  bytes: {len(payload)}\n"
            f"  hex:   {hex_preview[:200]}{'...' if len(hex_preview) > 200 else ''}"
        )

    baud = int(args.get("baud") or 9600)
    timeout_s = float(args.get("timeout_s") or 2.0)
    parity_raw = (args.get("parity") or "N").upper()
    stopbits = int(args.get("stopbits") or 1)
    bytesize = int(args.get("bytesize") or 8)

    parity = _coerce_parity(parity_raw, pyserial)
    if parity is None:
        return f"Error: invalid parity {parity_raw!r}. Use 'N', 'E', or 'O'."

    with _lock:
        try:
            ser = pyserial.Serial(
                port=port, baudrate=baud, parity=parity,
                stopbits=stopbits, bytesize=bytesize,
                timeout=timeout_s, write_timeout=timeout_s,
            )
        except Exception as e:
            return _open_error_msg(port, e)
        try:
            written = ser.write(payload)
            try:
                ser.flush()
            except Exception:
                pass
        except Exception as e:
            return f"Error writing to {port}: {e}"
        finally:
            try:
                ser.close()
            except Exception:
                pass

    return f"Wrote {written} byte(s) to {port} @ {baud}."


# ── Helpers ────────────────────────────────────────────────────────


def _encode_payload(data: str, fmt: str) -> bytes:
    """Convert the agent-supplied data string into bytes."""
    if fmt == "hex":
        # Strip whitespace + common separators; pyserial's hex idiom
        cleaned = "".join(ch for ch in data if ch not in " \t\n\r:,;")
        if len(cleaned) % 2 != 0:
            raise ValueError(
                "hex payload must have an even number of digits "
                f"(got {len(cleaned)} after stripping whitespace)"
            )
        try:
            return bytes.fromhex(cleaned)
        except ValueError as e:
            raise ValueError(f"invalid hex payload: {e}") from None
    # Default text: UTF-8. Caller is responsible for explicit \n / \r\n.
    return data.encode("utf-8", errors="replace")


def _coerce_parity(parity: str, pyserial_module):
    """Map our 'N'/'E'/'O' to pyserial constants. Returns None if invalid."""
    table = {
        "N": pyserial_module.PARITY_NONE,
        "E": pyserial_module.PARITY_EVEN,
        "O": pyserial_module.PARITY_ODD,
    }
    return table.get(parity)


def _open_error_msg(port: str, exc: Exception) -> str:
    """Friendly platform-aware error message when a port won't open."""
    base = f"Failed to open {port}: {exc}"
    msg = str(exc).lower()
    hints = []
    if "permission" in msg or "access denied" in msg or "errno 13" in msg:
        if sys.platform.startswith("linux"):
            hints.append(
                "Linux: your user needs the 'dialout' group to read "
                "serial devices. Run:\n"
                "    sudo usermod -aG dialout $USER\n"
                "then log out and back in."
            )
        elif sys.platform == "darwin":
            hints.append(
                "macOS: an Apple Silicon CH340/CH341 cable usually "
                "needs the WCH driver from "
                "https://www.wch.cn/downloads/CH34XSER_MAC_ZIP.html. "
                "FTDI-based cables work out of the box."
            )
        else:
            hints.append(
                "Windows: check that no other app (Arduino IDE, Putty, "
                "vendor utility) has the port open."
            )
    elif "could not open port" in msg or "no such file" in msg or "filenotfound" in msg:
        hints.append(
            "Port name may be wrong. Call serial_list_ports to see "
            "what's actually plugged in."
        )
    elif "device busy" in msg or "resource busy" in msg:
        hints.append(
            "Another program holds the port. On macOS / Linux, also "
            "check /var/lock/ for stale lock files."
        )
    if hints:
        return base + "\n" + "\n".join(hints)
    return base


def _pyserial_missing_msg() -> str:
    return (
        "pyserial is not installed. It ships as a hard dependency in "
        "qwe-qwe ≥ 0.18.6. To install manually:\n"
        "    pip install pyserial"
    )


def _platform_troubleshooting() -> str:
    if sys.platform.startswith("linux"):
        return (
            "  - Confirm the kernel sees the device: run `dmesg | tail` "
            "right after plugging it in.\n"
            "  - Check group membership: `groups | grep dialout`. If "
            "missing: `sudo usermod -aG dialout $USER` then re-login.\n"
            "  - Some cheap CH340 cables work better with the "
            "ch341-uart kernel module (usually bundled).\n"
            "  - Try `ls /dev/ttyUSB* /dev/ttyACM*` directly."
        )
    if sys.platform == "darwin":
        return (
            "  - On Apple Silicon, CH340 / CH341 cables need the WCH "
            "driver: https://www.wch.cn/downloads/CH34XSER_MAC_ZIP.html.\n"
            "  - FTDI-based cables work without extra drivers.\n"
            "  - Try `ls /dev/tty.* /dev/cu.*` directly."
        )
    if sys.platform == "win32":
        return (
            "  - Check Device Manager → Ports (COM & LPT). If you see "
            "a yellow warning triangle, the driver isn't installed.\n"
            "  - CH340: install the WCH driver "
            "(https://www.wch.cn/download/CH341SER_EXE.html).\n"
            "  - FTDI: drivers are usually auto-installed via Windows "
            "Update."
        )
    return f"  - Unknown platform: {sys.platform}. Check vendor docs for serial driver setup."
