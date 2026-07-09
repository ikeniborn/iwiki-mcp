---
review:
  plan_hash: e916abeb399d4bf2
  last_run: 2026-07-09
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings: []
result_check:
  verdict: OK
  plan_hash: e916abeb399d4bf2
  last_run: 2026-07-09
  reviewed: true
  docs_checked: true
chain:
  intent: docs/superpowers/intents/2026-07-08-wiki-auto-remediation-intent.md
  spec: docs/superpowers/specs/2026-07-09-wiki-auto-remediation-design.md
---
# Wiki Auto Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only `wiki_remediation_plan` MCP tool that turns lint freshness findings into agent-ready remediation candidates without adding a second apply/update API.

**Architecture:** Keep the new behavior in `server.py` beside existing MCP tools. The tool calls the existing lint engine, enriches lint findings with current page/source context, and returns guidance for existing write/update/delete/index tools. No page files, ingest logs, commits, embedding calls, or indexes are changed by the planning tool.

**Tech Stack:** Python 3.10+, FastMCP, pytest, existing `iwiki_mcp.server`, `iwiki_mcp.engine.lint`, `iwiki_mcp.ignore`, and `iwiki_mcp.resources.AUTHORING_RULES`.

---

## File Structure

- Modify: `src/iwiki_mcp/server.py`
  - Add `SOURCE_CONTENT_MAX_BYTES`.
  - Add private helpers for slug conversion, heading extraction, fail-soft file reads, and source-content reads.
  - Add `wiki_remediation_plan(domain: str | None = None)`.
  - Register `wiki_remediation_plan` with `mcp.tool()`.
- Modify: `tests/test_server_lint_sync.py`
  - Add focused server-level tests for planning behavior.
- Modify: `docs/superpowers/specs/2026-07-09-wiki-auto-remediation-design.md`
  - Only if implementation discoveries require small spec corrections.
- Modify through iwiki MCP tools: project wiki pages `mcp-server` and `authoring-and-linting`
  - Document the new read-only tool and workflow after implementation.
- Modify: `pyproject.toml`
  - Patch bump from `0.1.10` to `0.1.11` for the implementation change.

---

### Task 1: Planning API Shape Tests

**Files:**
- Modify: `tests/test_server_lint_sync.py`

- [ ] **Step 1: Add imports and helpers for remediation plan tests**

Append or adapt these helpers in `tests/test_server_lint_sync.py`:

```python
import json
import os

from iwiki_mcp import base, server


def _seed_remediation(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
    domain = b / "backend"
    (domain / ".iwiki").mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text('read = ["backend"]\nwrite = "backend"\n')
    monkeypatch.setenv("IWIKI_BASE_DIR", str(b))
    monkeypatch.setenv("IWIKI_PROJECT_DIR", str(proj))
    return b, domain, proj


def _write_log(domain_path, records):
    log = domain_path / ".iwiki" / "log.jsonl"
    with open(log, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
```

- [ ] **Step 2: Write failing test for empty lint plan**

Add this test:

```python
def test_remediation_plan_empty_when_lint_has_no_candidates(tmp_path, monkeypatch):
    _seed_remediation(tmp_path, monkeypatch)

    out = server.wiki_remediation_plan()

    assert out["domain"] == "backend"
    assert out["update_candidates"] == []
    assert out["delete_candidates"] == []
    assert out["blocked_candidates"] == []
    assert out["lint"]["wiki_present"] is False
    assert "use wiki_update_page" in " ".join(out["next_steps"]).lower()
    assert "## Overview" in out["authoring_rules"]
```

- [ ] **Step 3: Write failing test for non-write domain rejection**

Add this test:

```python
def test_remediation_plan_rejects_non_write_domain(tmp_path, monkeypatch):
    b, _, proj = _seed_remediation(tmp_path, monkeypatch)
    (b / "other" / ".iwiki").mkdir(parents=True)
    (proj / ".iwiki.toml").write_text(
        'read = ["backend", "other"]\nwrite = "backend"\n',
        encoding="utf-8",
    )

    out = server.wiki_remediation_plan("other")

    assert "error" in out
    assert "bound write domain" in out["hint"]
```

- [ ] **Step 4: Write failing test that planning does not mutate files or logs**

Add this test:

```python
def test_remediation_plan_does_not_mutate_page_or_log(tmp_path, monkeypatch):
    b, domain, _ = _seed_remediation(tmp_path, monkeypatch)
    page = domain / "auth.md"
    page.write_text("# Auth\n## Overview\nold\n## Flow\nold body\n", encoding="utf-8")
    src = tmp_path / "auth.py"
    src.write_text("new source\n", encoding="utf-8")
    _write_log(domain, [{
        "op": "ingest",
        "source": str(src),
        "page": "auth.md",
        "src_hash": "oldhash",
    }])
    log_before = (domain / ".iwiki" / "log.jsonl").read_text(encoding="utf-8")
    page_before = page.read_text(encoding="utf-8")
    index_path = base.index_path(str(b), "backend")
    assert not os.path.exists(index_path)

    out = server.wiki_remediation_plan()

    assert "error" not in out
    assert page.read_text(encoding="utf-8") == page_before
    assert (domain / ".iwiki" / "log.jsonl").read_text(encoding="utf-8") == log_before
    assert not os.path.exists(index_path)
```

- [ ] **Step 5: Run tests and verify they fail on missing function**

Run:

```bash
uv run pytest tests/test_server_lint_sync.py -q
```

Expected: FAIL with `AttributeError: module 'iwiki_mcp.server' has no attribute 'wiki_remediation_plan'`.

---

### Task 2: Minimal Read-Only Tool Skeleton

**Files:**
- Modify: `src/iwiki_mcp/server.py`
- Test: `tests/test_server_lint_sync.py`

- [ ] **Step 1: Add source cap and next-step constants**

In `src/iwiki_mcp/server.py`, near existing module constants, add:

```python
SOURCE_CONTENT_MAX_BYTES = 200_000

_REMEDIATION_NEXT_STEPS = [
    "Regenerate stale wiki markdown from source semantics.",
    "Use wiki_update_page for compatible section-body edits.",
    "Use wiki_delete_page then wiki_write_page when the article structure must change.",
    "Use wiki_delete_page for missing_source delete candidates.",
    "Run wiki_lint and report planned, updated, deleted, failed, and remaining_lint.",
]
```

- [ ] **Step 2: Add minimal `wiki_remediation_plan`**

In `src/iwiki_mcp/server.py`, after `wiki_lint`, add:

```python
@_safe
def wiki_remediation_plan(domain: str | None = None) -> dict:
    from .engine.lint import lint

    bind = base.resolve_binding()
    if not bind.write:
        return {
            "error": "no write domain bound",
            "hint": "set write in .iwiki.toml via wiki_bind",
        }
    target = _validate_domain(domain or bind.write)
    if target != bind.write:
        return {
            "error": "domain must match bound write domain",
            "hint": f"use the bound write domain '{bind.write}'",
        }
    dom_path = _domain_path(bind.base, target)
    report = lint(str(dom_path), project_dir=bind.project_dir)
    return {
        "domain": target,
        "lint": report,
        "update_candidates": [],
        "delete_candidates": [],
        "blocked_candidates": [],
        "authoring_rules": AUTHORING_RULES,
        "next_steps": list(_REMEDIATION_NEXT_STEPS),
    }
```

- [ ] **Step 3: Register the MCP tool**

In the thin wrapper registration block, add:

```python
mcp.tool()(wiki_remediation_plan)
```

Place it near `wiki_lint` because this is a read-only planning companion to lint.

- [ ] **Step 4: Run Task 1 tests**

Run:

```bash
uv run pytest tests/test_server_lint_sync.py -q
```

Expected: Task 1 tests pass except candidate enrichment assertions not yet added.

- [ ] **Step 5: Commit Task 2**

```bash
git add src/iwiki_mcp/server.py tests/test_server_lint_sync.py
git commit -m "feat: add wiki remediation planning skeleton"
```

---

### Task 3: Candidate Enrichment

**Files:**
- Modify: `src/iwiki_mcp/server.py`
- Modify: `tests/test_server_lint_sync.py`

- [ ] **Step 1: Add failing stale candidate test**

Add this test:

