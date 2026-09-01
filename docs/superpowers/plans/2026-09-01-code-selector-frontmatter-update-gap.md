---
review:
  plan_hash: b5ae669684849954
  last_run: 2026-09-01
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-08-31-code-selector-frontmatter-update-gap-intent.md
  spec: docs/superpowers/specs/2026-09-01-code-selector-frontmatter-update-gap-design.md
---
# Code Selector Frontmatter Update Gap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `wiki_update_page` with code-only and atomic section-plus-code
selector updates while preserving 35 tools, backend concurrency, body bytes,
specification evidence, and code-graph hydration.

**Architecture:** One side-effect-free classifier validates section/code modes and
selector grammar. Existing Git and PostgreSQL branches apply an optional section
transform and optional complete `code` replacement, then execute one current backend
mutation. A post-registration helper adds root `anyOf` to only the published schema.

**Tech Stack:** Python 3.10+, FastMCP 1.28.1, pytest/pytest-asyncio,
PostgreSQL/pgvector, Git Markdown, iwiki code graph and GWT projection.

---

## Requirement coverage

| Specification requirements | Tasks |
|---|---|
| R1-R8: signature, modes, selector semantics, responses | 1-2 |
| R9: root `anyOf`, runtime fallback, 35 tools | 3 |
| R10-R13: preparation, backend pipelines, failures | 1-2 |
| R14: scenario hashes, bindings, evidence | 2 |
| R15: republish and hydrated Wiki context | 4 |
| R16: surgical files, docs, version, no new tool | 3, 5-7 |

## File map

- `src/iwiki_mcp/server.py`: sole production change.
- `tests/test_server_update.py`, `tests/codegraph/test_frontmatter_roundtrip.py`:
  Git contract and selector lifecycle.
- `tests/postgres/test_section_ops.py`, `tests/postgres/test_tool_matrix.py`:
  PostgreSQL public handler, CAS, embedding, specification evidence.
- `tests/test_mcp_smoke.py`: serialized MCP schema.
- `tests/postgres/conftest.py`, `tests/postgres/test_code_graph_publication.py`:
  update-to-republish hydration.
- `README.md`, `docs/README.ru.md`, `docs/architecture.md`,
  `src/iwiki_mcp/resources.py`: repository contract.
- Iwiki pages are parent-owned; subagents never mutate them.

Tasks 1-3 run strictly sequentially because each owns a bounded portion of
`src/iwiki_mcp/server.py`; no two workers edit that file concurrently.

### Task 1: Git request modes and selector mutation

**Closes:** R1-R8, R10, R12-R13 and the Git half of all three GWT scenarios.

**Ownership:** Fresh subagent owns `src/iwiki_mcp/server.py`,
`tests/test_server_update.py`, and `tests/codegraph/test_frontmatter_roundtrip.py` for
this task. Preserve prior commits; no unrelated cleanup.

**Files:**
- Modify: `src/iwiki_mcp/server.py` near `wiki_update_page`
- Modify: `tests/test_server_update.py`
- Modify: `tests/codegraph/test_frontmatter_roundtrip.py`

- [ ] **Step 1: Write failing signature and mode tests**

Replace the signature assertion and add early-validation coverage:

```python
def test_update_public_signature_exposes_optional_modes_and_trailing_code():
    signature = inspect.signature(server.wiki_update_page)
    assert list(signature.parameters) == [
        "domain", "slug", "heading", "new_body", "source", "description",
        "status", "new_heading", "expected_revision", "expected_section_hash",
        "code",
    ]
    assert signature.parameters["heading"].default is None
    assert signature.parameters["new_body"].default is None
    assert signature.parameters["code"].default is None


def test_update_rejects_invalid_shapes_before_freshness(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page("backend", "auth", BASE_MD)
    monkeypatch.setattr(
        server.sync, "ensure_fresh",
        lambda _base: (_ for _ in ()).throw(AssertionError("freshness reached")),
    )
    partial = server.wiki_update_page(
        "backend", "concept/auth", heading="Flow"
    )
    empty = server.wiki_update_page("backend", "concept/auth")
    metadata = server.wiki_update_page(
        "backend", "concept/auth", code={}, status="stable"
    )
    assert partial["error"] == "heading and new_body must be provided together"
    assert empty["error"] == "no update operation requested"
    assert metadata["error"] == "code-only update cannot change section metadata"
```

