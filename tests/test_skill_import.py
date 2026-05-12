"""Tests for `skills/skill_import.py` — the skills.sh / GitHub importer.

External HTTP is mocked at the `_fetch_url` boundary so tests don't
touch the live internet. Mock target lives on the module so
monkeypatch.setattr works cleanly.

Coverage:
  - SKILL.md frontmatter parsing (happy + edge cases)
  - URL resolution: skills.sh / github tree / github raw
  - SSRF / allowlist / scheme validation
  - Name validation (matches the agentskills.io spec regex)
  - Collision protection (built-in + user-skill cases)
  - License classification (OSS vs source-available)
  - End-to-end install with mocked HTTP — writes .py + assets,
    records in skill_imports table, agent's skill loader picks
    up the new file
  - REST endpoints via TestClient (round-trip)
  - JS contract pins for the UI modal
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import urllib.request
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def index_html_src():
    """Session-cached read of static/index.html — JS-contract tests
    re-grep it without paying repeated disk I/O."""
    p = Path(__file__).resolve().parent.parent / "static" / "index.html"
    return p.read_text(encoding="utf-8")


@pytest.fixture
def si(qwe_temp_data_dir):
    for mod in ("config", "db", "skills"):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
        else:
            importlib.import_module(mod)
    if "skills.skill_import" in sys.modules:
        importlib.reload(sys.modules["skills.skill_import"])
    from skills import skill_import
    return skill_import


# ── SKILL.md parsing ──────────────────────────────────────────────


def test_parse_skill_md_minimal(si):
    body = (
        "---\n"
        "name: pdf-tools\n"
        "description: Tools for PDFs.\n"
        "---\n"
        "\n"
        "# PDF tools\n"
    )
    p = si.parse_skill_md(body)
    assert p["frontmatter"]["name"] == "pdf-tools"
    assert p["frontmatter"]["description"] == "Tools for PDFs."
    assert "# PDF tools" in p["body"]


def test_parse_skill_md_with_metadata_block(si):
    body = (
        "---\n"
        "name: x\n"
        "description: Y\n"
        "license: Apache-2.0\n"
        "metadata:\n"
        "  version: \"1.0\"\n"
        "  author: someone\n"
        "---\n"
        "body\n"
    )
    p = si.parse_skill_md(body)
    assert p["frontmatter"]["license"] == "Apache-2.0"
    assert p["frontmatter"]["metadata"] == {"version": "1.0", "author": "someone"}


def test_parse_skill_md_rejects_no_frontmatter(si):
    with pytest.raises(si.SkillImportError) as exc:
        si.parse_skill_md("# No frontmatter here\n")
    assert exc.value.code == "no_frontmatter"


def test_parse_skill_md_rejects_missing_name(si):
    body = "---\ndescription: just desc\n---\nbody\n"
    with pytest.raises(si.SkillImportError) as exc:
        si.parse_skill_md(body)
    assert exc.value.code == "missing_name"


def test_parse_skill_md_rejects_missing_description(si):
    body = "---\nname: x\n---\nbody\n"
    with pytest.raises(si.SkillImportError) as exc:
        si.parse_skill_md(body)
    assert exc.value.code == "missing_description"


def test_parse_skill_md_handles_quoted_strings(si):
    body = (
        "---\n"
        'name: "with-quotes"\n'
        "description: 'single-quoted desc'\n"
        "---\n"
    )
    p = si.parse_skill_md(body)
    assert p["frontmatter"]["name"] == "with-quotes"
    assert p["frontmatter"]["description"] == "single-quoted desc"


# ── URL resolution ─────────────────────────────────────────────────


def test_resolve_skills_sh_url(si):
    info = si.resolve_skill_source("https://skills.sh/anthropics/skills/pdf")
    assert info["owner"] == "anthropics"
    assert info["repo"] == "skills"
    assert info["path"] == "skills/pdf"
    assert info["fallback_path"] == "pdf"  # both layouts probed
    assert info["kind"] == "skills_sh"


def test_resolve_github_tree_url(si):
    url = "https://github.com/anthropics/skills/tree/main/skills/document/pdf"
    info = si.resolve_skill_source(url)
    assert info["owner"] == "anthropics"
    assert info["path"] == "skills/document/pdf"
    assert info["ref"] == "main"
    assert info["fallback_path"] is None


def test_resolve_github_raw_strips_skill_md_suffix(si):
    """If the user pasted the raw SKILL.md URL itself, the resolver
    should strip the trailing /SKILL.md so we get the directory."""
    url = "https://raw.githubusercontent.com/x/y/main/skills/foo/SKILL.md"
    info = si.resolve_skill_source(url)
    assert info["path"] == "skills/foo"


def test_resolve_skills_sh_url_requires_skill_name(si):
    with pytest.raises(si.SkillImportError) as exc:
        si.resolve_skill_source("https://skills.sh/anthropics/skills")
    assert exc.value.code == "bad_url"


def test_resolve_rejects_unknown_url(si):
    with pytest.raises(si.SkillImportError) as exc:
        si.resolve_skill_source("https://random.example.com/skill")
    assert exc.value.code == "bad_url"


# ── SSRF / safety ─────────────────────────────────────────────────


def test_check_url_safety_rejects_non_http(si):
    for u in ("ftp://skills.sh/x", "file:///etc/passwd", "javascript:alert(1)"):
        with pytest.raises(si.SkillImportError) as exc:
            si._check_url_safety(u)
        assert exc.value.code == "bad_scheme"


def test_check_url_safety_rejects_unknown_host(si):
    with pytest.raises(si.SkillImportError) as exc:
        si._check_url_safety("https://attacker.example.com/x")
    assert exc.value.code == "host_not_allowed"


def test_check_url_safety_rejects_private_ip(si, monkeypatch):
    # Make github.com resolve to a private IP via mocked getaddrinfo
    import socket
    monkeypatch.setattr(socket, "getaddrinfo",
                         lambda host, port: [(0, 0, 0, "", ("127.0.0.1", 0))])
    with pytest.raises(si.SkillImportError) as exc:
        si._check_url_safety("https://github.com/x/y")
    assert exc.value.code == "private_ip"


def test_check_url_safety_private_ip_override_via_env(si, monkeypatch):
    """QWE_ALLOW_PRIVATE_URLS=1 should let private IPs through —
    same opt-out as /api/knowledge/url."""
    monkeypatch.setenv("QWE_ALLOW_PRIVATE_URLS", "1")
    import socket
    monkeypatch.setattr(socket, "getaddrinfo",
                         lambda host, port: [(0, 0, 0, "", ("127.0.0.1", 0))])
    # No exception
    si._check_url_safety("https://github.com/x/y")


# ── Name validation ───────────────────────────────────────────────


@pytest.mark.parametrize("name", ["pdf", "pdf-tools", "a", "a-b-c", "x1", "abc-123"])
def test_check_name_accepts_valid(si, name):
    si._check_name(name)


@pytest.mark.parametrize("name", [
    "", "PDF", "Has Space", "with_underscore", "-leading", "trailing-",
    "double--dash", "x" * 65, None,
])
def test_check_name_rejects_invalid(si, name):
    with pytest.raises(si.SkillImportError):
        si._check_name(name)


# ── Collision protection ──────────────────────────────────────────


def test_check_collision_rejects_builtin(si):
    """Built-in skills (browser, canvas, skill_creator, etc.) MUST
    NOT be overridable via import — that's a footgun where someone
    typosquats a built-in name on GitHub and ships malicious code."""
    for builtin in ("browser", "canvas", "skill_creator", "serial_port", "soul_editor"):
        with pytest.raises(si.SkillImportError) as exc:
            si._check_collision(builtin, overwrite=False)
        assert exc.value.code == "builtin_collision"


def test_check_collision_user_skill_requires_overwrite(si):
    user_dir = si._user_skills_dir()
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "myskill.py").write_text("# stub\nDESCRIPTION=''\nTOOLS=[]\ndef execute(n,a): return ''\n")
    with pytest.raises(si.SkillImportError) as exc:
        si._check_collision("myskill", overwrite=False)
    assert exc.value.code == "user_collision"
    # overwrite=True passes
    si._check_collision("myskill", overwrite=True)


def test_check_collision_passes_for_fresh_name(si):
    si._check_collision("brand-new-skill", overwrite=False)


# ── License classification ────────────────────────────────────────


@pytest.mark.parametrize("lic", [
    "APACHE-2.0", "MIT", "BSD-3-CLAUSE", "GPL-3.0-OR-LATER",
    "LGPL-2.1", "ISC", "CC0-1.0", "MPL-2.0", "UNLICENSE",
])
def test_license_oss_classification_passes(si, lic):
    assert si._looks_like_oss_license(lic)


@pytest.mark.parametrize("lic", [
    "COMPLETE TERMS IN LICENSE.TXT",
    "PROPRIETARY", "ALL RIGHTS RESERVED",
    "",  # unspecified
])
def test_license_non_oss_classification_blocks(si, lic):
    assert not si._looks_like_oss_license(lic)


# ── Safe asset path filter ────────────────────────────────────────


@pytest.mark.parametrize("path", [
    "scripts/foo.py", "scripts/foo.sh", "references/api.md",
    "assets/template.html", "lib/util.js", "config.yaml",
])
def test_safe_asset_path_accepts_known_extensions(si, path):
    assert si._is_safe_asset_path(path)


@pytest.mark.parametrize("path", [
    "icon.png", "demo.gif", "image.jpg", "binary.bin", "lib.so",
    "a.exe", "../escape/foo.py",  # path-escape guard
])
def test_safe_asset_path_rejects_binaries_and_escape(si, path):
    assert not si._is_safe_asset_path(path)


# ── End-to-end install with mocked HTTP ───────────────────────────


_REAL_SKILL_MD = b"""---
name: pdf-helper
description: Read and summarise PDF files.
license: Apache-2.0
metadata:
  version: "1.2"
  author: testorg
