---
review:
  plan_hash: ac09f48051707d73
  last_run: 2026-08-13
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

**Goal:** Deliver the approved Python-only schema-v2 code-graph MVP with deterministic full rebuilds, a typed file/module/symbol query surface, four fail-soft MCP tools, bounded context, and human-authored Wiki selectors.

**Architecture:** Remediate the schema-v1 code-graph baseline in place before adding Unit C. A language-neutral core builds a per-primary-domain, exactly-five-table SQLite cache, derives module occurrences from file rows, creates a query-time typed union, and publishes snapshots through the ordered two-verification protocol. Existing Wiki storage, `wiki_search`, Markdown, vectors, and ingest logs remain independent and available on every code-graph failure.

**Tech Stack:** Python 3.10+, SQLite WAL, `filelock`, `pathspec`, Tree-sitter, FastMCP, `pytest`, and `pytest-asyncio`.

**Approved spec:** `docs/superpowers/specs/2026-08-09-iwiki-mcp-code-graph-design.md` (`aa4913d5457d7806`)

**Execution baseline:** commit `c87ccb0`, package version `0.7.89`. Tasks 1–12, both prior benchmark-remediation approvals, Task 7R, the approved v1 latency intent, and the threshold-only approved specification are preserved. The uncommitted Task 7R-P query diff passed `1749` tracked tests and improved the authoritative search warm maximum to `246.256735 ms`, then stopped on the former `<150 ms` gate with evidence SHA-256 `a472e90074868fee39516973eb551b2adb5838b017baafb966607118d06a4d24`.

**Plan status:** checked and human-approved threshold-only release-gate revision; execution remains blocked until the approval commit is complete.

**Resume point:** Tasks 1–12, the original Schema-v2 Remediation Gate, approval commits through `c87ccb0`, and Task 7R commit `b8b8f16` are historical committed inputs. Task 7R-P Steps 1–3 are preserved uncommitted work and MUST NOT be replayed. New execution resumes with the Task 13 threshold seam described before Task 7R-P Step 4, then returns to Task 7R-P Step 4. Task 14 result reconciliation still verifies every historical input.

---

## Non-negotiable execution contract

- Preserve the approved sequential ownership: Unit A owns R-001–R-009; Unit B owns R-010–R-015, R-017, R-018, R-025, and the status/index/search portion of R-016; Unit C owns context completion, Wiki links, lint, regression evidence, benchmarks, docs, and debt tracking.
- Preserve all completed Tasks 1–12 commits plus approval commit `14c9f66` and Task 7R commit `b8b8f16`. Task 7R-P is a forward remediation commit; do not amend, rewrite, or replay completed history.
- Task 8 MUST NOT begin until the Schema-v2 Remediation Gate after Task 7 records every command as passing.
- Schema v2 has exactly five authoritative tables and exactly twenty named explicit indexes. It has no `modules` table, FTS/shadow table, persisted search projection, trigger-maintained copy, Python SQLite UDF, or hidden candidate cap.
- Canonical public entities are a query-time discriminated union of `file`, `module`, and `symbol`. A module is an optional occurrence facet of a file row and is never a synthetic symbol.
- Search implements the exact nine ranks as sequential exclusive queries: qualified exact, local exact, explicit-alias exact, canonical prefix, explicit-alias prefix, canonical lexical, explicit-alias lexical, signature, and path. Stop only after the public limit is full; do not add a hidden cap.
- The first-release search gate is `<500 ms` for the maximum of ten warm samples for every 100,000-entity case. The report also records whether each case meets `<150 ms` as a non-blocking post-v1 target; that comparison MUST NOT affect the first-release verdict.
- Context accepts `seeds`, not symbol-only inputs, and every seed is an exact file/module/symbol `entity_id`.
- `wiki_code_status`, `wiki_code_index`, `wiki_code_search`, and `wiki_code_context` are the complete code-tool surface. None accepts `domain`; `wiki_search` is unchanged.
- Incremental indexing and TypeScript remain separate technical debt. No task may add an incremental parameter, TypeScript adapter, or claim either capability.
- Every behavior task updates its owning iwiki page with `wiki_update_page` and runs `wiki_lint(domain="iwiki-mcp")`. Existing unrelated lint advisories may remain; no changed code-graph page may remain stale, broken, or missing its source.
- Every repository commit changes the version in `pyproject.toml` and `src/iwiki_mcp/__init__.py`, runs `uv lock`, stages the resulting `uv.lock`, and proves parity with `uv lock --check` plus `tests/test_package.py`.
- Before every task commit, run `uv run pytest -q`, `uv run flake8 src tests`, `uv lock --check`, and `git diff --check`. Starting with Task 13, include `eval/code_graph` in the flake8 command. A task is not complete and its commit MUST be amended before handoff when any gate fails.
- After plan approval, keep this body immutable so `plan_hash` remains valid. Workers report checkbox/evidence state in their task handoff and commits; they do not rewrite checked boxes or plan text. Only `check-chain result` may update frontmatter and the task-log row before the final commit.

## Closed human checkpoints

**HUMAN CHECKPOINT — CLOSED 2026-08-13:** Revised spec hash `3625b9f571db61f1` fixes sequential exclusive-rank queries with public-limit early-stop; separate search and production corpora; production-only DB/source measurement; canonical semantic determinism; independent Unicode/lexical truth; and one cold plus one warm-up plus ten warm samples. Thresholds, schema, indexes, public contracts, and Unit ownership remain unchanged.

**HUMAN CHECKPOINT — CLOSED 2026-08-13:** The corrected Task 13 benchmark passed every quality and production-corpus gate but measured lower search ranks at `261–445 ms` against the former `<150 ms` gate. Review traced the miss to incomplete Task 7R execution: canonical SQL still materialized a broad three-arm union and applied kind/path/rank predicates outside each arm. The checked predicate-pushdown plan added only that missing implementation and verification detail without changing the then-current contract.

**HUMAN CHECKPOINT — CLOSED 2026-08-13:** Task 7R-P branch-local predicate pushdown passed the full tracked suite and every non-search benchmark gate, improved search from `444.799033 ms` to `246.256735 ms`, and still missed only the former `<150 ms` release gate. The user approved a threshold-only first-release contract: `<500 ms` warm maximum is blocking, while `<150 ms` remains reported and non-blocking after v1. Approved intent `42d4d324998d40e3` and spec `aa4913d5457d7806` change no schema, index, corpus, sample policy, rank, query semantics, correctness, safety, or resource gate.

No design fork remains beyond approval of this checked revision. Reopen design review and stop if implementation would weaken a hard constraint, add another authoritative/search table or index-backed projection, apply NFC/NFKC, add FTS/UDF/candidate caps, expose incremental/TypeScript behavior, change the four tools or `wiki_search`, allow module/alias selectors, change publication order, or move requirement ownership across units.

## Release sequence

| Gate or task | Required version | Commit intent |
|---|---:|---|
| Checked and human-approved plan | `0.7.72` | `docs(codegraph): approve schema v2 implementation plan` |
| Task 1 | `0.7.73` | schema-v2 contracts and normalization |
| Task 2 | `0.7.74` | exact schema and publication primitives |
| Task 3 | `0.7.75` | occurrence-aware discovery fingerprints |
| Task 4 | `0.7.76` | schema-v2 Python extraction |
| Task 5 | `0.7.77` | typed conservative resolution |
| Task 6 | `0.7.78` | schema-v2 rebuild and publication lifecycle |
| Task 7 | `0.7.79` | typed-union search conformance |
| Task 8 | `0.7.80` | typed bounded context |
| Task 9 | `0.7.81` | selectors and derived Wiki links |
| Task 10 | `0.7.82` | exact four-tool registration |
| Task 11 | `0.7.83` | code-aware Wiki lint |
| Task 12 | `0.7.84` | recovery and concurrency evidence |
| Checked and human-approved benchmark-remediation plan | `0.7.85` | `docs(codegraph): approve benchmark remediation plan` |
| Task 7R | `0.7.86` | sequential exclusive-rank search execution |
| Checked and human-approved predicate-pushdown plan | `0.7.87` | `docs(codegraph): approve predicate-pushdown remediation plan` |
| Checked and human-approved v1 latency intent | `0.7.88` | `docs(codegraph): approve v1 search latency gate` |
| Checked and human-approved threshold-only spec | `0.7.89` | `docs(codegraph): approve v1 search latency spec` |
| Checked and human-approved threshold-only plan | `0.7.90` | `docs(codegraph): approve v1 search latency plan` |
| Task 7R-P | `0.7.91` | branch-local search predicate execution |
| Task 13 | `0.7.92` | corrected quality and split-corpus benchmark |
| Task 14 | `0.7.93` | docs, debt, and final gates |

Before Task 7R, validate this file with `$check-chain plan docs/superpowers/plans/2026-08-09-iwiki-mcp-code-graph.md`, obtain human approval, bump `pyproject.toml`, `src/iwiki_mcp/__init__.py`, and `tests/test_package.py` from `0.7.84` to `0.7.85`, run `uv lock`, and commit only the approved spec, checked plan, `docs/TODO.md`, package-version test, and three version artifacts. Do not stage the untracked `eval/code_graph/` scaffold or Task 13 tests in the approval commit. Execution resumes only from that committed state.

```bash
git add docs/TODO.md docs/superpowers/specs/2026-08-09-iwiki-mcp-code-graph-design.md docs/superpowers/plans/2026-08-09-iwiki-mcp-code-graph.md pyproject.toml src/iwiki_mcp/__init__.py tests/test_package.py uv.lock
git commit -m "docs(codegraph): approve benchmark remediation plan"
```

Before Task 7R-P, validate this revised body with `$check-chain plan`, obtain human approval, bump the package and package-test version from `0.7.86` to `0.7.87`, run `uv lock`, and commit only this checked plan, `docs/TODO.md`, the package-version test, and the three version artifacts. Preserve the stopped untracked Task 13 runner and tests without staging them in the approval commit.

```bash
git add docs/TODO.md docs/superpowers/plans/2026-08-09-iwiki-mcp-code-graph.md pyproject.toml src/iwiki_mcp/__init__.py tests/test_package.py uv.lock
git commit -m "docs(codegraph): approve predicate-pushdown remediation plan"
```

Before resuming Task 7R-P, validate this threshold-only revision with `$check-chain plan`, obtain human approval, bump the package and package-test version from `0.7.89` to `0.7.90`, run `uv lock`, and commit only this checked plan, `docs/TODO.md`, the package-version test, and the three version artifacts. Preserve both the uncommitted Task 7R-P query diff and the untracked Task 13 scaffold/tests without staging them in the approval commit.

```bash
git add docs/TODO.md docs/superpowers/plans/2026-08-09-iwiki-mcp-code-graph.md pyproject.toml src/iwiki_mcp/__init__.py tests/test_package.py uv.lock
git commit -m "docs(codegraph): approve v1 search latency plan"
```

## File map

| Path | Responsibility | Owning unit/tasks |
|---|---|---|
| `src/iwiki_mcp/codegraph/models.py` | Schema-v2 records, typed entities, stable IDs, normalization primitives | A/1, B/5, C/8 |
| `src/iwiki_mcp/codegraph/config.py` | `[code_graph]` parsing and exact four environment overrides | A/1 |
| `src/iwiki_mcp/codegraph/location.py` | Validated per-primary DB/WAL/SHM/lock/metadata locations | A/1 |
| `src/iwiki_mcp/codegraph/schema.py` | Exact five-table schema v2, twenty indexes, parity metadata | A/2 |
| `src/iwiki_mcp/codegraph/store.py` | Short-lived SQLite access, schema/integrity checks, ordered publication primitives | A/2, B/6, C/12 |
| `src/iwiki_mcp/codegraph/discovery.py` | Contained, non-symlink source discovery and exclusions | B/3 |
| `src/iwiki_mcp/codegraph/fingerprint.py` | Versioned source/config/parser/normalizer/Unicode fingerprints and revision | B/3, B/6 |
| `src/iwiki_mcp/codegraph/languages/base.py` | Language-neutral adapter protocol | B/4 |
| `src/iwiki_mcp/codegraph/languages/python.py` | Python file/module/symbol/reference extraction only | B/4, B/5 |
| `src/iwiki_mcp/codegraph/resolver.py` | Typed project-local conservative resolution | B/5 |
| `src/iwiki_mcp/codegraph/indexer.py` | Deterministic full rebuild/no-op and selector seam | B/6, C/9, C/12 |
| `src/iwiki_mcp/codegraph/runtime.py` | Binding/config/state facade, validation precedence, sanitized diagnostics | B/6–7, C/8, C/10–12 |
| `src/iwiki_mcp/codegraph/query.py` | Projection-free typed-union search and sequential exclusive rank execution | B/7, B/7R |
| `src/iwiki_mcp/codegraph/context.py` | Deterministic bounded BFS and guarded source reads | C/8 |
| `src/iwiki_mcp/codegraph/linking.py` | Selector parsing, file/symbol link materialization, lint inputs | C/9, C/11 |
| `src/iwiki_mcp/engine/frontmatter.py` | Nested `code` mapping parse/render round trip | C/9 |
| `src/iwiki_mcp/okf.py` | Preservation of authored `code` selectors | C/9 |
| `src/iwiki_mcp/server.py` | Thin FastMCP registration and Wiki-lint composition | B/7, C/9–11 |
| `tests/codegraph/` | Focused contract, fixture, security, integration, and concurrency tests | all |
| `tests/fixtures/codegraph/` | Golden Python, duplicate-module, Unicode, and unsafe-path corpora | B/3–5, C/13 |
| `eval/code_graph/` | Non-production split-corpus benchmark runner and reports | C/13 |
| `README.md`, `docs/README.ru.md`, `docs/architecture.md` | User and architecture documentation | C/14 |

## Unit A — contracts, storage, and lifecycle primitives

### Task 1: Remediate schema-v2 records, identities, normalization, and pure validation

**Closes:** R-001–R-004 and the Unit A identity primitives for R-012; produces focused AC-01–AC-04 and AC-12 inputs.

