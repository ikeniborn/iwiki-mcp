---
review:
  plan_hash: d0fe18073cb8717a
  last_run: 2026-07-12
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-07-11-wiki-push-recovery-intent.md
  spec: docs/superpowers/specs/2026-07-11-wiki-push-recovery-design.md
result_check:
  verdict: OK
  plan_hash: d0fe18073cb8717a
  last_run: 2026-07-12
  reviewed: true
  docs_checked: true
---
# Wiki Push Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make wiki write pushes recover safely within three synchronization attempts and expose exact sanitized failure details without weakening credential or conflict safety.

**Architecture:** Keep subprocess policy, failure classification, retry, and metadata in `sync.py`; keep `server.py` limited to deterministic result assembly. Retry one already-created commit under the existing base lock, never resolve content conflicts, and use sanitized probes/tests rather than handling credentials.

**Tech Stack:** Python 3.10+, subprocess Git, filelock, FastMCP, pytest, pytest monkeypatch/tmp_path.

---

### Task 1: Non-interactive Git boundary and failure taxonomy

**Files:**
- Modify: `src/iwiki_mcp/sync.py:14-16,67-73`
- Test: `tests/test_sync.py`

- [x] **Step 1: Write failing subprocess-policy and classification tests**

Add tests that monkeypatch `subprocess.run`, call `_run`, and assert `shell` is absent, `stdin=subprocess.DEVNULL`, and `env["GIT_TERMINAL_PROMPT"] == "0"`. Add parametrized assertions for `_classify_remote_failure` covering non-fast-forward, credential-unavailable (`Permission denied (publickey)` and terminal-prompts-disabled HTTPS), transport-unavailable, permanent remote/ref errors, and unknown text. Include a fake secret in stderr and assert `_sanitize_git_output` removes URL userinfo.

```python
@pytest.mark.parametrize("stderr, expected", [
    ("! [rejected] main -> main (fetch first)", "non_fast_forward"),
    ("git@host: Permission denied (publickey).", "credential_unavailable"),
    ("fatal: could not read Username: terminal prompts disabled", "credential_unavailable"),
    ("fatal: unable to access: Could not resolve host", "transport_unavailable"),
    ("fatal: remote origin does not appear to be a git repository", "permanent"),
    ("unexpected git failure", "unknown"),
])
def test_classify_remote_failure(stderr, expected):
    proc = subprocess.CompletedProcess(["git"], 1, "", stderr)
    assert sync._classify_remote_failure(proc) == expected
```

- [x] **Step 2: Run tests and confirm RED**

```bash
uv run pytest -q tests/test_sync.py
```

Expected: failures because the policy environment and classifier helpers do not exist.

- [x] **Step 3: Implement minimal safe boundary and closed classifier**

Update `_run` to copy `os.environ`, force `GIT_TERMINAL_PROMPT=0`, and pass `stdin=subprocess.DEVNULL`. Add `_classify_remote_failure` with only the spec-approved signatures and `_sanitize_git_output` that strips URL userinfo before results are returned. Do not invoke a shell, inspect credential values, or scan sockets.

- [x] **Step 4: Run focused tests and confirm GREEN**

```bash
uv run pytest -q tests/test_sync.py
```

Expected: all `tests/test_sync.py` tests pass.

- [x] **Step 5: Commit**

```bash
git add src/iwiki_mcp/sync.py tests/test_sync.py
git commit -m "fix(sync): make git subprocesses non-interactive"
```

### Task 2: Bounded synchronization recovery

**Files:**
- Modify: `src/iwiki_mcp/sync.py:76-106`
- Test: `tests/test_sync.py`

- [x] **Step 1: Write failing attempt-state tests**

Use a scripted `_run` fake and monkeypatched `time.sleep`. Cover first-attempt success; pull credential failure then success; push credential failure then success; third-attempt exhaustion; permanent/unknown immediate stop; no sleep after the last attempt. Assert `sync_attempts`, `push_attempts`, `failure_class`, exact sanitized `warning`, and sleep calls `[0.25]` or `[0.25, 0.25]`.

```python
def test_sync_recovers_when_credentials_become_available(monkeypatch, git_base):
    script = iter([failed_pull_auth(), ok_pull(), ok_push()])
    monkeypatch.setattr(sync, "_run", lambda *a, **k: next(script))
    sleeps = []
    monkeypatch.setattr(sync.time, "sleep", sleeps.append)
    out = sync.sync(str(git_base), push_retries=3)
    assert out == {"pulled": True, "pushed": True,
                   "sync_attempts": 2, "push_attempts": 1}
    assert sleeps == [0.25]
```