---

# PDF helper

When the user asks about a PDF, use `scripts/extract.py` to pull text.
"""

_REAL_SCRIPT = b"# stub extract.py\nprint('hello')\n"


def _make_tree_payload(skill_path: str) -> bytes:
    """Mock GitHub trees API response listing one script file
    under the skill directory."""
    payload = {
        "tree": [
            {"path": f"{skill_path}/SKILL.md", "type": "blob", "size": len(_REAL_SKILL_MD)},
            {"path": f"{skill_path}/scripts/extract.py", "type": "blob", "size": len(_REAL_SCRIPT)},
            {"path": f"{skill_path}/scripts/icon.png", "type": "blob", "size": 100},  # excluded
        ],
    }
    return json.dumps(payload).encode("utf-8")


@pytest.fixture
def mock_http(si, monkeypatch):
    """Replace _fetch_url with a dispatcher that returns canned
    responses based on the URL."""
    def _fake(url, max_bytes):
        if url.endswith("/SKILL.md"):
            return _REAL_SKILL_MD
        if "/git/trees/" in url:
            # Figure out the skill path from previous fetch's URL —
            # for simplicity hardcode the most-recent test layout.
            # Caller should construct the tree payload manually if
            # they want richer cases.
            return _make_tree_payload("skills/pdf-helper")
        if url.endswith("/scripts/extract.py"):
            return _REAL_SCRIPT
        raise AssertionError(f"unexpected URL fetched in test: {url}")
    monkeypatch.setattr(si, "_fetch_url", _fake)
    return _fake


def test_import_skill_end_to_end(si, mock_http):
    """The full path: resolve URL → fetch SKILL.md → parse → fetch
    asset listing → fetch each safe asset → write adapter .py +
    staged assets → record in skill_imports table."""
    # Use a skills.sh URL — the resolver maps it to the `skills/pdf-helper`
    # path which our mock_http happens to serve.
    res = si.import_skill("https://skills.sh/anthropics/skills/pdf-helper")

    assert res["name"] == "pdf-helper"
    assert "Apache-2.0" in (res["license"] or "")
    assert res["tools_count"] == 1
    assert res["py_path"].endswith("pdf-helper.py")
    assert "SKILL.md" in res["files_imported"]
    assert "scripts/extract.py" in res["files_imported"]
    # png excluded by _is_safe_asset_path
    assert "scripts/icon.png" not in res["files_imported"]
    # Files actually written
    py = Path(res["py_path"])
    assert py.exists()
    py_text = py.read_text(encoding="utf-8")
    assert "DESCRIPTION" in py_text
    assert "TOOLS" in py_text
    assert "def execute" in py_text
    # The adapter's tool name is <name>_help (hyphens → underscores
    # for python identifier safety).
    assert "pdf_helper_help" in py_text
    # SKILL.md body is embedded
    assert "When the user asks about a PDF" in py_text
    # Staged assets present
    assets_dir = Path(res["assets_dir"])
    assert (assets_dir / "SKILL.md").exists()
    assert (assets_dir / "scripts" / "extract.py").exists()
    # DB record present
    rec = si.get_import_record("pdf-helper")
    assert rec is not None
    assert rec["source_kind"] == "skills_sh"
    assert rec["hash"]


def test_import_skill_blocks_on_license_without_confirm(si, monkeypatch):
    """Non-OSS license → 451 with details, no files written."""
    def _fake(url, max_bytes):
        if url.endswith("/SKILL.md"):
            return (
                b"---\nname: docx\ndescription: Word docs.\n"
                b"license: Complete terms in LICENSE.txt\n---\nbody\n"
            )
        return b'{"tree": []}'
    monkeypatch.setattr(si, "_fetch_url", _fake)

    with pytest.raises(si.SkillImportError) as exc:
        si.import_skill("https://skills.sh/anthropics/skills/docx")
    assert exc.value.code == "license_confirm_required"
    assert exc.value.status == 451
    assert exc.value.details["license"] == "Complete terms in LICENSE.txt"
    # No .py file written before the confirm step
    assert not (si._user_skills_dir() / "docx.py").exists()


def test_import_skill_succeeds_with_license_accepted(si, monkeypatch):
    def _fake(url, max_bytes):
        if url.endswith("/SKILL.md"):
            return b"---\nname: docx\ndescription: Word docs.\nlicense: Custom\n---\nbody\n"
        return b'{"tree": []}'
    monkeypatch.setattr(si, "_fetch_url", _fake)

    res = si.import_skill(
        "https://skills.sh/anthropics/skills/docx",
        accept_license=True,
    )
    assert res["name"] == "docx"
    assert res["license"] == "Custom"


def test_import_skill_user_collision_requires_overwrite(si, mock_http):
    si.import_skill("https://skills.sh/anthropics/skills/pdf-helper")
    # Second import without overwrite — should fail
    with pytest.raises(si.SkillImportError) as exc:
        si.import_skill("https://skills.sh/anthropics/skills/pdf-helper")
    assert exc.value.code == "user_collision"
    # With overwrite=True — succeeds
    res = si.import_skill(
        "https://skills.sh/anthropics/skills/pdf-helper",
        overwrite=True,
    )
    assert res["name"] == "pdf-helper"


def test_import_skill_blocks_builtin_overwrite(si, monkeypatch):
    """Even with overwrite=True, a built-in skill name like `canvas`
    must NOT be importable — that's the typosquatting defense."""
    def _fake(url, max_bytes):
        if url.endswith("/SKILL.md"):
            return b"---\nname: canvas\ndescription: typosquat.\nlicense: MIT\n---\nbody\n"
        return b'{"tree": []}'
    monkeypatch.setattr(si, "_fetch_url", _fake)
    with pytest.raises(si.SkillImportError) as exc:
        si.import_skill(
            "https://skills.sh/attacker/skills/canvas",
            overwrite=True,
        )
    assert exc.value.code == "builtin_collision"


