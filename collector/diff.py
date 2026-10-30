"""Work out what changed between two stored versions of the same source file.

Called by collect.py whenever a new version is archived. Writes:
  data/raw/changes.jsonl                  one summary line per new version (counts only, no PII)
  data/raw/diffs/<source>/<date>/...      detail files; roster details are mode 0600

Handles: SOS turnout JSON, SOS election index, voter rosters (CSV / XLSX / ZIP of
those, matched on VUID), HTML pages, PDFs (via `pdftotext` if installed), any text.
"""
from __future__ import annotations

import csv
import difflib
import io
import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

VUID_HEADERS = re.compile(r"^(vuid|id_voter|voter_?id|voter id|voterid|sos_?vuid|sos voter id|vuid_?number|state voter id)$", re.I)
NUMERIC_ID = re.compile(r"^\d{7,10}(\.0)?$")
METHOD_HEADERS = re.compile(r"(voting_?method|method|ballot_?type|vote_?type|status)", re.I)
PRECINCT_HEADERS = re.compile(r"(precinct|pct)", re.I)


# ------------------------------------------------------------------ readers
def xlsx_rows(data: bytes) -> list[list[str]]:
    """Minimal stdlib XLSX reader: all sheets, concatenated, values as strings."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    z = zipfile.ZipFile(io.BytesIO(data))
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns):
            shared.append("".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t")))
    rows: list[list[str]] = []
    for name in sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")):
        for r in ET.fromstring(z.read(name)).iter(f"{{{ns['m']}}}row"):
            vals: dict[int, str] = {}
            for c in r.findall("m:c", ns):
                ref = c.get("r", "")
                col = 0
                for ch in re.match(r"[A-Z]*", ref).group(0):
                    col = col * 26 + (ord(ch) - 64)
                v = c.find("m:v", ns)
                t = c.get("t")
                if t == "s" and v is not None:
                    val = shared[int(v.text)]
                elif t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter(f"{{{ns['m']}}}t"))
                else:
                    val = v.text if v is not None else ""
                vals[col - 1 if col else len(vals)] = val or ""
            if vals:
                rows.append([vals.get(i, "") for i in range(max(vals) + 1)])
    return rows


def csv_rows(data: bytes) -> list[list[str]]:
    text = data.decode("utf-8-sig", "replace")
    return list(csv.reader(io.StringIO(text)))


def table_rows(path: Path, data: bytes) -> list[list[str]] | None:
    ext = path.suffix.lower()
    try:
        if ext in (".xlsx", ".xlsm") or data[:2] == b"PK" and b"xl/" in data[:2000]:
            return xlsx_rows(data)
        if ext in (".csv", ".txt"):
            return csv_rows(data)
    except Exception:
        return None
    return None


def pdf_text(data: bytes) -> str | None:
    exe = shutil.which("pdftotext")
    if not exe:
        return None
    p = subprocess.run([exe, "-layout", "-", "-"], input=data, capture_output=True, timeout=120)
    return p.stdout.decode("utf-8", "replace") if p.returncode == 0 else None


def html_text(data: bytes) -> str:
    t = data.decode("utf-8", "replace")
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", t)
    t = re.sub(r"(?i)<\s*(tr|br|p|div|li|h\d)[^>]*>", "\n", t)
    t = re.sub(r"(?i)<\s*t[dh][^>]*>", " | ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in t.splitlines()]
    return "\n".join(ln for ln in lines if ln)


# ------------------------------------------------------------------ roster diff
def roster_index(rows: list[list[str]]) -> dict[str, tuple[str, str]] | None:
    """Return {vuid: (method, precinct)} if this table looks like a voter roster."""
    if len(rows) < 2:
        return None
    # header row = first row within the first 10 that has a VUID-ish header, else row 0
    hdr_i, vcol = 0, None
    for i, r in enumerate(rows[:10]):
        for j, h in enumerate(r):
            if VUID_HEADERS.match(h.strip()):
                hdr_i, vcol = i, j
                break
        if vcol is not None:
            break
    body = rows[hdr_i + 1:]
    if vcol is None:  # fall back: a column that is mostly 7-10 digit numbers
        width = max(len(r) for r in body[:200]) if body else 0
        for j in range(width):
            sample = [r[j].strip() for r in body[:500] if len(r) > j and r[j].strip()]
            if sample and sum(bool(NUMERIC_ID.match(s)) for s in sample) / len(sample) > 0.8:
                vcol = j
                break
    if vcol is None:
        return None
    hdr = rows[hdr_i] if hdr_i < len(rows) else []
    mcol = next((j for j, h in enumerate(hdr) if j != vcol and METHOD_HEADERS.search(h)), None)
    pcol = next((j for j, h in enumerate(hdr) if j != vcol and PRECINCT_HEADERS.search(h)), None)
    out: dict[str, tuple[str, str]] = {}
    for r in body:
        if len(r) <= vcol:
            continue
        v = r[vcol].strip()
        if not NUMERIC_ID.match(v):
            continue
        v = v.split(".")[0].zfill(10)
        out[v] = (r[mcol].strip() if mcol is not None and len(r) > mcol else "",
                  r[pcol].strip() if pcol is not None and len(r) > pcol else "")
    return out if len(out) >= max(1, len(body) // 2) else None


def roster_diff(old: dict, new: dict) -> tuple[dict, dict]:
    o, n = set(old), set(new)
    added, removed = sorted(n - o), sorted(o - n)
    meth = sorted(v for v in o & n if old[v][0] != new[v][0])
    pct = sorted(v for v in o & n if old[v][1] != new[v][1])

    def by_method(d, keys):
        c: dict[str, int] = {}
        for k in keys:
            c[d[k][0] or "?"] = c.get(d[k][0] or "?", 0) + 1
        return c

    summary = {"kind": "roster", "old_rows": len(o), "new_rows": len(n), "added": len(added),
               "removed": len(removed), "method_changed": len(meth), "precinct_changed": len(pct),
               "added_by_method": by_method(new, added), "removed_by_method": by_method(old, removed)}
    detail = {"added": [[v, *new[v]] for v in added], "removed": [[v, *old[v]] for v in removed],
              "method_changed": [[v, old[v][0], new[v][0]] for v in meth],
              "precinct_changed": [[v, old[v][1], new[v][1]] for v in pct]}
    return summary, detail


# ------------------------------------------------------------------ SOS JSON diffs
def sos_turnout_diff(old: dict, new: dict) -> tuple[dict, dict]:
    fields = ("registered_voters", "in_person_votes_on_date", "total_in_person_votes_for_election",
              "total_mail_votes_for_election")
    oc = {c["name"]: c for c in old.get("turnout_by_county", [])}
    changes = []
    for c in new.get("turnout_by_county", []):
        p = oc.get(c["name"])
        for f in fields:
            if p is None or p.get(f) != c.get(f):
                changes.append({"county": c["name"], "field": f, "old": None if p is None else p.get(f),
                                "new": c.get(f), "delta": (c.get(f) or 0) - ((p or {}).get(f) or 0)})
    missing = sorted(set(oc) - {c["name"] for c in new.get("turnout_by_county", [])})
    tot = lambda d, f: sum((c.get(f) or 0) for c in d.get("turnout_by_county", []))  # noqa: E731
    summary = {"kind": "sos_turnout", "date_updated": [old.get("date_updated"), new.get("date_updated")],
               "counties_changed": len({c["county"] for c in changes}), "field_changes": len(changes),
               "counties_missing": missing,
               "statewide_delta": {f: tot(new, f) - tot(old, f) for f in fields[1:]},
               "decreases": sum(1 for c in changes if c["delta"] < 0)}
    return summary, {"changes": changes}


def sos_index_diff(old: dict, new: dict) -> tuple[dict, dict]:
    oe = {e["id"]: e for e in old.get("elections", [])}
    ne = {e["id"]: e for e in new.get("elections", [])}
    det = {"added": [ne[i]["election_name"] for i in ne.keys() - oe.keys()],
           "removed": [oe[i]["election_name"] for i in oe.keys() - ne.keys()],
           "changed": []}
    for i in ne.keys() & oe.keys():
        for k in ("certified", "early_voting_dates", "election_date", "election_name"):
            if oe[i].get(k) != ne[i].get(k):
                det["changed"].append({"id": i, "field": k})
    return {"kind": "sos_index", **{k: len(v) for k, v in det.items()}}, det


# ------------------------------------------------------------------ text diff
def text_diff(a: str, b: str, name: str) -> tuple[dict, str]:
    ud = list(difflib.unified_diff(a.splitlines(), b.splitlines(), f"prev/{name}", f"new/{name}", lineterm="", n=1))
    plus = sum(1 for ln in ud if ln.startswith("+") and not ln.startswith("+++"))
    minus = sum(1 for ln in ud if ln.startswith("-") and not ln.startswith("---"))
    return {"kind": "text", "lines_added": plus, "lines_removed": minus}, "\n".join(ud)


# ------------------------------------------------------------------ entry point
def compare(raw_root: Path, source: str, key: str, url: str, prev_rel: str, new_rel: str,
            pii: bool, ts: str) -> dict:
    prev_p, new_p = raw_root / prev_rel, raw_root / new_rel
    a, b = prev_p.read_bytes(), new_p.read_bytes()
    rec = {"ts": ts, "source": source, "key": key, "url": url, "prev": prev_rel, "new": new_rel,
           "bytes": [len(a), len(b)]}
    detail: str | None = None
    detail_pii = pii
    ext = new_p.suffix.lower()
    try:
        if key.startswith("sos|turnout|"):
            s, d = sos_turnout_diff(json.loads(a), json.loads(b)); detail = json.dumps(d, indent=1)
        elif key == "sos|index":
            s, d = sos_index_diff(json.loads(a), json.loads(b)); detail = json.dumps(d, indent=1)
        elif ext == ".zip" and not (b"xl/" in b[:2000]):
            za, zb = zipfile.ZipFile(io.BytesIO(a)), zipfile.ZipFile(io.BytesIO(b))
            ma = {i.filename: i.CRC for i in za.infolist()}
            mb = {i.filename: i.CRC for i in zb.infolist()}
            members = {"added": sorted(mb.keys() - ma.keys()), "removed": sorted(ma.keys() - mb.keys()),
                       "changed": sorted(k for k in ma.keys() & mb.keys() if ma[k] != mb[k])}
            s = {"kind": "zip", **{k: len(v) for k, v in members.items()}, "members": members}
            ra: dict = {}
            rb: dict = {}
            for zf, acc in ((za, ra), (zb, rb)):
                for nm in zf.namelist():
                    rows = table_rows(Path(nm), zf.read(nm))
                    idx = roster_index(rows) if rows else None
                    if idx:
                        acc.update(idx)
            if ra or rb:
                rs, d = roster_diff(ra, rb); s["roster"] = rs; detail = json.dumps(d); detail_pii = True
        else:
            ta, tb = table_rows(prev_p, a), table_rows(new_p, b)
            ia = roster_index(ta) if ta else None
            ib = roster_index(tb) if tb else None
            if ia is not None and ib is not None:
                s, d = roster_diff(ia, ib); detail = json.dumps(d); detail_pii = True
            elif ta is not None and tb is not None:
                s, detail = text_diff("\n".join(",".join(r) for r in ta), "\n".join(",".join(r) for r in tb), new_p.name)
            elif ext == ".pdf":
                pa, pb = pdf_text(a), pdf_text(b)
                if pa is None or pb is None:
                    s = {"kind": "pdf", "note": "install poppler-utils (pdftotext) for PDF diffs"}
                else:
                    s, detail = text_diff(pa, pb, new_p.name); s["kind"] = "pdf_text"
            elif ext in (".html", ".htm", ".bin") or b"<html" in b[:2000].lower():
                s, detail = text_diff(html_text(a), html_text(b), new_p.name); s["kind"] = "html_text"
            else:
                s = {"kind": "binary"}
    except Exception as e:  # never let a diff failure stop collection
        s = {"kind": "error", "error": f"{type(e).__name__}: {e}"[:300]}
    rec["summary"] = s
    if detail:
        out = raw_root / "diffs" / new_rel
        out = out.with_name(out.name + (".json" if detail.lstrip().startswith(("{", "[")) else ".diff"))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(detail)
        if detail_pii:
            os.chmod(out, 0o600)
        rec["detail"] = str(out.relative_to(raw_root))
    with (raw_root / "changes.jsonl").open("a") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")
    return s


def one_line(s: dict) -> str:
    k = s.get("kind")
    if k == "roster":
        return f"roster {s['old_rows']:,}→{s['new_rows']:,} (+{s['added']:,} −{s['removed']:,}, method Δ{s['method_changed']}, pct Δ{s['precinct_changed']})"
    if k == "sos_turnout":
        sd = s["statewide_delta"]
        return (f"{s['counties_changed']} counties changed, {s['decreases']} decreases; statewide "
                f"in-person {sd['total_in_person_votes_for_election']:+,}, mail {sd['total_mail_votes_for_election']:+,}")
    if k in ("text", "pdf_text", "html_text"):
        return f"{k} +{s['lines_added']}/−{s['lines_removed']} lines"
    return json.dumps(s)[:160]
