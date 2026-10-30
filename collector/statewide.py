#!/usr/bin/env python3
"""Statewide early-vote counts for all 254 Texas counties, straight from the SOS Civix turnout
feed (election 53815) we already archive. Aggregate counts only.

The voter-by-voter reconciliation (The Reconciler) runs only for the counties we have a roster
parser for; every other county still gets its SOS early-vote totals here, so the counter covers
the whole state. Writes frontend/dashboard/statewide_ev.json.

Usage: python3 collector/statewide.py
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "frontend" / "dashboard" / "statewide_ev.json"
MATCHED = {"harris", "dallas", "bexar", "travis", "hidalgo", "el-paso", "cameron", "hays"}


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")


def main() -> int:
    files = sorted(glob.glob(str(RAW / "sos" / "*" / "*turnout_53815_*.json")))
    if not files:
        raise SystemExit("no SOS turnout captures found")
    d = json.load(open(files[-1]))  # newest capture: cumulative totals are election-level
    counties = []
    sw_reg = sw_ip = sw_mail = 0
    reporting = 0
    for r in d.get("turnout_by_county", []):
        name = r["name"].strip()
        if name.upper().startswith(("STATE", "STATEWIDE")):
            continue
        reg = r.get("registered_voters") or 0
        ip = r.get("total_in_person_votes_for_election") or 0
        ml = r.get("total_mail_votes_for_election") or 0
        slug = slugify(name)
        tot = ip + ml
        sw_reg += reg; sw_ip += ip; sw_mail += ml
        if tot > 0:
            reporting += 1
        counties.append({
            "slug": slug, "name": name.title(),
            "registered": reg, "in_person": ip, "mail": ml, "total": tot,
            "turnout_pct": round(100 * tot / reg, 2) if reg else None,
            "matched": slug in MATCHED,
        })
    counties.sort(key=lambda c: c["total"], reverse=True)
    data = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "as_of": d.get("date_updated"),
        "election": "November 3, 2026 General Election (SOS election 53815)",
        "statewide": {
            "registered": sw_reg, "in_person": sw_ip, "mail": sw_mail, "total": sw_ip + sw_mail,
            "turnout_pct": round(100 * (sw_ip + sw_mail) / sw_reg, 2) if sw_reg else None,
            "counties": len(counties), "counties_reporting": reporting,
            "matched": len(MATCHED),
        },
        "counties": counties,
    }
    OUT.write_text(json.dumps(data, indent=1))
    sw = data["statewide"]
    print(f"written {OUT}  ({sw['counties']} counties, {sw['total']:,} early votes, {sw['turnout_pct']}% turnout)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
