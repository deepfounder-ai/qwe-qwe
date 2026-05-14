"""Tests for the project blog feed endpoint (Presets view).

The endpoint is a small server-side proxy over `https://deepfounder.ai/
tag/castor/rss/`. We cache for 30 minutes and degrade gracefully when
the upstream is unreachable. Tests pin:

- the parser's bounds (string length, item count) so a malicious /
  malformed feed can't blow up the response
- the parser's tolerance of incomplete items (skips, doesn't raise)
- the cache TTL (warm hits don't re-fetch)
- graceful fallback to last-known items on fetch failure
- always returns 200 — never raises into the UI
"""

from __future__ import annotations

import io
import time

import pytest


def _make_feed(items_xml: str = "") -> bytes:
    """Wrap a list of <item>…</item> blocks into a minimal RSS 2.0 feed."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0">'
        '<channel>'
        '<title>castor</title>'
        '<link>https://deepfounder.ai/</link>'
        '<description>test</description>'
        f'{items_xml}'
        '</channel>'
        '</rss>'
    ).encode("utf-8")


def _item(title="t", link="https://example.com/a", description="d",
          pub_date="Fri, 08 May 2026 12:13:18 GMT", categories=None):
    cats = "".join(f"<category><![CDATA[{c}]]></category>" for c in (categories or []))
    return (
        "<item>"
        f"<title><![CDATA[{title}]]></title>"
        f"<link>{link}</link>"
        f"<description><![CDATA[{description}]]></description>"
        f"<pubDate>{pub_date}</pubDate>"
        f"<guid>{link}</guid>"
        f"{cats}"
        "</item>"
    )


@pytest.fixture
def fresh_server(qwe_temp_data_dir, monkeypatch):
    """Reload server so we get a fresh `_feed_cache` per test."""
    import importlib
    import sys
    if "server" in sys.modules:
        importlib.reload(sys.modules["server"])
    import server
    return server


# ── Parser unit tests ───────────────────────────────────────────────


def test_parse_returns_empty_list_for_empty_feed(fresh_server):
    assert fresh_server._parse_blog_feed_xml(_make_feed("")) == []


def test_parse_extracts_known_fields(fresh_server):
    body = _make_feed(_item(
        title="Hello World",
        link="https://deepfounder.ai/post-1/",
        description="A short summary",
        pub_date="Fri, 08 May 2026 12:00:00 GMT",
        categories=["castor", "release"],
    ))
    items = fresh_server._parse_blog_feed_xml(body)
    assert len(items) == 1
    it = items[0]
    assert it["title"] == "Hello World"
    assert it["link"] == "https://deepfounder.ai/post-1/"
    assert it["description"] == "A short summary"
    assert it["pub_date"] == "Fri, 08 May 2026 12:00:00 GMT"
    assert it["categories"] == ["castor", "release"]
    # guid populated from <guid>; we set it to the link in _item()
    assert it["guid"] == "https://deepfounder.ai/post-1/"


def test_parse_skips_items_missing_title_or_link(fresh_server):
    """Items without both title and link are useless to the UI — skip
    rather than render a broken card."""
    body = _make_feed(
        "<item><title>only-title</title></item>"
        "<item><link>https://example.com/only-link</link></item>"
        + _item(title="real", link="https://example.com/real")
    )
    items = fresh_server._parse_blog_feed_xml(body)
    assert len(items) == 1
    assert items[0]["title"] == "real"


def test_parse_caps_at_max_items(fresh_server):
    """No matter how many items the upstream lists, we only return
    `_FEED_MAX_ITEMS`. Bounded payload → bounded UI render cost."""
    cap = fresh_server._FEED_MAX_ITEMS
    items_xml = "".join(_item(title=f"t{i}", link=f"https://example.com/{i}")
                        for i in range(cap + 5))
    items = fresh_server._parse_blog_feed_xml(_make_feed(items_xml))
    assert len(items) == cap


def test_parse_truncates_oversize_strings(fresh_server):
    """A pathological / malicious feed shouldn't be able to push a
    50KB title through to the JSON response. Each field is bounded."""
    long_title = "X" * 5000
    long_desc = "Y" * 5000
    body = _make_feed(_item(
        title=long_title, description=long_desc,
        link="https://example.com/x",
    ))
    items = fresh_server._parse_blog_feed_xml(body)
    assert len(items) == 1
    assert len(items[0]["title"]) <= 300
    assert len(items[0]["description"]) <= 500


def test_parse_caps_categories_count(fresh_server):
    """≤8 categories per item, regardless of upstream."""
    body = _make_feed(_item(categories=[f"cat{i}" for i in range(20)]))
    items = fresh_server._parse_blog_feed_xml(body)
    assert len(items[0]["categories"]) <= 8


def test_parse_raises_on_malformed_xml(fresh_server):
    """The parser surfaces XML errors — the endpoint catches them and
    returns a graceful fallback. We pin the boundary here: parser =
    strict, endpoint = forgiving."""
    import xml.etree.ElementTree as ET
    with pytest.raises(ET.ParseError):
        fresh_server._parse_blog_feed_xml(b"this is not xml at all <<>")


# ── Endpoint behavior ──────────────────────────────────────────────


@pytest.fixture
def http_client(fresh_server):
    """TestClient bound to the freshly-loaded server."""
    from fastapi.testclient import TestClient
    with TestClient(fresh_server.app) as c:
        yield c


@pytest.fixture
def mock_urlopen(monkeypatch, fresh_server):
    """Replace urllib.request.urlopen so tests don't hit the live feed.

    Yields a controller object: assign `.body` (bytes) to set the next
    response, or `.exc` to make the next call raise.
    """
    class _Resp:
        def __init__(self, body):
            self._body = body
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class _Controller:
        def __init__(self):
            self.body = _make_feed(_item())
            self.exc = None
            self.calls = 0

        def urlopen(self, req, timeout=None, **_kw):
            self.calls += 1
            if self.exc is not None:
                raise self.exc
            return _Resp(self.body)

    ctrl = _Controller()
    import server as srv  # the freshly-loaded one
    monkeypatch.setattr(srv.urllib.request, "urlopen", ctrl.urlopen)
    return ctrl


def test_endpoint_returns_items_on_first_call(http_client, mock_urlopen):
    r = http_client.get("/api/feed/blog")
    assert r.status_code == 200
    j = r.json()
    assert j["cached"] is False
    assert len(j["items"]) == 1
    assert j["items"][0]["title"] == "t"
    assert mock_urlopen.calls == 1


def test_endpoint_serves_warm_cache(http_client, mock_urlopen):
    """Second call within the TTL should NOT touch the network."""
    http_client.get("/api/feed/blog")
    r = http_client.get("/api/feed/blog")
    j = r.json()
    assert j["cached"] is True
    assert mock_urlopen.calls == 1  # still 1


def test_endpoint_falls_back_to_stale_on_error(http_client, mock_urlopen, fresh_server):
    """If a fresh fetch is needed but upstream is down, return the
    last-known items + an `error` field. Never break the Presets view."""
    # Prime the cache with one good fetch
    http_client.get("/api/feed/blog")
    # Force the cache to be stale so the next call attempts a refetch
    fresh_server._feed_cache["fetched_at"] = 0.0
    # Now break the network
    mock_urlopen.exc = OSError("network down")
    r = http_client.get("/api/feed/blog")
    assert r.status_code == 200  # still 200 — never raise into UI
    j = r.json()
    assert "error" in j
    assert "network down" in j["error"]
    # And we still serve the cached items
    assert len(j["items"]) == 1


def test_endpoint_returns_empty_items_on_cold_failure(http_client, mock_urlopen):
    """Worst case: cold cache + upstream down. Return `items: []` +
    `error`, NOT 500. The UI renders nothing in this case (empty
    feed → strip hidden) but the rest of the Presets view works."""
    mock_urlopen.exc = OSError("dns failed")
    r = http_client.get("/api/feed/blog")
    assert r.status_code == 200
    j = r.json()
    assert j["items"] == []
    assert "error" in j


def test_endpoint_url_points_at_project_blog(fresh_server):
    """Pin the feed URL so a refactor that drops the trailing /rss/
    doesn't silently make us hit a 404 page (which Ghost serves with
    200 + HTML body — would break the parser)."""
    assert fresh_server._FEED_URL == "https://deepfounder.ai/tag/castor/rss/"
