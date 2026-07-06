"""Click-tracking redirect service (issue #15).

GET /r/{short_id} → 302 to the target URL, logging the click in PostgreSQL.
GET /healthz     → 200 (for uptime checks).

Runs as a long-lived process (systemd/docker), stdlib HTTP server — traffic
is a Telegram channel's outbound clicks, so no framework is needed.

    python -m aibp.tracking.redirect_service          # port from TRACKING_PORT
"""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import structlog

from aibp.db.connection import execute, fetch_one
from aibp.utils.config import get_settings

log = structlog.get_logger()


# ═══════════════════════════════════════════════════════════════════
# Link registry
# ═══════════════════════════════════════════════════════════════════

def make_short_id(feed_item_id: int, target_url: str) -> str:
    """Deterministic 8-char slug per (post, url) pair."""
    return hashlib.sha256(f"{feed_item_id}:{target_url}".encode()).hexdigest()[:8]


def register_link(feed_item_id: int, target_url: str) -> str:
    """Create (or reuse) a tracked link. Returns the short_id."""
    short_id = make_short_id(feed_item_id, target_url)
    execute(
        """
        INSERT INTO tracked_links (short_id, feed_item_id, target_url)
        VALUES (%s, %s, %s)
        ON CONFLICT (short_id) DO NOTHING
        """,
        (short_id, feed_item_id, target_url),
    )
    return short_id


def short_url(short_id: str) -> str:
    """Public URL for a short_id, e.g. https://domain/r/ab12cd34."""
    base = get_settings().tracking_base_url.rstrip("/")
    return f"{base}/r/{short_id}"


def resolve_and_log_click(short_id: str, user_agent: str | None = None) -> str | None:
    """Look up target URL and record the click. Returns None for unknown ids."""
    link = fetch_one(
        "SELECT feed_item_id, target_url FROM tracked_links WHERE short_id = %s",
        (short_id,),
    )
    if link is None:
        return None
    execute(
        """
        INSERT INTO link_clicks (feed_item_id, short_id, target_url, user_agent)
        VALUES (%s, %s, %s, %s)
        """,
        (link["feed_item_id"], short_id, link["target_url"], user_agent),
    )
    return link["target_url"]


# ═══════════════════════════════════════════════════════════════════
# HTTP server
# ═══════════════════════════════════════════════════════════════════

class RedirectHandler(BaseHTTPRequestHandler):
    server_version = "aibp-redirect/1.0"

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.path == "/healthz":
            self._respond(200, b"ok")
            return

        if not self.path.startswith("/r/"):
            self._respond(404, b"not found")
            return

        short_id = self.path[len("/r/"):].split("?")[0].strip("/")
        if not short_id or not short_id.isalnum():
            self._respond(404, b"not found")
            return

        try:
            target = resolve_and_log_click(short_id, self.headers.get("User-Agent"))
        except Exception as e:
            log.error("click_logging_failed", short_id=short_id, error=str(e))
            self._respond(500, b"internal error")
            return

        if target is None:
            self._respond(404, b"not found")
            return

        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _respond(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        log.info("redirect_request", path=self.path, client=self.client_address[0])


def run(port: int | None = None) -> int:
    """Start the redirect server (blocking)."""
    if port is None:
        port = get_settings().tracking_port
    server = ThreadingHTTPServer(("0.0.0.0", port), RedirectHandler)
    log.info("redirect_service_started", port=port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