```python
def test_remediation_plan_returns_stale_update_candidate(tmp_path, monkeypatch):
    _, domain, _ = _seed_remediation(tmp_path, monkeypatch)
    page = domain / "auth.md"
    page.write_text("# Auth\n## Overview\nold\n## Flow\nold body\n", encoding="utf-8")
    src = tmp_path / "auth.py"
    src.write_text("def login():\n    return 'token'\n", encoding="utf-8")
    _write_log(domain, [{
        "op": "ingest",
        "source": str(src),
        "page": "auth.md",
        "src_hash": "oldhash",
    }])

    out = server.wiki_remediation_plan()

    assert out["blocked_candidates"] == []
    cand = out["update_candidates"][0]
    assert cand["domain"] == "backend"
    assert cand["slug"] == "auth"
    assert cand["page"] == str(page)
    assert cand["source"] == str(src)
    assert cand["current_markdown"].startswith("# Auth")
    assert "def login" in cand["source_content"]
    assert cand["source_bytes"] == len(src.read_bytes())
    assert cand["source_truncated"] is False
    assert cand["current_headings"] == ["Overview", "Flow"]
    assert cand["recommended_tools"] == [
        "wiki_update_page",
        "wiki_delete_page",
        "wiki_write_page",
        "wiki_lint",
    ]
```

- [ ] **Step 2: Add failing delete candidate test**

Add this test:

```python
def test_remediation_plan_returns_missing_source_delete_candidate(tmp_path, monkeypatch):
    _, domain, _ = _seed_remediation(tmp_path, monkeypatch)
    page = domain / "old.md"
    page.write_text("# Old\n## Overview\nold\n", encoding="utf-8")
    missing = tmp_path / "deleted.py"
    _write_log(domain, [{
        "op": "ingest",
        "source": str(missing),
        "page": "old.md",
        "src_hash": None,
    }])

    out = server.wiki_remediation_plan()

    assert out["update_candidates"] == []
    cand = out["delete_candidates"][0]
    assert cand == {
        "domain": "backend",
        "slug": "old",
        "page": str(page),
        "source": str(missing),
        "recommended_tools": ["wiki_delete_page", "wiki_lint"],
    }
```

- [ ] **Step 3: Run tests and verify enrichment failures**

Run:

```bash
uv run pytest tests/test_server_lint_sync.py::test_remediation_plan_returns_stale_update_candidate tests/test_server_lint_sync.py::test_remediation_plan_returns_missing_source_delete_candidate -q
```

Expected: FAIL because candidate lists are empty.

- [ ] **Step 4: Add private helpers**

In `src/iwiki_mcp/server.py`, add these helpers near `_page_path`:

```python
def _slug_from_page_path(dom_path: Path, page_path: str) -> str:
    rel = Path(page_path).resolve().relative_to(dom_path.resolve())
    if rel.suffix != ".md":
        raise ValueError(f"invalid page path '{page_path}'")
    return rel.with_suffix("").as_posix()


def _h2_headings(markdown: str) -> list[str]:
    import re

    return [m.group(1).strip() for m in re.finditer(r"^##\s+(.*?)\s*$", markdown, re.MULTILINE)]


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _read_source_preview(path: str) -> tuple[str, int, bool]:
    with open(path, "rb") as fh:
        data = fh.read(SOURCE_CONTENT_MAX_BYTES + 1)
    truncated = len(data) > SOURCE_CONTENT_MAX_BYTES
    if truncated:
        data = data[:SOURCE_CONTENT_MAX_BYTES]
    text = data.decode("utf-8", errors="replace")
    return text, os.path.getsize(path), truncated
```

- [ ] **Step 5: Implement candidate enrichment**

Update `wiki_remediation_plan` after `report = lint(...)`:

```python
    update_candidates = []
    delete_candidates = []
    blocked_candidates = []

    for finding in report.get("stale", []):
        page = finding.get("page", "")
        source = finding.get("source", "")
        try:
            slug = _slug_from_page_path(dom_path, page)
            current_markdown = _read_text(page)
            source_content, source_bytes, source_truncated = _read_source_preview(source)
        except OSError as e:
            blocked_candidates.append({
                "domain": target,
                "page": page,
                "source": source,
                "reason": "source_unreadable" if source and not os.path.isfile(source) else "page_unreadable",
                "error": str(e),
            })
            continue
        except Exception as e:
            blocked_candidates.append({
                "domain": target,
                "page": page,
                "source": source,
                "reason": "page_unreadable",
                "error": str(e),
            })
            continue
        update_candidates.append({
            "domain": target,
            "slug": slug,
            "page": page,
            "source": source,
            "current_markdown": current_markdown,
            "source_content": source_content,
            "source_bytes": source_bytes,
            "source_truncated": source_truncated,
            "current_headings": _h2_headings(current_markdown),
            "recommended_tools": [
                "wiki_update_page",
                "wiki_delete_page",
                "wiki_write_page",
                "wiki_lint",
            ],
        })

    for finding in report.get("missing_source", []):
        page = finding.get("page", "")
        source = finding.get("source", "")
        try:
            slug = _slug_from_page_path(dom_path, page)
        except Exception as e:
            blocked_candidates.append({
                "domain": target,
                "page": page,
                "source": source,
                "reason": "page_unreadable",
                "error": str(e),
            })
            continue
        delete_candidates.append({
            "domain": target,
            "slug": slug,
            "page": page,
            "source": source,
            "recommended_tools": ["wiki_delete_page", "wiki_lint"],
        })
```

