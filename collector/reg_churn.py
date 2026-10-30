#!/usr/bin/env python3
"""Registered-voter churn tracker (all 254 Texas counties).

Two public, aggregate sources, no voter-level data:
  * Baseline (anchor): the Texas SOS certified "November 2026 Voter Registration Figures"
    (sos.state.tx.us/elections/historical/nov2026.shtml) - per-county registration and
    suspense counts as of the Oct 5, 2026 registration deadline. Fetched once and cached;
    the rolls should not grow after the deadline, and the federal NVRA 90-day quiet period
    bars systematic removals through the election, so this is what "unchanged" means.
  * Live: `registered_voters` per county from the SOS Civix turnout feed we already archive
    every 15 minutes (data/raw/sos). This is the same number, watched over time.

The tracker reports each county's current registration vs. the baseline, flags EVERY change
observed in our capture window (noise is zero - the number is otherwise flat), and writes
frontend/dashboard/reg_churn.json for churn.html. Counts only; nothing voter-level.

Usage: python3 collector/reg_churn.py
"""
from __future__ import annotations

import datetime as dt
import glob
import hashlib
import html.parser
import json
import os
import re
import ssl
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
REGDIR = RAW / "sos-reg"                     # git-ignored (under data/raw)
ANCHOR = REGDIR / "anchor_nov2026.json"      # the immutable Oct-5 baseline, set on first run
OUT = ROOT / "frontend" / "dashboard" / "reg_churn.json"
VR_URL = "https://www.sos.state.tx.us/elections/historical/nov2026.shtml"
VR_LABEL = "Texas SOS certified figures, November 2026 (registration deadline Oct 5, 2026)"
UA = "Mozilla/5.0 (X11; Linux x86_64) TXEarlyVoteTracker/0.1 (+public-interest research)"


def norm(name: str) -> str:
    """County key: letters only, uppercased. 'De Witt' == 'DeWitt' == 'DEWITT'."""
    return re.sub(r"[^A-Z]", "", (name or "").upper())


def to_int(s: str):
    s = re.sub(r"[^\d-]", "", s or "")
    return int(s) if s not in ("", "-") else None


