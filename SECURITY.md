# Security Policy

## Supported versions

qwe-qwe is a rapidly iterating solo/small-team project. We support **only the latest minor release** (currently `0.17.x`). If you're on an older version, upgrade first:

```bash
git pull && pip install -e . --upgrade
```

## Reporting a vulnerability

**Please do NOT open a public issue for security bugs.** Instead:

1. Preferred — use GitHub's **Private Security Advisory** flow:
   Repository → **Security** tab → **Report a vulnerability** → fill out the form.
   Only repo maintainers see it until we publish a fix.
2. Alternative — DM [@kir_altman](https://github.com/kir_altman) on GitHub with subject `qwe-qwe security: <short title>`.

We aim to acknowledge within **48 hours**. For severe issues (remote code execution, secret exfiltration) we'll cut a patch release within a week and credit you in the release notes if you'd like.

## What's in scope

qwe-qwe runs shell commands, fetches URLs, reads/writes files, and handles API keys. The following are all fair game for reports:

- **Shell safety bypass**: `tools._check_shell_safety` is a *speed bump*, not a fence (documented in `tools.py`). However, novel obfuscation patterns that slip past the hardened patterns (Unicode normalisation, hex unescape, `$(...)` dynamic command construction, etc.) are worth reporting — we extend the catalogue over time. See `tests/test_shell_safety.py` for the current bypass collection.
- **Path traversal**: `tools._resolve_path(..., for_write=True)` enforces a workspace whitelist. If you can write outside `~/.qwe-qwe/workspace/`, `~/.qwe-qwe/`, or the project cwd, that's a bug.
- **SSRF**: `/api/knowledge/url` blocks private / loopback / link-local IPs via `socket.getaddrinfo` + `ipaddress.ip_address`. If you can bypass (DNS rebinding post-check, header smuggling, redirect chains) — report.
- **Secret exfiltration via memory**: `memory._scrub_secrets` redacts common key shapes (OpenAI, Anthropic, Groq, GitHub, AWS, Slack, JWT, dotenv lines) before persistence. If you can find a key format that slips through, report + we'll extend the regex catalogue (`tests/test_secret_scrub.py`).
- **Web UI XSS**: static/index.html interpolates user/agent content via `innerHTML`; `esc()` is the canonical escape helper. Missing escape calls on untrusted data (filenames, URLs, chat content, memory pills, graph labels) are real bugs.
- **`/api/kv` write**: the blocklist in `server.py` rejects writes to `telegram:owner_id`, `version:`, `setup_`, `_migrated_`, `provider:config:`, `setting:`, `soul:`. If you can bypass the allowlist to clobber internal state, report.
- **Authentication**: when `QWE_PASSWORD` is set and LAN access is enabled, routes under `/api/*` and `/ws` require the password cookie. Bypasses welcome.
- **MCP server subprocess**: `mcp_client.py` spawns external processes. Escape via stdio injection, subprocess escalation, or circuit-breaker bypass counts.
- **Vault**: `vault.py` stores encrypted secrets via Fernet. Key-derivation or plaintext leak is in scope.

## What's NOT in scope

- **Local privilege escalation via the agent**: qwe-qwe runs with your user privileges. The agent *can* (and is designed to) run shell commands, read your files, send HTTP requests. That's the feature. If you're concerned, run it in a container with a read-only rootfs and no network — not a bug for us to fix.
- **Your local LLM provider misconfiguration**: LM Studio / Ollama running with LAN exposed is your problem, not ours. We default everything to localhost.
- **Secrets in `~/.qwe-qwe/qwe_qwe.db`**: it's encrypted on disk only via whatever your filesystem provides. If someone has read access to your home directory, they have your data — this is expected.
- **Social engineering of the LLM** (prompt injection, jailbreak): we care about *downstream* consequences (e.g. the model tells `shell` to `rm -rf /`, and it runs — that's in scope via shell safety). But "I made the model say a bad word" isn't a security issue.
- **DoS via unbounded memory / CPU**: if you OOM your own machine by sending gigabytes of input, that's not a security report.
- **Issues in transitive dependencies** without a concrete exploit path in qwe-qwe. Dependabot handles those separately.

## Disclosure timeline

1. You report → we acknowledge within 48h.
2. We investigate + propose a fix → expect 1-7 days depending on severity.
3. We merge the fix to `main`, release a patch, update release notes crediting you (if you want).
4. 90 days after the patch ships, the full details of the vulnerability can be disclosed publicly — earlier by mutual agreement.

Thank you for helping keep qwe-qwe users safe.
