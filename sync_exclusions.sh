#!/bin/bash
# Commits and pushes exclusions.txt if it has changed since the last run.
# Meant to run daily via cron on the Pi, where the running app is the only
# thing that modifies this file.
set -euo pipefail
cd "$(dirname "$0")"

# Pick up any commits pushed from elsewhere before we commit, so we never
# end up diverged from origin/main. Safe because this script only ever
# touches exclusions.txt, so rebasing our own commits onto origin/main
# cannot conflict with commits made elsewhere.
git fetch origin main
git rebase --autostash origin/main

if ! git diff --quiet -- exclusions.txt; then
    git add exclusions.txt
    git commit -m "Auto-update exclusions.txt"
    if ! git push origin main; then
        # Someone else pushed in the meantime; rebase once more and retry.
        git fetch origin main
        git rebase origin/main
        git push origin main
    fi
fi
