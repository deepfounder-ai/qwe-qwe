"""Import Anthropic-style SKILL.md skills from skills.sh / GitHub.

skills.sh and the Anthropic-skills repo (and forks) publish skills as
directories containing a SKILL.md (YAML frontmatter + Markdown body)
plus optional scripts/, references/, and assets/. qwe-qwe skills are
single Python `.py` files with TOOLS + execute(). This module bridges
the two by:

1. **Fetching** the SKILL.md (+ optional adjacent files) from a user-
   supplied URL. Two URL shapes are recognised:
     - https://skills.sh/<owner>/<repo>/<skill-name>  (skills.sh browse)
     - https://github.com/<owner>/<repo>/tree/<ref>/.../<skill-name>
   Both resolve to a GitHub raw download of the same SKILL.md.

2. **Parsing** the YAML frontmatter for `name`, `description`,
   `license`, `compatibility`, `metadata` (per agentskills.io spec).

3. **Generating** a thin adapter `.py` at
   `~/.qwe-qwe/skills/<name>.py` whose:
     - DESCRIPTION = frontmatter description
     - INSTRUCTION = a short intro pointing at the `<name>_help` tool
       (full SKILL.md body is too big to inject every turn — token
       budget protection)
     - TOOLS = one `<name>_help()` returning the full SKILL.md body
     - execute() dispatches it

4. **Staging** any additional scripts / references the upstream skill
   ships into `~/.qwe-qwe/skills_imported/<name>/` so the agent's
   regular read_file / shell tools can use them.

5. **Recording** the import in the `skill_imports` SQLite table so
   we have provenance (source URL, hash, license, timestamp) for
   audits + future "check for upstream updates" flows.

## Safety

- SSRF: URL must use http/https, must NOT resolve to private /
  loopback / link-local IPs (override with QWE_ALLOW_PRIVATE_URLS=1)
- Domain allowlist: skills.sh and github.com/raw.githubusercontent.com
- Name validation: matches the spec's `[a-z0-9-]{1,64}` constraint
- Collision protection: refuses to overwrite built-in skills,
  requires explicit `overwrite=True` for user-skill collisions
- License surfacing: imports of source-available content (Anthropic
  docx/pdf/pptx/xlsx) return the license in the response so the UI
  can show a confirmation step
- Size caps: SKILL.md ≤ 100 KB, total fetched bytes ≤ 1 MB, ≤ 50
  files per skill

This module is import-safe under tests: external HTTP calls go
through `_fetch_url` which tests monkeypatch.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import time
import urllib.error    # explicit — urllib.request transitively imports it
import urllib.parse    # but relying on the transitive import is fragile
import urllib.request
from pathlib import Path

import config
import db


# ── URL parsing / domain allowlist ──────────────────────────────────


_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_MAX_NAME_LEN = 64
_ALLOWED_HOSTS = {
    "skills.sh",
    "www.skills.sh",
    "github.com",
    "raw.githubusercontent.com",
    "api.github.com",
}
_HTTP_TIMEOUT = 15.0
_SKILL_MD_CAP = 100 * 1024            # 100 KB
_TOTAL_FETCH_CAP = 1024 * 1024        # 1 MB
_MAX_FILES_PER_SKILL = 50
_FETCH_USER_AGENT = "qwe-qwe-skill-importer"


class SkillImportError(Exception):
    """Raised for any expected import failure (bad URL, validation,
    collision, etc.). Caught by the REST endpoint and surfaced as a
    400/403/409 with the message as the error body. NOT used for
    "the agent did something weird" — those propagate as 500."""

    def __init__(self, message: str, code: str = "import_failed",
                 status: int = 400, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}


def _check_url_safety(url: str) -> None:
    """Raise SkillImportError if the URL is unsafe (wrong scheme,
    disallowed host, resolves to private IP). Mirrors the SSRF
    pattern used by server.py::_url_resolves_to_private for
    /api/knowledge/url — same env-var escape hatch."""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        raise SkillImportError("Invalid URL", code="bad_url") from None
    if parsed.scheme not in ("http", "https"):
        raise SkillImportError(
            f"URL scheme must be http or https, got {parsed.scheme!r}.",
            code="bad_scheme")
    host = (parsed.hostname or "").lower()
    if not host:
        raise SkillImportError("URL is missing a hostname.", code="bad_url")
    # Domain allowlist — only skills.sh + GitHub. Add more here if
    # we ever support generic repos.
    if host not in _ALLOWED_HOSTS:
        raise SkillImportError(
            f"Host {host!r} is not in the import allowlist. "
            f"Allowed: {', '.join(sorted(_ALLOWED_HOSTS))}.",
            code="host_not_allowed",
            status=403)
    # SSRF guard (skip if env var set — same opt-out as
    # /api/knowledge/url for self-hosted GitHub Enterprise etc.)
    if os.environ.get("QWE_ALLOW_PRIVATE_URLS", "").strip() == "1":
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception as e:
        raise SkillImportError(
            f"Could not resolve {host}: {e}", code="dns_failed",
            status=502) from None
    for info in infos:
        addr = info[4][0]
        if "%" in addr:
            addr = addr.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified:
            raise SkillImportError(
                f"URL resolves to a private address ({addr}). "
                "Set QWE_ALLOW_PRIVATE_URLS=1 to override.",
                code="private_ip",
                status=403)


class _SafetyCheckingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate each redirect target through `_check_url_safety`.

    The default `urllib.request` follows 3xx silently. A compromised
    or evil upstream could 302 the SKILL.md fetch to `http://169.254.
    169.254/...` (cloud metadata service) or any other private/
    loopback IP. The initial SSRF check on the user-supplied URL
    is meaningless if redirects aren't checked.

    Spec: HTTPRedirectHandler.redirect_request returns a new Request
    or None. We can't easily raise in there (urllib's flow eats some
    exceptions), so we set an attr on the handler and check after
    the fetch. The fetch itself is short-circuited by raising a
    SkillImportError inside http_error_30X.
    """

    def http_error_301(self, req, fp, code, msg, headers):
        self._check_redirect_target(req, headers)
        return super().http_error_301(req, fp, code, msg, headers)

    def http_error_302(self, req, fp, code, msg, headers):
        self._check_redirect_target(req, headers)
        return super().http_error_302(req, fp, code, msg, headers)

    def http_error_303(self, req, fp, code, msg, headers):
        self._check_redirect_target(req, headers)
        return super().http_error_303(req, fp, code, msg, headers)

    def http_error_307(self, req, fp, code, msg, headers):
        self._check_redirect_target(req, headers)
        return super().http_error_307(req, fp, code, msg, headers)

    def http_error_308(self, req, fp, code, msg, headers):
        self._check_redirect_target(req, headers)
        return super().http_error_308(req, fp, code, msg, headers)

    def _check_redirect_target(self, req, headers):
        new_url = headers.get("Location") or headers.get("URI") or ""
        if not new_url:
            return
        # Absolutise — relative Location headers are legal per RFC.
        new_url = urllib.parse.urljoin(req.full_url, new_url)
        try:
            _check_url_safety(new_url)
        except SkillImportError as e:
            # Re-raise with a more specific message so the user sees
            # this came from a redirect, not the initial URL.
            raise SkillImportError(
                f"Redirect to {new_url!r} blocked: {e}",
                code="redirect_blocked",
                status=e.status,
            ) from None


