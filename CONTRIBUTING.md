# Contributing to qwe-qwe

Thanks for your interest. qwe-qwe is **small-model-first, single-process,
local-first**: patches that preserve those properties (no extra daemons, no
cloud dependencies, still runs on a 3B model) are the most welcome. If a
change moves in the opposite direction, please open an issue first so we can
discuss.

## Setup

```bash
git clone https://github.com/deepfounder-ai/qwe-qwe
cd qwe-qwe
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate
pip install -e '.[dev]'
```

Requires Python 3.11+. A local LLM endpoint (LM Studio or Ollama) is needed
to run the agent end-to-end, but tests do not require one.

## Running tests and lint

```bash
pytest tests/ --cov -v  # unit + integration tests with coverage
ruff check .            # lint
```

`pytest --cov` is the canonical local-run command: it measures coverage
alongside the normal test suite. CI enforces a floor (`fail_under` in
`pyproject.toml`, currently **24%**) — a PR that drops total coverage below
that threshold fails the test job. The floor is intentionally a couple of
points below the current baseline so unrelated changes don't trip it, but
real regressions do. Bump it up when coverage improves; don't lower it to
make a PR green.

## Branching and commits

- `main` is always releasable — never push broken builds there.
- Feature work lives on a branch (`feature/foo`, `fix/bar`, …) and lands via pull request.
- **Squash-merge is preferred**; the squashed commit subject should read like a good standalone commit message.
- First line imperative mood, under ~70 chars. Body wraps at 80. Explain *why*, not *what*.
- LLM-assisted commits should include `Co-Authored-By: <model> <noreply@anthropic.com>` at the end of the message.

## Pre-commit checks

CI runs four checks on every push / PR (see `.github/workflows/test.yml`).
Run them locally before opening a PR:

```bash
ruff check .                                            # 1. lint
python -c "import ast, pathlib; [ast.parse(p.read_text('utf-8'), filename=str(p), feature_version=(3,11)) for p in pathlib.Path('.').glob('*.py')]"   # 2. Python 3.11 syntax
python -c "import agent, agent_loop, tools, server, memory, rag, providers"   # 3. import-time smoke
pytest tests/ --cov -v                                  # 4. tests + coverage floor
```

The import-time smoke check matters: a syntax error in `rag.py` will only
surface when a request hits it, and pytest won't catch it unless the test
happens to import that module.

## Adding a tool

Edit `tools.py`: append an OpenAI function-schema entry to the `TOOLS` list
(around line 466) and add a matching branch in `execute()` (around line
1016). Keep the description short — small models need clarity. If the tool
is only occasionally useful, expose it through a skill instead so it doesn't
bloat the default schema.

## Adding a skill

Drop a `.py` file into either the `skills/` package (shipped with the
release) or `~/.qwe-qwe/skills/` (user-local). It must export
`DESCRIPTION`, `INSTRUCTION`, `TOOLS` (OpenAI function schemas), and
`execute(name, args) -> str`. See `skills/notes.py` or `skills/timer.py`
for a minimal template.

## Release flow

1. Bump `VERSION` in **both** `config.py` and `pyproject.toml` (same string).
2. Update the version badge in `README.md`.
3. Add an entry to `RELEASE_NOTES.md` (and `CHANGELOG.md`).
4. Verify the release checklist in [CLAUDE.md](CLAUDE.md#release-checklist) — `py-modules` coverage, `--doctor` checks, compile check, fresh-venv install.
5. `git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin main --tags`.
6. `gh release create vX.Y.Z --title "vX.Y.Z — ..." --notes-file RELEASE_NOTES.md`.

## Dependabot

Dependency upgrades are opened weekly on Monday, grouped by `python-minor-patch`
(one PR for all minor/patch bumps). Security updates **bypass grouping** and
open their own PRs so they can be merged fast. Major bumps for FastAPI,
openai, qdrant-client and pydantic are ignored — those need manual review.
GitHub Actions pins are bumped monthly. Config lives in
`.github/dependabot.yml`.

## See also

- [ARCHITECTURE.md](ARCHITECTURE.md) — system diagram, core modules, state layout.
- [CLAUDE.md](CLAUDE.md) — LLM-agent workflow details and release checklist.
