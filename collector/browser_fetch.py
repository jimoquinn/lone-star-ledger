#!/usr/bin/env python3
"""Fetch URLs through a plain headless Chromium so pages that require JavaScript
(Hays County: CivicPlus behind Cloudflare's "just a moment" check) can be archived.
Runs on the US prod box; collect.py calls it over SSH: `python3 browser_fetch.py URL [URL...]`.

The browser is not disguised: default user agent, default automation flags. If the site
still refuses, that is the site's decision and the collector logs the status.

Output: one JSON line per URL on stdout: {"url", "status", "content_type", "b64"}.
Diagnostics go to stderr. The browser profile persists in ~/.lsl-browser so cookies
survive between runs.

Requires: ~/lsl-venv with playwright + `playwright install --with-deps chromium`.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time

from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

PROFILE = os.path.expanduser("~/.lsl-browser")
CHALLENGE_TITLES = ("just a moment", "attention required", "human verification", "access denied")


def is_challenge(title: str, body: str) -> bool:
    t = (title or "").lower()
    return any(c in t for c in CHALLENGE_TITLES)


def fetch_page(page, url: str, wait_s: int = 45) -> tuple[int, str, str]:
    """Navigate; if an interstitial page appears, wait for it to clear. Returns (status, content_type, html)."""
    resp = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    status = resp.status if resp else 0
    ctype = (resp.headers.get("content-type", "") if resp else "")
    deadline = time.time() + wait_s
    while time.time() < deadline:
        title = page.title()
        html = page.content()
        if not is_challenge(title, html):
            return (200 if status in (403, 503) else status), ctype, html
        page.wait_for_timeout(1500)
    return status, ctype, page.content()


def site_root(url: str) -> str:
    return url.split("/DocumentCenter")[0] + "/"


def main(urls: list[str]) -> int:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE, headless=True, locale="en-US",
                                                   timezone_id="America/Chicago")
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            for url in urls:
                rec = {"url": url, "status": 0, "content_type": "", "b64": ""}
                try:
                    if "/DocumentCenter/" not in url:
                        status, ctype, html = fetch_page(page, url)
                        rec.update(status=status, content_type=ctype or "text/html",
                                   b64=base64.b64encode(html.encode("utf-8")).decode())
                    else:
                        # Documents: visit the site first so the session's cookies exist, then
                        # download with the browser's own cookie jar.
                        if not page.url.startswith(site_root(url)) or is_challenge(page.title(), page.content()):
                            fetch_page(page, site_root(url))
                        r = ctx.request.get(url, timeout=90_000, headers={"Referer": page.url})
                        body, ctype, status = r.body(), r.headers.get("content-type", ""), r.status
                        rec.update(status=status, content_type=ctype, b64=base64.b64encode(body).decode())
                except PWTimeout as e:
                    rec["error"] = f"timeout: {str(e)[:120]}"
                except Exception as e:  # keep going with the other URLs
                    rec["error"] = f"{type(e).__name__}: {str(e)[:160]}"
                print(json.dumps(rec), flush=True)
                print(f"{rec['status']} {rec.get('content_type', '')[:30]} {len(rec['b64']) * 3 // 4:>9}B {url[:90]}",
                      file=sys.stderr)
        finally:
            ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
