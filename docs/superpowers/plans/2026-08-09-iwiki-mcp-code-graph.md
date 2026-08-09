---
review:
  plan_hash: 34f6385c56ed6a06
  last_run: 2026-08-09
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings: []
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

## Unit A — contracts, storage, and lifecycle

### Task 1: Configuration, locations, models, and mandatory dependencies

**Closes:** R-001, R-002, R-003, R-004, R-012; produces AC-01, AC-02, AC-03, AC-04, and the ID part of AC-12.

**Files:**
- Create: `src/iwiki_mcp/codegraph/__init__.py`
- Create: `src/iwiki_mcp/codegraph/config.py`
- Create: `src/iwiki_mcp/codegraph/location.py`
- Create: `src/iwiki_mcp/codegraph/models.py`
- Create: `tests/codegraph/__init__.py`
- Create: `tests/codegraph/test_config_location_models.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

- [ ] **Step 1: Write failing config, location, and ID tests**

```python
from iwiki_mcp.codegraph.config import CodeGraphConfig, CodeGraphConfigError
from iwiki_mcp.codegraph.location import CodeGraphLocationResolver
from iwiki_mcp.codegraph.models import file_id, symbol_id


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
    assert symbol_id("backend", "pkg.a", "Thing.run", "(self,x)").startswith(
        "py:symbol:"
    )


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
```

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


@dataclass(frozen=True)
class ReferenceRecord:
    source_symbol_id: str | None
    source_file_id: str
    relation_type: str
    target_reference: str
    source_line: int | None


@dataclass(frozen=True)
class RelationRecord:
    relation_id: str
    source_symbol_id: str | None
    source_file_id: str
    target_symbol_id: str | None
    target_reference: str
    relation_type: str
    source_line: int | None
    confidence: float
    resolution_state: str


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
    qualified_name: str
    match: str


class CodeGraphError(RuntimeError):
    code = "code_graph_error"


def file_id(domain: str, language: str, path: str) -> str:
    value = "\0".join(("file", domain, language, PurePosixPath(path).as_posix()))
    return f"{_language_prefix(language)}:file:{sha256(value.encode()).hexdigest()}"


def symbol_id(domain: str, module: str, qualified: str, signature: str) -> str:
    value = "\0".join(("symbol", "python", domain, module, qualified, signature))
    return f"py:symbol:{sha256(value.encode()).hexdigest()}"
```

Implement `CodeGraphConfig.from_mapping` with the defaults and allowed fields from spec Section 6.3, explicit integer/boolean validation, environment overrides, rejection of `database`, `project_id`, `project_uuid`, and `incremental`, and only `python` as a language. Implement `CodeGraphLocationResolver.resolve` by validating the domain with the same rules as `server._validate_domain`, resolving `base/.iwiki`, and returning the five exact paths.

- [ ] **Step 4: Lock dependency resolution and run focused tests**

```bash
uv lock
uv run pytest -q tests/codegraph/test_config_location_models.py
```

Expected: all focused tests pass and `uv.lock` contains both Tree-sitter packages.

- [ ] **Step 5: Bump version and commit Unit A contracts**

Set `pyproject.toml` version to `0.7.63`.

```bash
git add pyproject.toml uv.lock src/iwiki_mcp/codegraph tests/codegraph
git commit -m "feat(codegraph): add configuration and identities"
```

### Task 2: SQLite schema and store primitives

**Closes:** R-005, R-006, R-007; produces AC-05, AC-06, and AC-07.

**Files:**
- Create: `src/iwiki_mcp/codegraph/schema.py`
- Create: `src/iwiki_mcp/codegraph/store.py`
- Create: `tests/codegraph/test_store.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write failing schema, cascade, and integrity tests**

```python
from iwiki_mcp.codegraph.store import CodeGraphStore


