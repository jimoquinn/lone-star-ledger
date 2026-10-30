#!/usr/bin/env python3
"""Assemble aggregate-only JSON for the public dashboard from the latest SOS turnout, the VUID
match, the party-lean and the registration check. COUNTS ONLY - no names, no VUIDs. Writes
frontend/dashboard/data.json.
Usage: python3 collector/dashboard_data.py [--match data/match/<ts>]
"""
import argparse, glob, json, re, datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
ORDER = ["harris", "dallas", "bexar", "travis", "hidalgo", "el-paso", "cameron", "hays"]
NICE = {"harris": "Harris", "dallas": "Dallas", "bexar": "Bexar", "travis": "Travis",
        "hidalgo": "Hidalgo", "el-paso": "El Paso", "cameron": "Cameron", "hays": "Hays"}


def source_last_changed():
    """Max timestamp of a *changed* capture per source, from the collector manifest. Lets the
    dashboard footer show when each source last actually moved (304 revalidations don't count)."""
    last = {}
    mf = RAW / "manifest.jsonl"
    if mf.exists():
        with open(mf, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if not r.get("changed"):
                    continue
                s, ts = r.get("source"), r.get("ts")
                if s and ts and ts > last.get(s, ""):
                    last[s] = ts
    return last


def latest_sos_turnout():
    files = sorted(glob.glob(str(RAW / "sos" / "*" / "*_turnout_53815_*.json")))
    by_date = {}
    for f in files:
        m = re.search(r"turnout_53815_(\d{4}-\d\d-\d\d)\.json$", f)
        if m:
            by_date[m[1]] = f  # later capture (sorted) wins per date
    if not by_date:
        return None, None
    d = max(by_date)
    return d, json.load(open(by_date[d]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--match", default="")
    a = ap.parse_args()
    if a.match:
        md = Path(a.match)
    else:
        # Newest match dir that has a summary.json — skip any dir a concurrent match run is
        # still writing (the hourly cron can be mid-run when deploy fires).
        done = [p for p in sorted(glob.glob(str(ROOT / "data" / "match" / "*"))) if (Path(p) / "summary.json").exists()]
        if not done:
            raise SystemExit("no completed match dir (with summary.json) found")
        md = Path(done[-1])
    summary = json.load(open(md / "summary.json"))
    mres = {r["county"]: r for r in summary["results"]}

    sos_date, sos = latest_sos_turnout()
    sos_by = {}
    if sos:
        for r in sos.get("turnout_by_county", []):
            sos_by[r["name"].strip().lower().replace(" ", "-")] = r

    counties = []
    for c in ORDER:
        s = sos_by.get(c, {})
        m = mres.get(c, {})
        lean = None
        lf = md / f"{c}_lean.json"
        if lf.exists():
            lj = json.load(open(lf))
            b = lj.get("baseline_county_roster") or lj.get("baseline_sos_roster") or {}
            lean = {
                "d_share_of_known": b.get("D_share_of_known"),
                "known_share": b.get("known_share"),
                "no_primary": b.get("no-primary"),
                "never_voted": b.get("never_voted_any_recorded_election"),
                "n": b.get("n"),
            }
        reg = None
        rf = md / f"{c}_regcheck.json"
        if rf.exists():
            rj = json.load(open(rf))
            g = rj.get("sos_only_in-person_completed_days") or {}
            reg = {"not_in_registration_file": g.get("not_in_registration_file"),
                   "registered_2026": g.get("registered_2026"),
                   "suspense": (g.get("by_status") or {}).get("SUSPENSE"),
                   "n": g.get("n")}
        counties.append({
            "slug": c, "name": NICE[c],
            "registered": s.get("registered_voters"),
            "sos_in_person": s.get("total_in_person_votes_for_election"),
            "sos_mail": s.get("total_mail_votes_for_election"),
            "sos_total": (s.get("total_in_person_votes_for_election") or 0) + (s.get("total_mail_votes_for_election") or 0),
            "roster_county": m.get("county_vuids"),
            "roster_sos": m.get("sos_vuids"),
            "both": m.get("both"),
            "sos_only": m.get("sos_only"),
            "county_only": m.get("county_only"),
            "definition": m.get("definition"),
            "has_county_roster": bool(m.get("county_vuids")),
            "lean": lean, "regcheck": reg,
        })

    lc = source_last_changed()
    names = {"sos": "Secretary of State", **NICE}
    sources = [{"key": k, "name": names[k], "last_changed": lc[k]}
               for k in ["sos"] + ORDER if lc.get(k)]

    data = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "sos_as_of": sos_date,
        "election": "November 3, 2026 General Election (SOS election 53815)",
        "counties": counties,
        "sources": sources,
    }
    out = ROOT / "frontend" / "dashboard" / "data.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=1))
    print(f"written {out}")
    print(json.dumps(data, indent=1))


if __name__ == "__main__":
    main()
