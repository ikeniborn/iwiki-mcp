---
review:
  plan_hash: c4faa3021d3b5042
  last_run: 2026-08-10
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings:
    - id: F-001
      phase: coverage
      severity: CRITICAL
      section: "Task 2: SQLite schema, lifecycle states, and recovery primitives"
      section_hash: b60443e13cc51a11
      fragment: "Put the exact five-table and eleven-index SQL from spec Section 7 in `schema.py`."
      text: >-
        Spec Section 7.4 defines twelve required indexes; the plan instructs
        implementing eleven, so the schema-parity constant and the created schema
        would be missing one index and AC-06 index parity cannot pass.
      fix: >-
        Replace "eleven-index" with "twelve-index" and assert the exact index
        name set from spec Section 7.4 in the Task 2 test.
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-002
      phase: coverage
      severity: CRITICAL
      section: "Task 9: Wiki selector parsing and derived links"
      section_hash: e45054151d4639ca
      fragment: "Extend frontmatter parsing to preserve a `code` mapping without changing existing normalized fields."
      text: >-
        R-021/R-024 require authored `code:` selectors to survive Wiki authoring,
        but `engine/frontmatter.split/render` is a flat key/value subset that
        mangles nested mappings, and `okf.build_frontmatter` rebuilds the block
        from a fixed key set, so `wiki_write_page` / `wiki_update_page` drop the
        selectors. The plan touches neither `okf.py` nor the server write path and
        has no round-trip test, so AC-21/AC-24 are unverifiable as planned.
      fix: >-
        Add nested-mapping parse/render support plus preservation in
        `okf.build_frontmatter`, list `src/iwiki_mcp/okf.py` in the Task 9 files,
        and add a write/update round-trip test asserting `code:` survives
        `wiki_write_page` and `wiki_update_page` unchanged.
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-003
      phase: consistency
      severity: CRITICAL
      section: "(document)"
      section_hash: c4faa3021d3b5042
      fragment: null
      text: >-
        The approved intent states that any task touching proposal-first or
        no-autonomy decisions is marked HUMAN CHECKPOINT in the plan, and its
        Done-when requires identifying all remaining human checkpoints. The plan
        contains no HUMAN CHECKPOINT marker although Task 1 (dependency packaging,
        symbol-ID format), Task 2 (final SQL schema), Task 5 (resolver semantics),
        Task 6 (stale/rebuild state transitions) and Task 10 (MCP JSON contracts)
        all touch proposal-first decisions.
      fix: >-
        Mark the affected tasks HUMAN CHECKPOINT with the approval already
        recorded in spec Section 20, or add an explicit statement that spec
        Section 20 closed those forks and no open checkpoint remains.
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-004
      phase: coverage
      severity: WARNING
      section: "(document)"
      section_hash: c4faa3021d3b5042
      fragment: null
      text: >-
        AC-02, AC-05 and AC-24 are claimed as produced (Tasks 1, 2, 9) but no step
        verifies them: no test proves that `enabled=false` creates and reads no
        code database, none asserts the existing Wiki graph schema is unchanged,
        and none asserts that no MVP path mutates Wiki `code` selectors.
      fix: >-
        Add the three assertions to the owning tasks or move the AC claim to the
        task that actually verifies it.
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-005
      phase: consistency
      severity: WARNING
      section: "Unit A — contracts, storage, and lifecycle"
      section_hash: dc160ca9f5025522
      fragment: null
      text: >-
        Spec Section 17.1 assigns R-008 and R-009 to Unit A and Section 17.2
        assigns R-016 plus the `status`/`index`/`search` tool exposure to Unit B.
        The plan defers R-008/R-009 to Tasks 6 and 12 and all MCP registration to
        Task 10 in Unit C, which conflicts with R-029/AC-29 requiring the three
        delivery units of Section 17 to be preserved.
      fix: >-
        Realign task-to-unit assignment with spec Section 17, or amend Section 17
        through the spec before planning.
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-006
      phase: consistency
      severity: WARNING
      section: "Task 1: Configuration, locations, models, and mandatory dependencies"
      section_hash: 83776e5357fb77ed
      fragment: "class SymbolRecord:"
      text: >-
        The exact contracts omit columns the spec Section 7 schema declares NOT
        NULL or required: `SymbolRecord` has no `content_hash` (NOT NULL), no
        `visibility` and no `metadata_json`; `FileRecord` has no `repository_id`
        (NOT NULL foreign key). Task 2 inserts cannot satisfy the schema without
        changing the Task 1 contract.
      fix: >-
        Extend the Task 1 record definitions with the missing schema fields or
        state explicitly where each NOT NULL column is supplied during insert.
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-007
      phase: consistency
      severity: WARNING
      section: "Task 3: Safe discovery and deterministic fingerprints"
      section_hash: cc7db1d199fbafcc
      fragment: "snapshot = discover_python(project, CodeGraphConfig())"
      text: >-
        R-015/AC-15 require the core discovery module to hold no language-specific
        rules, but the planned public entry point of `discovery.py` is
        `discover_python`, a language-named core API.
      fix: >-
        Rename to a language-neutral entry point taking the adapter or extension
        set, for example `discover_sources(project, config, adapter)`.
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-008
      phase: coverage
      severity: WARNING
      section: "Task 3: Safe discovery and deterministic fingerprints"
      section_hash: cc7db1d199fbafcc
      fragment: "**Closes:** R-010, R-011, R-012; produces AC-10, AC-11, and AC-12."
      text: >-
        Task 3 claims to close R-010 and produce AC-10, but full rebuild versus
        fingerprint no-op is implemented in Task 6, which claims the same
        requirement. The duplicate claim makes the traceability table unreliable.
      fix: "Drop R-010/AC-10 from Task 3 and keep the single claim in Task 6."
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-009
      phase: verifiability
      severity: INFO
      section: "Task 3: Safe discovery and deterministic fingerprints"
      section_hash: cc7db1d199fbafcc
      fragment: "discover_python(project, CodeGraphConfig())"
      text: >-
        The Task 3 test constructs `CodeGraphConfig()` with no arguments while
        Task 1 specifies only `CodeGraphConfig.from_mapping`; the default
        constructor contract is unspecified.
      fix: "State the default constructor in Task 1 or use `from_mapping({})` in Task 3."
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-010
      phase: verifiability
      severity: INFO
      section: "(document)"
      section_hash: c4faa3021d3b5042
      fragment: null
      text: >-
        The intent health metric "source text, credentials and secret-like files
        appear in 0 external embedding requests" has no verifying step; only
        sanitized logging is covered through AC-25.
      fix: >-
        Add an assertion that the code-graph build path issues no embedding call,
        for example by failing the test if `indexer.embed_texts` is invoked.
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-011
      phase: dependencies
      severity: CRITICAL
      section: "Task 9: Wiki selector parsing and derived links"
      section_hash: e45054151d4639ca
      fragment: "server.wiki_code_context([\"pkg.Service.run\"], include_wiki=True)"
      text: >-
        Task 9 called `wiki_code_context` before Task 10 registered it, so its
        RED/GREEN cycle could not pass in delivery order.
      fix: >-
        Keep Task 9 round-trip coverage on write/update/index/lint and move the
        context non-mutation assertion to Task 10.
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-012
      phase: coverage
      severity: CRITICAL
      section: "Task 1: Configuration, locations, models, and mandatory dependencies"
      section_hash: 83776e5357fb77ed
      fragment: "CodeGraphConfig.from_mapping"
      text: >-
        The plan defined mapping parsing but no source for the `[code_graph]`
        mapping because Binding intentionally discards raw project config.
      fix: >-
        Add `load_code_graph_config(project_dir)` using `load_project_config`,
        enumerate the four environment overrides, and test TOML plus overrides.
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-013
      phase: verifiability
      severity: CRITICAL
      section: "Task 6: Full-build indexer and runtime state facade"
      section_hash: 99212348bd6efefc
      fragment: "tests/codegraph/conftest.py"
      text: >-
        Tasks 6 through 12 depended on undefined fixtures and test-double
        attributes, while the repository had no codegraph conftest.
      fix: >-
        Create the shared conftest in Task 6, define its exact harness contract,
        and extend it only in Tasks 9, 11, and 12 as dependencies become ready.
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-014
      phase: verifiability
      severity: CRITICAL
      section: "Task 14: User documentation, Wiki update, and final regression gates"
      section_hash: 97bf92e2370e7578
      fragment: "uv run flake8 src tests eval"
      text: >-
        The repository-wide lint command already fails on an unrelated
        `eval/search_pipeline` E501, making final evidence red before this work.
      fix: >-
        Scope this delivery's lint gate to `src tests eval/code_graph` and name
        the pre-existing out-of-scope finding explicitly.
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-015
      phase: coverage
      severity: WARNING
      section: "Task 1: Configuration, locations, models, and mandatory dependencies"
      section_hash: 83776e5357fb77ed
      fragment: "test_main_does_not_import_tree_sitter_language_pack"
      text: >-
        Existing startup tests did not prove lazy Tree-sitter grammar/parser
        initialization required by AC-03.
      fix: >-
        Add a startup test proving the grammar pack remains absent after main
        and a separate installed-dependency import command.
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-016
      phase: coverage
      severity: WARNING
      section: "Task 6: Full-build indexer and runtime state facade"
      section_hash: 99212348bd6efefc
      fragment: "test_nonready_state_guard_and_bounded_auto_rebuild"
      text: >-
        AC-08 had no state-driven proof for missing/dirty/rebuilding/failed or
        the bounded auto-rebuild branch.
      fix: >-
        Cover all five runtime states and bounded rebuild in Task 6, then add
        dirty search/context integration assertions in Tasks 7 and 8.
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-017
      phase: coverage
      severity: WARNING
      section: "Task 1: Configuration, locations, models, and mandatory dependencies"
      section_hash: 83776e5357fb77ed
      fragment: "CodeGraphLocationResolver(base, \"../unsafe\", project)"
      text: "AC-04 claimed unsafe-domain rejection without an assertion."
      fix: "Add an explicit unsafe-domain exception test in Task 1."
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-018
      phase: coverage
      severity: WARNING
      section: "Task 2: SQLite schema, lifecycle states, and recovery primitives"
      section_hash: b60443e13cc51a11
      fragment: "DELETE FROM files WHERE file_id = ?"
      text: "AC-22 lacked SQL proof that stale Wiki-code links cascade away."
      fix: >-
        Seed schema-valid symbol/file links and assert file/repository deletion
        removes links and dependent rows.
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-019
      phase: coverage
      severity: WARNING
      section: "Task 6: Full-build indexer and runtime state facade"
      section_hash: 99212348bd6efefc
      fragment: "test_build_logs_are_sanitized_and_cache_is_git_ignored"
      text: >-
        AC-25 claimed sanitized logs without checking source, secrets, or
        absolute project paths in failure logs.
      fix: "Add caplog assertions around an injected build failure."
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-020
      phase: coverage
      severity: CRITICAL
      section: "Task 1: Configuration, locations, models, and mandatory dependencies"
      section_hash: 83776e5357fb77ed
      fragment: "base.ensure_graph_store_excluded(base)"
      text: >-
        Planned code-cache paths were not connected to the existing Git
        exclusion mechanism, risking accidental cache commits during sync.
      fix: >-
        Exclude the cache during location resolution and test both check-ignore
        and clean git status after a build.
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-021
      phase: coverage
      severity: WARNING
      section: "Task 12: Recovery and multi-process concurrency evidence"
      section_hash: 4253dff784ac2e08
      fragment: "test_branch_switch_forces_full_rebuild"
      text: >-
        Section 16.3 branch-switch and dirty added/changed/deleted full-rebuild
        cases had no task owner.
      fix: "Add the four source-transition cases to Task 12."
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-022
      phase: consistency
      severity: CRITICAL
      section: "Task 1: Configuration, locations, models, and mandatory dependencies"
      section_hash: 83776e5357fb77ed
      fragment: "class SearchResult:"
      text: >-
        SearchResult omitted fields required by the public result contract and
        accessed by Task 7 tests.
      fix: >-
        Add kind, local name, signature, relative path, and line/byte ranges to
        the exact Task 1 model and assert their presence in Task 7.
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-023
      phase: consistency
      severity: CRITICAL
      section: "Task 5: Imports, calls, inheritance, and conservative resolution"
      section_hash: b456974ded320915
      fragment: "relation_id(language, source_identity, relation_type, source_location, target_identity_or_reference)"
      text: >-
        Task 5 assigned relation IDs without the canonical Section 8 formula,
        leaving AC-12 relation determinism undefined.
      fix: >-
        Define the exact helper in Task 1, call it explicitly in Task 5, and
        compare IDs across relocated fixtures.
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-024
      phase: consistency
      severity: INFO
      section: "Task 1: Configuration, locations, models, and mandatory dependencies"
      section_hash: 83776e5357fb77ed
      fragment: "LANGUAGE_PREFIXES = {\"python\": \"py\"}"
      text: "The `_language_prefix` helper was used but not defined."
      fix: "Define the exact Python prefix map and unsupported-language error."
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-025
      phase: consistency
      severity: CRITICAL
      section: "Unit B — discovery, Python indexing, resolution, and search"
      section_hash: 40d813c6b7a1ff41
      fragment: null
      text: >-
        F-005 was closed prematurely: the linked spec assigned contradictory
        requirement ranges and outputs to Unit A and Unit B.
      fix: >-
        Amend spec Section 17 first, move Task 3 under Unit B, and make each
        cross-unit requirement portion and evidence owner explicit.
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-026
      phase: consistency
      severity: WARNING
      section: "Task 9: Wiki selector parsing and derived links"
      section_hash: e45054151d4639ca
      fragment: "Extend the stdlib-only frontmatter parser/renderer only for the exact nested `code` contract"
      text: >-
        Nested `code` parsing/rendering could regress existing flat-frontmatter,
        OKF, validation, export, resource, and lint consumers.
      fix: >-
        Add every named existing frontmatter/OKF/resource/lint test file to the
        Task 9 regression command.
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-027
      phase: verifiability
      severity: WARNING
      section: "Task 2: SQLite schema, lifecycle states, and recovery primitives"
      section_hash: b60443e13cc51a11
      fragment: "with closing(store.connect()) as connection:"
      text: >-
        Using a raw sqlite3 connection as a context manager commits or rolls
        back but does not close the handle before quarantine/replace tests.
      fix: >-
        Require `contextlib.closing` for raw connections and internal closure in
        every store helper.
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-028
      phase: verifiability
      severity: WARNING
      section: "Task 14: User documentation, Wiki update, and final regression gates"
      section_hash: 97bf92e2370e7578
      fragment: "test_readme_documents_code_tools_and_deferred_scope"
      text: >-
        Task 14 omitted tests/test_package.py from Files and could produce pytest
        exit 5 before the new `-k code_tools` test existed.
      fix: >-
        List the file, create the named test in Step 1, and require a collected
        assertion failure rather than empty selection.
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-029
      phase: consistency
      severity: WARNING
      section: "Task 13: Quality and performance benchmark"
      section_hash: 311e7cc7a3f5c365
      fragment: "A missed provisional threshold writes evidence, raises `BenchmarkGateError`"
      text: >-
        Benchmark threshold failures were reported but did not gate acceptance
        or invoke the intent's escalation stop rule.
      fix: >-
        Make threshold misses persist evidence, fail the CLI, stop downstream
        work, and return to the human checkpoint without weakening targets.
      verdict: fixed
      verdict_at: 2026-08-09
    - id: F-030
      phase: coverage
      severity: CRITICAL
      section: "Task 10: Register bounded context and complete the four-tool contract"
      section_hash: 1ae58853f546faca
      fragment: "assert_exact_code_tool_registry((await session.list_tools()).tools)"
      text: >-
        Comparing four Python function names did not prove FastMCP registration
        or the absence of a fifth code tool.
      fix: >-
        Inspect the real stdio `list_tools` result, assert the exact code-tool
        set, and prove every schema omits `domain`.
      verdict: fixed
      verdict_at: 2026-08-09
