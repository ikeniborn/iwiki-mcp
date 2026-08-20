# JavaScript Code-Graph Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Index JavaScript (`.js`/`.jsx`/`.mjs`/`.cjs`) into the code graph as a first-class
language with `DECLARES`, `IMPORTS`, `CALLS`, and `INHERITS` relations, without changing
Python or TypeScript output.

**Architecture:** A new `languages/_ecmascript.py` holds the Tree-sitter machinery both
ECMAScript adapters share, parameterized by a frozen `LanguageProfile`. `typescript.py`
becomes a thin adapter over it with a profile whose flags reproduce today's behaviour
exactly; `javascript.py` adds `JavaScriptAdapter` with the JavaScript profile, plus
JavaScript-only reference extraction (CommonJS `require`, `CALLS`) and relative-specifier
resolution against the project `SymbolIndex`. `resolver.py` gains language-family scoping
so JavaScript module names cannot perturb Python resolution.

**Tech Stack:** Python 3.11+, `tree-sitter` + `tree-sitter-typescript` (tsx grammar, no new
dependency), pytest (`asyncio_mode=auto`, `pythonpath=["src"]`), flake8 (max-line-length 100),
SQLite/PostgreSQL snapshot store.

**Spec:** `docs/superpowers/specs/2026-08-20-codegraph-javascript-support-design.md`

## Global Constraints

- No new runtime dependency in `pyproject.toml`. The tsx grammar comes from the already
  declared `tree-sitter-typescript>=0.23.0`.
- No code execution during indexing: no `node`, no `tsc`, no bundler, no `node_modules`
  traversal, no network.
- JavaScript identity is exactly `language = "javascript"`, `prefix = "js"`,
  `extensions = (".js", ".jsx", ".mjs", ".cjs")`, `adapter_version = "javascript-adapter-v1"`.
- No schema migration and no publication-protocol change.
- Python and TypeScript records stay byte-identical; Tasks 1–2 capture the baselines that
  prove it and every later task must keep them green.
- `uv run flake8 src tests` must stay clean (max-line-length 100); there is no formatter,
  so match surrounding style by hand.
- Tests never hit the network; follow the existing `monkeypatch` patterns in
  `tests/codegraph/`.
- Emit no relation on a guess: unresolvable targets keep their raw text with
  `resolution_hint = "unresolved"`.

---

### Task 1: TypeScript adapter-level golden baseline

Captures TypeScript's current output **before** any refactor. Every later task keeps this
test green. This task must be committed before Task 3 touches `typescript.py`.

**Files:**
- Create: `tests/fixtures/codegraph/typescript_golden/walker.ts`
- Create: `tests/fixtures/codegraph/typescript_golden/shapes.ts`
- Create: `tests/codegraph/fixtures/typescript_golden.json` (generated, committed)
- Create: `tests/codegraph/test_typescript_golden.py`

**Interfaces:**
- Consumes: `iwiki_mcp.codegraph.languages.typescript.TypeScriptAdapter`,
  `iwiki_mcp.codegraph.resolver.SymbolIndex`.
- Produces: `tests/codegraph/test_typescript_golden.py::_serialize_parsed(adapter, path, source)`
  returning a JSON-ready dict — reused by no other task, but the golden JSON file it
  writes is the contract Task 3 must not break.

- [ ] **Step 1: Create the fixture exercising every walker branch the refactor touches**

`tests/fixtures/codegraph/typescript_golden/walker.ts`:

```typescript
import defaultThing, { named as renamed, other } from "./shapes";
import * as ns from "./shapes";
import "./shapes";

export const arrowConst = (a: number, b: string): string => b;
var functionExpression = function (x: number) { return x; };

export const literalApi = {
  shorthand(a: number) { return a; },
  pairValued: function (b: number) { return b; },
  arrowValued: (c: number) => c,
};

export function outerFunction(seed: number) {
  function innerFunction(step: number) { return seed + step; }
  return innerFunction;
}

export class Walker extends defaultThing implements other {
  private _hidden: number = 1;

  async method(a: number): Promise<number> {
    const insideMethod = { shorthand() { return 1; } };
    return a;
  }
}
```

`tests/fixtures/codegraph/typescript_golden/shapes.ts`:

```typescript
export type Alias = string;
export enum Mode { On, Off }
export interface Shape extends Base { size: number; }
export interface Base { id: string; }

export namespace Outer {
  export class Inner extends Base2 {}
  export class Base2 {}
}
```

- [ ] **Step 2: Write the golden test with a regeneration guard**

`tests/codegraph/test_typescript_golden.py`:

```python
"""Byte-level baseline of TypeScript adapter output, captured pre-refactor.

Regenerating GOLDEN_PATH is an explicit, reviewed act: run this module with
IWIKI_REGENERATE_TYPESCRIPT_GOLDEN=1 and commit the diff deliberately. A silent
regeneration would defeat the whole point of the baseline.
"""
import dataclasses
import json
import os
from pathlib import Path

from iwiki_mcp.codegraph.languages.typescript import TypeScriptAdapter
from iwiki_mcp.codegraph.resolver import SymbolIndex

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "codegraph" / "typescript_golden"
GOLDEN_PATH = Path(__file__).resolve().parent / "fixtures" / "typescript_golden.json"
SOURCES = ("walker.ts", "shapes.ts")


def _adapter():
    return TypeScriptAdapter("golden-domain", (), parser_version="golden-parser")


def _capture():
    adapter = _adapter()
    parsed = {
        name: adapter.parse_file((FIXTURES / name).read_bytes(), name)
        for name in SOURCES
    }
    index = SymbolIndex.from_parsed_files(parsed.values())
    captured = {}
    for name, item in parsed.items():
        result = adapter.resolve_references(item, index)
        captured[name] = {
            "file": dataclasses.asdict(item.file),
            "symbols": [dataclasses.asdict(symbol) for symbol in item.symbols],
            "references": [dataclasses.asdict(ref) for ref in item.references],
            "relations": [dataclasses.asdict(rel) for rel in result.relations],
            "parse_warnings": list(item.warnings),
            "resolve_warnings": list(result.warnings),
        }
    return captured


def test_typescript_output_matches_golden_baseline():
    captured = _capture()
    if os.environ.get("IWIKI_REGENERATE_TYPESCRIPT_GOLDEN") == "1":
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(json.dumps(captured, indent=2, sort_keys=True) + "\n")
    golden = json.loads(GOLDEN_PATH.read_text())
    assert captured == golden
```

- [ ] **Step 3: Run it to verify it fails (no golden file yet)**

Run: `uv run pytest tests/codegraph/test_typescript_golden.py -v`
Expected: FAIL with `FileNotFoundError` on `typescript_golden.json`.

- [ ] **Step 4: Generate the golden file from the current, unmodified adapter**

```bash
IWIKI_REGENERATE_TYPESCRIPT_GOLDEN=1 uv run pytest tests/codegraph/test_typescript_golden.py -q
```

- [ ] **Step 5: Run it again without the env var to verify it passes**

Run: `uv run pytest tests/codegraph/test_typescript_golden.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/fixtures/codegraph/typescript_golden tests/codegraph/test_typescript_golden.py tests/codegraph/fixtures/typescript_golden.json
git commit -m "test(codegraph): capture TypeScript adapter golden baseline pre-refactor"
```

---

### Task 2: Run-level Python + TypeScript baseline

Proves the intent's "Done when" comparison: a full index run over the mixed fixture
repository produces byte-identical Python and TypeScript rows before and after the change.

**Files:**
- Create: `tests/codegraph/fixtures/mixed_python_typescript_rows.json` (generated, committed)
- Create: `tests/codegraph/test_mixed_language_baseline.py`
- Read for reference: `tests/codegraph/test_mixed_language_indexing.py:17-68`

