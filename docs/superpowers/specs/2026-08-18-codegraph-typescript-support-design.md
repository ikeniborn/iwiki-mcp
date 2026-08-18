---
review:
  spec_hash: 48375472246e15ea
  last_run: 2026-08-18
  phases:
    structure:
      status: passed
    coverage:
      status: passed
    clarity:
      status: passed
    consistency:
      status: passed
  findings:
    - id: F-001
      phase: clarity
      severity: WARNING
      section: "2.6 `query.py` changes"
      section_hash: c825a422c1a7b495
      fragment: "the project's configured code_graph.languages (plumbed through from the runtime call site, not the global KNOWN_LANGUAGES registry"
      text: "Default languages source was stated ambiguously — both 'all of KNOWN_LANGUAGES' and 'configured code_graph.languages' were named as the default."
      fix: "Resolved: default is config.languages (the project's actually-enabled subset), not the global registry."
      verdict: fixed
      verdict_at: 2026-08-18
chain:
  intent: docs/superpowers/intents/2026-08-18-codegraph-typescript-support-intent.md
---
# Design: codegraph-typescript-support

**Date:** 2026-08-18
**Status:** approved
**Intent:** [docs/superpowers/intents/2026-08-18-codegraph-typescript-support-intent.md](../intents/2026-08-18-codegraph-typescript-support-intent.md)

## Acceptance (from intent)

Desired Outcomes (verbatim from the approved intent):
- `wiki_code_index` indexes `.ts`/`.tsx` files alongside `.py` in the same run.
- `wiki_code_search` / `wiki_code_context` return correct TypeScript symbols and
  relations (imports, calls, class/interface members, type references).
- A mixed Python + TypeScript repository builds a working graph — single vs.
  separate-but-coexisting snapshots was an open design question; **resolved
  below as a single unified graph** (Approach A, see §0).

Done when: `wiki_code_index` indexes a real TypeScript/TSX fixture repo producing
schema-v2 records; `wiki_code_search`/`wiki_code_context` return correct results
against that fixture; a mixed Python+TypeScript repo indexes and queries
successfully; the Python-only benchmark and fixture suite shows zero regression.

## 0. Decision: single unified graph (Approach A)

`files`/`symbols`/`relations` already carry a per-entity `language` column
(`query.py` filters `f.language = ?`), and the identity scheme already hashes
`language` into every ID (`concept/code-graph-identities`). `SnapshotHeader.languages`
is already a `tuple`. The indexer (`indexer.py::CodeGraphIndexer`) is already
language-registry-driven: `adapter_factories: Mapping[str, AdapterFactory]`,
`_factories`/`_extensions`/`_parse`/`_resolve` all loop over `config.languages`
generically — `indexer.build_rows` already sets
`languages=tuple(config.languages)` correctly.

Rejected alternative — separate per-language snapshots
(`domain#python`, `domain#typescript`): duplicates build/publish orchestration,
breaks "one graph per project," forces the agent to know which snapshot to
query. Rejected: the schema was already built for per-entity `language`
tagging; B fights the existing design for no benefit.

**Consequence:** most of the pipeline needs zero change. The real gap is four
literal `("python",)`/`!= "python"` gates that were never updated to read the
registry, plus one single-language assumption in the search request shape.

## 1. Architecture

1. **New adapter** — `languages/typescript.py::TypeScriptAdapter`, registered
   in the composition root (`server.py::_code_graph_adapter_factories`)
   alongside `"python"`. No change to `LanguageAdapter`, `AdapterFactory`,
   `indexer.py`, or `discovery.py` — they are already generic.