# Module-level opener with our safety-checking redirect handler.
# Built lazily on first use so test monkeypatches of urllib still
# work (some tests stub urllib.request.urlopen directly).
_opener = None


def _get_opener():
    global _opener
    if _opener is None:
        _opener = urllib.request.build_opener(_SafetyCheckingRedirectHandler())
    return _opener


def _fetch_url(url: str, max_bytes: int) -> bytes:
    """Fetch a URL with size cap. Validates safety first AND
    re-validates each redirect target (in case of 30X to a private
    IP)."""
    _check_url_safety(url)
    req = urllib.request.Request(url, headers={
        "User-Agent": _FETCH_USER_AGENT,
        # Pin uncompressed transfer so our size cap can't be bypassed
        # by gzip-compressed responses (we'd cap bytes-on-wire, then
        # decompress to many MB of content). GitHub serves raw files
        # uncompressed by default but this locks the contract.
        "Accept-Encoding": "identity",
    })
    # Use our custom opener so redirects get the safety check.
    # Note: tests that stub `urllib.request.urlopen` directly still
    # work — they bypass the opener — that's the intended monkeypatch
    # surface.
    opener = _get_opener()
    with opener.open(req, timeout=_HTTP_TIMEOUT) as r:
        body = r.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise SkillImportError(
            f"Response exceeds {max_bytes} byte cap "
            f"(URL={url!r}). Refusing to download oversize content.",
            code="oversize",
            status=413)
    return body


# ── URL resolution: skills.sh / github.com → GitHub raw refs ───────


_GITHUB_TREE_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.+?)/?$"
)
_GITHUB_RAW_RE = re.compile(
    r"^https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+?)/?$"
)
_SKILLS_SH_RE = re.compile(
    r"^https?://(?:www\.)?skills\.sh/([^/]+)/([^/]+)(?:/([^/]+))?/?$"
)


