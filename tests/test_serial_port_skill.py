"""Tests for the cross-platform serial_port hardware skill.

The skill is a thin wrapper over `pyserial`. We mock both
`serial.Serial` (the connection) and `serial.tools.list_ports.comports`
(the discovery) so tests run identically on Windows / macOS / Linux
CI without any hardware. The contract being pinned:

- Discovery returns the right shape regardless of platform.
- Reads honor the `until` modes (newline / bytes:N / timeout).
- Writes are GATED by `confirm=true`. Default is dry-run.
- Hex payloads decode correctly (with whitespace stripping).
- Parity / port-not-found / permission errors map to friendly
  messages with platform-aware hints.

Pytest's `monkeypatch` is enough — no fixtures needed beyond the
standard tmp_path / capsys.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# Path import — the skill lives in skills/ and is auto-loaded by the
# main agent at runtime, but tests load it directly so we don't drag
# in the full skill loader / kv state.
def _load_skill():
    spec = importlib.util.spec_from_file_location(
        "_serial_port_under_test",
        Path(__file__).resolve().parent.parent / "skills" / "serial_port.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def sp():
    return _load_skill()


# ── Shape / metadata ───────────────────────────────────────────────


def test_skill_exposes_three_tools(sp):
    names = [t["function"]["name"] for t in sp.TOOLS]
    assert names == ["serial_list_ports", "serial_read_once", "serial_write"]


def test_skill_has_description_and_instruction(sp):
    """skills/__init__.py validates skills require DESCRIPTION + TOOLS.
    We additionally rely on INSTRUCTION (injected into system prompt
    when the skill is active) to teach the agent about the safety
    gate on writes."""
    assert isinstance(sp.DESCRIPTION, str) and len(sp.DESCRIPTION) > 20
    assert isinstance(sp.INSTRUCTION, str)
    assert "confirm=true" in sp.INSTRUCTION.lower() or "confirm" in sp.INSTRUCTION
    assert "damage" in sp.INSTRUCTION  # safety wording present


def test_unknown_tool_returns_friendly_error(sp):
    out = sp.execute("definitely_not_a_tool", {})
    assert "Unknown tool" in out


# ── serial_list_ports ──────────────────────────────────────────────


def _fake_port(device, description="USB-Serial CH340", manufacturer="wch.cn",
               hwid="USB VID:PID=1A86:7523 SER=AB12345"):
    """Minimal stand-in for pyserial's ListPortInfo objects."""
    p = MagicMock()
    p.device = device
    p.description = description
    p.manufacturer = manufacturer
    p.hwid = hwid
    return p


def test_list_ports_empty_includes_platform_hints(sp, monkeypatch):
    """No ports plugged in → tell the user how to troubleshoot, not
    just an empty list."""
    monkeypatch.setattr(
        "serial.tools.list_ports.comports",
        lambda: [],
    )
    out = sp.execute("serial_list_ports", {})
    assert "No serial ports detected" in out
    # At least one platform's troubleshooting block should fire (the
    # one matching sys.platform on the test host).
    assert any(
        marker in out
        for marker in ("Device Manager", "ttyUSB", "tty.usbserial", "Unknown platform")
    )


def test_list_ports_renders_each_device(sp, monkeypatch):
    monkeypatch.setattr(
        "serial.tools.list_ports.comports",
        lambda: [
            _fake_port("COM3", "USB-Serial CH340", "wch.cn"),
            _fake_port("COM7", "FT232R USB UART", "FTDI",
                       hwid="USB VID:PID=0403:6001 SER=A50285BI"),
        ],
    )
    out = sp.execute("serial_list_ports", {})
    assert "2 serial port(s)" in out
    assert "COM3" in out and "CH340" in out
    assert "COM7" in out and "FT232R" in out
    # Manufacturer surfaces so the agent can disambiguate
    assert "wch.cn" in out and "FTDI" in out
    # VID:PID surfaces for unique ID
    assert "VID:PID" in out


def test_list_ports_handles_clones_with_missing_metadata(sp, monkeypatch):
    """Cheap CH340 clones often report None for manufacturer/product —
    skill must not crash."""
    p = MagicMock()
    p.device = "/dev/ttyUSB0"
    p.description = None
    p.manufacturer = None
    p.hwid = ""
    monkeypatch.setattr("serial.tools.list_ports.comports", lambda: [p])
    out = sp.execute("serial_list_ports", {})
    assert "/dev/ttyUSB0" in out
    assert "(no description)" in out


