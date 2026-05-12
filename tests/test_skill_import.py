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
import sys
from pathlib import Path

import pytest


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


def _read_index_html() -> str:
    return (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")


def test_import_button_renders_in_tools_tab():
    src = _read_index_html()
    tab_at = src.find("function renderTabTools()")
    assert tab_at >= 0
    window = src[tab_at: tab_at + 8000]
    assert 'data-act="open-skill-import"' in window, (
        "Tools tab missing the Import skill button"
    )
    assert "Import skill" in window


def test_import_modal_function_exists():
    src = _read_index_html()
    assert "function openSkillImportModal" in src
    assert "/api/skills/import" in src


def test_import_modal_handles_451_license_reprompt():
    """The 451 response from POST /api/skills/import means the model
    declared a non-OSS license and needs user confirmation. The
    modal must re-open with a license-confirm panel rather than
    failing silently."""
    src = _read_index_html()
    fn_at = src.find("function openSkillImportModal")
    assert fn_at >= 0
    body = src[fn_at: fn_at + 6000]
    assert "license_confirm_required" in body
    # Re-opens with the license info
    assert "openSkillImportModal({" in body or "openSkillImportModal({" in body
    assert "accept_license" in body