def resolve_skill_source(url: str) -> dict:
    """Map a user-supplied URL to canonical info we can fetch from.

    Returns:
        {
            "owner": str,
            "repo": str,
            "ref": str,                   # branch / tag / commit (default "main")
            "path": str,                  # path under repo (e.g. "skills/pdf")
            "kind": "skills_sh" | "github",
            "canonical_url": str,         # the URL we'll record for provenance
        }

    Raises SkillImportError on URLs we can't parse.
    """
    url = (url or "").strip().rstrip("/")
    if not url:
        raise SkillImportError("URL required.", code="bad_url")

    m = _SKILLS_SH_RE.match(url)
    if m:
        owner, repo, skill = m.group(1), m.group(2), m.group(3)
        if not skill:
            raise SkillImportError(
                "skills.sh URL must include a skill name "
                f"(e.g. https://skills.sh/{owner}/{repo}/<skill-name>).",
                code="bad_url")
        # Convention: skills.sh references map to GitHub
        # `<owner>/<repo>/skills/<skill>/` (most skills.sh-listed
        # repos use that layout). We probe both `skills/<skill>` and
        # bare `<skill>` later if the first 404s.
        return {
            "owner": owner, "repo": repo, "ref": "main",
            "path": f"skills/{skill}",
            "fallback_path": skill,        # try this if first 404s
            "skill_name_hint": skill,
            "kind": "skills_sh",
            "canonical_url": url,
        }

    m = _GITHUB_TREE_RE.match(url)
    if m:
        owner, repo, ref, path = m.group(1), m.group(2), m.group(3), m.group(4)
        return {
            "owner": owner, "repo": repo, "ref": ref, "path": path,
            "fallback_path": None,
            "skill_name_hint": path.rsplit("/", 1)[-1],
            "kind": "github",
            "canonical_url": url,
        }

    m = _GITHUB_RAW_RE.match(url)
    if m:
        owner, repo, ref, path = m.group(1), m.group(2), m.group(3), m.group(4)
        # If user pasted a raw URL pointing AT SKILL.md, drop the file
        # name; we want the directory.
        if path.endswith("/SKILL.md"):
            path = path[: -len("/SKILL.md")]
        return {
            "owner": owner, "repo": repo, "ref": ref, "path": path,
            "fallback_path": None,
            "skill_name_hint": path.rsplit("/", 1)[-1],
            "kind": "github",
            "canonical_url": url,
        }

    raise SkillImportError(
        "Unrecognised URL. Supported shapes:\n"
        "  https://skills.sh/<owner>/<repo>/<skill-name>\n"
        "  https://github.com/<owner>/<repo>/tree/<ref>/<path>/<skill-name>\n"
        "  https://raw.githubusercontent.com/<owner>/<repo>/<ref>/<path>/<skill-name>/SKILL.md",
        code="bad_url")


def _raw_url(owner: str, repo: str, ref: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"


def _tree_api_url(owner: str, repo: str, ref: str) -> str:
    return f"https://api.github.com/repos/{owner}/{repo}/git/trees/{ref}?recursive=1"


# ── YAML frontmatter parser (subset — no external dep) ─────────────


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?\n)---\s*\n?(.*)\Z", re.DOTALL)


def parse_skill_md(body: str) -> dict:
    """Parse SKILL.md into {frontmatter: {...}, body: "..."}.

    Supports the subset of YAML used by the agentskills.io spec:
    flat key: value pairs, quoted strings, simple lists (one item
    per line with leading `-`), and one level of nested mapping
    for `metadata`. Doesn't pull pyyaml as a dep — the spec's
    surface is tiny and well-bounded.

    Raises SkillImportError if the document has no frontmatter or
    the required `name` / `description` fields are missing.
    """
    if not isinstance(body, str):
        raise SkillImportError("SKILL.md is not text.", code="bad_skill_md")
    m = _FRONTMATTER_RE.match(body)
    if not m:
        raise SkillImportError(
            "SKILL.md is missing YAML frontmatter (expected ---/.../--- block at the top).",
            code="no_frontmatter")
    frontmatter_text, markdown_body = m.group(1), m.group(2)
    fm = _parse_yaml_subset(frontmatter_text)
    if not isinstance(fm, dict):
        raise SkillImportError(
            "SKILL.md frontmatter must be a mapping.",
            code="bad_frontmatter")
    if not fm.get("name"):
        raise SkillImportError(
            "SKILL.md frontmatter missing required `name` field.",
            code="missing_name")
    if not fm.get("description"):
        raise SkillImportError(
            "SKILL.md frontmatter missing required `description` field.",
            code="missing_description")
    return {"frontmatter": fm, "body": markdown_body.strip()}


