# Collector

Every 15 minutes: fetch the Texas SOS early-vote files (Civix EVR, election 53815) and
the 8 counties' files. When a file's bytes change (sha256), the new version is archived
and diffed against the previous one. Python 3 stdlib only.

## Status (2026-10-27)

Live on the dev LXC via cron since 2026-10-27 12:15 UTC. Working: SOS turnout + rosters, Harris, Bexar,
Travis, Hidalgo, El Paso, Cameron, Dallas (via the prod box). Hays is blocked: CivicPlus/Cloudflare hard-blocks
datacenter addresses and, after a few fetches, the dev box's residential address too, even for a real headless Chromium.
Hays files arrive through the manual inbox (below); the browser path is probed only every 6 hours.
HTML index pages are archived only when their visible text changes (nonces change on every load).

## Remote and browser fetches

`collector/env` (git-ignored, sourced by run.sh) can set:

- `REMOTE_FETCH_HOST=prod`: fetch Dallas and Hays with curl on that SSH host (US address). Dallas needs this.
- `BROWSER_SOURCES=hays` + `BROWSER_FETCH_HOST=local` (or an SSH host): fetch Hays through headless Chromium there, using `browser_fetch.py`
  deployed at `~/lsl/browser_fetch.py` with a venv at `~/lsl-venv` (`pip install playwright`,
  `playwright install --with-deps chromium`). Test by hand first:
  `~/lsl-venv/bin/python ~/lsl/browser_fetch.py https://www.hayscountytx.gov/DocumentCenter/View/5414`

## Manual inbox (Hays)

Save a file from a desktop browser into `data/inbox/<source>/` (e.g. `data/inbox/hays/5414.xlsx`,
`5454.xlsx`, `election-page.html`). The next run archives it under `data/raw/<source>/`, diffs it against the
last version with the same file name, and deletes it from the inbox. Use the same file name each day so the
diff history chains. `SOURCE_EVERY_MIN="hays=360"` (default) limits automated Hays fetches to one probe every
6 hours so the address is not blocked further; set `hays=0` to fetch every run again once the county allowlists us.

## Setup (once)

    sudo apt install -y poppler-utils        # pdftotext, for PDF diffs (Harris, Travis, Cameron, El Paso)
    cd ~/texas-voter-data-issues
    python3 collector/collect.py --rosters   # first full pull, a few minutes
    (crontab -l 2>/dev/null; echo '*/15 * * * * bash $HOME/texas-voter-data-issues/collector/run.sh') | crontab -

## What runs when

- Every 15 min: SOS election index + turnout for every EV date (all 254 counties), all county files.
- Every 30 min: SOS per-county voter rosters for the 8 counties. Change with
  `ROSTER_EVERY_MIN=15` in the crontab line.

## Where things land (data/raw/)

| Path | What |
|---|---|
| `<source>/<UTC date>/<HHMMSS>_<sha>_<name>` | each distinct version of each file, kept forever |
| `manifest.jsonl` | every fetch attempt: time, URL, HTTP status, bytes, hash, changed? |
| `changes.jsonl` | one line per new version: what changed, counts only |
| `diffs/...` | details: per-county field changes (SOS), VUIDs added/removed (rosters, mode 0600), text diffs |
| `state.json` | last hash/ETag per URL |

What gets compared:
- **SOS turnout:** every county field, with flags for any number that went down.
- **Rosters (SOS or county CSV/XLSX/ZIP):** voters added and removed, plus method and precinct changes, matched on VUID.
- **HTML pages and PDFs:** line diffs.

Logs are in `data/logs/`.

Roster files and roster diffs contain voter names/VUIDs: mode 0600, git-ignored, never printed.

## Parsing the archive (parse.py)

`parse.py` turns archived turnout files into observation rows (`source, county, ev_date,
metric, value, definition, file`; metrics `in_person_day`, `in_person_cum`, `mail_cum`,
`total_cum`, `registered`; cumulative values are "through ev_date"). It never touches
rosters, except Cameron's mail roster, from which only a count of rows per received date is taken.

    python3 -I collector/parse.py            # CSV of every parseable file under data/raw
    python3 -I collector/parse.py --check    # latest county cumulative vs SOS cumulative, same ev_date
    python3 -I collector/parse.py --list     # which files would be parsed

Handled: SOS turnout JSON, Harris EVPA PDF, Travis Daily Totals PDF, Cameron county PDF + mail XLSX,
El Paso stats PDFs (in-person and mail), Hidalgo EV-SUBS PDF, Bexar HTML. Dallas and Hays: not yet
(no files archived). `total_cum` is only emitted when one file carries both in-person and mail.
Each parser checks its day values against the PDF's TOTAL column and warns on stderr if they differ.

## VUID match (SOS roster vs county roster)

`python3 collector/match.py` (run.sh runs it hourly, log in `data/logs/match-*.log`) builds, per county, the set of
VUIDs in the latest version of every per-date SOS roster and the set in the county's own latest roster files, then
reports `both`, `sos_only` (SOS says they voted, county never listed them) and `county_only`, broken down by date and
method. Counts go to `data/match/<ts>/summary.{json,csv}`; the VUID lists are `<county>_sos_only.csv` and
`<county>_county_only.csv`, mode 0600, git-ignored, never served. Read the by-date breakdown before drawing
conclusions: the SOS roster for a day keeps filling for days afterwards, counties post a day's roster the next
morning, and SOS rosters begin Oct 14, so mail ballots received before then have no SOS roster row at all.
County definitions are in `COUNTY_DEFS` in match.py. Bexar publishes no roster we have found; Hays comes via the inbox.

## Party lean of the discrepancy lists (primary history)

Texas has no party registration, so lean is estimated from which primary a voter chose. `collector/history.py`
downloads Harris's official primary rosters (2020 EV/BBM, 2022, 2024, 2026 primaries and runoffs) into
`data/history/harris/` (0600, git-ignored). `python3 collector/lean.py build harris` streams them into
`primary_history.csv` (vuid,election,party). `python3 collector/lean.py apply harris` then splits each match list
(sos_only, county_only, by method, and sos_only in-person on completed days) into D / R / mixed / no-primary, next
to the same split for the whole SOS roster and the whole county roster, so a lean is read against its baseline.
Counts only are printed and saved as `<county>_lean.json`. Other counties: drop a `data/history/<county>/primary_history.csv`
with the same three columns (converted from the SOS statewide voter file with history) and run `apply`.

## Hays via your own Chrome (supervised)

`collector/hays_fetch.py` attaches over the DevTools protocol to a Chrome you started and clicked
through Cloudflare yourself, and reuses that real session to download the three Hays files into
`data/inbox/hays/`. It is not disguised and not headless; it only works while your browser is open
and past the challenge, so it is a supervised pull. Steps:

1. Fully quit Chrome, then start a dedicated debuggable instance (keeps it apart from normal browsing):
   `google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/.config/google-chrome-lsl"`
2. In that window open the Hays election page once and clear any Cloudflare check.
3. `./hays-pull.sh`  (runs hays_fetch.py then `collect.py --only hays`), or just `~/lsl-venv/bin/python collector/hays_fetch.py`
   and let the next scheduled run pick the files up.

The DocumentCenter files land under stable base names (absentee-received-5414, in-person-checkins-5454)
so their diff history chains day to day; the collector guesses each file's real extension from its bytes.
