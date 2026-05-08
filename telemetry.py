"""Anonymous telemetry — opt-in, transparent, audit-friendly.

# Privacy guarantees

1. **Default OFF.** Nothing leaves the machine until the user explicitly
   opts in via the first-run prompt or Settings → Privacy → Telemetry.
2. **No chat content.** No user input, no assistant replies, no thinking
   blocks, no thread titles, no message metadata that could contain
   user-typed text.
3. **No soul / personality.** Trait names, levels, custom traits — none
   of this is collected. The agent's persona is the user's design, not
   project metrics.
4. **No identifiers that could deanonymize.** No IP, hostname, username,
   API keys, file paths, exact model names (could be custom finetunes),
   provider URLs (could be internal corporate endpoints), specific
   skill names (user-created skills could leak company identity), or
   tool-call args / results.
5. **Anonymous user ID** is a random UUID generated once at opt-in,
   stored locally in `kv` table. Never derived from any PII. User can
   reset it any time without disabling telemetry.
6. **Allowed-events whitelist.** Every event name and its schema are
   declared in `ALLOWED_EVENTS` below. Unknown events are dropped with
   a warning. Schemas pin the type of every property so a future
   refactor can't accidentally add a string-valued field that smuggles
   chat text.
7. **All collection goes through `track_event()`.** Easy to audit by
   grepping the codebase for `telemetry.track_event`. There is no
   alternate path — no direct queue access from outside this module.
8. **Inert until endpoint configured.** If `telemetry_endpoint` setting
   is empty, the module collects events into the local queue but never
   sends anything over the network. Users who want self-hosted analytics
   can point this at their own collector (PostHog / Plausible / custom).
   The project doesn't ship a default endpoint until the privacy policy
   is signed off.

See `docs/PRIVACY.md` for the human-readable version of this contract.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from typing import Any, Callable

import config

_log = logging.getLogger("qwe.telemetry")

# ── HTTP send tunables ───────────────────────────────────────────────
# Single timeout cap for the whole urlopen call. urllib doesn't separate
# connect/read — one cap is fine for our purposes (collector receives
# tiny JSON, no streaming).
_HTTP_TIMEOUT_S = 10.0
# Retry policy: up to 3 total attempts with exponential backoff between
# them (1s, 2s, 4s). 5xx + network errors retry; 4xx does NOT (it means
# config error — bad endpoint, malformed body — and re-sending just
# spams the receiver).
_MAX_ATTEMPTS = 3
_BACKOFF_SCHEDULE_S = (1.0, 2.0, 4.0)

# ── Whitelist of allowed events ──────────────────────────────────────
#
# Each entry: event name → {prop_name: prop_type}.
# Validation is type-strict — `int`, `str`, `bool`, `float`, `list`, `dict`.
# Lists / dicts are checked one level deep (no recursive type-check) but
# the OUTER schema lock prevents arbitrary fields from sneaking in.
#
# Adding an event here is a deliberate act. Code review should ask:
# - Could this prop value contain user-typed text? (Reject.)
# - Could it contain a path / URL / identifier that ties back to the
#   user's environment beyond OS + version? (Reject or anonymize.)
# - Is the cardinality bounded? (String values should be enums, not
#   free text.)

ALLOWED_EVENTS: dict[str, dict[str, type]] = {
    "session_start": {
        "qwe_version": str,        # e.g. "0.18.4" — already public on GitHub
        "python_version": str,     # e.g. "3.12.10"
        "os": str,                 # "linux" / "macos" / "windows"
        # Provider KIND only — never the URL (could be internal corp endpoint)
        # Allowed values: lmstudio / ollama / openai / azure / bedrock /
        # groq / openrouter / deepseek / together / unknown
        "provider_kind": str,
        # Bucketed model size — never the exact model id (could be a
        # custom finetune that uniquely identifies the org)
        # Allowed values: small (<=4B) / medium (4-13B) / large (>13B) /
        # unknown
        "model_size_bucket": str,
        # Boolean feature flags — what's *enabled*, not what's used
        "has_web_ui": bool,
        "has_telegram": bool,
        "has_voice": bool,
        "has_camera": bool,
        "has_scheduler": bool,
        "has_mcp": bool,
        # Counts only — never names. Three skills, not "acme_invoice_proc".
        "active_skills_count": int,
        "scheduled_jobs_count": int,
        "indexed_sources_count": int,
    },
    "turn_complete": {
        "duration_ms": int,
        "rounds": int,
        # CATEGORIES of tools used (memory / files / shell / browser /
        # http / vision / voice / automation / skills / orchestration),
        # never the specific tool names. Keeps cardinality bounded and
        # avoids leaking custom-skill names.
        "tool_categories_used": list,  # list[str], values from a fixed set
        "tool_calls_count": int,
        "tool_errors_count": int,
        "input_tokens": int,
        "output_tokens": int,
        "context_hits": int,           # number of memory-recall items injected
        # Surface where the turn came from
        "source": str,                  # "web" / "cli" / "telegram" / "scheduler"
    },
    "tool_error": {
        # Category, not specific tool name. Same set as
        # tool_categories_used above.
        "tool_category": str,
        # Error class, not the message text (which could include args /
        # paths / user content). Set: timeout / exception /
        # validation_failed / rate_limited / aborted / blocked
        "error_kind": str,
    },
    "skill_creator_pipeline": {
        # outcome: success / syntax_error / smoke_fail / validate_fail /
        # max_attempts_exhausted / aborted
        "outcome": str,
        "attempts": int,
        "duration_ms": int,
        # Tools count in the GENERATED skill — not their names
        "tools_count": int,
    },
    "feature_first_use": {
        # Tracks first-time activation of a feature in this session, so
        # we can see what users actually try. Single string value from a
        # fixed enum: camera_capture / live_voice / telegram_send /
        # scheduler_create / skill_create / browser_visible / mcp_add /
        # preset_activate / knowledge_index_url / knowledge_index_file
        "feature": str,
    },
}

# Categories used for tool_categories_used and tool_error.tool_category.
# Bound enum prevents free-text leakage of skill / tool names.
TOOL_CATEGORIES = frozenset({
    "memory", "files", "shell", "http", "browser", "vision", "voice",
    "automation", "skills", "orchestration", "vault", "rag", "other",
})

# Error kinds for tool_error.error_kind.
ERROR_KINDS = frozenset({
    "timeout", "exception", "validation_failed", "rate_limited",
    "aborted", "blocked", "not_found", "unauthorized", "other",
})

# Sources for turn_complete.source.
SOURCES = frozenset({"web", "cli", "telegram", "scheduler", "other"})

# Provider kinds.
PROVIDER_KINDS = frozenset({
    "lmstudio", "ollama", "openai", "azure", "bedrock", "groq",
    "openrouter", "deepseek", "together", "unknown",
})

# Model size buckets.
MODEL_SIZE_BUCKETS = frozenset({"small", "medium", "large", "unknown"})

# Outcomes for skill_creator_pipeline.
PIPELINE_OUTCOMES = frozenset({
    "success", "syntax_error", "smoke_fail", "validate_fail",
    "max_attempts_exhausted", "aborted",
})

# Features for feature_first_use.
FEATURES = frozenset({
    "camera_capture", "live_voice", "telegram_send",
    "scheduler_create", "skill_create", "browser_visible",
    "mcp_add", "preset_activate", "knowledge_index_url",
    "knowledge_index_file",
})

# Per-property enum constraints — additional check beyond type
_ENUM_CONSTRAINTS: dict[tuple[str, str], frozenset] = {
    ("session_start", "provider_kind"): PROVIDER_KINDS,
    ("session_start", "model_size_bucket"): MODEL_SIZE_BUCKETS,
    ("turn_complete", "source"): SOURCES,
    ("tool_error", "tool_category"): TOOL_CATEGORIES,
    ("tool_error", "error_kind"): ERROR_KINDS,
    ("skill_creator_pipeline", "outcome"): PIPELINE_OUTCOMES,
    ("feature_first_use", "feature"): FEATURES,
}

# ── Module state ─────────────────────────────────────────────────────

# Bounded queue — hard cap so a never-flushed install can't grow
# unbounded memory / disk usage.
_MAX_QUEUE = 1000
_queue: deque[dict] = deque(maxlen=_MAX_QUEUE)
_queue_lock = threading.Lock()

# Per-process session id, regenerated each start. Lets the receiver
# group events from one run without persisting any cross-session id
# beyond the user's anonymous_id.
_SESSION_ID = uuid.uuid4().hex

# ── Public API ───────────────────────────────────────────────────────


def enabled() -> bool:
    """Is telemetry enabled? Default False. Authoritative check used by
    track_event() to short-circuit before any work is done."""
    val = config.get("telemetry_enabled")
    return bool(val)


def anonymous_id() -> str:
    """Get the user's anonymous id, generating one on first call.

    Generated on first opt-in, persisted in `kv` table, never derived
    from any PII. User can call `reset_anonymous_id()` to rotate it
    without re-opting-in.
    """
    aid = config.get("telemetry_anonymous_id") or ""
    if not aid:
        aid = uuid.uuid4().hex
        config.set("telemetry_anonymous_id", aid)
    return aid


def session_id() -> str:
    """Per-process session id. Resets on every qwe-qwe start."""
    return _SESSION_ID


def opt_in() -> str:
    """Enable telemetry + ensure anonymous_id exists. Returns the id.

    Called by the first-run prompt or the Settings → Privacy toggle.
    Idempotent — safe to call repeatedly.
    """
    aid = anonymous_id()  # generates if missing
    config.set("telemetry_enabled", 1)
    _log.info("telemetry enabled (anonymous_id=%s)", aid[:8] + "...")
    return aid


def opt_out() -> None:
    """Disable telemetry and drop any queued events. Anonymous id is
    NOT deleted by default — keeping it lets a future re-opt-in stay
    consistent. Use `forget_me()` to also wipe the id."""
    config.set("telemetry_enabled", 0)
    with _queue_lock:
        dropped = len(_queue)
        _queue.clear()
    _log.info("telemetry disabled (%d queued events dropped)", dropped)


def forget_me() -> None:
    """Disable telemetry, drop queue, and wipe the anonymous id.

    Stronger than opt_out(): the next opt-in (if any) will get a fresh
    id, so nothing ties the two periods of opt-in together.
    """
    opt_out()
    config.set("telemetry_anonymous_id", "")
    _log.info("telemetry: anonymous_id wiped")


def reset_anonymous_id() -> str:
    """Generate a fresh anonymous id without changing the enabled flag.

    For users who want to "start over" without going through opt-out /
    opt-in. Useful if they fear correlation across long timeframes.
    """
    aid = uuid.uuid4().hex
    config.set("telemetry_anonymous_id", aid)
    _log.info("telemetry anonymous_id rotated")
    return aid


def track_event(name: str, props: dict | None = None) -> bool:
    """Add an event to the queue. Returns True if accepted.

    No-op (returns False) when:
    - telemetry is disabled (default)
    - event name is not in ALLOWED_EVENTS
    - any prop has a wrong type
    - any enum-constrained prop has a value outside its allowed set
    - tool_categories_used contains a category outside TOOL_CATEGORIES

    The strict validation is the audit point: if you're reading this
    code wondering "could a future bug leak chat content via this
    track_event call?", the answer is no — only declared props pass,
    only declared types match, and the enum constraints lock the
    string values down to bounded sets.
    """
    if not enabled():
        return False
    if name not in ALLOWED_EVENTS:
        _log.warning("telemetry: dropping unknown event %r", name)
        return False

    schema = ALLOWED_EVENTS[name]
    props = props or {}

    # Reject any extra keys
    extra = set(props.keys()) - set(schema.keys())
    if extra:
        _log.warning("telemetry: dropping event %r with extra keys %s", name, extra)
        return False

    # Type-check each declared prop
    cleaned: dict[str, Any] = {}
    for prop_name, prop_type in schema.items():
        if prop_name not in props:
            # Missing prop is allowed — schema is the upper bound, not
            # the requirement set. Skip and let the receiver handle.
            continue
        val = props[prop_name]
        if not isinstance(val, prop_type):
            _log.warning(
                "telemetry: dropping event %r — prop %r expected %s, got %s",
                name, prop_name, prop_type.__name__, type(val).__name__,
            )
            return False
        # Enum constraint
        constraint = _ENUM_CONSTRAINTS.get((name, prop_name))
        if constraint is not None and val not in constraint:
            _log.warning(
                "telemetry: dropping event %r — prop %r value %r not in allowed set",
                name, prop_name, val,
            )
            return False
        # List-of-strings check for tool_categories_used
        if isinstance(val, list) and prop_name == "tool_categories_used":
            if not all(isinstance(c, str) for c in val):
                _log.warning("telemetry: tool_categories_used must be list[str]")
                return False
            invalid = [c for c in val if c not in TOOL_CATEGORIES]
            if invalid:
                _log.warning(
                    "telemetry: dropping event — invalid categories %s",
                    invalid,
                )
                return False
        cleaned[prop_name] = val

    # Wrap with common metadata. anonymous_id is generated on first
    # access if missing — but we already checked enabled() above, and
    # opt_in() would have set it.
    event = {
        "event": name,
        "anonymous_id": anonymous_id(),
        "session_id": _SESSION_ID,
        "ts": time.time(),
        "props": cleaned,
    }

    with _queue_lock:
        _queue.append(event)
    return True


def get_pending_events() -> list[dict]:
    """Snapshot of the current queue. For UI inspection — lets the user
    see what's actually queued before they hit "send"."""
    with _queue_lock:
        return list(_queue)


