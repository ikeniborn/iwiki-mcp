---
review:
  spec_hash: 416bdf66544dc25a
  last_run: 2026-06-30
  phases:
    structure:    { status: passed }
    coverage:     { status: passed }
    clarity:      { status: passed }
    consistency:  { status: passed }
  findings:
    - id: F-001
      phase: coverage
      severity: WARNING
      section: "## Problem"
      section_hash: 00589f93278e2231
      fragment: "`write file -> append log -> reindex` is not atomic across processes."
      text: >-
        Problem symptom #4 (non-atomic write->log->reindex across processes) is listed
        as a git-race gap but the Concurrency design only wraps auto_commit (add+commit)
        and sync (pull/push) in base_lock; the write->log->reindex sequence in
        wiki_write_page runs before auto_commit and is not covered by the lock. The
        usage model (different write-domains -> domain-isolated index files) implicitly
        neutralizes it, but the design never states that this symptom is thereby resolved
        or out of scope.
      fix: >-
        Add one sentence either in the Concurrency design or Out of scope explicitly
        noting that symptom #4 is neutralized by the per-domain isolation assumption
        (each project writes its own <domain>/.iwiki/index.jsonl), or otherwise covered.
      verdict: fixed
      verdict_at: 2026-06-30
chain:
  intent: null
---
# Design: Concurrent base sync + `.iwikiignore`

Date: 2026-06-30
Status: approved (design), pending implementation plan

## Problem

`iwiki-mcp` runs as one stdio MCP server **per client session / project**. Many such
servers can share a single git-backed *base* directory. Two gaps surface under that
concurrency:

1. **Git races on the shared base.** Every `wiki_write_page` and `wiki_create_domain`
   calls `sync.auto_commit`, which runs `git add -A` + `commit` against the base repo
   with no locking. `wiki_sync` runs `pull --rebase` + `push`. With parallel projects:
   - `git add -A` stages the *entire* work tree, so process A captures process B's
     uncommitted files — commits get interleaved across domains.
   - Two concurrent `commit`s collide on git's `index.lock` (the second aborts).
   - Two concurrent `push`es collide (non-fast-forward).
   - `write file -> append log -> reindex` is not atomic across processes.

2. **No content filter.** There is a stub `engine.config._load_ignore` (compiles a
   gitignore-style `PathSpec`) and a `Config.ignore` field, but `Config.load` defaults
   to `load_ignore=False` and nothing ever requests it. There is no `.iwikiignore`
   creation, and no enforcement point. Agents can ingest source paths that should never
   reach the wiki (secrets, build noise).

## Scope & usage model (confirmed)

- Parallel projects write to **different write-domains** in **one shared base** repo.
  File contents never collide; only the shared git index and `push` collide.
- On lock contention: **wait with timeout**, and additionally **retry push** on
  non-fast-forward.
- Inter-process lock implemented with the **`filelock`** library.
- `.iwikiignore` purpose: keep **secrets** and **noise** out of the wiki, **seeded from
  `.gitignore`**. It is enforced server-side as a **gate on the `source=` argument** of
  `wiki_write_page` (the only place the server sees a project source path).
- The server never scans project sources; agents author finished markdown via
  `wiki_write_page(domain, slug, markdown, source=...)`.

## Architecture overview

Two independent, fail-soft features (consistent with the whole server: every handler
returns a JSON-serializable dict, exceptions become `{"error","hint"}`).

- **Concurrency:** inter-process lock on the base + commit scoped to the write-domain
  pathspec + push-retry.
- **`.iwikiignore`:** gitignore-syntax file at the project root; a `source=` gate on
  write + auto-creation when a domain is initialized / a project binds.

New modules: `lock.py` (thin `filelock` wrapper), `ignore.py` (load / create / match).
Edits: `sync.py`, `server.py`, `pyproject.toml`.

## Concurrency design

### `lock.py`

```python
def base_lock(base: str, timeout: float = 15.0) -> FileLock:
    """Inter-process lock for git mutations on the shared base.
    Lock file lives at base/.iwiki/lock."""
```