- [ ] **Step 2: Verify the tests fail for the missing contract**

```bash
uv run pytest -q tests/test_server_update.py -k "optional_modes or invalid_shapes"
```

Expected: failure from required section arguments or missing `code`; freshness is not
reached.

- [ ] **Step 3: Write failing Git selector lifecycle tests**

Add this semantic core to `tests/codegraph/test_frontmatter_roundtrip.py`:

```python
def test_code_only_sets_replaces_and_clears_without_body_change(
    tmp_path, monkeypatch,
):
    _patch_server(monkeypatch, tmp_path)
    server.wiki_write_page("d", "service", _authored_markdown())
    page = tmp_path / "d" / "concept" / "service.md"
    _, original_body = fm.split(page.read_text(), strict_code=True)

    replacement = {"files": ["src/new.py"]}
    result = server.wiki_update_page("d", "concept/service", code=replacement)
    meta, body = fm.split(page.read_text(), strict_code=True)
    assert "error" not in result
    assert "heading" not in result
    assert result["embedded"] == 0
    assert meta["code"] == replacement
    assert body == original_body

    result = server.wiki_update_page("d", "concept/service", code={})
    meta, body = fm.split(page.read_text(), strict_code=True)
    assert "error" not in result
    assert "code" not in meta
    assert body == original_body


def test_invalid_code_only_preserves_bytes_and_skips_freshness(
    tmp_path, monkeypatch,
):
    _patch_server(monkeypatch, tmp_path)
    server.wiki_write_page("d", "service", _authored_markdown())
    page = tmp_path / "d" / "concept" / "service.md"
    original = page.read_bytes()
    monkeypatch.setattr(
        server.sync, "ensure_fresh",
        lambda _base: (_ for _ in ()).throw(AssertionError("freshness reached")),
    )
    result = server.wiki_update_page(
        "d", "concept/service", code={"modules": ["pkg.service"]}
    )
    assert result["error"] == "unsupported code selector key"
    assert result["hint"] == (
        "use only code.symbols, code.files, and code.source_globs"
    )
    assert page.read_bytes() == original
```

Add a combined test to `tests/test_server_update.py` that asserts updated section,
stored `code`, normalized `heading`, and exactly one `commit_and_push` call.

- [ ] **Step 4: Verify selector tests fail before implementation**

```bash
uv run pytest -q tests/test_server_update.py tests/codegraph/test_frontmatter_roundtrip.py -k "code_only or combines_section_and_code or optional_modes"
```

Expected: failures point to missing `code` and unconditional section logic.

- [ ] **Step 5: Implement the complete request classifier**

Add immediately before `wiki_update_page`:

```python
def _prepare_update_page_request(
    heading: str | None,
    new_body: str | None,
    *,
    source: str | None,
    description: str | None,
    status: str | None,
    new_heading: str | None,
    expected_section_hash: str | None,
    code: dict | None,
) -> tuple[bool, bool, dict | None] | dict:
    section_requested = heading is not None or new_body is not None
    if (heading is None) != (new_body is None):
        return {
            "error": "heading and new_body must be provided together",
            "hint": "provide both for a section update, or omit both for code-only",
        }
    code_requested = code is not None
    if not section_requested and not code_requested:
        return {
            "error": "no update operation requested",
            "hint": "provide heading and new_body, code, or both",
        }
    if code_requested and not section_requested and any(
        value is not None
        for value in (source, description, status, new_heading, expected_section_hash)
    ):
        return {
            "error": "code-only update cannot change section metadata",
            "hint": "omit section-only parameters, or provide heading and new_body",
        }
    replacement = None
    if code_requested:
        try:
            validated = _codegraph_linking.validate_code_mapping(code)
        except _codegraph_linking.SelectorError as exc:
            return {
                "error": str(exc),
                "hint": "use only code.symbols, code.files, and code.source_globs",
            }
        if any(validated.values()):
            replacement = dict(code)
    return section_requested, code_requested, replacement
```

