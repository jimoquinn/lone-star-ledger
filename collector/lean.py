#!/usr/bin/env python3
"""Party lean of VUID lists, estimated from primary voting history.

Texas has no party registration; the usual proxy is which party's primary a voter chose.
Step 1 (`build`): read the county's historical primary rosters in data/history/<county>/ and
write data/history/<county>/primary_history.csv (vuid,election,party; mode 0600). Harris is
parsed from the files history.py downloads; any county can instead drop a CSV with those three
columns (e.g. converted from the SOS statewide voter file) and skip the build.
Step 2 (`apply`): for a match run (data/match/<ts>/), classify each list's VUIDs as D, R, mixed
or no-primary-history, and print counts only, next to the same split for the whole SOS roster
and the whole county roster so a lean can be read against its baseline.

Usage:
  python3 collector/lean.py build harris
  python3 collector/lean.py apply harris [--match data/match/<ts>]
"""
from __future__ import annotations

import argparse
import collections
import csv
import glob
import io
import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import match  # noqa: E402  (sos_rosters, COUNTY_PARSERS)

ROOT = Path(__file__).resolve().parent.parent
HIST = ROOT / "data" / "history"
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


# ---------------------------------------------------------------- streaming xlsx
def col_index(ref: str) -> int:
    n = 0
    for ch in ref:
        if ch.isalpha():
            n = n * 26 + ord(ch.upper()) - 64
        else:
            break
    return n - 1


def xlsx_stream(path: str, want: tuple[str, ...]):
    """Yield dicts {header: value} for the wanted headers, streaming sheet1 (handles 1 GB sheets)."""
    z = zipfile.ZipFile(path)
    sst = []
    if "xl/sharedStrings.xml" in z.namelist():
        with z.open("xl/sharedStrings.xml") as fh:
            for ev, el in ET.iterparse(fh, events=("end",)):
                if el.tag == NS + "si":
                    sst.append("".join(t.text or "" for t in el.iter(NS + "t"))); el.clear()
    header: dict[int, str] = {}
    want_idx: dict[int, str] = {}
    with z.open("xl/worksheets/sheet1.xml") as fh:
        for ev, el in ET.iterparse(fh, events=("end",)):
            if el.tag != NS + "row":
                continue
            cells = {}
            for c in el.findall(NS + "c"):
                i = col_index(c.get("r", ""))
                t = c.get("t")
                if t == "inlineStr":
                    v = "".join(x.text or "" for x in c.iter(NS + "t"))
                else:
                    v = c.findtext(NS + "v") or ""
                    if t == "s" and v:
                        v = sst[int(v)]
                cells[i] = v
            el.clear()
            if not header:
                header = {i: v.strip() for i, v in cells.items()}
                want_idx = {i: h for i, h in header.items() if h in want}
                continue
            yield {h: cells.get(i, "") for i, h in want_idx.items()}


# ---------------------------------------------------------------- build
HARRIS_FILES = [  # (election label, path glob, vuid column, party column, reader)
    ("2020-03P", "Cumulative_EV_0320.zip", "StateVoterID", "Party", "csvzip"),
    ("2020-03P", "Cumulative_BBM_0320.zip", "StateVoterID", "Party", "csvzip"),
    ("2022-03P", "0322_VoterHistory_BothParties.xlsx", "VUID", "PARTY_CODE", "xlsx"),
    ("2022-05R", "0522_VoterHistory_BothParties.xlsx", "VUID", "PARTY_CODE", "xlsx"),
    ("2024-03P", "x/0324_VoterHistory.xlsx", "VUID", "PARTY_CODE", "xlsx"),
    ("2024-05R", "0524_VoterHistory_Both_Parties.xlsx", "VUID", "PARTY_CODE", "xlsx"),
    ("2026-03P", "x/0326_OfficialRoster_*.xlsx", "StateVoterID", "Party", "xlsx"),
    ("2026-05R", "x/0526_OfficialRoster_*.xlsx", "StateVoterID", "Party", "xlsx"),
]


HARRIS_GENERAL = [  # official general-election rosters (no party): proves a VUID voted in Harris before
    ("2022-11G", "g/1122_VoterHistory*.xlsx", "VUID"),
    ("2024-11G", "g/1124_VoterHistory*.xlsx", "VUID"),
    ("2025-11G", "g/1125_VoterHistory*.xlsx", "VUID"),
]


def build_harris_general(out: Path) -> None:
    d = HIST / "harris"
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n"); w.writerow(["vuid", "election"])
        for label, pat, vcol in HARRIS_GENERAL:
            for f in sorted(glob.glob(str(d / pat))):
                n = 0
                for r in xlsx_stream(f, (vcol,)):
                    v = (r.get(vcol) or "").strip()
                    if re.fullmatch(r"\d{8,11}", v):
                        w.writerow([v, label]); n += 1
                print(f"  {label} {Path(f).name}: {n:,} rows", flush=True)


