"""Structured logging for qwe-qwe — pure stdlib, no API, no AI.

Logs to:
  logs/qwe-qwe.log       — all events (INFO+)
  logs/errors.log         — errors only (WARNING+)
  console (stderr)        — critical only (won't mess up TUI)

Log format: timestamp | level | module | message
Rotation: 5MB per file, 3 backups.

Usage:
  from logger import log
  log.info("agent turn started", extra={"user_input": "hello"})
  log.error("tool failed", exc_info=True)
"""

import logging
import logging.handlers
import json
import time
import os
from pathlib import Path

# ── Log directory ──
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ── Custom formatter with structured extras ──

class StructuredFormatter(logging.Formatter):
    """Compact structured log lines with optional JSON extras."""

    def format(self, record: logging.LogRecord) -> str:
        # Base line
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
        ms = int(record.created * 1000) % 1000
        base = f"{ts}.{ms:03d} | {record.levelname:<7} | {record.name:<12} | {record.getMessage()}"

        # Append structured extras if present
        extras = {k: v for k, v in record.__dict__.items()
                  if k not in logging.LogRecord("", 0, "", 0, None, None, None).__dict__
                  and k not in ("message", "msg", "args", "exc_info", "exc_text",
                                "stack_info", "taskName")}
        if extras:
            try:
                base += " | " + json.dumps(extras, ensure_ascii=False, default=str)
            except Exception:
                pass

        # Exception info
        if record.exc_info and record.exc_info[1]:
            base += "\n" + self.formatException(record.exc_info)

        return base


# ── Setup ──

def _setup() -> logging.Logger:
    root = logging.getLogger("qwe")
    root.setLevel(logging.DEBUG)

    # Prevent duplicate handlers on reimport
    if root.handlers:
        return root

    fmt = StructuredFormatter()

    # 1) Main log — everything INFO+
    main_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "qwe-qwe.log",
        maxBytes=5 * 1024 * 1024,  # 5MB
        backupCount=3,
        encoding="utf-8",
    )
    main_handler.setLevel(logging.INFO)
    main_handler.setFormatter(fmt)
    root.addHandler(main_handler)

    # 2) Error log — WARNING+
    err_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "errors.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    err_handler.setLevel(logging.WARNING)
    err_handler.setFormatter(fmt)
    root.addHandler(err_handler)

    # 3) Console — CRITICAL only (don't spam the TUI)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.CRITICAL)
    console_handler.setFormatter(fmt)
    root.addHandler(console_handler)

    return root


# ── Public interface ──
# Usage: from logger import log
# Sub-loggers: logger.get("tools"), logger.get("agent"), etc.

_root = _setup()
log = _root


def get(name: str) -> logging.Logger:
    """Get a child logger: logger.get('agent') → 'qwe.agent'"""
    return _root.getChild(name)


# ── Convenience: event logger for structured events ──

def event(name: str, **data):
    """Log a structured event. Example: logger.event("tool_call", tool="shell", args="ls")"""
    extra_str = json.dumps(data, ensure_ascii=False, default=str) if data else ""
    _root.info(f"EVENT:{name} {extra_str}")


def metric(name: str, value: float, **tags):
    """Log a metric. Example: logger.metric("turn_tokens", 1523, model="qwen3.5")"""
    parts = [f"METRIC:{name}={value}"]
    if tags:
        parts.append(json.dumps(tags, ensure_ascii=False, default=str))
    _root.info(" ".join(parts))
