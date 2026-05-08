"""Tests for the telemetry module — privacy contract enforcement.

These tests pin the design rules:
- default OFF
- only whitelisted events accepted
- type-strict prop validation
- enum-constrained string props reject out-of-set values
- opt_in / opt_out / forget_me state transitions
- queue management (cap, clear, snapshot)
- flush is no-op without endpoint
- track_event silently no-ops when disabled (no exceptions raised)

If a future refactor weakens any of these guarantees, these tests fail.
"""

from __future__ import annotations

import importlib
import sys

import pytest


@pytest.fixture
def fresh_tel(qwe_temp_data_dir):
    """Reload telemetry against a fresh QWE_DATA_DIR so each test starts
    with a clean kv table + empty queue."""
    if "telemetry" in sys.modules:
        importlib.reload(sys.modules["telemetry"])
    import telemetry as t
    t.clear_queue()
    return t


# ── Default-OFF contract ─────────────────────────────────────────────


def test_telemetry_disabled_by_default(fresh_tel):
    assert fresh_tel.enabled() is False


def test_track_event_is_noop_when_disabled(fresh_tel):
    accepted = fresh_tel.track_event("session_start", {
        "qwe_version": "0.18.4",
        "python_version": "3.12.0",
        "os": "linux",
        "provider_kind": "openai",
        "model_size_bucket": "large",
        "has_web_ui": True,
        "has_telegram": False,
        "has_voice": False,
        "has_camera": False,
        "has_scheduler": False,
        "has_mcp": False,
        "active_skills_count": 4,
        "scheduled_jobs_count": 0,
        "indexed_sources_count": 0,
    })
    assert accepted is False
    assert fresh_tel.queue_size() == 0


# ── opt-in / opt-out / forget_me ─────────────────────────────────────


def test_opt_in_enables_and_creates_anonymous_id(fresh_tel):
    aid = fresh_tel.opt_in()
    assert fresh_tel.enabled() is True
    assert isinstance(aid, str)
    assert len(aid) >= 16  # uuid hex is 32 chars; allow some slack


def test_opt_out_disables_and_clears_queue(fresh_tel):
    fresh_tel.opt_in()
    fresh_tel.track_event("feature_first_use", {"feature": "camera_capture"})
    assert fresh_tel.queue_size() == 1
    fresh_tel.opt_out()
    assert fresh_tel.enabled() is False
    assert fresh_tel.queue_size() == 0


def test_opt_out_keeps_anonymous_id_for_consistency(fresh_tel):
    """opt_out preserves the id so a future re-opt-in stays consistent.
    forget_me is the heavy hammer if the user wants to break correlation."""
    fresh_tel.opt_in()
    aid_before = fresh_tel.anonymous_id()
    fresh_tel.opt_out()
    # Id is still in kv even though telemetry is off
    import config
    assert config.get("telemetry_anonymous_id") == aid_before


def test_forget_me_wipes_anonymous_id(fresh_tel):
    fresh_tel.opt_in()
    aid_before = fresh_tel.anonymous_id()
    fresh_tel.forget_me()
    assert fresh_tel.enabled() is False
    import config
    assert config.get("telemetry_anonymous_id") == ""
    # And opt-in again gives a different id (not the old one)
    aid_after = fresh_tel.opt_in()
    assert aid_after != aid_before


def test_reset_anonymous_id_rotates_without_disabling(fresh_tel):
    fresh_tel.opt_in()
    aid_before = fresh_tel.anonymous_id()
    aid_after = fresh_tel.reset_anonymous_id()
    assert aid_before != aid_after
    assert fresh_tel.enabled() is True  # still enabled


# ── Consent versioning ──────────────────────────────────────────────


def test_opt_in_stamps_current_consent_version(fresh_tel):
    """opt_in() persists _CURRENT_CONSENT_VERSION so we can detect
    later when the policy moved past what the user agreed to."""
    import config
    fresh_tel.opt_in()
    stored = int(config.get("telemetry_consent_version") or 0)
    assert stored == fresh_tel._CURRENT_CONSENT_VERSION
    assert stored >= 1


def test_consent_needs_reprompt_false_when_disabled(fresh_tel):
    """No active consent → no prompt needed regardless of stored version."""
    import config
    config.set("telemetry_consent_version", 0)  # stale
    assert fresh_tel.enabled() is False
    assert fresh_tel.consent_needs_reprompt() is False


def test_consent_needs_reprompt_true_when_stored_below_current(fresh_tel):
    """User opted in under v0 (legacy), policy moved to v1 → prompt."""
    import config
    fresh_tel.opt_in()
    # Simulate older consent version (legacy install)
    config.set("telemetry_consent_version", 0)
    assert fresh_tel.consent_needs_reprompt() is True


def test_consent_needs_reprompt_false_after_reopt_in(fresh_tel):
    """Re-confirming via opt_in() restamps the version, clears flag."""
    import config
    fresh_tel.opt_in()
    config.set("telemetry_consent_version", 0)
    assert fresh_tel.consent_needs_reprompt() is True
    fresh_tel.opt_in()  # re-confirm
    assert fresh_tel.consent_needs_reprompt() is False