# ── serial_read_once ───────────────────────────────────────────────


class _FakeSerial:
    """Minimal Serial double that supports the read modes we use."""
    def __init__(self, port=None, baudrate=None, parity=None,
                 stopbits=None, bytesize=None, timeout=None,
                 write_timeout=None):
        self.port = port
        self.baudrate = baudrate
        self.parity = parity
        self.stopbits = stopbits
        self.bytesize = bytesize
        self.timeout = timeout
        self.write_timeout = write_timeout
        # caller can override any of these between init and reads
        self._line = b""
        self._read_buffer = b""
        self.in_waiting = 0
        self.write_log: list[bytes] = []
        self.closed = False
        self.flushed = False

    def readline(self):
        return self._line

    def read(self, n):
        if not self._read_buffer:
            return b""
        chunk, self._read_buffer = self._read_buffer[:n], self._read_buffer[n:]
        return chunk

    def write(self, data):
        self.write_log.append(bytes(data))
        return len(data)

    def flush(self):
        self.flushed = True

    def close(self):
        self.closed = True


def _patch_serial(monkeypatch, fake):
    """Replace `serial.Serial` with a factory returning the supplied fake."""
    import serial as pyserial
    monkeypatch.setattr(pyserial, "Serial", lambda **kw: fake)


def test_read_once_requires_port(sp):
    out = sp.execute("serial_read_once", {})
    assert "'port' required" in out


def test_read_once_returns_text_line_by_default(sp, monkeypatch):
    fake = _FakeSerial()
    fake._line = b"ST,GS,+00012.345,kg\r\n"
    _patch_serial(monkeypatch, fake)

    out = sp.execute("serial_read_once", {"port": "COM3", "baud": 9600})

    assert "12.345" in out
    assert "COM3" in out
    assert "9600" in out
    assert fake.closed is True  # always closes after read


def test_read_once_hex_format_for_binary_protocols(sp, monkeypatch):
    """Modbus RTU and similar binary protocols want hex output."""
    fake = _FakeSerial()
    fake._line = bytes([0x01, 0x03, 0x04, 0x00, 0x0A, 0x00, 0x14, 0x9B, 0x42])
    _patch_serial(monkeypatch, fake)

    out = sp.execute(
        "serial_read_once",
        {"port": "COM3", "format": "hex"},
    )

    assert "010304000A0014" in out.replace(" ", "")
    # byte count surfaces so the agent can sanity-check Modbus framing
    assert "9 bytes hex" in out


def test_read_once_bytes_mode_reads_exact_count(sp, monkeypatch):
    fake = _FakeSerial()
    fake._read_buffer = b"\x01\x02\x03\x04\x05\x06\x07\x08"
    _patch_serial(monkeypatch, fake)

    out = sp.execute(
        "serial_read_once",
        {"port": "COM3", "until": "bytes:4", "format": "hex"},
    )

    # Should have read exactly 4 bytes
    assert "01020304" in out.replace(" ", "")
    assert "08" not in out  # remaining bytes left in buffer


def test_read_once_invalid_parity_rejected(sp):
    out = sp.execute("serial_read_once", {"port": "COM3", "parity": "X"})
    assert "invalid parity" in out


def test_read_once_translates_permission_error_on_linux(sp, monkeypatch):
    """A user without dialout group sees a hint, not just `Permission
    denied: '/dev/ttyUSB0'`."""
    import serial as pyserial

    def boom(**kw):
        raise PermissionError(13, "Permission denied", "/dev/ttyUSB0")

    monkeypatch.setattr(pyserial, "Serial", boom)
    monkeypatch.setattr(sp, "sys", type("S", (), {"platform": "linux"})())

    out = sp.execute("serial_read_once", {"port": "/dev/ttyUSB0"})
    assert "Failed to open" in out
    assert "dialout" in out  # platform-aware hint fired


def test_read_once_translates_filenotfound_to_helpful_message(sp, monkeypatch):
    import serial as pyserial

    def boom(**kw):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(pyserial, "Serial", boom)

    out = sp.execute("serial_read_once", {"port": "COM999"})
    assert "Failed to open" in out
    assert "serial_list_ports" in out


def test_read_once_returns_no_data_message_on_timeout(sp, monkeypatch):
    fake = _FakeSerial()
    fake._line = b""  # nothing arrived
    _patch_serial(monkeypatch, fake)

    out = sp.execute("serial_read_once", {"port": "COM3", "timeout_s": 0.1})

    assert "no data" in out.lower()


