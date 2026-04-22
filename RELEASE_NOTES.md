# v0.17.16 — YouTube: android player_client + native language detection

User tested the Russian evolution video (`gzrAd7tCqYk`) again and still got metadata-only. Turned out the "429 rate limit" diagnosis in v0.17.15 was only half the story.

## 🔍 Real root cause

After probing with every `player_client` yt-dlp supports (`web`, `ios`, `android`, `mweb`, `tv`, `web_safari`), only **`android` and `ios`** succeeded on this video:

| Client | Result |
|---|---|
| `web` | ❌ "Requested format is not available" |
| `ios` | ❌ format unavailable |
| `mweb` | ❌ format unavailable |
| `tv` | ❌ **"This video is DRM protected"** |
| `web_safari` | ❌ format unavailable |
| `web` + `ios` | ❌ |
| **`android`** | ✅ **237 KB VTT downloaded** |
| `ios` + `android` | ✅ 237 KB VTT (same, via android fallback) |

YouTube is applying **different access policies per client**. Some videos work on the YouTube Android app but are blocked on the web player as DRM-protected. yt-dlp's default client order (starts with `web`) hits the wall. The 429 errors I was seeing were yt-dlp's fallback attempts on the other clients failing for the same reason.

## 🔧 Fix

Pin `extractor_args.youtube.player_client = ["android", "ios", "web"]` in both the info-extract and download phases. yt-dlp tries them in order, first success wins.

### Second fix: prefer the video's native language

The same video had only auto-captions (no manual), in every language on Earth including `ru` (native) and `en` (auto-translated). My code defaulted to `en` because `stt_language` defaults to `en`. YouTube rate-limits auto-translated fetches harder than direct ones, so `en` failed first.

Language priority is now:

```
1. stt_language (if user set it in Settings → Voice)
2. info.language — the video's OWN language from yt-dlp metadata
3. en / en-US / en-GB fallback
```

For a Russian video: priority becomes `[ru, en, en-US, en-GB]` → picks `ru` auto-caption (direct, not translated) → downloads instantly.

## ✅ Verified

Same URL that returned 917 bytes of fallback metadata before:

```
META: {'lang': 'ru', 'has_transcript': True, 'source_type': 'auto-generated', 'fmt': 'vtt'}
MD LEN: 22,642
Transcript LEN: 21,640

Transcript first 500 chars:
В немецком сланце Позитония рабочие добывают камень уже веками,
превращая древнее морское дно в черепицу и столешницы. Но иногда
их инструменты натыкаются на что-то неожиданное…
```

**22 KB of real content** (21.6 KB transcript) in 1.2 s, zero 429s.

## 📦 Upgrade

```bash
git pull && pip install -e . --upgrade
# Restart the server
```

The `yt_cookies_from_browser` setting from v0.17.15 is still there as a second layer — use it if you hit issues with more-protected videos.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