def _parse_yaml_subset(text: str) -> dict:
    """Parse the YAML subset used by SKILL.md frontmatter.

    Recognises:
      key: value
      key: "quoted value"           — outer quotes stripped
      key: 'quoted value'
      key: value  # trailing-comment (stripped)
      key:
        sub: nested                 — one level of nested mapping

    LOUDLY REJECTS (raises SkillImportError rather than silently
    losing data):
      key: |                        — block scalar (literal newlines)
      key: >                        — block scalar (folded)
      key:                          — list scalar
        - item

    The reject-loud policy is the right call: SKILL.md authors who
    use these forms in `description:` or `allowed-tools:` would
    silently get truncated / empty data with a quiet parse. Better
    to fail the import so they (or we) know to extend the parser.
    """
    out: dict = {}
    current_key: str | None = None
    current_indent = -1
    for raw_line in text.split("\n"):
        line = raw_line.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()

        # Detect YAML list items (`- foo`) — we don't parse them, so
        # if we see one under a `key:` with empty value, raise loud.
        if stripped.startswith("- "):
            if current_key:
                raise SkillImportError(
                    f"SKILL.md frontmatter uses a YAML list under "
                    f"`{current_key}:` — qwe-qwe's minimal parser "
                    f"doesn't support lists. Either rewrite the "
                    f"frontmatter as a flat scalar (e.g. space-"
                    f"separated string), or open an issue to extend "
                    f"the parser.",
                    code="yaml_list_unsupported")
            # Top-level `- ` outside any key — malformed
            raise SkillImportError(
                "SKILL.md frontmatter has a `- ` list item at the "
                "top level. Frontmatter must be a mapping (key: value).",
                code="bad_frontmatter")

        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()

        # Strip trailing `# comment` BEFORE quote-strip. YAML comments
        # are space-hash-anything-to-EOL but only outside quoted
        # strings. We approximate: only strip when there's a space
        # before the hash AND the line isn't fully quoted.
        if value and not (len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'")):
            hash_idx = value.find(" #")
            if hash_idx >= 0:
                value = value[:hash_idx].rstrip()

        # Strip matched outer quotes
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]

        # Detect block-scalar markers AFTER stripping comments + quotes.
        # `|`, `>`, `|-`, `>-`, `|+`, `>+` are all YAML block-scalar
        # indicators that say "the value follows on indented lines."
        # Our parser doesn't handle the indented content, so the
        # caller would get `value == "|"` / etc. — silently wrong.
        if value in ("|", ">", "|-", ">-", "|+", ">+"):
            raise SkillImportError(
                f"SKILL.md frontmatter uses a YAML block scalar "
                f"(`{key}: {value}`) — qwe-qwe's minimal parser "
                f"doesn't support multi-line block scalars. Rewrite "
                f"the value as a single-line quoted string, or open "
                f"an issue to extend the parser.",
                code="yaml_block_scalar_unsupported")

        if not value:
            # Could be a nested-mapping start (key: \n  sub: ...)
            current_key = key
            current_indent = indent
            out[key] = {}
            continue
        if indent > current_indent and current_key and isinstance(out.get(current_key), dict):
            # Nested under previous "key:" with empty value
            out[current_key][key] = value
        else:
            out[key] = value
            current_key = None
            current_indent = -1
    return out


# ── Tree listing (for staging assets) ──────────────────────────────


def _list_repo_tree(owner: str, repo: str, ref: str, base_path: str) -> list[dict]:
    """Return the list of files under `<base_path>/` in the repo,
    via the GitHub trees API. Each entry: {path, size, type}.

    Returns [] if the API call fails — staging assets is opportunistic;
    the import still succeeds with just the SKILL.md if listing fails.
    """
    try:
        body = _fetch_url(_tree_api_url(owner, repo, ref), max_bytes=_TOTAL_FETCH_CAP)
        data = json.loads(body)
    except Exception:
        return []
    if not isinstance(data, dict) or "tree" not in data:
        return []
    base_prefix = base_path.strip("/") + "/"
    entries: list[dict] = []
    for item in data.get("tree", []):
        path = item.get("path", "")
        if not path.startswith(base_prefix):
            continue
        if item.get("type") != "blob":
            continue
        # Bound by file count + by individual file size (we re-check
        # the cumulative cap at fetch time too).
        if len(entries) >= _MAX_FILES_PER_SKILL:
            break
        entries.append({
            "path": path[len(base_prefix):],
            "size": int(item.get("size") or 0),
            "type": item.get("type"),
        })
    return entries


# ── Install pipeline ──────────────────────────────────────────────


def _user_skills_dir() -> Path:
    """The directory where user-installed `.py` skills live. Mirrors
    `skills.__init__.USER_SKILLS_DIR` to avoid a circular import."""
    return Path(config.DATA_DIR) / "skills"


def _imported_assets_dir() -> Path:
    """Where we stage the upstream skill's scripts/references/assets.
    Separate from the user skills dir because the agent's read_file
    tool reaches into this directory, but qwe-qwe's skill loader
    only scans for `.py` files in skills/."""
    return Path(config.DATA_DIR) / "skills_imported"


def _check_name(name: str) -> None:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise SkillImportError(
            f"Skill name {name!r} must match {_NAME_RE.pattern} "
            f"(lowercase letters, digits, hyphens; ≤{_MAX_NAME_LEN} chars; "
            "no leading/trailing/consecutive hyphens).",
            code="bad_name")
    if len(name) > _MAX_NAME_LEN:
        raise SkillImportError(
            f"Skill name longer than {_MAX_NAME_LEN} chars.",
            code="bad_name")


