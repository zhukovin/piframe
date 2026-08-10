# Claude Code Memory Setup

This project's Claude Code memory is stored in `.claude/memory/` and tracked
in git, so it survives moving to a new machine, a hardware failure, or
cloning this repo somewhere else.

## What is this?

Claude Code (the CLI) keeps a small set of curated notes about this project
— decisions made, your preferences, open TODOs, reference links — in a
`memory/` folder under `~/.claude/projects/<mangled-path-to-this-repo>/`.
That location is normally *not* part of this repo and *not* backed up
anywhere, so it's lost if this machine dies or you start working from
somewhere else.

To fix that, `~/.claude/projects/.../memory` is a **symlink** into
`.claude/memory/` in this repo. Claude reads and writes memory exactly as
before — it has no idea the location is a symlink — but the actual files now
live in git, alongside the rest of this project.

**Not included:** Claude's full raw conversation transcripts (the `.jsonl`
files used by `/export`, `/resume`, etc.) live in that same
`~/.claude/projects/...` directory but are **not** linked or backed up here
— they're large, unstructured, and not meant to be portable. Only the small,
curated `memory/` folder is.

## `scripts/link-memory.sh`

Recreates the symlink described above. It derives the expected
`~/.claude/projects/<mangled-path>/` location from wherever this repo
actually lives on disk (by replacing every `/` with `-` in the repo's
absolute path), so it works no matter what machine, username, or directory
you've cloned this into — there's nothing to edit.

**When to run it:**
- Right after cloning this repo on a new machine, so Claude picks up all
  prior project memory immediately in your first session there.
- After restoring this repo from a backup onto replacement hardware.
- It's idempotent and safe to re-run any time — if the symlink already
  exists and points to the right place, it does nothing.

```bash
./scripts/link-memory.sh
```

If it errors saying a real directory already exists at the target location
(not a symlink), that means Claude already wrote some fresh memory there
under an unlinked path before you ran this script. Back that directory up
somewhere safe, remove it, then re-run the script.

## A note on secrets

Memory files (and other project docs Claude writes) can end up containing
credentials, tokens, or other sensitive details captured during a session.
That's fine for a private repo you control, but worth checking before making
this repo public or adding collaborators — grep `.claude/memory/` and rotate
anything sensitive first.
