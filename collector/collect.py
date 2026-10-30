#!/usr/bin/env python3
"""Raw-archive collector for Texas early-vote data (SOS Civix EVR + 8 counties).

Stdlib only. Every run fetches each source and stores the bytes only when they
changed (sha256), under data/raw/<source>/<YYYY-MM-DD>/. Every attempt is logged
to data/raw/manifest.jsonl. Parsing into Postgres happens later, from the archive.

Usage:
  python3 collector/collect.py              # everything except SOS rosters
  python3 collector/collect.py --rosters    # also SOS per-county voter rosters
  python3 collector/collect.py --only sos,bexar

PII: roster files hold voter names and VUIDs. They are written with 0600 perms
and never printed or logged (only counts and byte sizes).
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diff  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
STATE = RAW / "state.json"
MANIFEST = RAW / "manifest.jsonl"

UA = "Mozilla/5.0 (X11; Linux x86_64) TXEarlyVoteTracker/0.1 (+public-interest research)"
TIMEOUT = 90

# Some county sites (Dallas: AWS WAF, Hays: Cloudflare) serve CAPTCHA challenges to non-US addresses.
# Those sources are fetched with curl on a US host over SSH: REMOTE_FETCH_HOST=<ssh host alias>.
# REMOTE_FETCH_HOST=local runs the same curl here (useful when this box is on a US VPN, or for testing).
REMOTE_HOST = os.environ.get("REMOTE_FETCH_HOST", "")
REMOTE_SOURCES = {x.strip() for x in os.environ.get("REMOTE_FETCH_SOURCES", "dallas,hays").split(",") if x.strip()}
# Sources that need a real browser (JavaScript interstitial). Fetched on REMOTE_FETCH_HOST with
# collector/browser_fetch.py (deployed to ~/lsl/ there, venv at ~/lsl-venv). Example: BROWSER_SOURCES=hays
BROWSER_SOURCES = {x.strip() for x in os.environ.get("BROWSER_SOURCES", "").split(",") if x.strip()}
BROWSER_CMD = os.environ.get("BROWSER_CMD", "~/lsl-venv/bin/python ~/lsl/browser_fetch.py")
# Where the browser runs: an SSH host, or "local" for this machine. Defaults to REMOTE_FETCH_HOST.
BROWSER_HOST = os.environ.get("BROWSER_FETCH_HOST", REMOTE_HOST)
# Sources to fetch only every N minutes instead of every run, e.g. SOURCE_EVERY_MIN="hays=360".
# Hays' Cloudflare rules hard-block us after a few automated fetches, so we probe rarely.
SOURCE_EVERY_MIN = {k.strip(): int(v) for k, v in
                    (x.split("=", 1) for x in os.environ.get("SOURCE_EVERY_MIN", "hays=360").split(",") if "=" in x)}
# Manual drop folder: a file saved from a desktop browser into data/inbox/<source>/ is archived and
# diffed like a fetched one (keyed by its file name), then removed from the inbox.
INBOX = ROOT / "data" / "inbox"

SOS_BASE = "https://goelect.txelections.civixapps.com/api-ivis-system/api/v1"
SOS_ELECTION_ID = "53815"  # 2026 NOVEMBER GENERAL ELECTION
COUNTIES = {  # name in Civix -> slug
    "TRAVIS": "travis", "BEXAR": "bexar", "HARRIS": "harris", "DALLAS": "dallas",
    "HAYS": "hays", "HIDALGO": "hidalgo", "EL PASO": "el-paso", "CAMERON": "cameron",
}

DOC_EXT = r"\.(pdf|xlsx|xls|csv|zip|txt)(\?|$)"

# Fixed URLs fetched every run.
FIXED = {
    "harris": [
        "https://appfiles.harrisvotes.com/harrisvotes/prd/docs/Uploads/EVPA_unofficial.pdf",
        # Unofficial cumulative EV and ballot-by-mail rosters (linked from app.harrisvotes.com/ElectionResults/ElectionRosters)
        "https://appfiles.harrisvotes.com/harrisvotes/prd/Reports/Cumulative_EV_ToPost_1126.zip",
        "https://appfiles.harrisvotes.com/harrisvotes/prd/Reports/Cumulative_BBM_ToPost_1126.zip",
    ],
    "bexar": [
        "https://earlyvotecountsdual.bexar.org/",
        "https://earlyvotecounts.bexar.org/",
    ],
    "hidalgo": [
        "https://www.hidalgocounty.us/DocumentCenter/View/73361/EV-SUBS",
        "https://www.hidalgocounty.us/DocumentCenter/View/73363/EV-Roster-November-3-2026-Cumulative",
        "https://www.hidalgocounty.us/DocumentCenter/View/73301/ABBM-LIST-November-3-2026-Cumulative",
    ],
    "hays": [
        "https://www.hayscountytx.gov/DocumentCenter/View/5414",
        "https://www.hayscountytx.gov/DocumentCenter/View/5454",
    ],
    "travis": [
        "https://votetravis.gov/wp-content/uploads/G26-Voter-Rosters.zip",
    ],
}

# Index pages: store the page, then download every linked document matching the filter.
PAGES = {
    "travis": [("https://votetravis.gov/current-election-information/current-election/", r"G26-.*(Daily|Total|Roster|Mail)")],
    "dallas": [("https://www.dallascountyvotes.org/election-results/historical/november-3-2026-general-and-joint-election/",
                r"wp-content/uploads/2026/1[01]/.*(Turnout|Mail-Ballot|Early-Voting|Roster)")],
    "el-paso": [("https://epcountyvotestx.gov/voter-information/current-election",
                 r"s3\.amazonaws\.com/documents/.*(stat|total|turnout|roster|voter_list|received_ballots|mail)")],
    "cameron": [("https://cameroncountytx.gov/elections/results/", r"uploads/2026/1[01]/(01_Cameron_County|MAIL-GN26-Cameron-County)")],
    "hidalgo": [("https://www.hidalgocounty.us/3523/Unofficial-Early-Voting-Totals-Rosters", r"DocumentCenter/View/\d+/(EV-|ABBM|.*Roster|.*Total|.*Mail)")],
    "hays": [("https://www.hayscountytx.gov/873/November-3-2026-General-Election",
              r"DocumentCenter/View/.*(5414|5454|Early.?Voting|Absentee|Mail.?Ballot|Roster|Turnout)")],
    "harris": [("https://www.harrisvotes.com/Election-Results/Election-Reports?filter=EV%20Report", r"(EVPA_unofficial|Reports/.*(2026|1126))")],
}

# Index pages fetched less often than every run (minutes). Hidalgo's host returns HTTP 429 when polled every 15 min.
PAGE_EVERY_MIN = {"hidalgo": 60}

# Bexar publishes dated per-voter rosters in CivicEngage DocumentCenter folder 245: daily
# "Early-Vote-Ballots" (in-person check-ins, Name/VUID/Polling Place/Precinct) and cumulative
# "Received-Mail-Ballots". The folder index is JavaScript-rendered (no <a> links in the HTML),
# so the PAGES link-scraper can't see them. Instead each document is a stable numeric id:
# GET /DocumentCenter/View/<id> 301-redirects to a slug carrying the type + date, and the file
# downloads from /ImageRepository/Document?documentID=<id>. No RSS feed and no ETag/Last-Modified,
# so we discover by scanning ids forward from the last frontier and fetch each matching id once
# (the dated files are immutable once posted; a correction would appear as a new id).
BEXAR_DC = "https://elections.bexar.gov"
BEXAR_DC_FLOOR = 1900          # ids below this are prior elections / unrelated folders
BEXAR_DC_LOOKBACK = 3          # re-probe a few ids below the frontier each run
BEXAR_DC_404_STOP = 30         # stop after this many consecutive 404s past the frontier
BEXAR_DC_MAX_PROBES = 120      # hard cap on redirect probes per run
BEXAR_DC_EVERY_MIN = 60        # discovery cadence once backfilled (files appear ~once a day)
BEXAR_DC_SLUG = re.compile(r"/View/\d+/(Early-Vote-Ballots|Received-Mail-Ballots)-+(\d{4}-\d{2}-\d{2})", re.I)

PII_HINTS = ("roster", "topost", "check-ins", "check_ins", "checkins", "voter_list", "voter-list", "voter list", "received_ballots", "abbm-list", "abbm_list",
             "in-person-early-voting-turnout-details", "mail-ballot-returns", "mail-gn26", "ballot_address",
             "ballot by mail", "absentee")
PII_RE = re.compile(rb"VUID|Voter ?Name|VOTER LIST|id_voter", re.I)


def looks_pii(name: str, data: bytes) -> bool:
    """Name hint, or a voter-ID column in a CSV/XLSX/ZIP. Never prints anything."""
    if any(h in name.lower() for h in PII_HINTS):
        return True
    if data[:4] == b"PK\x03\x04":
        try:
            import io, zipfile
            z = zipfile.ZipFile(io.BytesIO(data))
            names = z.namelist()
            if "xl/sharedStrings.xml" in names:
                return bool(PII_RE.search(z.read("xl/sharedStrings.xml")[:400_000]))
            return any(n.lower().endswith((".csv", ".xlsx", ".xls", ".txt")) for n in names)
        except Exception:
            return False
    if data[:5] not in (b"%PDF-",) and b"<html" not in data[:2000].lower():
        return bool(PII_RE.search(data[:8000]))
    return False


# ---------------------------------------------------------------- utilities
def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(STATE)


def log(rec: dict) -> None:
    with MANIFEST.open("a") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")


def http_get(url: str, cond: dict | None = None) -> tuple[int, bytes, dict]:
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if cond:
        if cond.get("etag"):
            headers["If-None-Match"] = cond["etag"]
        if cond.get("last_modified"):
            headers["If-Modified-Since"] = cond["last_modified"]
    url = urllib.parse.quote(url, safe=":/?&=%+@;,$!*'()[]#~")  # encode spaces, keep existing %xx
    req = urllib.request.Request(url, headers=headers)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, b"", dict(e.headers or {})


def curl_get(url: str, host: str, cond: dict | None = None) -> tuple[int, bytes, dict]:
    """GET via curl, locally (host == 'local') or on `host` over SSH (BatchMode, so it never prompts)."""
    import shlex
    import subprocess
    curl = ["curl", "-sS", "-L", "--max-time", str(TIMEOUT - 10), "-A", UA, "-H", "Accept: */*",
            "-o", "-", "-w", "%{stderr}CURLMETA %{http_code} %{content_type} etag=%header{etag} lm=%header{last-modified}\n"]
    if cond and cond.get("etag"):
        curl += ["-H", f"If-None-Match: {cond['etag']}"]
    if cond and cond.get("last_modified"):
        curl += ["-H", f"If-Modified-Since: {cond['last_modified']}"]
    curl.append(urllib.parse.quote(url, safe=":/?&=%+@;,$!*'()[]#~"))  # encode spaces, keep existing %xx
    if host == "local":
        cmd = curl
    else:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host, " ".join(shlex.quote(c) for c in curl)]
    r = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT + 30)
    meta = re.search(rb"CURLMETA (\d+) ?(.*?) etag=(.*) lm=(.*)", r.stderr)
    if not meta:
        raise RuntimeError(f"curl via {host}: rc={r.returncode} {r.stderr.decode('utf-8', 'replace').strip()[:200]}")
    hdrs = {"Content-Type": meta.group(2).decode("utf-8", "replace").strip()}
    if meta.group(3).strip():
        hdrs["ETag"] = meta.group(3).decode("utf-8", "replace").strip()
    if meta.group(4).strip():
        hdrs["Last-Modified"] = meta.group(4).decode("utf-8", "replace").strip()
    return int(meta.group(1)), r.stdout, hdrs


def browser_get(url: str, host: str) -> tuple[int, bytes, dict]:
    """GET through headless Chromium on `host` (see browser_fetch.py). Returns (status, body, headers)."""
    import shlex
    import subprocess
    if host == "local":
        cmd = ["bash", "-c", BROWSER_CMD + " " + shlex.quote(url)]
    else:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host, BROWSER_CMD + " " + shlex.quote(url)]
    r = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT + 120)
    line = next((ln for ln in r.stdout.splitlines() if ln.startswith(b"{")), None)
    if not line:
        raise RuntimeError(f"browser via {host}: rc={r.returncode} {r.stderr.decode('utf-8', 'replace').strip()[-200:]}")
    rec = json.loads(line)
    if rec.get("error"):
        raise RuntimeError(f"browser via {host}: {rec['error']}")
    return int(rec["status"]), base64.b64decode(rec["b64"]), {"Content-Type": rec.get("content_type", "")}


def safe_name(url: str) -> str:
    p = urllib.parse.urlparse(url)
    tail = (p.path.rstrip("/").split("/")[-1] or p.netloc) + (("_" + p.query) if p.query else "")
    tail = urllib.parse.unquote(tail)
    return re.sub(r"[^A-Za-z0-9._-]+", "_", tail)[:120] or "index"


def guess_ext(name: str, headers: dict, data: bytes = b"") -> str:
    if re.search(r"\.[A-Za-z0-9]{2,4}$", name):
        return ""
    if data[:5] == b"%PDF-":
        return ".pdf"
    if data[:4] == b"PK\x03\x04":
        return ".xlsx" if b"xl/workbook.xml" in data[-4000:] or b"xl/workbook.xml" in data[:4000] else ".zip"
    head = data[:4000].lstrip(b"\xef\xbb\xbf")
    if head[:1] in (b"{", b"["):
        return ".json"
    if b"<html" in head.lower() or b"<!doctype" in head.lower():
        return ".html"
    if head and b"," in head.split(b"\n", 1)[0] and all(32 <= c < 127 or c in (9, 10, 13) for c in head[:512]):
        return ".csv"
    ct = (headers.get("Content-Type") or headers.get("content-type") or "").lower()
    for k, v in (("pdf", ".pdf"), ("spreadsheetml", ".xlsx"), ("ms-excel", ".xls"), ("csv", ".csv"),
                 ("zip", ".zip"), ("json", ".json"), ("html", ".html"), ("text/plain", ".txt")):
        if k in ct:
            return v
    return ".bin"


def store(source: str, key: str, url: str, data: bytes, headers: dict, state: dict,
          name: str | None = None, pii: bool = False) -> bool:
    """Write data if its hash differs from the last stored version of `key`. Returns changed."""
    sha = hashlib.sha256(data).hexdigest()
    prev = state.get(key, {})
    changed = prev.get("sha256") != sha
    text_sha = None
    if changed and b"<html" in data[:4000].lower():
        # HTML pages carry nonces that change every load; only count a change in the visible text.
        text_sha = hashlib.sha256(diff.html_text(data).encode()).hexdigest()
        changed = prev.get("text_sha") != text_sha
    ts = now_utc()
    rec = {"ts": ts.isoformat(timespec="seconds"), "source": source, "url": url, "status": 200,
           "bytes": len(data), "sha256": sha, "changed": changed}
    if changed:
        day_dir = RAW / source / ts.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        base = name or safe_name(url)
        fname = f"{ts.strftime('%H%M%S')}_{sha[:10]}_{base}{guess_ext(base, headers, data)}"
        path = day_dir / fname
        pii = pii or looks_pii(base, data)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if pii else 0o644)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        rec["path"] = str(path.relative_to(RAW))
    if changed and prev.get("path") and (RAW / prev["path"]).exists():
        try:
            summary = diff.compare(RAW, source, key, url, prev["path"], rec["path"], pii, rec["ts"])
            print(f"    changed: {diff.one_line(summary)}")
        except Exception as e:  # a diff failure must never stop collection
            print(f"    diff failed: {type(e).__name__}: {e}")
    state[key] = {"sha256": sha, "text_sha": text_sha or prev.get("text_sha"), "etag": headers.get("ETag") or headers.get("etag"),
                  "last_modified": headers.get("Last-Modified") or headers.get("last-modified"),
                  "last_seen": rec["ts"], **({"path": rec["path"]} if changed else {"path": prev.get("path")})}
    log(rec)
    return changed


def fetch_and_store(source: str, url: str, state: dict, pii: bool = False, name: str | None = None) -> bytes | None:
    key = f"{source}|{url}"
    browser = source in BROWSER_SOURCES and BROWSER_HOST
    remote = source in REMOTE_SOURCES and REMOTE_HOST and not browser
    try:
        if browser:
            status, body, hdrs = browser_get(url, BROWSER_HOST)
        elif remote:
            status, body, hdrs = curl_get(url, REMOTE_HOST, state.get(key))
        else:
            status, body, hdrs = http_get(url, state.get(key))
    except Exception as e:  # network error: log and move on
        log({"ts": now_utc().isoformat(timespec="seconds"), "source": source, "url": url,
             "status": None, "error": type(e).__name__ + ": " + str(e)[:200]})
        print(f"  ! {source}: {url} -> {type(e).__name__}")
        return None
    if status == 304:
        log({"ts": now_utc().isoformat(timespec="seconds"), "source": source, "url": url,
             "status": 304, "changed": False})
        print(f"  = {source}: {safe_name(url)} (304)")
        return None
    if status != 200 or not body:
        log({"ts": now_utc().isoformat(timespec="seconds"), "source": source, "url": url,
             "status": status, "bytes": len(body)})
        via = f" (browser on {BROWSER_HOST})" if browser else (f" (via {REMOTE_HOST})" if remote else "")
        print(f"  ! {source}: {safe_name(url)} -> HTTP {status}, {len(body)} bytes{via}")
        return None
    changed = store(source, key, url, body, hdrs, state, name=name, pii=pii)
    print(f"  {'+' if changed else '='} {source}: {name or safe_name(url)} ({len(body):,} bytes)")
    return body


# ---------------------------------------------------------------- SOS (Civix EVR)
def sos_get(params: dict) -> tuple[bytes | None, str]:
    url = f"{SOS_BASE}/{params.pop('_ep', 'getFile')}?" + urllib.parse.urlencode(params, safe="/")
    status, body, _ = http_get(url)
    if status != 200:
        raise RuntimeError(f"HTTP {status}")
    if not body.strip():
        return b"", url  # Civix returns empty body for empty rosters
    return base64.b64decode(json.loads(body)["upload"]), url


def collect_sos(state: dict, rosters: bool) -> None:
    print("SOS (Civix EVR)")
    idx_bytes, url = sos_get({"type": "EVR_ELECTION"})
    store("sos", "sos|index", url, idx_bytes, {"Content-Type": "application/json"}, state, name="EVR_ELECTION.json")
    idx = json.loads(idx_bytes)
    election = next(e for e in idx["elections"] if str(e["id"]) == SOS_ELECTION_ID)
    today = now_utc().astimezone(dt.timezone(dt.timedelta(hours=-5))).date()  # Central (CDT)
    dates = [d["date"] for d in election["early_voting_dates"]
             if dt.datetime.strptime(d["date"], "%m/%d/%Y").date() <= today]
    county_ids = {c["name"]: c["county_id"] for c in election["counties"]}

    for d in dates:
        try:
            data, url = sos_get({"type": "EVR_EARLYVOTING", "electionId": SOS_ELECTION_ID, "electionDate": d})
        except Exception as e:
            print(f"  ! turnout {d}: {e}")
            log({"ts": now_utc().isoformat(timespec="seconds"), "source": "sos", "url": f"turnout {d}", "error": str(e)})
            continue
        iso = dt.datetime.strptime(d, "%m/%d/%Y").strftime("%Y-%m-%d")
        ch = store("sos", f"sos|turnout|{iso}", url, data, {"Content-Type": "application/json"}, state,
                   name=f"turnout_{SOS_ELECTION_ID}_{iso}.json")
        print(f"  {'+' if ch else '='} sos turnout {iso}")
        time.sleep(0.5)

    if not rosters:
        return
    for d in dates:
        iso = dt.datetime.strptime(d, "%m/%d/%Y").strftime("%Y-%m-%d")
        for cname, slug in COUNTIES.items():
            try:
                data, url = sos_get({"_ep": "getFileByFormat", "type": "EVR_EARLYVOTING",
                                     "electionId": SOS_ELECTION_ID, "electionDate": d,
                                     "county": cname, "countyId": county_ids[cname], "format": "csv"})
            except Exception as e:
                print(f"  ! roster {slug} {iso}: {e}")
                continue
            ch = store("sos-roster", f"sos|roster|{slug}|{iso}", url, data, {"Content-Type": "text/csv"}, state,
                       name=f"roster_{SOS_ELECTION_ID}_{slug}_{iso}.csv", pii=True)
            rows = max(data.count(b"\n") - 1, 0)
            print(f"  {'+' if ch else '='} sos roster {slug} {iso}: {rows:,} rows")
            time.sleep(0.5)


# ---------------------------------------------------------------- counties
HREF = re.compile(r"""href\s*=\s*["']([^"'#]+)["']""", re.I)


def ingest_inbox(slug: str, state: dict) -> None:
    folder = INBOX / slug
    if not folder.is_dir():
        return
    for f in sorted(folder.iterdir()):
        if not f.is_file() or f.name.startswith("."):
            continue
        data = f.read_bytes()
        if not data:
            continue
        # Firefox saves CivicPlus files under their content-disposition name, e.g.
        # "In-Person Check-ins 20261103 General_202610262308520537.html": the trailing number is the
        # upload timestamp, so strip it (and a " (1)" duplicate suffix) to keep one diff chain per file.
        stem = re.sub(r"\s*\(\d+\)$", "", f.stem)
        stem = re.sub(r"_\d{12,}$", "", stem)
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_")[:120] or "file"
        key = f"{slug}|inbox:{stem}"
        pii = any(h in f.name.lower() for h in PII_HINTS)
        changed = store(slug, key, f"inbox:{f.name}", data, {}, state, name=stem, pii=pii)
        print(f"  {'+' if changed else '='} {slug}: inbox {f.name} ({len(data):,} bytes)")
        f.unlink()


def dc_redirect(url: str, host: str) -> str | None:
    """Location of a single redirect without fetching the body, or None (404/error). Used to read
    a DocumentCenter View id's type+date off its 301 slug before deciding whether to download."""
    import shlex
    import subprocess
    curl = ["curl", "-sS", "--max-time", "30", "-o", os.devnull, "-w", "%{http_code} %{redirect_url}", url]
    cmd = curl if host in ("", "local") else \
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host, " ".join(shlex.quote(c) for c in curl)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=45)
    except Exception:
        return None
    code, _, loc = r.stdout.decode("utf-8", "replace").strip().partition(" ")
    return loc.strip() or None


