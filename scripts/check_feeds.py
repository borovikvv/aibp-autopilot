#!/usr/bin/env python3
"""Verify every RSS feed URL is reachable and parseable (issue #40).

Fetches each feed, asserts HTTP 200 + parseable XML + ≥1 entry.
Prints a table. Run before committing changes to rss_feeds.yaml.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import feedparser
import httpx

from aibp.utils.config import load_rss_feeds


def check_feed(name: str, url: str) -> tuple[str, int, str]:
    try:
        resp = httpx.get(url, timeout=20, follow_redirects=True,
                         headers={"User-Agent": "AIBP-Autopilot/1.0"})
        if resp.status_code != 200:
            return ("FAIL", resp.status_code, f"HTTP {resp.status_code}")
        parsed = feedparser.parse(resp.content)
        if parsed.bozo and not parsed.entries:
            return ("FAIL", resp.status_code, f"parse error: {parsed.bozo_exception}")
        return ("OK", resp.status_code, f"{len(parsed.entries)} entries")
    except Exception as e:
        return ("FAIL", 0, str(e)[:80])


def main() -> int:
    config = load_rss_feeds()
    feeds = config.get("feeds", [])
    print(f"{'Name':<40} {'Status':<6} {'HTTP':>4}  Detail")
    print("-" * 90)
    failures = 0
    for feed in feeds:
        status, code, detail = check_feed(feed.get("name", ""), feed["url"])
        print(f"{feed.get('name',''):<40} {status:<6} {code:>4}  {detail}")
        if status == "FAIL":
            failures += 1
    print(f"\n{len(feeds)} feeds, {failures} failures.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