def test_imports_list_and_delete(si, mock_http):
    si.import_skill("https://skills.sh/anthropics/skills/pdf-helper")
    listed = si.list_imports()
    assert len(listed) == 1
    assert listed[0]["name"] == "pdf-helper"
    assert listed[0]["source_url"]

    deleted = si.delete_import("pdf-helper")
    assert deleted is True
    # File + record both gone
    assert not (si._user_skills_dir() / "pdf-helper.py").exists()
    assert si.get_import_record("pdf-helper") is None
    assert si.list_imports() == []


# ── Generated adapter .py is loadable by qwe-qwe's skill validator ──


def test_imported_adapter_passes_skill_validator(si, mock_http):
    """The adapter .py we generate must satisfy skills.validate_skill
    — otherwise the imported skill won't activate. This is the
    end-to-end contract."""
    res = si.import_skill("https://skills.sh/anthropics/skills/pdf-helper")
    import skills
    ok, errs = skills.validate_skill(res["py_path"])
    assert ok is True, f"adapter failed validation: {errs}"


# ── REST endpoint round-trip ──────────────────────────────────────


@pytest.fixture
def http_client(si):
    """TestClient against a fresh server that shares the SAME
    `skill_import` module object the `si` fixture vended — otherwise
    monkeypatched _fetch_url on `si` wouldn't be visible to the
    endpoint's `from skills import skill_import` lookup."""
    import importlib as _imp
    import sys as _sys
    # Reload server AFTER skill_import is settled — server's endpoint
    # does `from skills import skill_import` inside the handler, so
    # the lookup happens lazily against the live sys.modules entry.
    # We just need to make sure no other code stomps it.
    for mod in ("server",):
        if mod in _sys.modules:
            _imp.reload(_sys.modules[mod])
        else:
            _imp.import_module(mod)
    import server
    # Sanity: confirm si and server's view of skill_import is the
    # same module object. If this asserts, the test setup is wrong
    # and patching would be invisible to the endpoint.
    assert _sys.modules["skills.skill_import"] is si, (
        "skill_import module identity drifted between fixtures — "
        "monkeypatches on `si` won't reach the server endpoint."
    )
    from fastapi.testclient import TestClient
    with TestClient(server.app) as c:
        yield c