- [x] **Step 2: Run attempt tests and confirm RED**

```bash
uv run pytest -q tests/test_sync.py -k "attempt or recover or exhaustion or permanent"
```

Expected: missing metadata and pull/auth failures do not retry.

- [x] **Step 3: Implement the three-attempt state machine**

Refactor `sync` so each loop iteration is one synchronization attempt. Retry only the three approved recoverable classes from pull or push; delay `0.25` seconds only when another attempt remains. Preserve existing lock scope, no-remote behavior, and fail-soft returns. Return `sync_attempts: 0` and `push_attempts: 0` for pre-attempt exits.

- [x] **Step 4: Run sync tests and confirm GREEN**

```bash
uv run pytest -q tests/test_sync.py tests/test_sync_concurrency.py tests/test_ensure_fresh.py
```

Expected: all selected tests pass and no attempt exceeds the configured maximum.

- [x] **Step 5: Commit**

```bash
git add src/iwiki_mcp/sync.py tests/test_sync.py
git commit -m "fix(sync): retry recoverable remote failures"
```

### Task 3: Preserve true rebase conflicts

**Files:**
- Modify: `src/iwiki_mcp/sync.py:84-106`
- Create: `tests/test_sync_parallel.py`

- [x] **Step 1: Write local-bare-remote integration tests**

Create one bare remote and two clones. Prove non-overlapping commits rebase and push automatically. Then edit the same line in both clones, push clone A, call `sync` in clone B, and assert `pushed is False`, `failure_class == "rebase_conflict"`, `conflict is True`, actionable `hint`, no rebase state, unchanged remote head, and clone B's local commit still exists.

- [x] **Step 2: Run parallel tests and confirm RED**

```bash
uv run pytest -q tests/test_sync_parallel.py
```

Expected: conflict metadata assertions fail before implementation.

- [x] **Step 3: Return safe conflict metadata after abort**

On detected rebase state, run `rebase --abort`, stop without delay/retry, preserve the original class even if abort emits an error, and return sanitized guidance to resolve the base repo then call `wiki_sync`. Never stage or select conflict content.

- [x] **Step 4: Run integration tests and confirm GREEN**

```bash
uv run pytest -q tests/test_sync_parallel.py tests/test_sync_concurrency.py
```

Expected: non-conflicting and conflicting scenarios pass.

- [x] **Step 5: Commit**

```bash
git add src/iwiki_mcp/sync.py tests/test_sync_parallel.py
git commit -m "fix(sync): preserve commits on rebase conflict"
```

### Task 4: Propagate sync metadata through commit_and_push

**Files:**
- Modify: `src/iwiki_mcp/sync.py:171-190`
- Test: `tests/test_commit_and_push.py`

- [x] **Step 1: Write failing propagation and single-commit tests**

Extend current tests so successful, exhausted, conflict, and failed-local-commit paths assert `sync_attempts`, `push_attempts`, `failure_class`, `conflict`, and `hint`. Count `auto_commit` calls and prove three synchronization attempts still call it once.

- [x] **Step 2: Run focused tests and confirm RED**

```bash
uv run pytest -q tests/test_commit_and_push.py
```

Expected: metadata is currently dropped.

- [x] **Step 3: Copy an explicit allowlist of safe sync fields**

Build the result from `committed`, `pushed`, and an allowlist of `sync_attempts`, `push_attempts`, `failure_class`, `conflict`, and `hint`; continue normalizing terminal `error`/`warning` to sanitized `warning`. A failed local commit returns both attempt counts as zero and never calls `sync`.

- [x] **Step 4: Run focused tests and confirm GREEN**

```bash
uv run pytest -q tests/test_commit_and_push.py
```

Expected: all tests pass; `auto_commit` count is one.

- [x] **Step 5: Commit**

```bash
git add src/iwiki_mcp/sync.py tests/test_commit_and_push.py
git commit -m "fix(sync): expose safe push recovery metadata"
```

### Task 5: Expose push failures from write tools

**Files:**
- Modify: `src/iwiki_mcp/server.py:373-376,485-498,584-598`
- Test: `tests/test_server_write.py`
- Test: `tests/test_server_update.py`
- Test: `tests/test_server_write_frontmatter.py`

- [x] **Step 1: Write failing write/update result-contract tests**

Mock `commit_and_push` with a committed-but-unpushed result containing all safe metadata and assert both tools return it. Add warning-priority tests: commit warning wins over freshness and frontmatter warnings; without a commit warning, existing freshness then frontmatter behavior remains unchanged. Inject `https://secret@host` and assert returned warning is already sanitized at the sync boundary.