chain:
  intent: docs/superpowers/intents/2026-08-09-iwiki-mcp-code-graph-intent.md
  spec: docs/superpowers/specs/2026-08-09-iwiki-mcp-code-graph-design.md
---

# iwiki-mcp Python Code Graph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the approved Python-only code-graph MVP with deterministic full rebuilds, four fail-soft MCP tools, bounded source context, and Wiki-code selectors.

**Architecture:** Add an isolated `iwiki_mcp.codegraph` package behind a request-scoped runtime facade. Build immutable per-primary-domain SQLite snapshots from safe Tree-sitter extraction, publish them atomically, and keep all Wiki behavior available when the code graph is disabled, stale, busy, or failed.

**Tech Stack:** Python 3.10+, SQLite WAL, `filelock`, `pathspec`, `tree-sitter>=0.26.0`, `tree-sitter-language-pack>=1.13.3`, FastMCP, pytest, pytest-asyncio.

**Design:** `docs/superpowers/specs/2026-08-09-iwiki-mcp-code-graph-design.md`
**Status:** approved

---

## File map

| Path | Responsibility |
|---|---|
| `src/iwiki_mcp/codegraph/models.py` | Immutable normalized records, stable IDs, result types |
| `src/iwiki_mcp/codegraph/config.py` | `[code_graph]` and environment configuration |
| `src/iwiki_mcp/codegraph/location.py` | Safe per-primary cache paths |
| `src/iwiki_mcp/codegraph/schema.py` | Schema v1 SQL and parity metadata |
| `src/iwiki_mcp/codegraph/store.py` | SQLite reads/writes, integrity, staging publication |
| `src/iwiki_mcp/codegraph/discovery.py` | Contained source enumeration and exclusions |
| `src/iwiki_mcp/codegraph/fingerprint.py` | Git/config/parser/source fingerprints and revisions |
| `src/iwiki_mcp/codegraph/languages/base.py` | Adapter protocol |
| `src/iwiki_mcp/codegraph/languages/python.py` | Tree-sitter Python extraction |
| `src/iwiki_mcp/codegraph/resolver.py` | Conservative project-local resolution |
| `src/iwiki_mcp/codegraph/linking.py` | Wiki selector parsing and derived links |
| `src/iwiki_mcp/codegraph/indexer.py` | Full build/no-op pipeline |
| `src/iwiki_mcp/codegraph/query.py` | Deterministic symbol search |
| `src/iwiki_mcp/codegraph/context.py` | Bounded traversal and safe source reads |
| `src/iwiki_mcp/codegraph/runtime.py` | Binding/config/state facade and diagnostics |
| `src/iwiki_mcp/server.py` | Four MCP registrations and code-aware lint composition |
| `eval/code_graph/` | Reproducible quality/performance benchmark |
| `tests/codegraph/` | Unit, golden, security, integration, concurrency tests |

## Human checkpoints

The proposal-first decisions touched by Tasks 1, 2, 5, 6, and 10 are **HUMAN CHECKPOINT — CLOSED** by the approved choices in design Sections 2.3, 17, and 20. Implementation may execute those exact contracts without another pause. Any deviation in dependency packaging, identity format, SQL schema, resolver semantics, stale/rebuild transitions, MCP JSON contracts, or delivery-unit boundaries reopens the checkpoint and returns to design review.

## Unit A — contracts, storage, and lifecycle

### Task 1: Configuration, locations, models, and mandatory dependencies

**Closes:** R-001, R-002, R-003, and R-004; produces the configuration/model portions of AC-01, AC-02, AC-03, AC-04, and AC-12. Runtime proof for AC-01 and AC-02 belongs to Tasks 6, 7, and 10; Task 3 owns R-012/AC-12.

**HUMAN CHECKPOINT — CLOSED:** Dependency packaging and symbol-ID format were approved in design Sections 2.3, 6, and 8.

**Files:**
- Create: `src/iwiki_mcp/codegraph/__init__.py`
- Create: `src/iwiki_mcp/codegraph/config.py`
- Create: `src/iwiki_mcp/codegraph/location.py`
- Create: `src/iwiki_mcp/codegraph/models.py`
- Create: `tests/codegraph/__init__.py`
- Create: `tests/codegraph/test_config_location_models.py`
- Modify: `tests/test_server_startup.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

- [ ] **Step 1: Write failing config, location, and ID tests**

```python
import importlib.util
import subprocess