**HUMAN CHECKPOINT — CLOSED:** dependency packaging, identity format, normalizer, and query bounds are approved.

**Baseline carry-forward:** `config.py` already loads `[code_graph]` plus the exact four overrides, and `location.py` already derives/validates the five primary-domain paths. This task keeps both files unchanged unless their named regression tests fail; Step 4 re-verifies R-001–R-004 rather than duplicating those implementations.

**Files:**
- Verify unchanged: `src/iwiki_mcp/codegraph/config.py`
- Verify unchanged: `src/iwiki_mcp/codegraph/location.py`
- Modify: `src/iwiki_mcp/codegraph/models.py`
- Modify: `src/iwiki_mcp/codegraph/languages/base.py`
- Modify: `src/iwiki_mcp/codegraph/query.py`
- Modify: `tests/codegraph/test_config_location_models.py`
- Modify: `tests/codegraph/test_query.py`
- Modify: `tests/test_server_startup.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`

- [ ] **Step 1: Add failing schema-v2 contract tests**

Add `test_schema_v2_identity_normalization_and_typed_records` to `tests/codegraph/test_config_location_models.py` and `test_query_text_validation_is_pure_and_bounded` to `tests/codegraph/test_query.py`:

```python
def test_schema_v2_identity_normalization_and_typed_records():
    key = module_key("src/pkg/service.py")
    assert key == "src/pkg/service.py"
    assert file_id("python", "py", "backend", key).startswith("py:file:")
    assert module_id("python", "py", "backend", key, "pkg.service").startswith("py:module:")
    assert symbol_id("python", "py", "backend", key, "pkg.service.run", "function(x)") != symbol_id(
        "python", "py", "backend", "vendor/pkg/service.py", "pkg.service.run", "function(x)"
    )
    assert NORMALIZER_VERSION == "casefold-token-v1"
    assert token_key("Straße_value Straße") == "\x1fstrasse\x1fvalue\x1f"
    assert compact_casefold("ascii.py") is None
    assert compact_casefold("Straße.py") == "strasse.py"


@pytest.mark.parametrize("value", ["x\0y", "\ud800", "x" * 4097])
def test_query_text_validation_is_pure_and_bounded(value):
    with pytest.raises(CodeGraphQueryError):
        validate_search_request(value)


def test_query_rejects_more_than_sixty_four_distinct_tokens():
    query = " ".join(f"token{number}" for number in range(65))
    with pytest.raises(CodeGraphQueryError):
        validate_search_request(query)
```

The record test must instantiate `FileRecord`, `SymbolRecord`, `RelationRecord`, and `SearchResult` with every field from spec Sections 10.2 and 7.7, including typed IDs and full byte/line ranges.

- [ ] **Step 2: Run RED checks**

```bash
uv run pytest -q tests/codegraph/test_config_location_models.py::test_schema_v2_identity_normalization_and_typed_records tests/codegraph/test_query.py::test_query_text_validation_is_pure_and_bounded
```

Expected: FAIL because schema-v1 `models.py` has no `module_key`, `module_id`, `NORMALIZER_VERSION`, typed `SearchResult`, or complete validation bounds.

- [ ] **Step 3: Implement minimal schema-v2 contracts**

Use the exact schema-column dataclasses from spec Section 10.2. Add only these shared primitives:

```python
NORMALIZER_VERSION = "casefold-token-v1"
UNICODE_DATA_VERSION = unicodedata.unidata_version
_TOKENS = re.compile(r"[^\W_]+", re.UNICODE)


def token_key(*values: str) -> str:
    tokens = sorted({token for value in values for token in _TOKENS.findall(value.casefold())})
    return "\x1f" + "\x1f".join(tokens) + "\x1f"


def compact_casefold(value: str | None) -> str | None:
    if value is None or value.isascii():
        return None
    return value.casefold()


def module_key(path: str) -> str:
    return _validated_relative_posix(path)


def file_id(language: str, language_prefix: str, domain: str, path: str) -> str:
    normalized_path = _validated_relative_posix(path)
    digest = _hashed("file", domain, language, normalized_path)
    return f"{language_prefix}:file:{digest}"


def module_id(
    language: str,
    language_prefix: str,
    domain: str,
    key: str,
    qualified_name: str,
) -> str:
    digest = _hashed("module", language, domain, key, qualified_name)
    return f"{language_prefix}:module:{digest}"


def symbol_id(
    language: str,
    language_prefix: str,
    domain: str,
    key: str,
    qualified_name: str,
    normalized_signature: str,
) -> str:
    digest = _hashed("symbol", language, domain, key, qualified_name, normalized_signature)
    return f"{language_prefix}:symbol:{digest}"
```

Make `LanguageAdapter` expose the registered stable prefix and pass it into ID helpers; remove the core `LANGUAGE_PREFIXES` Python lookup. Make `relation_id` hash `"relation", language, domain`, the exact source entity, relation type, four source-range integers, typed target/reference, binding kind, and binding name, then prefix the digest with the adapter prefix. `validate_search_request` must reject NUL, lone surrogates, over 4,096 UTF-8 bytes, and more than 64 distinct exact tokens before binding or I/O; it must never truncate or normalize with NFC/NFKC.

Keep dependencies mandatory and startup lazy. Do not add a DB path, project UUID, incremental option, TypeScript language, or new environment variable.

In `languages/base.py`, add `prefix: str` beside `language` and `extensions` on `LanguageAdapter`; the Python adapter supplies `language="python"` and `prefix="py"`. Core identity code consumes those values without a Python-specific lookup.

- [ ] **Step 4: Run GREEN checks and update iwiki**

```bash
uv run pytest -q tests/codegraph/test_config_location_models.py tests/codegraph/test_query.py -k 'validation or identity or normalization or config or location'
uv run pytest -q tests/test_server_startup.py
```

Expected: schema-v2 record/ID/normalizer tests pass; invalid inputs fail before I/O; startup does not initialize the parser; existing config/location tests remain green.

Create iwiki page `concept/code-graph-identities` with `wiki_write_page`, documenting the implemented record/identity/normalizer contract and source `src/iwiki_mcp/codegraph/models.py`; then run `wiki_lint(domain="iwiki-mcp")`. Expected: the new page is linked from `concept/code-graph-storage`, is not orphan/stale, and adds no broken/missing-source finding. Keep `reference/code-graph-schema-v2-design` sourced from the approved specification.

- [ ] **Step 5: Bump version and commit**

Set `pyproject.toml` and `src/iwiki_mcp/__init__.py` to `0.7.73`, then run `uv lock` and the mandatory per-task verification gate.

```bash
git add pyproject.toml src/iwiki_mcp/__init__.py uv.lock src/iwiki_mcp/codegraph/models.py src/iwiki_mcp/codegraph/languages/base.py src/iwiki_mcp/codegraph/query.py tests/codegraph/test_config_location_models.py tests/codegraph/test_query.py tests/test_server_startup.py
git commit -m "feat(codegraph): align schema v2 contracts"
```

### Task 2: Remediate the exact schema and ordered publication primitives

**Closes:** R-005–R-009 storage primitives; produces AC-05–AC-09 unit evidence. Task 6 integrates full rebuild and publication.

**HUMAN CHECKPOINT — CLOSED:** exact SQL, incompatibility policy, states, and publication protocol are approved.

**Files:**
- Modify: `src/iwiki_mcp/codegraph/schema.py`
- Modify: `src/iwiki_mcp/codegraph/store.py`
- Modify: `tests/codegraph/test_store.py`
- Modify: `tests/codegraph/test_indexer_runtime.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`

- [ ] **Step 1: Add failing schema-parity and publication-order tests**

Add `test_schema_v2_has_exact_five_tables_and_twenty_indexes`, `test_schema_v1_is_incompatible_not_migrated`, and `test_publication_primitive_orders_two_canonical_verifications`:

```python
from contextlib import closing

EXPECTED_TABLES = {"repositories", "files", "symbols", "relations", "wiki_code_links"}
EXPECTED_INDEXES = {
    "idx_files_repository_path", "idx_files_repository_local",
    "idx_files_content_hash", "idx_files_repository_module_key",
    "idx_files_repository_module_qualified", "idx_files_repository_module_local",
    "idx_symbols_file", "idx_symbols_qualified", "idx_symbols_local", "idx_symbols_kind",
    "idx_relations_source_file_type", "idx_relations_source_module_type",
    "idx_relations_source_symbol_type", "idx_relations_target_module_type",
    "idx_relations_target_symbol_type", "idx_relations_reference",
    "idx_relations_explicit_alias", "idx_wiki_links_page",
    "idx_wiki_links_symbol", "idx_wiki_links_file",
}


def test_schema_v2_has_exact_five_tables_and_twenty_indexes(tmp_path):
    with closing(CodeGraphStore(tmp_path / "code.sqlite3").connect()) as connection:
        assert SCHEMA_VERSION == 2
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
        indexes = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        )}
        assert tables == EXPECTED_TABLES
        assert indexes == EXPECTED_INDEXES
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
```

The publication test records hooks and asserts exactly `replace`, `metadata_rebuilding`, `verify_1`, `metadata_ready_pending`, `verify_2`, `timing_refresh`.

- [ ] **Step 2: Run RED checks**

```bash
uv run pytest -q tests/codegraph/test_store.py::test_schema_v2_has_exact_five_tables_and_twenty_indexes tests/codegraph/test_store.py::test_schema_v1_is_incompatible_not_migrated tests/codegraph/test_indexer_runtime.py::test_publication_primitive_orders_two_canonical_verifications
```

Expected: FAIL because baseline declares `SCHEMA_VERSION = 1`, twelve indexes, schema-v1 columns, and lacks the exact schema-v2 publication primitive contract.

- [ ] **Step 3: Replace schema constants and add minimal ordered primitives**

Copy the five `CREATE TABLE` statements and twenty `CREATE INDEX` statements verbatim from spec Section 7 into the existing `TABLE_DDL` and `INDEX_DDL` mappings. Persist `normalizer_version`, `unicode_data_version`, module facets, normalization columns, typed relation endpoints, source ranges, binding fields, and schema constraints. Validate implicit unique indexes separately from the twenty named indexes.

```python
SCHEMA_VERSION = 2
EXPECTED_TABLES = frozenset({"repositories", "files", "symbols", "relations", "wiki_code_links"})
EXPECTED_INDEXES = frozenset({
    "idx_files_repository_path", "idx_files_repository_local",
    "idx_files_content_hash", "idx_files_repository_module_key",
    "idx_files_repository_module_qualified", "idx_files_repository_module_local",
    "idx_symbols_file", "idx_symbols_qualified", "idx_symbols_local", "idx_symbols_kind",
    "idx_relations_source_file_type", "idx_relations_source_module_type",
    "idx_relations_source_symbol_type", "idx_relations_target_module_type",
    "idx_relations_target_symbol_type", "idx_relations_reference",
    "idx_relations_explicit_alias", "idx_wiki_links_page",
    "idx_wiki_links_symbol", "idx_wiki_links_file",
})


def inspect_compatibility(connection: sqlite3.Connection) -> str:
    return "compatible" if schema_version(connection) == SCHEMA_VERSION else "incompatible"
```

`CodeGraphStore` must refuse schema v1 as incompatible derived state and never run `ALTER TABLE`, copy v1 rows, or build during startup. Add staging reservation, WAL checkpoint/close, atomic replace, provisional metadata, independent canonical verification helpers, and timing-only refresh. Do not integrate the full build here; Task 6 owns that call sequence.

- [ ] **Step 4: Run GREEN checks and update iwiki**

```bash
uv run pytest -q tests/codegraph/test_store.py tests/codegraph/test_indexer_runtime.py -k 'schema or publication or incompatible or state or integrity'
```

Expected: exact five tables, exact twenty named indexes, implicit unique-index parity, constraints, WAL, busy timeout, foreign-key/integrity checks, v1 incompatibility, and ordered primitives pass. Existing Wiki graph table assertions remain unchanged. Task 7 removes the baseline query UDF and supplies final no-UDF AC-06 evidence before the remediation gate.

Update `concept/code-graph-storage` with `wiki_update_page`, replacing heading `Schema v1` with heading `Schema v2` and refreshing `Recovery and publication`, source `src/iwiki_mcp/codegraph/schema.py`; update `reference/code-graph-schema-v2-design`, heading `Lifecycle and compatibility`, source `src/iwiki_mcp/codegraph/store.py`; run iwiki lint with the same no-new-stale/broken expectation.

- [ ] **Step 5: Bump version and commit**

Set `pyproject.toml` and `src/iwiki_mcp/__init__.py` to `0.7.74`, run `uv lock`, then run the mandatory per-task verification gate.

```bash
git add pyproject.toml src/iwiki_mcp/__init__.py uv.lock src/iwiki_mcp/codegraph/schema.py src/iwiki_mcp/codegraph/store.py tests/codegraph/test_store.py tests/codegraph/test_indexer_runtime.py
git commit -m "feat(codegraph): install exact schema v2"
```

## Unit B — discovery, Python indexing, resolution, and search

### Task 3: Make discovery fingerprints occurrence- and Unicode-version-aware

**Closes:** R-011 and the discovery/fingerprint portion of R-012; produces AC-11 and deterministic-input AC-12 evidence.

**Files:**
- Modify: `src/iwiki_mcp/codegraph/fingerprint.py`
- Modify: `tests/codegraph/test_discovery_fingerprint.py`
- Create: `tests/fixtures/codegraph/python_duplicate_modules/root_a/pkg/service.py`
- Create: `tests/fixtures/codegraph/python_duplicate_modules/root_b/pkg/service.py`
- Create: `tests/fixtures/codegraph/python_unicode/pkg/straße.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`

- [ ] **Step 1: Add failing occurrence and version-input tests**