def queue_size() -> int:
    """Cheap count without copying the queue."""
    with _queue_lock:
        return len(_queue)


def clear_queue() -> int:
    """Drop everything in the queue without sending. Returns dropped count."""
    with _queue_lock:
        n = len(_queue)
        _queue.clear()
    return n


def flush(send_fn: Callable[[list[dict]], bool] | None = None) -> int:
    """Send the queue to `telemetry_endpoint`. Returns the number of
    events successfully sent (0 if disabled, no endpoint, or send fails).

    `send_fn` parameter is for tests — production path uses the
    built-in HTTP POST (`_default_sender`). If endpoint is empty,
    returns 0 without doing anything. Queue is cleared only on a 2xx
    response — failures (4xx, 5xx after retries, network errors) keep
    events queued for the next flush attempt.
    """
    if not enabled():
        return 0
    endpoint = (config.get("telemetry_endpoint") or "").strip()
    if not endpoint:
        return 0  # No endpoint configured → silent no-op
    with _queue_lock:
        events = list(_queue)
    if not events:
        return 0
    sender = send_fn or _default_sender
    if sender(events):
        with _queue_lock:
            # Remove only the events we sent — newer events that
            # arrived during the network call stay in the queue
            for _ in range(min(len(events), len(_queue))):
                _queue.popleft()
        return len(events)
    return 0


