# v0.17.0 — Premium web UI rebuild + universal document ingest

Major overhaul of the web UI and a big expansion of the knowledge pipeline. Cross-platform install (Linux / macOS / Windows) verified end-to-end.

## ✨ Highlights

### Premium web UI (Linear / Vercel / Anthropic-Console aesthetic)

Full replacement of the legacy 6 021-line SPA with a single-file vanilla-JS shell. **Zero runtime JS dependencies** (no React, no CDN build step).

- **Editorial chat transcript** — Geist (UI) + Instrument Serif (headings, big stat numbers, thinking italic) + Geist Mono (timestamps, tokens, tags, technical metadata)
- **Streaming without flicker** — in-place body patches, targeted DOM updates, no full re-render during a turn
- **Right-side Inspector** — thread meta · context-window gauge · INPUT / OUTPUT cards · sparkbars (tokens per turn) · recalled memories (real `/api/knowledge/search`) · active tools · latency bars
- **Tool calls grouped by category** (memory / knowledge / files / shell / browser / web / vision / voice / automation / skills / orchestration), each expandable for full JSON input + output
- **Code blocks** — proper line-number gutter, filename + language label, copy button, python/js syntax highlighting
- **Markdown rendering** — H1–H6, bold / italic / strike, inline code, blockquote, lists, links, bare URLs
- **Thread list** — rename / delete inline actions, search, pinned group
- **⌘K command palette** + Gmail-style **Alt+letter** nav (Alt+T/M/S/P/,) + cheatsheet modal (Shift+?)
- **Platform-aware modifiers** — ⌘ on Mac, Ctrl on Windows/Linux
- **Regenerate** = clean restart — server deletes the last user→assistant turn so the model has no idea it's a regeneration
- **Persistent attachments** — images + files saved to message meta, survive server restart
- **Spicy mode easter egg** restored (5 hearts in Settings → Tools, 7 taps in 6s)
- **Mobile** — bottom tab bar, slide-in drawer, iPhone safe-area insets on all 4 sides, 16 px inputs (no iOS auto-zoom), `100dvh` viewport

### Universal document ingest (MarkItDown)

Microsoft **MarkItDown** is now a hard dep. Together with explicit pins for `python-docx`, `python-pptx`, `openpyxl`, `mammoth`, `markdownify`, `beautifulsoup4`, `pdfminer.six` — a fresh `pip install -e .` never ships a degraded KB pipeline.

- **50+ formats**: PDF · DOCX · PPTX · XLSX · EPUB · ODT · RTF · `.ipynb` · HTML · 40+ code languages · JSON / CSV / YAML / TOML / XML / SVG · reStructuredText / AsciiDoc / TeX
- **URL scraping** — `POST /api/knowledge/url` fetches, extracts, indexes as markdown
- **Folder scan** — preview + batch index
- Stdlib fallbacks (`_read_docx/pptx/xlsx/epub/odt/rtf/ipynb`) kick in if MarkItDown errors on a file

### New settings surfaces

All legacy features now live in v2 — `/legacy` route + `static/index-legacy.html` removed.

- **Full settings editor** (Advanced → Settings) — all 30+ `EDITABLE_SETTINGS` exposed as forms, 7 grouped sections
- **Abort** button in composer during active turn
- **Login modal** for password-protected installs (detects 401)
- **Camera preview** with live `<video>` + test capture
- **TTS reference audio** upload for voice cloning
- **Clear graph** button in Memory
- **Update progress polling**
- **Secrets** — add + list + delete (new `POST /api/secrets`)

### Stability

- Windows `_ProactorBasePipeTransport._call_connection_lost` shutdown errors silenced via `_QuietPolicy` + monkey-patch
- `pyreadline3` compat in `cli.py` (each keybinding wrapped in try/except — fixes startup crash when markitdown transitively pulls pyreadline3)
- WS event names aligned with server (`content_delta`, `thinking_delta`, `tool_call`, `reply`) — previously live streaming silently no-oped
- Scroll preservation across renders (no more jump-to-top after agent replies)
- iPhone safe-area insets on top / bottom / left / right; notch + home-indicator handled

### Removed

- Legacy `/legacy` route
- `static/index-legacy.html` (6 021 lines)

## 🐛 Notable fixes

- Thread switching now actually switches (string IDs, was `parseInt` → NaN)
- Presets list actually populates (server returns `items`, not `presets`)
- Memory stats show real chunk counts from Qdrant payload scroll
- Inspector hydrates from message meta on reload / thread switch
- Tool calls + thinking persist through history reload
- Upload zone supports multi-file (was failing with 409 on 2nd file)
- Preset activation shows real indexed-files count after completion
- Images and files sent via chat survive server restart (stored in `messages.meta`)

## 📦 Upgrade

```bash
# Linux / macOS
curl -fsSL https://raw.githubusercontent.com/deepfounder-ai/qwe-qwe/main/install.sh | bash

# Any platform
git pull && pip install -e . --upgrade
```

New runtime deps auto-installed: `markitdown[all]`, `python-docx`, `python-pptx`, `openpyxl`, `mammoth`, `markdownify`, `beautifulsoup4`, `pdfminer.six`.

## 🌍 Cross-platform

Verified on **Linux**, **macOS** (Intel + Apple Silicon), **Windows 10/11**. All platform-specific branches (shell dispatch, path conversion, asyncio policies, readline compat) are properly guarded — installing on any OS is `pip install -e .`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