def test_consent_decision_made_false_at_install(fresh_tel):
    """Fresh install: consent_version=0 → no decision yet."""
    assert fresh_tel.consent_decision_made() is False


def test_consent_decision_made_true_after_opt_in(fresh_tel):
    fresh_tel.opt_in()
    assert fresh_tel.consent_decision_made() is True


def test_consent_decision_made_true_after_opt_out(fresh_tel):
    """opt_out also stamps the version — declining counts as a decision."""
    fresh_tel.opt_out()
    assert fresh_tel.consent_decision_made() is True


def test_track_event_blocked_when_consent_stale(fresh_tel):
    """Stale consent gate: enabled + old version → reject events.
    This is the privacy-critical guarantee — UI banner is advisory,
    track_event is the actual gate."""
    import config
    fresh_tel.opt_in()
    config.set("telemetry_consent_version", 0)  # downgrade to stale
    assert fresh_tel.consent_needs_reprompt() is True

    accepted = fresh_tel.track_event("feature_first_use", {"feature": "camera_capture"})
    assert accepted is False
    assert fresh_tel.queue_size() == 0


def test_flush_blocked_when_consent_stale(fresh_tel):
    """Belt-and-suspenders: events queued under v0 must not be sent
    once the policy bumps to v1, even though _send_raw / _send_countly
    don't check consent. flush() is the gate."""
    import config
    fresh_tel.opt_in()
    fresh_tel.track_event("feature_first_use", {"feature": "camera_capture"})
    assert fresh_tel.queue_size() == 1

    # Now downgrade consent (simulate project bumping the policy)
    config.set("telemetry_consent_version", 0)
    sent = fresh_tel.flush(send_fn=lambda evs: True)
    assert sent == 0
    # Queue intact — events stay until user re-confirms
    assert fresh_tel.queue_size() == 1


# ── Whitelist enforcement ────────────────────────────────────────────


def test_unknown_event_is_dropped(fresh_tel):
    fresh_tel.opt_in()
    accepted = fresh_tel.track_event("totally_made_up_event", {"x": 1})
    assert accepted is False
    assert fresh_tel.queue_size() == 0


def test_extra_keys_drop_event(fresh_tel):
    """A future refactor adding an unwhitelisted key shouldn't smuggle data."""
    fresh_tel.opt_in()
    accepted = fresh_tel.track_event("feature_first_use", {
        "feature": "camera_capture",
        "user_input": "this is the kind of leak we're guarding against",
    })
    assert accepted is False
    assert fresh_tel.queue_size() == 0


def test_wrong_type_drops_event(fresh_tel):
    fresh_tel.opt_in()
    accepted = fresh_tel.track_event("turn_complete", {
        "duration_ms": "fast",  # should be int
        "rounds": 3,
        "tool_categories_used": [],
        "tool_calls_count": 0,
        "tool_errors_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "context_hits": 0,
        "source": "web",
    })
    assert accepted is False
    assert fresh_tel.queue_size() == 0


def test_enum_constrained_value_rejected(fresh_tel):
    """source enum is fixed — sending arbitrary string drops the event."""
    fresh_tel.opt_in()
    accepted = fresh_tel.track_event("turn_complete", {
        "duration_ms": 100,
        "rounds": 1,
        "tool_categories_used": [],
        "tool_calls_count": 0,
        "tool_errors_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "context_hits": 0,
        "source": "secret_internal_admin_panel",  # not in SOURCES enum
    })
    assert accepted is False


def test_invalid_tool_category_in_list_rejected(fresh_tel):
    """tool_categories_used must contain only enum values — guards
    against leaking custom skill names like 'acme_invoicing'."""
    fresh_tel.opt_in()
    accepted = fresh_tel.track_event("turn_complete", {
        "duration_ms": 100,
        "rounds": 1,
        "tool_categories_used": ["memory", "acme_internal_skill"],  # 2nd not in TOOL_CATEGORIES
        "tool_calls_count": 1,
        "tool_errors_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "context_hits": 0,
        "source": "web",
    })
    assert accepted is False


def test_provider_kind_must_be_in_enum(fresh_tel):
    fresh_tel.opt_in()
    accepted = fresh_tel.track_event("session_start", {
        "qwe_version": "0.18.4",
        "python_version": "3.12.0",
        "os": "linux",
        "provider_kind": "https://my-internal-llm.corp.com",  # leak attempt
        "model_size_bucket": "large",
        "has_web_ui": True,
        "has_telegram": False,
        "has_voice": False,
        "has_camera": False,
        "has_scheduler": False,
        "has_mcp": False,
        "active_skills_count": 0,
        "scheduled_jobs_count": 0,
        "indexed_sources_count": 0,
    })
    assert accepted is False


# ── Happy path ───────────────────────────────────────────────────────


def test_valid_event_accepted_and_queued(fresh_tel):
    fresh_tel.opt_in()
    accepted = fresh_tel.track_event("feature_first_use", {
        "feature": "camera_capture",
    })
    assert accepted is True
    assert fresh_tel.queue_size() == 1
    pending = fresh_tel.get_pending_events()
    assert len(pending) == 1
    e = pending[0]
    assert e["event"] == "feature_first_use"
    assert e["props"] == {"feature": "camera_capture"}
    assert "anonymous_id" in e
    assert "session_id" in e
    assert "ts" in e


