---
review:
  plan_hash: b4b42701b7aa550d
  last_run: 2026-07-01
  phases:
    structure: {status: passed}
    coverage: {status: passed}
    dependencies: {status: passed}
    verifiability: {status: passed}
    consistency: {status: passed}
  findings: []
chain:
  intent: null
  spec: 3c71c3a2097a399a
---
# Ensure base freshness before write — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pull remote changes into the wiki base *before* any mutating tool writes, so the write lands on the current remote tip and the eventual push is a fast-forward; refuse the write when the base has genuinely diverged.

**Architecture:** Add a fail-soft `sync.ensure_fresh(base)` that fetches, and fast-forwards the base when it is cleanly behind its upstream. Call it at the top of all five mutating handlers in `server.py` (after domain validation, before any filesystem check); a `diverged` state makes the handler return an error+hint with zero side effects, every other state proceeds. The existing post-write `commit_and_push` (pull --rebase + push) stays as the second line of defence.

**Tech Stack:** Python 3, `subprocess` git calls, `filelock` (via `sync.base_lock`), pytest (no-network: local bare-repo remotes, `monkeypatch` on `indexer.embed_texts`).

## Global Constraints

- Version bump: `pyproject.toml` `0.1.4` → `0.1.5` (patch).
- Fail-soft handlers: `ensure_fresh` never raises; it returns a `{"state": ...}` dict. Every `wiki_*` handler is wrapped by `@_safe` — keep implementation functions plain.
- Path-traversal guards are load-bearing: `_validate_domain` must run **before** `ensure_fresh` and before any path join. `ensure_fresh` touches only `bind.base`, never a user path.
- No linter/formatter configured — match surrounding style by hand.
- Tests never hit the network: git remotes are local bare repos; `indexer.embed_texts` is monkeypatched; `IWIKI_*` env vars are dummy (see `tests/test_server_write.py::_seed`).
- `asyncio_mode = "auto"`, `pythonpath = ["src"]` — import `iwiki_mcp` directly; async tests need no marker.

---

### Task 1: `sync.ensure_fresh` + helpers

**Files:**
- Modify: `src/iwiki_mcp/sync.py` (add `_ahead_behind`, `_tree_clean`, `ensure_fresh`)
- Test: `tests/test_ensure_fresh.py` (create)

**Interfaces:**
- Consumes: existing `sync._run`, `sync.is_git_repo`, `sync._has_remote`, `sync._output`, `sync.base_lock`, `sync.Timeout`.
- Produces:
  - `ensure_fresh(base: str, timeout: float = 15.0) -> dict` — returns `{"state": <state>}` with an optional `"warning"`. States: `no_repo`, `no_remote`, `no_upstream`, `offline`, `up_to_date`, `ahead`, `updated`, `dirty`, `diverged`.
  - `_ahead_behind(base: str) -> tuple[int, int] | None` — `(behind, ahead)` vs `@{upstream}`, or `None` when there is no upstream.
  - `_tree_clean(base: str) -> bool`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ensure_fresh.py`:

```python
import subprocess

from iwiki_mcp import sync


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")