class _VR(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row = None
        self._cell = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("th", "td") and self._row is not None:
            self._cell = tag
            self._buf = []

    def handle_data(self, data):
        if self._cell:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag in ("th", "td") and self._cell:
            self._row.append("".join(self._buf).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None


def fetch_vr_page() -> str | None:
    req = urllib.request.Request(VR_URL, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=60, context=ssl.create_default_context()) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        print(f"  ! VR page fetch failed: {type(e).__name__}: {e}")
        return None


def parse_vr(page: str) -> dict:
    """-> {norm: {name, precincts, reg, suspense, nonsuspense}} plus '_statewide'."""
    p = _VR()
    p.feed(page)
    out: dict = {}
    for row in p.rows:
        if len(row) < 5:
            continue
        name = re.sub(r"\s+", " ", row[0]).strip()
        precincts, reg, suspense, nonsus = (to_int(row[i]) for i in range(1, 5))
        if not name or reg is None:
            continue
        key = "_statewide" if name.upper().startswith("STATEWIDE") else norm(name)
        if not key:
            continue
        out[key] = {"name": name.title() if key != "_statewide" else "Statewide",
                    "precincts": precincts, "reg": reg,
                    "suspense": suspense, "nonsuspense": nonsus}
    return out


def load_anchor(parsed: dict | None) -> dict:
    """Return the immutable Oct-5 baseline. Set it from the first successful parse; keep it
    thereafter so the anchor never drifts even if SOS later revises the page."""
    if ANCHOR.exists():
        anchor = json.loads(ANCHOR.read_text())
        # Note (not overwrite) if the live page now disagrees with the anchor.
        if parsed:
            diffs = {k: [anchor[k]["reg"], v["reg"]] for k, v in parsed.items()
                     if k in anchor and k != "_statewide" and anchor[k]["reg"] != v["reg"]}
            if diffs:
                print(f"  * SOS VR page now differs from the anchor for {len(diffs)} counties (official revision)")
        return anchor
    if not parsed:
        raise SystemExit("no anchor cached and VR page fetch failed; cannot establish baseline")
    REGDIR.mkdir(parents=True, exist_ok=True)
    ANCHOR.write_text(json.dumps(parsed, indent=1))
    print(f"  + anchor set from VR page: {len(parsed)-1} counties")
    return parsed


def archive_vr_page(page: str) -> None:
    """Append-only archive of the raw VR page (git-ignored), so official revisions are kept."""
    sha = hashlib.sha256(page.encode()).hexdigest()
    day = REGDIR / dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    existing = glob.glob(str(REGDIR / "*" / "*_nov2026.html"))
    if any(hashlib.sha256(Path(f).read_bytes()).hexdigest() == sha for f in existing[-3:]):
        return
    day.mkdir(parents=True, exist_ok=True)
    (day / f"{dt.datetime.now(dt.timezone.utc).strftime('%H%M%S')}_{sha[:10]}_nov2026.html").write_text(page)


def capture_ts(path: str) -> str:
    m = re.search(r"/sos/(\d{4}-\d\d-\d\d)/(\d{2})(\d{2})(\d{2})_", path)
    return f"{m[1]}T{m[2]}:{m[3]}:{m[4]}Z" if m else ""


def build_feed_series() -> tuple[dict, dict, list]:
    """Walk archived turnout captures chronologically. Returns current reg per county,
    per-county change-point series, and a flat list of change events (every change)."""
    files = sorted(glob.glob(str(RAW / "sos" / "*" / "*turnout_53815_*.json")))
    last: dict[str, int] = {}
    series: dict[str, list] = {}
    events: list = []
    for f in files:
        ts = capture_ts(f)
        try:
            d = json.load(open(f))
        except Exception:
            continue
        for r in d.get("turnout_by_county", []):
            nm = r.get("name", "")
            key = norm(nm)
            if not key or key in ("STATE", "STATEWIDETOTAL"):
                continue
            reg = r.get("registered_voters")
            if reg is None:
                continue
            if key not in last:
                last[key] = reg
                series[key] = [{"ts": ts, "reg": reg}]
            elif reg != last[key]:
                events.append({"ts": ts, "county": key, "from": last[key], "to": reg,
                               "delta": reg - last[key]})
                series[key].append({"ts": ts, "reg": reg})
                last[key] = reg
    return last, series, events


def main() -> int:
    print("registered-voter churn")
    page = fetch_vr_page()
    if page:
        archive_vr_page(page)
    parsed = parse_vr(page) if page else None
    anchor = load_anchor(parsed)

    current, series, feed_events = build_feed_series()
    print(f"  feed: {len(current)} counties, {len(feed_events)} in-window change events")

    counties = []
    unmatched = []
    sw_base = anchor.get("_statewide", {}).get("reg")
    sw_cur = 0
    for key, a in sorted(anchor.items(), key=lambda kv: kv[1]["name"]):
        if key == "_statewide":
            continue
        cur = current.get(key)
        if cur is None:
            unmatched.append(a["name"])
        else:
            sw_cur += cur
        delta = (cur - a["reg"]) if cur is not None else None
        evs = [e for e in feed_events if e["county"] == key]
        counties.append({
            "name": a["name"],
            "precincts": a["precincts"],
            "baseline_reg": a["reg"],
            "baseline_suspense": a["suspense"],
            "baseline_nonsuspense": a["nonsuspense"],
            "current_reg": cur,
            "delta": delta,
            "pct": round(100 * delta / a["reg"], 4) if (delta and a["reg"]) else 0,
            "changed": bool(delta) or bool(evs),
            "last_change_ts": evs[-1]["ts"] if evs else None,
            "series": series.get(key, []),
            "events": evs,
        })

    n_changed = sum(1 for c in counties if c["changed"])
    data = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "baseline_label": VR_LABEL,
        "baseline_source": VR_URL,
        "statewide": {"baseline_reg": sw_base, "current_reg": sw_cur,
                      "delta": (sw_cur - sw_base) if sw_base else None,
                      "baseline_suspense": anchor.get("_statewide", {}).get("suspense")},
        "n_counties": len(counties),
        "n_changed": n_changed,
        "events": sorted(feed_events, key=lambda e: e["ts"]),
        "counties": counties,
    }
    OUT.write_text(json.dumps(data, indent=1))
    print(f"  written {OUT}  (counties changed: {n_changed}; unmatched: {len(unmatched)})")
    if unmatched:
        print("    unmatched county names:", ", ".join(unmatched[:10]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