def test_session_id_stable_within_process(fresh_tel):
    fresh_tel.opt_in()
    fresh_tel.track_event("feature_first_use", {"feature": "camera_capture"})
    fresh_tel.track_event("feature_first_use", {"feature": "live_voice"})
    pending = fresh_tel.get_pending_events()
    assert len(pending) == 2
    assert pending[0]["session_id"] == pending[1]["session_id"]


def test_anonymous_id_stable_across_calls(fresh_tel):
    fresh_tel.opt_in()
    fresh_tel.track_event("feature_first_use", {"feature": "camera_capture"})
    fresh_tel.track_event("feature_first_use", {"feature": "live_voice"})
    pending = fresh_tel.get_pending_events()
    assert pending[0]["anonymous_id"] == pending[1]["anonymous_id"]


# ── Flush behaviour ──────────────────────────────────────────────────


def test_flush_is_noop_without_endpoint(fresh_tel):
    """Empty endpoint → flush is no-op, queue intact."""
    import config
    config.set("telemetry_endpoint", "")  # explicit override (project ships a default)
    fresh_tel.opt_in()
    fresh_tel.track_event("feature_first_use", {"feature": "camera_capture"})
    sent = fresh_tel.flush()
    assert sent == 0
    # Queue is intact — no endpoint means no send means no clear
    assert fresh_tel.queue_size() == 1


def test_flush_with_test_send_fn(fresh_tel):
    """Tests that flush wires through to a custom send_fn correctly when
    an endpoint IS configured. Stub the network call entirely."""
    import config
    config.set("telemetry_endpoint", "https://stub.invalid/track")
    fresh_tel.opt_in()
    fresh_tel.track_event("feature_first_use", {"feature": "camera_capture"})
    fresh_tel.track_event("feature_first_use", {"feature": "live_voice"})

    sent_to_stub = []

    def fake_send(events):
        sent_to_stub.extend(events)
        return True  # Pretend the server accepted

    sent = fresh_tel.flush(send_fn=fake_send)
    assert sent == 2
    assert fresh_tel.queue_size() == 0
    assert len(sent_to_stub) == 2


def test_flush_failed_send_keeps_queue(fresh_tel):
    """Network error → events stay queued for retry."""
    import config
    config.set("telemetry_endpoint", "https://stub.invalid/track")
    fresh_tel.opt_in()
    fresh_tel.track_event("feature_first_use", {"feature": "camera_capture"})
    sent = fresh_tel.flush(send_fn=lambda events: False)
    assert sent == 0
    assert fresh_tel.queue_size() == 1


# ── Helpers ──────────────────────────────────────────────────────────


def test_bucket_model_size(fresh_tel):
    assert fresh_tel.bucket_model_size(None) == "unknown"
    assert fresh_tel.bucket_model_size(0.5) == "small"
    assert fresh_tel.bucket_model_size(4.0) == "small"
    assert fresh_tel.bucket_model_size(4.1) == "medium"
    assert fresh_tel.bucket_model_size(13.0) == "medium"
    assert fresh_tel.bucket_model_size(70.0) == "large"


def test_os_kind_returns_one_of_known_values(fresh_tel):
    assert fresh_tel.os_kind() in {"linux", "macos", "windows", "other"}


def test_python_version_format(fresh_tel):
    v = fresh_tel.python_version()
    parts = v.split(".")
    assert len(parts) == 3
    for p in parts:
        assert p.isdigit()


# ── Real HTTP sender (`_default_sender`) ─────────────────────────────
#
# The wire-up commit replaces the stub with a real urllib POST + retry
# loop. These tests pin the contract:
# - 2xx → True (queue cleared by flush)
# - 4xx → False, ONE attempt only (no retry — config error)
# - 5xx → retry up to _MAX_ATTEMPTS, success counts, terminal failure
#         returns False
# - URLError (DNS / refused / timeout) → same as 5xx
# - missing endpoint → False without urlopen call
# - other exceptions → swallowed, return False
# - request body shape: {"events": [...]}
# - headers: Content-Type + User-Agent (+ X-QWE-Anonymous-Id when
#   events carry one)
#
# Mock urlopen via monkeypatch — never hits the network. Backoff sleeps
# are no-op'd so the suite doesn't actually wait 7s per retry test.


class _FakeResponse:
    """urllib.request.urlopen returns an http.client.HTTPResponse-shaped
    context manager. Tests only need the bits `_default_sender` reads:
    .status (and getcode() as fallback) and the context-manager protocol."""

    def __init__(self, status: int = 200, body: bytes = b'{"ok":true}'):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status


def _make_http_error(code: int, msg: str = "boom"):
    """Build a urllib.error.HTTPError with sensible defaults — its
    constructor wants url/code/msg/hdrs/fp."""
    import io

    import urllib.error
    return urllib.error.HTTPError(
        url="https://stub.invalid/track",
        code=code,
        msg=msg,
        hdrs={},
        fp=io.BytesIO(b""),
    )