def _setup(tmp_path):
    """A base repo tracking origin/main with one seed commit pushed."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)],
                   check=True)
    base = tmp_path / "base"
    _init_repo(base)
    _git(base, "checkout", "-q", "-b", "main")
    (base / "seed.md").write_text("0")
    _git(base, "add", "-A")
    _git(base, "commit", "-q", "-m", "seed")
    _git(base, "remote", "add", "origin", str(remote))
    _git(base, "push", "-q", "-u", "origin", "main")
    return remote, base


def _neighbor_push(remote, tmp_path):
    """A second clone advances origin/main behind our back."""
    nb = tmp_path / "nb"
    subprocess.run(["git", "clone", "-q", str(remote), str(nb)], check=True)
    _git(nb, "config", "user.email", "n@n")
    _git(nb, "config", "user.name", "n")
    (nb / "nb.md").write_text("1")
    _git(nb, "add", "-A")
    _git(nb, "commit", "-q", "-m", "neighbor")
    _git(nb, "push", "-q", "origin", "main")


def _log(base):
    return subprocess.run(["git", "log", "--oneline"], cwd=base,
                          capture_output=True, text=True).stdout


def test_ensure_fresh_up_to_date(tmp_path):
    _remote, base = _setup(tmp_path)
    assert sync.ensure_fresh(str(base))["state"] == "up_to_date"


def test_ensure_fresh_updated_fast_forwards(tmp_path):
    remote, base = _setup(tmp_path)
    _neighbor_push(remote, tmp_path)
    res = sync.ensure_fresh(str(base))
    assert res["state"] == "updated"
    assert "neighbor" in _log(base)  # ff pulled the neighbor commit in


def test_ensure_fresh_ahead_only(tmp_path):
    _remote, base = _setup(tmp_path)
    (base / "local.md").write_text("x")
    _git(base, "add", "-A")
    _git(base, "commit", "-q", "-m", "local")
    assert sync.ensure_fresh(str(base))["state"] == "ahead"


def test_ensure_fresh_diverged_leaves_base_untouched(tmp_path):
    remote, base = _setup(tmp_path)
    _neighbor_push(remote, tmp_path)
    (base / "local.md").write_text("x")
    _git(base, "add", "-A")
    _git(base, "commit", "-q", "-m", "local")
    res = sync.ensure_fresh(str(base))
    assert res["state"] == "diverged"
    log = _log(base)
    assert "local" in log
    assert "neighbor" not in log  # no ff / rebase happened


def test_ensure_fresh_no_remote(tmp_path):
    base = tmp_path / "plain"
    _init_repo(base)
    (base / "x.md").write_text("hi")
    _git(base, "add", "-A")
    _git(base, "commit", "-q", "-m", "c")
    assert sync.ensure_fresh(str(base))["state"] == "no_remote"


def test_ensure_fresh_non_repo(tmp_path):
    assert sync.ensure_fresh(str(tmp_path))["state"] == "no_repo"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ensure_fresh.py -q`
Expected: FAIL — `AttributeError: module 'iwiki_mcp.sync' has no attribute 'ensure_fresh'`.

- [ ] **Step 3: Implement the helpers and `ensure_fresh`**

Append to `src/iwiki_mcp/sync.py` (after the `sync` function, before `commit_and_push`):

```python
def _ahead_behind(base: str) -> tuple[int, int] | None:
    """(behind, ahead) relative to @{upstream}, or None if no upstream is set."""
    r = _run(base, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    if r.returncode != 0:
        return None
    parts = r.stdout.split()
    if len(parts) != 2:
        return None
    behind, ahead = parts
    return int(behind), int(ahead)


def _tree_clean(base: str) -> bool:
    r = _run(base, "status", "--porcelain")
    return r.returncode == 0 and not r.stdout.strip()


def ensure_fresh(base: str, timeout: float = 15.0) -> dict:
    """Bring the base up to date with its remote BEFORE a local mutation.

    Fetches, then fast-forwards when the base is cleanly behind its upstream.
    Fail-soft: returns a {"state": ...} dict, never raises. A "diverged" state
    (local commits AND remote ahead) signals the caller to refuse the write.
    """
    if not is_git_repo(base):
        return {"state": "no_repo"}
    try:
        with base_lock(base, timeout):
            if not _has_remote(base):
                return {"state": "no_remote"}
            fetch = _run(base, "fetch")
            if fetch.returncode != 0:
                return {"state": "offline", "warning": _output(fetch)}
            counts = _ahead_behind(base)
            if counts is None:
                return {"state": "no_upstream",
                        "warning": "branch has no upstream; skipped freshness check"}
            behind, ahead = counts
            if behind == 0:
                return {"state": "ahead" if ahead else "up_to_date"}
            if ahead:
                return {"state": "diverged"}
            if not _tree_clean(base):
                return {"state": "dirty",
                        "warning": "local changes present; skipped fast-forward"}
            ff = _run(base, "merge", "--ff-only", "@{upstream}")
            if ff.returncode != 0:
                return {"state": "offline", "warning": _output(ff)}
            return {"state": "updated"}
    except Timeout:
        return {"state": "offline", "warning": "base busy: lock timeout"}
    except Exception as e:
        return {"state": "offline", "warning": str(e)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ensure_fresh.py -q`
Expected: PASS — 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/sync.py tests/test_ensure_fresh.py
git commit -m "feat(sync): add ensure_fresh pre-write freshness guard"
```

---

### Task 2: Wire `ensure_fresh` into the five mutating handlers

**Files:**
- Modify: `src/iwiki_mcp/server.py` (add `_DIVERGED` const + `_fresh_warn` helper; call the guard in `wiki_write_page`, `wiki_update_page`, `wiki_delete_page`, `wiki_index`, `wiki_create_domain`)
- Test: `tests/test_server_fresh.py` (create)

**Interfaces:**
- Consumes: `sync.ensure_fresh` (Task 1); existing `base.resolve_binding`, `_validate_domain`, `sync.commit_and_push`.
- Produces: no new public surface — behavior change only. On `diverged`, handlers return `{"error": "base diverged from remote", "hint": ...}`; otherwise any freshness `warning` is added under the `"warning"` key of the success dict.

- [ ] **Step 1: Write the failing handler tests**

Create `tests/test_server_fresh.py`:

```python
import os
import subprocess

from iwiki_mcp import base, indexer, server


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def _seed_git(tmp_path, monkeypatch):
    """Base is a git repo tracking origin/main, with a `backend` domain."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)],
                   check=True)
    b = tmp_path / "wiki"
    b.mkdir()
    _git(b, "init", "-q")
    _git(b, "config", "user.email", "t@t")
    _git(b, "config", "user.name", "t")
    _git(b, "checkout", "-q", "-b", "main")
    (b / "backend" / ".iwiki").mkdir(parents=True)
    (b / ".gitkeep").write_text("")
    _git(b, "add", "-A")
    _git(b, "commit", "-q", "-m", "seed")
    _git(b, "remote", "add", "origin", str(remote))
    _git(b, "push", "-q", "-u", "origin", "main")

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text('read = ["backend"]\nwrite = "backend"\n')
    monkeypatch.setenv("IWIKI_BASE_DIR", str(b))
    monkeypatch.setenv("IWIKI_PROJECT_DIR", str(proj))
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", "2")
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, t: [[1.0, 0.0] for _ in t])
    return b, remote