- Lock file: `base/.iwiki/lock`. The `base/.iwiki/` directory holds server metadata at
  the *base* level (distinct from a domain's `<domain>/.iwiki/`). It is never treated as
  a domain — `list_domains`/`domain_exists` already exclude `.`-prefixed names — and is
  never staged, because commits are scoped to a domain pathspec (below). `base_lock`
  ensures `base/.iwiki/` exists.
- Timeout semantics: blocking acquire up to `timeout`; on expiry raises `filelock.Timeout`,
  which callers translate into a fail-soft warning dict.

### `sync.py` changes

`auto_commit(base, message, pathspec=None, timeout=15.0)`:
- Acquire `base_lock(base, timeout)` around the add + commit.
- Replace `git add -A` with `git add -- <pathspec>` where `pathspec` is the write-domain
  directory (`<domain>/`). When `pathspec` is `None`, fall back to the current behavior
  for callers that have nothing domain-scoped (none in the planned wiring).
- `commit -m message`. Return shape unchanged
  (`{"committed": bool, ...optional "warning"}`).
- `filelock.Timeout` -> `{"committed": False, "warning": "base busy: lock timeout"}`.

`sync(base, timeout=15.0, push_retries=3)`:
- Acquire `base_lock` around the whole pull/push.
- `pull --rebase` -> `push`.
- If `push` fails with a non-fast-forward signal (`non-fast-forward` / `fetch first`
  in stderr), retry: `pull --rebase` + `push`, up to `push_retries` times.
- Rebase conflict -> `rebase --abort` + the existing error/hint (unchanged).
- `filelock.Timeout` -> `{"pulled": False, "pushed": False, "warning": "base busy: lock timeout"}`.

Timing parameters (`timeout`, `push_retries`) are function arguments with defaults —
**not** routed through `Config.load`, so commit/sync stay independent of the LLM-key stop
rule. Optional env overrides can be added later without changing call sites.

### `server.py` wiring

- `wiki_write_page`: `sync.auto_commit(bind.base, msg, pathspec=valid_domain)`.
- `wiki_create_domain`: `sync.auto_commit(bind.base, msg, pathspec=valid_domain)`.

Problem symptom #4 (`write -> log -> reindex` not atomic across processes) is **not**
wrapped by `base_lock` — that sequence runs before `auto_commit`. It is neutralized by
the confirmed usage model: parallel projects write to different domains, so each touches
its own `<domain>/.iwiki/index.jsonl` and `log.jsonl`. Concurrent writes to the *same*
domain from two processes are out of scope (see Out of scope); the lock covers only the
shared git index and push, which is where this design's races actually occur.

## `.iwikiignore` design

### `ignore.py`

```python
def ensure_iwikiignore(project_dir: str) -> bool:
    """Create project_dir/.iwikiignore if absent. Idempotent.
    Returns True if a file was created."""

def load_project_ignore(project_dir: str) -> PathSpec | None:
    """Compile project_dir/.iwikiignore. None if absent / no real patterns."""

def is_ignored(spec: PathSpec | None, source: str, project_dir: str) -> bool:
    """Match source against spec. Path inside project_dir -> relpath match;
    outside -> basename match. spec None -> False."""
```

- `ensure_iwikiignore` writes a seeded file when missing:
  - Header comment explaining purpose + gitignore syntax.
  - Default secret patterns: `.env`, `.env.*`, `*.key`, `*.pem`, `*.p12`,
    `*secret*`, `*credentials*`.
  - Inherited block: the lines of `project_dir/.gitignore` if it exists (one-time copy).
  - An existing `.iwikiignore` is left untouched.
- `load_project_ignore` reuses the compile logic of `engine.config._load_ignore`
  (gitignore style; a file with only comments/blanks -> `None`). Reading takes an explicit
  path, so it does not touch cwd and does not trigger `ConfigError`.

### Enforcement — `wiki_write_page`

Before the write transaction begins, when `source` is provided:
- `spec = load_project_ignore(bind.project_dir)`.
- If `is_ignored(spec, source, bind.project_dir)`:
  `return {"error": "source matches .iwikiignore", "hint": "<path> is excluded; "
  "remove it from .iwikiignore to ingest, or omit source"}`.
- Nothing is written, logged, indexed, or committed (gate is before the transaction, so
  no rollback path is involved).

`source` is optional, so the gate only fires when a path is supplied. This is
defense-in-depth, not an absolute guarantee.

### Creation — two points

`ensure_iwikiignore(bind.project_dir)` is called from:
- `wiki_create_domain` (as originally requested — initializing a domain).
- `wiki_bind` (a project can bind to *existing* domains without creating one; otherwise
  the file would never appear).

Both are idempotent; the second call is a no-op once the file exists.

"Seed from `.gitignore`" is a one-time copy at creation. At runtime only `.iwikiignore`
is read — a single file for the user to edit.

## Error handling

All paths flow through the existing `@_safe` envelope. Lock timeout, push failure, and
ignore-match all degrade to warning/error dicts — never raised. The `.iwikiignore` gate
rejects before the transaction starts, so it introduces no rollback. `wiki_write_page`'s
existing transactional guarantee (file -> log -> index, rollback on failure) is unchanged.

## Testing

`tests/test_sync_concurrency.py`:
- `auto_commit` with a pathspec commits only the write-domain — a sibling domain's
  uncommitted files are not captured (tmp base git repo, two domains, write into one).
- `base_lock` acquires/releases; a second acquire while held blocks then succeeds (thread
  or short-timeout assertion).
- push-retry: with a bare remote and an emulated non-fast-forward, `sync` retries
  `pull --rebase` + `push` and converges.

`tests/test_iwikiignore.py`:
- `ensure_iwikiignore` creates when absent, does not overwrite when present, seeds from
  `.gitignore`.
- `wiki_write_page` rejects when `source` matches, writes when it does not.
- `wiki_create_domain` and `wiki_bind` create `.iwikiignore` in `project_dir`.

Follow the established test pattern (`tests/test_server_write.py::_seed`): monkeypatch
`indexer.embed_texts`, set dummy `IWIKI_*` env. `filelock` is real and fast.

## Dependencies

- Add `filelock` to `pyproject.toml` runtime dependencies.
- `pathspec` is already a dependency.

## Affected files

- Edit: `src/iwiki_mcp/sync.py`, `src/iwiki_mcp/server.py`, `pyproject.toml`.
- New: `src/iwiki_mcp/lock.py`, `src/iwiki_mcp/ignore.py`,
  `tests/test_sync_concurrency.py`, `tests/test_iwikiignore.py`.
- Docs: update `docs/wiki/indexing.md` and/or `docs/wiki/architecture.md` per the repo's
  docs-upkeep mandate.

## Out of scope

- Indexing project sources directly (the server keeps consuming finished markdown).
- Removing already-indexed pages via ignore patterns (`.iwikiignore` filters ingestion,
  not the existing index).
- Per-domain `.iwikiignore` inside the base.
- Concurrent writes to the **same** domain from two processes (the confirmed model is
  one write-domain per project). The `write -> log -> reindex` sequence is not locked.
