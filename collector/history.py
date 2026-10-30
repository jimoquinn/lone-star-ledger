#!/usr/bin/env python3
"""One-off download of historical primary rosters (party-labeled voter history) used to estimate
party lean of VUIDs. Files land in data/history/<county>/ mode 0600, git-ignored (PII).
Usage: python3 collector/history.py [--only harris]
Only Harris publishes these for free (app.harrisvotes.com/ElectionResults/ElectionRosters).
For the other counties use the SOS statewide voter file with history, loaded by lean.py.
"""
import argparse, os, subprocess, sys, urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UA = "Mozilla/5.0 (X11; Linux x86_64) TXEarlyVoteTracker/0.1 (+public-interest research)"
H = "https://appfiles.harrisvotes.com/harrisvotes/prd/Reports/"
FILES = {
    "harris": [  # (election, url)
        ("2026-03 primary", H + "0326_OfficialRoster.zip"),
        ("2026-05 primary runoff", H + "0526_OfficialRoster.zip"),
        ("2024-03 primary", H + "0324 Official Voter Rosters.zip"),
        ("2024-05 primary runoff", H + "0524_VoterHistory_Both Parties.xlsx"),
        ("2022-03 primary", H + "0322_VoterHistory_BothParties.xlsx"),
        ("2022-05 primary runoff", H + "0522_VoterHistory_BothParties.xlsx"),
        # General-election official rosters: no party, but they show whether a VUID has ever voted in Harris.
        ("2024-11 general official roster set 1", H + "1124_VoterHistory (Set 1 to 6).zip"),
        ("2024-11 general official roster set 2", H + "1124_VoterHistory (Set 7 to 11).zip"),
        ("2022-11 general official roster", H + "1122 Official Voter Rosters.zip"),
        ("2025-11 general official roster", H + "1125_Official Voter Roster.zip"),
        ("2020-03 primary EV (unofficial)", H + "Cumulative_EV_0320.zip"),
        ("2020-03 primary BBM (unofficial)", H + "Cumulative_BBM_0320.zip"),
    ],
}

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--only", default=""); a = ap.parse_args()
    for county, files in FILES.items():
        if a.only and county != a.only:
            continue
        d = ROOT / "data" / "history" / county; d.mkdir(parents=True, exist_ok=True); os.chmod(d, 0o700)
        for label, url in files:
            name = urllib.parse.unquote(url.rsplit("/", 1)[-1]).replace(" ", "_")
            out = d / name
            if out.exists() and out.stat().st_size > 0:
                print(f"= {county} {label}: {name} ({out.stat().st_size:,} bytes)"); continue
            tmp = out.with_suffix(out.suffix + ".part")
            r = subprocess.run(["curl", "-sS", "-L", "--fail", "-A", UA, "-o", str(tmp),
                                "-w", "%{http_code} %{size_download}", urllib.parse.quote(url, safe=":/?&=%+@;,$!*'()[]#~")],
                               capture_output=True, text=True)
            if r.returncode == 0 and tmp.exists():
                os.chmod(tmp, 0o600); tmp.rename(out)
                print(f"+ {county} {label}: {name} ({r.stdout})")
            else:
                print(f"! {county} {label}: {name} -> {r.stdout} {r.stderr.strip()[:120]}")
                tmp.unlink(missing_ok=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
