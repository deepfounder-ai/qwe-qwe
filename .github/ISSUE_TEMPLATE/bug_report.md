---
name: Bug report
about: Something is broken or behaving unexpectedly
title: ""
labels: bug
assignees: ""
---

## What happened

<!-- One clear sentence. What did you expect, what did you get? -->

## Reproduction

<!-- Minimum steps that trigger the bug. If it's a one-shot chat message, paste it. -->

1.
2.
3.

## Environment

Paste the top of `qwe-qwe --doctor` output (**first 30 lines, up to "System"**).
It includes version, OS, Python, provider, model, and which subsystems are loaded — the exact context needed to diagnose most bugs.

```
$ qwe-qwe --doctor
<paste here>
```

## Logs (optional but helpful)

If the bug is a crash / hang / wrong output, please attach the last ~50 lines of `~/.qwe-qwe/logs/qwe-qwe.log` — redact any sensitive content first.

```
<paste here>
```

## What you think might be wrong

<!-- Optional. If you have a hunch ("maybe the SSRF check missed this URL shape"), share it — saves time. -->

---

Before submitting, please check:

- [ ] I'm on the latest version (`git pull && pip install -e . --upgrade`)
- [ ] I searched existing issues and didn't find a duplicate
- [ ] I redacted any API keys / personal paths / message content I don't want public