def test_schema_v1_has_required_tables_indexes_and_cascades(tmp_path):
    store = CodeGraphStore(tmp_path / "code-backend.sqlite3")
    with store.connect() as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert names == {
            "repositories", "files", "symbols", "relations", "wiki_code_links"
        }
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
```

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

Put the exact five-table and eleven-index SQL from spec Section 7 in `schema.py`. In `CodeGraphStore.connect`, configure the connection, create only an empty v1 schema, compare normalized `sqlite_master` DDL and `PRAGMA table_info` against constants, and raise sanitized `CodeGraphSchemaError` on mismatch. Add transaction helpers for inserting normalized snapshots, reading rows in stable ID order, and deleting cascaded repositories.

- [ ] **Step 4: Run store tests including corruption and DDL mismatch**

```bash
uv run pytest -q tests/codegraph/test_store.py
```

Expected: schema, index, cascade, WAL, busy-timeout, integrity, and incompatible-DDL tests pass.

- [ ] **Step 5: Bump version and commit store foundation**

Set version to `0.7.64`.

```bash
git add pyproject.toml src/iwiki_mcp/codegraph/schema.py src/iwiki_mcp/codegraph/store.py tests/codegraph/test_store.py
git commit -m "feat(codegraph): add SQLite store schema"
```

### Task 3: Safe discovery and deterministic fingerprints

**Closes:** R-010, R-011, R-012; produces AC-10, AC-11, and AC-12.

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

    snapshot = discover_python(project, CodeGraphConfig())

    assert [item.path for item in snapshot.files] == ["safe.py"]
    assert {warning.code for warning in snapshot.warnings} >= {
        "secret_excluded", "symlink_excluded"
    }
```

- [ ] **Step 2: Run focused tests and confirm missing discovery API**

```bash
uv run pytest -q tests/codegraph/test_discovery_fingerprint.py
```

Expected: import failure for `discover_python`.

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

Use `os.scandir` without symlink following, project-relative POSIX paths, `pathspec` for Git/iwiki/config rules, hard secret exclusions that negation cannot override, and pre-read count/size limits. Add Git commit and dirty marker helpers using sanitized subprocess output. Compose source/config/parser fingerprints without absolute paths, timestamps, PIDs, or random values.

- [ ] **Step 4: Run focused and existing ignore tests**

```bash
uv run pytest -q tests/codegraph/test_discovery_fingerprint.py tests/test_iwikiignore.py tests/test_base.py
```

Expected: all tests pass; relocated projects with identical domain/content have identical fingerprints.

- [ ] **Step 5: Bump version and commit discovery**

Set version to `0.7.65`.

```bash
git add pyproject.toml src/iwiki_mcp/codegraph/discovery.py src/iwiki_mcp/codegraph/fingerprint.py tests/codegraph/test_discovery_fingerprint.py tests/fixtures/codegraph/security_paths
git commit -m "feat(codegraph): add safe source discovery"
```

## Unit B — Python indexing, resolution, and search

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

Set version to `0.7.66`.

```bash
git add pyproject.toml src/iwiki_mcp/codegraph/languages tests/codegraph/test_python_adapter.py tests/fixtures/codegraph/python_basic tests/fixtures/codegraph/python_syntax_errors
git commit -m "feat(codegraph): extract Python declarations"
```

### Task 5: Imports, calls, inheritance, and conservative resolution

**Closes:** R-013, R-014, and R-015; produces AC-13, AC-14, AC-15, and quality input for AC-27.

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

Extend the adapter to emit normalized `IMPORTS`, `CALLS`, and `INHERITS` references with source symbol/file and line. Resolve local absolute/relative imports, aliases, imported names, unique project classes, local bindings, imported callables, and unambiguous `self`/class methods. Emit one candidate relation per ambiguous target. Preserve dynamic/reflection/DI/wildcard/external references without guessing. Sort every output by source identity, relation type, source line, and target/reference before assigning relation IDs.

- [ ] **Step 4: Run adapter/resolver fixtures**

```bash
uv run pytest -q tests/codegraph/test_python_adapter.py tests/codegraph/test_resolver.py
```

Expected: exact, partial, ambiguous, unresolved, alias, relative-import, call, and inheritance tests pass.

- [ ] **Step 5: Bump version and commit resolver**

Set version to `0.7.67`.

```bash
git add pyproject.toml src/iwiki_mcp/codegraph/languages/python.py src/iwiki_mcp/codegraph/resolver.py tests/codegraph/test_resolver.py tests/fixtures/codegraph/python_imports tests/fixtures/codegraph/python_inheritance tests/fixtures/codegraph/python_dynamic
git commit -m "feat(codegraph): resolve Python relations"
```