```python
def test_duplicate_module_paths_keep_distinct_occurrence_keys():
    root = Path("tests/fixtures/codegraph/python_duplicate_modules")
    snapshot = discover_sources(root, CodeGraphConfig(), extensions=(".py",))
    keys = [module_key(item.path) for item in snapshot.files]
    assert keys == ["root_a/pkg/service.py", "root_b/pkg/service.py"]
    assert len(set(keys)) == 2


def test_fingerprint_changes_for_schema_normalizer_and_unicode_versions():
    files = (SourceFile("a.py", b"a", "a" * 64, 1),)
    kwargs = dict(
        repository_id="backend", git_commit="1" * 40, dirty_marker="clean",
        schema_version=1, parser_version="parser@1", grammar_version="grammar@1",
        adapter_version="adapter@1", resolver_version="resolver@1",
        normalizer_version="casefold-token-v1", unicode_data_version="15.0.0",
    )
    base = compose_fingerprints(files, CodeGraphConfig(), **kwargs)
    assert compose_fingerprints(files, CodeGraphConfig(), **(kwargs | {"schema_version": 2})).inputs != base.inputs
    assert compose_fingerprints(files, CodeGraphConfig(), **(kwargs | {"normalizer_version": "casefold-token-v2"})).inputs != base.inputs
    assert compose_fingerprints(files, CodeGraphConfig(), **(kwargs | {"unicode_data_version": "future"})).inputs != base.inputs
```

- [ ] **Step 2: Run RED checks**

```bash
uv run pytest -q tests/codegraph/test_discovery_fingerprint.py -k 'occurrence_keys or schema_normalizer_and_unicode'
```

Expected: FAIL because baseline fingerprint inputs omit schema-v2 normalizer/Unicode versions and do not assert occurrence-key preservation.

- [ ] **Step 3: Extend only canonical fingerprint inputs**

```python
def _parser_inputs(
    *,
    languages: Iterable[str],
    schema_version: int | str,
    parser_version: str,
    grammar_version: str,
    adapter_version: str,
    resolver_version: str,
    normalizer_version: str,
    unicode_data_version: str,
) -> dict[str, object]:
    return {
        "adapter_version": adapter_version,
        "grammar_version": grammar_version,
        "languages": sorted(set(languages)),
        "normalizer_version": normalizer_version,
        "parser_version": parser_version,
        "resolver_version": resolver_version,
        "schema_version": schema_version,
        "unicode_data_version": unicode_data_version,
    }
```

Include sorted relative paths/content hashes, Git commit/dirty marker, normalized config/languages/excludes, schema, grammar, adapter, resolver, normalizer, and Unicode-data versions. Exclude absolute paths, timestamps, PIDs, and randomness. Discovery continues to reject all symlinks, secret-like paths, outside paths, and over-budget candidates before parsing. Core discovery accepts adapter extensions and contains no Python filename/module rules.

- [ ] **Step 4: Run GREEN checks and update iwiki**

```bash
uv run pytest -q tests/codegraph/test_discovery_fingerprint.py
```

Expected: all containment, symlink, ignore, secret, size/count, deterministic ordering, relocation, duplicate-occurrence, and version-drift tests pass.

Update `concept/code-graph-discovery-fingerprints`, headings `Fingerprints` and `Containment and race resistance`, source `src/iwiki_mcp/codegraph/fingerprint.py`; run iwiki lint and require the changed page current with no new broken/missing-source entry.

- [ ] **Step 5: Bump version and commit**

Set `pyproject.toml` and `src/iwiki_mcp/__init__.py` to `0.7.75`, run `uv lock`, then run the mandatory per-task verification gate.

```bash
git add pyproject.toml src/iwiki_mcp/__init__.py uv.lock src/iwiki_mcp/codegraph/fingerprint.py tests/codegraph/test_discovery_fingerprint.py tests/fixtures/codegraph/python_duplicate_modules tests/fixtures/codegraph/python_unicode
git commit -m "feat(codegraph): fingerprint schema v2 inputs"
```

### Task 4: Extract schema-v2 files, optional modules, symbols, and normalization

**Closes:** R-013 and supports R-012/R-015; produces AC-13 and language-isolation AC-15 evidence.

**Files:**
- Modify: `src/iwiki_mcp/codegraph/languages/base.py`
- Modify: `src/iwiki_mcp/codegraph/languages/python.py`
- Modify: `src/iwiki_mcp/codegraph/models.py`
- Modify: `src/iwiki_mcp/codegraph/indexer.py`
- Modify: `src/iwiki_mcp/server.py`
- Modify: `tests/codegraph/test_python_adapter.py`
- Modify: `tests/codegraph/conftest.py`
- Create: `tests/fixtures/codegraph/python_basic/empty.py`
- Create: `tests/fixtures/codegraph/python_duplicate_modules/namespace/pkg/service.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`

- [ ] **Step 1: Add failing whole-file/module-facet extraction tests**

```python
def test_schema_v2_file_module_and_symbol_contract(adapter):
    source = b"async def work(value: str = 'x') -> None:\n    return None\n"
    parsed = adapter.parse_file(source, "pkg/__init__.py")
    assert parsed.file.start_line == 1
    assert parsed.file.end_line == 3
    assert parsed.file.start_byte == 0
    assert parsed.file.end_byte == len(source)
    assert parsed.file.module_key == "pkg/__init__.py"
    assert parsed.file.module_qualified_name == "pkg"
    assert parsed.file.module_id.startswith("py:module:")
    assert parsed.symbols[0].kind == "async_function"
    assert parsed.symbols[0].name_tokens_casefold == "\x1fwork\x1f"


def test_unprovable_module_keeps_file_symbols_and_warning(adapter):
    adapter = PythonAdapter(module_names={"namespace/pkg/service.py": None})
    parsed = adapter.parse_file(b"def work():\n    pass\n", "namespace/pkg/service.py")
    assert parsed.file.module_key == "namespace/pkg/service.py"
    assert parsed.file.module_id is None
    assert parsed.symbols
    assert "module_name_unavailable" in parsed.warnings
```

- [ ] **Step 2: Run RED checks**

```bash
uv run pytest -q tests/codegraph/test_python_adapter.py -k 'schema_v2_file_module or unprovable_module'
```

Expected: FAIL because baseline file records have no whole-file ranges/module facets/normalization columns and baseline kinds do not fully distinguish `async_function`.

- [ ] **Step 3: Return schema-v2 normalized records**

```python
file = FileRecord(
    file_id=file_id("python", "py", domain, path), repository_id=domain,
    path=path, path_casefold=compact_casefold(path),
    file_local_name=PurePosixPath(path).name,
    file_name_tokens_casefold=token_key(PurePosixPath(path).name),
    language="python", content_hash=sha256(source).hexdigest(),
    parser_version=self.parser_version, size_bytes=len(source),
    start_line=1, end_line=max(1, source.count(b"\n") + 1),
    start_byte=0, end_byte=len(source), module_key=path,
    module_id=derived_module_id, module_qualified_name=qualified,
    module_local_name=local, module_name_tokens_casefold=module_tokens,
)
```

Derive a deterministic path-to-module mapping from source-root/package evidence and pass it into `PythonAdapter`; the core still calls the unchanged `parse_file(source, path)` protocol. Derive `pkg/__init__.py` as `pkg` only when that mapping proves it. Preserve duplicate dotted names as distinct occurrences. For unprovable names, emit file-only rows and warnings. Persist symbol kinds/ranges/signatures/name tokens/signature casefold and safe metadata without source. Lazy Tree-sitter loading and no execution/import of project code remain mandatory.

Keep project evidence behind the composition seam rather than adding Python rules to core. Extend the neutral factory with the discovered relative path set and let the Python constructor own module-name derivation:

```python
@dataclass(frozen=True)
class AdapterFactory:
    create: Callable[[tuple[str, ...]], LanguageAdapter]
    parser_version: str
    grammar_version: str
    adapter_version: str

    def bind(self, source_paths: tuple[str, ...]) -> AdapterBinding:
        return AdapterBinding(
            adapter=self.create(source_paths),
            parser_version=self.parser_version,
            grammar_version=self.grammar_version,
            adapter_version=self.adapter_version,
        )


def python_adapter(source_paths: tuple[str, ...]) -> PythonAdapter:
    return PythonAdapter(module_names=derive_module_names(source_paths))
```

`CodeGraphIndexer` passes only sorted normalized relative paths to `AdapterFactory.bind`; `derive_module_names` remains in `languages/python.py`. Static dependency tests must prove core modules still do not import `PythonAdapter` or Python grammar rules.

- [ ] **Step 4: Run GREEN checks and update iwiki**

```bash
uv run pytest -q tests/codegraph/test_python_adapter.py
uv run pytest -q tests/codegraph/test_indexer_runtime.py::test_codegraph_core_has_no_python_adapter_dependency
```

Expected: empty/declaration-free, package-init, duplicate-module, file-only, Unicode, declarations, signatures, partial syntax, lazy parser, and language-isolation tests pass.

Update `concept/code-graph-python-extraction`, headings `Parser boundary` and `Extraction result`, source `src/iwiki_mcp/codegraph/languages/python.py`; run iwiki lint and require that previously stale extraction page is current.

- [ ] **Step 5: Bump version and commit**

Set `pyproject.toml` and `src/iwiki_mcp/__init__.py` to `0.7.76`, run `uv lock`, then run the mandatory per-task verification gate.

```bash
git add pyproject.toml src/iwiki_mcp/__init__.py uv.lock src/iwiki_mcp/codegraph/languages/base.py src/iwiki_mcp/codegraph/languages/python.py src/iwiki_mcp/codegraph/models.py src/iwiki_mcp/codegraph/indexer.py src/iwiki_mcp/server.py tests/codegraph/test_python_adapter.py tests/codegraph/conftest.py tests/fixtures/codegraph/python_basic/empty.py tests/fixtures/codegraph/python_duplicate_modules/namespace/pkg/service.py
git commit -m "feat(codegraph): extract schema v2 entities"
```

### Task 5: Resolve typed module/symbol relations with full provenance

**Closes:** R-014 and reinforces R-012/R-015; produces AC-14 and typed-relation AC-12/AC-15 evidence.

**HUMAN CHECKPOINT — CLOSED:** typed targets, four resolution states, duplicate ambiguity, and conservative non-goals are approved.

**Files:**
- Modify: `src/iwiki_mcp/codegraph/models.py`
- Modify: `src/iwiki_mcp/codegraph/languages/python.py`
- Modify: `src/iwiki_mcp/codegraph/resolver.py`
- Modify: `tests/codegraph/test_resolver.py`
- Modify: `tests/codegraph/test_python_adapter.py`
- Modify: `tests/fixtures/codegraph/python_imports/pkg/a.py`
- Modify: `tests/fixtures/codegraph/python_imports/pkg/b.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`

- [ ] **Step 1: Add failing typed resolution and ambiguity tests**

```python
def test_duplicate_modules_and_aliases_preserve_typed_targets_and_ranges(parsed_project):
    result = resolve_project(parsed_project)
    imports = [row for row in result.relations if row.relation_type == "IMPORTS"]
    duplicates = [row for row in imports if row.binding_name == "service"]
    assert len(duplicates) == 2
    assert {row.resolution_state for row in duplicates} == {"ambiguous"}
    assert all(row.target_module_id and row.target_symbol_id is None for row in duplicates)
    assert all(row.source_start_line >= 1 and row.source_end_byte > row.source_start_byte for row in imports)
    declares = [row for row in result.relations if row.relation_type == "DECLARES"]
    assert any(row.source_module_id and row.target_symbol_id for row in declares)
    assert any(row.source_symbol_id and row.target_symbol_id for row in declares)


def test_explicit_and_implicit_bindings_and_external_reference_are_preserved(parsed_project):
    relations = resolve_project(parsed_project).relations
    assert any(row.binding_kind == "explicit_alias" and row.binding_name == "svc" for row in relations)
    assert any(row.binding_kind == "implicit_binding" and row.binding_name == "helper" for row in relations)
    external = next(row for row in relations if row.target_reference == "external.pkg")
    assert external.resolution_state == "unresolved"
    assert external.target_module_id is None and external.target_symbol_id is None
```

- [ ] **Step 2: Run RED checks**

```bash
uv run pytest -q tests/codegraph/test_resolver.py -k 'typed_targets_and_ranges or explicit_and_implicit'
```

Expected: FAIL because baseline relations are symbol/reference-only, use single source positions, and omit module endpoints and binding provenance.

- [ ] **Step 3: Implement exact relation contract and conservative fan-out**

```python
@dataclass(frozen=True)
class RelationRecord:
    relation_id: str
    source_file_id: str
    source_module_id: str | None
    source_symbol_id: str | None
    target_module_id: str | None
    target_symbol_id: str | None
    target_reference: str | None
    relation_type: Literal["DECLARES", "IMPORTS", "CALLS", "INHERITS"]
    source_start_line: int
    source_end_line: int
    source_start_byte: int
    source_end_byte: int
    binding_name: str | None
    binding_kind: Literal["implicit_binding", "explicit_alias"] | None
    binding_name_tokens_casefold: str | None
    confidence: float
    resolution_state: Literal["resolved", "partially_resolved", "unresolved", "ambiguous"]
    metadata_json: str
```

Use one relation per ambiguous candidate, sorted by the full identity. Resolve duplicate module occurrences to all valid module IDs. Persist explicit `as` only as `explicit_alias`; all other introduced import names are `implicit_binding`. Known-module/missing-member is partially resolved with typed prefix plus reference. External/dynamic/wildcard targets remain unresolved. Never guess runtime dispatch, reflection, DI, monkey patching, or external members.

Generate `DECLARES` rows before cross-file reference resolution: module facets declare their top-level symbols, enclosing class/function symbols declare nested symbols and methods, and file-only occurrences use `source_file_id` with no fabricated module target. These rows use the declared symbol ranges and deterministic relation IDs; Task 8 must consume them rather than reconstructing ownership.

- [ ] **Step 4: Run GREEN checks and update iwiki**

```bash
uv run pytest -q tests/codegraph/test_resolver.py tests/codegraph/test_python_adapter.py
```

Expected: four states, module targets, relation ranges, binding provenance, duplicate ambiguity, partial targets, unresolved externals, conservative calls/inheritance, deterministic IDs, and relocation tests pass.

