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
    """Opt in + set a stub endpoint so flush() / _default_sender don't
    short-circuit on the empty-endpoint guard."""
    import config
    config.set("telemetry_endpoint", "https://stub.invalid/track")
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


# ─── Plausible format ────────────────────────────────────────────────


@pytest.fixture
def plausible_endpoint(fresh_tel):
    """Opt in + set a Plausible-shaped endpoint + domain."""
    import config
    config.set("telemetry_endpoint", "https://stub.invalid/api/event")
    config.set("telemetry_format", "plausible")
    config.set("telemetry_plausible_domain", "qwe-qwe.test")
    fresh_tel.opt_in()
    return fresh_tel


def test_aid_to_synthetic_ip_stable(fresh_tel):
    aid = "abcdef1234567890" + "00" * 8
    ip1 = fresh_tel._aid_to_synthetic_ip(aid)
    ip2 = fresh_tel._aid_to_synthetic_ip(aid)
    assert ip1 == ip2


def test_aid_to_synthetic_ip_distinguishes_users(fresh_tel):
    aid_a = "11" + "00" * 15
    aid_b = "ff" + "00" * 15
    assert fresh_tel._aid_to_synthetic_ip(aid_a) != fresh_tel._aid_to_synthetic_ip(aid_b)


def test_aid_to_synthetic_ip_remaps_loopback(fresh_tel):
    """127.x.x.x and 0.x.x.x get remapped to 10.x.x.x so Plausible
    accepts them. Otherwise X-Forwarded-For: 127.0.0.1 would collapse
    every loopback-prefixed UUID into one Plausible visitor."""
    aid = "7fcafe00" + "00" * 12  # First byte 7f = 127
    ip = fresh_tel._aid_to_synthetic_ip(aid)
    assert not ip.startswith("127.")
    assert not ip.startswith("0.")


def test_aid_to_synthetic_ip_handles_short_input(fresh_tel):
    assert fresh_tel._aid_to_synthetic_ip("") == "10.0.0.1"
    assert fresh_tel._aid_to_synthetic_ip("ab") == "10.0.0.1"
    assert fresh_tel._aid_to_synthetic_ip("not-hex!") == "10.0.0.1"


def test_to_plausible_props_lists_become_csv(fresh_tel):
    out = fresh_tel._to_plausible_props({"tool_categories_used": ["memory", "files"]})
    assert out == {"tool_categories_used": "memory,files"}


def test_to_plausible_props_preserves_numbers_bools_strings(fresh_tel):
    out = fresh_tel._to_plausible_props({"count": 5, "ok": True, "name": "foo"})
    assert out == {"count": 5, "ok": True, "name": "foo"}


def test_to_plausible_props_empty_list_becomes_empty_string(fresh_tel):
    out = fresh_tel._to_plausible_props({"tools": []})
    assert out == {"tools": ""}


def test_send_plausible_no_endpoint_returns_false(fresh_tel):
    import config
    config.set("telemetry_format", "plausible")
    config.set("telemetry_plausible_domain", "qwe-qwe.test")
    fresh_tel.opt_in()
    fresh_tel.track_event("feature_first_use", {"feature": "camera_capture"})
    assert fresh_tel._send_plausible(fresh_tel.get_pending_events()) is False


def test_send_plausible_no_domain_returns_false(fresh_tel):
    """domain is required by Plausible — refuse to send without it."""
    import config
    config.set("telemetry_endpoint", "https://stub.invalid/api/event")
    config.set("telemetry_format", "plausible")
    config.set("telemetry_plausible_domain", "")
    fresh_tel.opt_in()
    fresh_tel.track_event("feature_first_use", {"feature": "camera_capture"})
    assert fresh_tel._send_plausible(fresh_tel.get_pending_events()) is False


def test_send_plausible_post_shape(monkeypatch, plausible_endpoint, no_sleep):
    """Verify each POST: URL=endpoint, method=POST, body has Plausible
    keys (name/url/domain/props), headers carry UA + X-Forwarded-For."""
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
        return _FakeResponse(status=202)

    monkeypatch.setattr(_ur, "urlopen", _fake_urlopen)

    plausible_endpoint.track_event("feature_first_use", {"feature": "camera_capture"})
    plausible_endpoint.track_event("turn_complete", {
        "duration_ms": 100, "rounds": 1,
        "tool_categories_used": ["memory", "files"],
        "tool_calls_count": 2, "tool_errors_count": 0,
        "input_tokens": 10, "output_tokens": 20, "context_hits": 0,
        "source": "web",
    })
    assert plausible_endpoint._send_plausible(plausible_endpoint.get_pending_events()) is True

    assert len(captured) == 2
    c0 = captured[0]
    assert c0["url"] == "https://stub.invalid/api/event"
    assert c0["method"] == "POST"
    assert c0["body"]["name"] == "feature_first_use"
    assert c0["body"]["domain"] == "qwe-qwe.test"
    assert c0["body"]["url"].startswith("app://qwe-qwe/event/")
    assert c0["body"]["props"]["feature"] == "camera_capture"
    headers_lc = {k.lower(): v for k, v in c0["headers"].items()}
    assert headers_lc["content-type"] == "application/json"
    assert headers_lc["user-agent"].startswith("qwe-qwe/")
    assert "x-forwarded-for" in headers_lc

    c1 = captured[1]
    assert c1["body"]["name"] == "turn_complete"
    # List prop became CSV string
    assert c1["body"]["props"]["tool_categories_used"] == "memory,files"


