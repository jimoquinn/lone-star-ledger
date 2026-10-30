#!/usr/bin/env bash
# Collection (including Hays, which needs the dev box's own Chrome) all happens here on the dev
# container. This regenerates the dashboard data/series here and copies the static site up to the
# prod server, which only serves it. Prod dir /srv/ledger is owned by hc, so no sudo is needed.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 collector/dashboard_data.py >/dev/null
python3 collector/series.py >/dev/null
python3 collector/reg_churn.py >/dev/null
python3 collector/mail_gap.py >/dev/null
python3 collector/statewide.py >/dev/null
if ssh prod 'command -v rsync >/dev/null 2>&1'; then
  rsync -az --delete frontend/dashboard/ prod:/srv/ledger/
else
  tar czf - -C frontend/dashboard index.html discovered.html technical.html churn.html mail.html statewide.html data.json series.json reg_churn.json mail_gap.json statewide_ev.json \
    | ssh prod 'tar xzf - -C /srv/ledger'
fi
echo "deployed $(date -u +%FT%TZ) -> prod:/srv/ledger"