def _default_sender(events: list[dict]) -> bool:
    """Dispatch by `telemetry_format` to the right wire encoder.

    Two supported formats:
    - "raw" (default) — single batched POST with `{"events": [...]}`
      shape. For custom collectors that accept our schema directly.
    - "plausible" — per-event POST in Plausible Analytics' `/api/event`
      format. For users who self-host Plausible (https://plausible.io).
    """
    fmt = (config.get("telemetry_format") or "raw").strip().lower()
    if fmt == "plausible":
        return _send_plausible(events)
    return _send_raw(events)


def _send_raw(events: list[dict]) -> bool:
    """Built-in HTTP sender (raw format). POSTs the batch as JSON to
    `telemetry_endpoint`.

    Returns True only on a 2xx response from the receiver, in which case
    `flush()` clears the sent events from the queue. Returns False on:
    - missing endpoint (defensive — `flush()` short-circuits earlier)
    - 4xx (config error — don't retry, don't clear queue, let the user
      notice in logs)
    - 5xx after `_MAX_ATTEMPTS` retries
    - network error (DNS / refused / timeout) after retries
    - any exception during request building / encoding

    Never raises out of this function — telemetry must never break the
    caller. Network errors and exceptions are caught and converted into
    a False return.

    Privacy notes:
    - The request body is NEVER logged (it would defeat the
      no-chat-content guarantee if a future event schema regression
      slipped a string field in).
    - The endpoint URL is logged at DEBUG level only; default INFO logs
      show counts and error classes but no URL.
    - We skip the SSRF guard that `rag.py` applies to user-content URLs:
      `telemetry_endpoint` is set explicitly by the user (Settings →
      Privacy → Telemetry endpoint), so a self-hosted PostHog at
      192.168.x.x is a legitimate use case. Endpoint URL is documented
      as the user's responsibility in `docs/PRIVACY.md`.
    """
    endpoint = (config.get("telemetry_endpoint") or "").strip()
    if not endpoint:
        # Defensive: flush() also checks. If we got here directly,
        # don't even attempt urlopen.
        return False

    # Build the request once. Failures here are not retryable.
    try:
        body = json.dumps({"events": events}).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"qwe-qwe/{config.VERSION}",
        }
        # Echo anonymous_id as a header so receivers can bucket without
        # parsing JSON. All events in a batch share the same id (track_event
        # stamps it from the same source), so we read the first event.
        if events:
            aid = events[0].get("anonymous_id")
            if isinstance(aid, str) and aid:
                headers["X-QWE-Anonymous-Id"] = aid
        req = urllib.request.Request(
            endpoint, data=body, headers=headers, method="POST"
        )
    except Exception as e:
        _log.warning(
            "telemetry: send failed building request: %s",
            type(e).__name__,
        )
        return False

    _log.debug("telemetry: sending %d events to %s", len(events), endpoint)

    last_err_class: str = "unknown"
    for attempt in range(_MAX_ATTEMPTS):
        # Backoff BEFORE attempts after the first. Schedule is short
        # enough to be unobtrusive and bounded so flush() can't block
        # the caller indefinitely.
        if attempt > 0:
            time.sleep(_BACKOFF_SCHEDULE_S[attempt - 1])

        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
                # urllib gives `.status` (3.9+) — fall back to getcode()
                # for any odd response object that doesn't expose it.
                status = getattr(resp, "status", None)
                if status is None:
                    try:
                        status = resp.getcode()
                    except Exception:
                        status = 0
                if 200 <= int(status) < 300:
                    _log.info("telemetry: sent %d events", len(events))
                    return True
                # Non-2xx without HTTPError is unusual but possible if a
                # custom opener swallowed it. Treat as terminal failure.
                last_err_class = f"HTTP{status}"
                if 400 <= int(status) < 500:
                    # 4xx — don't retry.
                    break
                # 5xx — fall through to retry path.
                continue
        except urllib.error.HTTPError as he:
            # HTTPError is a subclass of URLError but carries .code.
            code = int(getattr(he, "code", 0) or 0)
            last_err_class = f"HTTP{code}"
            if 400 <= code < 500:
                # 4xx → terminal. Don't retry, don't clear queue.
                break
            # 5xx (or other non-4xx code) → retry.
            continue
        except urllib.error.URLError as ue:
            # DNS fail / connection refused / timeout etc. Retry.
            last_err_class = type(ue).__name__
            continue
        except Exception as e:
            # Any other exception (socket.timeout from older Pythons,
            # SSL error, weird custom error) — treat as retryable so we
            # don't lose the queue on transient flakes, but never raise.
            last_err_class = type(e).__name__
            continue

    _log.warning(
        "telemetry: send failed after %d retries: %s",
        _MAX_ATTEMPTS, last_err_class,
    )
    return False