_BUILTIN_SKILL_NAMES = {
    "browser", "canvas", "mcp_manager", "notes", "serial_port",
    "skill_creator", "soul_editor", "spicy_duck", "timer", "weather",
}


def _check_collision(name: str, overwrite: bool) -> None:
    """Refuse to clobber built-in skills. User-skill collision requires
    overwrite=True."""
    if name in _BUILTIN_SKILL_NAMES:
        raise SkillImportError(
            f"'{name}' collides with a built-in qwe-qwe skill. "
            "Built-in skills are NOT overridable via import — "
            "rename the imported skill or fork it under a different "
            "directory name in the upstream repo.",
            code="builtin_collision",
            status=409)
    target = _user_skills_dir() / f"{name}.py"
    if target.exists() and not overwrite:
        raise SkillImportError(
            f"A user skill named '{name}' already exists. "
            "Pass overwrite=true to replace it.",
            code="user_collision",
            status=409,
            details={"existing": str(target)})


def import_skill(url: str, overwrite: bool = False,
                 accept_license: bool = False) -> dict:
    """Fetch + install a skills.sh / GitHub skill.

    Returns the same shape as the REST endpoint:
        {
            "name": str,
            "description": str,
            "license": str | None,
            "source_url": str,
            "py_path": str,
            "assets_dir": str,
            "files_imported": [str, ...],
            "tools_count": int,
            "hash": str,
        }

    Raises SkillImportError on validation failures.
    """
    info = resolve_skill_source(url)

    # Fetch SKILL.md — try primary path, then fallback if 404
    paths_to_try = [info["path"]]
    if info.get("fallback_path"):
        paths_to_try.append(info["fallback_path"])

    skill_md_body: str | None = None
    chosen_path: str | None = None
    last_err: Exception | None = None
    for path in paths_to_try:
        try:
            body = _fetch_url(
                _raw_url(info["owner"], info["repo"], info["ref"], f"{path}/SKILL.md"),
                max_bytes=_SKILL_MD_CAP,
            )
            skill_md_body = body.decode("utf-8", errors="replace")
            chosen_path = path
            break
        except urllib.request.HTTPError as e:
            if e.code == 404:
                last_err = e
                continue
            raise SkillImportError(
                f"Failed to fetch SKILL.md: HTTP {e.code} ({e.reason}).",
                code="fetch_failed", status=502) from None
        except urllib.error.URLError as e:
            last_err = e
            continue

    if skill_md_body is None or chosen_path is None:
        raise SkillImportError(
            f"Could not find SKILL.md at any of: "
            f"{', '.join(_raw_url(info['owner'], info['repo'], info['ref'], p + '/SKILL.md') for p in paths_to_try)}."
            + (f" Last error: {last_err}" if last_err else ""),
            code="not_found", status=404)

    parsed = parse_skill_md(skill_md_body)
    fm = parsed["frontmatter"]
    skill_md_md = parsed["body"]

    name = str(fm.get("name", "")).strip().lower()
    _check_name(name)
    _check_collision(name, overwrite=overwrite)

    description = str(fm.get("description", "")).strip()
    license_field = (str(fm.get("license") or "").strip()) or None

    # Surface "source-available" licenses to the caller. Pure OSS
    # identifiers (Apache-2.0, MIT, BSD-*, GPL-*) pass silently;
    # anything else we treat as "user must confirm" (e.g. Anthropic's
    # docx/pdf/etc. which say "Complete terms in LICENSE.txt").
    needs_license_confirm = False
    if license_field:
        if not _looks_like_oss_license(license_field):
            needs_license_confirm = True
    if needs_license_confirm and not accept_license:
        raise SkillImportError(
            f"Skill '{name}' has a non-OSS-style license: {license_field!r}. "
            "Some skills.sh skills (e.g. Anthropic's docx/pdf/pptx/xlsx) "
            "are source-available, not open source. Confirm you accept "
            "the upstream terms by re-importing with accept_license=true.",
            code="license_confirm_required",
            status=451,
            details={"license": license_field, "name": name,
                     "description": description})

    # Stage assets (scripts/, references/, assets/) — opportunistic
    tree = _list_repo_tree(info["owner"], info["repo"], info["ref"], chosen_path)
    asset_dir = _imported_assets_dir() / name
    asset_dir.mkdir(parents=True, exist_ok=True)
    # Always save the SKILL.md too — provenance & easy reading
    (asset_dir / "SKILL.md").write_text(skill_md_body, encoding="utf-8")

    files_imported = ["SKILL.md"]
    total_bytes = len(skill_md_body.encode("utf-8", errors="replace"))
    for entry in tree:
        rel = entry["path"]
        if rel == "SKILL.md":
            continue
        # Skip dotfiles, lockfiles, and the things we don't want to
        # auto-stage (binaries, fonts, etc. would blow up our budget).
        if rel.startswith(".") or any(rel.startswith(p) for p in (".github/",)):
            continue
        # Only stage common asset / script extensions. Binaries/images
        # are excluded by default — the user can pull them later if
        # they want.
        if not _is_safe_asset_path(rel):
            continue
        url_one = _raw_url(info["owner"], info["repo"], info["ref"],
                            f"{chosen_path}/{rel}")
        try:
            content = _fetch_url(url_one, max_bytes=_SKILL_MD_CAP * 4)
        except Exception:
            continue
        if total_bytes + len(content) > _TOTAL_FETCH_CAP:
            break
        total_bytes += len(content)
        out_path = asset_dir / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(content)
        files_imported.append(rel)

    # Compute hash over canonical SKILL.md content (for future
    # update-check workflow).
    sha = hashlib.sha256(skill_md_body.encode("utf-8", errors="replace")).hexdigest()

    # Generate adapter .py and write to user skills dir.
    py_body = _render_adapter_py(
        name=name,
        description=description,
        skill_md_body=skill_md_md,
        asset_dir_abs=str(asset_dir.resolve()),
        source_url=info["canonical_url"],
        license_field=license_field,
    )
    user_dir = _user_skills_dir()
    user_dir.mkdir(parents=True, exist_ok=True)
    py_path = user_dir / f"{name}.py"

    # Write atomically: temp file with .py suffix (so validate_skill
    # can importlib.util.spec_from_file_location it) in the SAME
    # directory (so os.replace is atomic on every OS), then validate,
    # then os.replace into final position.
    #
    # The temp filename uses `__qwepartial__<name>__<uuid>.py` —
    # picked so it (a) ends in .py for the validator, (b) starts
    # with `__` so the skill loader's _all_skill_paths() (which
    # globs `*.py` and treats stem as skill name) ignores it as
    # "private" if anyone scans before we replace.
    import tempfile
    fd, tmp_str = tempfile.mkstemp(
        suffix=".py", prefix=f"__qwepartial__{name}__", dir=str(user_dir))
    tmp_path = Path(tmp_str)
    try:
        os.close(fd)
        tmp_path.write_text(py_body, encoding="utf-8")
        # Validate before the atomic replace — if the generated
        # adapter doesn't pass skills.validate_skill, the install
        # would land a broken .py that fails on next skill load.
        # Better to fail loud here. Avoid circular import via local.
        from . import validate_skill as _vs
        ok, errs = _vs(str(tmp_path))
        if not ok:
            try:
                tmp_path.unlink()
            except Exception:
                pass
            raise SkillImportError(
                "Generated adapter failed skills.validate_skill: "
                + "; ".join(errs)[:400] +
                ". This is a bug in skills/skill_import.py — open an "
                "issue with the upstream URL.",
                code="adapter_invalid", status=500)
        os.replace(str(tmp_path), str(py_path))
    finally:
        # Cleanup tempfile if it's still around (success path moved
        # it via os.replace; failure path tried to unlink already).
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass

    # Record provenance
    _record_import(
        name=name, source_url=info["canonical_url"], source_kind=info["kind"],
        hash_=sha, license_field=license_field, description=description,
        meta={"files_imported": files_imported,
              "frontmatter_metadata": fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {},
              "compatibility": fm.get("compatibility")},
    )

    return {
        "name": name,
        "description": description,
        "license": license_field,
        "source_url": info["canonical_url"],
        "py_path": str(py_path.resolve()),
        "assets_dir": str(asset_dir.resolve()),
        "files_imported": files_imported,
        "tools_count": 1,
        "hash": sha,
    }