Create `concept/code-graph-resolution` with `wiki_write_page`, document typed targets, four states, duplicate/alias fan-out, binding provenance, and conservative non-goals with source `src/iwiki_mcp/codegraph/resolver.py`; link it from `concept/code-graph-python-extraction`, then run iwiki lint. Expected: the new page is not orphan/stale and adds no broken/missing-source finding. Keep the approved design page sourced from the specification.

- [ ] **Step 5: Bump version and commit**

Set `pyproject.toml` and `src/iwiki_mcp/__init__.py` to `0.7.77`, run `uv lock`, then run the mandatory per-task verification gate.

```bash
git add pyproject.toml src/iwiki_mcp/__init__.py uv.lock src/iwiki_mcp/codegraph/models.py src/iwiki_mcp/codegraph/languages/python.py src/iwiki_mcp/codegraph/resolver.py tests/codegraph/test_resolver.py tests/codegraph/test_python_adapter.py tests/fixtures/codegraph/python_imports/pkg/a.py tests/fixtures/codegraph/python_imports/pkg/b.py
git commit -m "feat(codegraph): resolve typed relations"
```

### Task 6: Integrate schema-v2 full rebuild, no-op, state, and ordered publication

**Closes:** R-008–R-010, R-017, R-025, and status/index portions of R-016; produces AC-08–AC-10, AC-17, and AC-25 build evidence.

**HUMAN CHECKPOINT — CLOSED:** incompatible-cache behavior, no-startup-build rule, state transitions, cancellation boundary, and exact publication order are approved.

**Files:**
- Modify: `src/iwiki_mcp/codegraph/indexer.py`
- Modify: `src/iwiki_mcp/codegraph/runtime.py`
- Modify: `src/iwiki_mcp/codegraph/store.py`
- Modify: `src/iwiki_mcp/codegraph/fingerprint.py`
- Modify: `src/iwiki_mcp/server.py`
- Modify: `tests/codegraph/test_indexer_runtime.py`
- Modify: `tests/codegraph/test_server_tools.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`

- [ ] **Step 1: Add failing schema-v2 rebuild/no-op and protocol tests**

```python
def test_schema_v1_explicit_index_rebuilds_v2_without_row_migration(seed_runtime):
    install_schema_v1_cache(seed_runtime.paths.database, sentinel="must-not-copy")
    assert seed_runtime.status()["state"] != "ready"
    built = seed_runtime.index(force=False)
    assert built["state"] == "ready" and built["schema_version"] == 2
    assert "must-not-copy" not in dump_rows(seed_runtime.paths.database)


def test_publication_order_is_replace_metadata_verify_ready_verify_refresh(seed_runtime):
    events = []
    seed_runtime.observe_publication(events.append)
    seed_runtime.index(force=True)
    assert events == [
        "replace", "metadata_rebuilding", "canonical_verify_1",
        "metadata_ready_pending", "canonical_verify_2", "timing_refresh",
    ]
    assert seed_runtime.status()["pending_final_verify"] is True
    assert seed_runtime.status()["phase_timings_ms"]["final_verification"] >= 0
```

- [ ] **Step 2: Run RED checks**

```bash
uv run pytest -q tests/codegraph/test_indexer_runtime.py -k 'schema_v1_explicit or publication_order_is_replace'
```

Expected: FAIL because baseline build persists schema-v1 rows/contracts and does not produce complete schema-v2 observations in the approved order.

- [ ] **Step 3: Wire the full schema-v2 lifecycle**

Implement the exact eighteen steps from spec Section 12.1. The minimal publication skeleton is:

```python
store.validate_staging(staging)
store.checkpoint_and_close(staging)
control.enter_publication(deadline=deadline, lock_path=paths.lock)
try:
    store.replace_canonical(staging)
    metadata.write_rebuilding(revision)
    store.verify_canonical(revision, pass_number=1)
    metadata.write_ready(revision, pending_final_verify=True)
    store.verify_canonical(revision, pass_number=2)
    metadata.refresh_timings_only(final_verification_ms=elapsed)
finally:
    control.leave_publication()
```

No-op requires matching ready fingerprint and schema/parser/resolver/normalizer/Unicode versions. Startup inspects metadata/schema only; it never discovers, migrates, or builds. Non-ready query guards return no old nodes and `fresh=false`. Status/build include typed entity counts, module warnings, exclusions, parser errors, resolution states, and all phase timings without source or credentials.

- [ ] **Step 4: Run GREEN checks and update iwiki**

```bash
uv run pytest -q tests/codegraph/test_indexer_runtime.py tests/codegraph/test_server_tools.py
```

Expected: full rebuild/no-op, all five states, v1 incompatibility, cancellation, prior-snapshot preservation, two verifications, dirty/toolchain drift, sanitized failures/logs, disabled/no-primary behavior, and observability tests pass.

Update `concept/code-graph-runtime`, headings `Full-build lifecycle`, `Runtime states and query guard`, and `Fail-soft diagnostics`, source `src/iwiki_mcp/codegraph/runtime.py`; update `concept/code-graph-storage`, heading `Recovery and publication`, source `src/iwiki_mcp/codegraph/store.py`; run iwiki lint and require both changed pages current.

- [ ] **Step 5: Bump version and commit**

Set `pyproject.toml` and `src/iwiki_mcp/__init__.py` to `0.7.78`, run `uv lock`, then run the mandatory per-task verification gate.

```bash
git add pyproject.toml src/iwiki_mcp/__init__.py uv.lock src/iwiki_mcp/codegraph/indexer.py src/iwiki_mcp/codegraph/runtime.py src/iwiki_mcp/codegraph/store.py src/iwiki_mcp/codegraph/fingerprint.py src/iwiki_mcp/server.py tests/codegraph/test_indexer_runtime.py tests/codegraph/test_server_tools.py
git commit -m "feat(codegraph): publish schema v2 snapshots"
```

### Task 7: Replace symbol-only search with the typed union and exact nine ranks

**Closes:** R-018 and the search portion of R-016; produces AC-16 and AC-18 focused conformance evidence. Unit C retains the 100,000-entity performance gate.

**Historical delivery input:** the version `0.7.79` commit is present. Task 7R below retains its public contract and replaces only the all-rank execution strategy after the required Task 13 benchmark stop; Task 14 result reconciliation decides final completion.

**Files:**
- Modify: `src/iwiki_mcp/codegraph/models.py`
- Modify: `src/iwiki_mcp/codegraph/query.py`
- Modify: `src/iwiki_mcp/codegraph/runtime.py`
- Modify: `src/iwiki_mcp/server.py`
- Modify: `tests/codegraph/conftest.py`
- Modify: `tests/codegraph/test_query.py`
- Modify: `tests/codegraph/test_server_tools.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`

- [ ] **Step 1: Add failing typed-union, rank, alias, and no-cap tests**

```python
from pathlib import Path


EXPECTED_MATCHES = [
    "qualified_exact", "local_exact", "alias_exact", "canonical_prefix",
    "alias_prefix", "canonical_lexical", "alias_lexical", "signature", "path",
]


def test_search_returns_typed_union_in_exact_rank_order(schema_v2_search_connection):
    results = search_all_rank_fixture(schema_v2_search_connection)
    assert [row.match for row in results] == EXPECTED_MATCHES
    assert {row.entity_type for row in results} == {"file", "module", "symbol"}
    assert all(row.entity_id in {row.file_id, row.module_id, row.symbol_id} for row in results)


def test_alias_aggregation_deduplicates_before_limit_without_candidate_cap(schema_v2_search_connection):
    results = CodeGraphQuery("backend").search(schema_v2_search_connection, validate_search_request("svc", limit=1))
    assert len(results) == 1
    assert results[0].matched_alias == "svc"
    assert results[0].alias_target_count == 2
    assert results[0].alias_ambiguous is True
    assert no_sql_statement_contains_hidden_candidate_limit(schema_v2_search_connection)


def test_query_uses_no_python_sqlite_udf():
    source = Path("src/iwiki_mcp/codegraph/query.py").read_text(encoding="utf-8")
    assert ".create_function(" not in source
```

Add invalid-precedence assertions that NUL, lone surrogate, byte, and token violations touch neither binding nor SQLite even when the feature is disabled or missing.

- [ ] **Step 2: Run RED checks**

```bash
uv run pytest -q tests/codegraph/test_query.py -k 'typed_union_in_exact_rank or alias_aggregation or no_python_sqlite_udf'
```

Expected: FAIL because baseline search returns symbols only, has six ranks, lacks file/module and alias results, and still registers `iwiki_code_fallback_rank` as a Python SQLite UDF.

- [ ] **Step 3: Implement a projection-free typed `UNION ALL`**

```python
MATCH_RANK = {
    "qualified_exact": 0, "local_exact": 1, "alias_exact": 2,
    "canonical_prefix": 3, "alias_prefix": 4,
    "canonical_lexical": 5, "alias_lexical": 6,
    "signature": 7, "path": 8,
}
```

Construct file/module/symbol rows exactly as spec Section 7.7. Canonical tiers query that union. Alias tiers query only `IMPORTS` with `binding_kind='explicit_alias'` and typed module/symbol targets. Aggregate repeated sites per target entity; retain all ambiguous targets; choose lowest Unicode-code-point alias for public output. De-duplicate all tiers by `entity_id` before the caller limit, then sort `(match_rank, qualified_name, entity_id)`. Use persisted casefold/token columns and literal `instr` for signature/path. `%` and `_` are literals. Do not use FTS, a projection table, UDF, or pre-limit candidate cap.

- [ ] **Step 4: Run GREEN checks and update iwiki**

```bash
uv run pytest -q tests/codegraph/test_query.py tests/codegraph/test_server_tools.py
```

Expected: six public kinds, three entity types, nine ranks, Unicode bounds, exact tokens, aliases, ambiguous fan-out, path target filtering, de-duplication-before-limit, stable ties, invalid precedence, literal wildcard characters, and no-cap SQL conformance pass.

Update `concept/code-graph-search`, headings `Validation`, `Candidate retrieval`, and `Ranking and result contract`, source `src/iwiki_mcp/codegraph/query.py`; update `mcp-server`, heading `Tool surface`, source `src/iwiki_mcp/server.py`; run iwiki lint and require both changed pages current. Keep `reference/code-graph-schema-v2-design` sourced from the specification.

- [ ] **Step 5: Bump version and commit**

Set `pyproject.toml` and `src/iwiki_mcp/__init__.py` to `0.7.79`, run `uv lock`, then run the mandatory per-task verification gate.

```bash
git add pyproject.toml src/iwiki_mcp/__init__.py uv.lock src/iwiki_mcp/codegraph/models.py src/iwiki_mcp/codegraph/query.py src/iwiki_mcp/codegraph/runtime.py src/iwiki_mcp/server.py tests/codegraph/conftest.py tests/codegraph/test_query.py tests/codegraph/test_server_tools.py
git commit -m "feat(codegraph): search typed schema v2 entities"
```

## Schema-v2 Remediation Gate — required before Task 8

This gate is part of Unit B completion, not a new unit and not a substitute for Tasks 1–7. Run from version `0.7.79` after all seven commits:

**Historical gate record:** this gate preceded Task 8 in the preserved commit sequence. Task 7R must rerun its focused query, integration, full-suite, and static gates; Task 14 result reconciliation determines final compliance.

```bash
uv run pytest -q tests/codegraph/test_config_location_models.py tests/codegraph/test_store.py tests/codegraph/test_discovery_fingerprint.py tests/codegraph/test_python_adapter.py tests/codegraph/test_resolver.py tests/codegraph/test_indexer_runtime.py tests/codegraph/test_query.py tests/codegraph/test_server_tools.py
uv run python -m compileall -q src/iwiki_mcp/codegraph tests/codegraph
git diff --check
```

Also inspect the built test database and record these exact facts in execution evidence:

- `SCHEMA_VERSION == 2`.
- Persistent table set equals `repositories`, `files`, `symbols`, `relations`, `wiki_code_links`.
- Named explicit index set equals the twenty names in spec Section 7.8; required implicit unique indexes also match.
- No `modules`, FTS/shadow/search-projection table, trigger-maintained copy, Python SQLite UDF, or hidden candidate cap exists.
- File/module/symbol typed-union tests, nine ranks, duplicate modules, aliases, full rebuild/no-op, schema-v1 incompatibility, and ordered two-verification publication pass.
- `wiki_lint(domain="iwiki-mcp")` reports every changed code-graph page current and adds no broken or missing-source finding.

**Gate result:** PASS only when every command exits zero and every fact is evidenced. On any miss, remain in Tasks 1–7, fix the owning task, rerun its RED/GREEN cycle, and amend the still-unpushed Task 7 commit and evidence while retaining version `0.7.79`; if that commit has already been shared, create the next patch version and update the release evidence instead of rewriting shared history. Rerun this complete gate. Task 8 is forbidden while this gate is incomplete or failing.

## Unit C — context, Wiki links, lint, and evidence

### Task 8: Add typed bounded context and guarded source reads

**Closes:** R-019 and R-020; produces AC-19 and AC-20.

**Files:**
- Create: `src/iwiki_mcp/codegraph/context.py`
- Modify: `src/iwiki_mcp/codegraph/models.py`
- Modify: `src/iwiki_mcp/codegraph/store.py`
- Modify: `src/iwiki_mcp/codegraph/runtime.py`
- Create: `tests/codegraph/test_context.py`
- Modify: `tests/codegraph/conftest.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`

- [ ] **Step 1: Add failing typed-seed and source-safety tests**

```python
def test_context_accepts_file_module_and_symbol_seeds(ready_context):
    response = ready_context.context([ready_context.file_id, ready_context.module_id, ready_context.symbol_id])
    assert {node["entity_type"] for node in response["nodes"]} == {"file", "module", "symbol"}
    assert response["limits"] == {"depth": 1, "max_nodes": 50, "max_files": 20, "max_source_bytes": 200000}
    assert response["truncated"] is False


def test_source_requires_explicit_true_and_current_hash(ready_context):
    assert all("source" not in row for row in ready_context.context([ready_context.file_id])["files"])
    ready_context.change_source_after_index()
    response = ready_context.context([ready_context.file_id], include_source=True)
    assert response["fresh"] is False
    assert all("source" not in row for row in response["files"])
```

