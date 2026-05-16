"""Phase 3 — per-goal browser sessions for parallel goal isolation.

Before this commit, skills/browser.py had module-level _browser / _page /
_pages globals. Two parallel goals both calling browser_open would share
the SAME Chrome instance and clobber each other's cookies, page state,
network log. LinkedIn login from goal A would leak into goal B's session,
or evict it on cross-page navigation.

This commit refactored to a BrowserSession registry keyed by goal_id:
each goal gets its own persistent user_data_dir + Chrome process. The
default singleton "__default__" still handles chat / cli / telegram so
the existing non-goal paths are unchanged.

Tests below verify:
  1. Sessions are distinct objects with distinct user_data_dir paths
  2. The active session is picked from ctx.goal_id (Goal-bound turn)
  3. No ctx → falls back to "__default__"
  4. The executor-thread override flag (_executor_thread_session) wins
     over ctx — this is what makes parallelism work across the
     ThreadPoolExecutor hop
  5. Session close drops it from the registry; subsequent get re-creates
"""
from __future__ import annotations

import importlib.util
import threading
from pathlib import Path


def _load_browser():
    spec = importlib.util.spec_from_file_location(
        "browser_under_test",
        str(Path(__file__).resolve().parent.parent / "skills" / "browser.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sessions_are_isolated_per_goal(qwe_temp_data_dir):
    """Different session_ids produce distinct BrowserSession objects
    with distinct user_data_dir paths."""
    browser = _load_browser()
    s1 = browser._get_session("goal_alpha")
    s2 = browser._get_session("goal_beta")
    s_def = browser._get_session("__default__")

    assert s1 is not s2
    assert s1 is not s_def
    assert s2 is not s_def

    assert s1.user_data_dir != s2.user_data_dir
    assert s1.user_data_dir != s_def.user_data_dir
    assert "goal_alpha" in str(s1.user_data_dir)
    assert "goal_beta" in str(s2.user_data_dir)


def test_get_session_is_idempotent(qwe_temp_data_dir):
    """Repeated calls with the same id return the SAME object — no
    accidentally launching two Chromes for one goal."""
    browser = _load_browser()
    a = browser._get_session("goal_x")
    b = browser._get_session("goal_x")
    assert a is b


def test_close_session_drops_from_registry(qwe_temp_data_dir):
    """_close_session removes the entry so a future get re-creates fresh."""
    browser = _load_browser()
    s1 = browser._get_session("goal_y")
    browser._close_session("goal_y")
    s2 = browser._get_session("goal_y")
    # Different object after close+re-get
    assert s1 is not s2


def test_get_active_session_uses_ctx_goal_id(qwe_temp_data_dir):
    """When a TurnContext with goal_id is active, _get_active_session
    routes to that goal's per-goal session — not the default."""
    browser = _load_browser()
    import tools as _tools
    from turn_context import TurnContext

    _tools._set_turn_ctx(TurnContext(source="cli", goal_id="goal_routed"))
    try:
        active = browser._get_active_session()
        assert active.session_id == "goal_routed"
    finally:
        _tools._set_turn_ctx(None)


def test_get_active_session_falls_back_to_default(qwe_temp_data_dir):
    """No ctx → default session. Chat / cli / telegram path."""
    browser = _load_browser()
    import tools as _tools
    _tools._set_turn_ctx(None)
    active = browser._get_active_session()
    assert active.session_id == "__default__"


def test_executor_thread_session_override_wins(qwe_temp_data_dir):
    """When _executor_thread_session.session_id is set on the inner
    thread (the way execute() propagates session across the executor
    hop), it takes precedence over ctx lookup. Without this property,
    parallel goals would all collapse to '__default__' inside the
    browser executor thread."""
    browser = _load_browser()
    import tools as _tools
    from turn_context import TurnContext

    # ctx says goal_a, but explicit override says goal_b.
    # Override wins because it's the explicit cross-thread propagation.
    _tools._set_turn_ctx(TurnContext(source="cli", goal_id="goal_a"))
    try:
        browser._executor_thread_session.session_id = "goal_b"
        active = browser._get_active_session()
        assert active.session_id == "goal_b"
    finally:
        browser._executor_thread_session.session_id = None
        _tools._set_turn_ctx(None)


def test_resolve_session_id_from_ctx(qwe_temp_data_dir):
    """The helper used by execute() to capture the target session in the
    CALLER thread (before hopping to the browser executor) returns the
    goal_id from ctx, or '__default__' when no goal."""
    browser = _load_browser()
    import tools as _tools
    from turn_context import TurnContext

    _tools._set_turn_ctx(TurnContext(source="cli", goal_id="goal_xyz"))
    assert browser._resolve_session_id_from_ctx() == "goal_xyz"

    _tools._set_turn_ctx(None)
    assert browser._resolve_session_id_from_ctx() == "__default__"


def test_parallel_threads_get_different_sessions(qwe_temp_data_dir):
    """Two threads with different ctx.goal_id values each see their OWN
    session via _get_active_session. This is the property that makes
    parallel goals actually isolated, not just nominally so."""
    browser = _load_browser()
    import tools as _tools
    from turn_context import TurnContext

    results: dict[str, str] = {}

    def _worker(goal_id: str):
        _tools._set_turn_ctx(TurnContext(source="cli", goal_id=goal_id))
        try:
            sess = browser._get_active_session()
            results[goal_id] = sess.session_id
        finally:
            _tools._set_turn_ctx(None)

    t1 = threading.Thread(target=_worker, args=("goal_one",))
    t2 = threading.Thread(target=_worker, args=("goal_two",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert results == {"goal_one": "goal_one", "goal_two": "goal_two"}


def test_user_data_dir_under_data_dir(qwe_temp_data_dir):
    """user_data_dir is rooted at config.DATA_DIR/browser_sessions/<id> so
    it follows the user's CASTOR_DATA_DIR setting (tests use a tempdir)."""
    browser = _load_browser()
    import config
    s = browser._get_session("dirtest")
    expected_root = Path(config.DATA_DIR) / "browser_sessions"
    assert str(s.user_data_dir).startswith(str(expected_root))
    assert s.user_data_dir.name == "dirtest"