Make `heading`/`new_body` optional and append `code`. Invoke the helper after each
backend write-scope guard and before PostgreSQL revision checks or Git freshness.

- [ ] **Step 6: Implement optional Git section/code application**

Initialize `updated_body = original_body`; run link normalization, section hash,
`replace_section`, and `validate_page` only for section/combined mode. Then apply:

```python
if code_requested:
    if replacement_code is None:
        meta.pop("code", None)
    else:
        meta["code"] = replacement_code
```

Retain current timestamp, specification transaction, rollback, reindex, commit, graph
refresh, and heading-rename transaction. Add `heading` to success results only when
`section_requested` is true.

- [ ] **Step 7: Run full Git update regressions**

```bash
uv run pytest -q tests/test_server_update.py tests/codegraph/test_frontmatter_roundtrip.py
```

Expected: all selected tests pass; pre-existing section call shapes stay green.

- [ ] **Step 8: Commit Task 1**

```bash
git add src/iwiki_mcp/server.py tests/test_server_update.py tests/codegraph/test_frontmatter_roundtrip.py
git commit -m "feat: update wiki page code selectors"
```

Expected: focused commit and no pending Task 1 paths.

### Task 2: PostgreSQL atomicity, CAS, embeddings, and specification evidence

**Closes:** PostgreSQL half of R4-R8, R11-R14 and all three GWT scenarios.

**Ownership:** Fresh subagent owns the PostgreSQL branch of
`src/iwiki_mcp/server.py`, `tests/postgres/test_section_ops.py`, and
`tests/postgres/test_tool_matrix.py`. Do not change `PostgresStore`; its current
single-call `update_page` transaction and unchanged-body chunk reuse are required.

**Files:**
- Modify: `src/iwiki_mcp/server.py` inside the PostgreSQL branch of `wiki_update_page`
- Modify: `tests/postgres/test_section_ops.py`
- Modify: `tests/postgres/test_tool_matrix.py`
- Reference: `tests/postgres/test_store.py`

- [ ] **Step 1: Write failing public PostgreSQL behavior tests**

Using the real `postgres_section_ops` fixture, add tests that prove:

- code-only set and clear preserve `body` exactly, increment revision once, retain only
  PostgreSQL's `page`, `revision`, and `indexed_chunks` fields, and omit `heading`;
- combined section-plus-code changes both values in one call and increments revision
  once;
- stale `expected_revision` returns `conflict` and preserves body and metadata;
- omitted `expected_revision` returns `expected_revision_required`;
- invalid selector keys return before `store.update_page` and preserve the row.

For code-only embedding reuse, monkeypatch the embedding client used by the fixture and
assert its recorded input list stays empty. Keep the existing store-level
`test_update_page_reuses_unchanged_chunk_embeddings` unchanged as lower-level evidence.

- [ ] **Step 2: Write failing GWT evidence preservation test**

Extend the existing specification update test in `test_section_ops.py`: seed one
scenario and one successful `ResolutionAttempt`, record scenario hash, bindings, and
verification evidence, run a code-only selector update, then assert those three values
are unchanged after reread. Use the canonical parser/projector helpers already imported
by that test module.

- [ ] **Step 3: Verify PostgreSQL tests fail at the public handler**

```bash
uv run pytest -q tests/postgres/test_section_ops.py tests/postgres/test_tool_matrix.py -k "code_only or combines_section_and_code or selector_update_preserves or expected_revision"
```