def _neighbor_push(remote, tmp_path):
    nb = tmp_path / "nb"
    subprocess.run(["git", "clone", "-q", str(remote), str(nb)], check=True)
    _git(nb, "config", "user.email", "n@n")
    _git(nb, "config", "user.name", "n")
    (nb / "neighbor.md").write_text("hello")
    _git(nb, "add", "-A")
    _git(nb, "commit", "-q", "-m", "neighbor")
    _git(nb, "push", "-q", "origin", "main")


def test_write_refuses_on_diverged_with_zero_side_effects(tmp_path, monkeypatch):
    b, remote = _seed_git(tmp_path, monkeypatch)
    _neighbor_push(remote, tmp_path)
    # local commit → base is now ahead AND behind (diverged)
    (b / "local.md").write_text("x")
    _git(b, "add", "-A")
    _git(b, "commit", "-q", "-m", "local")

    md = "# Auth\n## Overview\nsummary\n## Flow\nlogin then token\n"
    out = server.wiki_write_page("backend", "auth", md)

    assert "error" in out and "diverged" in out["error"]
    assert "hint" in out
    assert not os.path.isfile(b / "backend" / "auth.md")
    assert not os.path.isfile(b / "backend" / ".iwiki" / "log.jsonl")


def test_write_fast_forwards_when_behind_then_writes(tmp_path, monkeypatch):
    b, remote = _seed_git(tmp_path, monkeypatch)
    _neighbor_push(remote, tmp_path)  # base is cleanly behind

    md = "# Auth\n## Overview\nsummary\n## Flow\nlogin then token\n"
    out = server.wiki_write_page("backend", "auth", md)

    assert out["page"] == "backend/auth.md"
    assert os.path.isfile(b / "backend" / "auth.md")
    assert os.path.isfile(b / "neighbor.md")  # ff pulled the neighbor commit in
    assert out["pushed"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server_fresh.py -q`
Expected: FAIL — `test_write_refuses_on_diverged...` fails because the page file IS written (no guard yet); the diverged assertion is not met.

- [ ] **Step 3: Add the guard helpers to `server.py`**

Add near the other module-level helpers in `src/iwiki_mcp/server.py` (e.g. just above `wiki_write_page`):

```python
_DIVERGED = {
    "error": "base diverged from remote",
    "hint": "run wiki_sync to reconcile (pull --rebase + push), "
            "or resolve the conflict in the base repo, then retry",
}


def _fresh_warn(fresh: dict) -> dict:
    """Freshness warning as a spreadable dict fragment ({} when there is none)."""
    w = fresh.get("warning")
    return {"warning": w} if w else {}
```

- [ ] **Step 4: Call the guard in each of the five handlers**

In each handler, insert immediately after the `valid_domain = _validate_domain(...)` line and before the first `_domain_path(...)` / `.is_dir()` / existence check:

```python
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
```

Then add `**_fresh_warn(fresh)` to the success-return dict of each handler.

`wiki_write_page` — after line `valid_domain = _validate_domain(domain)`:

```python
    valid_domain = _validate_domain(domain)
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    dom_path = _domain_path(bind.base, valid_domain)
```

and its success return becomes:

```python
    return {
        "page": page_rel,
        "indexed_chunks": stats["indexed_chunks"],
        "bytes": stats["bytes"],
        "over_cap": stats["over_cap"],
        "committed": commit.get("committed", False),
        "pushed": commit.get("pushed", False),
        **_fresh_warn(fresh),
    }
```

`wiki_update_page` — after `valid_domain = _validate_domain(domain)`:

```python
    valid_domain = _validate_domain(domain)
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    dom_path = _domain_path(bind.base, valid_domain)
```

and add `**_fresh_warn(fresh),` as the last entry of its success-return dict.

`wiki_delete_page` — after `valid_domain = _validate_domain(domain)`:

```python
    valid_domain = _validate_domain(domain)
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    dom_path = _domain_path(bind.base, valid_domain)
```

and add `**_fresh_warn(fresh),` as the last entry of its success-return dict.

`wiki_index` — after `valid_domain = _validate_domain(target)`:

```python
    valid_domain = _validate_domain(target)
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    dom_path = _domain_path(bind.base, valid_domain)
```

and its success return becomes:

```python
    return {"domain": valid_domain, **stats,
            "committed": commit.get("committed", False),
            "pushed": commit.get("pushed", False),
            **_fresh_warn(fresh)}
```

`wiki_create_domain` — after `valid_domain = _validate_domain(name)`:

```python
    valid_domain = _validate_domain(name)
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    dom_path = _domain_path(bind.base, valid_domain)
```

and its success return becomes:

```python
    return {"created": valid_domain, "committed": commit.get("committed", False),
            "pushed": commit.get("pushed", False), **_fresh_warn(fresh)}
```

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `uv run pytest tests/test_server_fresh.py -q`
Expected: PASS — 2 passed.

- [ ] **Step 6: Run the full suite to confirm no regression**

Run: `uv run pytest -q`
Expected: PASS — entire suite green (non-git bases return `no_repo`, so existing handler tests are unaffected).

- [ ] **Step 7: Commit**

```bash
git add src/iwiki_mcp/server.py tests/test_server_fresh.py
git commit -m "feat(server): refuse writes on diverged base, fast-forward before mutating"
```

---

### Task 3: Docs + version bump

**Files:**
- Modify: `pyproject.toml` (version `0.1.4` → `0.1.5`)
- Modify: `README.md` ("Git sync of the base" section)
- Modify: `CLAUDE.md` (transactional-write / git convention bullet)
- Modify: `docs/wiki/` sync page via the iwiki tools (gated on `wiki_status`)

**Interfaces:**
- Consumes: nothing new — documentation of Task 1/2 behavior.
- Produces: no code.

- [ ] **Step 1: Bump the version**

In `pyproject.toml` line 3, change:

```toml
version = "0.1.4"
```

to:

```toml
version = "0.1.5"
```

- [ ] **Step 2: Update README "Git sync of the base"**

In `README.md`, in the paragraph starting "When `IWIKI_BASE_DIR` is a git repository, every mutating tool …", append a sentence describing the pre-write freshness check:

```markdown
Before writing, each mutating tool first fetches and fast-forwards the base when it is cleanly behind its remote, so the change lands on the current tip and the push is a fast-forward. If the base has genuinely diverged (local unpushed commits *and* the remote moved ahead), the tool refuses with `base diverged from remote` and a hint to run `wiki_sync` (or resolve the conflict in the base repo) before retrying — it does not stack another commit onto the divergence.
```

- [ ] **Step 3: Update project `CLAUDE.md`**

In `CLAUDE.md`, in the "Conventions that aren't obvious from a single file" section, extend the **Transactional write** bullet (or add a sibling bullet) with:

```markdown
- **Pre-write freshness guard.** Every mutating handler runs `sync.ensure_fresh(base)` first (after `_validate_domain`, before any filesystem check): it fetches and fast-forwards the base when cleanly behind its remote so writes land on the current tip and push fast-forwards. A `diverged` base (local commits *and* remote ahead) makes the handler return `base diverged from remote` + hint with zero side effects; all other states proceed, threading any `warning` onto the result.
```

- [ ] **Step 4: Run the full suite (sanity after doc/version edits)**

Run: `uv run pytest -q`
Expected: PASS — entire suite green.

- [ ] **Step 5: Commit the code-repo docs + version**

```bash
git add pyproject.toml README.md CLAUDE.md
git commit -m "docs: document pre-write freshness guard; bump version to 0.1.5"
```

- [ ] **Step 6: Update the iwiki wiki (gated)**

Call `wiki_status`. If it reports a domain bound to this project:

1. `wiki_bind(read=[<domain>], write=<domain>)`.
2. `wiki_search "sync git base"` to find the page covering base git sync.
3. `wiki_update_page(<domain>, <slug>, <heading>, <new_body>, source="src/iwiki_mcp/sync.py")` — add the `ensure_fresh` pre-write contract (fetch + ff-only when cleanly behind; refuse on diverged) to the sync/architecture page.
4. `wiki_lint` — confirm no broken refs, orphans, or stale pages.

If `wiki_status` reports no domain bound to this project, skip this step (iwiki is not set up here) and note it in the final summary.

---

## Self-Review

**1. Spec coverage**

| Spec requirement | Task |
|---|---|
| §1 `ensure_fresh` fail-soft function + `base_lock` | Task 1 Step 3 |
| §1 ahead/behind via `git rev-list --left-right --count @{upstream}...HEAD` | Task 1 `_ahead_behind` |
| §1 nine-state model + reactions | Task 1 `ensure_fresh` + Task 1 tests |
| §2 guard in all 5 mutating handlers, after binding, before validate/write | Task 2 Step 4 |
| §2 `diverged` → error+hint, zero side effects | Task 2 test `test_write_refuses_on_diverged_with_zero_side_effects` |
| §2 warning surfaced next to committed/pushed; `wiki_sync` untouched | Task 2 `_fresh_warn`; `wiki_sync` not modified |
| §3 unit tests (updated/diverged/up_to_date/no_remote/ahead) | Task 1 Step 1 |
| §3 handler tests (diverged refuse zero-effect; behind ff+write+push) | Task 2 Step 1 |
| §4 wiki + README + CLAUDE.md + version bump + TODO row | Task 3 (TODO row already opened by `/check-chain spec`) |

No gaps.

**2. Placeholder scan** — no `TODO`/`TBD`/`FIXME`/"handle edge cases"; every code step shows complete code. (String "TODO" appears only as the filename `docs/TODO.md`.)

**3. Type consistency** — `ensure_fresh` returns `dict` with `"state"` everywhere; callers read `fresh.get("state")` and `_fresh_warn(fresh)`; `_ahead_behind` returns `(behind, ahead)` and `ensure_fresh` unpacks `behind, ahead = counts` in that order. Consistent across tasks.