### Task 6: Full-build indexer and runtime state facade

**Closes:** R-007, R-008, R-009, R-010, R-017, and R-025; produces AC-07, AC-08, AC-09, AC-10, AC-17, and AC-25.

**Files:**
- Create: `src/iwiki_mcp/codegraph/indexer.py`
- Create: `src/iwiki_mcp/codegraph/runtime.py`
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
```

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

Implement spec Section 12's 13 ordered build steps. Use a UUID only for the staging filename, never for portable IDs. Acquire the per-domain `FileLock`, validate staging, checkpoint/close it, atomically replace the canonical DB, atomically replace metadata JSON, reopen and verify revision, then release. Status reads metadata/schema only. Matching fingerprint returns no-op. Missing/dirty bounded lazy build uses the configured request budget and never returns stale graph rows as current.

- [ ] **Step 4: Run runtime, store, graph, and concurrency-adjacent tests**

```bash
uv run pytest -q tests/codegraph/test_indexer_runtime.py tests/codegraph/test_store.py tests/test_graph_runtime.py tests/test_lock.py
```

Expected: all focused tests pass; fault injection never exposes staging or changes the previous ready revision.

- [ ] **Step 5: Bump version and commit indexing lifecycle**

Set version to `0.7.68`.

```bash
git add pyproject.toml src/iwiki_mcp/codegraph/indexer.py src/iwiki_mcp/codegraph/runtime.py src/iwiki_mcp/codegraph/store.py tests/codegraph/test_indexer_runtime.py
git commit -m "feat(codegraph): build atomic graph snapshots"
```

### Task 7: Deterministic symbol search

**Closes:** R-018; produces AC-18.

**Files:**
- Create: `src/iwiki_mcp/codegraph/query.py`
- Create: `tests/codegraph/test_query.py`
- Modify: `src/iwiki_mcp/codegraph/runtime.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write failing tier-order and filter tests**

```python
def test_search_orders_exact_local_before_prefix_and_lexical(ready_runtime):
    out = ready_runtime.search("run", kinds=["method"], path="src/", limit=4)

    assert [item["match"] for item in out["results"]] == [
        "exact_local", "prefix", "lexical", "path"
    ]
    assert all(not item["path"].startswith("/") for item in out["results"])
    assert out["results"] == ready_runtime.search(
        "run", kinds=["method"], path="src/", limit=4
    )["results"]
```

- [ ] **Step 2: Run query tests and confirm missing search implementation**

```bash
uv run pytest -q tests/codegraph/test_query.py
```

Expected: `CodeGraphRuntime` has no `search` method.

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

- [ ] **Step 4: Run query/store tests**

```bash
uv run pytest -q tests/codegraph/test_query.py tests/codegraph/test_store.py
```

Expected: ranking, filters, limits, ranges, stable ties, and invalid-input tests pass.

- [ ] **Step 5: Bump version and commit search**

Set version to `0.7.69`.