Expected: new calls fail because `heading` and `new_body` remain required or code is not
applied; existing revision tests stay green.

- [ ] **Step 4: Implement one PostgreSQL candidate mutation**

After request classification and revision validation, split the stored markdown once.
Set `updated_body = original_body`; apply section replacement and section-hash checks
only when `section_requested`; apply the complete `code` replacement/removal only when
`code_requested`. Serialize one candidate and call `store.update_page(...)` once with
the caller's `expected_revision`.

Preserve current specification transaction/rollback and PostgreSQL response fields; do
not add `embedded` or an operation discriminator. Include `heading` only when a section
operation ran. Prove zero new embedding calls through the recorded embedding inputs.

- [ ] **Step 5: Run PostgreSQL update and store regressions**

```bash
uv run pytest -q tests/postgres/test_section_ops.py tests/postgres/test_tool_matrix.py tests/postgres/test_store.py -k "update_page or code_only or combines_section_and_code or selector_update_preserves or expected_revision"
```

Expected: selected tests pass; real PostgreSQL tests may skip only when the repository's
documented test database is unavailable, which must be recorded as unverified evidence.

- [ ] **Step 6: Commit Task 2**

```bash
git add src/iwiki_mcp/server.py tests/postgres/test_section_ops.py tests/postgres/test_tool_matrix.py
git commit -m "feat: update postgres page code selectors"
```

Expected: focused commit; production changes remain confined to `server.py`.

### Task 3: FastMCP root operation schema

**Closes:** R1, R9, and the no-new-tool/tool-count portion of R16.

**Ownership:** Fresh subagent owns the schema-registration portion of
`src/iwiki_mcp/server.py`, `tests/test_mcp_smoke.py`, and schema assertions in
`tests/postgres/test_tool_matrix.py`. Preserve all registered tool names and HTTP write
allowlists.

**Files:**
- Modify: `src/iwiki_mcp/server.py` immediately after `wiki_update_page` registration
- Modify: `tests/test_mcp_smoke.py`
- Modify: `tests/postgres/test_tool_matrix.py`

- [ ] **Step 1: Write failing serialized-schema tests**

Read `wiki_update_page` from the FastMCP tool manager and assert exactly:

```python
assert schema["required"] == ["domain", "slug"]
assert schema["anyOf"] == [
    {
        "required": ["heading", "new_body"],
        "properties": {
            "heading": {"type": "string"},
            "new_body": {"type": "string"},
        },
    },
    {
        "required": ["code"],
        "properties": {"code": {"type": "object"}},
    },
]
assert "code" in schema["properties"]
assert len(server.mcp._tool_manager._tools) == 35
```

Also assert section-only, code-only, and combined samples validate against the schema,
while `{domain, slug}`, heading-only, body-only, and null-only samples fail.

- [ ] **Step 2: Verify the schema test fails**

```bash
uv run pytest -q tests/test_mcp_smoke.py tests/postgres/test_tool_matrix.py -k "update_page_schema or tool_count"
```

Expected: generated signature schema lacks root `anyOf`; tool count remains 35.

- [ ] **Step 3: Configure the registered tool schema**

Add and invoke once immediately after `mcp.tool()(wiki_update_page)`:

```python
def _configure_update_page_input_schema() -> None:
    tool = mcp._tool_manager.get_tool("wiki_update_page")
    if tool is None:
        raise RuntimeError("wiki_update_page registration missing")
    parameters = dict(tool.parameters)
    parameters["required"] = ["domain", "slug"]
    parameters["anyOf"] = [
        {
            "required": ["heading", "new_body"],
            "properties": {
                "heading": {"type": "string"},
                "new_body": {"type": "string"},
            },
        },
        {
            "required": ["code"],
            "properties": {"code": {"type": "object"}},
        },
    ]
    tool.parameters = parameters
```

Do not change FastMCP internals, `fn_metadata`, the tool registry, or hosted HTTP write
tool lists. Runtime validation from Tasks 1-2 remains authoritative for clients that do
not enforce schema.