# ── Helpers ────────────────────────────────────────────────────────


# OSS-license detection — word-bounded regex over an SPDX-ish set.
# Anchored at word boundaries so "MIT-style license, not the actual
# MIT" doesn't match (whereas the old substring match did). Also
# matches AGPL via the trailing-`GPL-3` substring + AGPL is itself
# an OSS license so that's still correct.
_OSS_LICENSE_RE = re.compile(
    r"\b("
    r"APACHE[ -]?2(\.\d+)?"
    r"|MIT(-0)?"
    r"|BSD[ -]?[23](-CLAUSE)?"
    r"|GPL[ -]?[23](\.\d+)?(-OR-LATER)?"
    r"|AGPL[ -]?3(\.\d+)?(-OR-LATER)?"
    r"|LGPL[ -]?[23](\.\d+)?"
    r"|MPL[ -]?2(\.\d+)?"
    r"|ISC"
    r"|UNLICENSE"
    r"|CC0(-1\.0)?"
    r")\b",
    re.IGNORECASE,
)
# Known non-OSS-but-source-available markers. If any of these appear,
# we OVERRIDE an OSS match (because licenses like "Apache 2.0 with
# Commons Clause" claim Apache but aren't OSS due to the rider).
_NON_OSS_OVERRIDES = (
    "COMMONS CLAUSE",
    "BSL-1.1", "BSL 1.1", "BUSINESS SOURCE LICENSE", "BUSL",
    "SSPL",                                     # Server Side Public License
    "ELASTIC LICENSE", "ELASTIC-LICENSE",       # Elastic v2
    "PROPRIETARY",
    "ALL RIGHTS RESERVED",
    "COMPLETE TERMS IN",                        # Anthropic's source-available phrasing
)