- [ ] **Step 2: Run RED**

```bash
uv run pytest -q tests/codegraph/test_context.py
```

Expected: collection FAIL because `codegraph.context` does not exist.

- [ ] **Step 3: Implement deterministic BFS and post-traversal source reads**

```python
@dataclass(frozen=True)
class ContextRequest:
    seeds: tuple[str, ...]
    direction: Literal["in", "out", "both"] = "both"
    depth: int = 1
    relations: tuple[str, ...] | None = None
    include_source: bool = False
    include_wiki: bool = True
    max_nodes: int = 50
    max_files: int = 20
    max_source_bytes: int = 200_000
```

Add `validate_context_request(seeds, direction, depth, relations, include_source, include_wiki, max_nodes, max_files, max_source_bytes) -> ContextRequest` as a pure validator and `CodeGraphContext.context(request: ContextRequest) -> dict[str, object]` as the only traversal entry point. The method returns the exact spec Section 13.5 keys and never accepts qualified names in place of entity IDs.

Validate all inputs before binding/I/O. Resolve seeds against the canonical typed union. File seeds activate their module facet at depth 0; module and symbol expansion follows spec Section 13.5, including file-only relations. Order each BFS frontier by `(relation_type, source_entity_id, target_entity_id_or_reference, source_start_byte, relation_id)`. Return full binding/range/unresolved provenance, effective limits, exhausted budget, and truncation. Read source only after explicit true and containment, secret, content-hash, per-file, aggregate-byte checks.

- [ ] **Step 4: Run GREEN and update iwiki**

```bash
uv run pytest -q tests/codegraph/test_context.py tests/codegraph/test_indexer_runtime.py tests/codegraph/test_query.py
```

Expected: every seed type, direction/depth/relation filter, deterministic order, file/node/source budgets, unresolved evidence, stale-source omission, and default no-source behavior pass.

Create `concept/code-graph-context` with `wiki_write_page`, document typed seeds, BFS order/budgets, unresolved evidence, and guarded source reads with source `src/iwiki_mcp/codegraph/context.py`; link it from `concept/code-graph-search`, then run iwiki lint. Expected: the new page is not orphan/stale and adds no broken/missing-source finding.

- [ ] **Step 5: Bump version and commit**

Set `pyproject.toml` and `src/iwiki_mcp/__init__.py` to `0.7.80`, run `uv lock`, then run the mandatory per-task verification gate.

```bash
git add pyproject.toml src/iwiki_mcp/__init__.py uv.lock src/iwiki_mcp/codegraph/context.py src/iwiki_mcp/codegraph/models.py src/iwiki_mcp/codegraph/store.py src/iwiki_mcp/codegraph/runtime.py tests/codegraph/test_context.py tests/codegraph/conftest.py
git commit -m "feat(codegraph): add typed bounded context"
```

### Task 9: Preserve Wiki selectors and materialize generic derived links

**Closes:** R-021, R-022, and R-024; produces AC-21, AC-22, and AC-24.

**Files:**
- Create: `src/iwiki_mcp/codegraph/linking.py`
- Modify: `src/iwiki_mcp/codegraph/indexer.py`
- Modify: `src/iwiki_mcp/codegraph/context.py`
- Modify: `src/iwiki_mcp/engine/frontmatter.py`
- Modify: `src/iwiki_mcp/okf.py`
- Modify: `src/iwiki_mcp/server.py`
- Create: `tests/codegraph/test_linking.py`
- Create: `tests/codegraph/test_frontmatter_roundtrip.py`
- Modify: `tests/codegraph/conftest.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`

- [ ] **Step 1: Add failing selector grammar/link/round-trip tests**

```python
def test_selectors_materialize_only_symbol_or_file_targets(link_fixture):
    links = resolve_selectors(link_fixture.markdown, link_fixture.snapshot)
    assert {row["selector_kind"] for row in links} == {"symbol", "file", "source_glob"}
    assert all((row["symbol_id"] is None) != (row["file_id"] is None) for row in links)
    assert not any("module_id" in row for row in links)


def test_write_update_round_trip_preserves_authored_code_mapping(seed_wiki):
    authored = {"symbols": [{"qualified_name": "pkg.Service.run"}], "files": ["src/pkg/service.py"], "source_globs": ["src/pkg/**"]}
    write_then_update_page_with_code(seed_wiki, authored)
    assert read_frontmatter(seed_wiki)["code"] == authored
```

Reject `modules`, `module_id`, `aliases`, and import bindings as selector keys.

- [ ] **Step 2: Run RED**

```bash
uv run pytest -q tests/codegraph/test_linking.py tests/codegraph/test_frontmatter_roundtrip.py
```

Expected: collection FAIL because `linking.py` and nested `code` round-trip support do not exist.

- [ ] **Step 3: Implement selector preservation and specificity**

Parse exactly `symbols`, `files`, `source_globs`. Symbol selectors create symbol links; file selectors create file links; globs materialize file links only. Deduplicate by target using `symbol > file > source_glob`, retain selector source/provenance, and use `DOCUMENTED_BY` only in `wiki_code_links`. Never mutate authored selectors or generate authoritative/suggested links. Extend frontmatter parse/render and `okf.build_frontmatter` to preserve the nested mapping unchanged.

- [ ] **Step 4: Run GREEN and update iwiki**

```bash
uv run pytest -q tests/codegraph/test_linking.py tests/codegraph/test_frontmatter_roundtrip.py tests/test_frontmatter.py tests/test_chunk_frontmatter.py tests/test_frontmatter_governance.py tests/test_server_write_frontmatter.py tests/test_server_update.py tests/test_okf_build_frontmatter.py tests/test_validate_frontmatter.py tests/test_export_okf.py tests/test_resources_frontmatter.py tests/engine/test_lint.py
```

Expected: exact grammar, provenance, specificity, cascades, safe glob behavior, write/update round trip, no module/alias target, and no selector mutation pass.

Create `concept/code-graph-wiki-linking` with `wiki_write_page`, document exact selector grammar, specificity, file/symbol targets, round-trip preservation, and human authority with source `src/iwiki_mcp/codegraph/linking.py`; link it from `authoring-and-linting`, then run iwiki lint. Expected: the new page is not orphan/stale and adds no broken/missing-source finding.

- [ ] **Step 5: Bump version and commit**

Set `pyproject.toml` and `src/iwiki_mcp/__init__.py` to `0.7.81`, run `uv lock`, then run the mandatory per-task verification gate.

```bash
git add pyproject.toml src/iwiki_mcp/__init__.py uv.lock src/iwiki_mcp/codegraph/linking.py src/iwiki_mcp/codegraph/indexer.py src/iwiki_mcp/codegraph/context.py src/iwiki_mcp/engine/frontmatter.py src/iwiki_mcp/okf.py src/iwiki_mcp/server.py tests/codegraph/test_linking.py tests/codegraph/test_frontmatter_roundtrip.py tests/codegraph/conftest.py
git commit -m "feat(codegraph): link Wiki selectors to code"
```

### Task 10: Register exactly four code tools with typed contracts

**Closes:** remaining R-016 and reinforces R-001/R-017; produces final AC-01, AC-16, and AC-17 tool-surface evidence.

**HUMAN CHECKPOINT — CLOSED:** four signatures, primary routing, `seeds`, and default source omission are approved.

**Files:**
- Modify: `src/iwiki_mcp/server.py`
- Modify: `src/iwiki_mcp/codegraph/runtime.py`
- Modify: `tests/codegraph/test_server_tools.py`
- Modify: `tests/test_mcp_smoke.py`
- Modify: `tests/test_package.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`

- [ ] **Step 1: Add failing real-registry and signature tests**

```python
async def test_real_stdio_registry_has_exact_code_tools(session):
    tools = {tool.name: tool for tool in (await session.list_tools()).tools if tool.name.startswith("wiki_code_")}
    assert set(tools) == {"wiki_code_status", "wiki_code_index", "wiki_code_search", "wiki_code_context"}
    assert all("domain" not in tool.inputSchema.get("properties", {}) for tool in tools.values())
    context = tools["wiki_code_context"].inputSchema["properties"]
    assert "seeds" in context and "symbols" not in context
    assert context["include_source"]["default"] is False
```

- [ ] **Step 2: Run RED**

```bash
uv run pytest -q tests/codegraph/test_server_tools.py tests/test_mcp_smoke.py -k 'exact_code_tools or registry'
```

Expected: FAIL because the Unit B registry still lacks `wiki_code_context` and therefore cannot expose the approved `seeds` contract.

- [ ] **Step 3: Add thin handlers only**

Register status, index, typed search, and context through the existing FastMCP pattern. Context signature must use `seeds` and `include_source=False`. Handlers validate pure request values before binding, require `binding.primary`, load project config next, then call the runtime. Every typed/unexpected failure returns sanitized `{error, hint}` and permits a succeeding ordinary Wiki call. Keep `wiki_search` function and schema byte-compatible.

- [ ] **Step 4: Run GREEN and update iwiki**

```bash
uv run pytest -q tests/codegraph/test_server_tools.py tests/test_mcp_smoke.py tests/test_server_search.py tests/test_server_startup.py tests/test_package.py
```

Expected: real stdio lists exactly four tools, schemas have no domain, primary routing and invalid precedence pass, injected failures do not block Wiki calls, startup stays lazy, and `wiki_search` is unchanged.

Update `mcp-server`, heading `FastMCP wiring`, source `src/iwiki_mcp/server.py`; run iwiki lint and require the previously stale server-backed page current.

- [ ] **Step 5: Bump version and commit**

Set `pyproject.toml` and `src/iwiki_mcp/__init__.py` to `0.7.82`, run `uv lock`, then run the mandatory per-task verification gate.

```bash
git add pyproject.toml src/iwiki_mcp/__init__.py uv.lock src/iwiki_mcp/server.py src/iwiki_mcp/codegraph/runtime.py tests/codegraph/test_server_tools.py tests/test_mcp_smoke.py tests/test_package.py
git commit -m "feat(codegraph): expose exact code tool surface"
```

### Task 11: Add code-aware Wiki lint without blocking ordinary lint

**Closes:** R-023 and reinforces R-026; produces AC-23 and lint-path AC-26 evidence.

**Files:**
- Modify: `src/iwiki_mcp/codegraph/linking.py`
- Modify: `src/iwiki_mcp/server.py`
- Create: `tests/codegraph/test_lint.py`
- Modify: `tests/test_server_lint_sync.py`
- Modify: `tests/engine/test_lint.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`

- [ ] **Step 1: Add failing finding-matrix tests**

```python
def test_code_lint_finding_matrix_preserves_markdown_report(seed_code_lint):
    report = server.wiki_lint("backend")["reports"]["backend"]
    assert {item["type"] for item in report["code_graph"]["findings"]} == {
        "unknown_symbol", "ambiguous_symbol", "missing_file", "empty_glob",
        "unsafe_selector", "ignored_selector", "secret_selector",
        "conflicting_selectors", "stale_revision",
    }
    assert "broken" in report


def test_unavailable_code_graph_does_not_block_ordinary_lint(seed_code_lint_without_graph):
    report = server.wiki_lint("backend")["reports"]["backend"]
    assert report["code_graph"]["available"] is False
    assert "broken" in report
```

- [ ] **Step 2: Run RED**

```bash
uv run pytest -q tests/codegraph/test_lint.py tests/test_server_lint_sync.py
```

Expected: collection or assertion FAIL because ordinary lint lacks the separate `code_graph` block.

- [ ] **Step 3: Compose read-only code diagnostics after ordinary lint**

`lint_domain` returns `{available, state, revision, findings, hint}` and never builds or mutates selectors. Reuse the Task 9 selector parser and discovery safety rules. Disabled, missing, dirty, rebuilding, failed, or incompatible graphs produce availability/remediation diagnostics, not exceptions. Preserve all existing Markdown lint fields and behavior.

- [ ] **Step 4: Run GREEN and update iwiki**

```bash
uv run pytest -q tests/codegraph/test_lint.py tests/test_server_lint_sync.py tests/test_lint_frontmatter.py tests/engine/test_lint.py
```

Expected: complete finding matrix, unavailable behavior, ordinary lint compatibility, and no selector mutation pass.

Update `authoring-and-linting`, heading `Health linting`, source `src/iwiki_mcp/server.py`; run iwiki lint and require the changed page current.

- [ ] **Step 5: Bump version and commit**

Set `pyproject.toml` and `src/iwiki_mcp/__init__.py` to `0.7.83`, run `uv lock`, then run the mandatory per-task verification gate.

```bash
git add pyproject.toml src/iwiki_mcp/__init__.py uv.lock src/iwiki_mcp/codegraph/linking.py src/iwiki_mcp/server.py tests/codegraph/test_lint.py tests/test_server_lint_sync.py tests/engine/test_lint.py
git commit -m "feat(codegraph): lint Wiki code selectors"
```

### Task 12: Prove recovery and multi-process concurrency

**Reinforces:** R-006–R-009, R-017, and R-025 without moving Unit A/B ownership; produces integration AC-06–AC-09, AC-17, and AC-25 evidence.

**Files:**
- Modify: `src/iwiki_mcp/codegraph/store.py`
- Modify: `src/iwiki_mcp/codegraph/indexer.py`
- Modify: `src/iwiki_mcp/codegraph/runtime.py`
- Create: `tests/codegraph/test_recovery_concurrency.py`
- Modify: `tests/codegraph/conftest.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`

- [ ] **Step 1: Add failing process/race/fault-injection tests**

```python
def test_competing_writer_is_busy_and_reader_sees_complete_revision(runtime_pair):
    first, second = runtime_pair
    old = first.index(force=True)["revision"]
    with first.pause_before_replace():
        assert second.index(force=True)["error"] == "busy"
        assert second.status()["revision"] == old


@pytest.mark.parametrize("fault", ["replace", "metadata_rebuilding", "verify_1", "ready_pending", "verify_2", "timing_refresh"])
def test_publication_faults_never_expose_unverified_snapshot(seed_runtime, fault):
    seed_runtime.inject_publication_fault(fault)
    seed_runtime.index(force=True)
    status = seed_runtime.status()
    assert status["state"] != "ready" or status["phase_timings_ms"].get("final_verification") is not None
```