- [ ] **Step 4: Run registry and schema regressions**

```bash
uv run pytest -q tests/test_mcp_smoke.py tests/postgres/test_tool_matrix.py
```

Expected: all pass; serialized schema contains root `anyOf`; tool count is exactly 35.

- [ ] **Step 5: Commit Task 3**

```bash
git add src/iwiki_mcp/server.py tests/test_mcp_smoke.py tests/postgres/test_tool_matrix.py
git commit -m "feat: publish update page operation schema"
```

Expected: schema-only production delta plus tests.

### Task 4: Selector republish and Wiki-context hydration

**Closes:** R15 and end-to-end selector discoverability.

**Ownership:** Fresh subagent owns only `tests/postgres/conftest.py` and
`tests/postgres/test_code_graph_publication.py`. This task changes no production code.

**Files:**
- Modify: `tests/postgres/conftest.py`
- Modify: `tests/postgres/test_code_graph_publication.py`

- [ ] **Step 1: Expose the fixture's Markdown store without duplicating setup**

Add `GraphFixture.markdown_store()` returning the same bound `PostgresStore` currently
created inside `write_markdown_page`; refactor that method to call the helper. Preserve
existing fixture behavior.

- [ ] **Step 2: Write the update-to-republish integration test**

Using `pg_ranked_graph` plus the existing `hosted_empty_code` fixture (which performs
`_bind_hosted_code` and resets server state), select the first published symbol's
canonical selector. Seed a Wiki page without selectors, call the public
`server.wiki_update_page` in code-only mode with current revision, assert
`wiki_code_status` reports stale, republish through the existing begin/batch/finish
helpers, then assert:

```python
assert fresh_status["state"] == "ready"
assert fresh_status["wiki_links"] > 0
assert page_slug in {
    page["slug"] for page in context["wiki_pages"]
}
```

Request context through the public code-context path with Wiki inclusion enabled. Do not
call hosted `wiki_code_index`; this repository test owns a local checkout and publication
fixture.

- [ ] **Step 3: Verify failure before fixture/test support is complete**

```bash
uv run pytest -q tests/postgres/test_code_graph_publication.py -k "selector_update_republishes_wiki_context"
```

Expected: new integration test fails until public update and fixture helper work together.

- [ ] **Step 4: Complete fixture wiring and run publication regressions**

```bash
uv run pytest -q tests/postgres/test_code_graph_publication.py
```

Expected: publication suite passes; code-only update becomes discoverable only after a
fresh publication.

- [ ] **Step 5: Commit Task 4**

```bash
git add tests/postgres/conftest.py tests/postgres/test_code_graph_publication.py
git commit -m "test: verify selector republish hydration"
```

Expected: test-only commit.

### Task 5: Public contract documentation and package version

**Closes:** documentation and version parts of R16.

**Ownership:** Fresh subagent owns `README.md`, `docs/README.ru.md`,
`docs/architecture.md`, `src/iwiki_mcp/resources.py`, `tests/test_resources.py`, and
verification of the already-applied `0.7.226` version bump. Do not bump again for this
change set.

**Files:**
- Modify: `README.md`
- Modify: `docs/README.ru.md`
- Modify: `docs/architecture.md`
- Modify: `src/iwiki_mcp/resources.py`
- Modify: `tests/test_resources.py`
- Verify: `pyproject.toml`, `uv.lock`, `src/iwiki_mcp/__init__.py`, `tests/test_package.py`

- [ ] **Step 1: Write failing resource-contract assertions**

Extend `tests/test_resources.py` to require the agent-facing resource text to state:

- `wiki_update_page` accepts section-only, code-only, and combined modes;
- code-only preserves body and replaces or clears the whole code mapping;
- combined mode is one atomic backend mutation;
- omitted or null `code` preserves existing selectors;
- empty/all-empty `code` clears selectors;
- PostgreSQL still requires current `expected_revision`.