**Interfaces:**
- Consumes: the `_build_indexer` helper pattern from
  `tests/codegraph/test_mixed_language_indexing.py:17-55`, `indexer.build_rows().tables`.
- Produces: `tests/codegraph/test_mixed_language_baseline.py::_pinned_factories()` returning
  `dict[str, AdapterFactory]` with fixed version strings — reused conceptually by Task 11.

- [ ] **Step 1: Read the existing helper so the baseline builds the same way**

Run: `sed -n 1,70p tests/codegraph/test_mixed_language_indexing.py`
Expected: the `_build_indexer(...)` helper signature, its `adapter_factories` parameter, and
the `build_rows().tables` usage.

- [ ] **Step 2: Write the baseline test**

`tests/codegraph/test_mixed_language_baseline.py` — mirror `_build_indexer` from
`test_mixed_language_indexing.py`, but always pass pinned factories so `parser_version`
cannot drift with an installed dependency bump:

```python
"""Run-level baseline: Python + TypeScript rows must not move.

Version strings are pinned rather than read from installed distributions, so a
dependency bump changes no baseline row (only `parser_version` would drift;
identifiers do not hash it).

Regenerate deliberately with IWIKI_REGENERATE_MIXED_BASELINE=1.
"""
import json
import os
from pathlib import Path

from iwiki_mcp.codegraph import indexer as codegraph_indexer
from iwiki_mcp.codegraph.languages import python as codegraph_python
from iwiki_mcp.codegraph.languages import typescript as codegraph_typescript

BASELINE_PATH = Path(__file__).resolve().parent / "fixtures" / "mixed_python_typescript_rows.json"
BASELINE_LANGUAGES = ("python", "typescript")


def _pinned_factories(domain):
    return {
        "python": codegraph_indexer.AdapterFactory(
            create=lambda paths: codegraph_python.PythonAdapter(
                domain, paths, parser_version="pinned-python",
            ),
            extensions=(".py",),
            parser_version="pinned-python",
            grammar_version="pinned-python-grammar",
            adapter_version="python-adapter-v2",
        ),
        "typescript": codegraph_indexer.AdapterFactory(
            create=lambda paths: codegraph_typescript.TypeScriptAdapter(
                domain, paths, parser_version="pinned-typescript",
            ),
            extensions=(".ts", ".tsx"),
            parser_version="pinned-typescript",
            grammar_version="pinned-typescript-grammar",
            adapter_version="typescript-adapter-v1",
        ),
    }


def _baseline_rows(tables):
    rows = {}
    for table in ("files", "symbols", "relations"):
        rows[table] = sorted(
            (
                {key: value for key, value in row.items()}
                for row in tables[table]
            ),
            key=lambda row: json.dumps(row, sort_keys=True),
        )
    return rows


def test_python_typescript_rows_match_baseline(tmp_path, monkeypatch):
    tables = _build_mixed_tables(tmp_path, monkeypatch, languages=BASELINE_LANGUAGES)
    captured = _baseline_rows(tables)
    if os.environ.get("IWIKI_REGENERATE_MIXED_BASELINE") == "1":
        BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BASELINE_PATH.write_text(json.dumps(captured, indent=2, sort_keys=True) + "\n")
    baseline = json.loads(BASELINE_PATH.read_text())
    assert captured == baseline
```

`_build_mixed_tables(tmp_path, monkeypatch, *, languages)` is a local helper copied from
`test_mixed_language_indexing.py`'s `_build_indexer` usage: it copies
`tests/fixtures/codegraph/mixed_python_typescript/` into `tmp_path`, writes an
`.iwiki.toml` whose `code_graph.languages` is `languages`, constructs the indexer with
`_pinned_factories(domain)`, and returns `indexer.build_rows().tables`. Read the existing
file and reuse its exact construction so the two tests cannot diverge.

- [ ] **Step 3: Run to verify it fails (no baseline file)**

Run: `uv run pytest tests/codegraph/test_mixed_language_baseline.py -v`
Expected: FAIL with `FileNotFoundError`.

- [ ] **Step 4: Generate the baseline from the current, unmodified code**

```bash
IWIKI_REGENERATE_MIXED_BASELINE=1 uv run pytest tests/codegraph/test_mixed_language_baseline.py -q
```

- [ ] **Step 5: Verify it passes and the whole suite is green**

Run: `uv run pytest tests/codegraph/test_mixed_language_baseline.py -v && uv run pytest -q`
Expected: PASS, full suite green.

- [ ] **Step 6: Commit**

```bash
git add tests/codegraph/test_mixed_language_baseline.py tests/codegraph/fixtures/mixed_python_typescript_rows.json
git commit -m "test(codegraph): capture run-level Python/TypeScript row baseline"
```

---

### Task 3: Extract the shared ECMAScript core

Pure move plus profile parameterization. TypeScript behaviour must not change; Tasks 1–2
are the proof.

**Files:**
- Create: `src/iwiki_mcp/codegraph/languages/_ecmascript.py`
- Modify: `src/iwiki_mcp/codegraph/languages/typescript.py` (whole file restructured)
- Test: `tests/codegraph/test_typescript_golden.py` (existing, must stay green),
  `tests/codegraph/test_typescript_adapter.py` (existing, must stay green)

**Interfaces:**
- Consumes: nothing new.
- Produces, in `_ecmascript.py`:
  - `LanguageProfile(language: str, prefix: str, kind_by_node: Mapping[str, str],
    handles_interface: bool = True, handles_namespace: bool = True,
    object_literal_scope: bool = False, declaration_hooks: tuple = ())` — frozen dataclass.
  - `get_parser(grammar: str) -> Any`
  - `text(source: bytes, node) -> str`
  - `relative_path(path: str) -> str`
  - `param_signature(source, params_node) -> str`
  - `return_type_signature(source, return_type_node) -> str`
  - `visibility(name: str) -> str`
  - `PendingHeritage` (frozen dataclass, gains a `resolution_scope: str = "file"` field)
  - `pending_heritage_references(source, node, *, owner_symbol_id, source_file_id,
    owner_qualified=None) -> tuple[PendingHeritage, ...]`
  - `heritage_scope_candidates(owner_qualified, module_dotted_name) -> tuple[str, ...]`
  - `resolve_heritage_references(pending, qualified_names, module_dotted_name)
    -> tuple[ReferenceRecord, ...]`
  - `import_bindings(source, clause) -> tuple[tuple[str, str], ...]`
  - `esm_import_references(source, root, *, file_record) -> tuple[ReferenceRecord, ...]`
  - `extract_symbols(source, root, *, profile, repository_id, relative_path, file_record,
    module_dotted_name) -> tuple[tuple[SymbolRecord, ...], tuple[PendingHeritage, ...]]`
  - `dedupe_symbols(symbols) -> tuple[list[SymbolRecord], tuple[str, ...]]`

- [ ] **Step 1: Confirm the baselines are green before touching anything**

Run: `uv run pytest tests/codegraph/test_typescript_golden.py tests/codegraph/test_mixed_language_baseline.py tests/codegraph/test_typescript_adapter.py -q`
Expected: PASS.

- [ ] **Step 2: Create `_ecmascript.py` by moving code verbatim**

Move these from `typescript.py` unchanged except for dropping the leading underscore from
the names listed in **Interfaces**: `_PARSERS`, `_get_parser`, `_text`, `_relative_path`,
`_param_signature`, `_return_type_signature`, `_visibility`, `_PendingHeritage`,
`_pending_heritage_references`, `_heritage_scope_candidates`,
`_resolve_heritage_references`, `_import_bindings`, `_extract_references` (renamed
`esm_import_references`), `_extract_symbols` (renamed `extract_symbols`).

Do **not** move: `_run_tsc_boost`, `_TSC_BOOST_SCRIPT`, `_TypeScriptParsedFile`,
`TypeScriptAdapter`, `_grammar_name`. Four existing tests monkeypatch
`iwiki_mcp.codegraph.languages.typescript._run_tsc_boost`; moving it breaks them.

