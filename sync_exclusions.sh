#!/bin/bash
# Commits and pushes exclusions.txt if it has changed since the last run.
# Meant to run daily via cron on the Pi, where the running app is the only
# thing that modifies this file.
set -euo pipefail
cd "$(dirname "$0")"

if ! git diff --quiet -- exclusions.txt; then
    git add exclusions.txt
    git commit -m "Auto-update exclusions.txt"
    git push origin main
fi