Then return these local lists instead of empty lists.

- [ ] **Step 6: Run enrichment tests**

Run:

```bash
uv run pytest tests/test_server_lint_sync.py::test_remediation_plan_returns_stale_update_candidate tests/test_server_lint_sync.py::test_remediation_plan_returns_missing_source_delete_candidate -q
```

Expected: PASS.

- [ ] **Step 7: Commit Task 3**

```bash
git add src/iwiki_mcp/server.py tests/test_server_lint_sync.py
git commit -m "feat: enrich wiki remediation candidates"
```

---

### Task 4: Guardrails for Ignore, Truncation, and Blocking

**Files:**
- Modify: `src/iwiki_mcp/server.py`
- Modify: `tests/test_server_lint_sync.py`

- [ ] **Step 1: Add failing ignored-source test**

Add this test:

```python
def test_remediation_plan_blocks_ignored_source_content(tmp_path, monkeypatch):
    _, domain, proj = _seed_remediation(tmp_path, monkeypatch)
    (proj / ".iwikiignore").write_text("secret.py\n", encoding="utf-8")
    page = domain / "secret.md"
    page.write_text("# Secret\n## Overview\nold\n", encoding="utf-8")
    src = proj / "secret.py"
    src.write_text("token = 'secret'\n", encoding="utf-8")
    _write_log(domain, [{
        "op": "ingest",
        "source": str(src),
        "page": "secret.md",
        "src_hash": "oldhash",
    }])

    out = server.wiki_remediation_plan()

    assert out["update_candidates"] == []
    blocked = out["blocked_candidates"][0]
    assert blocked["reason"] == "source_ignored"
    assert blocked["source"] == str(src)
    assert "source_content" not in blocked
```

- [ ] **Step 2: Add failing source truncation test**

Add this test:

```python
def test_remediation_plan_truncates_large_source(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SOURCE_CONTENT_MAX_BYTES", 12)
    _, domain, _ = _seed_remediation(tmp_path, monkeypatch)
    page = domain / "large.md"
    page.write_text("# Large\n## Overview\nold\n", encoding="utf-8")
    src = tmp_path / "large.py"
    src.write_text("abcdefghijklmnop", encoding="utf-8")
    _write_log(domain, [{
        "op": "ingest",
        "source": str(src),
        "page": "large.md",
        "src_hash": "oldhash",
    }])

    out = server.wiki_remediation_plan()

    cand = out["update_candidates"][0]
    assert cand["source_content"] == "abcdefghijkl"
    assert cand["source_bytes"] == 16
    assert cand["source_truncated"] is True
```

- [ ] **Step 3: Add failing unreadable page/source tests**

Add this test for missing page behind a stale log record:

```python
def test_remediation_plan_blocks_missing_page_for_stale_record(tmp_path, monkeypatch):
    _, domain, _ = _seed_remediation(tmp_path, monkeypatch)
    src = tmp_path / "auth.py"
    src.write_text("new source\n", encoding="utf-8")
    _write_log(domain, [{
        "op": "ingest",
        "source": str(src),
        "page": "auth.md",
        "src_hash": "oldhash",
    }])

    out = server.wiki_remediation_plan()

    assert out["update_candidates"] == []
    assert out["blocked_candidates"] == []
    assert out["lint"]["stale"] == []
```

This confirms the plan inherits lint's current behavior: stale findings only exist when both page and source exist.

- [ ] **Step 4: Run guardrail tests and verify failures**

Run:

```bash
uv run pytest tests/test_server_lint_sync.py::test_remediation_plan_blocks_ignored_source_content tests/test_server_lint_sync.py::test_remediation_plan_truncates_large_source -q
```

Expected: FAIL because ignore blocking is not implemented and truncation may not expose expected values until helper details are final.

- [ ] **Step 5: Implement `.iwikiignore` blocking**

