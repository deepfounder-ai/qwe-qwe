"""Thread folder organisation — threads.meta.folder string-based grouping.

v0.17.x feature: users can drop threads into folders to keep sidebar tidy.
No separate schema — folder name lives in ``threads.meta`` JSON. These
tests pin the CRUD: set, clear, list, normalisation, error path.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def fresh_threads(qwe_temp_data_dir):
    import importlib
    import sys
    for m in ("config", "db", "threads"):
        if m in sys.modules:
            importlib.reload(sys.modules[m])
        else:
            importlib.import_module(m)
    return sys.modules["threads"]


def test_set_folder_stores_in_meta(fresh_threads):
    t = fresh_threads.create("work thread")
    out = fresh_threads.set_folder(t["id"], "Work")
    assert out == {"ok": True, "folder": "Work"}
    fetched = fresh_threads.get(t["id"])
    assert fetched["meta"].get("folder") == "Work"


def test_set_folder_clears_on_empty(fresh_threads):
    t = fresh_threads.create("personal thread")
    fresh_threads.set_folder(t["id"], "Home")
    # Empty string removes the key
    out = fresh_threads.set_folder(t["id"], "")
    assert out == {"ok": True, "folder": ""}
    fetched = fresh_threads.get(t["id"])
    assert "folder" not in fetched["meta"]


def test_set_folder_none_clears(fresh_threads):
    t = fresh_threads.create("thread")
    fresh_threads.set_folder(t["id"], "X")
    fresh_threads.set_folder(t["id"], None)
    fetched = fresh_threads.get(t["id"])
    assert "folder" not in fetched["meta"]


def test_set_folder_trims_and_caps_length(fresh_threads):
    t = fresh_threads.create("thread")
    fresh_threads.set_folder(t["id"], "   Padded   ")
    assert fresh_threads.get(t["id"])["meta"]["folder"] == "Padded"
    # Long name truncated to 60
    long_name = "x" * 200
    fresh_threads.set_folder(t["id"], long_name)
    assert len(fresh_threads.get(t["id"])["meta"]["folder"]) == 60


def test_set_folder_unknown_id_returns_error(fresh_threads):
    out = fresh_threads.set_folder("t_nonexistent", "Work")
    assert "error" in out


def test_list_folders_distinct_sorted_case_insensitive(fresh_threads):
    a = fresh_threads.create("a")
    b = fresh_threads.create("b")
    c = fresh_threads.create("c")
    d = fresh_threads.create("d")
    fresh_threads.set_folder(a["id"], "Zeta")
    fresh_threads.set_folder(b["id"], "alpha")
    fresh_threads.set_folder(c["id"], "alpha")  # duplicate
    fresh_threads.set_folder(d["id"], "")       # none
    folders = fresh_threads.list_folders()
    assert folders == ["alpha", "Zeta"]  # case-insensitive sort, distinct


def test_list_folders_empty_when_none_set(fresh_threads):
    fresh_threads.create("t1")
    fresh_threads.create("t2")
    assert fresh_threads.list_folders() == []


def test_list_all_exposes_folder_via_meta(fresh_threads):
    t = fresh_threads.create("work thread")
    fresh_threads.set_folder(t["id"], "Projects")
    entries = fresh_threads.list_all()
    e = next(x for x in entries if x["id"] == t["id"])
    assert e["meta"]["folder"] == "Projects"
