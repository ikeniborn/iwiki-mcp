---
review:
  plan_hash: 18e576150cc36943
  last_run: 2026-07-04
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-07-04-guard-iwiki-bind-scope-intent.md
  spec: docs/superpowers/specs/2026-07-04-guard-iwiki-bind-scope-design.md
---
# Guard Iwiki Bind Scope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent `wiki_bind` and iwiki agent instructions from changing project binding scope outside the current project domain.

**Architecture:** Add pure binding-policy helpers in `base.py`, enforce them at the `server.wiki_bind` trust boundary, then update tests and docs/templates to remove automatic bootstrap guidance. Existing search/write tools keep using resolved binding data; only bind-time mutation is guarded.

**Tech Stack:** Python 3.10+, FastMCP, pytest, TOML via `tomllib`/`tomli`.

---

## File Structure

- Modify: `src/iwiki_mcp/base.py` — add current project domain inference and append-only read merge helper.
- Modify: `src/iwiki_mcp/server.py` — enforce current-project-only `write` and protected `read` in `wiki_bind`.
- Modify: `tests/test_base.py` — unit-test helper semantics.
- Modify: `tests/test_server_write.py` — update old bind test and add server-level bind guard tests.
- Modify: `tests/test_server_iwikiignore.py` — keep `.iwikiignore` bind test compatible with current-project write.
- Modify: `README.md` — document guarded bind behavior and remove startup bootstrap wording.
- Modify: `docs/README.ru.md` — Russian mirror of README changes.
- Modify: `templates/AGENTS.md.snippet` — remove automatic bootstrap/source-area indexing instruction.
- Modify: `templates/CLAUDE.md.snippet` — same guidance as AGENTS snippet.
- Modify: `pyproject.toml` — bump patch version to `0.1.6`.
- Update via MCP: iwiki page `iwiki-mcp/base-binding` section `Writing .iwiki.toml`; run `wiki_lint`.

---

### Task 1: Add Binding Policy Helpers

**Files:**
- Modify: `src/iwiki_mcp/base.py`
- Modify: `tests/test_base.py`

- [ ] **Step 1: Add failing helper tests**

Append to `tests/test_base.py`:

```python
def test_current_project_domain_uses_project_dir_basename(tmp_path):
    proj = tmp_path / "my-project"
    proj.mkdir()

    assert base.current_project_domain(str(proj)) == "my-project"


def test_merge_read_scope_sets_read_when_existing_empty():
    merged, error = base.merge_read_scope((), ("backend", "shared"), "backend")

    assert error is None
    assert merged == ("backend", "shared")


def test_merge_read_scope_appends_current_domain_only():
    merged, error = base.merge_read_scope(("foreign",), ("backend",), "backend")

    assert error is None
    assert merged == ("foreign", "backend")


def test_merge_read_scope_preserves_existing_when_current_already_present():
    merged, error = base.merge_read_scope(
        ("foreign", "backend"),
        ("backend",),
        "backend",
    )

    assert error is None
    assert merged == ("foreign", "backend")


def test_merge_read_scope_rejects_new_non_current_domain():
    merged, error = base.merge_read_scope(("foreign",), ("shared",), "backend")

    assert merged == ("foreign",)
    assert error == "read scope is protected"
```

- [ ] **Step 2: Run helper tests and confirm failure**

```bash
uv run pytest tests/test_base.py -q
```

Expected: FAIL with `AttributeError` for `current_project_domain` or `merge_read_scope`.

- [ ] **Step 3: Implement helpers in `base.py`**

Add after `_as_str_tuple`:

```python
def current_project_domain(project_dir: str) -> str:
    project_name = os.path.basename(os.path.normpath(resolve_project_dir(project_dir)))
    if not project_name:
        raise BaseError("cannot infer current project domain")
    return project_name


def merge_read_scope(
    existing: list[str] | tuple[str, ...] | None,
    requested: list[str] | tuple[str, ...] | None,
    current_domain: str,
) -> tuple[tuple[str, ...], str | None]:
    existing_read = _as_str_tuple(existing)
    requested_read = _as_str_tuple(requested)
    if not existing_read:
        return requested_read, None

    for domain in requested_read:
        if domain not in existing_read and domain != current_domain:
            return existing_read, "read scope is protected"

    if current_domain in requested_read and current_domain not in existing_read:
        return (*existing_read, current_domain), None
    return existing_read, None
```

- [ ] **Step 4: Run helper tests and confirm pass**

```bash
uv run pytest tests/test_base.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit helper work**

```bash
git add src/iwiki_mcp/base.py tests/test_base.py
git commit -m "feat: add iwiki bind scope helpers"
```

---

### Task 2: Enforce `wiki_bind` Guards

**Files:**
- Modify: `src/iwiki_mcp/server.py`
- Modify: `tests/test_server_write.py`
- Modify: `tests/test_server_iwikiignore.py`

- [ ] **Step 1: Replace old bind test with guarded write-compatible test**

In `tests/test_server_write.py`, replace `test_bind_writes_config` with:

```python
def test_bind_writes_config_for_current_project_domain(tmp_path, monkeypatch):
    b, proj = _seed(tmp_path, monkeypatch)
    os.makedirs(os.path.join(b, "proj", ".iwiki"))

    out = server.wiki_bind(read=["backend", "proj"], write="proj")

    assert out["read"] == ["backend", "proj"]
    text = open(os.path.join(proj, ".iwiki.toml")).read()
    assert 'read = ["backend", "proj"]' in text
    assert 'write = "proj"' in text
```

- [ ] **Step 2: Add server-level guard tests**

Append to `tests/test_server_write.py`:

```python
def test_bind_preserves_existing_read_when_adding_current_project(tmp_path, monkeypatch):
    b, proj = _seed(tmp_path, monkeypatch)
    os.makedirs(os.path.join(b, "proj", ".iwiki"))

    out = server.wiki_bind(read=["proj"], write="proj")

    assert out["read"] == ["backend", "proj"]
    text = open(os.path.join(proj, ".iwiki.toml")).read()
    assert 'read = ["backend", "proj"]' in text
    assert 'write = "proj"' in text


def test_bind_does_not_remove_existing_read_when_current_already_present(
    tmp_path, monkeypatch
):
    b, proj = _seed(tmp_path, monkeypatch)
    os.makedirs(os.path.join(b, "proj", ".iwiki"))
    config_path = os.path.join(proj, ".iwiki.toml")
    open(config_path, "w").write('read = ["backend", "proj"]\nwrite = "proj"\n')

    out = server.wiki_bind(read=["proj"], write="proj")

    assert out["read"] == ["backend", "proj"]
    assert 'read = ["backend", "proj"]' in open(config_path).read()


def test_bind_rejects_new_non_current_read_without_writing(tmp_path, monkeypatch):
    b, proj = _seed(tmp_path, monkeypatch)
    os.makedirs(os.path.join(b, "shared", ".iwiki"))
    config_path = os.path.join(proj, ".iwiki.toml")

    out = server.wiki_bind(read=["shared"], write="proj")

    text = open(config_path).read()
    assert out["error"] == "read scope is protected"
    assert 'read = ["backend"]' in text
    assert 'shared' not in text


def test_bind_rejects_non_current_write_without_writing(tmp_path, monkeypatch):
    b, proj = _seed(tmp_path, monkeypatch)
    os.makedirs(os.path.join(b, "shared", ".iwiki"))
    config_path = os.path.join(proj, ".iwiki.toml")

    out = server.wiki_bind(write="shared")

    text = open(config_path).read()
    assert out["error"] == "write domain must match current project domain"
    assert 'write = "backend"' in text
    assert 'write = "shared"' not in text
