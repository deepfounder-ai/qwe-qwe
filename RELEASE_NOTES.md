# v0.17.13 — YouTube import now actually works (yt-dlp primary path)

User reported YouTube URLs imported as "почти пустой файл" — ~745 bytes of the YouTube footer ("About / Press / Copyright / Contact us") instead of the transcript.

## 🐛 Root cause

MarkItDown bundles `youtube-transcript-api` as the YouTube path. Since mid-2024 YouTube rolled out PotToken (Proof of Origin Token) bot-detection on the `timedtext` API endpoint. Without a valid token the endpoint returns **empty 200 OK** regardless of `fmt=` parameter (tested all of `xml / json3 / srv3 / vtt / ttml` — all zero bytes, even with proper browser headers + Referer + Origin). `youtube-transcript-api` then crashes with `ParseError: no element found`. MarkItDown swallows the exception and scrapes the watch page HTML instead — which for logged-out server traffic is just the footer. Result: empty file, user frustrated.

## 🔧 Fix: yt-dlp as primary YouTube path

`yt-dlp` ships with the JS-based PotToken workarounds and is actively maintained specifically to handle YouTube's bot-protection changes. Now:

```
Is this URL youtube.com / youtu.be / music.youtube.com / m.youtube.com?
  ├── YES → try yt-dlp first
  │         ├── manual subtitles → pick preferred language (user's stt_language setting → en → any)
  │         ├── auto-captions    → same priority chain
  │         ├── no transcript    → save title + channel + description as markdown
  │         └── yt-dlp not installed → fall through
  └── NO  → MarkItDown (unchanged for all other URL types)
```

### What you get in the knowledge base

The saved markdown file now looks like:

```markdown
<!-- Source: https://www.youtube.com/watch?v=dQw4w9WgXcQ -->
<!-- Fetched: 2026-04-21 22:15:03 -->
<!-- Converter: yt-dlp -->
<!-- Video: dQw4w9WgXcQ · 213s · en -->

# Rick Astley - Never Gonna Give You Up (Official Video) (4K Remaster)

**Channel:** Rick Astley · **Duration:** 213s · **Language:** en · **Source:** auto-generated

## Description

The official video for "Never Gonna Give You Up" by Rick Astley. …

## Transcript

(cleaned VTT/TTML — timestamps stripped, dedup'd, no HTML tags)
```

Tagged with `source:url` + `source:youtube` + the original URL so the agent can trace it back.

### Subtitle cleaning

`_clean_subtitle_text()` handles VTT, TTML, and SRV3 formats:

- Drops `WEBVTT` / `NOTE` / `Kind:` / `Language:` headers
- Drops cue numbers and `00:00:01.000 --> 00:00:04.000` timing lines
- Strips `<b>...</b>`, `<c.yellow>...</c>`, and `{\an1}` style markers
- Deduplicates consecutive identical lines (YouTube auto-captions repeat)

### Language priority

1. User's `stt_language` setting (if set in Settings → Voice)
2. Base language (e.g. `stt_language=ru-RU` → also tries `ru`)
3. English (`en`, `en-US`, `en-GB`)
4. Any English-prefixed track
5. First available

### Fallback chain

Even with yt-dlp installed, any failure (private video, region-locked, no transcripts, API hiccup) falls through cleanly to MarkItDown, and finally to stdlib HTML strip. Never crashes the indexer.

## 📦 New dependency

`yt-dlp>=2025.1.0` is now a hard dep (~3.3 MB wheel). Upgrade via:

```bash
git pull && pip install -e . --upgrade
# Restart the server
```

`--doctor` now shows a `yt-dlp` check so you can confirm it installed correctly.

## Verification

Tested on `https://www.youtube.com/watch?v=dQw4w9WgXcQ`:

| Path | Result |
|---|---|
| Old (youtube-transcript-api) | ❌ ParseError — XML empty |
| Old fallback (MarkItDown page scrape) | ❌ 745 bytes footer |
| **New (yt-dlp)** | ✅ 3,331 bytes — title, channel, description, full transcript |

🤖 Generated with [Claude Code](https://claude.com/claude-code)