def _looks_like_oss_license(license_str: str) -> bool:
    """Best-effort SPDX-id detection. False = caller must confirm.

    Anchored regex over known-OSS markers, with an explicit denylist
    of additive non-OSS riders (Commons Clause, BUSL, SSPL, Elastic
    License) that piggyback on permissive language. "Apache 2.0 with
    Commons Clause" must NOT pass — Commons Clause adds non-OSS
    restrictions even though the base license is OSS.
    """
    if not license_str:
        return False
    upper = license_str.upper()
    # Hard rejects first — if any non-OSS marker is present, refuse
    # even if there's an OSS one too.
    for marker in _NON_OSS_OVERRIDES:
        if marker in upper:
            return False
    return bool(_OSS_LICENSE_RE.search(upper))


def _is_safe_asset_path(rel: str) -> bool:
    """Allow common script + reference extensions. Binary blobs,
    images, fonts etc. are skipped to keep imports tight."""
    rel = rel.lower()
    # Path-escape defence — should never happen since we use the
    # GitHub tree API, but cheap to double-check.
    if ".." in rel.split("/"):
        return False
    return rel.endswith((
        ".md", ".txt", ".py", ".sh", ".js", ".ts", ".jsx", ".tsx",
        ".json", ".yaml", ".yml", ".toml", ".html", ".css", ".csv",
        ".sql",
    ))


def _record_import(name: str, source_url: str, source_kind: str,
                    hash_: str, license_field: str | None,
                    description: str, meta: dict) -> None:
    db.execute(
        "INSERT OR REPLACE INTO skill_imports "
        "(name, source_url, source_kind, hash, license, description, "
        "imported_at, meta) VALUES (?,?,?,?,?,?,?,?)",
        (name, source_url, source_kind, hash_, license_field,
         description[:1024], time.time(), json.dumps(meta) if meta else None)
    )


def get_import_record(name: str) -> dict | None:
    row = db.fetchone(
        "SELECT name, source_url, source_kind, hash, license, description, "
        "imported_at, meta FROM skill_imports WHERE name=?",
        (name,)
    )
    if not row:
        return None
    meta = json.loads(row[7]) if row[7] else None
    return {
        "name": row[0], "source_url": row[1], "source_kind": row[2],
        "hash": row[3], "license": row[4], "description": row[5],
        "imported_at": row[6], "meta": meta,
    }


def list_imports() -> list[dict]:
    rows = db.fetchall(
        "SELECT name, source_url, source_kind, license, description, "
        "imported_at FROM skill_imports ORDER BY imported_at DESC"
    )
    return [
        {"name": r[0], "source_url": r[1], "source_kind": r[2],
         "license": r[3], "description": r[4], "imported_at": r[5]}
        for r in rows
    ]


# Sentinel string embedded at the top of every generated adapter
# `.py`. Used by `delete_import` to verify it's removing a file WE
# wrote, not a same-named user skill the user wrote by hand. Keep in
# sync with the actual phrase emitted by `_render_adapter_py`.
_IMPORTER_SENTINEL = "Auto-generated by skills/skill_import.py"


def delete_import(name: str) -> bool:
    """Remove the adapter .py + staged assets + DB record.

    Sentinel-checks the `.py` before unlinking: if the file doesn't
    contain the importer's auto-generated sentinel string, we assume
    the user created/replaced it manually and leave it alone (but
    still delete the staged assets + DB row, since those ARE ours).

    Returns True if anything was actually deleted. The caller treats
    this as "did we have an entry to remove" — useful for UI feedback.
    """
    py = _user_skills_dir() / f"{name}.py"
    asset_dir = _imported_assets_dir() / name
    deleted_any = False

    # Only unlink the .py if it's still our generated adapter. A user
    # might have edited the file or replaced it with a different
    # skill of the same name — preserve their work.
    if py.exists():
        is_ours = False
        try:
            head = py.read_text(encoding="utf-8", errors="replace")[:2000]
            is_ours = _IMPORTER_SENTINEL in head
        except Exception:
            is_ours = False
        if is_ours:
            try:
                py.unlink()
                deleted_any = True
            except Exception:
                pass
        # else: leave the file — the user owns it now. The DB row
        # gets removed below so list_imports won't show it.

    if asset_dir.exists() and asset_dir.is_dir():
        # Recursive removal — guard against path-escape (we built the
        # dir from _imported_assets_dir() / name so it's already
        # within DATA_DIR, but the rmtree is gated all the same).
        import shutil
        try:
            shutil.rmtree(asset_dir)
            deleted_any = True
        except Exception:
            pass
    db.execute("DELETE FROM skill_imports WHERE name=?", (name,))
    return deleted_any