```

- [ ] **Step 3: Update `.iwikiignore` bind test**

In `tests/test_server_iwikiignore.py`, replace `test_bind_creates_iwikiignore` with:

```python
def test_bind_creates_iwikiignore(tmp_path, monkeypatch):
    b, proj = _seed(tmp_path, monkeypatch)
    os.makedirs(os.path.join(b, "proj", ".iwiki"))
    server.wiki_bind(read=["proj"], write="proj")
    assert os.path.isfile(os.path.join(proj, ".iwikiignore"))
```

- [ ] **Step 4: Run bind tests and confirm failure**

```bash
uv run pytest tests/test_server_write.py tests/test_server_iwikiignore.py -q
```

Expected: FAIL until `server.wiki_bind` is guarded.

- [ ] **Step 5: Implement `wiki_bind` guards**

Replace `wiki_bind` in `src/iwiki_mcp/server.py` with:

```python
@_safe
def wiki_bind(read: list[str] | None = None, write: str | None = None) -> dict:
    bind = base.resolve_binding()
    current_domain = _validate_domain(base.current_project_domain(bind.project_dir))
    valid_read = None if read is None else [_validate_domain(d) for d in read]
    valid_write = None if write is None else _validate_domain(write)
    for domain in valid_read or ():
        if not _domain_path(bind.base, domain).is_dir():
            return {
                "error": f"domain '{domain}' not found",
                "hint": "create it with wiki_create_domain",
            }
    if valid_write is not None:
        if valid_write != current_domain:
            return {
                "error": "write domain must match current project domain",
                "hint": f"use write='{current_domain}' for this project",
            }
        if not _domain_path(bind.base, valid_write).is_dir():
            return {
                "error": f"domain '{valid_write}' not found",
                "hint": "create it with wiki_create_domain",
            }

    merged_read = None
    if valid_read is not None:
        merged, read_error = base.merge_read_scope(
            bind.read,
            valid_read,
            current_domain,
        )
        if read_error:
            return {
                "error": read_error,
                "hint": "existing read scope is preserved; only the current "
                        "project domain may be appended automatically",
            }
        merged_read = list(merged)

    base.write_project_config(bind.project_dir, read=merged_read, write=valid_write)
    ignore.ensure_iwikiignore(bind.project_dir)
    new = base.resolve_binding()
    return {"read": list(new.read), "write": new.write, "project_dir": new.project_dir}
```

- [ ] **Step 6: Run bind tests and confirm pass**

```bash
uv run pytest tests/test_server_write.py tests/test_server_iwikiignore.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit server guard work**

```bash
git add src/iwiki_mcp/server.py tests/test_server_write.py tests/test_server_iwikiignore.py
git commit -m "fix: guard iwiki bind scope"
```

---

### Task 3: Update Agent Guidance And User Docs

**Files:**
- Modify: `templates/AGENTS.md.snippet`
- Modify: `templates/CLAUDE.md.snippet`
- Modify: `README.md`
- Modify: `docs/README.ru.md`

- [ ] **Step 1: Update agent snippets**

In both `templates/AGENTS.md.snippet` and `templates/CLAUDE.md.snippet`, replace the bootstrapping bullet with:

```markdown
- **Binding changes are manual setup:** do not create domains, call `wiki_bind`, or
  index source areas during ordinary project startup. If the project is not bound
  or the write domain is missing, report that setup is required and ask the user
  before changing `.iwiki.toml` or creating a domain.
```

- [ ] **Step 2: Update English README bind section**

In `README.md`, replace the paragraph after the `wiki_bind(...)` example with:

```markdown
`wiki_bind` validates that every provided read and write domain already exists. For an existing non-empty `read`, the tool preserves configured domains and may only append the current project domain. `write` must match the current project domain, derived from the project directory name. Create missing domains with `wiki_create_domain` as an explicit manual setup step before binding.
```

In the "Teach the agent to use iwiki" section, replace the sentence that starts `Both carry the same guidance:` with:

