#!/usr/bin/env python3
"""Mail-ballot acceptance gap: ballots each county reports RECEIVED vs. ballots the Secretary
of State reports COUNTED, over time. Aggregate counts only, no voter-level data.

This is NOT a rejection rate. The gap (received - counted) is "received but not yet counted":
during voting it is dominated by processing lag and pending SB1 cures, it can go negative when
the SOS counts a ballot our county file hasn't captured yet, and the true rejection figure is
only known after each county's ballot board and the canvass. Those canvass numbers will be
layered in here after the election. For now this tracks the gap honestly and its trajectory.

Sources (both already archived):
  * Received: each county's own mail roster count, from data/match/<ts>/summary.json
    (county_by_method.mail). Only the counties we match on voter ID appear.
  * Counted: the SOS Civix turnout feed's total_mail_votes_for_election (data/raw/sos) - the
    authoritative accepted-mail count (the SOS *roster* undercounts it, so we use turnout).

Hidalgo is shown but not comparable: its mail file is applications for a ballot (ABBM), not
returned ballots. Hays is flagged "sampled" - it's pulled only a few times a day.

Usage: python3 collector/mail_gap.py
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "frontend" / "dashboard" / "mail_gap.json"

ORDER = ["harris", "dallas", "bexar", "collin", "denton", "travis", "hidalgo", "el-paso", "cameron", "hays", "tarrant"]
NICE = {"harris": "Harris", "dallas": "Dallas", "bexar": "Bexar", "travis": "Travis",
        "hidalgo": "Hidalgo", "el-paso": "El Paso", "cameron": "Cameron", "hays": "Hays",
        "tarrant": "Tarrant", "collin": "Collin", "denton": "Denton"}
# Short, plain basis for each county's RECEIVED figure (mail side).
BASIS = {"harris": "ballots returned (BBM roster)", "dallas": "ballots returned (return date)",
         "bexar": "ballots received (daily roster)", "travis": "ballots received (daily roster)",
         "el-paso": "ballots received", "cameron": "ballots received",
         "hays": "absentee ballots received", "hidalgo": "applications for a ballot — not returned",
         "tarrant": "ballots returned (return date)", "collin": "ballots received (received date)",
         "denton": "ballots returned (return date)"}
NOT_COMPARABLE = {"hidalgo"}   # mail file is applications, not returned ballots
SAMPLED = {"hays"}             # pulled a few times a day, not every 15 min


def norm(s: str) -> str:
    return re.sub(r"[^A-Z]", "", (s or "").upper())


def parse_ts(s: str) -> dt.datetime | None:
    for fmt in ("%Y-%m-%dT%H%M%SZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone.utc)
        except (ValueError, TypeError):
            pass
    return None


def turnout_counted_series() -> dict:
    """slug-norm -> sorted list of (datetime, counted_mail) change-points."""
    pts: dict[str, list] = {}
    for f in sorted(glob.glob(str(RAW / "sos" / "*" / "*turnout_53815_*.json"))):
        m = re.search(r"/sos/(\d{4}-\d\d-\d\d)/(\d\d)(\d\d)(\d\d)_", f)
        if not m:
            continue
        ts = dt.datetime(int(m[1][:4]), int(m[1][5:7]), int(m[1][8:10]),
                         int(m[2]), int(m[3]), int(m[4]), tzinfo=dt.timezone.utc)
        try:
            d = json.load(open(f))
        except Exception:
            continue
        for r in d.get("turnout_by_county", []):
            k = norm(r.get("name", ""))
            c = r.get("total_mail_votes_for_election")
            if c is None:
                continue
            lst = pts.setdefault(k, [])
            if not lst or lst[-1][1] != c:
                lst.append((ts, c))
    for k in pts:
        pts[k].sort()
    return pts


def counted_at(series: list, when: dt.datetime):
    """Latest counted value at or before `when`."""
    val = None
    for ts, c in series:
        if ts <= when:
            val = c
        else:
            break
    return val


def main() -> int:
    print("mail acceptance gap")
    counted = turnout_counted_series()

    # received per county over time, from match summaries (chronological)
    rec: dict[str, list] = {c: [] for c in ORDER}  # slug -> [(datetime, received)]
    for p in sorted(glob.glob(str(ROOT / "data" / "match" / "*" / "summary.json"))):
        try:
            d = json.load(open(p))
        except Exception:
            continue
        when = parse_ts(d.get("ts", "")) or parse_ts(Path(p).parent.name)
        if not when:
            continue
        for r in d.get("results", []):
            slug = r["county"]
            if slug not in rec:
                continue
            mail = (r.get("county_by_method") or {}).get("mail")
            if mail is not None:
                rec[slug].append((when, mail))

    counties = []
    sw_r = sw_c = 0
    for slug in ORDER:
        pts = rec[slug]
        if not pts:
            continue
        cser = counted.get(norm(slug.replace("-", " ")), [])
        # daily series: last observation per calendar day
        byday: dict[str, dict] = {}
        for when, received in pts:
            cnt = counted_at(cser, when)
            byday[when.strftime("%Y-%m-%d")] = {
                "date": when.strftime("%Y-%m-%d"),
                "received": received,
                "counted": cnt,
                "gap": (received - cnt) if cnt is not None else None,
            }
        series = [byday[k] for k in sorted(byday)]
        cur = series[-1]
        comparable = slug not in NOT_COMPARABLE
        if comparable and cur["counted"] is not None:
            sw_r += cur["received"]
            sw_c += cur["counted"]
        counties.append({
            "slug": slug, "name": NICE[slug], "basis": BASIS[slug],
            "comparable": comparable, "sampled": slug in SAMPLED,
            "received": cur["received"], "counted": cur["counted"], "gap": cur["gap"],
            "gap_pct": round(100 * cur["gap"] / cur["received"], 1) if (cur["gap"] is not None and cur["received"]) else None,
            "series": series,
        })

    data = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "label": "Mail ballots reported RECEIVED by each county vs. COUNTED by the Secretary of State (election 53815). The gap is received-but-not-yet-counted, not a rejection rate.",
        "statewide": {"received": sw_r, "counted": sw_c, "gap": sw_r - sw_c,
                      "gap_pct": round(100 * (sw_r - sw_c) / sw_r, 1) if sw_r else None},
        "counties": counties,
    }
    OUT.write_text(json.dumps(data, indent=1))
    print(f"  written {OUT}  ({len(counties)} counties; comparable statewide gap {sw_r-sw_c:+,} of {sw_r:,})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
