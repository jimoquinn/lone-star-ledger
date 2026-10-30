# Lone Star Ledger

Tracks the gap between what Texas counties report for early voting and what the
Secretary of State (Civix EVR) reports for the same counties — day by day, and voter
by voter — for the November 3, 2026 general election. Live site:
**[ledger.precinctlabs.com](https://ledger.precinctlabs.com)**.

Two independent counts of the same early vote should agree. Where they don't — and the
state's silent revisions of past days — is the thing this watches. The full method is
written out on the site's [Methods and code](https://ledger.precinctlabs.com/technical.html)
page; this repo is that method's implementation.

## What it does

- **Collects** two counts every 15 minutes: the SOS Civix early-voting feed (turnout +
  per-county rosters) and each county's own published files, in whatever format they use.
- **Archives** every version it fetches, append-only. Several sources overwrite yesterday's
  file at the same URL, so a version missed is lost; nothing is overwritten on our side.
  This is what makes the state's past-day revisions visible.
- **Matches** the two rosters on voter-ID number to see which votes appear on both, which
  the state lists that a county's own roster does not, and the reverse — broken down by date
  and method, because the raw totals overstate the gap.
- **Publishes** a static dashboard of aggregate counts only.

Everything is Python standard library — no third-party packages to install.

## This is a reference implementation, not a turnkey app

It runs as our own rig and is published so the method is fully open and checkable, not as
a one-command install. Honest caveats before you try to run it:

- **Some sources need operator-specific setup.** Dallas serves a CAPTCHA to non-US
  addresses, so it's fetched through a US host over SSH (a host alias you'd supply in an
  untracked `collector/env`). **Hays** is behind a security vendor that blocks automated
  clients; it's collected by a *supervised* pull — a person opens the files in their own
  browser and a script reuses that session — so it needs a human and a desktop, and can't
  be fully automated. We don't disguise the client to get around either block.
- **No data is included.** The archive, rosters, match output, and voter-history files are
  git-ignored and never committed (see Privacy below). You point it at the live public
  sources yourself.
- **It assumes a dev box + cron.** Collection and deploy run from cron on one machine.

If you want to replicate the work, the highest-value path is usually to **read the method
and build your own** against the same public sources — independent implementations catch
bugs a copied one won't. But copy this freely; that's the point.

**Adding a county we don't cover?** [`CONTRIBUTING.md`](CONTRIBUTING.md) walks through the
five extension points — collect, parse, match, dashboard, label — with a worked example for
each shape of source (CSV, multi-sheet spreadsheet, zip of daily files, PDF, embedded HTML).

## Layout

- `collector/` — the stdlib collector, parsers, VUID match, party-lean, and deploy script.
  Start with `collector/README.md`, then `collect.py`, `match.py`, `dashboard_data.py`.
- `frontend/dashboard/` — the static site (`index.html`, `discovered.html`, `technical.html`)
  and the aggregate JSON the collector regenerates.
- `PLAN.md` — the original architecture and source notes.

## Data and privacy

The public site and this repo carry **aggregate counts only**. Voter names and ID numbers
never appear in logs, in git, or in anything published. Roster and voter-history files are
written at owner-only permissions and are git-ignored; the voter-by-voter match reads ID
numbers only and emits counts. The one tracked data file is an aggregate county-turnout
snapshot.

## License

[MIT](LICENSE). Use it, change it, point it at other counties, races, or states, and run it
next to ours and compare. If you replicate it and your numbers disagree with ours, please
get in touch — a contradiction we can't reproduce is a bug we want to find.