def test_send_plausible_synthetic_ip_matches_helper(monkeypatch, plausible_endpoint, no_sleep):
    """X-Forwarded-For for an event must equal _aid_to_synthetic_ip(aid)."""
    import urllib.request as _ur

    captured_xff = []

    def _fake_urlopen(req, *_a, **_kw):
        headers_lc = {k.lower(): v for k, v in req.header_items()}
        captured_xff.append(headers_lc.get("x-forwarded-for"))
        return _FakeResponse(status=202)

    monkeypatch.setattr(_ur, "urlopen", _fake_urlopen)

    plausible_endpoint.track_event("feature_first_use", {"feature": "camera_capture"})
    pending = plausible_endpoint.get_pending_events()
    aid = pending[0]["anonymous_id"]
    expected_ip = plausible_endpoint._aid_to_synthetic_ip(aid)

    assert plausible_endpoint._send_plausible(pending) is True
    assert captured_xff[0] == expected_ip


def test_send_plausible_bails_on_first_4xx(monkeypatch, plausible_endpoint, no_sleep):
    """If event N fails 4xx, don't keep trying events N+1..end. Bail."""
    import urllib.request as _ur

    call_count = {"n": 0}

    def _fake_urlopen(req, *_a, **_kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _FakeResponse(status=202)  # first event ok
        raise _make_http_error(400, "bad request")

    monkeypatch.setattr(_ur, "urlopen", _fake_urlopen)

    plausible_endpoint.track_event("feature_first_use", {"feature": "camera_capture"})
    plausible_endpoint.track_event("feature_first_use", {"feature": "live_voice"})
    plausible_endpoint.track_event("feature_first_use", {"feature": "telegram_send"})

    assert plausible_endpoint._send_plausible(plausible_endpoint.get_pending_events()) is False
    # First event ok (1 call), second 400 with no retry (1 call).
    # Third never attempted.
    assert call_count["n"] == 2


def test_send_plausible_retries_on_5xx_per_event(monkeypatch, plausible_endpoint, no_sleep):
    import urllib.request as _ur

    call_seq = iter([
        _make_http_error(503, "down"),
        _make_http_error(503, "down"),
        _FakeResponse(status=202),  # third attempt succeeds
    ])
    call_count = {"n": 0}

    def _fake_urlopen(req, *_a, **_kw):
        call_count["n"] += 1
        nxt = next(call_seq)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(_ur, "urlopen", _fake_urlopen)

    plausible_endpoint.track_event("feature_first_use", {"feature": "camera_capture"})
    assert plausible_endpoint._send_plausible(plausible_endpoint.get_pending_events()) is True
    assert call_count["n"] == 3


def test_default_sender_dispatches_to_plausible(monkeypatch, plausible_endpoint, no_sleep):
    """When format=plausible, _default_sender routes through
    _send_plausible, not _send_raw."""
    called = {"raw": False, "plausible": False}
    monkeypatch.setattr(plausible_endpoint, "_send_raw",
                        lambda evs: (called.update(raw=True), True)[1])
    monkeypatch.setattr(plausible_endpoint, "_send_plausible",
                        lambda evs: (called.update(plausible=True), True)[1])

    plausible_endpoint.track_event("feature_first_use", {"feature": "camera_capture"})
    plausible_endpoint._default_sender(plausible_endpoint.get_pending_events())
    assert called == {"raw": False, "plausible": True}


def test_default_sender_dispatches_to_raw_by_default(monkeypatch, configured_endpoint, no_sleep):
    """Default format=raw routes through _send_raw, not _send_plausible."""
    import config
    config.set("telemetry_format", "raw")
    called = {"raw": False, "plausible": False}
    monkeypatch.setattr(configured_endpoint, "_send_raw",
                        lambda evs: (called.update(raw=True), True)[1])
    monkeypatch.setattr(configured_endpoint, "_send_plausible",
                        lambda evs: (called.update(plausible=True), True)[1])

    configured_endpoint.track_event("feature_first_use", {"feature": "camera_capture"})
    configured_endpoint._default_sender(configured_endpoint.get_pending_events())
    assert called == {"raw": True, "plausible": False}