Include corrupt DB, schema-v1, metadata/SQL skew, branch switch, added/changed/deleted dirty files, cancellation before publication, cancellation after atomic entry, and invariant Wiki artifact hashes.

- [ ] **Step 2: Run RED**

```bash
uv run pytest -q tests/codegraph/test_recovery_concurrency.py
```

Expected: at least one new multi-process or fault-boundary assertion fails against the single-process-focused implementation.

- [ ] **Step 3: Make only evidence-driven hardening changes**

Use short-lived readers, one bounded per-domain writer deadline, deterministic corrupt-byte quarantine, SQL revision authority, and generation-checked metadata recovery. Cancellation prevents a new publication critical section; after entry, the complete ordered protocol may finish. Clean only the caller's staging files. Do not change schema, add fallback stores, or weaken fail-soft behavior.

- [ ] **Step 4: Run GREEN and update iwiki**

```bash
uv run pytest -q tests/codegraph/test_recovery_concurrency.py tests/codegraph/test_indexer_runtime.py tests/test_sync_concurrency.py tests/test_sync_parallel.py tests/test_lock.py
```

Expected: complete old-or-new reader observations, bounded competing writers, every fault boundary, corrupt/v1 recovery, metadata reconstruction, branch/dirty rebuild, cancellation rules, and unchanged Wiki hashes pass.

Update `concept/code-graph-storage`, heading `Recovery and publication`, source `src/iwiki_mcp/codegraph/store.py`; update `concept/code-graph-runtime`, heading `Runtime states and query guard`, source `src/iwiki_mcp/codegraph/runtime.py`; run iwiki lint and require both pages current.

- [ ] **Step 5: Bump version and commit**

Set `pyproject.toml` and `src/iwiki_mcp/__init__.py` to `0.7.84`, run `uv lock`, then run the mandatory per-task verification gate.

```bash
git add pyproject.toml src/iwiki_mcp/__init__.py uv.lock src/iwiki_mcp/codegraph/store.py src/iwiki_mcp/codegraph/indexer.py src/iwiki_mcp/codegraph/runtime.py tests/codegraph/test_recovery_concurrency.py tests/codegraph/conftest.py
git commit -m "test(codegraph): prove recovery and concurrency"
```

## Benchmark remediation — reopened Unit B search ownership

### Task 7R (historical): Execute exact search ranks sequentially with safe public-limit early-stop

**Closes reopened evidence:** R-018/AC-18 query execution and the search-latency owner for R-028/AC-28. This is a forward fix after completed Task 12; it changes no schema, index, identity, normalization, public result, or MCP contract.

**Files:**
- Modify: `src/iwiki_mcp/codegraph/query.py`
- Modify: `tests/codegraph/test_query.py`
- Modify: `tests/test_package.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`

- [ ] **Step 1: Add failing sequential-rank and equivalence tests**

```python
def _executed_ranks(connection, request):
    statements = []
    connection.set_trace_callback(statements.append)
    try:
        results = CodeGraphQuery("backend").search(connection, request)
    finally:
        connection.set_trace_callback(None)
    ranks = [
        name
        for statement in statements
        for name in MATCH_RANK
        if f"iwiki-rank:{name}" in statement
    ]
    return results, ranks


def test_exact_rank_fills_public_limit_and_stops(schema_v2_search_connection):
    results, ranks = _executed_ranks(
        schema_v2_search_connection,
        validate_search_request("needle", kinds=["file"], limit=1),
    )
    assert [item.match for item in results] == ["qualified_exact"]
    assert ranks == ["qualified_exact"]


def test_no_hit_executes_each_rank_once(schema_v2_search_connection):
    results, ranks = _executed_ranks(
        schema_v2_search_connection,
        validate_search_request("definitely absent", limit=20),
    )
    assert results == ()
    assert ranks == list(MATCH_RANK)


EXPECTED_NEEDLE_RESULTS = [
    ("file:typed:qualified-file", "qualified_exact"),
    ("module:typed:local-module", "local_exact"),
    ("symbol:typed:alias-exact-target", "alias_exact"),
    ("symbol:typed:canonical-prefix", "canonical_prefix"),
    ("symbol:typed:alias-prefix-target", "alias_prefix"),
    ("symbol:typed:canonical-lexical", "canonical_lexical"),
    ("module:typed:alias-lexical-target", "alias_lexical"),
    ("symbol:typed:signature", "signature"),
    ("file:typed:path", "path"),
]


def test_sequential_result_equals_complete_expected_order(
    schema_v2_search_connection,
):
    query = CodeGraphQuery("backend")
    complete = query.search(
        schema_v2_search_connection,
        validate_search_request("needle", limit=20),
    )
    limited = query.search(
        schema_v2_search_connection,
        validate_search_request("needle", limit=7),
    )
    assert [(item.entity_id, item.match) for item in complete] == (
        EXPECTED_NEEDLE_RESULTS
    )
    assert limited == complete[:7]


def test_only_public_remaining_limit_is_bound(schema_v2_search_connection):
    statements = []
    schema_v2_search_connection.set_trace_callback(statements.append)
    try:
        CodeGraphQuery("backend").search(
            schema_v2_search_connection,
            validate_search_request("svc", limit=1),
        )
    finally:
        schema_v2_search_connection.set_trace_callback(None)
    assert all(
        "LIMIT 1" in sql
        for sql in statements
        if "iwiki-rank:" in sql
    )
```

Replace the old assertion that forbids every SQL `LIMIT` with `test_only_public_remaining_limit_is_bound`; it permits only the current remaining public limit. Keep existing typed-union, six-kind, alias aggregation/fan-out, partially-resolved alias, Unicode, literal `%`/`_`, path-filter, lazy-database-error, validation-precedence, and strongest-tier overlap tests. Add one overlap fixture per adjacent canonical tier and alias tier so every lower predicate proves exclusion of stronger matches.

- [ ] **Step 2: Run the focused RED**

```bash
uv run pytest -q tests/codegraph/test_query.py -k 'fills_public_limit_and_stops or no_hit_executes_each_rank_once or sequential_result_equals_complete_expected_order or only_public_remaining_limit_is_bound'
```

Expected: focused tests FAIL because current search emits one all-rank CTE statement. The task-page and approved spec retain the prior approximately one-second benchmark-stop evidence; Task 13 produces fresh JSON/Markdown after its runner exists.

- [ ] **Step 3: Replace the all-rank CTE with nine exclusive query specifications**

```python
@dataclass(frozen=True)
class _RankQuery:
    name: str
    sql: str
    parameters: tuple[object, ...]


_RANK_ORDER = tuple(MATCH_RANK)


def _remaining_limit(request, results):
    return request.limit - len(results)


def _search_result(row, rank_name):
    matched_alias = None if row[17] is None else str(row[17])
    alias_target_count = int(row[18])
    return SearchResult(
        entity_id=str(row[0]),
        entity_type=str(row[1]),
        file_id=None if row[2] is None else str(row[2]),
        module_id=None if row[3] is None else str(row[3]),
        symbol_id=None if row[4] is None else str(row[4]),
        kind=str(row[5]),
        qualified_name=str(row[6]),
        local_name=str(row[7]),
        signature=None if row[9] is None else str(row[9]),
        path=str(row[11]),
        start_line=int(row[13]),
        end_line=int(row[14]),
        start_byte=int(row[15]),
        end_byte=int(row[16]),
        match=rank_name,
        matched_alias=matched_alias,
        alias_ambiguous=alias_target_count > 1,
        alias_target_count=alias_target_count,
    )


def _append_rank(rows, rank_name, results, seen_ids):
    for row in rows:
        item = _search_result(row, rank_name)
        if item.entity_id in seen_ids:
            continue
        results.append(item)
        seen_ids.add(item.entity_id)
```

Build `_rank_queries(domain, request)` in `_RANK_ORDER`. Every query begins with comment `/* iwiki-rank:<name> */`, constructs only required file/module/symbol branches, applies language/kind/path filters inside those branches, and ends with `ORDER BY qualified_name, entity_id LIMIT ?`. Bind `remaining_limit` only after the tier has completed all semantics.

Exact and prefix SQL use existing qualified/local/path indexes where applicable. Lexical SQL requires every U+001F-bounded query token via `instr`; signature/path SQL uses persisted casefold expressions. Alias SQL starts from filtered explicit-alias `IMPORTS` rows in `resolved`, `ambiguous`, or `partially_resolved`, calculates distinct filtered target count before ordering/limit, and chooses the lowest code-point alias. Each tier predicate includes the negation of every stronger canonical/alias condition. Do not add an index, projection, FTS, UDF, result cache, temporary persisted structure, or non-public candidate bound.

```python
results = []
seen_ids = set()
try:
    for rank in _rank_queries(self.domain, request):
        remaining = _remaining_limit(request, results)
        if remaining == 0:
            break
        rows = connection.execute(
            rank.sql, (*rank.parameters, remaining)
        )
        _append_rank(rows, rank.name, results, seen_ids)
except sqlite3.DatabaseError as exc:
    raise CodeGraphStoreError("code graph search failed") from exc
return tuple(results)
```

- [ ] **Step 4: Run focused GREEN, query-plan inspection, and full gates**

```bash
uv run pytest -q tests/codegraph/test_query.py
uv run pytest -q tests/codegraph/test_server_tools.py tests/codegraph/test_indexer_runtime.py
uv run pytest -q
uv run flake8 src tests
uv run python -m compileall -q src/iwiki_mcp/codegraph tests/codegraph
uv lock --check
git diff --check
```

Expected: sequential/expected-order/overlap tests pass; exact/prefix `EXPLAIN QUERY PLAN` assertions name only existing Section 7.8 indexes; early-stop trace contains no lower tier after the public limit fills; no-hit trace contains all nine ranks once; full suite and static gates exit zero.

Update `concept/code-graph-search`, headings `Candidate retrieval` and `Ranking and result contract`, source `src/iwiki_mcp/codegraph/query.py`. Run `wiki_lint(domain="iwiki-mcp")`; the changed page must be current with no broken or missing-source finding.

- [ ] **Step 5: Bump version and commit**

Set package and package-test versions to `0.7.86`, run `uv lock`, rerun `tests/test_package.py`, and commit only Task 7R files:

```bash
git add pyproject.toml src/iwiki_mcp/__init__.py uv.lock src/iwiki_mcp/codegraph/query.py tests/codegraph/test_query.py tests/test_package.py
git commit -m "fix(codegraph): execute search ranks sequentially"
```

### Task 7R-P: Push every rank predicate into only the required typed branches

**Closes reopened evidence:** the missing Task 7R Step 3 branch-local execution and Step 4 query-plan proof. Preserve commit `b8b8f16`; this is a forward fix resumed from approved threshold-only plan version `0.7.90`. Steps 1–3 below are preserved evidence and MUST NOT be replayed.

**Files:**
- Modify: `src/iwiki_mcp/codegraph/query.py`
- Modify: `tests/codegraph/test_query.py`
- Modify: `tests/test_package.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`

- [ ] **Step 1: Add failing branch-selection, predicate-placement, and EXPLAIN tests**

Add explicit entity-branch comments to generated SQL so tests can inspect the query contract without depending on whitespace:

```python
def _rank_sql(request, name):
    sql, parameters = _rank_query("backend", request, name, ())
    return sql, (*parameters, request.limit)


@pytest.mark.parametrize(
    ("kind", "present", "absent"),
    [
        ("file", "iwiki-entity:file", ("iwiki-entity:module", "iwiki-entity:symbol")),
        ("module", "iwiki-entity:module", ("iwiki-entity:file", "iwiki-entity:symbol")),
        ("method", "iwiki-entity:symbol", ("iwiki-entity:file", "iwiki-entity:module")),
    ],
)
def test_rank_sql_emits_only_requested_entity_branches(kind, present, absent):
    sql, _parameters = _rank_sql(
        validate_search_request("needle", kinds=[kind]),
        "canonical_lexical",
    )
    assert present in sql
    assert all(marker not in sql for marker in absent)


def test_rank_predicates_and_filters_are_inside_each_union_branch():
    request = validate_search_request(
        "needle", kinds=["file", "module", "method"], path="src/pkg"
    )
    sql, _parameters = _rank_sql(request, "canonical_lexical")
    branches, outer = sql.split("/* iwiki-after-branches */", 1)
    assert branches.count("f.repository_id = ?") == 3
    assert branches.count("f.language = ?") == 3
    assert branches.count("substr(f.path, 1, length(?)) = ?") == 3
    assert branches.count("name_tokens_casefold") >= 3
    assert "repository_id" not in outer
    assert "language" not in outer
    assert "kind IN" not in outer
    assert "path" not in outer
    assert "name_tokens_casefold" not in outer


def _query_plan(connection, request, name):
    sql, parameters = _rank_sql(request, name)
    return "\n".join(
        str(row[3])
        for row in connection.execute(
            "EXPLAIN QUERY PLAN " + sql, parameters
        )
    )


@pytest.mark.parametrize(
    ("kind", "rank", "expected_index"),
    [
        ("file", "qualified_exact", "idx_files_repository_path"),
        ("file", "local_exact", "idx_files_repository_local"),
        ("module", "qualified_exact", "idx_files_repository_module_qualified"),
        ("module", "local_exact", "idx_files_repository_module_local"),
        ("method", "qualified_exact", "idx_symbols_qualified"),
        ("method", "local_exact", "idx_symbols_local"),
    ],
)
def test_exact_rank_query_plan_uses_existing_endpoint_index(
    schema_v2_search_connection, kind, rank, expected_index
):
    plan = _query_plan(
        schema_v2_search_connection,
        validate_search_request("needle", kinds=[kind]),
        rank,
    )
    assert expected_index in plan
```

Keep the existing sequential trace, complete-order, adjacent-tier overlap, alias fan-out/count, partially-resolved alias, Unicode, literal wildcard, lazy cursor error, and public-limit-only tests. Add a regression that generated SQL contains no numeric or parameterized `LIMIT` except the one final public remaining-limit placeholder.

