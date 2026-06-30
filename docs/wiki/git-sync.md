# Git sync of the base

## Overview
`sync.py` keeps the shared wiki base in git: it auto-commits after successful writes and offers an explicit pull-rebase-push sync. Every operation is fail-soft — a non-repo base or a missing remote degrades to a warning in the result dict, never an exception. This is what lets a wiki base travel between machines and projects. Invoked by [[mcp-server#Write path]] and the `wiki_sync` tool.

## Auto-commit on write
`auto_commit(base, message)` stages and commits after a page write or domain create. It runs `git add -A`, checks `git status --porcelain`, and commits only when there is something to commit. The result reports `committed` (bool) plus a `warning` on any non-success: a non-repo base returns `committed: false` with a note, so the on-disk write still succeeds even when git does not.

## Explicit sync
`sync(base)` shares the base with a remote: `git pull --rebase` then `git push`. With no remote it warns that commits stay local. If the rebase conflicts it aborts (`git rebase --abort`) and returns an `error` plus a `hint` to resolve manually — re-running `wiki_index` regenerates a conflicted `.iwiki/index.jsonl` (see [[indexing#Index domain]]). It returns `pulled` and `pushed` flags.

## Repository detection
All git calls go through `_run`, which shells out with `git -C base` and a timeout, capturing output. `is_git_repo` tests `rev-parse --is-inside-work-tree`; `_has_remote` checks `git remote`; `_has_rebase_state` looks for a `rebase-merge`/`rebase-apply` dir to detect an in-progress rebase before aborting. Wrapping subprocess errors keeps the module from ever raising into [[mcp-server#Error handling]].