def test_endpoint_import_skill(http_client, si, mock_http):
    r = http_client.post("/api/skills/import",
                          json={"url": "https://skills.sh/anthropics/skills/pdf-helper"})
    assert r.status_code == 200, f"status={r.status_code} body={r.text}"
    j = r.json()
    assert j.get("name") == "pdf-helper", f"unexpected payload: {j}"


def test_endpoint_import_returns_451_for_license(http_client, si, monkeypatch):
    def _fake(url, max_bytes):
        if url.endswith("/SKILL.md"):
            return b"---\nname: pdf\ndescription: x.\nlicense: Proprietary\n---\nbody\n"
        return b'{"tree": []}'
    monkeypatch.setattr(si, "_fetch_url", _fake)
    r = http_client.post("/api/skills/import",
                          json={"url": "https://skills.sh/anthropics/skills/pdf"})
    assert r.status_code == 451
    assert r.json()["code"] == "license_confirm_required"


def test_endpoint_list_imports(http_client, si, mock_http):
    http_client.post("/api/skills/import",
                      json={"url": "https://skills.sh/anthropics/skills/pdf-helper"})
    r = http_client.get("/api/skills/imports")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "pdf-helper"


def test_endpoint_delete_import(http_client, si, mock_http):
    http_client.post("/api/skills/import",
                      json={"url": "https://skills.sh/anthropics/skills/pdf-helper"})
    r = http_client.delete("/api/skills/imports/pdf-helper")
    assert r.status_code == 200
    listed = http_client.get("/api/skills/imports").json()["items"]
    assert listed == []


# ── JS contract pins (static/index.html) ──────────────────────────


def test_import_button_renders_in_tools_tab(index_html_src):
    tab_at = index_html_src.find("function renderTabTools()")
    assert tab_at >= 0
    window = index_html_src[tab_at: tab_at + 8000]
    assert 'data-act="open-skill-import"' in window, (
        "Tools tab missing the Import skill button"
    )
    assert "Import skill" in window


def test_import_modal_function_exists(index_html_src):
    assert "function openSkillImportModal" in index_html_src
    assert "/api/skills/import" in index_html_src


def test_import_modal_handles_451_license_reprompt(index_html_src):
    """The 451 response from POST /api/skills/import means the model
    declared a non-OSS license and needs user confirmation. The
    modal must re-open with a license-confirm panel rather than
    failing silently."""
    fn_at = index_html_src.find("function openSkillImportModal")
    assert fn_at >= 0
    body = index_html_src[fn_at: fn_at + 6000]
    assert "license_confirm_required" in body
    assert "openSkillImportModal({" in body
    assert "accept_license" in body


# ═══════════════════════════════════════════════════════════════════
# Extended coverage — round 2
# ═══════════════════════════════════════════════════════════════════


# ── Real-world SKILL.md samples (Anthropic frontmatter quirks) ────


_ANTHROPIC_PDF_SKILL_MD = b"""---
name: pdf
description: Comprehensive PDF manipulation toolkit for reading text, extracting tables, creating new documents, merging files, and processing scanned PDFs with OCR.
license: Complete terms in LICENSE.txt
metadata:
  version: 1.0.0
---

# PDF Skill

Use this skill when you need to read, write, or manipulate PDF files.

## When to Use

- Extracting text or tables from PDFs
- Creating new PDF documents
- Merging or splitting PDFs
"""

# Frontmatter with no trailing newline, leading whitespace in values
_ANTHROPIC_DOCX_SKILL_MD = b"""---
name:   docx
description:    Create and edit Word documents with formatting, tables, and images.
license:   Complete terms in LICENSE.txt
metadata:
    version: 1.0
---
# Body
"""

# Empty `metadata:` block
_MINIMAL_SKILL_MD = b"""---
name: minimal
description: A skill with no metadata.
---

# Minimal
"""


def test_parse_real_anthropic_pdf_frontmatter(si):
    p = si.parse_skill_md(_ANTHROPIC_PDF_SKILL_MD.decode("utf-8"))
    fm = p["frontmatter"]
    assert fm["name"] == "pdf"
    assert "PDF manipulation" in fm["description"]
    # Anthropic's source-available license format — must be preserved
    # verbatim so we can show it in the confirm panel.
    assert fm["license"] == "Complete terms in LICENSE.txt"
    assert isinstance(fm.get("metadata"), dict)
    assert fm["metadata"].get("version") == "1.0.0"