@pytest.fixture
def no_sleep(monkeypatch):
    """Skip retry backoff so 5xx-loop tests don't actually wait 7s."""
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_kw: None)


@pytest.fixture
def configured_endpoint(fresh_tel):
    """Opt in + set a stub endpoint + force raw format. Project default
    is now countly; tests that exercise the raw codepath need to opt
    out of that explicitly."""
    import config
    config.set("telemetry_endpoint", "https://stub.invalid/track")
    config.set("telemetry_format", "raw")
    fresh_tel.opt_in()
    return fresh_tel


def _make_event(tel) -> dict:
    """Make a real event by routing through track_event so it gets the
    full envelope (anonymous_id, session_id, ts, props)."""
    tel.track_event("feature_first_use", {"feature": "camera_capture"})
    pending = tel.get_pending_events()
    return pending[-1]


def test_default_sender_posts_json(monkeypatch, configured_endpoint, no_sleep):
    """Body parses as JSON {"events": [...]}, method=POST, URL=endpoint,
    headers carry Content-Type + User-Agent."""
    import json as _json
    import urllib.request as _ur

    captured = {}

    def fake_urlopen(req, *_a, **_kw):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = req.data
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _FakeResponse(status=200)

    monkeypatch.setattr(_ur, "urlopen", fake_urlopen)

    event = _make_event(configured_endpoint)
    ok = configured_endpoint._default_sender([event])

    assert ok is True
    assert captured["url"] == "https://stub.invalid/track"
    assert captured["method"] == "POST"
    parsed = _json.loads(captured["body"].decode("utf-8"))
    assert "events" in parsed
    assert isinstance(parsed["events"], list)
    assert parsed["events"][0]["event"] == "feature_first_use"
    # urllib lowercases header names in get_header_items
    assert captured["headers"].get("content-type") == "application/json"
    ua = captured["headers"].get("user-agent", "")
    assert ua.startswith("qwe-qwe/")
    # Anonymous-Id echoed in header for receiver-side bucketing
    assert captured["headers"].get("x-qwe-anonymous-id") == event["anonymous_id"]


@pytest.mark.parametrize("status", [200, 201, 202, 204])
def test_default_sender_returns_true_on_2xx(
    monkeypatch, configured_endpoint, no_sleep, status
):
    """Any 2xx is treated as success — collector implementations vary
    (PostHog returns 200, some return 202, some 204 No Content)."""
    import urllib.request as _ur

    monkeypatch.setattr(
        _ur, "urlopen",
        lambda *_a, **_kw: _FakeResponse(status=status),
    )

    event = _make_event(configured_endpoint)
    assert configured_endpoint._default_sender([event]) is True


def test_default_sender_returns_false_on_4xx_no_retry(
    monkeypatch, configured_endpoint, no_sleep
):
    """4xx = config error (bad endpoint, malformed body). One attempt,
    no retry, return False so the queue stays put for the user to fix."""
    import urllib.request as _ur

    counter = {"n": 0}

    def fake(*_a, **_kw):
        counter["n"] += 1
        raise _make_http_error(400, "bad request")

    monkeypatch.setattr(_ur, "urlopen", fake)

    event = _make_event(configured_endpoint)
    ok = configured_endpoint._default_sender([event])
    assert ok is False
    assert counter["n"] == 1  # Crucially: NO retry on 4xx


def test_default_sender_retries_on_5xx_then_succeeds(
    monkeypatch, configured_endpoint, no_sleep
):
    """5xx is retryable. 500, 500, 200 → True after 3 attempts."""
    import urllib.request as _ur

    counter = {"n": 0}

    def fake(*_a, **_kw):
        counter["n"] += 1
        if counter["n"] <= 2:
            raise _make_http_error(500, "internal error")
        return _FakeResponse(status=200)

    monkeypatch.setattr(_ur, "urlopen", fake)

    event = _make_event(configured_endpoint)
    ok = configured_endpoint._default_sender([event])
    assert ok is True
    assert counter["n"] == 3


def test_default_sender_retries_on_5xx_then_fails(
    monkeypatch, configured_endpoint, no_sleep
):
    """Always 503 → 3 attempts, then False."""
    import urllib.request as _ur

    counter = {"n": 0}

    def fake(*_a, **_kw):
        counter["n"] += 1
        raise _make_http_error(503, "service unavailable")

    monkeypatch.setattr(_ur, "urlopen", fake)

    event = _make_event(configured_endpoint)
    ok = configured_endpoint._default_sender([event])
    assert ok is False
    assert counter["n"] == 3


def test_default_sender_retries_on_network_error(
    monkeypatch, configured_endpoint, no_sleep
):
    """URLError (DNS / refused / timeout) is treated like 5xx — retry,
    then terminal False."""
    import urllib.error
    import urllib.request as _ur

    counter = {"n": 0}

    def fake(*_a, **_kw):
        counter["n"] += 1
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(_ur, "urlopen", fake)

    event = _make_event(configured_endpoint)
    ok = configured_endpoint._default_sender([event])
    assert ok is False
    assert counter["n"] == 3