In `wiki_remediation_plan`, before reading source content for a stale finding, add:

```python
        spec = ignore.load_project_ignore(bind.project_dir)
        if source and ignore.is_ignored(spec, source, bind.project_dir):
            blocked_candidates.append({
                "domain": target,
                "page": page,
                "source": source,
                "reason": "source_ignored",
            })
            continue
```

- [ ] **Step 6: Tighten blocked reason handling**

Replace the broad stale exception block with separate reads:

```python
        try:
            slug = _slug_from_page_path(dom_path, page)
            current_markdown = _read_text(page)
        except Exception as e:
            blocked_candidates.append({
                "domain": target,
                "page": page,
                "source": source,
                "reason": "page_unreadable",
                "error": str(e),
            })
            continue
        try:
            source_content, source_bytes, source_truncated = _read_source_preview(source)
        except OSError as e:
            blocked_candidates.append({
                "domain": target,
                "slug": slug,
                "page": page,
                "source": source,
                "reason": "source_unreadable",
                "error": str(e),
            })
            continue
```

- [ ] **Step 7: Run guardrail tests**

Run:

```bash
uv run pytest tests/test_server_lint_sync.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit Task 4**

```bash
git add src/iwiki_mcp/server.py tests/test_server_lint_sync.py
git commit -m "fix: guard wiki remediation planning inputs"
```

---

### Task 5: Documentation, Version, and Full Verification

**Files:**
- Modify through iwiki MCP tools: `mcp-server`, `authoring-and-linting`
- Modify: `pyproject.toml`
- Verify: full test suite

- [ ] **Step 1: Bump package version**

In `pyproject.toml`, change:

```toml
version = "0.1.11"
```

- [ ] **Step 2: Run focused tests**

Run:

```bash
uv run pytest tests/test_server_lint_sync.py -q
```

Expected: PASS.

- [ ] **Step 3: Run full tests**

Run:

```bash
uv run pytest -q
```

Expected: PASS.

- [ ] **Step 4: Verify console script**

Run:

```bash
uv run iwiki-mcp --help
```

Expected: command exits 0 and prints `usage: iwiki-mcp`.

- [ ] **Step 5: Update iwiki tool surface documentation**

Use `wiki_update_page` to update `mcp-server` section `Tool surface`. New body must state:

```markdown
Fifteen tools cover status, read, search, planning, and write. Read/discovery: `wiki_status`, `wiki_list_domains`, `wiki_list_pages`, `wiki_read_page`, `wiki_related`. Search: `wiki_search` (modes `hybrid`/`vector`/`lexical`). Planning: `wiki_remediation_plan`, a read-only companion to `wiki_lint` that turns lint-backed `stale` and `missing_source` findings into agent-ready remediation candidates without writing pages or changing indexes. Write/maintenance: `wiki_write_page`, `wiki_update_page`, `wiki_delete_page`, `wiki_index`, `wiki_create_domain`, `wiki_bind`, `wiki_lint`, `wiki_sync`. Each returns a JSON-serializable dict. `wiki_index` defaults its target to the bound write domain when `domain` is omitted.
```

Use `source="src/iwiki_mcp/server.py"`.

- [ ] **Step 6: Update iwiki lint/remediation documentation**

Use `wiki_update_page` to update `authoring-and-linting` section `Health linting`. Preserve existing lint behavior and add that `wiki_remediation_plan` consumes the `stale` and `missing_source` fields to build read-only remediation guidance for agents. Use `source="src/iwiki_mcp/engine/lint.py"`.

- [ ] **Step 7: Run wiki lint**

Run the MCP tool `wiki_lint(domain="iwiki-mcp")`.

Expected: no broken refs. Existing unrelated `missing_source` or `long_lead` advisory findings may remain, but new documentation must not introduce stale or broken references.

- [ ] **Step 8: Run result check**

Run:

```bash
/check-chain result docs/superpowers/plans/2026-07-09-wiki-auto-remediation-plan.md
```

Expected: result report is generated; verdict is `OK` only if tests pass, docs/wiki evidence is present, and diff matches this plan.

- [ ] **Step 9: Commit final implementation**

```bash
git add src/iwiki_mcp/server.py tests/test_server_lint_sync.py pyproject.toml docs/superpowers/plans/2026-07-09-wiki-auto-remediation-plan.md docs/TODO.md docs/superpowers/reports/wiki-auto-remediation-results.html
git commit -m "feat: add wiki remediation planning"
```
