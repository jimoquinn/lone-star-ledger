#!/usr/bin/env python3
"""Convert a Travis County voter-history CSV (Registered_Voters_With_History.csv: per-election
'*VOTED' columns, primaries holding DEM/REP) into the two files lean.py reads:
  data/history/travis/primary_history.csv  vuid,election,party   (party from P**/PR** columns)
  data/history/travis/general_history.csv  vuid                  (voted in ANY election 2016-present)
Both 0600, git-ignored. Primary columns like P26VOTED/PR24VOTED give the party; every *VOTED column
being non-empty marks the voter as having a Travis voting record at all.
Usage: python3 collector/load_travis_history.py [path-to-history.csv]
"""
import csv, os, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data-test" / "Registered_Voters_With_History.csv"
OUT = ROOT / "data" / "history" / "travis"
OUT.mkdir(parents=True, exist_ok=True); os.chmod(OUT, 0o700)

PRIMARY = re.compile(r"^(PR?)(\d\d)VOTED$")   # P16VOTED, PR24VOTED ...
VOTEDCOL = re.compile(r"VOTED$")
PARTY = {"DEM": "D", "REP": "R", "D": "D", "R": "R"}


def main() -> int:
    ph = OUT / "primary_history.csv"; gh = OUT / "general_history.csv"
    pfd = os.open(ph, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    gfd = os.open(gh, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    npri = ngen = nrow = 0
    with open(SRC, encoding="utf-8", errors="replace", newline="") as fh, \
         os.fdopen(pfd, "w", newline="") as pf, os.fdopen(gfd, "w", newline="") as gf:
        r = csv.DictReader(fh)
        prim_cols = [c for c in r.fieldnames if PRIMARY.match(c or "")]
        voted_cols = [c for c in r.fieldnames if VOTEDCOL.search(c or "")]
        pw = csv.writer(pf, lineterminator="\n"); pw.writerow(["vuid", "election", "party"])
        gw = csv.writer(gf, lineterminator="\n"); gw.writerow(["vuid"])
        for row in r:
            nrow += 1
            v = (row.get("VUID") or "").strip()
            if not re.fullmatch(r"\d{8,11}", v):
                continue
            for c in prim_cols:
                p = PARTY.get((row.get(c) or "").strip().upper()[:3].strip())
                if p:
                    m = PRIMARY.match(c)
                    label = f"20{m[2]}-{m[1]}"  # 2026-P, 2024-PR ...
                    pw.writerow([v, label, p]); npri += 1
            if any((row.get(c) or "").strip() for c in voted_cols):
                gw.writerow([v]); ngen += 1
    print(f"rows={nrow:,}  primary_rows={npri:,} -> {ph}")
    print(f"ever-voted vuids={ngen:,} -> {gh}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