def test_default_sender_swallows_exceptions(
    monkeypatch, configured_endpoint, no_sleep
):
    """Any unexpected exception class must not propagate — telemetry
    must never break the caller."""
    import urllib.request as _ur

    def fake(*_a, **_kw):
        raise RuntimeError("yo")

    monkeypatch.setattr(_ur, "urlopen", fake)

    event = _make_event(configured_endpoint)
    ok = configured_endpoint._default_sender([event])
    assert ok is False  # Did not raise.


def test_default_sender_no_endpoint_returns_false_without_call(
    monkeypatch, fresh_tel
):
    """If endpoint is empty, sender returns False without ever invoking
    urlopen — the empty-endpoint guard runs before the network."""
    import config
    import urllib.request as _ur

    config.set("telemetry_endpoint", "")
    fresh_tel.opt_in()

    called = {"n": 0}

    def fake(*_a, **_kw):
        called["n"] += 1
        return _FakeResponse(status=200)

    monkeypatch.setattr(_ur, "urlopen", fake)

    fresh_tel.track_event("feature_first_use", {"feature": "camera_capture"})
    pending = fresh_tel.get_pending_events()
    ok = fresh_tel._default_sender(pending)
    assert ok is False
    assert called["n"] == 0


def test_flush_clears_queue_only_on_success(monkeypatch, configured_endpoint):
    """Round-trip: opt in, queue 3 events, mock send → True, flush clears.
    Then queue 2 more, mock send → False, flush leaves queue intact."""
    # First batch — successful send.
    for _ in range(3):
        configured_endpoint.track_event(
            "feature_first_use", {"feature": "camera_capture"}
        )
    assert configured_endpoint.queue_size() == 3
    sent = configured_endpoint.flush(send_fn=lambda _: True)
    assert sent == 3
    assert configured_endpoint.queue_size() == 0

    # Second batch — failed send.
    for _ in range(2):
        configured_endpoint.track_event(
            "feature_first_use", {"feature": "live_voice"}
        )
    assert configured_endpoint.queue_size() == 2
    sent = configured_endpoint.flush(send_fn=lambda _: False)
    assert sent == 0
    assert configured_endpoint.queue_size() == 2  # Events retained.


def test_request_payload_shape(monkeypatch, configured_endpoint, no_sleep):
    """Wire format pin: {"events": [{event, anonymous_id, session_id,
    ts, props}, ...]}. If a future refactor changes the envelope shape
    silently, downstream collectors break — this test catches it."""
    import json as _json
    import urllib.request as _ur

    captured_body = {}

    def fake(req, *_a, **_kw):
        captured_body["raw"] = req.data
        return _FakeResponse(status=200)

    monkeypatch.setattr(_ur, "urlopen", fake)

    configured_endpoint.track_event(
        "feature_first_use", {"feature": "camera_capture"}
    )
    configured_endpoint.track_event(
        "feature_first_use", {"feature": "live_voice"}
    )
    pending = configured_endpoint.get_pending_events()
    assert configured_endpoint._default_sender(pending) is True

    parsed = _json.loads(captured_body["raw"].decode("utf-8"))
    assert set(parsed.keys()) == {"events"}
    assert isinstance(parsed["events"], list)
    assert len(parsed["events"]) == 2
    for ev in parsed["events"]:
        assert set(ev.keys()) >= {"event", "anonymous_id", "session_id", "ts", "props"}
        assert isinstance(ev["event"], str)
        assert isinstance(ev["anonymous_id"], str)
        assert isinstance(ev["session_id"], str)
        # ts is a UNIX timestamp; serialised as a float by json.
        assert isinstance(ev["ts"], (int, float))
        assert isinstance(ev["props"], dict)


# ─── Countly format ──────────────────────────────────────────────────


@pytest.fixture
def countly_endpoint(fresh_tel):
    """Opt in + set a Countly-shaped endpoint + app_key."""
    import config
    config.set("telemetry_endpoint", "https://stub.invalid/i")
    config.set("telemetry_format", "countly")
    config.set("telemetry_countly_app_key", "test_app_key_12345")
    fresh_tel.opt_in()
    return fresh_tel


def test_to_countly_segmentation_lists_become_csv(fresh_tel):
    out = fresh_tel._to_countly_segmentation({"tool_categories_used": ["memory", "files"]})
    assert out == {"tool_categories_used": "memory,files"}


def test_to_countly_segmentation_preserves_primitives(fresh_tel):
    out = fresh_tel._to_countly_segmentation({"count": 5, "ok": True, "name": "foo"})
    assert out == {"count": 5, "ok": True, "name": "foo"}


def test_to_countly_segmentation_empty_list_becomes_empty_string(fresh_tel):
    out = fresh_tel._to_countly_segmentation({"tools": []})
    assert out == {"tools": ""}


def test_to_countly_event_basic_shape(fresh_tel):
    """Our event envelope → Countly event shape (key, count, segmentation, timestamp)."""
    ev = {
        "event": "feature_first_use",
        "anonymous_id": "abc",
        "session_id": "sess",
        "ts": 1700000000.5,
        "props": {"feature": "camera_capture"},
    }
    out = fresh_tel._to_countly_event(ev)
    assert out["key"] == "feature_first_use"
    assert out["count"] == 1
    assert out["segmentation"] == {"feature": "camera_capture"}
    assert out["timestamp"] == 1700000000  # truncated to int
    assert "dur" not in out  # no duration_ms in props → no dur key