def test_parse_handles_leading_whitespace_in_values(si):
    p = si.parse_skill_md(_ANTHROPIC_DOCX_SKILL_MD.decode("utf-8"))
    assert p["frontmatter"]["name"] == "docx"
    assert p["frontmatter"]["description"].startswith("Create and edit")


def test_parse_handles_empty_metadata(si):
    p = si.parse_skill_md(_MINIMAL_SKILL_MD.decode("utf-8"))
    assert p["frontmatter"]["name"] == "minimal"
    # metadata key missing entirely is fine
    assert "version" not in p["frontmatter"]


def test_parse_classifies_anthropic_license_as_non_oss(si):
    """The Anthropic source-available license string MUST land in
    the non-OSS bucket so the importer raises license_confirm_required
    instead of silently installing. This is the main user-protection
    surface for the whole feature."""
    p = si.parse_skill_md(_ANTHROPIC_PDF_SKILL_MD.decode("utf-8"))
    lic = p["frontmatter"]["license"]
    assert not si._looks_like_oss_license(lic.upper())


# ── HTTP error path coverage ──────────────────────────────────────


def test_fetch_404_returns_not_found(si, monkeypatch):
    """When the upstream returns 404 for the SKILL.md, the importer
    should fall through to the fallback path (skills/<x> vs <x>)
    and only then surface a clear not_found error."""
    import urllib.error
    calls = []
    def _raise_404(url, max_bytes):
        calls.append(url)
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    monkeypatch.setattr(si, "_fetch_url", _raise_404)
    with pytest.raises(si.SkillImportError) as exc:
        si.import_skill("https://skills.sh/anthropics/skills/nonexistent")
    assert exc.value.code == "not_found"
    assert exc.value.status == 404
    # The skills.sh URL has fallback_path enabled → at least 2 attempts
    # (skills/<x>/SKILL.md and <x>/SKILL.md)
    assert len(calls) >= 2


def test_fetch_500_surfaces_fetch_failed(si, monkeypatch):
    import urllib.error
    def _raise_500(url, max_bytes):
        raise urllib.error.HTTPError(url, 500, "Internal", {}, None)
    monkeypatch.setattr(si, "_fetch_url", _raise_500)
    with pytest.raises(si.SkillImportError) as exc:
        si.import_skill("https://skills.sh/x/y/z")
    assert exc.value.code == "fetch_failed"
    assert exc.value.status == 502


def test_skill_md_size_cap_enforced(si, monkeypatch):
    """SKILL.md > 100 KB cap should be rejected by _fetch_url's size
    guard, surfacing as oversize."""
    def _oversize(url, max_bytes):
        raise si.SkillImportError(
            f"Response exceeds {max_bytes} byte cap (URL={url!r}).",
            code="oversize", status=413)
    monkeypatch.setattr(si, "_fetch_url", _oversize)
    with pytest.raises(si.SkillImportError) as exc:
        si.import_skill("https://skills.sh/x/y/z")
    assert exc.value.code == "oversize"


def test_url_open_blocks_oversize_responses(si, monkeypatch):
    """Size cap inside _fetch_url — opener returns more bytes than
    max_bytes, must raise oversize."""
    class _FakeResp:
        def __init__(self, body):
            self._body = body
        def read(self, n=-1):
            return self._body[:n] if n >= 0 else self._body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class _FakeOpener:
        def open(self, req, timeout=None):
            return _FakeResp(b"X" * 50_000)

    monkeypatch.setattr(si, "_check_url_safety", lambda url: None)
    monkeypatch.setattr(si, "_opener", _FakeOpener())

    with pytest.raises(si.SkillImportError) as exc:
        si._fetch_url("https://skills.sh/x", max_bytes=30_000)
    assert exc.value.code == "oversize"
    assert exc.value.status == 413


# ── Path-traversal defense for asset names ────────────────────────


@pytest.mark.parametrize("malicious_path", [
    "../escape.py",
    "scripts/../../etc/passwd",
    "scripts/foo/../../../home/user/.ssh/id_rsa.py",
    "..",
    "../",
])
def test_asset_path_rejects_traversal(si, malicious_path):
    """The _is_safe_asset_path guard MUST reject any path containing
    `..` components. The GitHub trees API wouldn't normally return
    these, but defense in depth — a compromised intermediate API
    (or a hand-crafted MITM if SSL bypassed) shouldn't be able to
    write outside the staging dir."""
    assert not si._is_safe_asset_path(malicious_path), (
        f"Path-traversal asset {malicious_path!r} passed safety check"
    )


def test_import_skips_unsafe_asset_paths(si, monkeypatch):
    """End-to-end: even if the tree API returns a malicious path,
    the importer must skip it AND continue installing the rest of
    the skill (one bad apple doesn't fail the whole import)."""
    _skill_md = b"""---
name: traversal-test
description: Skill with malicious tree entries.
license: MIT
---
body
"""
    _good_script = b"# good\n"

    def _fake(url, max_bytes):
        if url.endswith("/SKILL.md"):
            return _skill_md
        if "/git/trees/" in url:
            return json.dumps({
                "tree": [
                    {"path": "skills/traversal-test/SKILL.md", "type": "blob", "size": len(_skill_md)},
                    # Three traversal attempts
                    {"path": "skills/traversal-test/../../etc/passwd.py", "type": "blob", "size": 100},
                    {"path": "skills/traversal-test/../escape.py", "type": "blob", "size": 100},
                    {"path": "skills/traversal-test/scripts/../../../leak.py", "type": "blob", "size": 100},
                    # Legit
                    {"path": "skills/traversal-test/scripts/good.py", "type": "blob", "size": len(_good_script)},
                ],
            }).encode("utf-8")
        if url.endswith("/scripts/good.py"):
            return _good_script
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(si, "_fetch_url", _fake)
    res = si.import_skill("https://skills.sh/anthropics/skills/traversal-test")
    assert res["name"] == "traversal-test"
    # Only SKILL.md + good.py staged
    assert "SKILL.md" in res["files_imported"]
    assert "scripts/good.py" in res["files_imported"]
    # None of the traversal attempts landed
    assets = Path(res["assets_dir"])
    for cousin in ("../passwd.py", "../escape.py", "../../leak.py"):
        assert not (assets / cousin).exists(), (
            f"path-traversal write succeeded: {cousin}")
    # Nothing outside the staging dir was created
    parent = assets.parent.parent  # ~/.qwe-qwe
    assert not (parent / "passwd.py").exists()
    assert not (parent / "leak.py").exists()