- [ ] **Step 2: Run the focused RED and capture the current plan**

```bash
uv run pytest -q tests/codegraph/test_query.py -k 'only_requested_entity_branches or predicates_and_filters_are_inside or query_plan_uses_existing_endpoint_index'
```

Expected: branch-selection and predicate-placement tests FAIL because `b8b8f16` always emits the broad file/module/symbol CTE and applies kind/path/rank predicates outside it; exact symbol EXPLAIN may use a file join plan instead of the endpoint index.

- [ ] **Step 3: Generate direct branch-local SQL without new storage or hidden bounds**

Build each canonical rank from zero to three direct `SELECT` branches selected from `request.kinds`: file kinds read only `files`; module reads only non-null module facets in `files`; symbol kinds read `symbols JOIN files`. Put repository, language, kind, target-path, exact/prefix/lexical/signature/path predicate, stronger-rank negation, and returned-entity exclusion inside each selected branch before `UNION ALL`. Mark branches `/* iwiki-entity:file */`, `/* iwiki-entity:module */`, or `/* iwiki-entity:symbol */`, and put `/* iwiki-after-branches */` immediately before the single final deterministic `ORDER BY qualified_name, entity_id LIMIT ?`.

For qualified/local exact, structure direct branches so Section 7.8 endpoint indexes are visible to SQLite. For canonical prefix, split qualified and local name endpoints inside each entity branch and use deterministic binary prefix bounds when an upper bound exists; de-duplicate the same entity before the final public limit. Lexical, signature, and path remain persisted-field scans but execute only for relevant entity kinds and with branch-local filters. Alias ranks continue to start from filtered explicit-alias `IMPORTS`, but push target kind/path/language and rank predicates into their module/symbol target branches before aggregation. Preserve distinct filtered target counts and the lowest-code-point public alias.

Do not change schema, add an index/table/projection/FTS/UDF/cache, add a candidate bound, normalize with NFC/NFKC, or move validation into the timed query.

- [ ] **Step 3A: Implement the threshold-only benchmark seam before rerunning Task 7R-P evidence**

Task 7R-P owns the next authoritative benchmark run, but the stopped Task 13 scaffold owns the benchmark threshold and report schema. Make this one bounded interleaving before Task 7R-P Step 4; keep all Task 13 files untracked and unstaged until Task 13 resumes.

Extend the runner-test imports with `DEFAULT_THRESHOLDS`, `SEARCH_POST_V1_TARGET_MS`, `_OPERATORS`, and `_meets_search_post_v1_target`. Add tests first in `tests/eval/test_code_graph_runner.py` and `tests/eval/test_code_graph_report.py`:

```python
def test_search_release_gate_and_post_v1_target_are_distinct():
    assert DEFAULT_THRESHOLDS["search_ms"] == 500.0
    assert SEARCH_POST_V1_TARGET_MS == 150.0
    operator, comparator = _OPERATORS["search_ms"]
    assert operator == "<"
    assert comparator(499.999, DEFAULT_THRESHOLDS["search_ms"]) is True
    assert comparator(500.0, DEFAULT_THRESHOLDS["search_ms"]) is False


def test_search_case_reports_nonblocking_post_v1_target():
    assert _meets_search_post_v1_target(149.999) is True
    assert _meets_search_post_v1_target(150.0) is False


def test_markdown_distinguishes_release_gate_from_post_v1_target():
    markdown = render_markdown(_report())
    assert "Release gate: <500 ms" in markdown
    assert "Post-v1 target: <150 ms (non-blocking)" in markdown
```

Add `"meets_post_v1_target": False` to the report test's existing `246.0 ms` search-case fixture so its expected evidence is explicit rather than inferred by the renderer.

Adapt fixture/helper names to the existing scaffold without weakening assertions. Run RED:

```bash
uv run pytest -q tests/eval/test_code_graph_runner.py tests/eval/test_code_graph_report.py -k 'release_gate_and_post_v1 or reports_nonblocking_post_v1 or distinguishes_release_gate'
```

Expected: tests FAIL because `DEFAULT_THRESHOLDS["search_ms"]` is `150.0`, cases have no post-v1 comparison, and Markdown does not distinguish the two numbers.

Then make only these behavior changes in `eval/code_graph/runner.py` and `eval/code_graph/report.py`:

```python
SEARCH_POST_V1_TARGET_MS = 150.0

DEFAULT_THRESHOLDS = {
    # Existing unchanged gates omitted.
    "search_ms": 500.0,
}

def _meets_search_post_v1_target(warm_max_ms: float) -> bool:
    return warm_max_ms < SEARCH_POST_V1_TARGET_MS


case["meets_post_v1_target"] = _meets_search_post_v1_target(
    case["warm_max_ms"]
)
```

Keep the release comparator strict `<`; exactly `500.0 ms` fails. Preserve one cold sample, one untimed warm-up, exactly ten warm samples, maximum aggregation, corpus, environment, all ranks/IDs, and every non-search gate. JSON and Markdown must expose both thresholds; only the `<500 ms` gate contributes to overall `passed`. Run the focused tests again and require GREEN. Do not version, stage, commit, update Wiki, or claim Task 13 complete at this seam.

- [ ] **Step 4: Run unit/integration gates and the threshold-only authoritative benchmark**

```bash
uv run pytest -q tests/codegraph/test_query.py
uv run pytest -q tests/codegraph/test_server_tools.py tests/codegraph/test_indexer_runtime.py
uv run pytest -q --ignore=tests/eval/test_code_graph_runner.py --ignore=tests/eval/test_code_graph_report.py
uv run flake8 src tests/codegraph tests/test_package.py
uv run python -m compileall -q src/iwiki_mcp/codegraph tests/codegraph
uv lock --check
git diff --check
uv run python -m eval.code_graph --fixture-root tests/fixtures/codegraph --output /tmp/iwiki-code-graph-evidence
```

Expected: unit and static gates exit zero; exact/local EXPLAIN plans name only existing Section 7.8 indexes; requested kinds emit no unused typed branch; early-stop and public-limit-only assertions remain green. The benchmark reports the same independent expected ranks/IDs, one cold plus one warm-up plus ten warm samples, and every search-case warm maximum `<500 ms`. Each case also reports whether it meets the non-blocking `<150 ms` post-v1 target. All non-search gates remain green. The CLI must exit `0` before Task 13 resumes.

If the benchmark misses `<500 ms` or any unchanged gate, preserve both evidence files and stop before version/commit/Task 13. Do not change the threshold, query strategy, corpus, sampling, or another contract without a new checked human decision. A case that misses only `<150 ms` is reported as post-v1 debt and does not stop first-release execution.

Update `concept/code-graph-search`, headings `Candidate retrieval` and `Ranking and result contract`, source `src/iwiki_mcp/codegraph/query.py`; run `wiki_lint(domain="iwiki-mcp")` and require no new broken, missing-source, or stale finding for the changed page.

- [ ] **Step 5: Bump version and commit only after the benchmark passes**

Set package and package-test versions to `0.7.91`, run `uv lock`, rerun `tests/test_package.py`, and commit only Task 7R-P files. Do not stage the Task 13 scaffold/tests whose threshold seam remains untracked for Task 13:

```bash
git add pyproject.toml src/iwiki_mcp/__init__.py uv.lock src/iwiki_mcp/codegraph/query.py tests/codegraph/test_query.py tests/test_package.py
git commit -m "fix(codegraph): push search predicates into typed branches"
```

## Unit C — resumed benchmark and final evidence

### Task 13: Correct quality evidence and split search from production benchmark gates

**Closes:** R-027 and R-028; produces AC-27 and AC-28.

**Resume state:** Steps 1–4 below are preserved untracked implementation/evidence from the stopped Task 13 run, including the split corpora, real production path, semantic determinism, corrected truth, and ten-sample policy. Task 7R-P Step 3A adds only the approved threshold/report seam. After Task 7R-P commits, Task 13 MUST NOT replay the old failed design; it resumes at Step 5, runs the focused and authoritative gates, writes method/Wiki evidence, then performs Step 6.

**Files:**
- Create: `eval/code_graph/__init__.py`
- Create: `eval/code_graph/__main__.py`
- Create: `eval/code_graph/runner.py`
- Create: `eval/code_graph/report.py`
- Create: `tests/eval/test_code_graph_runner.py`
- Create: `tests/eval/test_code_graph_report.py`
- Create: `docs/superpowers/evidence/code-graph-benchmark-method.md`
- Modify: `tests/test_package.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`

- [ ] **Step 1: Add failing split-corpus and truth tests**

```python
from eval.code_graph.runner import (
    BenchmarkGateError,
    _create_search_corpus,
    _write_production_tree,
    run_benchmark,
)


@pytest.fixture(scope="module")
def benchmark_report(tmp_path_factory):
    return run_benchmark(
        output=tmp_path_factory.mktemp("code-graph-benchmark"),
        fixture_root="tests/fixtures/codegraph",
    )


def test_report_separates_search_and_production_corpora(benchmark_report):
    corpora = benchmark_report["corpora"]
    assert corpora["search"]["entity_count"] >= 100_000
    assert corpora["production"]["accepted_source_bytes"] > 0
    assert corpora["search"]["sha256"] != corpora["production"]["sha256"]
    assert {"ascii_name", "unicode_name", "unicode_signature", "shared_unicode_path"} <= set(benchmark_report["strata"])


def test_corpus_builders_keep_search_and_source_bytes_separate(tmp_path):
    search = _create_search_corpus(tmp_path / "search.sqlite3")
    try:
        production = _write_production_tree(tmp_path / "project", 3)
        assert search["entity_count"] >= 100_000
        assert "source_bytes" not in search
        assert production.accepted_source_bytes == sum(
            (production.project / path).stat().st_size
            for path in production.accepted_paths
        )
        assert production.sha256 != search["sha256"]
    finally:
        search["connection"].close()


def test_search_records_cold_warmup_and_ten_warm_samples(benchmark_report):
    for case in benchmark_report["performance"]["search_cases"]:
        assert case["cold_ms"] >= 0
        assert case["warmup_runs"] == 1
        assert len(case["warm_samples_ms"]) == 10
        assert case["warm_max_ms"] == max(case["warm_samples_ms"])
        assert case["warm_max_ms"] < 500
        assert case["meets_post_v1_target"] is (
            case["warm_max_ms"] < 150
        )


def test_golden_truth_is_independent_and_casefold_only(benchmark_report):
    truth = benchmark_report["golden_truth"]
    assert truth["unicode_token_key"] == "\x1fpkg\x1fstrasse\x1f"
    assert truth["normalization"] == "none"
    assert truth["canonical_lexical_query"] == "some token target"
    assert benchmark_report["quality"]["unicode_correctness"] == 1.0


def test_determinism_excludes_only_operational_fields(benchmark_report):
    determinism = benchmark_report["determinism"]
    assert determinism["revision_equal"] is True
    assert determinism["semantic_row_hash_equal"] is True
    assert determinism["entity_relation_link_ids_equal"] is True
    assert determinism["excluded_fields"] == [
        "repositories.indexed_at",
        "repositories.state",
        "metadata.phase_timings",
        "metadata.transient_diagnostics",
    ]


def test_threshold_miss_writes_evidence_and_exits_nonzero(tmp_path):
    with pytest.raises(BenchmarkGateError):
        run_benchmark(
            output=tmp_path,
            fixture_root="tests/fixtures/codegraph",
            thresholds={"declarations": 1.01},
        )
    assert (tmp_path / "code-graph-benchmark.json").is_file()
    assert (tmp_path / "code-graph-benchmark.md").is_file()
```

- [ ] **Step 2: Run RED against the preserved failed scaffold and evidence**

```bash
uv run pytest -q tests/eval/test_code_graph_runner.py tests/eval/test_code_graph_report.py
```

Expected: on a clean checkout, collection FAILS because `eval.code_graph` is absent. When the stopped local scaffold is still present, tests instead FAIL on its single mixed corpus, three warm samples, raw-row hash, Unicode expected token, and lexical fixture. Either RED is valid; no Task 7R step depends on these uncommitted files.

- [ ] **Step 3: Build two deterministic corpora with separate ownership**

Implement `_create_search_corpus()` as deterministic SQL-only schema-v2 setup. Canonical lexical data persists `models.token_key("lexical.some_token_target", "some_token_target")`; hard-code expected query/rank separately. Include fixed ASCII, Unicode name, Unicode signature, shared Unicode path, duplicate module, repeated alias, and ambiguous alias strata. Return no `source_bytes` from this corpus and never use its file size for storage gates.

```python
def _hash_corpus(parts):
    digest = hashlib.sha256()
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


@dataclass(frozen=True)
class ProductionCorpus:
    project: Path
    accepted_paths: tuple[str, ...]
    accepted_source_bytes: int
    sha256: str


def _write_production_tree(project: Path, count: int) -> ProductionCorpus:
    project.mkdir(parents=True, exist_ok=True)
    parts = []
    paths = []
    total = 0
    for index in range(count):
        relative = f"pkg_{index:05d}.py"
        source = (
            f'"""benchmark source {index:05d} ' + ("x" * 2048) + '\n"""\n'
            f"def symbol_{index:05d}(value: int) -> int:\n"
            f"    return value + {index}\n"
        ).encode("ascii")
        target = project / relative
        target.write_bytes(source)
        paths.append(relative)
        total += len(source)
        parts.append(relative.encode("utf-8") + b"\0" + source)
    return ProductionCorpus(
        project=project,
        accepted_paths=tuple(paths),
        accepted_source_bytes=total,
        sha256=_hash_corpus(parts),
    )
```

Use this real tree through production discovery, `PythonAdapter`, resolver, `CodeGraphIndexer`, store, and publication. Use 1,000 files for build/no-op/forced rebuild/context/DB ratio and 10,000 files through the same discovery/parser/resolver path under `tracemalloc` for memory. Do not monkeypatch `discover_sources` or count invented source sizes.

```bash
uv run pytest -q tests/eval/test_code_graph_runner.py -k 'corpus_builders_keep_search_and_source_bytes_separate'
```