def test_to_countly_event_duration_ms_to_dur_seconds(fresh_tel):
    """duration_ms in props → dur (seconds) in Countly event. Lets
    Countly compute event-duration averages natively."""
    ev = {
        "event": "turn_complete",
        "anonymous_id": "abc",
        "session_id": "sess",
        "ts": 1700000000,
        "props": {"duration_ms": 4200, "rounds": 3},
    }
    out = fresh_tel._to_countly_event(ev)
    assert out["dur"] == 4.2
    assert out["segmentation"]["duration_ms"] == 4200
    assert out["segmentation"]["rounds"] == 3


def test_send_countly_no_endpoint_returns_false(fresh_tel):
    import config
    config.set("telemetry_format", "countly")
    config.set("telemetry_countly_app_key", "x")
    fresh_tel.opt_in()
    fresh_tel.track_event("feature_first_use", {"feature": "camera_capture"})
    assert fresh_tel._send_countly(fresh_tel.get_pending_events()) is False


def test_send_countly_no_app_key_returns_false(fresh_tel):
    """app_key is required by Countly — refuse to send without it."""
    import config
    config.set("telemetry_endpoint", "https://stub.invalid/i")
    config.set("telemetry_format", "countly")
    config.set("telemetry_countly_app_key", "")
    fresh_tel.opt_in()
    fresh_tel.track_event("feature_first_use", {"feature": "camera_capture"})
    assert fresh_tel._send_countly(fresh_tel.get_pending_events()) is False


def test_send_countly_post_shape(monkeypatch, countly_endpoint, no_sleep):
    """One batched POST per device_id. Body has app_key, device_id,
    timestamp, events array. Headers carry Content-Type + User-Agent."""
    import json as _json
    import urllib.request as _ur

    captured = []

    def _fake_urlopen(req, *_a, **_kw):
        captured.append({
            "url": req.full_url,
            "method": req.get_method(),
            "headers": {k: v for k, v in req.header_items()},
            "body": _json.loads(req.data.decode("utf-8")),
        })
        return _FakeResponse(status=200)

    monkeypatch.setattr(_ur, "urlopen", _fake_urlopen)

    countly_endpoint.track_event("feature_first_use", {"feature": "camera_capture"})
    countly_endpoint.track_event("turn_complete", {
        "duration_ms": 4200, "rounds": 3,
        "tool_categories_used": ["memory", "files"],
        "tool_calls_count": 2, "tool_errors_count": 0,
        "input_tokens": 10, "output_tokens": 20, "context_hits": 0,
        "source": "web",
    })
    assert countly_endpoint._send_countly(countly_endpoint.get_pending_events()) is True

    # Both events for the same anonymous_id → one POST
    assert len(captured) == 1
    c = captured[0]
    assert c["url"] == "https://stub.invalid/i"
    assert c["method"] == "POST"
    assert c["body"]["app_key"] == "test_app_key_12345"
    assert isinstance(c["body"]["device_id"], str) and c["body"]["device_id"]
    assert "timestamp" in c["body"]
    assert isinstance(c["body"]["events"], list)
    assert len(c["body"]["events"]) == 2
    e0 = c["body"]["events"][0]
    assert e0["key"] == "feature_first_use"
    assert e0["count"] == 1
    assert e0["segmentation"]["feature"] == "camera_capture"
    e1 = c["body"]["events"][1]
    assert e1["key"] == "turn_complete"
    assert e1["dur"] == 4.2
    assert e1["segmentation"]["tool_categories_used"] == "memory,files"
    headers_lc = {k.lower(): v for k, v in c["headers"].items()}
    assert headers_lc["content-type"] == "application/json"
    assert headers_lc["user-agent"].startswith("qwe-qwe/")


def test_send_countly_uses_anonymous_id_as_device_id(monkeypatch, countly_endpoint, no_sleep):
    import urllib.request as _ur
    import json as _json

    captured_device_id = []

    def _fake_urlopen(req, *_a, **_kw):
        body = _json.loads(req.data.decode("utf-8"))
        captured_device_id.append(body["device_id"])
        return _FakeResponse(status=200)

    monkeypatch.setattr(_ur, "urlopen", _fake_urlopen)

    countly_endpoint.track_event("feature_first_use", {"feature": "camera_capture"})
    pending = countly_endpoint.get_pending_events()
    expected_aid = pending[0]["anonymous_id"]

    assert countly_endpoint._send_countly(pending) is True
    assert captured_device_id[0] == expected_aid


def test_send_countly_returns_false_on_4xx_no_retry(monkeypatch, countly_endpoint, no_sleep):
    import urllib.request as _ur

    call_count = {"n": 0}

    def _fake_urlopen(req, *_a, **_kw):
        call_count["n"] += 1
        raise _make_http_error(400, "bad app_key")

    monkeypatch.setattr(_ur, "urlopen", _fake_urlopen)

    countly_endpoint.track_event("feature_first_use", {"feature": "camera_capture"})
    assert countly_endpoint._send_countly(countly_endpoint.get_pending_events()) is False
    assert call_count["n"] == 1


