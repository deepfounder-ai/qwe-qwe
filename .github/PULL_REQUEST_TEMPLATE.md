<!--
Thanks for contributing! A few pointers:
- Small, focused PRs land faster than big ones. If you can split it, split it.
- Link any issue this fixes: "Fixes #123".
- If you're unsure about the approach, open a Discussion first — we'd rather
  shape the design together than reject a working but wrong-shaped PR.
-->

## Summary

<!-- One sentence: what changes and why. -->

## Test plan

<!-- How you verified this. E.g. "added 3 tests, ran pytest, smoke-tested via qwe-qwe --doctor". -->

- [ ]
- [ ]

## Before merging

- [ ] `ruff check .` passes
- [ ] `pytest tests/` passes (186+ tests)
- [ ] If you touched `static/index.html`: `python scripts/check_js.py` passes
- [ ] If you added a new top-level module: it's listed in `pyproject.toml` `[tool.setuptools] py-modules`
- [ ] If you added a new setting: it's in `config.py` `EDITABLE_SETTINGS` with a description
- [ ] If you changed behaviour: updated relevant tests (not just added new ones)
- [ ] I've read [CONTRIBUTING.md](../blob/main/CONTRIBUTING.md)

## Notes for reviewer

<!-- Anything non-obvious: trade-offs you considered, follow-ups you'd like to ship later, etc. -->