def collect_bexar_docs(state: dict) -> None:
    """Discover and archive Bexar's dated DocumentCenter rosters (folder 245). See BEXAR_DC notes."""
    host = REMOTE_HOST if ("bexar" in REMOTE_SOURCES and REMOTE_HOST) else "local"
    scan = state.get("bexar|dc_scan", {})
    fetched = set(scan.get("fetched", []))
    frontier = int(scan.get("max_id", BEXAR_DC_FLOOR))
    if fetched and (now_utc().hour * 60 + now_utc().minute) % BEXAR_DC_EVERY_MIN >= 15:
        print("  - bexar docs: off-cycle (every %d min)" % BEXAR_DC_EVERY_MIN)
        return
    i = max(BEXAR_DC_FLOOR, frontier - BEXAR_DC_LOOKBACK)
    misses = probes = 0
    while misses < BEXAR_DC_404_STOP and probes < BEXAR_DC_MAX_PROBES:
        loc = dc_redirect(f"{BEXAR_DC}/DocumentCenter/View/{i}", host)
        probes += 1
        if loc:
            frontier = max(frontier, i)
            misses = 0
            m = BEXAR_DC_SLUG.search(loc)
            if m and i not in fetched:
                kind = "ev" if m[1].lower().startswith("early") else "mail"
                url = f"{BEXAR_DC}/ImageRepository/Document?documentID={i}"
                if fetch_and_store("bexar", url, state, pii=True, name=f"{kind}_{m[2]}") is not None:
                    fetched.add(i)
                time.sleep(0.3)
        else:
            misses += 1
        i += 1
        time.sleep(0.2)
    scan["max_id"] = frontier
    scan["fetched"] = sorted(fetched)
    state["bexar|dc_scan"] = scan


