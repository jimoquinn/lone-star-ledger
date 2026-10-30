#!/usr/bin/env python3
"""Daily per-county series for the gap small-multiples and the SOS revision timeline.
Aggregate counts only. Writes frontend/dashboard/series.json.

- SOS cumulative per (county, EV date): latest captured value of total_in_person / total_mail
  in that date's turnout file.
- SOS revisions: for each (county, metric, EV date), the distinct values seen across captures,
  with the UTC time each was first seen. This is the "votes change after the fact" view.
- County cumulative per (county, EV date): from each county's own roster, bucketing the
  per-voter vote dates (where the roster carries them) and cumulating along the date axis.
  Counties whose roster lacks usable dates for a metric get null for that line.
"""
import datetime as dt, glob, json, re, collections
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import match  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
EV_START = "2026-10-19"
NICE = match.NICE if hasattr(match, "NICE") else None
ORDER = ["harris", "dallas", "bexar", "travis", "hidalgo", "el-paso", "cameron", "hays", "tarrant", "collin", "denton", "randall"]


def sos_captures():
    """Return {ev_date: [(utc_iso, {county: (ip, mail)}), ...]} ordered by capture time."""
    out = collections.defaultdict(list)
    for f in sorted(glob.glob(str(RAW / "sos" / "*" / "*_turnout_53815_*.json"))):
        m = re.search(r"/(\d{4}-\d\d-\d\d)/(\d{6})_[0-9a-f]+_turnout_53815_(\d{4}-\d\d-\d\d)\.json$", f)
        if not m:
            continue
        cap_day, hhmmss, ev_date = m[1], m[2], m[3]
        ts = f"{cap_day}T{hhmmss[:2]}:{hhmmss[2:4]}:{hhmmss[4:]}Z"
        try:
            d = json.load(open(f))
        except Exception:
            continue
        by = {}
        for r in d.get("turnout_by_county", []):
            slug = r["name"].strip().lower().replace(" ", "-")
            by[slug] = (r.get("total_in_person_votes_for_election") or 0, r.get("total_mail_votes_for_election") or 0)
        out[ev_date].append((ts, by))
    for k in out:
        out[k].sort()
    return out


def county_daily(slug, dates):
    """Return (ipCum|None, mailCum|None) aligned to `dates`, from the county's own roster."""
    try:
        recs = match.COUNTY_PARSERS[slug]()  # {vuid: (date, method)}
    except Exception:
        return None, None
    if not recs:
        return None, None
    day_ip = collections.Counter()
    day_mail = collections.Counter()
    tot = {"in-person": 0, "mail": 0}
    dated = {"in-person": 0, "mail": 0}
    for v, (d, meth) in recs.items():
        tot[meth] = tot.get(meth, 0) + 1
        if re.fullmatch(r"\d{4}-\d\d-\d\d", d or ""):
            dated[meth] += 1
            (day_ip if meth == "in-person" else day_mail)[d] += 1
    def cum(day, method):
        if tot.get(method, 0) == 0 or dated[method] < 0.5 * tot[method]:
            return None  # not enough usable dates; show SOS only
        out, run = [], 0
        # fold any pre-axis dates into the first bucket
        pre = sum(n for dd, n in day.items() if dd < dates[0])
        run = pre
        for i, D in enumerate(dates):
            run += day.get(D, 0) if i > 0 or True else 0
            out.append(run)
        # recompute cleanly: cumulative of (<=D)
        out, run = [], pre
        seen = set()
        for D in dates:
            run += day.get(D, 0)
            out.append(run)
        return out
    return cum(day_ip, "in-person"), cum(day_mail, "mail")


def main():
    caps = sos_captures()
    dates = sorted(caps)
    counties = {}
    revisions = []
    for slug in ORDER:
        ipS, mailS = [], []
        for D in dates:
            seq = caps.get(D, [])
            last = seq[-1][1].get(slug) if seq else None
            ipS.append(last[0] if last else 0)
            mailS.append(last[1] if last else 0)
            # revisions for this (county, metric, date)
            for mi, mname in ((0, "ip"), (1, "mail")):
                hist = []
                for ts, by in seq:
                    val = by.get(slug)
                    if not val:
                        continue
                    v = val[mi]
                    if not hist or hist[-1][1] != v:
                        hist.append([ts, v])
                if len(hist) >= 2:
                    revisions.append({"county": slug, "metric": mname, "evDate": D, "history": hist})
        ipC, mailC = county_daily(slug, dates)
        counties[slug] = {"ipCounty": ipC, "ipSOS": ipS, "mailCounty": mailC, "mailSOS": mailS}
    data = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "dates": dates, "evStart": EV_START, "order": ORDER,
        "counties": counties, "revisions": revisions,
    }
    out = ROOT / "frontend" / "dashboard" / "series.json"
    out.write_text(json.dumps(data, separators=(",", ":")))
    print(f"written {out} ({out.stat().st_size:,} bytes)")
    print("dates", dates[0], "..", dates[-1], "| revisions:", len(revisions))
    for slug in ORDER:
        c = counties[slug]
        print(f"  {slug}: ipCounty={'yes' if c['ipCounty'] else 'no':3} mailCounty={'yes' if c['mailCounty'] else 'no':3} "
              f"ipSOS_last={c['ipSOS'][-1]:>7,} mailSOS_last={c['mailSOS'][-1]:>6,}"
              + (f" ipCounty_last={c['ipCounty'][-1]:>7,}" if c['ipCounty'] else ""))


if __name__ == "__main__":
    main()