import pytest

from iwiki_mcp.codegraph.config import (
    CodeGraphConfig,
    CodeGraphConfigError,
    load_code_graph_config,
)
from iwiki_mcp.codegraph.location import (
    CodeGraphLocationError,
    CodeGraphLocationResolver,
)
from iwiki_mcp.codegraph.models import file_id, relation_id, symbol_id


def test_codegraph_config_location_and_ids(tmp_path):
    project = tmp_path / "project"
    base = tmp_path / "wiki"
    project.mkdir()
    base.mkdir()
    cfg = CodeGraphConfig.from_mapping({"enabled": True, "languages": ["python"]})
    paths = CodeGraphLocationResolver(base, "backend", project).resolve()

    assert cfg.languages == ("python",)
    assert paths.database == base / ".iwiki" / "code-backend.sqlite3"
    assert paths.lock == base / ".iwiki" / "code-backend.lock"
    assert file_id("backend", "python", "src/pkg/a.py") == file_id(
        "backend", "python", "src/pkg/a.py"
    )
    assert symbol_id("python", "backend", "pkg.a", "Thing.run", "(self,x)").startswith(
        "py:symbol:"
    )
    relation = relation_id(
        "python", "pkg.a:Thing.run", "CALLS", "10:4", "pkg.b:work"
    )
    assert relation == relation_id(
        "python", "pkg.a:Thing.run", "CALLS", "10:4", "pkg.b:work"
    )
    assert importlib.util.find_spec("tree_sitter") is not None
    assert importlib.util.find_spec("tree_sitter_language_pack") is not None


def test_config_rejects_incremental_and_unknown_language():
    for mapping in (
        {"languages": ["typescript"]},
        {"incremental": True},
    ):
        try:
            CodeGraphConfig.from_mapping(mapping)
        except CodeGraphConfigError:
            continue
        raise AssertionError("invalid code graph configuration was accepted")


def test_project_config_and_four_environment_overrides(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".iwiki.toml").write_text(
        "[code_graph]\n"
        "enabled = true\n"
        "max_file_bytes = 100\n"
        "max_total_files = 10\n"
        "auto_rebuild = \"off\"\n"
    )
    monkeypatch.setenv("IWIKI_CODE_GRAPH_ENABLED", "false")
    monkeypatch.setenv("IWIKI_CODE_GRAPH_MAX_FILE_BYTES", "200")
    monkeypatch.setenv("IWIKI_CODE_GRAPH_MAX_FILES", "20")
    monkeypatch.setenv("IWIKI_CODE_GRAPH_AUTO_REBUILD", "bounded")

    cfg = load_code_graph_config(project)

    assert cfg.enabled is False
    assert cfg.max_file_bytes == 200
    assert cfg.max_total_files == 20
    assert cfg.auto_rebuild == "bounded"


def test_location_rejects_unsafe_domain_and_excludes_cache_from_git(tmp_path):
    base = tmp_path / "wiki"
    project = tmp_path / "project"
    base.mkdir()
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=base, check=True)

    with pytest.raises(CodeGraphLocationError):
        CodeGraphLocationResolver(base, "../unsafe", project).resolve()

    paths = CodeGraphLocationResolver(base, "backend", project).resolve()
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", str(paths.database)],
        cwd=base,
        check=False,
    )
    assert ignored.returncode == 0
```

Add `test_main_does_not_import_tree_sitter_language_pack` to `tests/test_server_startup.py`: remove `tree_sitter_language_pack` from `sys.modules`, stub the existing config/probe/MCP-run path, call `server.main()`, and assert the module remains absent. This proves normal startup neither imports the grammar pack nor calls `get_parser`.

- [ ] **Step 2: Run the focused test and confirm import failure**

```bash
uv run pytest -q tests/codegraph/test_config_location_models.py
```

Expected: collection fails with `ModuleNotFoundError: No module named 'iwiki_mcp.codegraph'`.

- [ ] **Step 3: Add dependencies and minimal exact contracts**

```toml
dependencies = [
    "mcp>=1.2.0",
    "httpx>=0.27",
    "pathspec>=0.12",
    "filelock>=3.12",
    "numpy>=1.26",
    "tree-sitter>=0.26.0",
    "tree-sitter-language-pack>=1.13.3",
    "tomli>=2.0; python_version < '3.11'",
]
```

```python
@dataclass(frozen=True)
class CodeGraphPaths:
    database: Path
    wal: Path
    shm: Path
    lock: Path
    metadata: Path


@dataclass(frozen=True)
class FileRecord:
    file_id: str
    repository_id: str
    path: str
    language: str
    content_hash: str
    parser_version: str
    size_bytes: int


@dataclass(frozen=True)
class SymbolRecord:
    symbol_id: str
    file_id: str
    kind: str
    qualified_name: str
    local_name: str
    start_line: int
    end_line: int
    start_byte: int | None
    end_byte: int | None
    signature: str | None
    visibility: str | None
    content_hash: str
    metadata_json: str


@dataclass(frozen=True)
class ReferenceRecord:
    source_symbol_id: str | None
    source_file_id: str
    relation_type: str
    target_reference: str | None
    source_line: int | None


@dataclass(frozen=True)
class RelationRecord:
    relation_id: str
    source_symbol_id: str | None
    source_file_id: str
    target_symbol_id: str | None
    target_reference: str | None
    relation_type: str
    source_line: int | None
    confidence: float
    resolution_state: str
    metadata_json: str


