---
review:
  spec_hash: 3c71c3a2097a399a
  last_run: 2026-07-01
  phases:
    structure: {status: passed}
    coverage: {status: passed}
    clarity: {status: passed}
    consistency: {status: passed}
  findings: []
chain:
  intent: null
---
# Ensure base freshness before write — design

**Date:** 2026-07-01
**Status:** Approved (brainstorming)
**Topic:** `ensure-fresh-before-write`

## Problem

Every mutating tool (`wiki_write_page`, `wiki_update_page`, `wiki_delete_page`,
`wiki_reindex`, `wiki_create_domain`) commits locally first, then pushes via
`sync.commit_and_push` → `auto_commit` + `sync` (`pull --rebase` + `push`).

The `pull --rebase` therefore happens **after** the local commit. When the base
repo is behind its remote (e.g. another machine pushed since this checkout last
synced), the new local commit is built on a stale base. The subsequent
`pull --rebase` must replay it onto the remote tip, which frequently conflicts on
the generated `.iwiki/index.jsonl`. On conflict `sync` aborts the rebase; the
local commit stays unpushed and the base diverges from the remote — the classic
"switch machines → merge conflict" failure.

## Goal

Pull remote changes **before** a local mutation so the write lands on the current
remote tip and the eventual push is a fast-forward — no rebase over a stale base,
no `index.jsonl` conflict from staleness. When the base has already diverged,
refuse the write with a clear hint instead of stacking another commit onto the
divergence.

Non-goals: touching `wiki_sync` (it already pulls before push); caching
freshness across calls; auto-resolving genuine divergence.

## Design

### 1. `sync.ensure_fresh(base, timeout=15.0) -> dict`

New fail-soft function in `src/iwiki_mcp/sync.py`. Fetches the remote and, when
safe, fast-forwards the base. Never raises. Returns `{"state": <state>, ...}`
with an optional `warning`. Acquires `base_lock` like `sync()`.

Ahead/behind counts come from:

```
git rev-list --left-right --count @{upstream}...HEAD
```

(left = behind, right = ahead).

| State         | Condition                                   | Action                              |
|---------------|---------------------------------------------|-------------------------------------|
| `no_repo`     | base is not a git repo                       | proceed                             |
| `no_remote`   | no remote configured                         | proceed                             |
| `no_upstream` | branch has no `@{upstream}`                  | proceed + warning                   |
| `offline`     | `git fetch` failed                           | proceed + warning                   |
| `up_to_date`  | behind = 0, ahead = 0                        | proceed                             |
| `ahead`       | ahead > 0, behind = 0                        | proceed (push fast-forwards later)  |
| `updated`     | behind > 0, ahead = 0, tree clean            | `git merge --ff-only` → proceed     |
| `dirty`       | behind > 0, tree not clean                   | skip ff, proceed + warning          |
| `diverged`    | ahead > 0 **and** behind > 0                 | **BLOCK** — caller refuses          |

Notes:
- `--ff-only` guarantees no merge commit and no history rewrite.
- `dirty` should not normally happen (handlers auto-commit), so it degrades to a
  warning rather than a hard stop.
- All git failures degrade to `offline`/warning, matching the module's fail-soft
  philosophy.

### 2. Handler integration

Call `sync.ensure_fresh(bind.base)` at the top of each of the **five** mutating
handlers — after binding/base resolution, **before** any validate/write/index
step:

- `wiki_write_page`, `wiki_update_page`, `wiki_delete_page` (primary target)
- `wiki_reindex` (rewrites `index.jsonl` — freshness critical)
- `wiki_create_domain` (another machine may have created the same domain)

Behavior:

- `state == "diverged"` → return immediately, before touching the filesystem:

  ```json
  {"error": "base diverged from remote",
   "hint": "run wiki_sync to reconcile (pull --rebase + push), or resolve the conflict in the base repo, then retry"}
  ```

  No `.md`, no ingest-log line, no index record is written — identical to the
  transactional-rollback guarantee (zero side effects on refusal).

- any other state → proceed. Any `warning` from `ensure_fresh`
  (`offline` / `dirty` / `no_upstream`) is surfaced on the final result dict
  next to `committed` / `pushed`.

`wiki_sync` is unchanged.

### 3. Tests

Follow the project pattern: temp git repo, `monkeypatch indexer.embed_texts`,
dummy `IWIKI_*` env (see `tests/test_server_write.py::_seed`). No network.

Divergence fixture: two clones (A, B) of one bare remote. Commit+push from A,
commit in B → B is ahead **and** behind.

Unit (`ensure_fresh`):
- clean + behind → `"updated"`, HEAD advanced to remote tip.
- diverged → `"diverged"`, base unchanged.
- up-to-date → `"up_to_date"`.
- no remote → `"no_remote"`.
- ahead only → `"ahead"`.

Handler level:
- diverged base + `wiki_write_page` → returns `error`/`hint`, **zero side
  effects** (no new file, log line, or index record).
- behind base + `wiki_write_page` → base fast-forwarded, page written, push
  succeeds.

### 4. Docs & version

- **Wiki** (`docs/wiki/`): update the sync page (or `architecture.md`) with the
  new pre-write freshness contract via the iwiki MCP tools
  (`wiki_update_page`, then `wiki_lint`).
- **README**: add a line about pull-before-write only if it already documents
  write-time git behavior; do not invent a new section.
- **CLAUDE.md** (project): note in the transactional-write / git section that
  mutating handlers run `ensure_fresh` first and refuse on divergence.
- **Version**: patch bump in `pyproject.toml` (`0.1.4` → `0.1.5`).
- **docs/TODO.md**: one row for topic `ensure-fresh-before-write` via
  `/check-chain`.

## Branch

`dev-ensure-fresh-before-write` off fresh `master`, worked in place (no
worktree). PR into `master`.