Add the profile at the top of the new module:

```python
@dataclass(frozen=True)
class LanguageProfile:
    """Per-language switches for the shared ECMAScript walker."""

    language: str
    prefix: str
    kind_by_node: Mapping[str, str] = field(default_factory=dict)
    handles_interface: bool = True
    handles_namespace: bool = True
    object_literal_scope: bool = False
    declaration_hooks: tuple = ()
```

Every default reproduces TypeScript's current behaviour, so a profile built with only
`language`/`prefix`/`kind_by_node` drives the walker exactly as today.

- [ ] **Step 3: Parameterize the walker with the profile**

Inside `extract_symbols`, replace the hard-coded branches with profile lookups:

- `if ctype in _KIND_BY_NODE` becomes `if ctype in profile.kind_by_node`.
- the `interface_declaration` branch runs only `if profile.handles_interface`.
- the `internal_module` and ambient `module` branches run only `if profile.handles_namespace`.
- the `variable_declarator` `else` arm becomes:

```python
                    elif (
                        profile.object_literal_scope
                        and value is not None
                        and value.type == "object"
                        and name_node is not None
                        and name_node.type not in ("object_pattern", "array_pattern")
                    ):
                        local = text(source, name_node)
                        scope = (
                            f"{owner_qualified}.{local}" if owner_qualified
                            else f"{module_dotted_name}.{local}"
                        )
                        walk(value, scope)
                    else:
                        walk(declarator, owner_qualified)
```

- the catch-all `else` arm consults hooks before recursing:

```python
            else:
                claimed = False
                for hook in profile.declaration_hooks:
                    if hook(child, owner_qualified, make_symbol, symbols):
                        claimed = True
                        break
                if not claimed:
                    walk(child, owner_qualified)
```

Signature ordering for the hook contract:
`hook(node, owner_qualified, make_symbol, symbols) -> bool`, where `make_symbol` is the
walker's closure `(node, kind, name_node, *, owner_qualified=None, params_node=None,
return_type_node=None, is_async=False) -> tuple[str, str]`.

- [ ] **Step 4: Add the per-reference heritage scope**

`PendingHeritage` gains `resolution_scope: str = "file"`;
`pending_heritage_references` gains a keyword-only `resolution_scope: str = "file"` that it
stamps onto each record; `resolve_heritage_references` passes `item.resolution_scope` into
the emitted `ReferenceRecord` instead of the literal `"file"`. TypeScript passes nothing,
so it keeps `"file"`.

- [ ] **Step 5: Rewrite `typescript.py` to delegate**

Keep `TypeScriptAdapter`'s class attributes, `__init__` signature, boost probing, and
`_grammar_name`. Define the profile once at module level:

```python
_TYPESCRIPT_PROFILE = _ecmascript.LanguageProfile(
    language="typescript",
    prefix="ts",
    kind_by_node={
        "type_alias_declaration": "type_alias",
        "enum_declaration": "enum",
    },
)
```

`parse_file` calls `_ecmascript.get_parser`, `_ecmascript.extract_symbols(...,
profile=_TYPESCRIPT_PROFILE, ...)`, `_ecmascript.dedupe_symbols`,
`_ecmascript.resolve_heritage_references`, and `_ecmascript.esm_import_references`, in the
same order and with the same arguments as today.

- [ ] **Step 6: Run the baselines and the TypeScript suite**

Run: `uv run pytest tests/codegraph/test_typescript_golden.py tests/codegraph/test_typescript_adapter.py tests/codegraph/test_mixed_language_baseline.py -v`
Expected: PASS, with no golden diff. A golden failure means the move was not
behaviour-preserving — fix the move, never the golden file.

- [ ] **Step 7: Run the full suite and the linter**

Run: `uv run pytest -q && uv run flake8 src tests`
Expected: PASS, no lint output.

- [ ] **Step 8: Commit**

```bash
git add src/iwiki_mcp/codegraph/languages/_ecmascript.py src/iwiki_mcp/codegraph/languages/typescript.py
git commit -m "refactor(codegraph): extract shared ECMAScript core from TypeScript adapter"
```

---

### Task 4: Language-family scoping in the resolver

**Files:**
- Modify: `src/iwiki_mcp/codegraph/resolver.py` (`SymbolIndex`, `_symbol_candidates`,
  `_module_prefix_candidates`, `resolve_references`)
- Test: `tests/codegraph/test_resolver.py`

**Interfaces:**
- Consumes: `FileRecord.language` (`models.py:164`), `ParsedFile.file`.
- Produces:
  - `SymbolIndex.languages_by_file_id: Mapping[str, str]` — declared **before**
    `_adapter_evidence` and defaulted to an empty mapping.
  - `resolver.LANGUAGE_FAMILIES: Mapping[str, frozenset[str]]`
  - `resolver.family_languages(language: str) -> frozenset[str] | None` — `None` means
    "do not filter".
  - `_symbol_candidates(reference, index, language)` and
    `_module_prefix_candidates(target, index, language)` — both gain the third parameter.

- [ ] **Step 1: Write the failing tests**

Append to `tests/codegraph/test_resolver.py`:

```python
def test_python_import_ignores_same_named_javascript_module():
    python_file = _file_record(language="python", path="src/utils.py",
                               module_qualified_name="src.utils")
    javascript_file = _file_record(language="javascript", path="src/utils.js",
                                   module_qualified_name="src.utils")
    index = SymbolIndex.from_parsed_files((
        _parsed(python_file), _parsed(javascript_file),
    ))
    reference = _reference(
        relation_type="IMPORTS", target_reference="src.utils",
        target_kind_hint="module", resolution_scope="project",
        source_file_id="py-source",
    )
    relations = resolve_references("python", "py", "domain", (reference,), index)
    assert len(relations) == 1
    assert relations[0].resolution_state == "resolved"
    assert relations[0].target_module_id == python_file.module_id


def test_javascript_import_resolves_into_typescript_module():
    typescript_file = _file_record(language="typescript", path="src/shapes.ts",
                                   module_qualified_name="src.shapes")
    index = SymbolIndex.from_parsed_files((_parsed(typescript_file),))
    reference = _reference(
        relation_type="IMPORTS", target_reference="src.shapes",
        target_kind_hint="module", resolution_scope="project",
        source_file_id="js-source",
    )
    relations = resolve_references("javascript", "js", "domain", (reference,), index)
    assert relations[0].resolution_state == "resolved"
    assert relations[0].target_module_id == typescript_file.module_id


def test_unknown_language_is_not_filtered():
    other_file = _file_record(language="ruby", path="src/thing.rb",
                              module_qualified_name="src.thing")
    index = SymbolIndex.from_parsed_files((_parsed(other_file),))
    reference = _reference(
        relation_type="IMPORTS", target_reference="src.thing",
        target_kind_hint="module", resolution_scope="project",
        source_file_id="rb-source",
    )
    relations = resolve_references("ruby", "rb", "domain", (reference,), index)
    assert relations[0].resolution_state == "resolved"
```

Reuse the module's existing record-building helpers; if `_file_record` / `_parsed` /
`_reference` do not exist under those names, read the file and use whatever helpers it
already defines rather than inventing new ones.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/codegraph/test_resolver.py -k "same_named_javascript or into_typescript or unknown_language" -v`
Expected: FAIL — the Python case resolves `ambiguous` (two candidates) instead of
`resolved`.

- [ ] **Step 3: Implement the scoping**

In `resolver.py`:

```python
LANGUAGE_FAMILIES = {
    "python": frozenset({"python"}),
    "typescript": frozenset({"typescript", "javascript"}),
    "javascript": frozenset({"javascript", "typescript"}),
}


def family_languages(language: str) -> frozenset[str] | None:
    """Languages whose declarations may satisfy ``language``'s references.

    ``None`` means "apply no filter" -- an unknown language keeps the
    pre-scoping behaviour so nothing regresses for callers this map does
    not describe.
    """
    return LANGUAGE_FAMILIES.get(language)
```