def _aid_to_synthetic_ip(aid: str) -> str:
    """Map an anonymous_id hex string to a stable synthetic IPv4.

    Plausible counts unique visitors by hashing IP+UA+domain+rotating
    daily salt. Without a per-user IP, every visit folds into one
    "visitor" and unique counts collapse. With our anonymous_id
    deterministically mapped to a synthetic IPv4, Plausible counts
    unique opted-in installs without ever seeing a real IP.

    The IP is purely synthetic — derived from the first 4 bytes of the
    hex anonymous_id. Two installs only collide if their UUIDs share
    the first 4 bytes (1 in 4 billion).

    127.x.x.x (loopback) and 0.x.x.x (reserved) are remapped to the
    10.x.x.x RFC1918 private range so Plausible doesn't reject them.
    """
    if not aid or len(aid) < 8:
        return "10.0.0.1"
    try:
        b1 = int(aid[0:2], 16)
        b2 = int(aid[2:4], 16)
        b3 = int(aid[4:6], 16)
        b4 = int(aid[6:8], 16)
        if b1 in (0, 127):
            b1 = 10
        return f"{b1}.{b2}.{b3}.{b4}"
    except ValueError:
        return "10.0.0.1"


def _to_plausible_props(props: dict) -> dict:
    """Plausible accepts string / number / boolean prop values only,
    no nested objects or arrays. Coerce our props to that shape.

    Lists become comma-joined strings (TOOL_CATEGORIES are short enums,
    so the resulting string is always small + bounded cardinality —
    Plausible can group on "memory,files" as a discrete value).

    Booleans pass through. Ints and floats are stringified at the JSON
    serialiser later — keep as numbers here so Plausible knows the type.
    """
    out: dict = {}
    for k, v in (props or {}).items():
        if isinstance(v, list):
            out[k] = ",".join(str(x) for x in v) if v else ""
        elif isinstance(v, (bool, int, float, str)):
            out[k] = v
        else:
            out[k] = str(v)
    return out