- [ ] **Step 2: Verify resource assertions fail**

```bash
uv run pytest -q tests/test_resources.py -k "update_page"
```

Expected: current resource documents only section updates.

- [ ] **Step 3: Update repository documentation**

Document the same contract in English `README.md`, Russian `docs/README.ru.md`, and
English `docs/architecture.md`. Include a compact JSON example for code-only and combined
calls, the root `anyOf` guarantee, Git freshness/commit semantics, PostgreSQL CAS, and
republish requirement for refreshed code-graph Wiki links. Do not describe bulk edits,
selector inference, migration, or a new MCP tool.

Update `src/iwiki_mcp/resources.py` with matching operational guidance.

- [ ] **Step 4: Verify package version is synchronized once**

```bash
uv run python - <<'PY'
from pathlib import Path
import re

expected = "0.7.226"
assert f'version = "{expected}"' in Path("pyproject.toml").read_text()
assert f'__version__ = "{expected}"' in Path("src/iwiki_mcp/__init__.py").read_text()
assert f'assert iwiki_mcp.__version__ == "{expected}"' in Path("tests/test_package.py").read_text()
assert re.search(rf'name = "iwiki-mcp"\nversion = "{re.escape(expected)}"', Path("uv.lock").read_text())
PY
```

Expected: exit 0. Base was `0.7.225`; branch uses one patch bump to `0.7.226`.

- [ ] **Step 5: Run documentation/resource regressions**

```bash
uv run pytest -q tests/test_resources.py tests/test_package.py
```

Expected: all pass.

- [ ] **Step 6: Commit Task 5**

```bash
git add README.md docs/README.ru.md docs/architecture.md src/iwiki_mcp/resources.py tests/test_resources.py pyproject.toml uv.lock src/iwiki_mcp/__init__.py tests/test_package.py
git commit -m "docs: describe selector page updates"
```

Expected: docs/resource/version commit; repository version remains `0.7.226`.

### Task 6: Parent-owned iwiki and specification reconciliation

**Closes:** durable Wiki, code-graph, and GWT maintenance required by R14-R16.

**Ownership:** Parent agent only. Subagents must not call mutating iwiki tools or write
task ledger pages.

**Targets:**
- Iwiki: `iwiki-mcp/reference/code-selector-frontmatter-update-gap`
- Iwiki: `iwiki-mcp/architecture`
- Iwiki: `iwiki-mcp/authoring-and-linting`
- Iwiki: `iwiki-mcp/concept/code-graph-wiki-linking`
- Iwiki task/history pages for this topic

- [ ] **Step 1: Refresh or report local code-graph state**

Call `wiki_code_status`. After Python symbol changes, call `wiki_code_index` only through
a local MCP server that has this checkout. If the active hosted HTTP server returns
`source_unavailable`, record that state and use the published snapshot for reads; do not
misreport it as fresh.

- [ ] **Step 2: Resolve all three approved scenarios**

Call `wiki_spec_resolve` for:

- `update-existing-wiki-code-selectors`
- `atomically-update-wiki-section-and-selectors`
- `reject-unsafe-wiki-selector-update`

Preserve declared selectors. Record ambiguous, stale, or unresolved bindings as findings;
never guess replacements.

- [ ] **Step 3: Update affected iwiki pages with CAS**

Immediately before each mutation, read the full page and touched heading, then pass the
current `revision` and `section_hash`. Update only the sections that describe
`wiki_update_page`, selector authorship, and code-graph link refresh. Document the root
`anyOf`, code-only/combined semantics, full replacement/clear rule, PostgreSQL CAS, Git
freshness, and republish behavior.

- [ ] **Step 4: Lint and append durable evidence**

Run `wiki_lint`; treat task-page orphan advice as expected, but resolve any new broken
link, stale source, or structural finding caused by this task. Append commands, exit
status, repository revision, spec-resolution state, Wiki revisions, and lint outcome to
the bounded task history. Create and link a successor history segment before event 21.

