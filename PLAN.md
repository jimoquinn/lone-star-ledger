# Lone Star Ledger (ledger.precinctlabs.com): Texas Early Vote, County vs. Secretary of State — Plan

_Drafted 2026-10-27. Early voting runs Oct 19–30 (the SOS shows mail votes from Oct 14). Election Day is Nov 3._

## 1. The core idea

For each of the 8 counties (Travis, Bexar, Harris, Dallas, Hays, Hidalgo, El Paso, Cameron), we track what the **county says** and what the **SOS (Civix EVR) says**, day by day. We show the gap and how it changes over time.

**Key finding from the first pull:** the SOS goes back and revises earlier days.
- TexPolls had Bexar at 221,547 in-person votes through Oct 24 on the SOS feed.
- Today the same SOS feed shows 262,140 for that date.

So we need an append-only time series, not a copy of the latest numbers. That means storing every fetch with its timestamp and file hash, and never overwriting. With that history we can show both "how big is the gap" and "how long did the SOS take to catch up". Those two are the real story.

## 2. Data sources

| Source | Endpoint / location | Format | Update behavior | How we collect it |
|---|---|---|---|---|
| **SOS** | `goelect.txelections.civixapps.com/api-ivis-system/api/v1/getFile?type=EVR_EARLYVOTING&electionId=53815&electionDate=MM/DD/YYYY` | base64 JSON, all 254 counties | Earlier dates get revised | Hourly, fetch every EV date, hash the result, store it if it changed |
| SOS roster | `getFileByFormat?...&county=X&countyId=N&format=csv` | CSV (VUID, name, method, precinct) | Daily | Daily, 8 counties (phase 2) |
| **Harris** | `appfiles.harrisvotes.com/.../EVPA_unofficial.pdf` | PDF, sites × days | Same URL overwritten daily | Hourly, parse PDF |
| **Travis** | `votetravis.gov/wp-content/uploads/G26-Daily-Totals-MMDDYYYY.pdf` + `G26-Voter-Rosters.zip` | PDF, ZIP | New file each day | Fetch by date pattern |
| **Bexar** | `earlyvotecountsdual.bexar.org` | HTML table (in-person only) | Shows today only; no history | Hourly snapshot, sum the site rows |
| **Dallas** | dallascountyvotes.org Nov 3 2026 page → 3 XLSX (turnout by date/location, in-person list, mail returns) | XLSX | New file each day; names unpredictable | Scrape the page for links |
| **Hays** | hayscountytx.gov DocumentCenter 5414 (mail), 5454 (in-person roster) | XLSX/CSV | Overwritten | Fetch fixed IDs; may need Playwright |
| **Hidalgo** | hidalgocounty.us DocumentCenter 73361 (EV-SUBS totals), 73363 / 73301 (rosters) | XLSX/PDF | Overwritten | Fetch fixed IDs |
| **El Paso** | epcountyvotestx.gov current-election page → S3 PDFs/XLSX/CSV | PDF, XLSX, CSV | New S3 ID each day | Scrape the page for links; posted before 11am MT |
| **Cameron** | cameroncountytx.gov/elections/wp-content/uploads/2026/10/01_Cameron_County<MM-DD-YYYY>.pdf | PDF, sites × days | New file each day | Fetch by date pattern |

Phase 1 compares **cumulative in-person**, **cumulative mail**, and **total** by EV date. Phase 2 adds roster-level comparison (VUIDs in the county roster but missing from the SOS roster, and the reverse). That is where problems in TEAM itself would show up.

### First comparison (pulled 2026-10-27, approximate)

| County | Through | County in-person | SOS in-person | Gap |
|---|---|---|---|---|
| Harris | Oct 25 | 587,514 | 587,066 | +448 |
| Bexar | Oct 25 | 284,169 | 282,965 | +1,204 |
| Hidalgo | Oct 25 | 97,250 | 96,761 | +489 |
| Cameron | Oct 25 | 46,210 | 46,287 | −77 |

