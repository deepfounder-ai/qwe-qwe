#!/usr/bin/env python3
"""Syntax-check every <script> block inside static/index.html.

qwe-qwe's web UI is a ~5500-line single-file SPA with inline vanilla JS.
There is no Node build step, so typos like `stae.x = 1` only surface at
runtime. This script extracts each <script>...</script> body, writes it to
a temp file, and runs `node --check` against it — the cheapest possible
parser gate.

Exit codes:
    0 — all blocks parse (OR `node` not on PATH; we emit a friendly warning
        and exit 0 so developer machines without Node can still commit;
        CI has Node and will catch anything real).
    1 — at least one block failed `node --check`.

Output format on error is ruff-style: `<path>:<line>:<col>: <message>`.
Line numbers are remapped back onto the original HTML file.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML_PATH = ROOT / "static" / "index.html"

# Match each <script>...</script> block whose opening tag has no `src=`
# attribute (external scripts have no inline body to check). The body is
# captured in group 1. DOTALL so `.` spans newlines; non-greedy so we don't
# glue multiple blocks together.
_SCRIPT_RE = re.compile(
    r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script\s*>",
    re.DOTALL | re.IGNORECASE,
)

# node --check prints `<file>:<line>` on the second line of its error. We
# parse both the SyntaxError message and the caret line to produce a
# ruff-style report.
_NODE_LOC_RE = re.compile(r"^(?P<file>.+?):(?P<line>\d+)$")


def _line_of_offset(text: str, offset: int) -> int:
    """1-based line number of byte offset `offset` within `text`."""
    return text.count("\n", 0, offset) + 1


def _extract_blocks(html: str) -> list[tuple[int, str]]:
    """Return [(start_line_in_html, body), ...] for each inline script."""
    blocks: list[tuple[int, str]] = []
    for m in _SCRIPT_RE.finditer(html):
        body_start = m.start(1)
        body = m.group(1)
        # Line of the first char of the body (1-based).
        start_line = _line_of_offset(html, body_start)
        blocks.append((start_line, body))
    return blocks


def _parse_node_error(stderr: str, tmp_path: str) -> tuple[int | None, str]:
    """Extract (line, message) from a `node --check` stderr blob.

    `node --check` stderr looks like:

        <tmp>:42
        stae.x = 1
           ^

        SyntaxError: Unexpected identifier 'x'
            at ...

    We pull the line number from the first line, and the SyntaxError line
    as the message.
    """
    line_no: int | None = None
    message = stderr.strip() or "unknown parse error"

    for raw in stderr.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        m = _NODE_LOC_RE.match(raw)
        if m and os.path.normcase(m.group("file")) == os.path.normcase(tmp_path):
            try:
                line_no = int(m.group("line"))
            except ValueError:
                pass
            break

    for raw in stderr.splitlines():
        raw = raw.strip()
        if raw.startswith("SyntaxError:"):
            message = raw
            break

    return line_no, message


def _check_block(body: str, start_line: int, html_rel: str, node: str) -> list[str]:
    """Run `node --check` on one block. Return list of ruff-style errors."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(body)
        tmp_path = tf.name
    try:
        result = subprocess.run(
            [node, "--check", tmp_path],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return []

        line_no, msg = _parse_node_error(result.stderr, tmp_path)
        # Script body starts at `start_line - 1` offset? No — the body's
        # first char is on `start_line`, so block line 1 → html line
        # `start_line`. Therefore html_line = start_line + (block_line - 1).
        if line_no is None:
            return [f"{html_rel}:{start_line}: {msg}"]
        html_line = start_line + (line_no - 1)
        return [f"{html_rel}:{html_line}: {msg}"]
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def main() -> int:
    if not HTML_PATH.exists():
        print(f"check_js: {HTML_PATH} not found — nothing to check.")
        return 0

    node = shutil.which("node")
    if not node:
        print(
            "check_js: `node` not found on PATH — skipping JS syntax check. "
            "Install Node.js to enable local JS linting (CI will still run it)."
        )
        return 0

    html = HTML_PATH.read_text(encoding="utf-8")
    blocks = _extract_blocks(html)
    if not blocks:
        print("check_js: no inline <script> blocks found.")
        return 0

    try:
        html_rel = str(HTML_PATH.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        html_rel = str(HTML_PATH)

    errors: list[str] = []
    for start_line, body in blocks:
        errors.extend(_check_block(body, start_line, html_rel, node))

    if errors:
        for e in errors:
            print(e)
        print(f"check_js: {len(errors)} parse error(s) in {html_rel}.")
        return 1

    print(
        f"check_js: ok — {len(blocks)} inline script block(s) in {html_rel} parse cleanly."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