# ── Idempotent re-import cycle ────────────────────────────────────


def test_full_import_lifecycle(si, mock_http):
    """import → exists → delete → gone → re-import → exists.
    Repeating the cycle 3 times to catch any stale-state leak in
    DB or filesystem."""
    url = "https://skills.sh/anthropics/skills/pdf-helper"
    for cycle in range(3):
        res = si.import_skill(url, overwrite=(cycle > 0))
        assert res["name"] == "pdf-helper"
        assert (si._user_skills_dir() / "pdf-helper.py").exists()
        rec = si.get_import_record("pdf-helper")
        assert rec is not None
        listed = si.list_imports()
        assert len(listed) == 1, f"cycle {cycle}: duplicate rows in DB"

        ok = si.delete_import("pdf-helper")
        assert ok is True
        assert not (si._user_skills_dir() / "pdf-helper.py").exists()
        assert si.get_import_record("pdf-helper") is None
        assert si.list_imports() == []


def test_overwrite_updates_hash_and_imported_at(si, monkeypatch):
    """Two imports with different SKILL.md content under the same
    slug — second one must overwrite the DB record AND files."""
    bodies = [b"""---
name: changing
description: First version.
license: MIT
---
# v1
""", b"""---
name: changing
description: Second version.
license: MIT
---
# v2 NEW BODY
"""]
    counter = {"i": 0}
    def _fake(url, max_bytes):
        if url.endswith("/SKILL.md"):
            return bodies[counter["i"]]
        return b'{"tree": []}'
    monkeypatch.setattr(si, "_fetch_url", _fake)

    counter["i"] = 0
    res1 = si.import_skill("https://skills.sh/x/y/changing")
    rec1 = si.get_import_record("changing")
    assert rec1["description"] == "First version."

    counter["i"] = 1
    res2 = si.import_skill("https://skills.sh/x/y/changing", overwrite=True)
    rec2 = si.get_import_record("changing")
    assert rec2["description"] == "Second version."
    assert rec1["hash"] != rec2["hash"], "Hash should change on body change"
    assert rec2["imported_at"] >= rec1["imported_at"]
    # File was rewritten with new SKILL.md body embedded
    py = Path(res2["py_path"]).read_text(encoding="utf-8")
    assert "v2 NEW BODY" in py
    assert "v1" not in py.replace("# v2 NEW BODY", "")  # the v1 string shouldn't linger


# ── Activate + execute imported skill end-to-end ──────────────────


def test_imported_skill_loads_via_skill_loader(si, mock_http):
    """After import, qwe-qwe's `skills.list_all()` must include the
    new skill AND `skills.enable()` must succeed AND
    `skills.get_tools()` must surface the help tool. End-to-end
    contract — without this, the import "works" but the agent
    never actually sees the new tool."""
    res = si.import_skill("https://skills.sh/anthropics/skills/pdf-helper")
    assert res["name"] == "pdf-helper"

    import skills
    importlib.reload(skills)  # pick up the new .py file
    all_skills = skills.list_all()
    names = [s["name"] for s in all_skills]
    assert "pdf-helper" in names, f"imported skill not in list_all: {names}"

    # The list_all entry reports the right metadata
    entry = next(s for s in all_skills if s["name"] == "pdf-helper")
    assert entry["tools"] == 1  # the single <name>_help tool
    assert "PDF" in entry["description"] or "pdf" in entry["description"].lower()


def test_imported_skill_help_tool_returns_skill_md(si, mock_http):
    """The <name>_help tool — the whole point of the adapter —
    must return the SKILL.md body verbatim when called."""
    res = si.import_skill("https://skills.sh/anthropics/skills/pdf-helper")

    # Load the adapter module directly + call execute()
    import importlib.util
    spec = importlib.util.spec_from_file_location("_pdf_helper_under_test", res["py_path"])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    out = mod.execute("pdf_helper_help", {})
    assert "When the user asks about a PDF" in out, (
        f"<name>_help tool didn't return the SKILL.md body. Got: {out!r}"
    )
    # Unknown tool name returns a friendly error, not a crash
    err = mod.execute("nonexistent", {})
    assert "Unknown tool" in err


# ── SQL injection defense ─────────────────────────────────────────


def test_no_sql_injection_via_name_field(si, monkeypatch):
    """Even if the parser somehow let through a name with SQL meta-
    characters (which the regex blocks — `[a-z0-9-]` doesn't include
    `'` `"` `;` `--`), the parametrised INSERT we use can't be
    exploited. This test is paranoid defense — proves the regex AND
    the parameterised query both work."""
    # Direct: bad name fails at _check_name
    for evil in ["x'; DROP TABLE skill_imports; --", 'x" OR "1"="1', "x;DROP"]:
        with pytest.raises(si.SkillImportError):
            si._check_name(evil)

    # If _check_name were somehow bypassed (defence in depth), the
    # parameterised INSERT still defangs it. We test by writing
    # directly to _record_import with a weird-but-non-fatal name.
    si._record_import(
        name="legit-name", source_url="https://skills.sh/x/y/legit-name",
        source_kind="skills_sh", hash_="abc",
        license_field="MIT", description="x';--",
        meta={"injection": "'; DROP TABLE skill_imports; --"},
    )
    # Table still exists + row still readable
    rec = si.get_import_record("legit-name")
    assert rec is not None
    assert rec["description"] == "x';--"  # stored as literal


