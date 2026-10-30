# Adding a county (collector + parser)

This is the guide to teaching the Ledger a new source — a county we don't cover yet, or a
new file from one we do. It's the same five-step shape every time. Nothing here needs a
third-party package; the collector and parsers are Python standard library.

Before you start, read [`collector/README.md`](collector/README.md) and skim
[`collect.py`](collector/collect.py), [`match.py`](collector/match.py), and
[`dashboard_data.py`](collector/dashboard_data.py) — each has one clearly marked place you'll
edit. Throughout, a **slug** is the lowercase county name with spaces as hyphens
(`el-paso`), and a **VUID** is the voter's state ID number.

## The ground rules (don't skip these)

- **Aggregate out, voter-level never.** Roster files carry names and VUIDs. They live under
  `data/` at mode `0600`, are git-ignored, and the match emits counts only. Nothing
  voter-level is logged, committed, or published. Pass `pii=True` when you store a roster.
- **Label the definition.** A county's "early voters" might be check-ins, cast ballots, or
  mail *received* vs *accepted* — these differ. Every county gets a one-line definition
  (`COUNTY_DEFS`) so a gap is never read as fraud when it's a difference in what's counted.
- **Don't disguise the client.** If a site blocks automated access, we route through an
  allowed path (a US host, or a supervised human browser) — we never spoof a user agent or
  impersonate a person to get past a block.
- **Append-only.** We keep every version we fetch and never overwrite. That's what makes the
  revision history possible; don't defeat it.

## Step 1 — Collect the file(s): `collect.py`

Pick the mechanism that fits how the county publishes. Add your slug to the main loop list
in `main()` and, if the SOS covers the county, to the `COUNTIES` map (Civix name → slug) so
the state side is pulled too.

**(a) Fixed URLs** — the file lives at a stable address:

```python
FIXED = {
    "newcounty": [
        "https://county.example.gov/early-voting-roster.csv",
    ],
}
```

**(b) An index page with links** — scrape the page for document links matching a pattern
(the regex is matched against the full URL; `DOC_EXT` or a `DocumentCenter/View/` link is
also required):

```python
PAGES = {
    "newcounty": [("https://county.example.gov/elections/results/",
                   r"uploads/2026/1[01]/.*(Roster|Turnout|Mail)")],
}
```

**(c) Something weird** — a JavaScript-rendered index, an id-scan, etc. Write a small
function and call it from `collect_county()` for your slug. `collect_bexar_docs()` is the
worked example: the folder index had no links, so it discovers documents by number off a
redirect that reveals each file's type and date. Copy that shape.

Knobs you may need:
- `PAGE_EVERY_MIN[slug]` — fetch an index less often than every run (Hidalgo uses 60; its
  host returns HTTP 429 if polled harder).
- `REMOTE_FETCH_SOURCES` / `BROWSER_SOURCES` — fetch via a US SSH host, or a real browser,
  for sites that block this box. Configured in an untracked `collector/env`.
- **Inbox** (`data/inbox/<slug>/`) — for a supervised/manual drop when nothing automated
  works (see Hays). `ingest_inbox()` picks files up on the next run.
- **PII flag** — pass `pii=True` to `fetch_and_store(...)`, or let `PII_HINTS`/`looks_pii`
  catch it. Either way a roster is stored `0600`.

Give the stored file a stable, descriptive name if the URL doesn't (the `name=` argument),
so your parser can glob for it by type and date.

Test collection alone:

```
python3 collector/collect.py --only newcounty
```

Confirm the file lands under `data/raw/newcounty/<date>/` at `0600`.

## Step 2 — Parse it to VUIDs: `match.py`

Write `county_newcounty()` returning a dict of `VUID -> (date, method)`. Use `add(out, vuid,
date, method)` — it validates the VUID (`vuid_ok`) and keeps the first-seen date, so you can
union many daily files safely.

```python
def county_newcounty() -> dict:
    out: dict = {}
    f = latest("newcounty/*/*early-voting-roster*.csv")   # newest stored version
    if f:
        with open(f, encoding="utf-8-sig", errors="replace", newline="") as fh:
            for r in csv.DictReader(fh):
                add(out, r.get("VUID", ""), us_date(r.get("Vote Date", "")), "in-person")
    # ... repeat for the mail file, method="mail"
    return out
```

Helpers you'll reuse:
- `latest("glob")` — newest file matching a glob under `data/raw/`.
- `diff.xlsx_rows(bytes)` — stdlib XLSX reader that returns **all worksheets** concatenated
  as string rows (so a multi-tab workbook is fully read).
- `us_date("M/D/YYYY")` and `excel_date(serial-or-text)` — normalize to `YYYY-MM-DD`.
- `vuid_ok(v)` — true for an 8–11 digit ID.

Patterns already in the file to copy from:
- **CSV by column name** → `county_hidalgo` / `county_el_paso`.
- **XLSX, header row not first** → `county_dallas` (finds the header by looking for its
  column names; also shows handling a mid-election file rename).
- **Zip of many dated files, unioned** → `county_harris` (reads *every* CSV in the zip) and
  `county_travis` (keys off each filename, skips the roster types with no VUID).
- **PDF** → `county_bexar` (`pdftotext -layout`, one VUID token per row).
- **Rows embedded in HTML** → `county_hays`.

Then register it and add the definition and the slug:

```python
COUNTIES = [..., "newcounty"]                     # near the top
COUNTY_DEFS["newcounty"] = "in-person: ... ; mail: ..."   # the labeled definition
COUNTY_PARSERS["newcounty"] = county_newcounty
```

Test the match into a scratch dir (so you don't disturb the live one):

```
python3 collector/match.py --only newcounty --out /tmp/match-test
```

Sanity-check: `both` should dominate, `sos_only` should be a small, steady per-day stream
(like the other counties), and `county_only` reflects timing/definition lag. If `sos_only`
is huge, you're probably parsing a stale or partial file — check the `by date` breakdown.

## Step 3 — Put it on the dashboard: `dashboard_data.py`

Add the slug to `ORDER` and a display name to `NICE`. That's it — `has_county_roster` and
every tile, card, and the match table derive from the match output automatically.

```python
ORDER = [..., "newcounty"]
NICE  = {..., "newcounty": "New County"}
```

## Step 4 — Friendly label: `frontend/dashboard/index.html`

Add a plain-language definition to the `DEFS` map (the card renders it). Everything else on
the page is generic and needs no change.

```js
const DEFS = { ..., newcounty: "In-person: daily early-voting roster (check-ins). Mail: ballots received." };
```

## Step 5 — Run the pipeline and verify

```
python3 collector/collect.py --only newcounty        # fetch
python3 collector/match.py                            # full match (all counties)
python3 collector/dashboard_data.py                   # rebuild data.json
```

Open the dashboard and confirm the new card reads sensibly, then wire it into cron the way
the others run (collector every 15 min; the roster pull and match on their own cadence).

## A note on parsers for sources you *can't* reach

Some counties block automated access outright. We do not work around that by pretension —
if a county can't be fetched cleanly, the honest options are a supervised pull (a person
loads the files, a script reuses that session — see Hays) or asking the county to allow the
fetcher through. A pull request that disguises the client as a human browser won't be
merged. A new *parser* for data you obtained legitimately is always welcome.

---

Questions, or a county you've built and want folded in? There's a contact link on the
[dashboard](https://ledger.precinctlabs.com). If you replicate the whole thing independently
and your numbers disagree with ours, even better — tell us.
