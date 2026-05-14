"""Import Anthropic-style SKILL.md skills from skills.sh / GitHub.

skills.sh and the Anthropic-skills repo (and forks) publish skills as
directories containing a SKILL.md (YAML frontmatter + Markdown body)
plus optional scripts/, references/, and assets/. castor skills are
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
   `~/.castor/skills/<name>.py` whose:
     - DESCRIPTION = frontmatter description
     - INSTRUCTION = a short intro pointing at the `<name>_help` tool
       (full SKILL.md body is too big to inject every turn — token
       budget protection)
     - TOOLS = one `<name>_help()` returning the full SKILL.md body
     - execute() dispatches it

4. **Staging** any additional scripts / references the upstream skill
   ships into `~/.castor/skills_imported/<name>/` so the agent's
   regular read_file / shell tools can use them.

5. **Recording** the import in the `skill_imports` SQLite table so
   we have provenance (source URL, hash, license, timestamp) for
   audits + future "check for upstream updates" flows.

## Safety

- SSRF: URL must use http/https, must NOT resolve to private /
  loopback / link-local IPs (override with CASTOR_ALLOW_PRIVATE_URLS=1)
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
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

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
_FETCH_USER_AGENT = "castor-skill-importer"


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
    if os.environ.get("CASTOR_ALLOW_PRIVATE_URLS", "").strip() == "1":
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
                "Set CASTOR_ALLOW_PRIVATE_URLS=1 to override.",
                code="private_ip",
                status=403)


class _SafetyCheckingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate each redirect target through `_check_url_safety`.

    Without this, an upstream 302 could redirect a public-host fetch
    to a private IP (169.254.169.254 metadata service, 127.0.0.1, ...)
    and bypass the initial SSRF guard.

    `redirect_request` is the single canonical hook — all five
    `http_error_30X` methods in the stdlib call it with `newurl`
    already absolutised.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            _check_url_safety(newurl)
        except SkillImportError as e:
            raise SkillImportError(
                f"Redirect to {newurl!r} blocked: {e}",
                code="redirect_blocked",
                status=e.status,
            ) from None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SafetyCheckingRedirectHandler())


def _fetch_url(url: str, max_bytes: int) -> bytes:
    """Fetch a URL with size cap. Safety-checks the start URL and
    each redirect hop (via `_SafetyCheckingRedirectHandler`)."""
    _check_url_safety(url)
    req = urllib.request.Request(url, headers={
        "User-Agent": _FETCH_USER_AGENT,
        # Pinned so gzip-compressed responses can't bypass the byte
        # cap (we'd cap bytes-on-wire and decompress to many MB).
        "Accept-Encoding": "identity",
    })
    with _opener.open(req, timeout=_HTTP_TIMEOUT) as r:
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
    """Parse SKILL.md frontmatter via PyYAML.

    Was a hand-rolled parser; replaced with `yaml.safe_load` so that
    block scalars, lists, inline comments, and nested mappings all
    work natively without per-edge-case handling.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise SkillImportError(
            f"SKILL.md frontmatter is not valid YAML: {e}",
            code="bad_frontmatter") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SkillImportError(
            "SKILL.md frontmatter must be a mapping (key: value).",
            code="bad_frontmatter")
    return data


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
    tool reaches into this directory, but castor's skill loader
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
            f"'{name}' collides with a built-in castor skill. "
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
    # Normalise: any run of non-alphanumeric chars → single hyphen, trim edges.
    # Allows importing skills whose SKILL.md uses spaces or underscores in name.
    name = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
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

    # Atomic write: tempfile in the same dir (so os.replace is atomic
    # on every OS) → validate via skills.validate_skill → os.replace.
    # Prefix `__qwepartial__` keeps the partial file out of the skill
    # loader's glob if anyone scans before we replace. Suffix `.py` so
    # the validator's importlib loader can pick it up.
    # validate_skill is a local import to dodge the skills/__init__.py
    # → skills/skill_import.py circular reference.
    from . import validate_skill as _validate_skill
    fd, tmp_str = tempfile.mkstemp(
        suffix=".py", prefix=f"__qwepartial__{name}__", dir=str(user_dir))
    tmp_path = Path(tmp_str)
    os.close(fd)
    try:
        tmp_path.write_text(py_body, encoding="utf-8")
        ok, errs = _validate_skill(str(tmp_path))
        if not ok:
            raise SkillImportError(
                "Generated adapter failed skills.validate_skill: "
                + "; ".join(errs)[:400] +
                ". This is a bug in skills/skill_import.py — open an "
                "issue with the upstream URL.",
                code="adapter_invalid", status=500)
        os.replace(str(tmp_path), str(py_path))
    finally:
        # Single cleanup site. Success path: os.replace moved it
        # away. Failure path: still here, unlink it.
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


# Word-bounded SPDX-ish regex — anchored so "MIT-style license" in
# prose doesn't false-positive (the old substring match did).
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
# Markers that override an OSS match — e.g. "Apache 2.0 with Commons
# Clause" matches the OSS regex but the rider makes it non-OSS.
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
    """Best-effort SPDX-id detection. False = caller must confirm."""
    if not license_str:
        return False
    upper = license_str.upper()
    if any(marker in upper for marker in _NON_OSS_OVERRIDES):
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


def delete_import(name: str) -> bool:
    """Remove the adapter .py + staged assets + DB record.

    Unlinks the .py only if it still contains `_IMPORTER_SENTINEL` —
    if the user replaced it with hand-written content, their file
    survives (DB row + staged assets are still removed).
    """
    # Validate name before building any path — prevents path traversal
    if not _NAME_RE.match(name) or len(name) > _MAX_NAME_LEN:
        return False
    py = _user_skills_dir() / f"{name}.py"
    asset_dir = _imported_assets_dir() / name
    # Belt-and-suspenders: confirm resolved paths stay within expected dirs
    try:
        py.resolve().relative_to(_user_skills_dir().resolve())
        asset_dir.resolve().relative_to(_imported_assets_dir().resolve())
    except ValueError:
        return False
    deleted_any = False

    try:
        with open(py, "r", encoding="utf-8", errors="replace") as f:
            is_ours = _IMPORTER_SENTINEL in f.read(2000)
    except FileNotFoundError:
        is_ours = False
    except OSError:
        is_ours = False
    if is_ours:
        try:
            py.unlink()
            deleted_any = True
        except OSError:
            pass

    if asset_dir.is_dir():
        import shutil
        try:
            shutil.rmtree(asset_dir)
            deleted_any = True
        except OSError:
            pass
    db.execute("DELETE FROM skill_imports WHERE name=?", (name,))
    return deleted_any


# ── Adapter .py generator ──────────────────────────────────────────


# Embedded in every generated adapter and re-read by `delete_import`
# to distinguish files we wrote from same-named hand-written skills.
_IMPORTER_SENTINEL = "Auto-generated by skills/skill_import.py"


def _render_adapter_py(name: str, description: str, skill_md_body: str,
                       asset_dir_abs: str, source_url: str,
                       license_field: str | None) -> str:
    """Generate the thin Python adapter that castor's skill loader
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
        f"{_IMPORTER_SENTINEL}. Edit this file freely if you want to",
        "customise the wrapper; re-importing from the same URL will",
        "overwrite it.",
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
        f"# via the regular read_file tool — castor skills don't have\n"
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