On Oct 26, the SOS has in-person data for **Harris only** (4,503 votes vs. the county's 79,474). The other 7 counties show 0 for that day on the SOS.

Mail is consistently higher on the county side:
- Harris: county 39,012 vs. SOS 33,077
- Hidalgo: county 4,098 vs. SOS 3,712

## 3. Architecture

```
            ┌────────────── LXC (Debian Trixie) ───────────────┐
 cron/      │ collector (Python)                               │
 systemd ──►│  fetch → raw/ (files, sha256) → parse → Postgres │
 timer      │ FastAPI read API (cached JSON, ETag)             │
            │ Caddy/nginx → Quasar SPA (static build)          │
            └──────────────────────────────────────────────────┘
```

- **Backend: Python 3.13 + FastAPI + SQLAlchemy/psycopg.** We follow the documented Civix API from Abstract-Data/texas-turnout-scraper. The repo has no license file, so we write our own small client rather than copying its code. Parsing uses pdfplumber (PDF), openpyxl (XLSX), and Playwright only where a site requires it.
- **Collector:** a single `collect` command run by a systemd timer every 30–60 minutes during EV. Each source is a small plugin with `fetch() -> raw bytes` and `parse(raw) -> rows`. Raw files are kept forever under `raw/<source>/<YYYY-MM-DD>/<ts>_<sha>.ext`, so we can re-parse when a parser is fixed.
- **Postgres schema (core):**
  - `source(id, kind[sos|county], county, url, ...)`
  - `fetch(id, source_id, fetched_at, http_status, sha256, path, changed bool)`
  - `observation(fetch_id, county, ev_date, metric[in_person_day|in_person_cum|mail_cum|total_cum|registered], value)`. This is append-only.
  - Views: `latest_by_source`, `gap_daily` (county vs. SOS per county × ev_date, using the latest observation of each), `revision_log` (every change in an SOS value for a past date).
  - Phase 2: `roster_voter(source, county, vuid, method, ev_date, precinct)`. VUIDs and names are stored but never exposed through the public API; the API returns counts only.
- **Frontend:** Quasar/Vue 3 SPA in the dark, flashy On Point style (navy background, gradient highlight headline, mono uppercase labels, chip filters), kept much cleaner:
  1. **Hero:** total gap across the 8 counties, last-updated time, "SOS is N days behind" chips.
  2. **County cards** (8): county vs. SOS sparkline, gap badge, last-reported date for each side.
  3. **Gap chart:** gap over EV days per county, toggle in-person / mail / total.
  4. **Revision timeline:** how SOS numbers for a given day changed between fetches.
  5. **Ledger:** a plain sortable table with every number and a link to its source file, plus CSV download.
  6. **Context:** a short "what is TEAM / why this matters" panel linking news coverage.
- **Deploy:** an idempotent `bootstrap.sh` (pct create, apt, Postgres, venv, systemd units, Caddy) that works on the LAN dev container now and the datacenter Proxmox later. The code lives in a GitLab repo.

## 4. Timeline (aggressive)

| When | Deliverable |
|---|---|
| **Today (Oct 27)** | Collector for SOS + the 4 easy counties (Bexar HTML, Hidalgo, Cameron, Harris PDF) archiving raw files hourly. Starting the archive early matters more than anything else. |
| Oct 28 | Remaining parsers (Travis, Dallas, El Paso, Hays), Postgres load, gap views |
| Oct 29 | FastAPI + first Quasar dashboard on the dev container |
| Oct 30 | Polish, public deploy, bootstrap.sh for the datacenter |
| Oct 31–Nov 3 | Post-EV revisions continue; add Election Day turnout (`EVR_ELECTIONDAYTURNOUT`) |
| After | Roster-level matching (phase 2) |

## 5. Constraints and notes

- The SOS and county sites can't be reached from Claude's cloud workspace. All collection must run on the LXC (or the linked desktop).
- PII: the public dashboard shows aggregates only. Never log VUIDs or names.
- Each county has its own definition of "voted" (check-ins vs. ballots cast; mail received vs. accepted). The dashboard will label each source's definition so gaps aren't overstated.
