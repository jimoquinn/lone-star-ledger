#!/usr/bin/env python3
"""Turn archived raw files under data/raw/ into normalized observation rows.

Stdlib only (plus `pdftotext -layout` via diff.pdf_text). Each parser reads one
archived file and returns rows shaped like the `observation` table in PLAN.md:

  {source, county, ev_date, metric, value, definition, file}

  source      "sos" or the county slug the file came from
  county      slug (travis, bexar, harris, dallas, hays, hidalgo, el-paso, cameron; "state" for SOS statewide)
  ev_date     YYYY-MM-DD
  metric      in_person_day | in_person_cum | mail_cum | total_cum | registered
  value       int; cumulative metrics are "through ev_date"
  definition  what the source counts (check-ins vs ballots, received vs accepted ...)
  file        path relative to data/raw

Usage:
  python3 -I collector/parse.py                 # every parseable file in data/raw -> CSV on stdout
  python3 -I collector/parse.py path [path...]  # just those files
  python3 -I collector/parse.py --check         # cross-check table: latest county cum vs SOS cum, same ev_date

Rosters (names, VUIDs) are never parsed except the Cameron and Dallas mail rosters, from
which only a count of rows per received/return date is taken. Dallas's voter-level in-person
details workbook is ignored. Nothing voter-level is ever printed.
total_cum is emitted only when a single file carries both in-person and mail.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html as htmllib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect  # noqa: E402  (COUNTIES name map)
import diff  # noqa: E402  (xlsx_rows, csv_rows, html_text, pdf_text)

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
SOS_TO_SLUG = dict(collect.COUNTIES)
MONTHS = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
SKIP_DIRS = {"diffs", "sos-roster"}

# A number token in a pdftotext line: 1,234 / 1234 / N/A. Not part of a word, code, decimal or percent.
NUM = re.compile(r"(?<![\w,.%/-])(\d{1,3}(?:,\d{3})+|\d+|N/A)(?![\w,.%/-])")
# Date tokens as they appear in the county PDF headers.
DATE_TOKEN = re.compile(r"\b(?:(\d{1,2})-([A-Z][a-z]{2})|([A-Z][a-z]{2}) (\d{1,2})|(\d{1,2})/(\d{1,2})/(\d{2,4}))\b")


def warn(msg: str) -> None:
    print(f"parse: {msg}", file=sys.stderr)


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(RAW.resolve()))
    except ValueError:
        return str(path)


def archive_year(path: Path) -> int:
    """Year of the data/raw/<source>/<YYYY-MM-DD>/ directory the file sits in (fallback: today)."""
    m = re.search(r"/(\d{4})-\d{2}-\d{2}/", str(path.resolve()))
    return int(m.group(1)) if m else dt.date.today().year


def to_int(tok: str) -> int:
    return 0 if tok == "N/A" else int(tok.replace(",", ""))


# ---------------------------------------------------------------- row helpers
def row(source: str, county: str, ev_date: dt.date | str, metric: str, value: int, definition: str, file: str) -> dict:
    return {"source": source, "county": county, "ev_date": ev_date if isinstance(ev_date, str) else ev_date.isoformat(),
            "metric": metric, "value": int(value), "definition": definition, "file": file}


def trim_future(series: list[tuple[dt.date, int]]) -> list[tuple[dt.date, int]]:
    """Drop trailing zero columns (days that have not happened yet)."""
    last = max((i for i, (_, v) in enumerate(series) if v), default=-1)
    return series[: last + 1]


def daily_rows(source: str, county: str, series: list[tuple[dt.date, int]], day_metric: str | None,
               cum_metric: str | None, definition: str, file: str, through: dt.date | None = None) -> list[dict]:
    """Rows for a per-day series: the day value and the running total through each day."""
    out, cum = [], 0
    for d, v in series:
        if through is not None and d > through:
            break
        cum += v
        if day_metric:
            out.append(row(source, county, d, day_metric, v, definition, file))
        if cum_metric:
            out.append(row(source, county, d, cum_metric, cum, definition, file))
    return out


def total_rows(source: str, county: str, ip: list[tuple[dt.date, int]], mail: list[tuple[dt.date, int]],
               definition: str, file: str) -> list[dict]:
    """total_cum through each in-person day = running in-person + running mail (same file)."""
    mail_by = dict(mail)
    out, cum = [], 0
    for d, v in ip:
        cum += v + mail_by.get(d, 0)
        out.append(row(source, county, d, "total_cum", cum, definition, file))
    return out


# ---------------------------------------------------------------- layout-table helpers (pdftotext -layout)
class Header:
    """Date columns of a sites x days table: [(date, start, end)] plus the TOTAL column span."""

    def __init__(self, cols: list[tuple[dt.date, int, int]], total: tuple[int, int] | None):
        self.cols = cols
        self.total = total
        self.dates = [c[0] for c in cols]
        self.first_start = min(c[1] for c in cols)


def parse_date_token(m: re.Match, year: int) -> dt.date | None:
    g = m.groups()
    try:
        if g[0]:  # 19-Oct
            return dt.date(year, MONTHS[g[1]], int(g[0]))
        if g[2]:  # Oct 19
            return dt.date(year, MONTHS[g[2]], int(g[3]))
        y = int(g[6])
        return dt.date(y + 2000 if y < 100 else y, int(g[4]), int(g[5]))
    except (KeyError, ValueError):
        return None


def find_headers(lines: list[str], year: int) -> list[tuple[int, Header]]:
    """Every line with >= 3 date tokens becomes a header (multi-page PDFs repeat it)."""
    out = []
    for i, ln in enumerate(lines):
        cols = []
        for m in DATE_TOKEN.finditer(ln):
            d = parse_date_token(m, year)
            if d:
                cols.append((d, m.start(), m.end()))
        if len(cols) < 3:
            continue
        total = None
        for j in range(max(0, i - 3), min(len(lines), i + 3)):  # TOTAL label may sit on a neighbouring line
            tm = re.search(r"\bTOTALS?\b", lines[j], re.I)
            if tm and tm.start() > cols[-1][1]:
                total = (tm.start(), tm.end())
                break
        out.append((i, Header(cols, total)))
    return out


def header_for(headers: list[tuple[int, Header]], line_no: int) -> Header | None:
    h = None
    for i, hd in headers:
        if i < line_no:
            h = hd
    return h


def split_row(line: str, hdr: Header) -> tuple[str, list[tuple[str, int, int]]]:
    """Label text and the numeric tokens that sit in the value columns (right-aligned numbers)."""
    toks = [(m.group(1), m.start(1), m.end(1)) for m in NUM.finditer(line)]
    cut = hdr.first_start - 4
    vals = [t for t in toks if t[1] >= cut]
    label_end = vals[0][1] if vals else len(line)
    return line[:label_end].strip(), vals


def map_values(vals: list[tuple[str, int, int]], hdr: Header) -> tuple[dict[dt.date, int], int | None, str]:
    """Assign value tokens to date columns. Positional (nearest column end) first; if that is
    inconsistent or fails the sum check, fall back to ordinal order with the last token as TOTAL."""
    if not vals:
        return {}, None, "empty"
    ends = [(c[0], c[2]) for c in hdr.cols]
    if hdr.total:
        ends.append(("TOTAL", hdr.total[1]))

    def positional():
        got: dict = {}
        for tok, s, e in vals:
            key = min(ends, key=lambda c: abs(c[1] - e))[0]
            if key in got:
                return None
            got[key] = to_int(tok)
        return got

    def ordinal():
        nums = [to_int(t[0]) for t in vals]
        got: dict = {}
        if len(nums) == len(hdr.dates) + 1 or (len(nums) > 1 and hdr.total and len(nums) <= len(hdr.dates) + 1):
            got["TOTAL"] = nums.pop()
        if len(nums) > len(hdr.dates):
            return None
        for d, n in zip(hdr.dates, nums):
            got[d] = n
        return got

    def ok(got):
        if got is None:
            return False
        days = sum(v for k, v in got.items() if k != "TOTAL")
        return "TOTAL" not in got or got["TOTAL"] == days

    for how, fn in (("positional", positional), ("ordinal", ordinal)):
        got = fn()
        if ok(got):
            break
    else:
        got = ordinal() or positional() or {}
        how = "unverified"
    total = got.pop("TOTAL", None)
    series = {k: v for k, v in got.items() if isinstance(k, dt.date)}
    return series, total, how


def table_rows(text: str, year: int) -> list[tuple[str, dict[dt.date, int], int | None, str, Header]]:
    """(label, {date: value}, total, how) for every line that carries values under a date header."""
    lines = text.splitlines()
    headers = find_headers(lines, year)
    out = []
    for i, ln in enumerate(lines):
        hdr = header_for(headers, i)
        if hdr is None or DATE_TOKEN.search(ln):
            continue
        label, vals = split_row(ln, hdr)
        if not vals:
            continue
        series, total, how = map_values(vals, hdr)
        out.append((label, series, total, how, hdr))
    return out


def pick(rows, pattern: str):
    """(series, total, how) of the first table row whose label matches, else (None, None, None)."""
    rx = re.compile(pattern, re.I)
    for label, series, total, how, hdr in rows:
        if rx.search(label):
            return as_series(series, hdr), total, how
    return None, None, None


def as_series(series: dict[dt.date, int], hdr: Header) -> list[tuple[dt.date, int]]:
    return [(d, series.get(d, 0)) for d in hdr.dates]


def check_total(name: str, file: str, series, total, how) -> None:
    if series is None:
        warn(f"{file}: row '{name}' not found")
        return
    s = sum(v for _, v in series)
    if total is not None and total != s:
        warn(f"{file}: '{name}' day values sum to {s:,} but TOTAL column says {total:,} (mapping: {how})")
    elif how == "unverified":
        warn(f"{file}: '{name}' column mapping could not be verified")


# ---------------------------------------------------------------- SOS
def parse_sos_turnout(path: Path) -> list[dict]:
    d = json.loads(path.read_bytes())
    ev = d["early_voting_date"]
    f = rel(path)
    out = []
    for c in d.get("turnout_by_county", []):
        name = (c.get("name") or "").strip().upper()
        slug = SOS_TO_SLUG.get(name) or ("state" if name in ("STATE", "STATEWIDE", "TEXAS", "TOTAL") else None)
        if not slug:
            continue
        ip_day = c.get("in_person_votes_on_date") or 0
        ip = c.get("total_in_person_votes_for_election") or 0
        mail = c.get("total_mail_votes_for_election") or 0
        out += [
            row("sos", slug, ev, "in_person_day", ip_day, "SOS EVR: in-person votes on date", f),
            row("sos", slug, ev, "in_person_cum", ip, "SOS EVR: in-person votes through date", f),
            row("sos", slug, ev, "mail_cum", mail, "SOS EVR: mail ballots through date", f),
            row("sos", slug, ev, "total_cum", ip + mail, "SOS EVR: in-person + mail", f),
        ]
        if c.get("registered_voters"):
            out.append(row("sos", slug, ev, "registered", c["registered_voters"], "SOS EVR: registered voters", f))
    return out


# ---------------------------------------------------------------- Harris
def parse_harris(path: Path) -> list[dict]:
    f, text = rel(path), diff.pdf_text(path.read_bytes()) or ""
    rows = table_rows(text, archive_year(path))
    ip, ip_t, how = pick(rows, r"^Early In-Person Voters")
    check_total("Early In-Person Voters", f, ip, ip_t, how)
    mail, mail_t, how2 = pick(rows, r"^Mail Ballots Received")
    check_total("Mail Ballots Received", f, mail, mail_t, how2)
    tot, tot_t, _ = pick(rows, r"^Total Early Voters")
    out = []
    if ip:
        ip = trim_future(ip)
        out += daily_rows("harris", "harris", ip, "in_person_day", "in_person_cum", "in-person check-ins (unofficial)", f)
    if mail and ip:
        out += daily_rows("harris", "harris", mail, None, "mail_cum", "mail ballots received", f, through=ip[-1][0])
        out += total_rows("harris", "harris", ip, mail, "check-ins + mail ballots received", f)
        if tot and any(v != a + b for (_, v), (_, a), (_, b) in zip(tot, ip, mail)):
            warn(f"{f}: 'Total Early Voters' row != in-person + mail")
    return out


# ---------------------------------------------------------------- Travis
def parse_travis(path: Path) -> list[dict]:
    f, text = rel(path), diff.pdf_text(path.read_bytes()) or ""
    rows = table_rows(text, archive_year(path))
    loc, loc_t, h1 = pick(rows, r"Early Voting Location Voters")
    check_total("Early Voting Location Voters", f, loc, loc_t, h1)
    lim, lim_t, h2 = pick(rows, r"Limited Ballots")
    check_total("Limited Ballots", f, lim, lim_t, h2)
    mail, mail_t, h3 = pick(rows, r"Mail Ballots Received")
    check_total("Mail Ballots Received", f, mail, mail_t, h3)
    cum, cum_t, _ = pick(rows, r"^Cumulative")
    out = []
    m = re.search(r"Registered Voters \(([\d,]+)\)", text)
    if loc:
        ip = [(d, a + (lim[i][1] if lim else 0)) for i, (d, a) in enumerate(loc)]
        ip = trim_future(ip)
        out += daily_rows("travis", "travis", ip, "in_person_day", "in_person_cum",
                          "EV location voters + limited ballots", f)
        if mail:
            out += daily_rows("travis", "travis", mail, None, "mail_cum", "mail ballots received", f, through=ip[-1][0])
            out += total_rows("travis", "travis", ip, mail, "location voters + limited + mail received", f)
            # The "Cumulative" row is really the per-day sum of the three rows above it; verify.
            if cum and any(v != a + b for (_, v), (_, a), (_, b) in zip(cum, ip, mail)):
                warn(f"{f}: 'Cumulative' row != location + limited + mail per day")
        if m:
            out.append(row("travis", "travis", ip[-1][0], "registered", to_int(m.group(1)),
                           "registered voters (county PDF header)", f))
    return out


# ---------------------------------------------------------------- Cameron
def parse_cameron_pdf(path: Path) -> list[dict]:
    f, text = rel(path), diff.pdf_text(path.read_bytes()) or ""
    rows = table_rows(text, archive_year(path))
    tot, tot_t, how = pick(rows, r"^TOTAL$")
    check_total("TOTAL", f, tot, tot_t, how)
    if not tot:
        return []
    return daily_rows("cameron", "cameron", trim_future(tot), "in_person_day", "in_person_cum",
                      "in-person voters by EV location (unofficial)", f)


def parse_cameron_mail(path: Path) -> list[dict]:
    """Mail roster: only the count of rows per 'Ballot Received Date' is used. No names, no VUIDs."""
    f = rel(path)
    rows = diff.xlsx_rows(path.read_bytes())
    if not rows:
        return []
    hdr = [h.strip().lower() for h in rows[0]]
    try:
        col = next(i for i, h in enumerate(hdr) if "received" in h and "date" in h)
    except StopIteration:
        warn(f"{f}: no 'Ballot Received Date' column")
        return []
    counts: dict[dt.date, int] = {}
    bad = 0
    for r in rows[1:]:
        v = r[col].strip() if len(r) > col else ""
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", v) or re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", v)
        if not m:
            bad += 1
            continue
        g = [int(x) for x in m.groups()]
        d = dt.date(g[0], g[1], g[2]) if len(m.group(1)) == 4 else dt.date(g[2], g[0], g[1])
        counts[d] = counts.get(d, 0) + 1
    if bad:
        warn(f"{f}: {bad} rows without a parseable received date")
    if not counts:
        return []
    mf = re.search(r"(\d{4}-\d{2}-\d{2})\.xlsx$", path.name)
    asof = dt.date.fromisoformat(mf.group(1)) if mf else max(counts)
    d, cum, out = min(counts), 0, []
    while d <= max(asof, max(counts)):
        cum += counts.get(d, 0)
        out.append(row("cameron", "cameron", d, "mail_cum", cum, "mail ballots received (roster rows by received date)", f))
        d += dt.timedelta(days=1)
    return out


# ---------------------------------------------------------------- El Paso
def registered_from_text(text: str) -> int | None:
    m = re.search(r"Total Registered Voters[^:\n]*:\s*([\d,]+)", text)
    return to_int(m.group(1)) if m else None


def el_paso_rows(text: str, year: int):
    """El Paso wraps row labels onto two lines; the numbers sit on the second (label-less or
    partial-label) line. Glue each label-less value line to the text line before it."""
    lines = text.splitlines()
    hdrs = find_headers(lines, year)
    out = []
    for i, ln in enumerate(lines):
        hdr = header_for(hdrs, i)
        if hdr is None or DATE_TOKEN.search(ln):
            continue
        label, vals = split_row(ln, hdr)
        if not vals:
            continue
        prev = lines[i - 1].strip() if i else ""
        if prev and not split_row(lines[i - 1], hdr)[1] and not DATE_TOKEN.search(prev):
            label = (prev + " " + label).strip()
        series, total, how = map_values(vals, hdr)
        out.append((label, series, total, how, hdr))
    return out


def parse_el_paso_inperson(path: Path) -> list[dict]:
    f, text = rel(path), diff.pdf_text(path.read_bytes()) or ""
    rows = el_paso_rows(text, archive_year(path))
    tot, tot_t, how = pick(rows, r"Daily Totals")
    check_total("Daily Totals", f, tot, tot_t, how)
    out = []
    if tot:
        tot = trim_future(tot)
        out += daily_rows("el-paso", "el-paso", tot, "in_person_day", "in_person_cum",
                          "in-person voters by EV station (Daily Totals row)", f)
        reg = registered_from_text(text)
        if reg:
            out.append(row("el-paso", "el-paso", tot[-1][0], "registered", reg, "registered voters (county PDF header)", f))
    return out


def parse_el_paso_mail(path: Path) -> list[dict]:
    f, text = rel(path), diff.pdf_text(path.read_bytes()) or ""
    rows = el_paso_rows(text, archive_year(path))
    ret, ret_t, how = pick(rows, r"Mail Ballots Returned")
    check_total("Mail Ballots Returned", f, ret, ret_t, how)
    out = []
    if ret:
        ret = trim_future(ret)
        out += daily_rows("el-paso", "el-paso", ret, None, "mail_cum", "mail ballots returned", f)
    return out


# ---------------------------------------------------------------- Hidalgo
def parse_hidalgo(path: Path) -> list[dict]:
    f, text = rel(path), diff.pdf_text(path.read_bytes()) or ""
    rows = table_rows(text, archive_year(path))
    ip, ip_t, h1 = pick(rows, r"^Sub ?Total")
    check_total("Sub Total", f, ip, ip_t, h1)
    mail, mail_t, h2 = pick(rows, r"^Mail$")
    check_total("Mail", f, mail, mail_t, h2)
    tot, tot_t, _ = pick(rows, r"^Total$")
    out = []
    if ip:
        ip = trim_future(ip)
        out += daily_rows("hidalgo", "hidalgo", ip, "in_person_day", "in_person_cum", "in-person voters (Sub Total row)", f)
        if mail:
            out += daily_rows("hidalgo", "hidalgo", mail, None, "mail_cum", "mail ballots (Mail row)", f, through=ip[-1][0])
            out += total_rows("hidalgo", "hidalgo", ip, mail, "in-person + mail (Total row)", f)
            if tot and any(v != a + b for (_, v), (_, a), (_, b) in zip(tot, ip, mail)):
                warn(f"{f}: 'Total' row != Sub Total + Mail per day")
    return out


# ---------------------------------------------------------------- Bexar
TR = re.compile(r"(?is)<tr[^>]*>(.*?)</tr>")
TD = re.compile(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>")


def parse_bexar(path: Path) -> list[dict]:
    f = rel(path)
    data = path.read_bytes()
    text = diff.html_text(data)
    m = re.search(r"BALLOTS CAST THIS DATE\s+(\d{1,2})/(\d{1,2})/(\d{4})", text, re.I)
    if not m:
        warn(f"{f}: no 'BALLOTS CAST THIS DATE' line")
        return []
    ev = dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    day = cum = prev = 0
    sites, total_row = 0, None
    for tr in TR.findall(data.decode("utf-8", "replace")):
        cells = [re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", c))).strip() for c in TD.findall(tr)]
        if len(cells) < 4 or not all(re.fullmatch(r"[\d,]+", c) for c in cells[1:4]):
            continue
        d, p, t = (to_int(c) for c in cells[1:4])
        if re.search(r"total", cells[0], re.I):
            total_row = (d, p, t)
            continue
        if d + p != t:
            warn(f"{f}: site row daily+previous != to-date")
        sites += 1
        day, prev, cum = day + d, prev + p, cum + t
    if not sites:
        warn(f"{f}: no site rows")
        return []
    if total_row and any(total_row) and total_row != (day, prev, cum):
        warn(f"{f}: 'Total in Person' row {total_row} != sum of {sites} sites {(day, prev, cum)}")
    defn = "in-person ballots cast (sum of EV site rows)"
    return [row("bexar", "bexar", ev, "in_person_day", day, defn, f),
            row("bexar", "bexar", ev, "in_person_cum", cum, defn, f)]


# ---------------------------------------------------------------- Dallas
EXCEL_EPOCH = dt.date(1899, 12, 30)


def excel_date(cell: str) -> dt.date | None:
    """Excel serial (int or datetime float as xlsx_rows returns it) -> date."""
    m = re.fullmatch(r"\d{5}(?:\.\d+)?", cell.strip())
    return EXCEL_EPOCH + dt.timedelta(days=int(float(m.group(0)))) if m else None


def parse_dallas_turnout(path: Path) -> list[dict]:
    """Sites x days sheet: a 'Daily Unofficial Total' row and a 'Cumulative Total' column.
    Emits the per-date sum of the vote-center rows and verifies it against both."""
    f = rel(path)
    rows = diff.xlsx_rows(path.read_bytes())
    try:
        hi = next(i for i, r in enumerate(rows) if r and r[0].strip().lower() == "vote center name")
    except StopIteration:
        warn(f"{f}: no 'Vote Center Name' header row")
        return []
    hdr = rows[hi]
    cols = [(i, d) for i, c in enumerate(hdr) if (d := excel_date(c))]
    cum_col = next((i for i, c in enumerate(hdr) if "cumulative" in c.lower()), None)
    if len(cols) < 3:
        warn(f"{f}: fewer than 3 date columns in header")
        return []

    def nums(r: list[str], idx: list[int]) -> list[int]:
        return [to_int(r[i].strip()) if len(r) > i and re.fullmatch(r"[\d,]+", r[i].strip()) else 0 for i in idx]

    idx = [i for i, _ in cols]
    total_row, sites, sums = None, 0, [0] * len(cols)
    for r in rows[hi + 1:]:
        label = r[0].strip() if r else ""
        if not label:
            continue
        if re.search(r"total", label, re.I):
            total_row = nums(r, idx)
            continue
        vals = nums(r, idx)
        sites += 1
        sums = [a + b for a, b in zip(sums, vals)]
        if cum_col is not None and re.fullmatch(r"[\d,]+", (r[cum_col].strip() if len(r) > cum_col else "")) \
                and to_int(r[cum_col].strip()) != sum(vals):
            warn(f"{f}: site row day values != its Cumulative Total")
    if not sites:
        warn(f"{f}: no vote center rows")
        return []
    if total_row and total_row != sums:
        warn(f"{f}: 'Daily Unofficial Total' row != sum of {sites} vote centers")
    series = trim_future(list(zip([d for _, d in cols], sums)))
    return daily_rows("dallas", "dallas", series, "in_person_day", "in_person_cum",
                      "in-person voters by date and vote center (unofficial)", f)


def parse_dallas_mail(path: Path) -> list[dict]:
    """Mail roster: only the count of rows per 'Return Date' is used. No names, no VUIDs.
    The sheet lists returned ballots only; it carries no accepted/rejected status."""
    f = rel(path)
    rows = diff.xlsx_rows(path.read_bytes())
    hi = col = None
    for i, r in enumerate(rows[:10]):
        for j, h in enumerate(r):
            if "return" in h.lower() and "date" in h.lower():
                hi, col = i, j
                break
        if hi is not None:
            break
    if hi is None:
        warn(f"{f}: no 'Return Date' column")
        return []
    counts: dict[dt.date, int] = {}
    bad = 0
    for r in rows[hi + 1:]:
        d = excel_date(r[col]) if len(r) > col else None
        if d is None:
            bad += 1
            continue
        counts[d] = counts.get(d, 0) + 1
    if bad:
        warn(f"{f}: {bad} rows without a parseable return date")
    if not counts:
        return []
    mf = re.search(r"_(\d{1,2})\.(\d{1,2})\.(\d{2,4})\.xlsx$", path.name)
    if mf:
        y = int(mf.group(3))
        asof = dt.date(y + 2000 if y < 100 else y, int(mf.group(1)), int(mf.group(2)))
    else:
        asof = max(counts)
    d, cum, out = min(counts), 0, []
    while d <= max(asof, max(counts)):
        cum += counts.get(d, 0)
        out.append(row("dallas", "dallas", d, "mail_cum", cum,
                       "mail ballots returned (roster rows by Return Date; no accepted/rejected status)", f))
        d += dt.timedelta(days=1)
    return out


# ---------------------------------------------------------------- dispatch
PREFIX = re.compile(r"^\d{6}_[0-9a-f]{10}_(.*)$")
PARSERS: list[tuple[str, re.Pattern, object]] = [
    ("sos", re.compile(r"^turnout_\d+_\d{4}-\d{2}-\d{2}\.json$"), parse_sos_turnout),
    ("harris", re.compile(r"^EVPA_unofficial\.pdf$"), parse_harris),
    ("travis", re.compile(r"^G26-Daily-Totals-\d{8}\.pdf$"), parse_travis),
    ("cameron", re.compile(r"^01_Cameron_County\d{2}-\d{2}-\d{4}\.pdf$"), parse_cameron_pdf),
    ("cameron", re.compile(r"^MAIL-GN26-Cameron-County-CC-\d{4}-\d{2}-\d{2}\.xlsx$"), parse_cameron_mail),
    ("el-paso", re.compile(r"^Nov_2026_stats_Mail_.*\.pdf$", re.I), parse_el_paso_mail),
    ("el-paso", re.compile(r"^Nov_2026_stats_(?!Mail).*\.pdf$", re.I), parse_el_paso_inperson),
    ("hidalgo", re.compile(r"^EV-SUBS\.pdf$"), parse_hidalgo),
    ("bexar", re.compile(r"^earlyvotecounts(dual)?\.bexar\.org"), parse_bexar),
    ("dallas", re.compile(r"^Early-Voting-Turnout-By-Date-Location_\d{1,2}\.\d{1,2}\.\d{4}\.xlsx$"), parse_dallas_turnout),
    ("dallas", re.compile(r"^Mail-Ballot-Returns-Report_\d{1,2}\.\d{1,2}\.\d{2,4}\.xlsx$"), parse_dallas_mail),
]


def parser_for(path: Path):
    r = rel(path)
    parts = Path(r).parts
    if len(parts) < 2 or parts[0] in SKIP_DIRS:
        return None
    source = parts[0]
    m = PREFIX.match(path.name)
    base = m.group(1) if m else path.name
    for src, rx, fn in PARSERS:
        if src == source and rx.match(base):
            return fn
    return None


def parse_file(path: str | Path) -> list[dict]:
    """Rows for one archived file; [] when the file is not a known turnout file."""
    p = Path(path)
    fn = parser_for(p)
    return fn(p) if fn else []


def all_files() -> list[Path]:
    out = []
    for p in sorted(RAW.rglob("*")):
        if p.is_file() and parser_for(p):
            out.append(p)
    return out


# ---------------------------------------------------------------- cross-check
def cross_check(rows: list[dict]) -> str:
    latest_sos: dict[tuple, dict] = {}
    for r in rows:
        if r["source"] == "sos":
            k = (r["county"], r["ev_date"], r["metric"])
            if k not in latest_sos or r["file"] > latest_sos[k]["file"]:
                latest_sos[k] = r
    latest_county: dict[tuple, dict] = {}
    for r in rows:
        if r["source"] == "sos" or r["metric"] not in ("in_person_cum", "mail_cum"):
            continue
        k = (r["county"], r["metric"])
        cur = latest_county.get(k)
        if cur is None or (r["file"], r["ev_date"]) > (cur["file"], cur["ev_date"]):
            latest_county[k] = r
    lines = [f"{'county':8} {'through':10} {'metric':14} {'county':>10} {'sos':>10} {'gap':>8}  county definition / file"]
    for (county, metric), r in sorted(latest_county.items()):
        s = latest_sos.get((county, r["ev_date"], metric))
        sv = s["value"] if s else None
        gap = f"{r['value'] - sv:+,}" if sv is not None else "n/a"
        lines.append(f"{county:8} {r['ev_date']:10} {metric:14} {r['value']:>10,} {(f'{sv:,}' if sv is not None else '-'):>10} {gap:>8}  "
                     f"{r['definition']} [{r['file']}]")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="archived files (default: every parseable file under data/raw)")
    ap.add_argument("--check", action="store_true", help="print county-vs-SOS cross-check table instead of CSV")
    ap.add_argument("--list", action="store_true", help="only list the files that would be parsed")
    a = ap.parse_args()
    paths = [Path(p) for p in a.paths] if a.paths else all_files()
    if a.list:
        for p in paths:
            print(rel(p))
        return 0
    rows: list[dict] = []
    failed = 0
    for p in paths:
        try:
            got = parse_file(p)
        except Exception as e:  # keep going; message carries only the file name and error type
            warn(f"{rel(p)}: {type(e).__name__}: {str(e)[:120]}")
            failed += 1
            continue
        if not got and parser_for(p):
            warn(f"{rel(p)}: no rows")
        rows += got
    if a.check:
        print(cross_check(rows))
    else:
        w = csv.DictWriter(sys.stdout, fieldnames=["source", "county", "ev_date", "metric", "value", "definition", "file"])
        w.writeheader()
        w.writerows(rows)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
