# v0.17.15 — YouTube transcript: proper yt-dlp download + language fix + cookie bypass

User tested `https://www.youtube.com/watch?v=gzrAd7tCqYk` (Russian video) and still got ~800 bytes instead of a transcript. Three problems in one report:

## 🐛 What went wrong

### 1. Direct urllib fetch tripped HTTP 429

v0.17.13 took `track.get("url")` from yt-dlp's `extract_info()` result and then fetched that URL with plain `urllib.request.urlopen()`. That bypasses yt-dlp's authenticated session / cookies / retry logic, so YouTube rate-limits the anonymous scrape. Works for the first 1–2 videos, then **HTTP 429 Too Many Requests** for ~15+ minutes per IP.

### 2. Wrong language picked

Default priority was `[stt_language → en → en-US → en-GB]`. For a Russian video with manual Russian captions + auto-translated English captions, it tried `en` first → hit 429 on the auto-translate fetch → failed. Should have used the Russian manual subs in the first place — manual > auto in any language.

### 3. Fallback output looked empty

When the transcript fetch failed, the old code returned `None` and fell through to markitdown, which scraped the logged-out watch page and got the footer. User saw ~800 bytes of "About / Press / Copyright…" and rightly felt confused.

## 🔧 Fixes

### yt-dlp downloads subtitles itself

`_fetch_youtube_transcript()` now:

1. **Phase 1** — `extract_info(download=False)` to get metadata + caption catalogue.
2. **Phase 2** — runs `ydl.download([url])` with `writesubtitles=True/writeautomaticsub=True` + `subtitleslangs=[want_lang]` + `outtmpl=<tempdir>`. yt-dlp writes the subtitle file to disk using its own session (same cookies, retries, backoff). We read the file back and clean it.
3. **Phase 3** — `shutil.rmtree(tmpdir)` in `finally:` so we don't leak temp files.

Retry settings added: `extractor_retries=3`, `retries=5`, `fragment_retries=5`, `sleep_interval_requests=1`, `socket_timeout=30`.

### Smarter language priority

```
1. Manual caption matching user's stt_language (if set)
2. Manual caption in English family
3. ANY manual caption (native-language manual beats auto-translated English)
4. Auto caption matching stt_language or English family
```

For the user's Russian video: now picks the manual Russian track instead of trying an auto-translated English track that never arrives.

### `yt_cookies_from_browser` setting

**Settings → Memory → `yt_cookies_from_browser`** — put `chrome`, `firefox`, `edge`, `safari`, `brave`, `chromium`, `opera`, or `vivaldi` and yt-dlp will pull the YouTube session cookies from that browser. With an authenticated session YouTube's anonymous rate limit basically doesn't apply.

Default is empty (anonymous). If you hit repeated 429s on YouTube URLs, set this to your browser and try again.

### Metadata-only fallback when transcript can't be fetched

If the transcript download fails (network, rate limit, video has no captions, region lock), we no longer return `None` and fall through to markitdown. Instead we return a proper markdown file with **title + channel + duration + description**:

```markdown
<!-- Source: https://www.youtube.com/watch?v=gzrAd7tCqYk -->
<!-- Converter: yt-dlp -->
<!-- Video: gzrAd7tCqYk · 1297s · ? -->
<!-- Content: metadata only (transcript_download_failed) -->

# Почему Эволюция Снова и Снова Создаёт Одних и Тех Же Животных

**Channel:** ВымершийЗоопарк · **Duration:** 1297s

Под сланцами Германии рабочие обнаружили ископаемые, которых не должно было существовать…
```

For the problem URL: **917 bytes of real content** (title + Russian description) instead of 745 bytes of YouTube footer.

Tagged with `source:youtube:metadata-only` so you can find / re-import them later.

### UI surfacing

**Memory → Recent activity** now shows a `⚠ metadata only` badge next to any YouTube entry where the transcript couldn't be fetched, with the reason (e.g. `transcript_download_failed`) in the tooltip.

Status colour is `partial` (amber) for metadata-only, not `done` (green), so it's visually distinct at a glance.

## 📦 Upgrade

```bash
git pull && pip install -e . --upgrade
# Restart the server
```

If you import lots of YouTube URLs and keep hitting rate limits:
**Settings → Memory → yt_cookies_from_browser** → `chrome` (or whatever you use). Make sure you're logged into YouTube in that browser first.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