# ── Long-running / oversize file budget ───────────────────────────


def test_total_fetch_budget_stops_staging(si, monkeypatch):
    """Total bytes cap (_TOTAL_FETCH_CAP) should stop the asset-staging
    loop early — a malicious / huge upstream shouldn't be able to
    fill our disk via a single import."""
    big_chunk = b"X" * (si._SKILL_MD_CAP * 3)  # one file = 3/4 of the total cap
    _md = b"""---
name: bloat
description: Big assets.
license: MIT
---
body
"""

    def _fake(url, max_bytes):
        if url.endswith("/SKILL.md"):
            return _md
        if "/git/trees/" in url:
            return json.dumps({
                "tree": [{"path": f"skills/bloat/scripts/big{i}.py",
                          "type": "blob", "size": len(big_chunk)}
                         for i in range(5)],
            }).encode("utf-8")
        return big_chunk
    monkeypatch.setattr(si, "_fetch_url", _fake)

    res = si.import_skill("https://skills.sh/x/y/bloat")
    # Some assets staged, but NOT all 5 — the budget cut in early
    staged = [f for f in res["files_imported"] if f.startswith("scripts/")]
    assert 0 < len(staged) < 5, (
        f"budget didn't cap fetch: staged {len(staged)} files"
    )


# ── Live integration (gated; default skip) ────────────────────────


_LIVE_TESTS_ENABLED = bool(os.environ.get("RUN_LIVE_TESTS"))


def test_yaml_parser_handles_block_scalar_pipe(si):
    """`description: |` is YAML literal block scalar — PyYAML joins
    the indented lines with newlines."""
    body = "---\nname: x\ndescription: |\n  line 1\n  line 2\n---\nbody\n"
    p = si.parse_skill_md(body)
    assert "line 1" in p["frontmatter"]["description"]
    assert "line 2" in p["frontmatter"]["description"]


def test_yaml_parser_handles_block_scalar_fold(si):
    """Folded form `>` joins with spaces."""
    body = "---\nname: x\ndescription: >\n  one paragraph\n  continued\n---\nbody\n"
    p = si.parse_skill_md(body)
    assert "one paragraph" in p["frontmatter"]["description"]


def test_yaml_parser_handles_list_items(si):
    """PyYAML returns lists natively as Python lists."""
    body = (
        "---\n"
        "name: x\n"
        "description: y\n"
        "allowed-tools:\n"
        "  - shell\n"
        "  - read_file\n"
        "---\nbody\n"
    )
    p = si.parse_skill_md(body)
    assert p["frontmatter"]["allowed-tools"] == ["shell", "read_file"]


def test_yaml_parser_strips_trailing_inline_comments(si):
    body = (
        "---\n"
        "name: pdf-helper  # the one from anthropics\n"
        "description: y\n"
        "---\nbody\n"
    )
    p = si.parse_skill_md(body)
    assert p["frontmatter"]["name"] == "pdf-helper"


def test_yaml_parser_preserves_hash_inside_quotes(si):
    body = (
        "---\n"
        'name: x\n'
        'description: "size: #1 priority"\n'
        "---\nbody\n"
    )
    p = si.parse_skill_md(body)
    assert "#1" in p["frontmatter"]["description"]


def test_license_detector_accepts_spdx_ids(si):
    for lic in ["MIT", "MIT-0", "APACHE-2.0", "Apache 2.0",
                "BSD-3-Clause", "GPL-3.0-or-later", "AGPL-3.0",
                "LGPL-2.1"]:
        assert si._looks_like_oss_license(lic), f"OSS license {lic!r} rejected"


def test_license_rejects_commons_clause_rider(si):
    """`Apache 2.0 with Commons Clause` contains the OSS marker but
    the rider makes it non-OSS — must override."""
    assert not si._looks_like_oss_license("Apache 2.0 with Commons Clause")
    assert not si._looks_like_oss_license("MIT + Commons Clause")
    assert not si._looks_like_oss_license("Commons Clause")


def test_license_rejects_busl_sspl_elastic(si):
    for lic in [
        "BSL-1.1",
        "Business Source License 1.1",
        "SSPL-1.0",
        "Elastic License 2.0",
        "Proprietary",
        "All Rights Reserved",
        "Complete terms in LICENSE.txt",
    ]:
        assert not si._looks_like_oss_license(lic), (
            f"non-OSS license {lic!r} false-positive passed"
        )


def test_license_empty_blocks(si):
    assert not si._looks_like_oss_license("")
    assert not si._looks_like_oss_license(None or "")


def test_delete_import_preserves_user_owned_py(si, mock_http):
    """If user replaces our adapter with hand-written content, the
    sentinel check protects their work — only assets + DB row go."""
    res = si.import_skill("https://skills.sh/anthropics/skills/pdf-helper")
    py_path = Path(res["py_path"])

    user_owned = "# user skill\nDESCRIPTION='mine'\nTOOLS=[]\ndef execute(n,a): return ''\n"
    py_path.write_text(user_owned, encoding="utf-8")

    deleted = si.delete_import("pdf-helper")
    assert deleted is True
    assert py_path.exists(), "delete_import clobbered a user-owned .py"
    assert py_path.read_text(encoding="utf-8") == user_owned
    assert si.get_import_record("pdf-helper") is None


