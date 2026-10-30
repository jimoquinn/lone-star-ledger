#!/usr/bin/env python3
"""Pull the Hays County early-vote files through YOUR OWN already-open Chrome.

Hays' CivicPlus/Cloudflare setup blocks datacenter addresses and headless browsers, but a
person browsing normally from a residential connection gets through. This script does not
disguise anything: it attaches over the DevTools protocol (CDP) to a Chrome that you started
and personally clicked through Cloudflare, and reuses that real session's cookies to fetch the
three files you are already allowed to open. It is the manual download you do by hand, scripted.

It only works while that Chrome is open and past the challenge, so it is a supervised pull, not
an unattended one. Files are written into data/inbox/hays/ with stable names; the collector's
next run archives and diffs them like any other source (counts only; rosters stay mode 0600).

Setup (once): fully quit Chrome, then start a dedicated, debuggable instance:

    google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/.config/google-chrome-lsl"

In that window, open the Hays election page and clear any Cloudflare check once. Then:

    ~/lsl-venv/bin/python collector/hays_fetch.py          # or: --cdp http://localhost:9222

Requires the Playwright Python package (already in ~/lsl-venv); no browser download needed
because it attaches to your real Chrome.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import Error as PWError, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
INBOX = ROOT / "data" / "inbox" / "hays"

# (url, stable inbox base name). The collector keys the diff chain on the base name and guesses
# the extension from the file's own bytes, so use the same base every day.
TARGETS = [
    ("https://www.hayscountytx.gov/873/November-3-2026-General-Election", "election-page"),
    ("https://www.hayscountytx.gov/DocumentCenter/View/5414", "absentee-received-5414"),
    ("https://www.hayscountytx.gov/DocumentCenter/View/5454", "in-person-checkins-5454"),
]

CHALLENGE = (b"just a moment", b"attention required", b"you have been blocked",
             b"enable javascript and cookies", b"cf-browser-verification")


def looks_blocked(body: bytes, ctype: str) -> bool:
    if "html" not in (ctype or "").lower():
        return False  # a real spreadsheet/csv is not the challenge page
    head = body[:4000].lower()
    return any(c in head for c in CHALLENGE)


def ext_for(name: str, ctype: str, body: bytes) -> str:
    if re.search(r"\.[A-Za-z0-9]{2,4}$", name):
        return ""
    if body[:5] == b"%PDF-":
        return ".pdf"
    if body[:4] == b"PK\x03\x04":
        return ".xlsx"
    ct = (ctype or "").lower()
    for k, v in (("spreadsheet", ".xlsx"), ("excel", ".xls"), ("csv", ".csv"),
                 ("pdf", ".pdf"), ("json", ".json"), ("html", ".html"), ("plain", ".txt")):
        if k in ct:
            return v
    head = body[:2000].lstrip()
    return ".html" if head[:1] == b"<" else ".csv"


def nav_fetch(ctx, url: str) -> tuple[int, str, bytes]:
    """Fetch a URL through a real browser navigation, the way a person clicking it would.

    Cloudflare blocks Playwright's side-channel request client but lets a genuine navigation
    through, so both the HTML pages and the file downloads go through a live tab here. A
    DocumentCenter file arrives as a browser download (goto raises "Download is starting"); an
    HTML view arrives as a rendered page. Returns (status, content_type, body).
    """
    page = ctx.new_page()
    download = {}
    page.on("download", lambda d: download.setdefault("d", d))
    try:
        resp = None
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except PWError as e:
            if "Download is starting" not in str(e) and "net::ERR_ABORTED" not in str(e):
                raise
        for _ in range(240):  # up to 60s for a large download to finish
            if download:
                break
            page.wait_for_timeout(250)
        if download:
            d = download["d"]
            name = d.suggested_filename or ""
            tmp = Path(tempfile.gettempdir()) / f"lsl_hays_{os.getpid()}_{name or 'dl'}"
            try:
                d.save_as(tmp)  # waits for completion; works with CDP-attached contexts
                body = tmp.read_bytes()
            finally:
                tmp.unlink(missing_ok=True)
            ct = {".xlsx": "application/vnd.openxmlformats", ".csv": "text/csv",
                  ".pdf": "application/pdf"}.get(Path(name).suffix.lower(), "")
            return 200, ct, body
        page.wait_for_timeout(1500)  # let the view settle
        return (resp.status if resp else 200), "text/html", page.content().encode("utf-8")
    finally:
        page.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", default=os.environ.get("CHROME_CDP", "http://localhost:9222"),
                    help="DevTools endpoint of your running Chrome (default http://localhost:9222)")
    ap.add_argument("--out", default=str(INBOX), help="inbox folder (default data/inbox/hays)")
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(a.cdp, timeout=10_000)
        except Exception as e:
            print(f"Could not attach to Chrome at {a.cdp}: {type(e).__name__}: {str(e)[:160]}", file=sys.stderr)
            print("Start Chrome with:  google-chrome --remote-debugging-port=9222 "
                  '--user-data-dir="$HOME/.config/google-chrome-lsl"', file=sys.stderr)
            return 2
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        ok = 0
        for url, base in TARGETS:
            try:
                status, ctype, body = nav_fetch(ctx, url)
                if status != 200 or not body:
                    print(f"! {base}: HTTP {status}, {len(body)} bytes", file=sys.stderr); continue
                if looks_blocked(body, ctype):
                    print(f"! {base}: Cloudflare challenge/block returned ({len(body)} bytes) - "
                          "click through it in the Chrome window, then rerun", file=sys.stderr); continue
                dest = out / f"{base}{ext_for(base, ctype, body)}"
                fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(body)
                print(f"+ {dest.name} ({len(body):,} bytes, {ctype.split(';')[0]})")
                ok += 1
            except Exception as e:
                print(f"! {base}: {type(e).__name__}: {str(e)[:160]}", file=sys.stderr)
        browser.close()
    print(f"{ok}/{len(TARGETS)} files written to {out}")
    print("The next collector run (within 15 min) will archive and diff them; "
          "or run now:  python3 collector/collect.py --only hays")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