Expected: Wiki contract matches implemented behavior; task lifecycle remains
`in-progress` until result reconciliation and PR creation.

### Task 7: Whole-branch review, result gate, and PR

**Closes:** specification section 11 verification matrix, R1-R16 result evidence, and
delivery.

**Ownership:** Parent owns verification, task-ledger transitions, commits, push, and PR.
A fresh read-only reviewer owns whole-branch review. Any fixes return to a fresh bounded
worker with explicit file ownership, followed by re-review.

- [ ] **Step 1: Run focused behavioral suite**

```bash
uv run pytest -q tests/test_server_update.py tests/codegraph/test_frontmatter_roundtrip.py tests/test_mcp_smoke.py tests/test_resources.py tests/test_package.py tests/postgres/test_section_ops.py tests/postgres/test_tool_matrix.py tests/postgres/test_store.py tests/postgres/test_code_graph_publication.py
```

Expected: all selected tests pass, with PostgreSQL skips recorded only when infrastructure
is genuinely unavailable.

- [ ] **Step 2: Run full repository verification**

```bash
uv run pytest -q
uv run iwiki-mcp --help
git diff --check origin/master...HEAD
git status --short
```

Expected: pytest exit 0; CLI help exit 0; diff check empty; status contains no unintended
or uncommitted files.

- [ ] **Step 3: Verify public registry, schema, and version invariants**

```bash
uv run python - <<'PY'
from iwiki_mcp import __version__
from iwiki_mcp import server

tool = server.mcp._tool_manager.get_tool("wiki_update_page")
assert tool is not None
assert len(server.mcp._tool_manager._tools) == 35
assert tool.parameters["required"] == ["domain", "slug"]
assert tool.parameters["anyOf"][0]["required"] == ["heading", "new_body"]
assert tool.parameters["anyOf"][1]["required"] == ["code"]
assert __version__ == "0.7.226"
PY
```

Expected: exit 0.

- [ ] **Step 4: Request thorough whole-branch code review**

Use `superpowers:requesting-code-review` with intent, approved spec, approved plan, and
`origin/master...HEAD`. Reviewer must inspect runtime modes, exact root schema, Git and
PostgreSQL atomicity/CAS, body preservation, selector errors, specification evidence,
republish coverage, docs, version, and tool count. Record all findings with severity and
file/line evidence.

- [ ] **Step 5: Resolve findings and reverify**

For each valid finding, use `superpowers:receiving-code-review`, add a failing regression
first when behavior changes, apply the smallest fix, rerun focused and full commands, and
request re-review. Continue until no blocking or important findings remain.

- [ ] **Step 6: Run chain result reconciliation**

Run `$check-chain result docs/superpowers/plans/2026-09-01-code-selector-frontmatter-update-gap.md`.
Expected verdict: `OK`, with intent/spec/plan/result evidence aligned. On `needs_work`,
remain in result stage, change strategy, fix, reverify, and rerun.

- [ ] **Step 7: Finalize durable task state and commit artifacts**

Append final verification and result-gate evidence to task history. Set task lifecycle
`completion-pending` until the PR URL exists. Commit any reviewed documentation or
chain-metadata changes using Conventional Commits; never amend unrelated commits.

- [ ] **Step 8: Push and open the PR**

Use `apply_patch` to create `/tmp/code-selector-frontmatter-update-gap-pr.md` with the
reviewed summary, compatibility notes, exact test outputs, Wiki/spec evidence, and
configuration/migration impact (`none`). Then run:

```bash
git push -u origin dev-code-selector-frontmatter-update-gap
gh pr create --base master --head dev-code-selector-frontmatter-update-gap --title "feat: update wiki page code selectors" --body-file /tmp/code-selector-frontmatter-update-gap-pr.md
```

After obtaining the PR URL, record it in the task ledger, set lifecycle `done`, reread
the page, run task-page lint, and stop. Do not merge the PR.