def collect_county(slug: str, state: dict) -> None:
    print(slug)
    ingest_inbox(slug, state)
    every = SOURCE_EVERY_MIN.get(slug, 0)
    if every and (now_utc().hour * 60 + now_utc().minute) % every >= 15:
        print(f"  - {slug}: off-cycle (every {every} min)")
        return
    for url in FIXED.get(slug, []):
        fetch_and_store(slug, url, state, pii=any(h in url.lower() for h in PII_HINTS))
    for page_url, pattern in PAGES.get(slug, []):
        every = PAGE_EVERY_MIN.get(slug, 0)
        if every and (now_utc().hour * 60 + now_utc().minute) % every >= 15:
            continue  # off-cycle for this page; the FIXED urls above are still fetched every run
        body = fetch_and_store(slug, page_url, state)
        if body is None:  # unchanged (304) or failed: reuse last stored page for link discovery
            prev = state.get(f"{slug}|{page_url}", {}).get("path")
            if not prev or not (RAW / prev).exists():
                continue
            body = (RAW / prev).read_bytes()
        text = body.decode("utf-8", "replace")
        links = []
        for h in HREF.findall(text):
            u = urllib.parse.urljoin(page_url, html.unescape(h.strip()))
            if re.search(pattern, u, re.I) and (re.search(DOC_EXT, u, re.I) or "DocumentCenter/View/" in u):
                links.append(u)
        for u in dict.fromkeys(links):
            if u in FIXED.get(slug, []):
                continue
            fetch_and_store(slug, u, state, pii=any(h in u.lower() for h in PII_HINTS))
            time.sleep(0.3)
    if slug == "bexar":
        collect_bexar_docs(state)


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rosters", action="store_true", help="also pull SOS per-county voter rosters")
    ap.add_argument("--only", default="", help="comma list: sos,travis,bexar,harris,dallas,hays,hidalgo,el-paso,cameron")
    a = ap.parse_args()
    only = {s.strip() for s in a.only.split(",") if s.strip()}

    RAW.mkdir(parents=True, exist_ok=True)
    os.chmod(RAW, 0o750)
    lock = RAW / ".lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode()); os.close(fd)
    except FileExistsError:
        if time.time() - lock.stat().st_mtime < 3 * 3600:
            print("another run is active; exiting"); return 0
        lock.unlink(); return main()

    state = load_state()
    started = now_utc()
    print(f"run {started.isoformat(timespec='seconds')}")
    try:
        if not only or "sos" in only:
            try:
                collect_sos(state, a.rosters)
            except Exception as e:
                print(f"  ! SOS failed: {type(e).__name__}: {e}")
            save_state(state)
        for slug in ["harris", "bexar", "travis", "dallas", "hays", "hidalgo", "el-paso", "cameron"]:
            if only and slug not in only:
                continue
            try:
                collect_county(slug, state)
            except Exception as e:
                print(f"  ! {slug} failed: {type(e).__name__}: {e}")
            save_state(state)
    finally:
        save_state(state)
        lock.unlink(missing_ok=True)
    print(f"done in {(now_utc() - started).seconds}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