Expected: deterministic search corpus has at least 100,000 entities and no source denominator; production accepted bytes equal actual generated files; corpus identities differ.

- [ ] **Step 4: Implement canonical measurement and report semantics**

```python
_SEMANTIC_SELECTS = {
    "repositories": (
        "repository_id, root_path, git_remote, git_commit, "
        "source_fingerprint, config_fingerprint, parser_fingerprint, "
        "normalizer_version, unicode_data_version, revision"
    ),
    "files": "*",
    "symbols": "*",
    "relations": "*",
    "wiki_code_links": "*",
}


def _semantic_snapshot(connection):
    rows = {}
    for table, columns in _SEMANTIC_SELECTS.items():
        rows[table] = tuple(
            connection.execute(
                f"SELECT {columns} FROM {table} ORDER BY 1"
            )
        )
    return rows
```

Compare both revisions, `_semantic_snapshot()` hashes, and exact file/module/symbol/relation/link ID sets after two forced production builds. Exclude only the four documented operational groups; do not compare raw DB bytes.

Before measuring storage, open the canonical production DB under benchmark ownership, run `PRAGMA wal_checkpoint(TRUNCATE)`, close every connection, assert WAL is absent or zero bytes, and divide the main SQLite file bytes by `ProductionCorpus.accepted_source_bytes`. Measure cold startup inside a fresh Python subprocess with the timer around the code-graph import, excluding interpreter launch.

For each rank, create and validate the request before timing, take one cold production-query sample, execute one untimed warm-up, then take exactly ten warm samples through the same `CodeGraphQuery`, connection, and snapshot. Record cold, warm-up count, median, p95, maximum, all ten samples, observed rank, result IDs, and independent expected rank/IDs. Gate only `warm_max_ms`. Do not add a prepared-result or Python result cache.

```python
def _timed_search(engine, connection, request):
    started = time.perf_counter_ns()
    matched = engine.search(connection, request)
    elapsed = (time.perf_counter_ns() - started) / 1_000_000
    return round(elapsed, 6), matched


def _nearest_rank_p95(values):
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * 0.95) - 1]


cold_ms, cold_matches = _timed_search(engine, connection, request)
warmup_matches = engine.search(connection, request)
warm_runs = [
    _timed_search(engine, connection, request)
    for _sample in range(10)
]
warm_samples = [elapsed for elapsed, _matched in warm_runs]
case = {
    "cold_ms": cold_ms,
    "warmup_runs": 1,
    "warm_samples_ms": warm_samples,
    "warm_median_ms": statistics.median(warm_samples),
    "warm_p95_ms": _nearest_rank_p95(warm_samples),
    "warm_max_ms": max(warm_samples),
    "meets_post_v1_target": max(warm_samples) < 150.0,
}
```

Render `corpora.search`, `corpora.production`, `golden_truth`, revised determinism, the blocking `<500 ms` release gate, the non-blocking `<150 ms` post-v1 target, and per-case warm distribution in both JSON and Markdown. Reports must be written before raising `BenchmarkGateError`. Exactly `500.0 ms` fails the strict release comparator; exactly `150.0 ms` misses only the post-v1 target.

```bash
uv run pytest -q tests/eval/test_code_graph_runner.py tests/eval/test_code_graph_report.py -k 'search_records_cold or determinism_excludes or threshold_miss or markdown'
```

Expected: ten-sample search distribution, semantic determinism, split-corpus Markdown, and write-before-fail evidence pass.

- [ ] **Step 5: Run focused GREEN and the actual threshold-only benchmark gates**

```bash
uv run pytest -q tests/eval/test_code_graph_runner.py tests/eval/test_code_graph_report.py
uv run python -m eval.code_graph --fixture-root tests/fixtures/codegraph --output /tmp/iwiki-code-graph-evidence
```

Expected: CLI exits `0`. Declarations/methods `>=98%`, local imports `>=95%`, static calls `>=75%`, false resolved calls `<5%`, deterministic rebuild `100%`; startup `<100 ms`, no-op `<200 ms`, 1,000-file production build `<15 s`, every search case warm maximum `<500 ms`, production context `<300 ms`, checkpointed production DB/source `<3x`, and 10,000-file production-path memory `<1 GiB` on the recorded environment. Every search case reports correct independent expected rank/result, including canonical lexical and Unicode, plus its non-blocking `<150 ms` post-v1-target result.

```bash
uv run pytest -q
uv run flake8 src tests eval/code_graph
uv run python -m compileall -q src eval tests/eval tests/codegraph
uv lock --check
git diff --check
```

Expected: full tests and static gates exit zero. The intentional syntax-error fixture is outside the explicit compile paths.

Create `reference/code-graph-benchmark` with `wiki_write_page`, document corpus, environment, warm/cold policy, quality/performance gates, report locations, and stop behavior with source `eval/code_graph/runner.py`; link it from `reference/code-graph-schema-v2-design`, then run iwiki lint. Expected: the benchmark page is not orphan/stale and adds no broken/missing-source finding.

**Benchmark-miss stop rule:** Any blocking threshold miss or contradictory result stops execution before Task 14. Write the failed evidence without suppressing it, reopen the earliest affected spec/plan section and HUMAN CHECKPOINT, rerun the required chain checks, obtain approval of revised checked artifacts, then return to the owning implementation task. A miss of only the reported `<150 ms` post-v1 target does not fail or stop the first release. Never change the approved `<500 ms` release gate, truncate candidates, add FTS/UDF/projection/candidate caps, or continue on another blocking miss without a new checked human decision.

- [ ] **Step 6: Bump version and commit only after the actual gate passes**

Set package and package-test versions to `0.7.92`, run `uv lock`, rerun `tests/test_package.py`, and commit the benchmark only after the real CLI exits zero.

```bash
git add pyproject.toml src/iwiki_mcp/__init__.py uv.lock tests/test_package.py eval/code_graph tests/eval/test_code_graph_runner.py tests/eval/test_code_graph_report.py docs/superpowers/evidence/code-graph-benchmark-method.md
git commit -m "test(codegraph): gate quality and performance"
```

### Task 14: Document the Python MVP, preserve separate debt, and run final gates

**Closes:** R-026 and R-030; produces AC-26 and AC-30 plus final reconciliation evidence. The checked plan and the post-Task-7 remediation gate own R-029/AC-29; this task verifies and records that structural evidence without moving ownership.

**Files:**
- Modify: `README.md`
- Modify: `docs/README.ru.md`
- Modify: `docs/architecture.md`
- Modify: `docs/TODO.md`
- Modify: `docs/superpowers/plans/2026-08-09-iwiki-mcp-code-graph.md` frontmatter through `$check-chain result`
- Modify: `tests/test_package.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`
- Update through iwiki MCP: `architecture`, `mcp-server`, `installation`, `reference/code-graph-technical-debt`

- [ ] **Step 1: Add failing documentation-contract tests**

```python
import dataclasses
from pathlib import Path

from iwiki_mcp.codegraph.config import CodeGraphConfig


def test_user_docs_name_exact_code_tools_and_separate_debt():
    text = Path("README.md").read_text(encoding="utf-8")
    assert all(name in text for name in ("wiki_code_status", "wiki_code_index", "wiki_code_search", "wiki_code_context"))
    assert "Incremental indexing is not part of the Python MVP" in text
    assert "TypeScript is not part of the Python MVP" in text
    assert "incremental" not in {field.name for field in dataclasses.fields(CodeGraphConfig)}
    assert not Path("src/iwiki_mcp/codegraph/languages/typescript.py").exists()
```

- [ ] **Step 2: Run RED**

```bash
uv run pytest -q tests/test_package.py -k 'user_docs_name_exact_code_tools'
```

Expected: one selected test is collected and FAILS on missing schema-v2 user/debt text; an empty selection is not a valid RED result.

- [ ] **Step 3: Update product and architecture docs without extra claims**

Document exact configuration, derived paths, typed search/context contracts, four tools, no-startup-build rule, schema-v1 rebuild, source safety, recovery, benchmark command, and Python-only scope in English/Russian docs. State that incremental indexing and TypeScript require separate specifications and deliveries. Do not claim impact analysis, hybrid retrieval, module/alias selectors, automatic authoritative links, or deferred features exist.

Use iwiki MCP to update the four named pages with their changed source paths; the technical-debt page must keep incremental indexing and TypeScript as two distinct items. Run `wiki_lint(domain="iwiki-mcp")`; no code-graph page may be stale/broken/missing-source, graph parity must be ready, and unrelated pre-existing advisories must be reported rather than silently edited.

- [ ] **Step 4: Run complete final verification**

```bash
uv run pytest -q
uv run flake8 src tests eval/code_graph
uv run python -m compileall -q src eval tests/eval tests/codegraph
uv run iwiki-mcp --help
uv run python -m eval.code_graph --fixture-root tests/fixtures/codegraph --output /tmp/iwiki-code-graph-evidence
git diff --check
```

Expected: full suite, scoped lint, compile, CLI, benchmark, and diff checks pass; benchmark JSON/Markdown contain all gates; exact four-tool stdio registry passes; `wiki_search` regressions are zero; iwiki lint meets the code-graph condition above.

- [ ] **Step 5: Bump version, reconcile result, and create the final commit**

Set `pyproject.toml`, `src/iwiki_mcp/__init__.py`, and `tests/test_package.py` to `0.7.93`, run `uv lock`, rerun `tests/test_package.py`, `uv lock --check`, and `git diff --check`, then reconcile the complete branch diff before committing:

```bash
uv run pytest -q tests/test_package.py
uv lock --check
git diff --check
```

Invoke in Codex, not in the shell:

```text
$check-chain result docs/superpowers/plans/2026-08-09-iwiki-mcp-code-graph.md --since=f64b93c
```

The user requested text/Markdown artifacts rather than an HTML report, so decline the optional result HTML unless the user changes that preference. After `result_check: OK` is written, commit the reconciled state:

```bash
git add pyproject.toml src/iwiki_mcp/__init__.py uv.lock README.md docs/README.ru.md docs/architecture.md docs/TODO.md docs/superpowers/plans/2026-08-09-iwiki-mcp-code-graph.md tests/test_package.py
git commit -m "docs(codegraph): document schema v2 Python MVP"
```

Expected: `$check-chain result` writes `result_check: OK` against the current plan hash and closes the single `iwiki-mcp-code-graph` TODO row before the final commit. It may be `OK` only when every plan task, acceptance criterion, docs/wiki update, benchmark gate, and final verification command has evidence. If result is `needs_work`, do not commit or close the row; return to the owning task and rerun the affected gate.

## Requirement and acceptance coverage

| Requirements | Acceptance | Owning evidence task |
|---|---|---|
| R-001, R-002, R-003, R-004 | AC-01, AC-02, AC-03, AC-04 | Task 1 contracts; Tasks 6/10 runtime/tool proof |
| R-005, R-006, R-007, R-008, R-009 | AC-05, AC-06, AC-07, AC-08, AC-09 | Task 2 primitives; Tasks 6/12 integration |
| R-010 | AC-10 | Task 6 |
| R-011, R-012 | AC-11, AC-12 | Tasks 1, 3–6 |
| R-013 | AC-13 | Task 4 |
| R-014, R-015 | AC-14, AC-15 | Tasks 4–5 |
| R-016 | AC-16 | Tasks 6–8 and 10 |
| R-017, R-018 | AC-17, AC-18 | Tasks 6–7, 7R, 10, 12 |
| R-019, R-020 | AC-19, AC-20 | Task 8 |
| R-021, R-022 | AC-21, AC-22 | Task 9 |
| R-023, R-024 | AC-23, AC-24 | Tasks 9–11 |
| R-025 | AC-25 | Tasks 6 and 12 |
| R-026 | AC-26 | Tasks 10–12 and 14 |
| R-027, R-028 | AC-27, AC-28 | Tasks 7R and 13 |
| R-029 | AC-29 | Checked plan structure, historical Schema-v2 Remediation Gate, and forward Task 7R → Task 13 → Task 14 order |
| R-030 | AC-30 | Task 14 |

## Final evidence inventory

Execution result must provide:

- Release history from original plan version `0.7.72` through completed Task 12 version `0.7.84`, benchmark-remediation plan `0.7.85`, Task 7R `0.7.86`, predicate-pushdown plan `0.7.87`, v1 latency intent `0.7.88`, threshold-only spec `0.7.89`, threshold-only plan `0.7.90`, Task 7R-P `0.7.91`, Task 13 `0.7.92`, and final Task 14 `0.7.93`.
- Task-level RED failure and GREEN success output for original Tasks 1–14 plus reopened Task 7R.
- Schema inspection proving exact five tables, exact twenty named indexes, required implicit uniques, and absence of forbidden modules/FTS/projection/UDF structures.
- Typed union and exact nine-rank search results, alias aggregation/fan-out, no hidden candidate cap, invalid-input-before-I/O traces, and stable tie evidence.
- File/module/symbol context-seed coverage, deterministic BFS/budget evidence, and explicit-source safety evidence.
- Full build/no-op, schema-v1 incompatible rebuild, corrupt-cache recovery, ordered two-verification publication, cancellation, concurrent reader/writer, and fail-soft Wiki-continuation evidence.
- Real stdio registration proving exactly four code tools with no `domain`, `seeds` on context, default `include_source=false`, and unchanged `wiki_search`.
- Selector round trips, file/symbol-only derived links, code-aware lint matrix, and proof that no path mutates authored selectors.
- Quality and performance reports with environment, separate search/production corpus identities, versions, every stratum, cold plus ten warm search samples, blocking `<500 ms` release-gate evidence, non-blocking `<150 ms` post-v1-target evidence, every unchanged non-search threshold, production DB/source bytes, independent golden truth, and canonical semantic deterministic rebuild comparison.
- Repository docs and iwiki pages current; separate incremental and TypeScript debt preserved; iwiki lint result recorded.
- Final full pytest, flake8, compileall, CLI, benchmark, diff-check, and `$check-chain result` outputs.

Only this evidence supports completion claims. The baseline commit, task commits, or passing schema-v1 tests are not substitutes for schema-v2 result evidence.