@dataclass(frozen=True)
class ParsedFile:
    file: FileRecord
    symbols: tuple[SymbolRecord, ...]
    references: tuple[ReferenceRecord, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ResolutionResult:
    relations: tuple[RelationRecord, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class SearchResult:
    symbol_id: str
    kind: str
    qualified_name: str
    local_name: str
    signature: str | None
    path: str
    start_line: int
    end_line: int
    start_byte: int | None
    end_byte: int | None
    match: str


class CodeGraphError(RuntimeError):
    code = "code_graph_error"


LANGUAGE_PREFIXES = {"python": "py"}


def _language_prefix(language: str) -> str:
    try:
        return LANGUAGE_PREFIXES[language]
    except KeyError as exc:
        raise ValueError("unsupported code graph language") from exc


def file_id(domain: str, language: str, path: str) -> str:
    value = "\0".join(("file", domain, language, PurePosixPath(path).as_posix()))
    return f"{_language_prefix(language)}:file:{sha256(value.encode()).hexdigest()}"


def symbol_id(
    language: str,
    domain: str,
    module: str,
    qualified: str,
    signature: str,
) -> str:
    value = "\0".join(("symbol", language, domain, module, qualified, signature))
    return f"{_language_prefix(language)}:symbol:{sha256(value.encode()).hexdigest()}"


def relation_id(
    language: str,
    source_identity: str,
    relation_type: str,
    source_location: str,
    target_identity_or_reference: str,
) -> str:
    value = "\0".join(
        (
            "relation",
            source_identity,
            relation_type,
            source_location,
            target_identity_or_reference,
        )
    )
    return (
        f"{_language_prefix(language)}:relation:"
        f"{sha256(value.encode()).hexdigest()}"
    )
```

Implement `CodeGraphConfig.from_mapping` with the defaults and allowed fields from spec Section 6.3, explicit integer/boolean validation, exact `auto_rebuild = "off"|"bounded"` enum validation, rejection of `database`, `project_id`, `project_uuid`, and `incremental`, and only `python` as a language. Implement `load_code_graph_config(project_dir)` as `base.load_project_config(project_dir).get("code_graph", {})` followed by the four exact environment overrides `IWIKI_CODE_GRAPH_ENABLED`, `IWIKI_CODE_GRAPH_MAX_FILE_BYTES`, `IWIKI_CODE_GRAPH_MAX_FILES`, and `IWIKI_CODE_GRAPH_AUTO_REBUILD`; the runtime must call it with `binding.project_dir`. Implement `CodeGraphLocationResolver.resolve` by validating the domain with the same rules as `server._validate_domain`, resolving `base/.iwiki`, calling `base.ensure_graph_store_excluded(base)`, and returning the five exact paths.

- [ ] **Step 4: Lock dependency resolution and run focused tests**

```bash
uv lock
uv run python -c "import tree_sitter, tree_sitter_language_pack"
uv run pytest -q tests/codegraph/test_config_location_models.py tests/test_server_startup.py
```

Expected: config/TOML/env/location/identity tests pass, unsafe domains are rejected, code-cache paths are Git-excluded, `uv.lock` contains both Tree-sitter packages, and normal startup leaves `tree_sitter_language_pack` unimported.

- [ ] **Step 5: Bump version and commit Unit A contracts**

Set `pyproject.toml` version to `0.7.64`.

```bash
git add pyproject.toml uv.lock src/iwiki_mcp/codegraph tests/codegraph tests/test_server_startup.py
git commit -m "feat(codegraph): add configuration and identities"
```

### Task 2: SQLite schema, lifecycle states, and recovery primitives

**Closes:** R-005, R-006, R-007, R-008, and R-009; produces AC-05, AC-06, AC-07, and the state/recovery-primitive portions of AC-08/AC-09 within Unit A.

**HUMAN CHECKPOINT — CLOSED:** Final SQL schema and lifecycle states were approved in design Sections 7 and 12.

**Files:**
- Create: `src/iwiki_mcp/codegraph/schema.py`
- Create: `src/iwiki_mcp/codegraph/store.py`
- Create: `tests/codegraph/test_store.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write failing schema, cascade, and integrity tests**

```python
from contextlib import closing

from iwiki_mcp.codegraph.store import CodeGraphStore
from iwiki_mcp.engine.graph_store import GraphStore


EXPECTED_INDEXES = {
    "idx_files_repository_path",
    "idx_files_content_hash",
    "idx_symbols_file",
    "idx_symbols_qualified",
    "idx_symbols_local",
    "idx_symbols_kind",
    "idx_relations_source_type",
    "idx_relations_target_type",
    "idx_relations_reference",
    "idx_wiki_links_page",
    "idx_wiki_links_symbol",
    "idx_wiki_links_file",
}


def test_schema_v1_has_required_tables_indexes_and_cascades(tmp_path):
    store = CodeGraphStore(tmp_path / "code-backend.sqlite3")
    with closing(store.connect()) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert names == {
            "repositories", "files", "symbols", "relations", "wiki_code_links"
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert indexes == EXPECTED_INDEXES
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)

    snapshot = snapshot_with_symbol_file_and_wiki_links()
    store.insert_snapshot(snapshot)
    with closing(store.connect()) as connection:
        assert connection.execute("SELECT count(*) FROM wiki_code_links").fetchone() == (2,)
        connection.execute(
            "DELETE FROM files WHERE file_id = ?", (snapshot.files[0].file_id,)
        )
        assert connection.execute("SELECT count(*) FROM wiki_code_links").fetchone() == (0,)
        connection.execute("DELETE FROM repositories WHERE repository_id = 'backend'")
        assert connection.execute("SELECT count(*) FROM files").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM symbols").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM relations").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM wiki_code_links").fetchone() == (0,)

    wiki_store = GraphStore(tmp_path / "wiki")
    with closing(wiki_store.connect()) as connection:
        wiki_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert wiki_tables == {"domains", "pages", "anchors", "edges"}


def test_store_reports_missing_and_quarantines_corrupt_cache(tmp_path):
    path = tmp_path / "code-backend.sqlite3"
    store = CodeGraphStore(path)
    assert store.inspect_state() == "missing"
    path.write_bytes(b"not sqlite")
    quarantined = store.quarantine_corrupt()
    assert quarantined.name.startswith("code-backend.sqlite3.corrupt-")
    assert not path.exists()
```

Define `snapshot_with_symbol_file_and_wiki_links` locally in `test_store.py`; it returns one repository, one file, one symbol, one relation, one symbol link, and one file link with exact schema-valid fields. It is test data, not a production helper.

- [ ] **Step 2: Run the store test and confirm missing module failure**

```bash
uv run pytest -q tests/codegraph/test_store.py
```

Expected: collection fails because `iwiki_mcp.codegraph.store` does not exist.

- [ ] **Step 3: Implement schema parity and configured connections**

```python
SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5_000
TABLES = ("repositories", "files", "symbols", "relations", "wiki_code_links")


def configure(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA journal_mode = WAL")


def validate_integrity(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise CodeGraphStoreError("code graph foreign key check failed")
    if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        raise CodeGraphStoreError("code graph integrity check failed")
```

Put the exact five-table and twelve-index SQL from spec Section 7 in `schema.py`. In `CodeGraphStore.connect`, configure the connection, create only an empty v1 schema, compare normalized `sqlite_master` table/index DDL and `PRAGMA table_info` against constants, and raise sanitized `CodeGraphSchemaError` on mismatch. Every caller owns the returned raw connection and must wrap it in `contextlib.closing`; store transaction helpers close their connection internally before quarantine or publication. Add helpers for inserting normalized snapshots, reading rows in stable ID order, deleting cascaded repositories, inspecting `missing/ready/dirty/rebuilding/failed`, reconstructing metadata from SQL revision, and quarantining corrupt bytes without touching Wiki storage.

- [ ] **Step 4: Run store tests including corruption and DDL mismatch**

```bash
uv run pytest -q tests/codegraph/test_store.py
```

Expected: exact twelve-index parity, unchanged four-table Wiki graph, schema, cascade, WAL, busy-timeout, integrity, state, quarantine, and incompatible-DDL tests pass.

- [ ] **Step 5: Bump version and commit store foundation**

Set version to `0.7.65`.

```bash
git add pyproject.toml src/iwiki_mcp/codegraph/schema.py src/iwiki_mcp/codegraph/store.py tests/codegraph/test_store.py
git commit -m "feat(codegraph): add SQLite store schema"
```

## Unit B — discovery, Python indexing, resolution, and search

### Task 3: Safe discovery and deterministic fingerprints

**Closes:** R-011 and R-012; produces AC-11 and AC-12. Full-build/no-op ownership remains exclusively in Task 6 under R-010/AC-10.

**Files:**
- Create: `src/iwiki_mcp/codegraph/discovery.py`
- Create: `src/iwiki_mcp/codegraph/fingerprint.py`
- Create: `tests/codegraph/test_discovery_fingerprint.py`
- Create: `tests/fixtures/codegraph/security_paths/safe.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write failing containment, symlink, secret, and relocation tests**

```python
def test_discovery_rejects_symlinks_secrets_and_outside_paths(tmp_path):
    project = tmp_path / "project"
    outside = tmp_path / "outside.py"
    project.mkdir()
    outside.write_text("SECRET = 1\n")
    (project / "safe.py").write_text("def safe(): return 1\n")
    (project / ".env").write_text("KEY=secret\n")
    (project / "linked.py").symlink_to(outside)

    snapshot = discover_sources(
        project,
        CodeGraphConfig.from_mapping({}),
        extensions=(".py",),
    )

    assert [item.path for item in snapshot.files] == ["safe.py"]
    assert {warning.code for warning in snapshot.warnings} >= {
        "secret_excluded", "symlink_excluded"
    }
```

- [ ] **Step 2: Run focused tests and confirm missing discovery API**

```bash
uv run pytest -q tests/codegraph/test_discovery_fingerprint.py
```

Expected: import failure for `discover_sources`.

- [ ] **Step 3: Implement deterministic discovery and fingerprint records**

```python
@dataclass(frozen=True)
class SourceFile:
    path: str
    content: bytes
    content_hash: str
    size_bytes: int


@dataclass(frozen=True)
class DiscoverySnapshot:
    files: tuple[SourceFile, ...]
    warnings: tuple[DiscoveryWarning, ...]
    truncated: bool


def source_fingerprint(files: Iterable[SourceFile]) -> str:
    rows = [(item.path, item.content_hash) for item in sorted(files, key=lambda x: x.path)]
    return sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()
```

Implement the language-neutral `discover_sources(project, config, *, extensions)` entry point. Use `os.scandir` without symlink following, project-relative POSIX paths, the supplied extension set, `pathspec` for Git/iwiki/config rules, hard secret exclusions that negation cannot override, and pre-read count/size limits. Add Git commit and dirty marker helpers using sanitized subprocess output. Compose source/config/parser fingerprints without absolute paths, timestamps, PIDs, or random values. Do not import the Python adapter or encode Python grammar rules in `discovery.py`.

- [ ] **Step 4: Run focused and existing ignore tests**

```bash
uv run pytest -q tests/codegraph/test_discovery_fingerprint.py tests/test_iwikiignore.py tests/test_base.py
```

Expected: all tests pass; relocated projects with identical domain/content have identical fingerprints.

- [ ] **Step 5: Bump version and commit discovery**

Set version to `0.7.66`.

```bash
git add pyproject.toml src/iwiki_mcp/codegraph/discovery.py src/iwiki_mcp/codegraph/fingerprint.py tests/codegraph/test_discovery_fingerprint.py tests/fixtures/codegraph/security_paths
git commit -m "feat(codegraph): add safe source discovery"
```

### Task 4: Language protocol and Python declaration extraction

**Closes:** R-013 and R-015; produces AC-13 and AC-15.

**Files:**
- Create: `src/iwiki_mcp/codegraph/languages/__init__.py`
- Create: `src/iwiki_mcp/codegraph/languages/base.py`
- Create: `src/iwiki_mcp/codegraph/languages/python.py`
- Create: `tests/codegraph/test_python_adapter.py`
- Create: `tests/fixtures/codegraph/python_basic/sample.py`
- Create: `tests/fixtures/codegraph/python_syntax_errors/broken.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write failing declaration/range/signature tests**

```python
def test_python_adapter_extracts_classes_functions_and_methods():
    source = b"class Service:\n    def run(self, value: int = 1) -> str:\n        return str(value)\n"
    parsed = PythonAdapter().parse_file(source, "src/service.py")

    assert [(item.kind, item.qualified_name) for item in parsed.symbols] == [
        ("class", "service.Service"),
        ("method", "service.Service.run"),
    ]
    method = parsed.symbols[1]
    assert method.start_line == 2
    assert method.end_line == 3
    assert method.signature == "(self,value:int=1)->str"
```

- [ ] **Step 2: Run the adapter test and confirm missing adapter failure**

```bash
uv run pytest -q tests/codegraph/test_python_adapter.py
```

Expected: import failure for `PythonAdapter`.

- [ ] **Step 3: Implement protocol and Tree-sitter extraction**

```python
class LanguageAdapter(Protocol):
    language: str
    extensions: tuple[str, ...]

    def parse_file(self, source: bytes, path: str) -> ParsedFile:
        raise NotImplementedError

    def resolve_references(
        self, parsed: ParsedFile, project_index: SymbolIndex
    ) -> ResolutionResult:
        raise NotImplementedError
```

Load `get_parser("python")` lazily inside `PythonAdapter`. Traverse named nodes for modules, classes, functions, async functions, and methods; compute qualified names from the relative module plus lexical parents; normalize parameters, annotations, defaults, async marker, and return annotation; record byte/line ranges. Record parse warnings for error nodes and retain only declarations whose ranges do not intersect an error node. Never call `compile`, `exec`, `eval`, `importlib`, or project imports.

- [ ] **Step 4: Run adapter and startup tests**

```bash
uv run pytest -q tests/codegraph/test_python_adapter.py tests/test_server_startup.py
```

Expected: extraction fixtures pass and startup tests prove no parser initialization before a code request.

- [ ] **Step 5: Bump version and commit extraction**

Set version to `0.7.67`.

```bash
git add pyproject.toml src/iwiki_mcp/codegraph/languages tests/codegraph/test_python_adapter.py tests/fixtures/codegraph/python_basic tests/fixtures/codegraph/python_syntax_errors
git commit -m "feat(codegraph): extract Python declarations"
```

### Task 5: Imports, calls, inheritance, and conservative resolution

**Closes:** R-014; reinforces Task 4-owned R-013/R-015 and produces AC-14 plus quality input for AC-27.

**HUMAN CHECKPOINT — CLOSED:** Conservative resolver semantics were approved in design Sections 10.3 and 20.

**Files:**
- Create: `src/iwiki_mcp/codegraph/resolver.py`
- Modify: `src/iwiki_mcp/codegraph/languages/python.py`
- Create: `tests/codegraph/test_resolver.py`
- Create: `tests/fixtures/codegraph/python_imports/pkg/a.py`
- Create: `tests/fixtures/codegraph/python_imports/pkg/b.py`
- Create: `tests/fixtures/codegraph/python_inheritance/models.py`
- Create: `tests/fixtures/codegraph/python_dynamic/dynamic.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write failing resolution-state tests**

```python
def test_resolver_preserves_exact_ambiguous_and_unresolved_targets():
    result = resolve_fixture("tests/fixtures/codegraph/python_dynamic")
    states = {(rel.target_reference, rel.resolution_state) for rel in result.relations}

    assert ("known", "resolved") in states
    assert ("factory.make", "ambiguous") in states
    assert ("external.call", "unresolved") in states
    assert all(rel.target_reference for rel in result.relations)

    relocated = resolve_fixture(
        "tests/fixtures/codegraph/python_dynamic", relocated=True
    )
    assert [rel.relation_id for rel in relocated.relations] == [
        rel.relation_id for rel in result.relations
    ]
```

- [ ] **Step 2: Run resolver tests and confirm missing API**

```bash
uv run pytest -q tests/codegraph/test_resolver.py
```

Expected: import failure for `iwiki_mcp.codegraph.resolver`.

- [ ] **Step 3: Implement reference records and deterministic resolution**

```python
@dataclass(frozen=True)
class SymbolIndex:
    by_qualified: Mapping[str, tuple[SymbolRecord, ...]]
    by_module_local: Mapping[tuple[str, str], tuple[SymbolRecord, ...]]


def resolution_state(candidates: Sequence[SymbolRecord], module_known: bool) -> str:
    if len(candidates) == 1:
        return "resolved"
    if len(candidates) > 1:
        return "ambiguous"
    return "partially_resolved" if module_known else "unresolved"
```

Extend the adapter to emit normalized `IMPORTS`, `CALLS`, and `INHERITS` references with source symbol/file and line. Resolve local absolute/relative imports, aliases, imported names, unique project classes, local bindings, imported callables, and unambiguous `self`/class methods. Emit one candidate relation per ambiguous target. Preserve dynamic/reflection/DI/wildcard/external references without guessing. Sort every output by source identity, relation type, source location, and target/reference, then call Task 1's exact `relation_id(language, source_identity, relation_type, source_location, target_identity_or_reference)` formula. Relation IDs must remain identical across repeated and relocated builds.

- [ ] **Step 4: Run adapter/resolver fixtures**

```bash
uv run pytest -q tests/codegraph/test_python_adapter.py tests/codegraph/test_resolver.py
```

Expected: exact, partial, ambiguous, unresolved, alias, relative-import, call, and inheritance tests pass.

- [ ] **Step 5: Bump version and commit resolver**

Set version to `0.7.68`.

```bash
git add pyproject.toml src/iwiki_mcp/codegraph/languages/python.py src/iwiki_mcp/codegraph/resolver.py tests/codegraph/test_resolver.py tests/fixtures/codegraph/python_imports tests/fixtures/codegraph/python_inheritance tests/fixtures/codegraph/python_dynamic
git commit -m "feat(codegraph): resolve Python relations"
```

### Task 6: Full-build indexer and runtime state facade

**Closes:** R-010, R-017, and R-025; consumes Unit A's closed R-007/R-008/R-009 primitives and produces AC-10, AC-17, AC-25, plus full-build integration evidence for AC-07/AC-08/AC-09.

**HUMAN CHECKPOINT — CLOSED:** Stale/rebuild transitions were approved in design Sections 12 and 20.

**Files:**
- Create: `src/iwiki_mcp/codegraph/indexer.py`
- Create: `src/iwiki_mcp/codegraph/runtime.py`
- Create: `tests/codegraph/conftest.py`
- Create: `tests/codegraph/test_indexer_runtime.py`
- Modify: `src/iwiki_mcp/codegraph/store.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write failing full-build, no-op, and fault-publication tests**

```python
def test_indexer_builds_noops_and_preserves_previous_revision_on_failure(seed_runtime):
    first = seed_runtime.index(force=False)
    second = seed_runtime.index(force=False)

    assert first["state"] == "ready"
    assert second["no_op"] is True
    assert second["revision"] == first["revision"]

    seed_runtime.fail_before_publish = True
    failed = seed_runtime.index(force=True)
    assert failed["error"] == "code graph rebuild failed"
    assert seed_runtime.status()["revision"] == first["revision"]


def test_disabled_runtime_creates_no_database_and_build_never_embeds(
    seed_runtime, monkeypatch
):
    def fail_embed(*_args, **_kwargs):
        raise AssertionError("code graph called the Wiki embedding path")

    monkeypatch.setattr("iwiki_mcp.indexer.embed_texts", fail_embed)
    disabled = seed_runtime.with_config(enabled=False)
    assert disabled.status()["code"] == "not_configured"
    assert disabled.database_accesses == []
    assert not disabled.paths.database.exists()

    assert seed_runtime.index(force=True)["state"] == "ready"
    assert not seed_runtime.embedding_requests


def test_nonready_state_guard_and_bounded_auto_rebuild(seed_runtime):
    missing = seed_runtime.with_state("missing", auto_rebuild="off").query_guard()
    assert missing["fresh"] is False
    assert missing["results"] == []

    ready = seed_runtime.with_state("ready", auto_rebuild="off").query_guard()
    assert ready["fresh"] is True

    for state in ("dirty", "rebuilding", "failed"):
        runtime = seed_runtime.with_state(state, auto_rebuild="off")
        out = runtime.query_guard()
        assert out["fresh"] is False
        assert out["results"] == []
        assert out["hint"]

    bounded = seed_runtime.with_state(
        "dirty", auto_rebuild="bounded", max_rebuild_seconds=1
    )
    assert bounded.query_guard()["fresh"] is True
    assert bounded.build_attempts == 1


def test_build_logs_are_sanitized_and_cache_is_git_ignored(seed_runtime, caplog):
    secret = "fixture-secret-token"
    seed_runtime.fail_with_message(f"{secret} {seed_runtime.project_dir}")

    out = seed_runtime.index(force=True)

    assert out["code"] == "rebuild_failed"
    assert secret not in caplog.text
    assert str(seed_runtime.project_dir) not in caplog.text
    assert seed_runtime.git_status() == ""
```

`tests/codegraph/conftest.py` owns test harnesses, never production API. In Task 6 define `seed_runtime`, `ready_runtime`, `seed_binding`, `seed_without_primary`, and `fake_runtime_factory`; the factory returns the `FakeRuntime` double used by server tests. The runtime double records `embedding_requests`, `database_accesses`, and `build_attempts`, and offers `with_config`, `with_state`, `fail_with_message`, `project_file`, `git_status`, `wiki_hashes`, and `hold_publication_lock`. Later tasks extend this same file only when their dependencies exist: Task 9 adds `link_fixture`/`seed_wiki`, Task 11 adds `seed_lint`/`seed_lint_without_graph`, and Task 12 adds `runtime_pair` plus `switch_branch`/`mutate_sources`. Every fixture closes SQLite handles in teardown and exposes only deterministic project-relative fixture data.

- [ ] **Step 2: Run indexer/runtime tests and confirm missing facade**

```bash
uv run pytest -q tests/codegraph/test_indexer_runtime.py
```

Expected: import failure for `CodeGraphRuntime`.

- [ ] **Step 3: Implement the staged pipeline and sanitized runtime states**

```python
class CodeGraphRuntime:
    def status(self) -> dict:
        return self._status_without_build()

    def index(self, *, force: bool = False, languages: list[str] | None = None) -> dict:
        try:
            return self._indexer.build(force=force, languages=languages)
        except Timeout:
            return {"error": "code graph is busy", "code": "busy", "hint": "retry wiki_code_index"}
        except CodeGraphError:
            return {"error": "code graph rebuild failed", "code": "rebuild_failed", "hint": "inspect wiki_code_status and retry"}
```

Load configuration through `load_code_graph_config(binding.project_dir)`. Implement spec Section 12's 13 ordered build steps. Use a UUID only for the staging filename, never for portable IDs. Acquire the per-domain `FileLock`, validate staging, checkpoint/close it, atomically replace the canonical DB, atomically replace metadata JSON, reopen and verify revision, then release. Status reads metadata/schema only. Matching fingerprint returns no-op. Missing/dirty bounded lazy build uses the configured request budget; non-ready guards return `fresh=false`, a stable hint, and no stale graph rows. Emit only stable error codes/counts/timings to logs—never source, absolute project paths, environment values, or exception payloads.

- [ ] **Step 4: Run runtime, store, graph, and concurrency-adjacent tests**

```bash
uv run pytest -q tests/codegraph/test_indexer_runtime.py tests/codegraph/test_store.py tests/test_graph_runtime.py tests/test_lock.py
```

Expected: all focused tests pass; disabled mode creates/reads no code DB, bounded auto-rebuild and every non-ready state are covered, the build path makes zero embedding calls, logs are sanitized, code-cache artifacts remain absent from `git status`, and fault injection never exposes staging or changes the previous ready revision.

- [ ] **Step 5: Bump version and commit indexing lifecycle**

Set version to `0.7.69`.

```bash
git add pyproject.toml src/iwiki_mcp/codegraph/indexer.py src/iwiki_mcp/codegraph/runtime.py src/iwiki_mcp/codegraph/store.py tests/codegraph/conftest.py tests/codegraph/test_indexer_runtime.py
git commit -m "feat(codegraph): build atomic graph snapshots"
```

### Task 7: Deterministic symbol search and Unit B MCP exposure

**Closes:** R-018 and owns the Unit B `status`/`index`/`search` portion of R-016; produces AC-18 and partial AC-16 evidence. Task 10 completes R-016/AC-16 in Unit C.

**HUMAN CHECKPOINT — CLOSED:** Status/index/search JSON contracts were approved in design Sections 13.2–13.4 and 20.

**Files:**
- Create: `src/iwiki_mcp/codegraph/query.py`
- Create: `tests/codegraph/test_query.py`
- Create: `tests/codegraph/test_server_tools.py`
- Modify: `tests/test_mcp_smoke.py`
- Modify: `src/iwiki_mcp/codegraph/runtime.py`
- Modify: `src/iwiki_mcp/server.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write failing tier-order and filter tests**

```python
def test_search_orders_exact_local_before_prefix_and_lexical(ready_runtime):
    out = ready_runtime.search("run", kinds=["method"], path="src/", limit=4)

    assert [item["match"] for item in out["results"]] == [
        "exact_local", "prefix", "lexical", "path"
    ]
    assert all(not item["path"].startswith("/") for item in out["results"])
    assert set(out["results"][0]) >= {
        "symbol_id", "kind", "qualified_name", "local_name", "signature",
        "path", "start_line", "end_line", "start_byte", "end_byte", "match",
    }
    assert out["results"] == ready_runtime.search(
        "run", kinds=["method"], path="src/", limit=4
    )["results"]


def test_search_never_returns_stale_rows(ready_runtime):
    dirty = ready_runtime.with_state("dirty", auto_rebuild="off")

    out = dirty.search("run")

    assert out["fresh"] is False
    assert out["results"] == []
    assert out["hint"]


def test_unit_b_registers_status_index_and_search(
    seed_binding, monkeypatch, fake_runtime_factory
):
    calls = []
    monkeypatch.setattr(
        server,
        "_code_runtime",
        lambda binding: fake_runtime_factory(binding, calls),
    )

    assert server.wiki_code_status()["domain"] == "backend"
    assert server.wiki_code_index(force=True)["domain"] == "backend"
    assert server.wiki_code_search("run")["domain"] == "backend"
    assert calls == ["status:backend", "index:backend", "search:backend"]
```

- [ ] **Step 2: Run query tests and confirm missing search implementation**

```bash
uv run pytest -q tests/codegraph/test_query.py tests/codegraph/test_server_tools.py
```

Expected: `CodeGraphRuntime` has no `search` method and `server` has no `wiki_code_status`.

- [ ] **Step 3: Implement bounded SQL candidate retrieval and Python ranking**

```python
MATCH_RANK = {
    "exact_qualified": 0,
    "exact_local": 1,
    "prefix": 2,
    "lexical": 3,
    "signature": 4,
    "path": 5,
}


def result_key(item: SearchResult) -> tuple[int, str, str]:
    return MATCH_RANK[item.match], item.qualified_name, item.symbol_id
```

Validate nonblank query, known kinds, `python` language, project-relative safe path prefix, and `1 <= limit <= 100`. Retrieve exact/prefix/LIKE candidates through indexed columns, classify each match once at its strongest tier, sort by `result_key`, and return only relative path/range/signature fields plus common state metadata.

Add `_code_runtime(binding)` plus thin `wiki_code_status`, `wiki_code_index`, and `wiki_code_search` handlers with exact design Section 13.2–13.4 signatures. Map `CodeGraphError` before `_safe`'s generic branch, validate `primary`, register these three with FastMCP, and leave `wiki_search` unchanged. Update the existing MCP smoke registry expectation with these three tools; `wiki_code_context` remains absent until Unit C.

- [ ] **Step 4: Run query/store tests**

```bash
uv run pytest -q tests/codegraph/test_query.py tests/codegraph/test_store.py tests/codegraph/test_server_tools.py tests/test_mcp_smoke.py tests/test_server_search.py
```

Expected: ranking, filters, limits, ranges, stable ties, invalid-input tests, three Unit B MCP schemas, and unchanged `wiki_search` tests pass.

- [ ] **Step 5: Bump version and commit search**

Set version to `0.7.70`.

```bash
git add pyproject.toml src/iwiki_mcp/codegraph/query.py src/iwiki_mcp/codegraph/runtime.py src/iwiki_mcp/server.py tests/codegraph/test_query.py tests/codegraph/test_server_tools.py tests/test_mcp_smoke.py
git commit -m "feat(codegraph): add deterministic symbol search"
```

## Unit C — context, Wiki links, lint, and evidence

### Task 8: Bounded graph context and safe source reads

**Closes:** R-019 and R-020; produces AC-19 and AC-20.

**Files:**
- Create: `src/iwiki_mcp/codegraph/context.py`
- Create: `tests/codegraph/test_context.py`
- Modify: `src/iwiki_mcp/codegraph/runtime.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write failing traversal-budget and stale-source tests**

```python
def test_context_is_deterministic_bounded_and_omits_changed_source(ready_runtime):
    seed = ready_runtime.symbols["pkg.Service.run"]
    ready_runtime.project_file("src/pkg.py").write_text("changed\n")

    out = ready_runtime.context(
        [seed], depth=1, max_nodes=2, max_files=1, max_source_bytes=32
    )

    assert len(out["nodes"]) == 2
    assert len(out["files"]) == 1
    assert out["truncated"] is True
    assert out["fresh"] is False
    assert all("source" not in file for file in out["files"])


def test_context_never_returns_stale_nodes(ready_runtime):
    dirty = ready_runtime.with_state("dirty", auto_rebuild="off")

    out = dirty.context(["pkg.Service.run"])

    assert out["fresh"] is False
    assert out["nodes"] == []
    assert out["files"] == []
    assert out["hint"]
```

- [ ] **Step 2: Run context tests and confirm missing context method**

```bash
uv run pytest -q tests/codegraph/test_context.py
```

Expected: runtime has no `context` method.

- [ ] **Step 3: Implement deterministic BFS and guarded source loading**

```python
def neighbor_key(relation: RelationRecord) -> tuple[str, str, str]:
    return relation.relation_type, relation.source_symbol_id or "", relation.target_symbol_id or ""


def within(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return not candidate.is_symlink()
```

Validate direction/depth/relations/budgets; resolve seed IDs or exact qualified names; traverse breadth-first with `neighbor_key`; accept each node/file once; stop expansion on every exhausted budget; return effective limits and `truncated`. For explicit source inclusion, recheck containment, symlink, built-in exclusions, current content hash, per-file size, and aggregate bytes before decoding with replacement. Omit mismatches, mark `fresh=false`, and append a stable warning code.

- [ ] **Step 4: Run context and security tests**

```bash
uv run pytest -q tests/codegraph/test_context.py tests/codegraph/test_discovery_fingerprint.py
```

Expected: direction/depth/relation filters, deterministic BFS, all budgets, traversal rejection, hash mismatch, and truncation tests pass.

- [ ] **Step 5: Bump version and commit bounded context**

Set version to `0.7.71`.

```bash
git add pyproject.toml src/iwiki_mcp/codegraph/context.py src/iwiki_mcp/codegraph/runtime.py tests/codegraph/test_context.py
git commit -m "feat(codegraph): add bounded source context"
```

### Task 9: Wiki selector parsing and derived links

**Closes:** R-021, R-022, and R-024; produces AC-21, AC-22, and AC-24.

**Files:**
- Create: `src/iwiki_mcp/codegraph/linking.py`
- Create: `tests/codegraph/test_linking.py`
- Create: `tests/codegraph/test_frontmatter_roundtrip.py`
- Modify: `tests/codegraph/conftest.py`
- Modify: `src/iwiki_mcp/codegraph/indexer.py`
- Modify: `src/iwiki_mcp/codegraph/context.py`
- Modify: `src/iwiki_mcp/engine/frontmatter.py`
- Modify: `src/iwiki_mcp/okf.py`
- Modify: `src/iwiki_mcp/server.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write failing symbol/file/glob specificity tests**

```python
def test_linker_materializes_typed_links_with_specificity(link_fixture):
    links, findings = resolve_wiki_links(link_fixture.pages, link_fixture.snapshot)

    assert findings == ()
    assert {(link.selector_kind, bool(link.symbol_id), bool(link.file_id)) for link in links} == {
        ("symbol", True, False),
        ("file", False, True),
        ("source_glob", False, True),
    }
    exact = [link for link in links if link.qualified_name == "pkg.Service.run"]
    assert [link.selector_kind for link in exact] == ["symbol"]
    assert all(link.relation_type == "DOCUMENTED_BY" for link in links)


def test_authored_code_selectors_survive_write_and_update(seed_wiki):
    authored = """---
code:
  symbols:
    - qualified_name: pkg.Service.run
  files:
    - src/pkg/service.py
  source_globs:
    - src/pkg/**
---
# Service
## Flow
Original
"""
    assert "error" not in server.wiki_write_page(
        "backend", "service", authored, type="reference"
    )
    before, _ = frontmatter.split(
        server.wiki_read_page("backend", "reference/service")["markdown"]
    )

    assert "error" not in server.wiki_update_page(
        "backend", "reference/service", "Flow", "Updated"
    )
    after, _ = frontmatter.split(
        server.wiki_read_page("backend", "reference/service")["markdown"]
    )
    assert after["code"] == before["code"] == {
        "symbols": [{"qualified_name": "pkg.Service.run"}],
        "files": ["src/pkg/service.py"],
        "source_globs": ["src/pkg/**"],
    }

    server.wiki_code_index(force=True)
    server.wiki_lint("backend")
    final, _ = frontmatter.split(
        server.wiki_read_page("backend", "reference/service")["markdown"]
    )
    assert final["code"] == before["code"]
```

- [ ] **Step 2: Run linking tests and confirm missing resolver**

```bash
uv run pytest -q tests/codegraph/test_linking.py
```

Expected: import failure for `resolve_wiki_links`.

- [ ] **Step 3: Implement typed selector parsing and derived link resolution**

```python
SELECTOR_PRIORITY = {"symbol": 0, "file": 1, "source_glob": 2}


@dataclass(frozen=True)
class CodeSelector:
    kind: Literal["symbol", "file", "source_glob"]
    value: str
```

Extend the stdlib-only frontmatter parser/renderer only for the exact nested `code` contract: `symbols` is a list of single-key `{qualified_name: str}` mappings, while `files` and `source_globs` are string lists. Do not add a general YAML dependency. Add `preserved_meta: dict | None = None` to `okf.build_frontmatter`; after validating the nested shape, copy only `preserved_meta["code"]` into the rebuilt metadata. In `wiki_write_page`, split authored frontmatter from the submitted Markdown and pass it as `preserved_meta`; in update/type-move paths pass `existing_meta`. This preserves selector values semantically across write/update while still allowing governed fields and YAML formatting to be rebuilt.

Resolve qualified names against symbol rows, files against exact project-relative paths, and globs against safe non-secret file rows. Materialize symbol or file links, deduplicate by target using `SELECTOR_PRIORITY`, set `DOCUMENTED_BY`, and persist stable provenance. Do not generate suggested links or mutate the preserved `code` mapping. Extend context Wiki enrichment to use exact symbol and containing-file links.

- [ ] **Step 4: Run linking, frontmatter, and context tests**

```bash
uv run pytest -q tests/codegraph/test_linking.py tests/codegraph/test_frontmatter_roundtrip.py tests/codegraph/test_context.py tests/test_frontmatter.py tests/test_lint_frontmatter.py tests/test_server_write_frontmatter.py tests/test_server_update.py tests/test_chunk_frontmatter.py tests/test_okf_build_frontmatter.py tests/test_frontmatter_governance.py tests/test_validate_frontmatter.py tests/test_export_okf.py tests/test_resources_frontmatter.py tests/engine/test_lint.py
```

Expected: typed links, specificity, cascades, safe glob handling, nested `code` write/update round-trip, no automatic selector mutation, frontmatter compatibility, and Wiki enrichment tests pass.

- [ ] **Step 5: Bump version and commit Wiki-code links**

Set version to `0.7.72`.

```bash
git add pyproject.toml src/iwiki_mcp/codegraph/linking.py src/iwiki_mcp/codegraph/indexer.py src/iwiki_mcp/codegraph/context.py src/iwiki_mcp/engine/frontmatter.py src/iwiki_mcp/okf.py src/iwiki_mcp/server.py tests/codegraph/conftest.py tests/codegraph/test_linking.py tests/codegraph/test_frontmatter_roundtrip.py
git commit -m "feat(codegraph): link Wiki pages to code"
```

### Task 10: Register bounded context and complete the four-tool contract

**Closes:** the remaining R-016 contract; produces final four-tool AC-16 evidence and exposes Task 8-owned R-019/R-020 behavior. Unit B already owns status/index/search exposure.

**HUMAN CHECKPOINT — CLOSED:** Context JSON contract was approved in design Sections 13.5 and 20.

**Files:**
- Modify: `src/iwiki_mcp/server.py`
- Modify: `src/iwiki_mcp/codegraph/runtime.py`
- Modify: `tests/codegraph/test_server_tools.py`
- Modify: `tests/test_mcp_smoke.py`
- Modify: `tests/test_package.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write failing primary-routing and fail-soft handler tests**

```python
def test_context_uses_primary_and_completes_four_tool_surface(
    seed_binding, monkeypatch, fake_runtime_factory
):
    calls = []
    monkeypatch.setattr(
        server,
        "_code_runtime",
        lambda binding: fake_runtime_factory(binding, calls),
    )
    search_signature = inspect.signature(server.wiki_search)

    assert server.wiki_code_context(["pkg.Service.run"])["domain"] == "backend"
    assert calls == ["context:backend"]
    assert {
        server.wiki_code_status.__name__,
        server.wiki_code_index.__name__,
        server.wiki_code_search.__name__,
        server.wiki_code_context.__name__,
    } == {
        "wiki_code_status",
        "wiki_code_index",
        "wiki_code_search",
        "wiki_code_context",
    }
    assert inspect.signature(server.wiki_search) == search_signature


def test_missing_primary_is_fail_soft_and_wiki_status_still_works(seed_without_primary):
    assert server.wiki_code_status()["code"] == "not_configured"
    assert "domains" in server.wiki_status()


def test_context_does_not_mutate_authored_selectors(seed_wiki):
    before, _ = frontmatter.split(
        server.wiki_read_page("backend", "reference/service")["markdown"]
    )

    server.wiki_code_context(["pkg.Service.run"], include_wiki=True)

    after, _ = frontmatter.split(
        server.wiki_read_page("backend", "reference/service")["markdown"]
    )
    assert after["code"] == before["code"]


def assert_exact_code_tool_registry(listed):
    code_tools = {
        tool.name: tool
        for tool in listed
        if tool.name.startswith("wiki_code_")
    }
    assert set(code_tools) == {
        "wiki_code_status",
        "wiki_code_index",
        "wiki_code_search",
        "wiki_code_context",
    }
    assert all(
        "domain" not in tool.inputSchema.get("properties", {})
        for tool in code_tools.values()
    )
```

Call `assert_exact_code_tool_registry((await session.list_tools()).tools)` inside the existing real stdio session test in `tests/test_mcp_smoke.py`.

- [ ] **Step 2: Run server-tool tests and confirm missing functions**

```bash
uv run pytest -q tests/codegraph/test_server_tools.py tests/test_mcp_smoke.py
```

Expected: `server` has no `wiki_code_context`; the three Unit B tools already exist.

- [ ] **Step 3: Add thin composition-root handlers**

```python
@_safe
def wiki_code_context(
    symbols: list[str],
    direction: Literal["in", "out", "both"] = "both",
    depth: int = 1,
    relations: list[str] | None = None,
    include_source: bool = True,
    include_wiki: bool = True,
    max_nodes: int = 50,
    max_files: int = 20,
    max_source_bytes: int = 200_000,
) -> dict:
    return _code_runtime(base.resolve_binding()).context(
        symbols,
        direction=direction,
        depth=depth,
        relations=relations,
        include_source=include_source,
        include_wiki=include_wiki,
        max_nodes=max_nodes,
        max_files=max_files,
        max_source_bytes=max_source_bytes,
    )
```

Register `wiki_code_context` with FastMCP using the existing pattern. Reuse Unit B's `_code_runtime`, primary validation, config loading, and sanitized `CodeGraphError` mapping. Extend the real stdio MCP smoke harness—not only Python function-name assertions—to inspect `session.list_tools()`, prove exactly four registered `wiki_code_*` tools exist, prove none accepts `domain`, and leave `wiki_search` unchanged.

- [ ] **Step 4: Run MCP schema, smoke, startup, and Wiki search tests**

```bash
uv run pytest -q tests/codegraph/test_server_tools.py tests/test_mcp_smoke.py tests/test_server_startup.py tests/test_server_search.py tests/test_package.py
```

Expected: four schemas match the spec, startup remains lazy, old Wiki tests pass, and injected code failures permit succeeding Wiki calls.

- [ ] **Step 5: Bump version and commit MCP surface**

Set version to `0.7.73`.

```bash
git add pyproject.toml src/iwiki_mcp/server.py src/iwiki_mcp/codegraph/runtime.py tests/codegraph/test_server_tools.py tests/test_mcp_smoke.py tests/test_package.py
git commit -m "feat(codegraph): expose code graph MCP tools"
```

### Task 11: Add code-aware Wiki lint diagnostics

**Closes:** R-023 and R-026; produces AC-23 and AC-26.

**Files:**
- Modify: `src/iwiki_mcp/server.py`
- Modify: `src/iwiki_mcp/codegraph/linking.py`
- Modify: `tests/codegraph/conftest.py`
- Create: `tests/codegraph/test_lint.py`
- Modify: `tests/test_server_lint_sync.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write failing lint matrix and unavailable-graph tests**

```python
def test_wiki_lint_reports_code_selector_findings_without_blocking_markdown(seed_lint):
    report = server.wiki_lint("backend")["reports"]["backend"]

    assert {item["type"] for item in report["code_graph"]["findings"]} == {
        "unknown_symbol",
        "ambiguous_symbol",
        "missing_file",
        "empty_glob",
        "unsafe_selector",
        "ignored_selector",
        "conflicting_selectors",
        "stale_revision",
    }
    assert "broken" in report


def test_wiki_lint_survives_missing_code_graph(seed_lint_without_graph):
    report = server.wiki_lint("backend")["reports"]["backend"]
    assert report["code_graph"]["available"] is False
    assert "broken" in report
```

- [ ] **Step 2: Run lint tests and confirm missing report block**

```bash
uv run pytest -q tests/codegraph/test_lint.py tests/test_server_lint_sync.py
```

Expected: assertions fail because `code_graph` is absent.

- [ ] **Step 3: Compose code diagnostics after ordinary lint**

```python
report = lint(
    visible_domains[target],
    project_dir=bind.project_dir,
    domain=target,
    base_dir=bind.base,
    visible_domains=visible_domains,
)
report["code_graph"] = _code_runtime(bind).lint_domain(target)
reports[target] = report
```

`lint_domain` must be read-only, never build, never mutate selectors, and return `{available, state, revision, findings, hint}`. Use the same selector validation/resolution functions as indexing. Convert missing/dirty/failed stores into diagnostics, not exceptions. Keep existing Markdown fields byte-compatible.

- [ ] **Step 4: Run full lint/frontmatter tests**

```bash
uv run pytest -q tests/codegraph/test_lint.py tests/test_server_lint_sync.py tests/test_lint_frontmatter.py tests/engine/test_lint.py
```

Expected: full finding matrix and unavailable behavior pass with all existing lint tests.

- [ ] **Step 5: Bump version and commit lint integration**

Set version to `0.7.74`.

```bash
git add pyproject.toml src/iwiki_mcp/server.py src/iwiki_mcp/codegraph/linking.py tests/codegraph/conftest.py tests/codegraph/test_lint.py tests/test_server_lint_sync.py
git commit -m "feat(codegraph): lint Wiki code selectors"
```

### Task 12: Recovery and multi-process concurrency evidence

**Reinforces:** Task 2-owned R-006/R-007 and Task 6-owned R-017; supplies AC-06, AC-07, AC-17, and final multi-process evidence for Task 2 primitives and Task 6 AC-08/AC-09 integration without moving requirement ownership into Unit C.

**Files:**
- Modify: `src/iwiki_mcp/codegraph/store.py`
- Modify: `src/iwiki_mcp/codegraph/indexer.py`
- Modify: `src/iwiki_mcp/codegraph/runtime.py`
- Modify: `tests/codegraph/conftest.py`
- Create: `tests/codegraph/test_recovery_concurrency.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write failing competing-writer, reader, corruption, and metadata-skew tests**

```python
def test_competing_writer_is_busy_while_reader_sees_complete_snapshot(runtime_pair):
    first, second = runtime_pair
    original = first.index(force=True)["revision"]

    with first.hold_publication_lock():
        assert second.index(force=True)["code"] == "busy"
        observed = second.status()

    assert observed["revision"] == original
    assert observed["state"] == "ready"


def test_corrupt_database_rebuild_does_not_change_wiki_files(seed_runtime):
    before = seed_runtime.wiki_hashes()
    seed_runtime.paths.database.write_bytes(b"not sqlite")
    assert seed_runtime.index(force=True)["state"] == "ready"
    assert seed_runtime.wiki_hashes() == before


def test_branch_switch_forces_full_rebuild(seed_runtime):
    first = seed_runtime.index(force=True)
    seed_runtime.switch_branch("alternate")

    rebuilt = seed_runtime.index(force=False)

    assert rebuilt["full_rebuild"] is True
    assert rebuilt["revision"] != first["revision"]


@pytest.mark.parametrize("mutation", ["added", "changed", "deleted"])
def test_dirty_worktree_source_changes_force_full_rebuild(seed_runtime, mutation):
    first = seed_runtime.index(force=True)
    seed_runtime.mutate_sources(mutation, dirty=True)

    rebuilt = seed_runtime.index(force=False)

    assert rebuilt["full_rebuild"] is True
    assert rebuilt["no_op"] is False
    assert rebuilt["revision"] != first["revision"]
```

- [ ] **Step 2: Run recovery/concurrency tests and confirm failure**

```bash
uv run pytest -q tests/codegraph/test_recovery_concurrency.py
```

Expected: at least competing-writer, quarantine, or metadata-reconstruction assertions fail.

- [ ] **Step 3: Complete bounded replacement and recovery branches**

```python
def quarantine_path(database: Path, fingerprint: str) -> Path:
    return database.with_name(f"{database.name}.corrupt-{fingerprint[:16]}")


def atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    os.replace(temporary, path)
```

Use short-lived read-only connections, one per-domain `FileLock`, a shared lock deadline for build and replace-compatible handle retries, deterministic quarantine naming from corrupt bytes, SQL revision as metadata authority, and metadata reconstruction after mismatch. Ensure every error path closes connections, removes only its own staging files, preserves canonical Wiki artifacts, and returns stable `busy`, `store_failed`, or `rebuild_failed` diagnostics.

- [ ] **Step 4: Run recovery plus existing concurrency suites**

```bash
uv run pytest -q tests/codegraph/test_recovery_concurrency.py tests/test_sync_concurrency.py tests/test_sync_parallel.py tests/test_lock.py
```

Expected: concurrent readers see complete revisions, one writer publishes, competitors time out as `busy`, corruption rebuilds, branch switches and added/changed/deleted dirty-worktree inputs trigger full rebuilds, and Wiki hashes remain unchanged.

- [ ] **Step 5: Bump version and commit recovery/concurrency**

Set version to `0.7.75`.

```bash
git add pyproject.toml src/iwiki_mcp/codegraph/store.py src/iwiki_mcp/codegraph/indexer.py src/iwiki_mcp/codegraph/runtime.py tests/codegraph/conftest.py tests/codegraph/test_recovery_concurrency.py
git commit -m "fix(codegraph): harden recovery and concurrency"
```

### Task 13: Quality and performance benchmark

**Closes:** R-027 and R-028; produces AC-27 and AC-28.

**Files:**
- Create: `eval/code_graph/__init__.py`
- Create: `eval/code_graph/__main__.py`
- Create: `eval/code_graph/runner.py`
- Create: `eval/code_graph/report.py`
- Create: `tests/eval/test_code_graph_runner.py`
- Create: `tests/eval/test_code_graph_report.py`
- Create: `docs/superpowers/evidence/code-graph-benchmark-method.md`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write failing deterministic report-schema tests**

```python
def test_benchmark_report_contains_quality_performance_and_environment(tmp_path):
    report = run_benchmark(fixture_root="tests/fixtures/codegraph", output=tmp_path)

    assert set(report) >= {"environment", "corpus", "versions", "quality", "performance"}
    assert set(report["quality"]) >= {
        "declarations", "methods", "local_imports", "static_calls", "false_resolved_calls", "deterministic"
    }
    assert set(report["performance"]) >= {
        "startup_ms", "noop_ms", "build_1000_ms", "search_ms", "context_ms", "peak_memory_bytes", "database_ratio"
    }


def test_benchmark_threshold_miss_fails_the_gate(tmp_path):
    with pytest.raises(BenchmarkGateError):
        run_benchmark(
            fixture_root="tests/fixtures/codegraph",
            output=tmp_path,
            thresholds={"declarations": 1.01},
        )
```

- [ ] **Step 2: Run benchmark tests and confirm missing package**

```bash
uv run pytest -q tests/eval/test_code_graph_runner.py tests/eval/test_code_graph_report.py
```

Expected: import failure for `eval.code_graph`.

- [ ] **Step 3: Implement isolated runner, metrics, and JSON/Markdown report**

```python
@dataclass(frozen=True)
class BenchmarkResult:
    environment: dict[str, object]
    corpus: dict[str, object]
    versions: dict[str, str]
    quality: dict[str, float | bool]
    performance: dict[str, float | int]


def write_report(result: BenchmarkResult, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "code-graph-benchmark.json").write_text(
        json.dumps(asdict(result), indent=2, sort_keys=True) + "\n"
    )
```

Run against a copied temporary corpus so production configuration and databases cannot change. Record Python/platform/CPU, corpus hash/count/bytes, package/schema/adapter/resolver versions, exact command, all spec Section 16 metrics, and threshold pass/fail. Compare two forced builds row-for-row and revision-for-revision. A missed provisional threshold writes evidence, raises `BenchmarkGateError`, makes the CLI exit nonzero, stops downstream acceptance, and escalates to the human checkpoint required by the intent; implementation must not weaken a threshold or change production defaults autonomously.

- [ ] **Step 4: Run benchmark tests and a local smoke report**

```bash
uv run pytest -q tests/eval/test_code_graph_runner.py tests/eval/test_code_graph_report.py
uv run python -m eval.code_graph --fixture-root tests/fixtures/codegraph --output /tmp/iwiki-code-graph-evidence
```

Expected: tests pass; output contains JSON and Markdown with environment, corpus, versions, quality, performance, and deterministic comparison.

- [ ] **Step 5: Bump version and commit benchmark tooling**

Set version to `0.7.76`.

```bash
git add pyproject.toml eval/code_graph tests/eval/test_code_graph_runner.py tests/eval/test_code_graph_report.py docs/superpowers/evidence/code-graph-benchmark-method.md
git commit -m "test(codegraph): add benchmark evidence runner"
```

### Task 14: User documentation, Wiki update, and final regression gates

**Closes:** R-029 and R-030; reinforces Task 11-owned R-026 and produces AC-29/AC-30 plus final AC-26 documentation evidence.

**Files:**
- Modify: `README.md`
- Modify: `docs/README.ru.md`
- Modify: `docs/architecture.md`
- Modify: `tests/test_package.py`
- Modify: `pyproject.toml`
- Verify through iwiki MCP: `architecture`, `mcp-server`, `installation`, `reference/code-graph-technical-debt`

- [ ] **Step 1: Write failing documentation-contract tests**

```python
def test_readme_documents_code_tools_and_deferred_scope():
    text = Path("README.md").read_text(encoding="utf-8")
    for name in ("wiki_code_status", "wiki_code_index", "wiki_code_search", "wiki_code_context"):
        assert name in text
    assert "Incremental indexing" in text
    assert "TypeScript" in text
    assert "not part of the Python MVP" in text
```

- [ ] **Step 2: Run documentation contract and CLI smoke before edits**

```bash
uv run pytest -q tests/test_package.py -k code_tools
uv run iwiki-mcp --help
```

Expected: the newly added `test_readme_documents_code_tools_and_deferred_scope` is collected and fails its documentation assertion; CLI help still succeeds. Exit code 5 from an empty `-k code_tools` selection is not an acceptable RED result.

- [ ] **Step 3: Document configuration, tools, safety, lifecycle, and debt**

Add the exact `.iwiki.toml` block from spec Section 6.3, derived paths, four tool signatures/result semantics, no-startup-build rule, security exclusions, recovery command, benchmark command, and explicit Python-only/deferred Incremental/TypeScript statements to English and Russian user docs. Update architecture package/data-flow sections without claiming incremental indexing, TypeScript, impact analysis, or hybrid retrieval exists.

- [ ] **Step 4: Update iwiki through MCP and run final verification**

Use `wiki_update_page` for existing `architecture`, `mcp-server`, `installation`, and `reference/code-graph-technical-debt` sections, with changed source paths. Then run:

```bash
uv run pytest -q
uv run flake8 src tests eval/code_graph
uv run python -m compileall -q src tests eval
uv run iwiki-mcp --help
git diff --check
```

Expected: full suite passes; code-graph-scoped flake8 plus repository compileall/diff checks are clean; CLI help succeeds. The unrelated pre-existing `eval/search_pipeline/selection.py:937` E501 is outside this delivery and remains visible rather than being silently repaired. `wiki_lint(domain="iwiki-mcp")` has no code-graph broken/stale/missing-source findings and graph parity is ready.

- [ ] **Step 5: Bump version and commit documentation**

Set version to `0.7.77`.

```bash
git add pyproject.toml README.md docs/README.ru.md docs/architecture.md tests/test_package.py
git commit -m "docs(codegraph): document Python MVP"
```

## Final result evidence

After all tasks and task-level reviews:

```bash
uv run pytest -q
uv run flake8 src tests eval/code_graph
uv run python -m compileall -q src tests eval
uv run iwiki-mcp --help
uv run python -m eval.code_graph --fixture-root tests/fixtures/codegraph --output /tmp/iwiki-code-graph-evidence
git diff --check
```

Expected evidence:

- Every R-001…R-030 maps to at least one completed task and AC-01…AC-30 result.
- Existing Wiki tests and `wiki_search` contract remain green.
- Four code tools pass MCP schema and fail-soft integration tests.
- Security/concurrency/recovery suites pass.
- Benchmark report contains every required metric and threshold verdict.
- Wiki technical-debt page still excludes incremental indexing and TypeScript from Python MVP.
- Run `$check-chain result docs/superpowers/plans/2026-08-09-iwiki-mcp-code-graph.md` only after documentation and Wiki evidence are current.