def _send_plausible(events: list[dict]) -> bool:
    """POST each event individually to Plausible's /api/event endpoint.

    Why per-event (not batch): Plausible's API doesn't accept arrays —
    each event is its own POST. We send sequentially with per-event
    retry on 5xx. Returns True only if ALL events succeeded; on partial
    failure the queue is preserved so the next flush retries
    (potentially producing duplicates in Plausible — accepted trade-off
    for at-least-once delivery semantics).

    Plausible requires:
    - User-Agent (we send `qwe-qwe/{version}`)
    - X-Forwarded-For (we send a synthetic IPv4 derived from
      anonymous_id, so unique-visitor counts match unique installs)
    - body.domain matching a site registered in the Plausible dashboard
      (read from `telemetry_plausible_domain` setting)
    - body.url — synthetic `app://qwe-qwe/event/<event_name>` since
      qwe-qwe isn't a website and we have no real URL to attribute
    - body.name — the event name (one of our 5 ALLOWED_EVENTS)
    - body.props — flat string/number/bool key-value (we coerce lists
      to comma-joined strings)
    """
    endpoint = (config.get("telemetry_endpoint") or "").strip()
    if not endpoint:
        return False
    domain = (config.get("telemetry_plausible_domain") or "").strip()
    if not domain:
        _log.warning("telemetry: format=plausible but telemetry_plausible_domain is empty — set it to the site you registered in Plausible")
        return False

    sent_count = 0
    for ev in events:
        ok = _send_one_plausible(ev, endpoint, domain)
        if not ok:
            # First failure → bail. Don't burn through the queue trying
            # each event when the network or config is bad.
            _log.warning(
                "telemetry: plausible send failed at event %d/%d, keeping queue",
                sent_count + 1, len(events),
            )
            return False
        sent_count += 1

    _log.info("telemetry: sent %d events to plausible", sent_count)
    return True