2. **Lift four hardcoded python-only gates** to registry/config-driven checks:
   - `config.py::_languages()` — validate against `KNOWN_LANGUAGES = {"python", "typescript"}`.
   - `runtime.py::snapshot()` (~L724) — `SnapshotHeader.languages=("python",)`
     is a **latent bug**: it already disagrees with `indexer.build_rows`
     (which correctly uses `tuple(config.languages)`). Fix: derive the header
     from the distinct `language` values actually present in the stored
     `files` rows — self-describing from the snapshot, not from live config,
     so a repo indexed python-only historically never silently claims
     TypeScript coverage it doesn't have.
   - `runtime.py::index()` (~L1031) — generalize the `languages` argument
     guard to membership-of-registered-set instead of `!= "python"`.
   - `sqlite_adapter.py::begin()` (~L450) — accept any non-empty subset of
     the registered language set instead of literal `("python",)` equality.
3. **Generalize the search request** — `query.py::ValidatedSearchRequest.language: str`
   (singular, force-set to `"python"`) becomes `languages: tuple[str, ...]`
   (defaults to all configured languages when the caller omits the filter).
   `_canonical_rank_query`/`_alias_rank_query` swap `f.language = ?` for
   `f.language IN (...)` with an expanded parameter list.
   `mcp_adapter.py`'s `"languages": [request.language]` becomes
   `list(request.languages)`.

## 2. Components

### 2.1 `languages/typescript.py::TypeScriptAdapter` (new)

`language="typescript"`, `prefix="ts"`, `extensions=(".ts", ".tsx")` — one
adapter/one language slug covers both extensions; internally it selects the
`typescript` or `tsx` tree-sitter grammar per file extension (two distinct
grammars, unlike Python's single grammar — this dispatch is fully internal to
the adapter, invisible to core).

Tree-sitter baseline mirrors `PythonAdapter`'s lazy-grammar pattern: the first
`parse_file` call lazily resolves the grammar via
`tree_sitter_language_pack.get_parser(...)`, with the same offline fallback
shape Python already uses. Extraction parity target (full parity per intent):
functions, arrow functions, classes, interfaces, type aliases, enums, methods,
ESM `import`/`export` (CommonJS `require` as best-effort static evidence),
member bindings — same schema-v2 `FileRecord`/`SymbolRecord`/`RelationRecord`
shape as Python. `kind` grows `interface`/`type_alias`/`enum` alongside the
existing values; `KNOWN_ENTITY_KINDS`/`KNOWN_SYMBOL_KINDS` in `query.py` grows
to match.

`resolve_references` follows Python's two-phase shape (parse, then resolve
against the project `SymbolIndex`), producing `RelationRecord`s (`IMPORTS`,
`CALLS`, `EXTENDS`/`IMPLEMENTS` for classes/interfaces).

### 2.2 Optional type-resolution boost (adapter-internal, not a new `LanguageAdapter`)

New config field `code_graph.typescript_type_boost: bool = False` (default
off — the intent's No-go zone forbids default-enabling it). When `true`,
`TypeScriptAdapter` spawns an isolated, bounded-timeout Node.js subprocess
running the project's own `typescript` package (TS Compiler API) to resolve
otherwise-ambiguous type references. Subprocess absence, timeout, or
non-zero exit degrades silently to the Tree-sitter-only result for that file
— never raises, never blocks the baseline (hard constraint from intent).

### 2.3 `config.py` changes

- `KNOWN_LANGUAGES = frozenset({"python", "typescript"})`; `_languages()`
  validates membership against it instead of the `"python"` literal.
- `_FIELDS` gains `"typescript_type_boost"`; `CodeGraphConfig` gains
  `typescript_type_boost: bool = False`, validated by the existing `_bool()`.

### 2.4 `runtime.py` changes

- `snapshot()` (~L724): replace `languages=("python",)` with
  `languages=tuple(sorted({row["language"] for row in rows["files"]}))`.
- `index()` (~L1031): replace the `!= "python"` guard with
  `language not in KNOWN_LANGUAGES` (import from `config.py`).

### 2.5 `sqlite_adapter.py` changes

- `begin()` (~L450): replace `tuple(header.languages) != ("python",)` with
  `not set(header.languages) <= KNOWN_LANGUAGES or not header.languages`.

### 2.6 `query.py` changes

- `ValidatedSearchRequest.language: str` → `languages: tuple[str, ...]`.
- `validate_search_request(..., languages: list[str] | None = None)`:
  `None` → the project's configured `code_graph.languages` (plumbed through
  from the runtime call site, not the global `KNOWN_LANGUAGES` registry — a
  python-only project must not silently search a typescript column it never
  indexed); explicit list validated against `KNOWN_LANGUAGES`,
  same `CodeGraphQueryError("unsupported language")` on a miss.
