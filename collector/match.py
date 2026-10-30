#!/usr/bin/env python3
"""Roster match: which VUIDs the SOS says voted in a county vs. which VUIDs the county's own
roster lists. Answers "did the SOS add voters the county never reported?" (sos_only) and the
reverse (county_only). Aggregate counts go to stdout and data/match/<ts>/summary.{json,csv};
the VUID-level detail files are written mode 0600 under the same folder and are never
committed or served (PII rule in CLAUDE.md).

Usage: python3 collector/match.py [--only harris,travis] [--out data/match]

Definitions matter: the county side is each county's own list, labeled per county in
COUNTY_DEFS below (check-ins vs. ballots; mail returned vs. applications). Lag matters too:
the SOS roster for a day fills in over the following days, and counties post the day's roster
the next morning, so the by-date breakdowns separate "not posted yet" from "never reported".
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import glob
import io
import json
import os
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diff  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
COUNTIES = ["harris", "dallas", "bexar", "travis", "hidalgo", "el-paso", "cameron", "hays", "tarrant", "collin", "denton"]

COUNTY_DEFS = {
    "harris": "in-person: unofficial cumulative EV roster (check-ins); mail: unofficial BBM roster (ballots returned)",
    "dallas": "in-person: EV turnout details report (DATE VOTED); mail: mail ballot returns report (Return Date)",
    "travis": "in-person: daily Early Vote rosters in G26-Voter-Rosters.zip; mail: daily Ballot by Mail rosters (limited ballots have no VUID, not matched)",
    "hidalgo": "in-person: cumulative EV-Roster; mail: ABBM-LIST = applications for ballot by mail, not returned ballots",
    "el-paso": "in-person: cumulative Voter_List Ballot_Address DayN; mail: Received_Ballots_Voter_List (received)",
    "cameron": "in-person: 01_Cameron_County voter list (CHECK-IN date); mail: MAIL-GN26-Cameron-County-CC list (Ballot Received Date)",
    "bexar": "in-person: daily Early-Vote-Ballots roster (check-ins; no per-row date); mail: daily Received-Mail-Ballots roster (ballots received; elections.bexar.gov DocumentCenter folder 245)",
    "hays": "in-person: In-Person Check-ins searchable HTML, EARLY VOTED check-ins (item 5454, via the user's Chrome); mail: Absentee Ballots Received list (item 5414, received date)",
    "tarrant": "in-person: Early Voting in-person roster (SOS Voter ID; no per-row date); mail: Absentee returned-ballot roster (Return Date)",
    "comal": "in-person: daily IN PERSON roster (State ID = 10-digit VUID; no per-row date, file date = check-in date); mail: daily BBM RETD roster (ballots returned; comalcounty.gov DocumentCenter)",
    "collin": "in-person: cumulative early-voting roster (CertificateNumber = VUID, VoteDate per-row); mail: cumulative absentee-returns roster (VUID, ReceivedDate = ballots received); collincountytx.gov/elections/rosters",
    "denton": "in-person: cumulative Early-Voting-Roster (State ID = VUID, SOS-verified 99.8%; check-in Time only, no per-row vote date); mail: Returned_Mail_Roster (SOS_Vuid, Return Date = ballots returned); votedenton.gov/early-voting-election-day-rosters (via prod box)",
}

Rec = tuple  # (date 'YYYY-MM-DD' or '', method 'in-person'|'mail')

# Hays in-person check-ins (item 5454) embed their rows as JSON arrays in one <script>:
# ["VUID","Last","First","Prec","EARLY VOTED","2026-10-24","N"]. Capture VUID and the date.
HAYS_IP_RE = re.compile(r'\["(\d{8,11})",\s*"[^"]*",\s*"[^"]*",\s*"[^"]*",\s*"[^"]*",\s*"(\d{4}-\d\d-\d\d)"')


# ---------------------------------------------------------------- helpers
def latest(pattern: str) -> str | None:
    files = sorted(glob.glob(str(RAW / pattern)))
    return files[-1] if files else None


def excel_date(v: str) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", v or "")
        return f"{m[3]}-{int(m[1]):02d}-{int(m[2]):02d}" if m else ""
    return (dt.date(1899, 12, 30) + dt.timedelta(days=int(f))).isoformat()


def us_date(v: str) -> str:
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{2,4})", v or "")
    if not m:
        return ""
    y = int(m[3]); y = y + 2000 if y < 100 else y
    return f"{y}-{int(m[1]):02d}-{int(m[2]):02d}"


def vuid_ok(v: str) -> bool:
    return bool(re.fullmatch(r"\d{8,11}", (v or "").strip()))


def add(d: dict, vuid: str, date: str, method: str) -> None:
    v = (vuid or "").strip()
    if vuid_ok(v) and v not in d:
        d[v] = (date, method)


# ---------------------------------------------------------------- SOS side
def sos_rosters(county: str) -> tuple[dict, dict]:
    """vuid -> (date, method) over the latest version of every per-date SOS roster. Also per-date counts."""
    per_date: dict[str, str] = {}
    for f in sorted(glob.glob(str(RAW / "sos-roster" / "*" / f"*_roster_53815_{county}_*.csv"))):
        m = re.search(r"_(\d{4}-\d\d-\d\d)\.csv$", f)
        if m:
            per_date[m[1]] = f  # sorted by day dir then time, so the last one wins
    out: dict = {}
    counts = {}
    for date, f in sorted(per_date.items()):
        n = 0
        with open(f, encoding="utf-8", errors="replace", newline="") as fh:
            for r in csv.DictReader(fh):
                meth = "mail" if "MAIL" in (r.get("VOTING_METHOD") or "").upper() else "in-person"
                add(out, r.get("ID_VOTER", ""), date, meth)
                n += 1
        counts[date] = n
    return out, counts


# ---------------------------------------------------------------- county side
def county_harris() -> dict:
    out: dict = {}
    for z, meth in ((latest("harris/*/*_Cumulative_EV_ToPost_1126.zip"), "in-person"),
                    (latest("harris/*/*_Cumulative_BBM_ToPost_1126.zip"), "mail")):
        if not z:
            continue
        with zipfile.ZipFile(z) as zf:
            for name in zf.namelist():
                if not name.lower().endswith(".csv"):
                    continue
                for r in csv.DictReader(io.TextIOWrapper(zf.open(name), encoding="utf-8", errors="replace", newline="")):
                    add(out, r.get("StateVoterID", ""), us_date(r.get("ActivityDate", "")), meth)
    return out


def county_dallas() -> dict:
    out: dict = {}
    # Dallas renamed this report on 2026-10-28 from
    # "..._In-Person-Early-Voting-Turnout-Details-Report_MM.DD.YYYY.xlsx" to
    # "..._Early-Voting-Turnout-Details_MM.DD.YY.xlsx". Match both; the glob stays
    # narrow enough to exclude the separate "...Turnout-By-Date-Location..." file.
    f = latest("dallas/*/*Early-Voting-Turnout-Details*.xlsx")
    if f:
        rows = diff.xlsx_rows(Path(f).read_bytes())
        hdr = next((i for i, r in enumerate(rows[:5]) if "SOS VOTERID" in r), None)
        if hdr is not None:
            iv, idt = rows[hdr].index("SOS VOTERID"), rows[hdr].index("DATE VOTED")
            for r in rows[hdr + 1:]:
                if len(r) > max(iv, idt):
                    add(out, r[iv], excel_date(r[idt]), "in-person")
    f = latest("dallas/*/*_Mail-Ballot-Returns-Report_*.xlsx")
    if f:
        rows = diff.xlsx_rows(Path(f).read_bytes())
        hdr = next((i for i, r in enumerate(rows[:5]) if "State Voter ID" in r), None)
        if hdr is not None:
            iv, idt = rows[hdr].index("State Voter ID"), rows[hdr].index("Return Date")
            for r in rows[hdr + 1:]:
                if len(r) > max(iv, idt) and r[idt]:
                    add(out, r[iv], excel_date(r[idt]), "mail")
    return out


def county_travis() -> dict:
    out: dict = {}
    z = latest("travis/*/*_G26-Voter-Rosters.zip")
    if not z:
        return out
    with zipfile.ZipFile(z) as zf:
        for name in sorted(zf.namelist()):
            m = re.match(r"(\d\d)\.(\d\d)\.(\d{4}) (Early Vote|Ballot by Mail)", name)
            if not m:
                continue
            date = f"{m[3]}-{m[1]}-{m[2]}"
            meth = "in-person" if m[4] == "Early Vote" else "mail"
            rows = diff.xlsx_rows(zf.read(name))
            for r in rows:
                if r and vuid_ok(r[0]):
                    add(out, r[0], date, meth)
    return out


def county_hidalgo() -> dict:
    out: dict = {}
    f = latest("hidalgo/*/*_EV-Roster-November-3-2026-Cumulative.*")
    if f:
        with open(f, encoding="utf-8-sig", errors="replace", newline="") as fh:
            for r in csv.DictReader(fh):
                add(out, r.get("VUID", ""), "", "in-person")
    f = latest("hidalgo/*/*_ABBM-LIST-November-3-2026-Cumulative.xlsx")
    if f:
        rows = diff.xlsx_rows(Path(f).read_bytes())
        iv = rows[0].index("VUID") if rows and "VUID" in rows[0] else 1
        for r in rows[1:]:
            if len(r) > iv:
                add(out, r[iv], "", "mail")
    return out


def county_el_paso() -> dict:
    out: dict = {}
    f = latest("el-paso/*/*_Voter_List__Ballot_Address_Day*.xlsx")
    if f:
        for r in diff.xlsx_rows(Path(f).read_bytes()):
            if r and vuid_ok(r[0]):
                add(out, r[0], "", "in-person")
    f = latest("el-paso/*/*_Received_Ballots_Voter_List_*.csv")
    if f:
        with open(f, encoding="utf-8-sig", errors="replace", newline="") as fh:
            for r in csv.DictReader(fh):
                add(out, r.get("VUID", ""), us_date(r.get("ReceivedDate", "")), "mail")
    return out


def county_cameron() -> dict:
    out: dict = {}
    f = latest("cameron/*/*_01_Cameron_County*.xlsx")
    if f:
        rows = diff.xlsx_rows(Path(f).read_bytes())
        for r in rows:
            if len(r) >= 3 and vuid_ok(r[0]):
                add(out, r[0], us_date(r[2]), "in-person")
    f = latest("cameron/*/*_MAIL-GN26-Cameron-County-CC-*.xlsx")
    if f:
        for r in diff.xlsx_rows(Path(f).read_bytes()):
            if len(r) >= 4 and vuid_ok(r[0]):
                add(out, r[0], r[3][:10] if re.match(r"\d{4}-\d\d-\d\d", r[3]) else excel_date(r[3]), "mail")
    return out


def county_hays() -> dict:
    out: dict = {}
    f = latest("hays/*/*in-person-checkins-5454*.html")
    if f:
        text = Path(f).read_text(encoding="utf-8", errors="replace")
        for m in HAYS_IP_RE.finditer(text):
            add(out, m[1], m[2], "in-person")
    f = latest("hays/*/*absentee-received-5414*")
    if f:
        with zipfile.ZipFile(f) as z:
            csvn = next((n for n in z.namelist() if n.lower().endswith(".csv")), None)
            if csvn:
                for r in csv.DictReader(io.TextIOWrapper(z.open(csvn), encoding="utf-8-sig", errors="replace", newline="")):
                    add(out, r.get("VUID", ""), (r.get("Date") or "")[:10], "mail")
            elif "xl/workbook.xml" in z.namelist():
                rows = diff.xlsx_rows(Path(f).read_bytes())
                hdr = rows[0] if rows else []
                iv = hdr.index("VUID") if "VUID" in hdr else 0
                idt = hdr.index("Date") if "Date" in hdr else 4
                for r in rows[1:]:
                    if len(r) > max(iv, idt):
                        add(out, r[iv], (r[idt] or "")[:10], "mail")
    return out


def pdf_vuids(path: str):
    """Yield VUIDs from a Bexar roster PDF. Each row is Name | VUID | Polling Place | Precinct, so
    the single 8-11 digit token on a line is the VUID (precincts are <=4 digits). Never prints rows."""
    import subprocess
    try:
        out = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                             capture_output=True, timeout=600).stdout.decode("utf-8", "replace")
    except Exception as e:
        print(f"    ! pdftotext failed on {Path(path).name}: {type(e).__name__}: {e}")
        return
    for line in out.splitlines():
        m = re.search(r"\b(\d{8,11})\b", line)
        if m:
            yield m.group(1)


def county_bexar() -> dict:
    out: dict = {}
    # in-person: daily Early-Vote-Ballots files; the check-in date is the file's date (no date column)
    ev = {}
    for f in sorted(glob.glob(str(RAW / "bexar" / "*" / "*_ev_*.pdf"))):
        m = re.search(r"_ev_(\d{4}-\d\d-\d\d)\.pdf$", f)
        if m:
            ev[m[1]] = f  # one file per date; later capture wins (sorted by day dir then time)
    for date, f in sorted(ev.items()):
        for v in pdf_vuids(f):
            add(out, v, date, "in-person")
    # mail: Received-Mail-Ballots files are daily (ballots received that day, ~hundreds each), not
    # cumulative, so union every date; the file's date is the received date (no per-row date column).
    mail = {}
    for f in sorted(glob.glob(str(RAW / "bexar" / "*" / "*_mail_*.pdf"))):
        m = re.search(r"_mail_(\d{4}-\d\d-\d\d)\.pdf$", f)
        if m:
            mail[m[1]] = f  # one file per date; later capture wins
    for date, f in sorted(mail.items()):
        for v in pdf_vuids(f):
            add(out, v, date, "mail")
    return out


def county_tarrant() -> dict:
    """Tarrant posts tab-delimited .txt rosters (one zip each): the EV in-person report and the
    absentee returned-ballot report. Both carry a 'SOS Voter ID' column. In-person has no per-row
    date; mail has 'Return Date' (the returned/received date)."""
    out: dict = {}
    for pat, datecol, meth in (("tarrant/*/*Early_voting_in_person_report.zip", None, "in-person"),
                               ("tarrant/*/*absentee_returned_voter_report.zip", "Return Date", "mail")):
        z = latest(pat)
        if not z:
            continue
        with zipfile.ZipFile(z) as zf:
            for n in zf.namelist():
                if not n.lower().endswith(".txt"):
                    continue
                fh = io.TextIOWrapper(zf.open(n), encoding="utf-8-sig", errors="replace", newline="")
                for r in csv.DictReader(fh, delimiter="\t"):
                    date = us_date(r.get(datecol, "")) if datecol else ""
                    add(out, r.get("SOS Voter ID", ""), date, meth)
    return out


def county_comal() -> dict:
    """Comal posts daily per-voter PDFs on comalcounty.gov CivicPlus DocumentCenter: 'IN PERSON'
    (columns No. | Name | State ID | Ballot Style; State ID = 10-digit VUID, the only 8-11 digit
    token per row) and 'BBM RETD' (ballots returned by mail). Neither has a per-row date, so the
    file's date is the check-in / received date; both daily (not cumulative), so union every date.
    Same shape as Bexar -> reuse pdf_vuids(). Never prints rows."""
    out: dict = {}
    for pat, meth in (("comal/*/*_ev_*.pdf", "in-person"), ("comal/*/*_mail_*.pdf", "mail")):
        byday = {}
        for f in sorted(glob.glob(str(RAW / pat))):
            m = re.search(r"_(?:ev|mail)_(\d{4}-\d\d-\d\d)\.pdf$", f)
            if m:
                byday[m[1]] = f  # one file per date; later capture wins
        for date, f in sorted(byday.items()):
            for v in pdf_vuids(f):
                add(out, v, date, meth)
    return out


def _xlsx_dicts(path: str):
    """Yield each data row of an xlsx as a {header: cell} dict (header = first row)."""
    rows = diff.xlsx_rows(open(path, "rb").read())
    if not rows:
        return
    hdr = rows[0]
    for r in rows[1:]:
        yield {hdr[i]: (r[i] if i < len(r) else "") for i in range(len(hdr))}


def county_collin() -> dict:
    """Collin publishes free per-voter rosters at collincountytx.gov/elections/rosters (both .xlsx):
    in-person early voting (CertificateNumber = VUID verified against SOS ID_VOTER at 99.8%, VoteDate
    per-row, all VotedPhase='Early Voting') and by-mail absentee returns (VUID, ReceivedDate = ballots
    received). Both cumulative 'through <date>', so read the latest of each. excel_date() handles both
    a serial and an M/D/YYYY[ time] string. Never prints rows."""
    out: dict = {}
    f = latest("collin/*/*early-voters*.xlsx")
    if f:
        for r in _xlsx_dicts(f):
            add(out, r.get("CertificateNumber", ""), excel_date(r.get("VoteDate", "")), "in-person")
    elif (f := latest("collin/*/*early-voters*.csv")):  # fallback: CSV export, same columns
        with open(f, newline="", encoding="utf-8-sig", errors="replace") as fh:
            for r in csv.DictReader(fh):
                add(out, r.get("CertificateNumber", ""), us_date(r.get("VoteDate", "")), "in-person")
    f = latest("collin/*/*absentee-returns*.xlsx")
    if f:
        for r in _xlsx_dicts(f):
            add(out, r.get("VUID", ""), excel_date(r.get("ReceivedDate") or r.get("ReturnDate") or ""), "mail")
    return out


def county_denton() -> dict:
    """Denton publishes free per-voter xlsx rosters at votedenton.gov/early-voting-election-day-rosters/
    (fetched via the prod box; AWS WAF blocks the dev container's non-US IP). In-person
    'Early-Voting-Roster' has State ID = VUID (SOS-verified 99.8%) and only a check-in Time, no per-row
    vote date, so date is left blank. 'Returned_Mail_Roster' has SOS_Vuid + Return Date. Both cumulative
    'through <date>', read the latest of each. Never prints rows."""
    out: dict = {}
    f = latest("denton/*/*Early-Voting-Roster*.xlsx")
    if f:
        for r in _xlsx_dicts(f):
            add(out, r.get("State ID", ""), "", "in-person")
    f = latest("denton/*/*Returned_Mail_Roster*.xlsx")
    if f:
        for r in _xlsx_dicts(f):
            add(out, r.get("SOS_Vuid", ""), excel_date(r.get("Return Date", "")), "mail")
    return out


def county_none() -> dict:
    return {}


COUNTY_PARSERS = {"harris": county_harris, "dallas": county_dallas, "travis": county_travis,
                  "hidalgo": county_hidalgo, "el-paso": county_el_paso, "cameron": county_cameron,
                  "bexar": county_bexar, "hays": county_hays, "tarrant": county_tarrant,
                  "comal": county_comal, "collin": county_collin, "denton": county_denton}


# ---------------------------------------------------------------- compare
def by_date(d: dict, keys) -> dict:
    c = collections.Counter((d[k][0] or "?", d[k][1]) for k in keys)
    return {f"{date} {meth}": n for (date, meth), n in sorted(c.items())}


def compare(county: str, out_dir: Path) -> dict:
    sos, sos_counts = sos_rosters(county)
    cty = COUNTY_PARSERS[county]()
    s_keys, c_keys = set(sos), set(cty)
    both = s_keys & c_keys
    sos_only, cty_only = s_keys - c_keys, c_keys - s_keys
    res = {
        "county": county, "definition": COUNTY_DEFS[county],
        "sos_vuids": len(s_keys), "county_vuids": len(c_keys), "both": len(both),
        "sos_only": len(sos_only), "county_only": len(cty_only),
        "sos_by_method": dict(collections.Counter(v[1] for v in sos.values())),
        "county_by_method": dict(collections.Counter(v[1] for v in cty.values())),
        "sos_only_by_date": by_date(sos, sos_only), "county_only_by_date": by_date(cty, cty_only),
        "method_disagree": sum(1 for k in both if sos[k][1] != cty[k][1]),
        "sos_rows_by_date": sos_counts,
    }
    if not c_keys:
        res["note"] = "no county roster available; sos_only is the whole SOS list"
    for label, keys, src in (("sos_only", sos_only, sos), ("county_only", cty_only, cty)):
        if not c_keys:
            continue
        p = out_dir / f"{county}_{label}.csv"
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", newline="") as fh:
            w = csv.writer(fh, lineterminator="\n"); w.writerow(["vuid", "date", "method"])
            for k in sorted(keys):
                w.writerow([k, src[k][0], src[k][1]])
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--out", default=str(ROOT / "data" / "match"))
    a = ap.parse_args()
    only = {s.strip() for s in a.only.split(",") if s.strip()}
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    out_dir = Path(a.out) / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(out_dir, 0o700)
    results = []
    print(f"{'county':8} {'sos':>8} {'county':>8} {'both':>8} {'sos_only':>9} {'cty_only':>9}")
    for c in COUNTIES:
        if only and c not in only:
            continue
        try:
            r = compare(c, out_dir)
        except Exception as e:
            print(f"{c:8} failed: {type(e).__name__}: {e}")
            continue
        results.append(r)
        print(f"{c:8} {r['sos_vuids']:>8,} {r['county_vuids']:>8,} {r['both']:>8,} {r['sos_only']:>9,} {r['county_only']:>9,}")
        if r["county_vuids"]:
            print(f"         sos_only by date: {r['sos_only_by_date']}")
            print(f"         county_only by date: {r['county_only_by_date']}")
    (out_dir / "summary.json").write_text(json.dumps({"ts": ts, "results": results}, indent=1))
    with open(out_dir / "summary.csv", "w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["ts", "county", "sos_vuids", "county_vuids", "both", "sos_only", "county_only", "method_disagree"])
        for r in results:
            w.writerow([ts, r["county"], r["sos_vuids"], r["county_vuids"], r["both"], r["sos_only"], r["county_only"], r["method_disagree"]])
    os.chmod(out_dir / "summary.json", 0o644); os.chmod(out_dir / "summary.csv", 0o644)
    print(f"written {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