def _send_one_plausible(ev: dict, endpoint: str, domain: str) -> bool:
    """Single-event POST to Plausible. Internal retry on 5xx using the
    same backoff as `_send_raw`. Returns True on 2xx, False otherwise."""
    event_name = ev.get("event") or "unknown"
    aid = ev.get("anonymous_id") or ""
    sid = ev.get("session_id") or ""

    # Build Plausible-shaped props. Drop anonymous_id from props since
    # it's already used to synthesize the IP for unique-counting; keep
    # session_id so the receiver can group within a single run.
    props = _to_plausible_props(ev.get("props") or {})
    if sid:
        props["session_id"] = sid

    body_obj = {
        "name": event_name,
        "url": f"app://qwe-qwe/event/{event_name}",
        "domain": domain,
        "props": props,
    }

    try:
        body = json.dumps(body_obj).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"qwe-qwe/{config.VERSION}",
            "X-Forwarded-For": _aid_to_synthetic_ip(aid),
        }
        req = urllib.request.Request(
            endpoint, data=body, headers=headers, method="POST"
        )
    except Exception as e:
        _log.warning("telemetry: plausible request build failed: %s", type(e).__name__)
        return False

    last_err: str = "unknown"
    for attempt in range(_MAX_ATTEMPTS):
        if attempt > 0:
            time.sleep(_BACKOFF_SCHEDULE_S[attempt - 1])
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
                status = getattr(resp, "status", None)
                if status is None:
                    try:
                        status = resp.getcode()
                    except Exception:
                        status = 0
                if 200 <= int(status) < 300:
                    return True
                last_err = f"HTTP{status}"
                if 400 <= int(status) < 500:
                    return False
        except urllib.error.HTTPError as he:
            code = int(getattr(he, "code", 0) or 0)
            last_err = f"HTTP{code}"
            if 400 <= code < 500:
                return False
        except urllib.error.URLError as ue:
            last_err = type(ue).__name__
        except Exception as e:
            last_err = type(e).__name__

    _log.debug("telemetry: plausible event %r failed: %s", event_name, last_err)
    return False