def build_harris(out: Path) -> None:
    d = HIST / "harris"
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n"); w.writerow(["vuid", "election", "party"])
        for label, pat, vcol, pcol, kind in HARRIS_FILES:
            for f in sorted(glob.glob(str(d / pat))):
                n = 0
                if kind == "csvzip":
                    with zipfile.ZipFile(f) as z:
                        name = next(m for m in z.namelist() if m.lower().endswith(".csv"))
                        rows = csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8", errors="replace", newline=""))
                        for r in rows:
                            v, p = (r.get(vcol) or "").strip(), (r.get(pcol) or "").strip().upper()[:1]
                            if re.fullmatch(r"\d{8,11}", v) and p in ("D", "R"):
                                w.writerow([v, label, p]); n += 1
                else:
                    for r in xlsx_stream(f, (vcol, pcol)):
                        v, p = (r.get(vcol) or "").strip(), (r.get(pcol) or "").strip().upper()[:1]
                        if re.fullmatch(r"\d{8,11}", v) and p in ("D", "R"):
                            w.writerow([v, label, p]); n += 1
                print(f"  {label} {Path(f).name}: {n:,} rows", flush=True)


# ---------------------------------------------------------------- apply
def load_history(county: str) -> dict:
    """vuid -> {election: party}"""
    p = HIST / county / "primary_history.csv"
    h: dict = collections.defaultdict(dict)
    if not p.exists():
        return h
    with open(p, newline="") as fh:
        for r in csv.DictReader(fh):
            h[r["vuid"]][r["election"]] = r["party"]
    return h


def load_general(county: str) -> set:
    p = HIST / county / "general_history.csv"
    if not p.exists():
        return set()
    with open(p, newline="") as fh:
        return {r["vuid"] for r in csv.DictReader(fh)}


def lean(hist: dict) -> str:
    if not hist:
        return "no-primary"
    parties = set(hist.values())
    if parties == {"D"}:
        return "D"
    if parties == {"R"}:
        return "R"
    latest = hist[max(hist)]  # most recent election decides a mixed record
    return f"mixed->{latest}"


def split(vuids, history: dict, general: set = frozenset()) -> dict:
    vuids = list(vuids)
    c = collections.Counter(lean(history.get(v, {})) for v in vuids)
    n = sum(c.values()) or 1
    out = {k: c[k] for k in ("D", "R", "mixed->D", "mixed->R", "no-primary") if c[k]}
    if general:
        never = sum(1 for v in vuids if v not in history and v not in general)
        out["never_voted_any_recorded_election"] = never
        out["never_share"] = round(never / n, 3)
    d, r = c["D"] + c["mixed->D"], c["R"] + c["mixed->R"]
    out["D_share_of_known"] = round(d / (d + r), 3) if d + r else None
    out["known_share"] = round((d + r) / n, 3)
    out["n"] = n
    return out


def apply(county: str, match_dir: Path) -> dict:
    history = load_history(county)
    general = load_general(county)
    if not history:
        raise SystemExit(f"no primary history for {county}: run `lean.py build {county}` or drop data/history/{county}/primary_history.csv")
    res = {"county": county, "match": match_dir.name, "history_vuids": len(history)}
    sos, _ = match.sos_rosters(county)
    cty = match.COUNTY_PARSERS[county]()
    res["baseline_sos_roster"] = split(sos, history, general)
    res["baseline_county_roster"] = split(cty, history, general)
    for label in ("sos_only", "county_only"):
        p = match_dir / f"{county}_{label}.csv"
        if not p.exists():
            continue
        rows = list(csv.DictReader(open(p, newline="")))
        res[label] = split([r["vuid"] for r in rows], history, general)
        for meth in ("in-person", "mail"):
            sub = [r["vuid"] for r in rows if r["method"] == meth]
            if sub:
                res[f"{label}_{meth}"] = split(sub, history, general)
        # exclude the latest SOS date (still filling) from sos_only in-person, which is the real question
        if label == "sos_only":
            dates = sorted({r["date"] for r in rows if r["date"]})
            if len(dates) > 1:
                sub = [r["vuid"] for r in rows if r["method"] == "in-person" and r["date"] != dates[-1]]
                if sub:
                    res["sos_only_in-person_completed_days"] = split(sub, history, general)
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "build-general", "apply"])
    ap.add_argument("county")
    ap.add_argument("--match", default="")
    a = ap.parse_args()
    if a.cmd == "build":
        if a.county != "harris":
            raise SystemExit("build is implemented for harris; other counties: drop primary_history.csv (vuid,election,party)")
        out = HIST / "harris" / "primary_history.csv"
        build_harris(out)
        print(f"written {out}")
        return 0
    if a.cmd == "build-general":
        out = HIST / "harris" / "general_history.csv"
        build_harris_general(out)
        print(f"written {out}")
        return 0
    md = Path(a.match) if a.match else Path(sorted(glob.glob(str(ROOT / "data" / "match" / "*")))[-1])
    res = apply(a.county, md)
    import json
    print(json.dumps(res, indent=1))
    out = md / f"{a.county}_lean.json"
    out.write_text(json.dumps(res, indent=1)); os.chmod(out, 0o644)
    return 0


if __name__ == "__main__":
    sys.exit(main())