```bash
git add pyproject.toml src/iwiki_mcp/codegraph/query.py src/iwiki_mcp/codegraph/runtime.py tests/codegraph/test_query.py
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

Set version to `0.7.70`.

```bash
git add pyproject.toml src/iwiki_mcp/codegraph/context.py src/iwiki_mcp/codegraph/runtime.py tests/codegraph/test_context.py
git commit -m "feat(codegraph): add bounded source context"
```

### Task 9: Wiki selector parsing and derived links

**Closes:** R-021, R-022, and R-024; produces AC-21, AC-22, and AC-24.

**Files:**
- Create: `src/iwiki_mcp/codegraph/linking.py`
- Create: `tests/codegraph/test_linking.py`
- Modify: `src/iwiki_mcp/codegraph/indexer.py`
- Modify: `src/iwiki_mcp/codegraph/context.py`
- Modify: `src/iwiki_mcp/engine/frontmatter.py`
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

Extend frontmatter parsing to preserve a `code` mapping without changing existing normalized fields. Validate exact selector shapes. Resolve qualified names against symbol rows, files against exact project-relative paths, and globs against safe non-secret file rows. Materialize symbol or file links, deduplicate by target using `SELECTOR_PRIORITY`, set `DOCUMENTED_BY`, and persist stable provenance. Do not write Markdown or generate suggested links. Extend context Wiki enrichment to use exact symbol and containing-file links.

- [ ] **Step 4: Run linking, frontmatter, and context tests**

```bash
uv run pytest -q tests/codegraph/test_linking.py tests/codegraph/test_context.py tests/test_frontmatter.py tests/test_lint_frontmatter.py
```

Expected: typed links, specificity, cascades, safe glob handling, frontmatter compatibility, and Wiki enrichment tests pass.

- [ ] **Step 5: Bump version and commit Wiki-code links**

Set version to `0.7.71`.

```bash
git add pyproject.toml src/iwiki_mcp/codegraph/linking.py src/iwiki_mcp/codegraph/indexer.py src/iwiki_mcp/codegraph/context.py src/iwiki_mcp/engine/frontmatter.py tests/codegraph/test_linking.py
git commit -m "feat(codegraph): link Wiki pages to code"
```

### Task 10: Register the four fail-soft MCP tools

**Closes:** R-001, R-016, R-017, R-018, R-019, R-020, R-025, and R-026; produces AC-01, AC-16, AC-17, AC-18, AC-19, AC-20, AC-25, and AC-26.

**Files:**
- Modify: `src/iwiki_mcp/server.py`
- Modify: `src/iwiki_mcp/codegraph/runtime.py`
- Create: `tests/codegraph/test_server_tools.py`
- Modify: `tests/test_package.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write failing primary-routing and fail-soft handler tests**

```python
def test_code_tools_use_primary_and_do_not_change_wiki_search(seed_binding, monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_code_runtime", lambda binding: FakeRuntime(binding, calls))
    search_signature = inspect.signature(server.wiki_search)

    assert server.wiki_code_status()["domain"] == "backend"
    assert server.wiki_code_index(force=True)["domain"] == "backend"
    assert calls == ["status:backend", "index:backend"]
    assert inspect.signature(server.wiki_search) == search_signature


def test_missing_primary_is_fail_soft_and_wiki_status_still_works(seed_without_primary):
    assert server.wiki_code_status()["code"] == "not_configured"
    assert "domains" in server.wiki_status()
```

- [ ] **Step 2: Run server-tool tests and confirm missing functions**

```bash
uv run pytest -q tests/codegraph/test_server_tools.py
```

Expected: `server` has no `wiki_code_status`.

- [ ] **Step 3: Add thin composition-root handlers**

```python
@_safe
def wiki_code_status() -> dict:
    return _code_runtime(base.resolve_binding()).status()


@_safe
def wiki_code_index(force: bool = False, languages: list[str] | None = None) -> dict:
    return _code_runtime(base.resolve_binding()).index(force=force, languages=languages)
```

Add equally thin `wiki_code_search` and `wiki_code_context` with the exact spec Section 13 signatures. `_code_runtime` validates primary and loads `CodeGraphConfig` from `.iwiki.toml` plus environment. Extend `_safe` with sanitized `CodeGraphError` mapping before the generic exception branch. Register exactly these four functions with FastMCP using the existing registration pattern. Do not add a domain parameter or alter `wiki_search`.

- [ ] **Step 4: Run MCP schema, smoke, startup, and Wiki search tests**

```bash
uv run pytest -q tests/codegraph/test_server_tools.py tests/test_mcp_smoke.py tests/test_server_startup.py tests/test_server_search.py tests/test_package.py
```

Expected: four schemas match the spec, startup remains lazy, old Wiki tests pass, and injected code failures permit succeeding Wiki calls.

- [ ] **Step 5: Bump version and commit MCP surface**

Set version to `0.7.72`.

```bash
git add pyproject.toml src/iwiki_mcp/server.py src/iwiki_mcp/codegraph/runtime.py tests/codegraph/test_server_tools.py tests/test_package.py
git commit -m "feat(codegraph): expose code graph MCP tools"
```

### Task 11: Add code-aware Wiki lint diagnostics

**Closes:** R-023 and R-026; produces AC-23 and AC-26.

**Files:**
- Modify: `src/iwiki_mcp/server.py`
- Modify: `src/iwiki_mcp/codegraph/linking.py`
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