# ── Helpers for callers that need to bucket sensitive values ─────────


def bucket_model_size(param_count_b: float | None) -> str:
    """Map a model parameter count (in billions) to a coarse bucket.

    Use this anywhere you have an exact model id but don't want to send
    it. Cardinality of the output is fixed to 4 values, so it can't
    deanonymize.
    """
    if param_count_b is None:
        return "unknown"
    if param_count_b <= 4:
        return "small"
    if param_count_b <= 13:
        return "medium"
    return "large"


def os_kind() -> str:
    """OS string in the SOURCES enum format."""
    p = sys.platform
    if p.startswith("linux"):
        return "linux"
    if p == "darwin":
        return "macos"
    if p.startswith("win"):
        return "windows"
    return "other"


def python_version() -> str:
    """Python version as 'major.minor.patch' (no build / compiler info)."""
    v = sys.version_info
    return f"{v.major}.{v.minor}.{v.micro}"


def provider_kind_from_name(name: str | None) -> str:
    """Map a provider preset key to one of PROVIDER_KINDS.

    Use this anywhere you have an active provider name (`providers.get_active_name()`)
    but want to send only the kind, not the URL. Cardinality is fixed by the
    PROVIDER_KINDS frozenset — anything outside it (e.g. user-added custom
    providers, perplexity / cerebras / mistral presets that aren't in the
    enum) collapses to "unknown".
    """
    if not name:
        return "unknown"
    return name if name in PROVIDER_KINDS else "unknown"


# Per-process set of features that have already fired `feature_first_use`.
# Cleared on process restart (matches `_SESSION_ID` regen). The point is to
# emit one event per feature per session, even when callers fire repeatedly.
_FEATURES_USED_THIS_SESSION: set[str] = set()
_features_used_lock = threading.Lock()


def track_feature_first_use(feature: str) -> bool:
    """Emit a `feature_first_use` event the FIRST time a feature is used in
    this process. Subsequent calls with the same feature are silent no-ops.

    Returns True if the event was actually emitted (accepted by
    track_event), False otherwise (telemetry off, already fired this
    session, or feature outside the FEATURES enum).

    Privacy: feature is enum-bounded — anything outside FEATURES is
    rejected by track_event's validator before reaching the queue.
    """
    if feature not in FEATURES:
        # Reject early so a typo here can't smuggle a new free-text
        # value into the payload.
        return False
    with _features_used_lock:
        if feature in _FEATURES_USED_THIS_SESSION:
            return False
        _FEATURES_USED_THIS_SESSION.add(feature)
    return track_event("feature_first_use", {"feature": feature})


def provider_kind_from_url(url: str | None) -> str:
    """URL-based heuristic to classify a provider when only the URL is known.

    Substring match on host. Falls through to "unknown" when nothing matches —
    we never want to send a URL fragment off-machine, even if classification
    fails. This is a defense-in-depth mapping for callers that don't have
    access to the provider name.
    """
    if not url:
        return "unknown"
    u = url.lower()
    if "openai.com" in u:
        return "openai"
    if "openrouter.ai" in u:
        return "openrouter"
    if "groq.com" in u:
        return "groq"
    if "together.xyz" in u or "together.ai" in u:
        return "together"
    if "deepseek.com" in u:
        return "deepseek"
    if "azure.com" in u or "azure-api.net" in u or ".azureml." in u:
        return "azure"
    if "amazonaws.com" in u or "bedrock" in u:
        return "bedrock"
    if "11434" in u:
        return "ollama"
    if "1234" in u or "localhost:1234" in u:
        return "lmstudio"
    return "unknown"