def test_send_countly_retries_on_5xx_then_succeeds(monkeypatch, countly_endpoint, no_sleep):
    import urllib.request as _ur

    call_seq = iter([
        _make_http_error(503, "down"),
        _make_http_error(503, "down"),
        _FakeResponse(status=200),
    ])
    call_count = {"n": 0}

    def _fake_urlopen(req, *_a, **_kw):
        call_count["n"] += 1
        nxt = next(call_seq)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(_ur, "urlopen", _fake_urlopen)

    countly_endpoint.track_event("feature_first_use", {"feature": "camera_capture"})
    assert countly_endpoint._send_countly(countly_endpoint.get_pending_events()) is True
    assert call_count["n"] == 3


def test_send_countly_groups_by_device_id(monkeypatch, countly_endpoint, no_sleep):
    """If anonymous_id rotates mid-queue, each id gets its own POST."""
    import urllib.request as _ur
    import json as _json

    captured_devices = []

    def _fake_urlopen(req, *_a, **_kw):
        body = _json.loads(req.data.decode("utf-8"))
        captured_devices.append(body["device_id"])
        return _FakeResponse(status=200)

    monkeypatch.setattr(_ur, "urlopen", _fake_urlopen)

    countly_endpoint.track_event("feature_first_use", {"feature": "camera_capture"})
    countly_endpoint.reset_anonymous_id()
    countly_endpoint.track_event("feature_first_use", {"feature": "live_voice"})

    pending = countly_endpoint.get_pending_events()
    assert pending[0]["anonymous_id"] != pending[1]["anonymous_id"]

    assert countly_endpoint._send_countly(pending) is True
    assert len(captured_devices) == 2
    assert captured_devices[0] != captured_devices[1]


def test_default_sender_dispatches_to_countly(monkeypatch, countly_endpoint, no_sleep):
    """When format=countly, _default_sender routes through _send_countly."""
    called = {"raw": False, "countly": False}
    monkeypatch.setattr(countly_endpoint, "_send_raw",
                        lambda evs: (called.update(raw=True), True)[1])
    monkeypatch.setattr(countly_endpoint, "_send_countly",
                        lambda evs: (called.update(countly=True), True)[1])

    countly_endpoint.track_event("feature_first_use", {"feature": "camera_capture"})
    countly_endpoint._default_sender(countly_endpoint.get_pending_events())
    assert called == {"raw": False, "countly": True}


def test_default_sender_dispatches_to_raw_by_default(monkeypatch, configured_endpoint, no_sleep):
    """Default format=raw routes through _send_raw, not _send_countly."""
    import config
    config.set("telemetry_format", "raw")
    called = {"raw": False, "countly": False}
    monkeypatch.setattr(configured_endpoint, "_send_raw",
                        lambda evs: (called.update(raw=True), True)[1])
    monkeypatch.setattr(configured_endpoint, "_send_countly",
                        lambda evs: (called.update(countly=True), True)[1])

    configured_endpoint.track_event("feature_first_use", {"feature": "camera_capture"})
    configured_endpoint._default_sender(configured_endpoint.get_pending_events())
    assert called == {"raw": True, "countly": False}


# ── HTTP endpoint flow (mirrors first-run modal in static/index.html) ─


@pytest.fixture
def http_client(fresh_tel):
    """TestClient bound to a fresh qwe_temp_data_dir + reloaded telemetry.

    Mirrors what the browser does when the first-run modal opens:
    GET /status → modal opens iff consent_decision_made:false → user
    clicks Enable → POST /opt-in → on next load, GET /status must say
    consent_decision_made:true (otherwise the modal would re-prompt).
    Pinning this contract here prevents a regression like #unknown
    where a UI-side verify-step started crying false-positive on a
    correctly-persisted opt-in.
    """
    from fastapi.testclient import TestClient
    import server
    with TestClient(server.app) as c:
        yield c


def test_status_endpoint_reports_no_decision_on_fresh_install(http_client):
    """First-run state: telemetry off, no consent stamped, modal would open."""
    r = http_client.get("/api/telemetry/status")
    assert r.status_code == 200
    j = r.json()
    assert j["enabled"] is False
    assert j["consent_decision_made"] is False
    assert j["consent_needs_reprompt"] is False  # nothing to re-prompt yet
    assert j["current_consent_version"] >= 1


def test_opt_in_then_status_reports_decision_made(http_client):
    """The contract the modal relies on: after POST /opt-in, the next
    GET /status must report consent_decision_made:true. If this ever
    breaks, the first-run modal will loop on every page reload.
    """
    # Fresh state — modal would open
    pre = http_client.get("/api/telemetry/status").json()
    assert pre["consent_decision_made"] is False

    # User clicks Enable
    r = http_client.post("/api/telemetry/opt-in")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Next page load — modal must NOT re-prompt
    post = http_client.get("/api/telemetry/status").json()
    assert post["enabled"] is True
    assert post["consent_decision_made"] is True
    assert post["consent_needs_reprompt"] is False