`SymbolIndex` gains, declared before `_adapter_evidence`:

```python
    languages_by_file_id: Mapping[str, str] = field(default_factory=dict)
```

`from_parsed_files` populates it with `{item.file.file_id: item.file.language for item in parsed}`.

`_symbol_candidates(reference, index, language)` filters candidates whose
`index.languages_by_file_id.get(item.file_id)` is absent from the family (a missing entry
is never filtered). The `exact_modules` lookup and `_module_prefix_candidates` filter on
`FileRecord.language` directly.

In `_module_prefix_candidates`, filter `index.modules_by_qualified` **before** the longest
match is chosen:

```python
def _module_prefix_candidates(target, index, language):
    allowed = family_languages(language)
    names = tuple(
        name for name, files in index.modules_by_qualified.items()
        if (target == name or target.startswith(name + "."))
        and (allowed is None or any(item.language in allowed for item in files))
    )
    if not names:
        return ()
    longest = max(names, key=lambda name: (len(name), name))
    return tuple(
        item for item in index.modules_by_qualified[longest]
        if allowed is None or item.language in allowed
    )
```

- [ ] **Step 4: Run the new tests, then the guarded baselines**

Run: `uv run pytest tests/codegraph/test_resolver.py -v && uv run pytest tests/codegraph/test_typescript_golden.py tests/codegraph/test_mixed_language_baseline.py -q`
Expected: PASS everywhere. TypeScript output cannot change: every TS reference is either
`unresolved` or file-scoped.

- [ ] **Step 5: Run the full suite and the linter**

Run: `uv run pytest -q && uv run flake8 src tests`
Expected: PASS, no lint output.

- [ ] **Step 6: Commit**

```bash
git add src/iwiki_mcp/codegraph/resolver.py tests/codegraph/test_resolver.py
git commit -m "feat(codegraph): scope resolution candidates by language family"
```

---

### Task 5: JavaScript adapter — identity, module records, base declarations

**Files:**
- Create: `src/iwiki_mcp/codegraph/languages/javascript.py`
- Create: `tests/codegraph/test_javascript_adapter.py`