```markdown
Both carry the same guidance: search before a task, do not mutate binding during ordinary startup, author pages after functionality changes, and `wiki_sync` at end of session.
```

- [ ] **Step 3: Update Russian README bind section**

In `docs/README.ru.md`, replace the paragraph after the `wiki_bind(...)` example with:

```markdown
`wiki_bind` проверяет, что каждый указанный домен read и write уже существует. Для существующего непустого `read` инструмент сохраняет настроенные домены и может только добавить домен текущего проекта. `write` должен совпадать с доменом текущего проекта, который берётся из имени каталога проекта. Создавайте недостающие домены через `wiki_create_domain` как явный ручной шаг настройки перед привязкой.
```

In "Научите агента пользоваться iwiki", replace the sentence that starts `Оба несут одинаковые указания:` with:

```markdown
Оба несут одинаковые указания: искать перед задачей, не менять привязку при обычном старте проекта, писать страницы после изменений функциональности и вызывать `wiki_sync` в конце сессии.
```

- [ ] **Step 4: Verify bootstrap wording is gone**

```bash
grep -R "Bootstrapping a new write-target\\|bootstrap a write-target\\|инициализировать целевой домен" -n README.md docs/README.ru.md templates || true
```

Expected: no output.

- [ ] **Step 5: Commit docs/templates**

```bash
git add README.md docs/README.ru.md templates/AGENTS.md.snippet templates/CLAUDE.md.snippet
git commit -m "docs: document guarded iwiki binding"
```

---

### Task 4: Version, Full Verification, And Wiki Update

**Files:**
- Modify: `pyproject.toml`
- Update via MCP: `iwiki-mcp/base-binding`

- [ ] **Step 1: Bump package version**

In `pyproject.toml`, change:

```toml
version = "0.1.5"
```

to:

```toml
version = "0.1.6"
```

- [ ] **Step 2: Run focused tests**

```bash
uv run pytest tests/test_base.py tests/test_server_write.py tests/test_server_iwikiignore.py -q
```

Expected: PASS.

- [ ] **Step 3: Run full test suite**

```bash
uv run pytest -q
```

Expected: PASS.

- [ ] **Step 4: Verify package script still loads**

```bash
uv run iwiki-mcp --help
```

Expected: command exits 0 and prints argparse help containing `--project`.

- [ ] **Step 5: Update iwiki documentation**

Use the iwiki MCP tool `wiki_update_page`:

- domain: `iwiki-mcp`
- slug: `base-binding`
- heading: `Writing .iwiki.toml`
- source: `src/iwiki_mcp/base.py`
- new body:

```markdown
`write_project_config` updates `read`/`write` without clobbering unknown config. `wiki_bind` is the guarded public entry point: for a first-time binding it may set the requested read list, but once `read` is non-empty the existing domains are preserved. The only automatic read expansion allowed is appending the current project domain, derived from the project directory name. `write` must also match the current project domain. Non-current read additions or write targets return an error before `.iwiki.toml` is written.
```

- [ ] **Step 6: Run wiki lint**

Use the iwiki MCP tool `wiki_lint(domain="iwiki-mcp")`.

Expected: no broken links or stale pages introduced by this update. Existing unrelated advisory or old source-path findings may remain.

- [ ] **Step 7: Commit version and verification docs**

```bash
git add pyproject.toml
git commit -m "chore: bump version to 0.1.6"
```

---

## Plan Self-Review

- Spec coverage: R1 covered by Task 2 write tests and guard; R2-R6 covered by Task 1 helper tests and Task 2 server tests; R7 covered by preserving existing domain checks; docs/templates covered by Task 3; version/wiki verification covered by Task 4.
- Placeholder scan: no banned placeholder markers or unspecified implementation steps.
- Type consistency: helper names are `current_project_domain` and `merge_read_scope`; server code calls those exact names; tests assert tuple/list behavior matching helper/server boundaries.