Set version to `0.7.73`.

```bash
git add pyproject.toml src/iwiki_mcp/server.py src/iwiki_mcp/codegraph/linking.py tests/codegraph/test_lint.py tests/test_server_lint_sync.py
git commit -m "feat(codegraph): lint Wiki code selectors"
```

### Task 12: Recovery and multi-process concurrency evidence

**Closes:** R-006, R-007, R-008, R-009, and R-017; produces AC-06, AC-07, AC-08, AC-09, and AC-17.

**Files:**
- Modify: `src/iwiki_mcp/codegraph/store.py`
- Modify: `src/iwiki_mcp/codegraph/indexer.py`
- Modify: `src/iwiki_mcp/codegraph/runtime.py`
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

Expected: concurrent readers see complete revisions, one writer publishes, competitors time out as `busy`, corruption rebuilds, and Wiki hashes remain unchanged.

- [ ] **Step 5: Bump version and commit recovery/concurrency**

Set version to `0.7.74`.

```bash
git add pyproject.toml src/iwiki_mcp/codegraph/store.py src/iwiki_mcp/codegraph/indexer.py src/iwiki_mcp/codegraph/runtime.py tests/codegraph/test_recovery_concurrency.py
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

Run against a copied temporary corpus so production configuration and databases cannot change. Record Python/platform/CPU, corpus hash/count/bytes, package/schema/adapter/resolver versions, exact command, all spec Section 16 metrics, and threshold pass/fail. Compare two forced builds row-for-row and revision-for-revision. Report failures without changing production defaults.

- [ ] **Step 4: Run benchmark tests and a local smoke report**

```bash
uv run pytest -q tests/eval/test_code_graph_runner.py tests/eval/test_code_graph_report.py
uv run python -m eval.code_graph --fixture-root tests/fixtures/codegraph --output /tmp/iwiki-code-graph-evidence
```

Expected: tests pass; output contains JSON and Markdown with environment, corpus, versions, quality, performance, and deterministic comparison.

- [ ] **Step 5: Bump version and commit benchmark tooling**

Set version to `0.7.75`.

```bash
git add pyproject.toml eval/code_graph tests/eval/test_code_graph_runner.py tests/eval/test_code_graph_report.py docs/superpowers/evidence/code-graph-benchmark-method.md
git commit -m "test(codegraph): add benchmark evidence runner"
```

### Task 14: User documentation, Wiki update, and final regression gates

**Closes:** R-026, R-029, and R-030; produces AC-26, AC-29, and AC-30.

**Files:**
- Modify: `README.md`
- Modify: `docs/README.ru.md`
- Modify: `docs/architecture.md`
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

Expected: documentation assertion fails; CLI help still succeeds.

- [ ] **Step 3: Document configuration, tools, safety, lifecycle, and debt**

Add the exact `.iwiki.toml` block from spec Section 6.3, derived paths, four tool signatures/result semantics, no-startup-build rule, security exclusions, recovery command, benchmark command, and explicit Python-only/deferred Incremental/TypeScript statements to English and Russian user docs. Update architecture package/data-flow sections without claiming incremental indexing, TypeScript, impact analysis, or hybrid retrieval exists.

- [ ] **Step 4: Update iwiki through MCP and run final verification**

Use `wiki_update_page` for existing `architecture`, `mcp-server`, `installation`, and `reference/code-graph-technical-debt` sections, with changed source paths. Then run:

```bash
uv run pytest -q
uv run flake8 src tests eval
uv run python -m compileall -q src tests eval
uv run iwiki-mcp --help
git diff --check
```

Expected: full suite passes; flake8/compileall/diff checks are clean; CLI help succeeds. `wiki_lint(domain="iwiki-mcp")` has no code-graph broken/stale/missing-source findings and graph parity is ready.

- [ ] **Step 5: Bump version and commit documentation**

Set version to `0.7.76`.

```bash
git add pyproject.toml README.md docs/README.ru.md docs/architecture.md tests/test_package.py
git commit -m "docs(codegraph): document Python MVP"
```

## Final result evidence

After all tasks and task-level reviews:

```bash
uv run pytest -q
uv run flake8 src tests eval
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
