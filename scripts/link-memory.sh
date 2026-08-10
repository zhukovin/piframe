#!/bin/sh
# Links this repo's .claude/memory/ into Claude Code's per-project memory
# location, so memory persists via git across machines and hardware restores.
#
# Claude Code stores per-project state under ~/.claude/projects/<mangled-path>/,
# where <mangled-path> is this directory's absolute path with every "/"
# replaced by "-". This script derives that automatically, so it works
# regardless of username or where you've cloned this repo.
#
# Usage: run once after cloning this repo on a new machine (or right after
# this was first set up).
set -eu

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MEMORY_SRC="$REPO_DIR/.claude/memory"
MANGLED="$(printf '%s' "$REPO_DIR" | tr '/' '-')"
CLAUDE_PROJECT_DIR="$HOME/.claude/projects/$MANGLED"
MEMORY_LINK="$CLAUDE_PROJECT_DIR/memory"

mkdir -p "$CLAUDE_PROJECT_DIR"
mkdir -p "$MEMORY_SRC"

if [ -e "$MEMORY_LINK" ] && [ ! -L "$MEMORY_LINK" ]; then
  echo "error: $MEMORY_LINK already exists as a real directory (not a symlink)." >&2
  echo "back up and remove it manually, then re-run this script." >&2
  exit 1
fi

ln -sfn "$MEMORY_SRC" "$MEMORY_LINK"
echo "Linked $MEMORY_LINK -> $MEMORY_SRC"