# ── Adapter .py generator ──────────────────────────────────────────


def _render_adapter_py(name: str, description: str, skill_md_body: str,
                       asset_dir_abs: str, source_url: str,
                       license_field: str | None) -> str:
    """Generate the thin Python adapter that qwe-qwe's skill loader
    will pick up. The body of SKILL.md becomes the return value of
    a `<name>_help` tool — the agent calls this to fetch the full
    instructions on demand. INSTRUCTION is a short pointer to that
    tool (full body would blow the token budget on every turn).

    All embedded strings flow through `repr()` so backslashes /
    quotes / unicode escapes are correctly source-encoded. Critical
    on Windows where `asset_dir_abs` like `C:\\Users\\...\\skills_
    imported\\pdf-helper` would otherwise produce truncated \\U
    unicode-escape sequences in the generated .py.
    """
    # Tool name must be a valid Python identifier — hyphens are not
    # legal so we substitute underscores. Skill name itself stays as
    # the file stem (skill_loader matches by stem, hyphens are fine).
    help_tool = f"{name.replace('-', '_')}_help"

    # Single-line description for docstrings + DESCRIPTION attr
    short_desc = description.split("\n", 1)[0].strip()[:200] or name

    # The INSTRUCTION is a short pointer. We deliberately keep it
    # under ~400 tokens so it doesn't eat the system prompt budget
    # every turn the skill is active.
    instruction_text = (
        f"Skill `{name}` (imported from {source_url}).\n\n"
        f"{short_desc}\n\n"
        f"Call the `{help_tool}` tool to read the full SKILL.md "
        f"body — it contains the procedure / patterns you should "
        f"follow for this domain.\n\n"
        f"Additional assets (scripts, references) are staged at:\n"
        f"  {asset_dir_abs}\n"
        f"Read them with the read_file tool when the SKILL.md body "
        f"says to consult them."
    )

    # Header docstring — uses repr-substituted asset path so Windows
    # backslashes don't trip Python's escape parser.
    docstring_lines = [
        f"Imported skill — {short_desc}",
        "",
        f"Source: {source_url}",
        f"License: {license_field or '(unspecified — see staged SKILL.md)'}",
        "",
        "Auto-generated by skills/skill_import.py. Edit this file freely if",
        "you want to customise the wrapper; re-importing from the same URL",
        "will overwrite it.",
        "",
        f"The full SKILL.md body is exposed via the `{help_tool}` tool. The",
        "upstream skill's scripts / references / assets are staged at the",
        "path printed by:",
        f"  python -c \"from skills import {name.replace('-', '_')} as s; print(s.ASSETS_DIR)\"",
        "Use the read_file tool to access them when the SKILL.md body says to.",
    ]
    # Escape any " in the docstring so the """ wrapper survives
    docstring = "\n".join(docstring_lines).replace('"""', '\\"\\"\\"')

    py = (
        f'"""{docstring}\n"""\n'
        f"\n"
        f"DESCRIPTION = {repr(short_desc)}\n"
        f"\n"
        f"INSTRUCTION = {repr(instruction_text)}\n"
        f"\n"
        f"# Absolute path to the directory where this skill's upstream\n"
        f"# scripts/references/assets are staged. The agent reads them\n"
        f"# via the regular read_file tool — qwe-qwe skills don't have\n"
        f"# a sub-runtime for executing external scripts directly.\n"
        f"ASSETS_DIR = {repr(asset_dir_abs)}\n"
        f"\n"
        f"# The full SKILL.md body (markdown). Kept as a module constant\n"
        f"# so the {help_tool} tool can return it verbatim — no DB lookup,\n"
        f"# no IO. repr() above handles all the escaping concerns: \n"
        f"# backslashes, quotes, unicode escapes all source-encoded\n"
        f"# correctly even on Windows paths.\n"
        f"_SKILL_MD_BODY = {repr(skill_md_body)}\n"
        f"\n"
        f"\n"
        f"TOOLS = [\n"
        f"    {{\n"
        f"        \"type\": \"function\",\n"
        f"        \"function\": {{\n"
        f"            \"name\": {repr(help_tool)},\n"
        f"            \"description\": (\n"
        f"                \"Return the full SKILL.md body for the `{name}` \"\n"
        f"                \"imported skill — read this first when the user asks \"\n"
        f"                \"you to use {name}.\"\n"
        f"            ),\n"
        f"            \"parameters\": {{\"type\": \"object\", \"properties\": {{}}}},\n"
        f"        }},\n"
        f"    }},\n"
        f"]\n"
        f"\n"
        f"\n"
        f"def execute(name: str, args: dict) -> str:\n"
        f"    if name == {repr(help_tool)}:\n"
        f"        return _SKILL_MD_BODY\n"
        f"    return f\"Unknown tool: {{name}}\"\n"
    )
    return py