- `_canonical_rank_query`/`_alias_rank_query`: every `f.language = ?` /
  `AND f.language = ?` predicate becomes
  `f.language IN ({placeholders})` with `request.languages` spliced into
  `common_parameters`.
- `mcp_adapter.py` reader plumbing: `"languages": [request.language]` →
  `"languages": list(request.languages)`.

## 3. Data flow

```
wiki_code_index(languages=["python", "typescript"])
  -> config.languages validated against KNOWN_LANGUAGES
  -> discover_sources(extensions = union of registered adapters' extensions)
  -> indexer._parse(): per-file adapter picked by extension match (already generic)
  -> indexer._resolve(): per-file adapter.resolve_references (already generic)
  -> SnapshotHeader.languages = tuple(sorted(distinct file.language in parsed_files))
  -> one graph; files/symbols/relations tagged language="python"|"typescript"

wiki_code_search(query, languages=None | ["typescript"])
  -> validate_search_request: None -> configured languages; else validated subset
  -> SQL: f.language IN (...) replacing f.language = 'python'
  -> SearchResult shape unchanged
```

## 4. Error handling

- `code_graph.languages` with an unknown value → same `CodeGraphConfigError`
  shape, message names both supported values.
- `code_graph.typescript_type_boost` non-boolean → `CodeGraphConfigError` via
  the existing `_bool()` path.
- tsc-boost subprocess failure/timeout/absence → swallowed inside
  `TypeScriptAdapter`; adds one `KNOWN_WARNING_CODES` entry
  (`typescript_boost_unavailable`), recorded once per build, not per file.
- `wiki_code_search(languages=[...])` naming an unregistered language → same
  `CodeGraphQueryError("unsupported language")` shape.

## 5. Testing

- `TypeScriptAdapter` unit tests mirroring `PythonAdapter`'s fixture suite:
  `.ts`/`.tsx` declaration extraction, import/export resolution,
  class/interface/enum/type-alias kinds, member bindings.
- Discovery: `.ts`/`.tsx` picked up alongside `.py` when both languages are
  configured; still respects `exclude`.
- Config: `languages = ["python", "typescript"]` accepted; unknown language
  rejected; `typescript_type_boost` bool validation.
- Mixed-repo fixture (small Python + TypeScript sample) — `wiki_code_index`
  builds one snapshot, `SnapshotHeader.languages == ("python", "typescript")`;
  `wiki_code_search`/`wiki_code_context` return correct results for both
  languages from the same `repository_id`.
- Query regression: python-only search unaffected (Health Metric);
  `languages=["typescript"]` excludes Python entities and vice versa.
- tsc-boost: a test with the boost enabled against a stub subprocess (no real
  Node/npm dependency in CI) proving baseline success even when the stub
  fails/is absent.
- `reference/code-graph-benchmark`'s python-only suite rerun after the
  change — zero regression (Health Metric gate).

## Out of scope

- Cross-language relations (a TS file importing a Python module or vice
  versa) — no such coupling exists via a real language boundary; not
  requested by the intent.
- LSP/`tsserver`-based resolution — rejected by the intent's steering
  constraint (prefer TS Compiler API over a long-lived language server).
- Untyped JavaScript (`.js`/`.jsx`) — intent scopes TypeScript/TSX only.
- Hosted PostgreSQL publication schema changes — the existing `language`
  column is already per-entity; no migration expected, but the plan must add
  a verification step confirming the postgres publication path carries no
  python-only gate symmetrical to `sqlite_adapter.py::begin()`.