def test_delete_import_removes_our_adapter(si, mock_http):
    """Fresh adapter has the sentinel → delete_import unlinks it."""
    res = si.import_skill("https://skills.sh/anthropics/skills/pdf-helper")
    py_path = Path(res["py_path"])
    assert si._IMPORTER_SENTINEL in py_path.read_text(encoding="utf-8")
    si.delete_import("pdf-helper")
    assert not py_path.exists()


def test_import_validates_adapter_before_committing(si, monkeypatch):
    """Broken `_render_adapter_py` output must fail before landing
    at the final path — no partial .py, no tempfile residue."""
    monkeypatch.setattr(si, "_render_adapter_py",
                         lambda **kw: "this is not valid python (((\n")

    def _fake(url, max_bytes):
        if url.endswith("/SKILL.md"):
            return b"---\nname: brokey\ndescription: y\nlicense: MIT\n---\nbody\n"
        return b'{"tree": []}'
    monkeypatch.setattr(si, "_fetch_url", _fake)

    with pytest.raises(si.SkillImportError) as exc:
        si.import_skill("https://skills.sh/x/y/brokey")
    assert exc.value.code == "adapter_invalid"
    assert exc.value.status == 500

    final_py = si._user_skills_dir() / "brokey.py"
    assert not final_py.exists(), "Broken adapter committed to disk"
    user_dir = si._user_skills_dir()
    if user_dir.exists():
        residual = list(user_dir.glob("__qwepartial__brokey*"))
        assert not residual, f"Tempfile leak: {residual}"


def test_import_atomic_replace_preserves_old_on_failure(si, monkeypatch, mock_http):
    """A failed overwrite must leave the existing valid .py intact."""
    si.import_skill("https://skills.sh/anthropics/skills/pdf-helper")
    final_py = si._user_skills_dir() / "pdf-helper.py"
    good_content = final_py.read_text(encoding="utf-8")
    assert si._IMPORTER_SENTINEL in good_content

    monkeypatch.setattr(si, "_render_adapter_py",
                         lambda **kw: "((( bad python\n")
    with pytest.raises(si.SkillImportError):
        si.import_skill(
            "https://skills.sh/anthropics/skills/pdf-helper",
            overwrite=True,
        )
    assert final_py.read_text(encoding="utf-8") == good_content


def test_redirect_handler_blocks_private_ip_redirect(si, monkeypatch):
    """SSRF: a redirect to 127.0.0.1 (or any private IP) re-fails
    `_check_url_safety` and gets re-raised as `redirect_blocked`."""
    def _stub_check(url):
        if "127.0.0.1" in url:
            raise si.SkillImportError(
                "URL resolves to a private address (127.0.0.1).",
                code="private_ip", status=403)

    monkeypatch.setattr(si, "_check_url_safety", _stub_check)

    handler = si._SafetyCheckingRedirectHandler()
    req = urllib.request.Request("https://github.com/x/y")
    with pytest.raises(si.SkillImportError) as exc:
        handler.redirect_request(
            req, None, 302, "Found", {}, "http://127.0.0.1/secret")
    assert exc.value.code == "redirect_blocked"
    assert exc.value.status == 403


def test_fetch_url_pins_accept_encoding_identity(si):
    """Without `Accept-Encoding: identity`, a gzip-compressed response
    could bypass the byte cap (cap counts compressed bytes, decompress
    yields many MB)."""
    src = Path(si.__file__).read_text(encoding="utf-8")
    assert '"Accept-Encoding": "identity"' in src or \
           "'Accept-Encoding': 'identity'" in src, (
        "_fetch_url no longer pins Accept-Encoding: identity"
    )


def test_modal_short_circuits_on_401(index_html_src):
    """api() throws `new Error('401 ' + path)` and opens the login
    modal itself. The import modal must match that error shape and
    bail without piling on a toast."""
    fn_at = index_html_src.find("function openSkillImportModal")
    assert fn_at >= 0
    body = index_html_src[fn_at: fn_at + 6000]
    assert "/^401\\b/" in body, (
        "Import modal doesn't detect 401 from api() — login flow "
        "won't surface for password-protected installs."
    )


@pytest.mark.skipif(not _LIVE_TESTS_ENABLED,
                     reason="live network test — opt in with RUN_LIVE_TESTS=1")
def test_live_anthropic_pdf_skill_imports(si, qwe_temp_data_dir):
    """End-to-end against the real anthropics/skills repo. Skipped
    by default — opt in via RUN_LIVE_TESTS=1 for manual verification.

    This test catches:
      - Real-world frontmatter quirks my parser missed
      - skills.sh API drift
      - Network / SSRF guard misconfiguration
      - GitHub trees API rate limits
      - 451 confirm flow against the actual Anthropic license
    """
    # Anthropic's pdf skill has a non-OSS license — first call MUST
    # return license_confirm_required.
    with pytest.raises(si.SkillImportError) as exc:
        si.import_skill("https://skills.sh/anthropics/skills/pdf")
    assert exc.value.code == "license_confirm_required", (
        f"Anthropic pdf no longer returns 451? Got {exc.value.code} / "
        f"{exc.value.details}"
    )
    license_text = exc.value.details.get("license", "")
    assert license_text, "license missing from 451 details"

    # With accept_license, install succeeds and the adapter validates
    res = si.import_skill(
        "https://skills.sh/anthropics/skills/pdf",
        accept_license=True,
    )
    assert res["name"] == "pdf"
    import skills
    import importlib as _imp
    _imp.reload(skills)
    ok, errs = skills.validate_skill(res["py_path"])
    assert ok is True, f"live-import adapter failed validation: {errs}"


