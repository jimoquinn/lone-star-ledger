#!/usr/bin/env python3
"""Lead-chaser for the discrepancy lists. Joins a county's match VUID lists to the county
registration file (Registered_Voter_List.csv: VUID, Registration Date, Status, ...) and reports
COUNTS ONLY: how many of each list are registered, their status (active/suspense/cancelled),
and when they registered (this year vs earlier). This is how we tell an innocent explanation
(a brand-new registrant voting for the first time) from a real anomaly (a cancelled or
never-registered VUID the SOS says voted).

Usage: python3 collector/regcheck.py travis [--rvl data-test/Registered_Voter_List.csv] [--match data/match/<ts>]
Writes <county>_regcheck.json (counts only) into the match dir.
"""
import argparse, collections, csv, glob, json, os, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_rvl(path: str) -> dict:
    reg = {}
    with open(path, encoding="utf-8", errors="replace", newline="") as fh:
        r = csv.DictReader(fh)
        vcol = "VUID"
        dcol = next((c for c in r.fieldnames if c.strip().lower() == "registration date"), "Registration Date")
        scol = next((c for c in r.fieldnames if c.strip().lower() == "status"), "Status")
        for row in r:
            v = (row.get(vcol) or "").strip()
            if re.fullmatch(r"\d{8,11}", v):
                reg[v] = ((row.get(dcol) or "").strip(), (row.get(scol) or "").strip())
    return reg


def year_of(d: str) -> str:
    m = re.search(r"(\d{4})", d or "")
    if m:
        return m[1]
    m = re.match(r"\d{1,2}/\d{1,2}/(\d{2,4})", d or "")
    return m[1] if m else "?"


def classify(vuids, reg: dict) -> dict:
    status = collections.Counter()
    regyear = collections.Counter()
    not_registered = 0
    for v in vuids:
        if v not in reg:
            not_registered += 1
            continue
        d, s = reg[v]
        status[s or "(blank)"] += 1
        regyear[year_of(d)] += 1
    n = len(vuids) or 1
    return {
        "n": len(vuids),
        "not_in_registration_file": not_registered,
        "by_status": dict(status.most_common()),
        "registered_2026": regyear.get("2026", 0),
        "registered_2025_or_earlier": sum(c for y, c in regyear.items() if y.isdigit() and int(y) <= 2025),
        "reg_year_top": dict(regyear.most_common(6)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("county")
    ap.add_argument("--rvl", default=str(ROOT / "data-test" / "Registered_Voter_List.csv"))
    ap.add_argument("--match", default="")
    a = ap.parse_args()
    md = Path(a.match) if a.match else Path(sorted(glob.glob(str(ROOT / "data" / "match" / "*")))[-1])
    print(f"loading registration file {a.rvl} ...", flush=True)
    reg = load_rvl(a.rvl)
    print(f"  {len(reg):,} registered voters", flush=True)
    out = {"county": a.county, "match": md.name, "registered_total": len(reg)}
    for label in ("sos_only", "county_only"):
        p = md / f"{a.county}_{label}.csv"
        if not p.exists():
            continue
        rows = list(csv.DictReader(open(p, newline="")))
        out[label] = classify([r["vuid"] for r in rows], reg)
        ip = [r for r in rows if r["method"] == "in-person" and r["date"]]
        dates = sorted({r["date"] for r in ip})
        if label == "sos_only" and len(dates) > 1:
            sub = [r["vuid"] for r in ip if r["date"] != dates[-1]]
            out["sos_only_in-person_completed_days"] = classify(sub, reg)
    dest = md / f"{a.county}_regcheck.json"
    dest.write_text(json.dumps(out, indent=1)); os.chmod(dest, 0o644)
    print(json.dumps(out, indent=1))
    print(f"written {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