# ── serial_write — safety gate ─────────────────────────────────────


def test_write_default_is_dry_run_and_does_not_open_port(sp, monkeypatch):
    """The whole point of the safety gate: confirm=false MUST NOT
    physically touch the device. We pin this by making Serial() raise
    if it's ever called — the test passes only if no open is
    attempted."""
    import serial as pyserial

    def must_not_open(**kw):
        raise AssertionError("Port was opened despite confirm=false!")

    monkeypatch.setattr(pyserial, "Serial", must_not_open)

    out = sp.execute(
        "serial_write",
        {"port": "COM3", "data": "TARE\r\n"},
    )

    assert "DRY RUN" in out
    assert "confirm=true" in out
    # Hex preview still useful for the user to eyeball
    assert "544152450D0A" in out  # "TARE\r\n" in hex


def test_write_with_confirm_actually_writes(sp, monkeypatch):
    fake = _FakeSerial()
    _patch_serial(monkeypatch, fake)

    out = sp.execute(
        "serial_write",
        {"port": "COM3", "data": "TARE\r\n", "confirm": True},
    )

    assert "Wrote 6 byte(s)" in out
    assert fake.write_log == [b"TARE\r\n"]
    assert fake.flushed is True
    assert fake.closed is True


def test_write_hex_payload_decodes_correctly(sp, monkeypatch):
    fake = _FakeSerial()
    _patch_serial(monkeypatch, fake)

    out = sp.execute(
        "serial_write",
        {
            "port": "COM3",
            "data": "01 03 00 00 00 02 C4 0B",  # Modbus RTU read-holding-registers
            "format": "hex",
            "confirm": True,
        },
    )

    assert "Wrote 8 byte(s)" in out
    assert fake.write_log == [bytes.fromhex("0103000000 02C40B".replace(" ", ""))]


def test_write_hex_handles_separators(sp, monkeypatch):
    """User-supplied hex can use spaces, colons, commas, semicolons —
    all should be stripped before fromhex()."""
    fake = _FakeSerial()
    _patch_serial(monkeypatch, fake)

    sp.execute("serial_write", {
        "port": "COM3", "data": "01:03,00;00",
        "format": "hex", "confirm": True,
    })
    assert fake.write_log == [bytes.fromhex("01030000")]


def test_write_rejects_odd_length_hex(sp):
    """A typo dropping a digit would silently corrupt the protocol —
    fail loud instead."""
    out = sp.execute("serial_write", {
        "port": "COM3", "data": "01 03 0",
        "format": "hex", "confirm": True,
    })
    assert "even number of digits" in out


def test_write_rejects_non_hex_chars(sp):
    out = sp.execute("serial_write", {
        "port": "COM3", "data": "ZZ",
        "format": "hex", "confirm": True,
    })
    assert "invalid hex" in out


def test_write_requires_data(sp):
    out = sp.execute("serial_write", {"port": "COM3"})
    assert "'data' required" in out


def test_write_propagates_open_failure_with_hint(sp, monkeypatch):
    import serial as pyserial

    def boom(**kw):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(pyserial, "Serial", boom)
    monkeypatch.setattr(sp, "sys", type("S", (), {"platform": "linux"})())

    out = sp.execute(
        "serial_write",
        {"port": "/dev/ttyUSB0", "data": "x", "confirm": True},
    )
    assert "Failed to open" in out
    assert "dialout" in out


# ── Wiring sanity (skill registers via skills/ system) ─────────────


def test_skill_is_in_default_active_set():
    """serial_port should be on by default so users with hardware can
    discover it via tool_search('serial') without manual activation."""
    if "skills" in sys.modules:
        importlib.reload(sys.modules["skills"])
    import skills as skills_mod
    assert "serial_port" in skills_mod._DEFAULT_SKILLS


def test_tool_search_finds_serial_via_keyword():
    """`tool_search('serial')` should activate exactly the three
    serial_* tools."""
    if "tools" in sys.modules:
        importlib.reload(sys.modules["tools"])
    import tools
    expected = {"serial_list_ports", "serial_read_once", "serial_write"}
    assert set(tools._TOOL_SEARCH_INDEX["serial"]) == expected
    # Synonyms for real-world hardware terminology should resolve to
    # at least the relevant subset.
    for kw in ("modbus", "scale", "rfid", "barcode", "gps", "plc"):
        hits = set(tools._TOOL_SEARCH_INDEX[kw])
        assert hits.issubset(expected) and hits, f"keyword {kw!r} produced {hits!r}"
