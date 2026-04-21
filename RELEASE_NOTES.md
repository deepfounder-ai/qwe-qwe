# v0.17.1 — Mobile layout fixes

Patch release focused on mobile UI issues reported after v0.17.0 shipped.

## 🐛 Fixes

- **Mobile hamburger not visible** — CSS source order made `.mobile-hamburger { display: none }` override the `@media (max-width:780px)` rule that should have shown it. Now hidden by default (before media query) so the media query correctly re-enables it on phones. Tap the 💬 icon in the topbar → thread drawer slides in → tap `+` to create a thread.
- **Settings inputs crushed on narrow screens** — the `.setting-row` grid with a 220-px fixed control column was squeezing labels to ~100 px on 375-px iPhones. On mobile the row now stacks vertically: label + description on top, control full-width underneath. Applies everywhere — Model, Soul, Voice, Camera, Network, Privacy, Account, Advanced → Settings.
- **Provider / Model picker cards cut off** — forced 2-column grid on mobile was making every card too narrow for provider names like `qwen3.5-9b-instruct@openrouter`. Now a single column with text wrapping (`word-break: break-word` / `break-all` for URLs).

## 📦 Upgrade

```bash
git pull && pip install -e . --upgrade
```

Or the one-liner:

```bash
curl -fsSL https://raw.githubusercontent.com/deepfounder-ai/qwe-qwe/main/install.sh | bash
```

🤖 Generated with [Claude Code](https://claude.com/claude-code)
