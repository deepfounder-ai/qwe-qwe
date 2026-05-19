"""JS contract tests pinning the Goals UI to its expected wiring.

These tests don't execute JS — they grep static/index.html for stable anchor
strings and assert that the surrounding wiring is intact. Cheap regression
guards for the kind of bugs pytest would otherwise miss entirely (UI broken
because a rename in tools.py shifted an unrelated string).

Pattern matches `tests/test_telemetry.py::test_api_helper_disables_http_cache`
and `tests/test_ws_attachments.py::test_reload_path_runs_meta_files_through_splitfiles`.
"""
from pathlib import Path

INDEX_HTML = Path(__file__).resolve().parent.parent / "static" / "index.html"


def _read():
    return INDEX_HTML.read_text(encoding="utf-8")


def test_goals_nav_item_present():
    """Left nav rail must include a 'goals' button."""
    src = _read()
    assert "id: 'goals'" in src, "Goals nav item missing from renderRail()"
    assert "icon: 'target'" in src, "Goals nav uses the target icon"


def test_renderGoalsView_dispatched_from_view_router():
    src = _read()
    assert "case 'goals': return renderGoalsView();" in src, \
        "renderGoalsView() not dispatched from renderViewBody()"


def test_go_function_loads_goals_and_stops_polling():
    """go('goals') must trigger loadGoals AND any navigation away must stop polling."""
    src = _read()
    assert "if (view === 'goals') loadGoals();" in src, \
        "go() doesn't trigger loadGoals on navigation"
    assert "stopGoalPolling()" in src, "stopGoalPolling is wired"
    # On any non-goals navigation, polling must be torn down.
    assert "if (view !== 'goals') {" in src and "stopGoalPolling();" in src


def test_goal_detail_polling_uses_fast_cadence_when_running():
    """While the goal is running, poll at 2s; once terminal/paused, fall back to 10s."""
    src = _read()
    assert "next = (status === 'running') ? 2000 : 10000;" in src, \
        "Goal detail polling cadence not adaptive to running status"


def test_goal_actions_wired_to_real_endpoints():
    """The pause/resume/abort buttons must POST to the real API paths."""
    src = _read()
    # The handler builds the path from the act name with a small tweak for resume.
    assert "data-goal-act" in src, "goal action data attribute missing"
    assert "'/api/goals/' + encodeURIComponent(id) + '/' + path" in src, \
        "goal action POST URL doesn't match endpoint shape"


def test_create_goal_modal_posts_to_api():
    """Create modal must POST /api/goals with at minimum user_input."""
    src = _read()
    assert "openCreateGoalModal" in src, "Create goal modal opener missing"
    # Pin the POST contract — user_input is required.
    assert "user_input: input" in src, "Create modal doesn't send user_input"


def test_facts_tab_renders_real_goal_facts_endpoint():
    """Facts tab pulls from /api/goals/{id}/facts (Phase 5b backend)."""
    src = _read()
    assert "'/api/goals/' + encodeURIComponent(goalId) + '/facts'" in src, \
        "loadGoalDetail doesn't fetch /facts endpoint"


def test_load_goal_detail_checks_id_not_error_field():
    """loadGoalDetail must accept the goal row based on presence of ``id``,
    NOT absence of ``error``. Failed goals have an ``error`` field set —
    the old ``!gR.value.error`` guard rejected them so the detail view
    stuck on "Loading goal…" forever."""
    src = _read()
    # The correct guard: gR.value.id (every valid goal row has an id).
    assert "gR.value && gR.value.id" in src, \
        "loadGoalDetail should check gR.value.id, not !gR.value.error"
    # The broken guard must NOT be present.
    assert "!gR.value.error" not in src, \
        "loadGoalDetail still uses !gR.value.error which breaks failed goals"


def test_goals_view_uses_polling_not_websocket():
    """Phase 5 uses HTTP polling; WS push lands in a later phase. Pin that decision."""
    src = _read()
    # The polling is implemented via setTimeout, not WebSocket.
    assert "state._goalPollTimer = setTimeout(tick" in src, \
        "Goals polling not using setTimeout"
