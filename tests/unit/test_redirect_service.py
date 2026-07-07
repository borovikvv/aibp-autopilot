"""Tests for the click-tracking redirect service (issue #15).

DB calls are mocked with an in-memory registry; the HTTP smoke test runs
a real ThreadingHTTPServer on an ephemeral port.
"""
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import httpx
import pytest

from aibp.tracking import redirect_service as rs

# ═══════════════════════════════════════════════════════════════════
# In-memory DB double
# ═══════════════════════════════════════════════════════════════════

class FakeDB:
    def __init__(self):
        self.links: dict[str, dict] = {}
        self.clicks: list[tuple] = []

    def execute(self, sql: str, params: tuple = ()) -> int:
        if "INSERT INTO tracked_links" in sql:
            short_id, feed_item_id, target_url, offer_id = params
            self.links.setdefault(short_id, {
                "feed_item_id": feed_item_id, "target_url": target_url,
                "offer_id": offer_id,
            })
            return 1
        if "INSERT INTO link_clicks" in sql:
            self.clicks.append(params)
            return 1
        raise AssertionError(f"unexpected sql: {sql}")

    def fetch_one(self, sql: str, params: tuple = ()):
        return self.links.get(params[0])


@pytest.fixture()
def fake_db():
    db = FakeDB()
    with patch.object(rs, "execute", db.execute), \
         patch.object(rs, "fetch_one", db.fetch_one):
        yield db


# ═══════════════════════════════════════════════════════════════════
# Registry functions
# ═══════════════════════════════════════════════════════════════════

def test_short_id_is_deterministic():
    a = rs.make_short_id(1, "https://example.com/article")
    b = rs.make_short_id(1, "https://example.com/article")
    c = rs.make_short_id(2, "https://example.com/article")
    assert a == b
    assert a != c
    assert len(a) == 8


def test_register_link_and_resolve(fake_db):
    short_id = rs.register_link(42, "https://example.com/article")
    target = rs.resolve_and_log_click(short_id, user_agent="test-agent")
    assert target == "https://example.com/article"
    assert len(fake_db.clicks) == 1
    assert fake_db.clicks[0][0] == 42          # feed_item_id
    assert fake_db.clicks[0][1] == short_id


def test_resolve_unknown_returns_none(fake_db):
    assert rs.resolve_and_log_click("deadbeef") is None
    assert fake_db.clicks == []


# ═══════════════════════════════════════════════════════════════════
# HTTP smoke test
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture()
def live_server(fake_db):
    server = ThreadingHTTPServer(("127.0.0.1", 0), rs.RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", fake_db
    server.shutdown()


def test_smoke_redirect_302(live_server):
    base, db = live_server
    short_id = rs.register_link(7, "https://example.com/target")

    resp = httpx.get(f"{base}/r/{short_id}", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://example.com/target"
    assert len(db.clicks) == 1


def test_smoke_unknown_short_id_404(live_server):
    base, _ = live_server
    resp = httpx.get(f"{base}/r/00000000", follow_redirects=False)
    assert resp.status_code == 404


def test_smoke_healthz(live_server):
    base, _ = live_server
    resp = httpx.get(f"{base}/healthz")
    assert resp.status_code == 200


def test_smoke_non_redirect_path_404(live_server):
    base, _ = live_server
    resp = httpx.get(f"{base}/etc/passwd", follow_redirects=False)
    assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════
# Pipeline link wrapping
# ═══════════════════════════════════════════════════════════════════

def test_wrap_tracked_links_replaces_href(fake_db):
    from aibp.generation import pipeline

    post = '<b>Заголовок</b>\n\nТекст.\n\n<a href="https://example.com/article">Источник</a>'
    settings = type("S", (), {"tracking_base_url": "https://track.example/r-base"})()
    with patch.object(pipeline, "get_settings", return_value=settings), \
         patch.object(rs, "get_settings", return_value=settings):
        wrapped = pipeline.wrap_tracked_links(post, feed_item_id=42)

    short_id = rs.make_short_id(42, "https://example.com/article")
    assert f'<a href="https://track.example/r-base/r/{short_id}">' in wrapped
    assert "https://example.com/article" not in wrapped.split("Источник")[0].split("href")[-1]
    assert short_id in fake_db.links


def test_wrap_tracked_links_disabled_without_base_url():
    from aibp.generation import pipeline

    post = '<a href="https://example.com/a">Источник</a>'
    settings = type("S", (), {"tracking_base_url": ""})()
    with patch.object(pipeline, "get_settings", return_value=settings):
        assert pipeline.wrap_tracked_links(post, 1) == post


def test_wrap_tracked_links_keeps_direct_url_on_error():
    from aibp.generation import pipeline

    post = '<a href="https://example.com/a">Источник</a>'
    settings = type("S", (), {"tracking_base_url": "https://track.example"})()

    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    with patch.object(pipeline, "get_settings", return_value=settings), \
         patch.object(rs, "execute", boom):
        assert pipeline.wrap_tracked_links(post, 1) == post