- [x] **Step 2: Run focused tests and confirm RED**

```bash
uv run pytest -q tests/test_server_write.py tests/test_server_update.py tests/test_server_write_frontmatter.py
```

Expected: commit warning and metadata are absent from tool results.

- [x] **Step 3: Add one deterministic result helper**

Add a small server helper that copies the safe commit metadata allowlist and selects warning priority `commit -> freshness -> frontmatter`. Use it from both write paths without changing mutation, rollback, indexing, or response fields unrelated to synchronization.

- [x] **Step 4: Run focused tests and confirm GREEN**

```bash
uv run pytest -q tests/test_server_write.py tests/test_server_update.py tests/test_server_write_frontmatter.py tests/test_server_fresh.py
```

Expected: all selected tests pass with exact warning priority.

- [x] **Step 5: Commit**

```bash
git add src/iwiki_mcp/server.py tests/test_server_write.py tests/test_server_update.py tests/test_server_write_frontmatter.py
git commit -m "fix(server): surface write push failures"
```

### Task 6: Record sanitized credential-context evidence

**Files:**
- Create: `docs/superpowers/evidence/wiki-push-credential-context.md`
- Modify: `README.md`
- Modify: `docs/README.ru.md`

- [x] **Step 1: Run sanitized probes**

Run shell and MCP-like probes that record only transport category, credential-helper configured boolean, SSH-agent variable present/usable booleans, and failure class. Do not print env values, socket paths, helper output, remote URL, username, or tokens. Reproduce the failing call when available; otherwise mark evidence blocked rather than inventing a cause.

- [x] **Step 2: Write the evidence record and operational guidance**

Document commands in redacted/category-only form, observed results, root-cause verdict (`confirmed`, `disproved`, or `blocked`), the implemented standard-Git retry boundary, and safe options. State explicitly that client config changes, socket scanning, profile loading, brokers, and credential storage are not implemented.

- [x] **Step 3: Verify no credential material entered the diff**

```bash
git diff --check
git diff -- docs/superpowers/evidence/wiki-push-credential-context.md README.md docs/README.ru.md
```

Expected: only categories/booleans and placeholder hosts; no local paths, URLs, keys, tokens, or socket values.

- [x] **Step 4: Commit**

```bash
git add docs/superpowers/evidence/wiki-push-credential-context.md README.md docs/README.ru.md
git commit -m "docs: document push recovery and credential evidence"
```

### Task 7: Update wiki and run full verification

**Files:**
- Modify through MCP: iwiki domain `iwiki-mcp`, page `git-sync`, sections `Auto-commit on write` and `Explicit sync`
- Modify through MCP: iwiki domain `iwiki-mcp`, page `mcp-server`, section `Write path`
- Modify: `docs/TODO.md`

- [x] **Step 1: Update confirmed wiki behavior through MCP tools**

Use `wiki_update_page` with source anchors `src/iwiki_mcp/sync.py` and `src/iwiki_mcp/server.py`. Document bounded recovery, conflict abort/preservation, non-interactive Git, warning priority, and safe metadata. Do not claim a credential root cause beyond Task 6 evidence.

- [x] **Step 2: Lint the bound wiki domain**

Run `wiki_lint(domain="iwiki-mcp")`.

Expected: no new broken references, stale pages, missing sources, or orphans; pre-existing advisory findings are recorded separately.

Verification evidence (2026-07-12): `broken`, `stale`, and `missing_source`
were empty. The existing `architecture.md` orphan, `long_lead` advisories, and
`vector`/`vector-store` tag drift remained; no new blocking finding appeared.

- [x] **Step 3: Run full regression and security checks**

```bash
uv run pytest -q
uv run flake8 src tests
uv run iwiki-mcp --help
git diff --check
```

Expected: pytest and flake8 exit 0; help prints usage and exits 0; diff check is clean. Confirm tests prove maximum three sync attempts, no shell/prompt, no secret leakage, one local commit, and conflict preservation.

- [x] **Step 4: Reconcile implementation against intent and spec**

Run `$check-chain result docs/superpowers/plans/2026-07-11-wiki-push-recovery.md`. Fix every confirmed bug or missing requirement, rerun affected checks, and require verdict `OK` before closing the TODO row.

- [x] **Step 5: Commit final documentation state**

```bash
git add docs/TODO.md docs/superpowers/plans/2026-07-11-wiki-push-recovery.md
git commit -m "docs: close wiki push recovery chain"
```