**Interfaces:**
- Consumes: everything Task 3 exported from `_ecmascript`.
- Produces:
  - `javascript.JavaScriptAdapter(repository_id: str, source_paths: tuple[str, ...], *,
    parser_version: str = "tree-sitter-typescript")` with
    `language = "javascript"`, `prefix = "js"`,
    `extensions = (".js", ".jsx", ".mjs", ".cjs")`.
  - `javascript.JAVASCRIPT_PROFILE` — the `LanguageProfile` instance.
  - `JavaScriptAdapter.parse_file(source: bytes, path: str) -> ParsedFile`
  - `JavaScriptAdapter.resolve_references(parsed, project_index) -> ResolutionResult`
    (Task 8 extends this; here it delegates like TypeScript's does).

- [ ] **Step 1: Write the failing tests**

`tests/codegraph/test_javascript_adapter.py`:

```python
from iwiki_mcp.codegraph.languages.javascript import JavaScriptAdapter


def _adapter():
    return JavaScriptAdapter("domain", (), parser_version="test-parser")


def test_adapter_identity():
    adapter = _adapter()
    assert adapter.language == "javascript"
    assert adapter.prefix == "js"
    assert adapter.extensions == (".js", ".jsx", ".mjs", ".cjs")


def test_every_javascript_file_is_module_backed():
    parsed = _adapter().parse_file(b"module.exports = { a: 1 };\n", "src/legacy.cjs")
    assert parsed.file.module_qualified_name == "src.legacy"
    assert parsed.file.module_local_name == "legacy"
    assert parsed.file.module_id
    assert parsed.file.module_name_tokens_casefold
    assert parsed.file.module_key == "src/legacy.cjs"


def test_multi_dot_basename_stem_stops_at_first_dot():
    parsed = _adapter().parse_file(b"export const a = 1;\n", "src/util.test.js")
    assert parsed.file.module_qualified_name == "src.util"


def test_function_class_and_arrow_declarations_extracted():
    source = (
        b"export function outer(a, b) {\n"
        b"  function inner(c) { return c; }\n"
        b"  return inner;\n"
        b"}\n"
        b"export const arrow = async (x) => x;\n"
        b"var expr = function (y) { return y; };\n"
        b"export class Widget { async render(z) { return z; } }\n"
    )
    parsed = _adapter().parse_file(source, "src/app.js")
    found = {(symbol.kind, symbol.qualified_name) for symbol in parsed.symbols}
    assert ("function", "src.app.outer") in found
    assert ("function", "src.app.outer.inner") in found
    assert ("async_function", "src.app.arrow") in found
    assert ("function", "src.app.expr") in found
    assert ("class", "src.app.Widget") in found
    assert ("method", "src.app.Widget.render") in found


def test_anonymous_default_export_emits_no_symbol():
    parsed = _adapter().parse_file(b"export default function () { return 1; }\n", "a.js")
    assert parsed.symbols == ()


def test_jsx_parses_without_error():
    source = b"export const App = () => <div className='x'><Child {...p} /></div>;\n"
    parsed = _adapter().parse_file(source, "src/App.jsx")
    assert any(symbol.qualified_name == "src.App.App" for symbol in parsed.symbols)


def test_source_must_be_bytes():
    import pytest
    with pytest.raises(TypeError):
        _adapter().parse_file("export const a = 1;", "a.js")
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/codegraph/test_javascript_adapter.py -v`
Expected: FAIL with `ModuleNotFoundError: iwiki_mcp.codegraph.languages.javascript`.

- [ ] **Step 3: Implement the adapter**

`javascript.py` mirrors `typescript.py`'s `parse_file` with three differences:

```python
JAVASCRIPT_PROFILE = _ecmascript.LanguageProfile(
    language="javascript",
    prefix="js",
    kind_by_node={},
    handles_interface=False,
    handles_namespace=False,
    object_literal_scope=True,
    declaration_hooks=(),          # Task 6 adds the prototype hook
)
```

1. the grammar is always `"tsx"` (`_ecmascript.get_parser("tsx")`);
2. module identity is unconditional — no `is_module` probe:

```python
        posix_path = PurePosixPath(relative_path)
        local_name = posix_path.name.split(".", 1)[0]
        module_dotted_name = ".".join((*posix_path.parent.parts, local_name))
```

   with `module_key=relative_path`, `module_qualified_name=module_dotted_name`,
   `module_local_name=local_name`,
   `module_name_tokens_casefold=token_key(module_dotted_name, local_name)`, and
   `module_id=module_id(self.language, self.prefix, self.repository_id, relative_path,
   module_dotted_name)`;
3. no tsc boost — `resolve_references` returns
   `ResolutionResult(relations=sort_relations((*declares, *resolved)), warnings=())`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/codegraph/test_javascript_adapter.py -v`
Expected: PASS.

- [ ] **Step 5: Run the guarded baselines, the full suite, and the linter**

Run: `uv run pytest -q && uv run flake8 src tests`
Expected: PASS, no lint output.

- [ ] **Step 6: Commit**

```bash
git add src/iwiki_mcp/codegraph/languages/javascript.py tests/codegraph/test_javascript_adapter.py
git commit -m "feat(codegraph): add JavaScript adapter with module and base declarations"
```

---

### Task 6: Object-literal and prototype methods

**Files:**
- Modify: `src/iwiki_mcp/codegraph/languages/javascript.py` (add the prototype hook, wire it
  into `JAVASCRIPT_PROFILE`)
- Test: `tests/codegraph/test_javascript_adapter.py`

**Interfaces:**
- Consumes: the hook contract from Task 3
  (`hook(node, owner_qualified, make_symbol, symbols) -> bool`).
- Produces: `javascript.prototype_method_hook` with that signature.

- [ ] **Step 1: Write the failing tests**

```python
def test_object_literal_methods_are_scoped_under_the_declarator():
    source = (
        b"export const api = {\n"
        b"  get(u) { return u; },\n"
        b"  post: function (u) { return u; },\n"
        b"  put: (u) => u,\n"
        b"  [dynamic]: (u) => u,\n"
        b"  plain: 1,\n"
        b"};\n"
    )
    parsed = _adapter().parse_file(source, "src/api.js")
    names = {symbol.qualified_name for symbol in parsed.symbols}
    assert {"src.api.api.get", "src.api.api.post", "src.api.api.put"} <= names
    assert not any(name.endswith(".plain") for name in names)
    assert not any("dynamic" in name for name in names)


def test_destructuring_declarator_with_object_initializer_is_skipped():
    parsed = _adapter().parse_file(b"const { a } = { a() { return 1; } };\n", "src/d.js")
    assert parsed.symbols == ()


def test_prototype_method_attaches_to_a_known_local_symbol():
    source = (
        b"function Widget() {}\n"
        b"Widget.prototype.render = function (x) { return x; };\n"
        b"Unknown.prototype.hidden = function () {};\n"
    )
    parsed = _adapter().parse_file(source, "src/widget.js")
    names = {symbol.qualified_name for symbol in parsed.symbols}
    assert "src.widget.Widget.render" in names
    assert not any("hidden" in name for name in names)


def test_object_literal_method_signature_has_no_return_segment():
    parsed = _adapter().parse_file(b"const api = { get(a, b) { return a; } };\n", "s.js")
    symbol = next(item for item in parsed.symbols if item.local_name == "get")
    assert symbol.kind == "method"
    assert symbol.signature == "method|(a, b)"
    assert symbol.visibility == "public"


def test_duplicate_symbol_identity_warns():
    source = (
        b"if (x) { function dup() { return 1; } }\n"
        b"else { function dup() { return 2; } }\n"
    )
    parsed = _adapter().parse_file(source, "src/dup.js")
    assert "duplicate_symbol_identity" in parsed.warnings
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/codegraph/test_javascript_adapter.py -k "object_literal or prototype or destructuring or duplicate" -v`
Expected: FAIL — object-literal names missing, prototype method missing.

- [ ] **Step 3: Implement the prototype hook**

```python
def prototype_method_hook(node, owner_qualified, make_symbol, symbols):
    """Attach `C.prototype.m = function () {}` to an already-known `C`.

    Deliberately narrow: the owner must resolve to a symbol already
    extracted from this file, so a prototype patch on an imported or
    runtime-built object is skipped instead of guessed at.
    """
    if node.type != "expression_statement":
        return False
    assignment = next(
        (child for child in node.children if child.type == "assignment_expression"),
        None,
    )
    if assignment is None:
        return False
    left = assignment.child_by_field_name("left")
    value = assignment.child_by_field_name("right")
    if left is None or value is None:
        return False
    if value.type not in ("function_expression", "arrow_function"):
        return False
    parts = _member_chain(left)
    if parts is None or len(parts) != 3 or parts[1] != "prototype":
        return False
    owner_name, _, method_name = parts
    owner = next(
        (
            item for item in symbols
            if item.local_name == owner_name and item.kind in ("class", "function")
        ),
        None,
    )
    if owner is None:
        return False
    make_symbol(
        node, "method", left.child_by_field_name("property"),
        owner_qualified=owner.qualified_name,
        params_node=value.child_by_field_name("parameters"),
        is_async=any(child.type == "async" for child in value.children),
    )
    return True
```

`_member_chain(node)` returns the dotted parts of a non-computed member expression
(`("Widget", "prototype", "render")`) or `None` for anything computed or non-identifier;
Task 9 reuses it for `CALLS`, so define it at module level now.

Then set `declaration_hooks=(prototype_method_hook,)` in `JAVASCRIPT_PROFILE`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/codegraph/test_javascript_adapter.py -v`
Expected: PASS.

- [ ] **Step 5: Verify TypeScript did not move**

Run: `uv run pytest tests/codegraph/test_typescript_golden.py tests/codegraph/test_typescript_adapter.py -q`
Expected: PASS.

- [ ] **Step 6: Full suite, linter, commit**

```bash
uv run pytest -q && uv run flake8 src tests
git add src/iwiki_mcp/codegraph/languages/javascript.py tests/codegraph/test_javascript_adapter.py
git commit -m "feat(codegraph): extract JavaScript object-literal and prototype methods"
```

---

### Task 7: ESM and CommonJS import references

**Files:**
- Modify: `src/iwiki_mcp/codegraph/languages/javascript.py`
- Test: `tests/codegraph/test_javascript_adapter.py`

**Interfaces:**
- Consumes: `_ecmascript.esm_import_references`, `_ecmascript.import_bindings`.
- Produces:
  - `javascript.require_references(source, root, *, file_record) -> tuple[ReferenceRecord, ...]`
  - `javascript.enclosing_symbol_id(symbols, node) -> str | None` — the innermost symbol
    containing `node`; Task 9 reuses it.

- [ ] **Step 1: Write the failing tests**

```python
def _references_by_binding(parsed):
    return {ref.binding_name: ref for ref in parsed.references}


def test_esm_imports_produce_one_reference_per_binding():
    source = (
        b"import thing, { named as renamed, other } from './shapes';\n"
        b"import * as ns from './shapes';\n"
    )
    parsed = _adapter().parse_file(source, "src/app.js")
    bindings = _references_by_binding(parsed)
    assert bindings["thing"].binding_kind == "implicit_binding"
    assert bindings["renamed"].binding_kind == "explicit_alias"
    assert bindings["other"].binding_kind == "implicit_binding"
    assert bindings["ns"].binding_kind == "explicit_alias"
    assert all(ref.relation_type == "IMPORTS" for ref in parsed.references)


def test_side_effect_import_produces_no_reference():
    parsed = _adapter().parse_file(b"import './shapes';\n", "src/app.js")
    assert parsed.references == ()


def test_require_produces_import_references():
    source = (
        b"const shapes = require('./shapes');\n"
        b"const { named, other: aliased } = require('./more');\n"
    )
    parsed = _adapter().parse_file(source, "src/app.js")
    bindings = _references_by_binding(parsed)
    assert bindings["shapes"].target_reference == "./shapes"
    assert bindings["shapes"].binding_kind == "implicit_binding"
    assert bindings["named"].binding_kind == "implicit_binding"
    assert bindings["aliased"].binding_kind == "explicit_alias"


def test_dynamic_and_bare_require_produce_no_reference():
    source = b"const a = require(name);\nrequire('./side');\n"
    parsed = _adapter().parse_file(source, "src/app.js")
    assert parsed.references == ()


def test_module_level_reference_sets_module_id_not_symbol_id():
    parsed = _adapter().parse_file(b"import a from './b';\n", "src/app.js")
    reference = parsed.references[0]
    assert reference.source_symbol_id is None
    assert reference.source_module_id == parsed.file.module_id
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/codegraph/test_javascript_adapter.py -k "esm or require or side_effect or module_level" -v`
Expected: FAIL — `parsed.references` is empty.

- [ ] **Step 3: Implement**

`parse_file` sets
`references = (*_ecmascript.esm_import_references(source, root, file_record=file),
*require_references(source, root, file_record=file))`.

`require_references` walks `lexical_declaration` / `variable_declaration` nodes, and for
each `variable_declarator` whose value is a `call_expression` with callee `require` and a
single `string` argument, emits one `ReferenceRecord` per binding:

- an `identifier` name node → one binding, `implicit_binding`;
- an `object_pattern` name node → one binding per `shorthand_property_identifier_pattern`
  (`implicit_binding`) and per `pair_pattern` (the value identifier, `explicit_alias`);
- an `array_pattern` name node → no binding, no reference.

Every emitted record uses `relation_type="IMPORTS"`, the raw specifier as
`target_reference`, `resolution_hint="unresolved"` (Task 8 rewrites it),
`binding_name_tokens_casefold=token_key(binding_name)`, and the source attribution rule
below.

Source attribution, applied to **every** reference JavaScript emits (import, require,
inherits, call):

```python
def _attribute(reference, symbols, file_record, node):
    symbol_id = enclosing_symbol_id(symbols, node)
    return dataclasses.replace(
        reference,
        source_symbol_id=symbol_id,
        source_file_id=file_record.file_id,
        source_module_id=file_record.module_id if symbol_id is None else None,
    )
```

`source_module_id` must be `None` whenever `source_symbol_id` is set: `schema.py:138`
enforces `CHECK (source_module_id IS NULL OR source_symbol_id IS NULL)` and the build
aborts otherwise.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/codegraph/test_javascript_adapter.py -v`
Expected: PASS.

- [ ] **Step 5: Full suite, linter, commit**

```bash
uv run pytest -q && uv run flake8 src tests
git add src/iwiki_mcp/codegraph/languages/javascript.py tests/codegraph/test_javascript_adapter.py
git commit -m "feat(codegraph): extract ESM and CommonJS imports for JavaScript"
```

---

### Task 8: Relative-specifier resolution

**Files:**
- Modify: `src/iwiki_mcp/codegraph/languages/javascript.py`
  (`JavaScriptAdapter.resolve_references`)
- Test: `tests/codegraph/test_javascript_adapter.py`

**Interfaces:**
- Consumes: `SymbolIndex.modules_by_qualified`, `resolver.resolve_references`.
- Produces:
  - `javascript.dotted_candidate(importer_path: str, specifier: str) -> str | None` — the
    normalized dotted module name for a relative specifier, or `None` when the specifier is
    bare, a URL, a Node builtin, a subpath import, or escapes the repository root.
  - `javascript.resolve_specifier(reference, importer_path, index) -> ReferenceRecord` —
    the rewritten reference.

- [ ] **Step 1: Write the failing tests**

```python
from iwiki_mcp.codegraph.resolver import SymbolIndex


def test_dotted_candidate_normalizes_and_strips_extensions():
    from iwiki_mcp.codegraph.languages.javascript import dotted_candidate
    assert dotted_candidate("src/app.js", "./util") == "src.util"
    assert dotted_candidate("src/app.mjs", "./util.js") == "src.util"
    assert dotted_candidate("src/deep/app.js", "../shared/x.ts") == "src.shared.x"
    assert dotted_candidate("src/app.js", "react") is None
    assert dotted_candidate("src/app.js", "node:fs") is None
    assert dotted_candidate("src/app.js", "#alias/thing") is None
    assert dotted_candidate("src/app.js", "https://cdn/x.js") is None
    assert dotted_candidate("app.js", "../outside") is None


def test_relative_import_resolves_to_a_typescript_module():
    javascript = _adapter().parse_file(b"import s from './shapes';\n", "src/app.js")
    typescript = _typescript_parsed("export const a = 1;\n", "src/shapes.ts")
    index = SymbolIndex.from_parsed_files((javascript, typescript))
    relations = _adapter().resolve_references(javascript, index).relations
    imports = [item for item in relations if item.relation_type == "IMPORTS"]
    assert imports[0].resolution_state == "resolved"
    assert imports[0].target_module_id == typescript.file.module_id


def test_directory_import_falls_back_to_the_index_candidate():
    javascript = _adapter().parse_file(b"import s from './dir';\n", "src/app.js")
    target = _adapter().parse_file(b"export const a = 1;\n", "src/dir/index.js")
    index = SymbolIndex.from_parsed_files((javascript, target))
    relations = _adapter().resolve_references(javascript, index).relations
    imports = [item for item in relations if item.relation_type == "IMPORTS"]
    assert imports[0].target_module_id == target.file.module_id


def test_unmatched_relative_specifier_stays_unresolved_without_prefix_matching():
    javascript = _adapter().parse_file(b"import s from './missing';\n", "src/app.js")
    sibling = _adapter().parse_file(b"export const a = 1;\n", "src/other.js")
    index = SymbolIndex.from_parsed_files((javascript, sibling))
    relations = _adapter().resolve_references(javascript, index).relations
    imports = [item for item in relations if item.relation_type == "IMPORTS"]
    assert imports[0].resolution_state == "unresolved"
    assert imports[0].target_reference == "./missing"


def test_bare_specifier_stays_unresolved():
    javascript = _adapter().parse_file(b"import react from 'react';\n", "src/app.js")
    index = SymbolIndex.from_parsed_files((javascript,))
    relations = _adapter().resolve_references(javascript, index).relations
    imports = [item for item in relations if item.relation_type == "IMPORTS"]
    assert imports[0].resolution_state == "unresolved"
    assert imports[0].target_reference == "react"
```

`_typescript_parsed(source, path)` is a local helper building a
`TypeScriptAdapter("domain", (), parser_version="test-parser")` and calling `parse_file`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/codegraph/test_javascript_adapter.py -k "candidate or resolves or directory or unmatched or bare" -v`
Expected: FAIL — `dotted_candidate` does not exist; imports resolve `unresolved`.

- [ ] **Step 3: Implement**

```python
_SPECIFIER_EXTENSIONS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")


def dotted_candidate(importer_path: str, specifier: str) -> str | None:
    """Normalize a relative specifier into a project dotted module name.

    Returns ``None`` for anything whose target is not decidable from the
    path alone -- bare package names, subpath imports, URLs, Node
    builtins, and paths escaping the repository root. Guessing those
    would mean inventing an edge.
    """
    if not specifier.startswith("."):
        return None
    parts = list(PurePosixPath(importer_path).parent.parts)
    for segment in PurePosixPath(specifier).parts:
        if segment == ".":
            continue
        if segment == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(segment)
    if not parts:
        return None
    tail = parts[-1]
    for extension in _SPECIFIER_EXTENSIONS:
        if tail.casefold().endswith(extension):
            tail = tail[: -len(extension)]
            break
    parts[-1] = tail.split(".", 1)[0]
    return ".".join(parts)
```

`resolve_specifier` probes `candidate` then `candidate + ".index"` against
`index.modules_by_qualified`; the first hit rewrites the reference with
`target_reference=<hit>`, `resolution_hint=None`, `resolution_scope="project"`,
`target_kind_hint="module"`. No hit leaves the reference exactly as extracted
(`resolution_hint="unresolved"`), which also guarantees
`resolver._module_prefix_candidates` is never consulted for it.

`JavaScriptAdapter.resolve_references` rebuilds the reference tuple with
`dataclasses.replace` — `ReferenceRecord` is frozen and `ParsedFile` must not be mutated —
then calls `declaration_relations` and `resolver.resolve_references` as Task 5 wired them.

- [ ] **Step 4: Run the tests, then the full suite and linter**

Run: `uv run pytest tests/codegraph/test_javascript_adapter.py -v && uv run pytest -q && uv run flake8 src tests`
Expected: PASS, no lint output.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/codegraph/languages/javascript.py tests/codegraph/test_javascript_adapter.py
git commit -m "feat(codegraph): resolve relative JavaScript module specifiers"
```

---

### Task 9: INHERITS and CALLS

**Files:**
- Modify: `src/iwiki_mcp/codegraph/languages/javascript.py`
- Test: `tests/codegraph/test_javascript_adapter.py`

**Interfaces:**
- Consumes: `_ecmascript.pending_heritage_references` (with the `resolution_scope`
  parameter from Task 3), `javascript._member_chain`, `javascript.enclosing_symbol_id`.
- Produces:
  - `javascript.call_references(source, root, *, file_record, symbols, aliases)
    -> tuple[ReferenceRecord, ...]`
  - `javascript.import_aliases(source, root) -> Mapping[str, str]` — binding name → dotted
    prefix (`{"shapes": "./shapes"}`), covering both ESM and `require` bindings.

- [ ] **Step 1: Write the failing tests**

```python
def _calls(parsed):
    return {ref.target_reference for ref in parsed.references if ref.relation_type == "CALLS"}


def test_calls_are_extracted_for_plain_and_member_callees():
    source = (
        b"import { helper } from './lib';\n"
        b"function local() { return 1; }\n"
        b"export function run(o) {\n"
        b"  local();\n"
        b"  helper();\n"
        b"  o.a.b();\n"
        b"  new Widget();\n"
        b"  return o[key]();\n"
        b"}\n"
        b"class Widget {}\n"
    )
    parsed = _adapter().parse_file(source, "src/app.js")
    targets = _calls(parsed)
    assert "src.app.local" in targets
    assert "src.lib.helper" in targets
    assert "o.a.b" in targets
    assert "src.app.Widget" in targets
    assert not any("key" in target for target in targets)


def test_call_source_is_the_enclosing_symbol():
    source = b"function outer() { inner(); }\nfunction inner() {}\n"
    parsed = _adapter().parse_file(source, "src/app.js")
    call = next(ref for ref in parsed.references if ref.relation_type == "CALLS")
    outer = next(s for s in parsed.symbols if s.local_name == "outer")
    assert call.source_symbol_id == outer.symbol_id
    assert call.source_module_id is None


def test_type_arguments_pseudo_call_is_not_extracted():
    parsed = _adapter().parse_file(b"const r = a < b > (c);\n", "src/app.js")
    assert _calls(parsed) == set()


def test_require_call_is_not_also_a_call_reference():
    parsed = _adapter().parse_file(b"const x = require('./y');\n", "src/app.js")
    assert _calls(parsed) == set()


def test_jsx_element_produces_no_relation():
    parsed = _adapter().parse_file(b"const A = () => <Child />;\n", "src/a.jsx")
    assert parsed.references == ()


def test_class_extends_local_base_resolves_in_file_scope():
    source = b"class Base {}\nclass Derived extends Base {}\n"
    parsed = _adapter().parse_file(source, "src/app.js")
    inherits = next(r for r in parsed.references if r.relation_type == "INHERITS")
    assert inherits.target_reference == "src.app.Base"
    assert inherits.resolution_scope == "file"


def test_class_extends_imported_base_is_project_scoped():
    source = b"import { Base } from './base';\nclass Derived extends Base {}\n"
    parsed = _adapter().parse_file(source, "src/app.js")
    inherits = next(r for r in parsed.references if r.relation_type == "INHERITS")
    assert inherits.target_reference == "src.base.Base"
    assert inherits.resolution_scope == "project"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/codegraph/test_javascript_adapter.py -k "calls or type_arguments or extends or jsx_element" -v`
Expected: FAIL — no `CALLS` references exist yet.

- [ ] **Step 3: Implement**

`import_aliases` collects binding name → specifier from ESM clauses and `require`
declarators. A target head that is an alias expands to
`dotted_candidate(importer_path, specifier)` — path arithmetic only, no index probe, since
`parse_file` has no index — plus the remaining chain segments; an unexpandable alias leaves
the raw name.

`call_references` walks every `call_expression` / `new_expression` and skips:

- nodes with a `type_arguments` child — the tsx grammar parses the plain-JavaScript
  comparison `a < b > (c)` as exactly that shape, so extracting it would invent an edge;
- a computed callee, a callee that is itself a call, and tagged templates
  (`_member_chain` returns `None`);
- `require(...)` — Task 7 already models it as `IMPORTS`.

Scope selection: alias head → `resolution_scope="project"`; otherwise, if the dotted target
matches a `qualified_name` in this file's symbols (innermost enclosing scope first, then
outward to the module, using `_ecmascript.heritage_scope_candidates`) →
`resolution_scope="file"`; otherwise the raw callee text with
`resolution_hint="unresolved"` and no scope.

For `INHERITS`, call
`_ecmascript.pending_heritage_references(..., resolution_scope="project")` when the
heritage head is an import alias, and let the default `"file"` stand otherwise.

- [ ] **Step 4: Run the tests, TypeScript baselines, full suite, and linter**

Run: `uv run pytest tests/codegraph/test_javascript_adapter.py tests/codegraph/test_typescript_golden.py -v && uv run pytest -q && uv run flake8 src tests`
Expected: PASS, no lint output.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/codegraph/languages/javascript.py tests/codegraph/test_javascript_adapter.py
git commit -m "feat(codegraph): extract JavaScript inheritance and call relations"
```

---

### Task 10: Configuration and server wiring

**Files:**
- Modify: `src/iwiki_mcp/codegraph/config.py:22` (`KNOWN_LANGUAGES`), `:99-102`
  (validation message)
- Modify: `src/iwiki_mcp/server.py:54` (import), `:90-102` (version constants),
  `:110-148` (factory registry)
- Test: `tests/codegraph/test_config_location_models.py`,
  `tests/codegraph/test_server_tools.py`, `tests/codegraph/test_indexer_runtime.py`

**Interfaces:**
- Consumes: `javascript.JavaScriptAdapter` from Task 5.
- Produces: `_code_graph_adapter_factories(...)["javascript"]` — an `AdapterFactory` with
  `extensions=(".js", ".jsx", ".mjs", ".cjs")` and
  `adapter_version="javascript-adapter-v1"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_javascript_is_a_known_language(tmp_path):
    config = _write_config(tmp_path, languages=["python", "javascript"])
    assert load_code_graph_config(tmp_path).languages == ("python", "javascript")


def test_unknown_language_message_lists_javascript(tmp_path):
    _write_config(tmp_path, languages=["ruby"])
    with pytest.raises(CodeGraphConfigError) as excinfo:
        load_code_graph_config(tmp_path)
    assert "python, typescript, javascript" in str(excinfo.value)


def test_javascript_factory_is_registered():
    factories = server._code_graph_adapter_factories("domain")
    factory = factories["javascript"]
    assert factory.extensions == (".js", ".jsx", ".mjs", ".cjs")
    assert factory.adapter_version == "javascript-adapter-v1"
    assert factory.create(()).language == "javascript"


def test_adding_javascript_changes_the_configured_language_fingerprint(tmp_path):
    without = _fingerprint(tmp_path, languages=("python",))
    with_js = _fingerprint(tmp_path, languages=("python", "javascript"))
    assert without != with_js
```

Match each new test to the helpers its existing module already provides (`_write_config`,
`_fingerprint`); read the file before adding, and reuse rather than redefine.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/codegraph/test_config_location_models.py tests/codegraph/test_server_tools.py tests/codegraph/test_indexer_runtime.py -k "javascript" -v`
Expected: FAIL — `code_graph.languages supports only python, typescript`.

- [ ] **Step 3: Implement**

`config.py`:

```python
KNOWN_LANGUAGES = frozenset({"python", "typescript", "javascript"})
```

and the message becomes
`"code_graph.languages supports only python, typescript, javascript"`.

`server.py` imports `javascript as _codegraph_javascript`, adds

```python
    def create_javascript_adapter(source_paths):
        return _codegraph_javascript.JavaScriptAdapter(
            repository_id,
            source_paths,
            parser_version=_TYPESCRIPT_PARSER_VERSION,
        )
```

and registers:

```python
        "javascript": _codegraph_indexer.AdapterFactory(
            create=create_javascript_adapter,
            extensions=(".js", ".jsx", ".mjs", ".cjs"),
            parser_version=_TYPESCRIPT_PARSER_VERSION,
            grammar_version=";".join((
                "tree-sitter:" + _distribution_version("tree-sitter"),
                "tree-sitter-language-pack:"
                + _distribution_version("tree-sitter-language-pack"),
                _TYPESCRIPT_PARSER_VERSION,
            )),
            adapter_version="javascript-adapter-v1",
        ),
```

The tsx grammar artifact is shared with TypeScript, so the version strings are shared too;
`language` / `prefix` / `adapter_version` are what distinguish the two.

- [ ] **Step 4: Run the tests, full suite, and linter**

Run: `uv run pytest -q && uv run flake8 src tests`
Expected: PASS, no lint output.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/codegraph/config.py src/iwiki_mcp/server.py tests/codegraph
git commit -m "feat(codegraph): register JavaScript language in config and server"
```

---

### Task 11: Mixed-language indexing

**Files:**
- Create: `tests/fixtures/codegraph/mixed_python_typescript_javascript/` (Python +
  TypeScript + JavaScript sources, including a Python module and a JavaScript module that
  share a dotted name)
- Modify: `tests/codegraph/test_mixed_language_indexing.py`

**Interfaces:**
- Consumes: the `_build_indexer` helper in that module, Task 10's factory registry.
- Produces: no new production interface.

- [ ] **Step 1: Create the fixture**

```
mixed_python_typescript_javascript/
  service.py            # imports .helpers
  helpers.py
  shared/utils.py       # dotted name "shared.utils"
  shared/utils.js       # SAME dotted name -- the R6.1 collision guard
  shapes.ts             # export const shape = 1;
  app.js                # import { shape } from './shapes'; calls a local function
  legacy.cjs            # const helper = require('./app'); module.exports = {}
  widget.jsx            # export const Widget = () => <div />;
```

- [ ] **Step 2: Write the failing tests**

```python
def test_javascript_rows_carry_their_own_language(tmp_path, monkeypatch):
    tables = _build_tables(tmp_path, monkeypatch,
                           languages=("python", "typescript", "javascript"))
    languages = {row["language"] for row in tables["files"]}
    assert languages == {"python", "typescript", "javascript"}
    extensions = {
        row["path"].rsplit(".", 1)[-1]
        for row in tables["files"] if row["language"] == "javascript"
    }
    assert extensions == {"js", "jsx", "cjs"}


def test_identifiers_do_not_collide_across_languages(tmp_path, monkeypatch):
    tables = _build_tables(tmp_path, monkeypatch,
                           languages=("python", "typescript", "javascript"))
    for table, key in (("symbols", "symbol_id"), ("relations", "relation_id")):
        identifiers = [row[key] for row in tables[table]]
        assert len(identifiers) == len(set(identifiers))


def test_javascript_import_resolves_into_typescript(tmp_path, monkeypatch):
    tables = _build_tables(tmp_path, monkeypatch,
                           languages=("python", "typescript", "javascript"))
    resolved = [
        row for row in tables["relations"]
        if row["relation_type"] == "IMPORTS"
        and row["resolution_state"] == "resolved"
        and row["target_module_id"]
    ]
    assert resolved


def test_python_rows_are_unchanged_by_adding_javascript(tmp_path, monkeypatch):
    without = _build_tables(tmp_path, monkeypatch, languages=("python", "typescript"))
    with_js = _build_tables(tmp_path, monkeypatch,
                            languages=("python", "typescript", "javascript"))
    for table in ("files", "symbols", "relations"):
        before = _rows_for_languages(without, table, {"python", "typescript"})
        after = _rows_for_languages(with_js, table, {"python", "typescript"})
        assert before == after
```

`_rows_for_languages(tables, table, languages)` filters by the row's own `language` for
`files`/`symbols`, and for `relations` by the language prefix of `relation_id`
(`py:` / `ts:` / `js:`), returning a sorted list of dicts.

- [ ] **Step 2b: Run to verify they fail**

Run: `uv run pytest tests/codegraph/test_mixed_language_indexing.py -k "javascript or unchanged" -v`
Expected: FAIL — the fixture-driven build has no JavaScript rows until the fixture and the
factory wiring from Task 10 are both in place; the "unchanged" test is the R6.1 guard.

- [ ] **Step 3: Make them pass**

No production change should be required. If `test_python_rows_are_unchanged_by_adding_javascript`
fails, the cause is a real language-scoping gap in Task 4 — fix `resolver.py`, never the
assertion.

- [ ] **Step 4: Run every baseline, the full suite, and the linter**

Run: `uv run pytest -q && uv run flake8 src tests`
Expected: PASS, no lint output.

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/codegraph/mixed_python_typescript_javascript tests/codegraph/test_mixed_language_indexing.py
git commit -m "test(codegraph): cover mixed Python/TypeScript/JavaScript indexing"
```

---

### Task 12: Documentation, wiki, and release

**Files:**
- Modify: `docs/architecture.md`
- Modify: `README.md`
- Modify: `docs/README.ru.md`
- Modify: `pyproject.toml` (patch version bump)
- Wiki: the bound iwiki page covering code-graph languages

**Interfaces:**
- Consumes: the shipped behaviour of Tasks 5–11.
- Produces: no code interface.

- [ ] **Step 1: Find every place that enumerates code-graph languages**

Run: `rg -n "typescript" README.md docs/README.ru.md docs/architecture.md`
Expected: the code-graph language lists that need JavaScript added.

- [ ] **Step 2: Update `docs/architecture.md`**

Document: the `_ecmascript.py` shared core and the `LanguageProfile` seam; the JavaScript
adapter's identity (`javascript` / `js` / four extensions / tsx grammar, no new
dependency); every JavaScript file being module-backed; relative-specifier resolution with
the `.index` candidate; language-family scoping in `resolver.py` and why it exists.

- [ ] **Step 3: Update `README.md` and `docs/README.ru.md` identically**

Add JavaScript to the code-graph language list with its extensions and its stated limits:
no type inference, no bundler/tsconfig alias resolution, JS→TS edges only (TypeScript
imports stay unresolved), dynamic `require` and computed member access not extracted.
The two files must stay equivalent — English in `README.md`, Russian in `docs/README.ru.md`.

- [ ] **Step 4: Bump the version**

Patch bump in `pyproject.toml`, per the repository's versioning rule.

- [ ] **Step 5: Update the bound wiki**

Apply the iwiki Project Binding protocol, then update the code-graph language page with
`wiki_update_page` (pass the page's current `revision` as `expected_revision`), and run
`wiki_lint`.

- [ ] **Step 6: Final verification**

Run: `uv run pytest -q && uv run flake8 src tests`
Expected: PASS, no lint output.

- [ ] **Step 7: Commit**

```bash
git add docs README.md pyproject.toml
git commit -m "docs(codegraph): document JavaScript support and bump version"
```

---

## Task dependency order

1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12.

Tasks 1 and 2 **must** land before Task 3: they are the pre-refactor baselines. Task 4 is
independent of Tasks 5–9 in code but must precede Task 11, which asserts its guarantee.
Task 10 must precede Task 11, which needs the registered factory.

## Spec coverage map

| Spec | Task |
|---|---|
| R2.1, R2.2, R2.3, R2.4 | 3 (verified by 1) |
| R3.1, R3.2, R3.3 | 5, 10 |
| R4.1 | 5 |
| R4.2, R4.3, R4.4, R4.5 | 6 |
| R5.1, R5.2, R5.6 | 7 |
| R5.3 | 8 |
| R5.4, R5.5, R5.7 | 9 |
| R6.1 | 4 (guarded by 11) |
| R6.2, R6.3, R6.4 | 10 |
| R7.1 | 1, 2 |
| R7.2 | 5, 6, 7, 8, 9 |
| R7.3 | 11 |
| R7.4, R7.6 | 10 |
| R7.5 | 5, 10 |
| R7.7 | every task's final step |
| R8 | 12 |