def test_opt_out_then_status_reports_decision_made(http_client):
    """Same contract for the No-thanks branch: explicit opt-out must
    also stamp consent_version so the modal doesn't re-prompt.
    """
    pre = http_client.get("/api/telemetry/status").json()
    assert pre["consent_decision_made"] is False

    r = http_client.post("/api/telemetry/opt-out")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    post = http_client.get("/api/telemetry/status").json()
    assert post["enabled"] is False
    assert post["consent_decision_made"] is True  # decision = "no", but a decision


# ── thread_created event ─────────────────────────────────────────────


def test_thread_created_event_in_whitelist(fresh_tel):
    """The event is part of the validator's closed list — adding new
    events without ALLOWED_EVENTS update is rejected, so confirming
    membership is the contract this test pins."""
    assert "thread_created" in fresh_tel.ALLOWED_EVENTS
    schema = fresh_tel.ALLOWED_EVENTS["thread_created"]
    # The event carries ONLY a source — no name, no id, no meta. If
    # someone adds free-text fields here, this test fails loud.
    assert schema == {"source": str}


def test_thread_created_accepts_each_known_source(fresh_tel):
    """Every source value the call sites use (web/cli/telegram/
    scheduler/preset) must be accepted by the validator. Catches a
    typo regression where one site sends e.g. "tg" or "schedule"."""
    fresh_tel.opt_in()
    for src in ("web", "cli", "telegram", "scheduler", "preset", "other"):
        fresh_tel.clear_queue()
        accepted = fresh_tel.track_event("thread_created", {"source": src})
        assert accepted is True, f"source={src!r} rejected"
        assert fresh_tel.queue_size() == 1
        evt = fresh_tel.get_pending_events()[0]
        assert evt["event"] == "thread_created"
        assert evt["props"] == {"source": src}


def test_thread_created_rejects_unknown_source(fresh_tel):
    """Free-text source must be dropped — closed enum is the privacy
    guarantee that prevents thread names from leaking via this field."""
    fresh_tel.opt_in()
    accepted = fresh_tel.track_event("thread_created", {"source": "my_custom_thing"})
    assert accepted is False
    assert fresh_tel.queue_size() == 0


def test_thread_created_rejects_extra_keys(fresh_tel):
    """The thread name + meta are NEVER part of the event. If a future
    refactor tries to attach them, the validator drops the event."""
    fresh_tel.opt_in()
    accepted = fresh_tel.track_event("thread_created", {
        "source": "web",
        "name": "secret-project-x",  # smuggling attempt
    })
    assert accepted is False
    assert fresh_tel.queue_size() == 0


def test_threads_create_emits_telemetry_when_enabled(qwe_temp_data_dir, monkeypatch):
    """End-to-end: threads.create(source=...) routes through the
    telemetry helper and lands in the queue with the right shape."""
    import importlib
    import sys
    if "telemetry" in sys.modules:
        importlib.reload(sys.modules["telemetry"])
    if "threads" in sys.modules:
        importlib.reload(sys.modules["threads"])
    import telemetry as t
    import threads
    t.opt_in()
    t.clear_queue()

    threads.create("hello", source="web")

    assert t.queue_size() == 1
    evt = t.get_pending_events()[0]
    assert evt["event"] == "thread_created"
    assert evt["props"] == {"source": "web"}


def test_threads_create_emits_nothing_when_disabled(qwe_temp_data_dir, monkeypatch):
    """Default OFF — threads.create() must NOT touch the queue when
    telemetry is disabled. Privacy contract."""
    import importlib
    import sys
    if "telemetry" in sys.modules:
        importlib.reload(sys.modules["telemetry"])
    if "threads" in sys.modules:
        importlib.reload(sys.modules["threads"])
    import telemetry as t
    import threads
    assert t.enabled() is False  # default
    t.clear_queue()

    threads.create("hello")  # no source kwarg → defaults to "other"

    assert t.queue_size() == 0


def test_threads_create_coerces_unknown_source_to_other(qwe_temp_data_dir, monkeypatch):
    """The helper coerces invalid sources to 'other' so a caller who
    passes a typo doesn't drop the event entirely (still useful as a
    count, just bucketed)."""
    import importlib
    import sys
    if "telemetry" in sys.modules:
        importlib.reload(sys.modules["telemetry"])
    if "threads" in sys.modules:
        importlib.reload(sys.modules["threads"])
    import telemetry as t
    import threads
    t.opt_in()
    t.clear_queue()

    threads.create("hello", source="tgbot")  # not in SOURCES

    assert t.queue_size() == 1
    evt = t.get_pending_events()[0]
    assert evt["props"] == {"source": "other"}


def test_consent_version_bumped_to_2(fresh_tel):
    """thread_created adoption bumped consent from v1 to v2. Pinning
    so a future refactor that lowers it (or forgets to bump again)
    is caught here — without this, opted-in users would never see
    the re-confirm banner for the new event type."""
    assert fresh_tel._CURRENT_CONSENT_VERSION >= 2
