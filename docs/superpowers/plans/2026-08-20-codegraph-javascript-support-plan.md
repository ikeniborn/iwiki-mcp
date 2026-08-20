---
chain:
  intent: docs/superpowers/intents/2026-08-20-codegraph-javascript-support-intent.md
  spec: docs/superpowers/specs/2026-08-20-codegraph-javascript-support-design.md
result_check:
  verdict: OK
  plan_hash: c0ed918687cf0b0f
  last_run: 2026-08-20
review:
  plan_hash: c0ed918687cf0b0f
  last_run: 2026-08-20
  phases:
    structure: {status: passed}
    coverage: {status: passed}
    dependencies: {status: passed}
    verifiability: {status: passed}
    consistency: {status: passed}
  findings: []
---
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
- `uv run flake8 src tests` must stay clean (max-line-length 100); there is no formatter,
  so match surrounding style by hand.
- Tests never hit the network.
- Emit no relation on a guess: unresolvable targets keep their raw text with
  `resolution_hint = "unresolved"`.

### HUMAN CHECKPOINT — stop rules

Halt and request approval before proceeding if any task would:

1. **Change TypeScript or Python output** — i.e. produce any diff in
   `tests/codegraph/fixtures/typescript_golden.json` or
   `tests/codegraph/fixtures/mixed_python_typescript_rows.json`. These files are captured
   in Tasks 1–2 from unmodified code and are the intent's health-metric proof. A diff means
   the change is not output-preserving. **Never regenerate a baseline to make a task pass** —
   report the diff and stop. The capture scripts exist only for Tasks 1–2; re-running one
   later is a stop-rule violation, not a fix.
2. **Add a runtime dependency** to `pyproject.toml`.
3. **Touch** `src/iwiki_mcp/codegraph/schema.py` or the publication path.

These are the intent's proposal-first zones; no task resolves one autonomously.

---

### Task 1: TypeScript adapter-level golden baseline

Captures TypeScript's current output **before** any refactor. Every later task keeps this
test green. Must be committed before Task 3 touches `typescript.py`.

**Files:**
- Create: `tests/fixtures/codegraph/typescript_golden/walker.ts`
- Create: `tests/fixtures/codegraph/typescript_golden/shapes.ts`
- Create: `tests/fixtures/codegraph/typescript_golden/view.tsx`
- Create: `tests/codegraph/tools/capture_typescript_golden.py`
- Create: `tests/codegraph/fixtures/typescript_golden.json` (generated once, committed)
- Create: `tests/codegraph/test_typescript_golden.py`

**Interfaces:**
- Consumes: `iwiki_mcp.codegraph.languages.typescript.TypeScriptAdapter`,
  `iwiki_mcp.codegraph.resolver.SymbolIndex`.
- Produces: `tests/codegraph/tools/capture_typescript_golden.py::capture() -> dict` and the
  committed `typescript_golden.json`. The test module has **no** write path — the frozen
  JSON is the contract Task 3 must not break.

- [ ] **Step 1: Create the fixtures exercising every walker branch the refactor touches**

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
export interface Base { id: string; }
export interface Shape extends Base { size: number; }

export namespace Outer {
  export class Base2 {}
  export class Inner extends Base2 {}
}
```

`tests/fixtures/codegraph/typescript_golden/view.tsx` — the `.tsx` path exists so the
grammar-selection seam (`_grammar_name` stays in `typescript.py`, `get_parser` moves) is
baselined too:

```typescript
import { Shape } from "./shapes";

export const View = (props: { shape: Shape }) => <div id="v">{props.shape.size}</div>;

export function identity<T>(value: T): T { return value; }
```

- [ ] **Step 2: Write the capture script (the only writer of the golden file)**

`tests/codegraph/tools/capture_typescript_golden.py`:

```python
"""Capture the pre-refactor TypeScript adapter baseline.

Run once, from unmodified source, in Task 1 of the JavaScript code-graph
plan. It is deliberately NOT importable by the test module: a test that
can rewrite its own baseline is not a baseline.
"""
import dataclasses
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from iwiki_mcp.codegraph.languages.typescript import TypeScriptAdapter  # noqa: E402
from iwiki_mcp.codegraph.resolver import SymbolIndex  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "codegraph" / "typescript_golden"
GOLDEN_PATH = ROOT / "codegraph" / "fixtures" / "typescript_golden.json"
SOURCES = ("walker.ts", "shapes.ts", "view.tsx")


def capture():
    adapter = TypeScriptAdapter("golden-domain", (), parser_version="golden-parser")
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


if __name__ == "__main__":
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(json.dumps(capture(), indent=2, sort_keys=True) + "\n")
    print(f"wrote {GOLDEN_PATH}")
```

- [ ] **Step 3: Write the read-only test**

`tests/codegraph/test_typescript_golden.py`:

```python
"""TypeScript adapter output must match the pre-refactor baseline byte for byte.

A failure here means the refactor changed TypeScript behaviour, which the
intent forbids. Fix the code. Regenerating the baseline is a stop-rule
violation (see the plan's HUMAN CHECKPOINT).
"""
import dataclasses
import json
from pathlib import Path

from iwiki_mcp.codegraph.languages.typescript import TypeScriptAdapter
from iwiki_mcp.codegraph.resolver import SymbolIndex

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "codegraph" / "typescript_golden"
GOLDEN_PATH = Path(__file__).resolve().parent / "fixtures" / "typescript_golden.json"
SOURCES = ("walker.ts", "shapes.ts", "view.tsx")


def _capture():
    adapter = TypeScriptAdapter("golden-domain", (), parser_version="golden-parser")
    parsed = {
        name: adapter.parse_file((FIXTURES / name).read_bytes(), name)
        for name in SOURCES
    }
    index = SymbolIndex.from_parsed_files(parsed.values())
    return {
        name: {
            "file": dataclasses.asdict(item.file),
            "symbols": [dataclasses.asdict(symbol) for symbol in item.symbols],
            "references": [dataclasses.asdict(ref) for ref in item.references],
            "relations": [
                dataclasses.asdict(rel)
                for rel in adapter.resolve_references(item, index).relations
            ],
            "parse_warnings": list(item.warnings),
            "resolve_warnings": list(adapter.resolve_references(item, index).warnings),
        }
        for name, item in parsed.items()
    }


def test_typescript_output_matches_golden_baseline():
    assert _capture() == json.loads(GOLDEN_PATH.read_text())
```

- [ ] **Step 4: Run it to verify it fails (no golden file yet)**

Run: `uv run pytest tests/codegraph/test_typescript_golden.py -v`
Expected: FAIL with `FileNotFoundError` on `typescript_golden.json`.

- [ ] **Step 5: Verify the working tree is clean, then capture**

```bash
git status --porcelain src/iwiki_mcp/codegraph/
uv run python tests/codegraph/tools/capture_typescript_golden.py
```

Expected: the `git status` output is **empty** — capturing from a partially refactored tree
would bake the new behaviour into the baseline and silently void the guard. If it is not
empty, stop and clean the tree first.

- [ ] **Step 6: Run the test again to verify it passes**

Run: `uv run pytest tests/codegraph/test_typescript_golden.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add tests/fixtures/codegraph/typescript_golden tests/codegraph/tools tests/codegraph/test_typescript_golden.py tests/codegraph/fixtures/typescript_golden.json
git commit -m "test(codegraph): capture TypeScript adapter golden baseline pre-refactor"
```

---

### Task 2: Run-level Python + TypeScript baseline

Proves the intent's "Done when" comparison at snapshot-row level.

**Files:**
- Create: `tests/codegraph/tools/capture_mixed_baseline.py`
- Create: `tests/codegraph/fixtures/mixed_python_typescript_rows.json` (generated once,
  committed)
- Create: `tests/codegraph/test_mixed_language_baseline.py`
- Read for reference: `tests/codegraph/test_mixed_language_indexing.py:17-68`

**Interfaces:**
- Consumes: `tests/codegraph/test_mixed_language_indexing.py::_build_indexer(cache_base,
  project_dir, *, languages, adapter_factories=None, exclude=())` — imported, **not**
  copied. It points `project_dir` straight at the fixture directory and builds a
  `CodeGraphConfig(languages=..., exclude=...)` in Python; no `.iwiki.toml` is written or
  read, and nothing is monkeypatched.
- Produces:
  - `tests/codegraph/test_mixed_language_baseline.py::pinned_factories(domain)
    -> dict[str, AdapterFactory]` — version strings pinned so `parser_version` cannot drift
    with an installed dependency bump. Task 12 imports it.
  - `tests/codegraph/test_mixed_language_baseline.py::baseline_rows(tables) -> dict`.

- [ ] **Step 1: Read the existing helper before writing anything**

Run: `sed -n 1,70p tests/codegraph/test_mixed_language_indexing.py`
Expected: `_build_indexer`'s exact signature, its `adapter_factories` parameter, the
`_DOMAIN` constant, `(cache_base / _DOMAIN).mkdir(parents=True)` (note: **no**
`exist_ok=True`, so each call needs its own `cache_base`), and the `build_rows().tables`
usage.

- [ ] **Step 2: Write the shared helpers and the read-only test**

`tests/codegraph/test_mixed_language_baseline.py`:

```python
"""Run-level baseline: Python + TypeScript snapshot rows must not move.

Version strings are pinned rather than read from installed distributions,
so a dependency bump changes no baseline row (only `parser_version` would
drift; identifiers do not hash it).

A failure here means the change perturbed Python or TypeScript output.
Fix the code; regenerating the baseline is a stop-rule violation.
"""
import json
from pathlib import Path

from iwiki_mcp.codegraph import indexer as codegraph_indexer
from iwiki_mcp.codegraph.languages import python as codegraph_python
from iwiki_mcp.codegraph.languages import typescript as codegraph_typescript

from .test_mixed_language_indexing import FIXTURES, _build_indexer

BASELINE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "mixed_python_typescript_rows.json"
)
BASELINE_LANGUAGES = ("python", "typescript")


def pinned_factories(domain):
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


def baseline_rows(tables):
    return {
        table: sorted(
            (dict(row) for row in tables[table]),
            key=lambda row: json.dumps(row, sort_keys=True),
        )
        for table in ("files", "symbols", "relations")
    }


def build_mixed_tables(cache_base, *, languages, factories):
    """Build the mixed fixture once. `cache_base` must be unique per call.

    `_build_indexer` does `(cache_base / _DOMAIN).mkdir(parents=True)` with
    no `exist_ok`, so reusing one directory for two builds raises
    FileExistsError before any assertion runs.
    """
    cache_base.mkdir(parents=True, exist_ok=True)
    indexer = _build_indexer(
        cache_base,
        FIXTURES / "mixed_python_typescript",
        languages=languages,
        adapter_factories=factories,
    )
    return indexer.build_rows().tables


def test_python_typescript_rows_match_baseline(tmp_path):
    tables = build_mixed_tables(
        tmp_path / "baseline",
        languages=BASELINE_LANGUAGES,
        factories=pinned_factories("mixed-domain"),
    )
    assert baseline_rows(tables) == json.loads(BASELINE_PATH.read_text())
```

Read `test_mixed_language_indexing.py` for the real names of `FIXTURES` and the domain
constant that `_build_indexer` expects, and use those exact spellings.

- [ ] **Step 3: Write the capture script**

`tests/codegraph/tools/capture_mixed_baseline.py` is the only writer of the baseline. The
test module uses a *relative* import, so the script must put **both** the repo root and
`src` on `sys.path` and import it by its absolute package path:

```python
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from tests.codegraph.test_mixed_language_baseline import (  # noqa: E402
    BASELINE_LANGUAGES, BASELINE_PATH, baseline_rows, build_mixed_tables,
    pinned_factories,
)

if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        tables = build_mixed_tables(
            Path(tmp) / "baseline",
            languages=BASELINE_LANGUAGES,
            factories=pinned_factories("mixed-domain"),
        )
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(json.dumps(baseline_rows(tables), indent=2, sort_keys=True) + "\n")
    print(f"wrote {BASELINE_PATH}")
```

Use the real domain constant `_DOMAIN` from `test_mixed_language_indexing.py` in place of
the literal `"mixed-domain"` — `_build_indexer` derives its cache path from it.

- [ ] **Step 4: Run to verify the test fails (no baseline file)**

Run: `uv run pytest tests/codegraph/test_mixed_language_baseline.py -v`
Expected: FAIL with `FileNotFoundError`.

- [ ] **Step 5: Verify the tree is clean, capture, and re-run**

```bash
git status --porcelain src/iwiki_mcp/codegraph/
uv run python tests/codegraph/tools/capture_mixed_baseline.py
uv run pytest tests/codegraph/test_mixed_language_baseline.py -v && uv run pytest -q
```

Expected: empty `git status`, then PASS, then the full suite green.

- [ ] **Step 6: Commit**

```bash
git add tests/codegraph/test_mixed_language_baseline.py tests/codegraph/tools/capture_mixed_baseline.py tests/codegraph/fixtures/mixed_python_typescript_rows.json
git commit -m "test(codegraph): capture run-level Python/TypeScript row baseline"
```

---

### Task 3: Extract the shared ECMAScript core

Move plus one extraction, plus profile parameterization. TypeScript behaviour must not
change; Tasks 1–2 are the proof.

**Files:**
- Create: `src/iwiki_mcp/codegraph/languages/_ecmascript.py`
- Modify: `src/iwiki_mcp/codegraph/languages/typescript.py` (whole file restructured)
- Test: `tests/codegraph/test_typescript_golden.py`,
  `tests/codegraph/test_mixed_language_baseline.py`,
  `tests/codegraph/test_typescript_adapter.py` (all existing, all must stay green)

**Interfaces:**
- Consumes: nothing new.
- Produces, in `_ecmascript.py`:
  - `LanguageProfile(language: str, prefix: str, kind_by_node: Mapping[str, str] = {},
    handles_interface: bool = True, handles_namespace: bool = True,
    object_literal_scope: bool = False, declaration_hooks: tuple = ())` — frozen dataclass.
  - `get_parser(grammar: str) -> Any`
  - `text(source: bytes, node) -> str`
  - `relative_path(path: str) -> str`
  - `param_signature(source, params_node) -> str`
  - `return_type_signature(source, return_type_node) -> str`
  - `visibility(name: str) -> str`
  - `PendingHeritage` — frozen dataclass, gains `resolution_scope: str = "file"` and
    `target_reference_override: str | None = None`
  - `pending_heritage_references(source, node, *, owner_symbol_id, source_file_id,
    owner_qualified=None, target_rewriter=None)
    -> tuple[PendingHeritage, ...]` where
    `target_rewriter: Callable[[str], tuple[str, str] | None] | None` maps a raw heritage
    name to `(target_reference, resolution_scope)`, or returns `None` to fall through to
    the scope-candidate probe. TypeScript passes `None` and is unchanged.
  - `make_symbol` (the walker's closure) gains keyword-only `local_name: str | None = None`,
    overriding the name it would otherwise read from `name_node`. TypeScript never passes
    it; Task 6 uses it for quoted object-literal keys.
  - `heritage_scope_candidates(owner_qualified, module_dotted_name) -> tuple[str, ...]`
  - `resolve_heritage_references(pending, qualified_names, module_dotted_name)
    -> tuple[ReferenceRecord, ...]`
  - `import_bindings(source, clause) -> tuple[tuple[str, str], ...]`
  - `esm_import_references(source, root, *, file_record) -> tuple[ReferenceRecord, ...]`
  - `extract_symbols(source, root, *, profile, repository_id, relative_path, file_record,
    module_dotted_name, heritage_rewriter=None)
    -> tuple[tuple[SymbolRecord, ...], tuple[PendingHeritage, ...]]`. The walker forwards
    `heritage_rewriter` to every `pending_heritage_references` call it makes; the rewriter
    is per-file (it closes over the file's import aliases and path), which is why it is a
    call parameter and not a `LanguageProfile` field. TypeScript passes nothing.
  - `dedupe_symbols(symbols) -> tuple[list[SymbolRecord], tuple[str, ...]]`

- [ ] **Step 1: Confirm the baselines are green before touching anything**

Run: `uv run pytest tests/codegraph/test_typescript_golden.py tests/codegraph/test_mixed_language_baseline.py tests/codegraph/test_typescript_adapter.py -q`
Expected: PASS.

- [ ] **Step 2: Create `_ecmascript.py` by moving code verbatim**

Move these from `typescript.py`, dropping the leading underscore for the names listed in
**Interfaces**: `_PARSERS`, `_get_parser`, `_text`, `_relative_path`, `_param_signature`,
`_return_type_signature`, `_visibility`, `_PendingHeritage`,
`_pending_heritage_references`, `_heritage_scope_candidates`,
`_resolve_heritage_references`, `_import_bindings`, `_extract_references` (renamed
`esm_import_references`), `_extract_symbols` (renamed `extract_symbols`, taking its
`_namespace_qualified` inner closure with it).

`_KIND_BY_NODE` is **not** moved: it becomes the TypeScript profile's `kind_by_node`.
`extract_symbols` loses its current `language=` / `prefix=` keyword arguments — both come
from `profile.language` / `profile.prefix`, which feed `file_id` and `symbol_id`.

Do **not** move, per spec R2.1: `_run_tsc_boost`, `_TSC_BOOST_SCRIPT`,
`_TypeScriptParsedFile`, `TypeScriptAdapter._probe_boost_once`, `TypeScriptAdapter`,
`_grammar_name`. Four tests monkeypatch
`iwiki_mcp.codegraph.languages.typescript._run_tsc_boost`
(`test_typescript_adapter.py:422,436,459,481`); moving it breaks them.

`_ecmascript.py` needs these imports: `hashlib`; `dataclass`, `field` from `dataclasses`;
`PurePosixPath`, `PureWindowsPath` from `pathlib`; `Any`, `Callable`, `Mapping` from
`typing`; and from `..models`: `FileRecord`, `ReferenceRecord`, `SymbolRecord`,
`compact_casefold`, `file_id`, `symbol_id`, `token_key`.

Add the profile at the top:

```python
@dataclass(frozen=True)
class LanguageProfile:
    """Per-language switches for the shared ECMAScript walker.

    Every default reproduces TypeScript's current behaviour, so a profile
    built with only language/prefix/kind_by_node drives the walker exactly
    as `typescript.py` did before the extraction.
    """

    language: str
    prefix: str
    kind_by_node: Mapping[str, str] = field(default_factory=dict)
    handles_interface: bool = True
    handles_namespace: bool = True
    object_literal_scope: bool = False
    declaration_hooks: tuple = ()
```

- [ ] **Step 3: Extract the deduplication block into a function**

The dedup logic is currently **inlined** in `TypeScriptAdapter.parse_file`
(`typescript.py:600-625`: the `symbols_by_id` loop, the `duplicate_symbol_ids` set, the
sort, and the `warnings` tuple). Extract it verbatim into:

```python
def dedupe_symbols(symbols):
    """Collapse colliding symbol_ids, keeping the last by start byte.

    Anonymous scopes (IIFEs, callback bodies, if/try blocks) contribute no
    qualified-name segment, so same-named siblings there genuinely collide.
    This guarantees the build never hits the PRIMARY KEY constraint and
    surfaces the degradation instead of hiding it.
    """
```

returning `(symbols_list, warnings_tuple)`. `parse_file` calls it in place. This is an
extraction, not a move — Task 1's golden test is what proves it behaviour-preserving.

- [ ] **Step 4: Parameterize the walker with the profile**

Inside `extract_symbols`:

- `if ctype in _KIND_BY_NODE` becomes `if ctype in profile.kind_by_node`.
- the `interface_declaration` branch runs only `if profile.handles_interface`.
- the `internal_module` and ambient `module` branches run only `if profile.handles_namespace`.
- the `variable_declarator` arm gains a middle branch. Real order today
  (`typescript.py:321-344`) is `if (value is arrow_function|function_expression and
  name_node is not None)` → `else: walk(declarator, owner_qualified)`; insert between them:

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

  With `object_literal_scope=False` the branch short-circuits, so TypeScript keeps its
  exact current path.

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

  Hook contract: `hook(node, owner_qualified, make_symbol, symbols) -> bool`, where
  `make_symbol` is the walker's closure
  `(node, kind, name_node, *, owner_qualified=None, params_node=None,
  return_type_node=None, is_async=False, local_name=None) -> tuple[str, str]`. Hooks get
  no `source` argument — they read node text through `node.text`. TypeScript's profile has
  no hooks, so the loop is empty for it.

  A hook returning `True` stops the walker recursing into that node. That is intended:
  a function nested inside `post: function () {…}` is not extracted, while one inside a
  shorthand `get() {…}` is, because the shorthand goes through the base walker. Neither
  shape is a documented requirement, so no test pins it.

- [ ] **Step 5: Add the per-reference heritage scope and rewriter**

`PendingHeritage` gains `resolution_scope: str = "file"` and
`target_reference_override: str | None = None`.

`pending_heritage_references` gains keyword-only `target_rewriter=None`; for each heritage
target it calls `target_rewriter(name)` when one is supplied and stores the returned
`(target_reference, resolution_scope)` as `target_reference_override` / `resolution_scope`
on the record. A `None` return (or no rewriter) leaves both at their defaults.

`extract_symbols` gains keyword-only `heritage_rewriter=None` and passes it through as
`target_rewriter=` at both of its `pending_heritage_references` call sites (the
`class_declaration` and `interface_declaration` branches). This is the only path by which
`javascript.py` can reach that call — the walker owns it.

`resolve_heritage_references` uses `item.target_reference_override` when present, skipping
the scope-candidate probe entirely, and otherwise behaves exactly as today; it stamps
`item.resolution_scope` instead of the literal `"file"`.

Also add the `local_name` override to `make_symbol`:

```python
    def make_symbol(
        node, kind, name_node, *, owner_qualified=None,
        params_node=None, return_type_node=None, is_async=False,
        local_name=None,
    ):
        local_name = local_name if local_name is not None else text(source, name_node)
```

TypeScript never passes it, so the golden baseline is unaffected.

- [ ] **Step 6: Rewrite `typescript.py` to delegate**

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

`parse_file` calls `_ecmascript.get_parser(_grammar_name(relative_path))`,
`_ecmascript.extract_symbols(..., profile=_TYPESCRIPT_PROFILE, ...)`,
`_ecmascript.dedupe_symbols`, `_ecmascript.resolve_heritage_references`, and
`_ecmascript.esm_import_references`, in the same order and with the same arguments as
today.

- [ ] **Step 7: Run the baselines and the TypeScript suite**

Run: `uv run pytest tests/codegraph/test_typescript_golden.py tests/codegraph/test_typescript_adapter.py tests/codegraph/test_mixed_language_baseline.py -v`
Expected: PASS with no golden diff. A golden failure is a HUMAN CHECKPOINT hit — fix the
move, report the diff; never touch the baseline.

- [ ] **Step 8: Full suite, linter, commit**

```bash
uv run pytest -q && uv run flake8 src tests
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
- Consumes: `FileRecord.language` (`models.py:164`), `models.file_id`, `models.module_id`.
- Produces:
  - `SymbolIndex.languages_by_file_id: Mapping[str, str]` — declared **before**
    `_adapter_evidence`, `field(default_factory=dict)`.
  - `resolver.LANGUAGE_FAMILIES: Mapping[str, frozenset[str]]`
  - `resolver.family_languages(language: str) -> frozenset[str] | None` — `None` means
    "do not filter".
  - `_symbol_candidates(reference, index, language)` and
    `_module_prefix_candidates(target, index, language)` — both gain the third parameter.
    Each has exactly one call site (`resolver.py:270`, `resolver.py:284`).

- [ ] **Step 1: Write the failing tests with their own record builders**

`tests/codegraph/test_resolver.py` has no record factories — every existing test runs the
Python adapter (`_parse(adapter, path, source)` at `:43`). Its import line currently pulls
only `ReferenceRecord, SymbolRecord, token_key`; extend it with `FileRecord`, `ParsedFile`,
`compact_casefold`, `file_id`, and `module_id`. Then add these local helpers and tests:

```python
def _language_file_record(*, language, prefix, path, module_qualified_name):
    local_name = module_qualified_name.rsplit(".", 1)[-1]
    return FileRecord(
        file_id=file_id(language, prefix, "domain", path),
        repository_id="domain",
        path=path,
        path_casefold=compact_casefold(path),
        file_local_name=path.rsplit("/", 1)[-1],
        file_name_tokens_casefold=token_key(path.rsplit("/", 1)[-1]),
        language=language,
        content_hash="0" * 64,
        parser_version="test-parser",
        size_bytes=0,
        start_line=1,
        end_line=1,
        start_byte=0,
        end_byte=0,
        module_key=path,
        module_id=module_id(language, prefix, "domain", path, module_qualified_name),
        module_qualified_name=module_qualified_name,
        module_local_name=local_name,
        module_name_tokens_casefold=token_key(module_qualified_name, local_name),
    )


def _empty_parsed(file):
    return ParsedFile(file=file, symbols=(), references=(), warnings=())


def _module_reference(*, source_file_id, target_reference):
    return ReferenceRecord(
        source_symbol_id=None,
        source_file_id=source_file_id,
        source_module_id=None,
        relation_type="IMPORTS",
        target_reference=target_reference,
        source_line=1,
        source_byte=0,
        source_end_line=1,
        source_end_byte=1,
        binding_name="thing",
        binding_kind="implicit_binding",
        binding_name_tokens_casefold=token_key("thing"),
        target_kind_hint="module",
        resolution_scope="project",
    )
```

Read `models.py` for `FileRecord` / `ReferenceRecord` / `ParsedFile` field order and add
any required field this snippet omits — every persisted constructor parameter is required,
including nullable ones. Then:

```python
def test_python_import_ignores_same_named_javascript_module():
    python_file = _language_file_record(
        language="python", prefix="py", path="src/utils.py",
        module_qualified_name="src.utils",
    )
    javascript_file = _language_file_record(
        language="javascript", prefix="js", path="src/utils.js",
        module_qualified_name="src.utils",
    )
    index = SymbolIndex.from_parsed_files(
        (_empty_parsed(python_file), _empty_parsed(javascript_file))
    )
    reference = _module_reference(
        source_file_id=python_file.file_id, target_reference="src.utils",
    )
    relations = resolve_references("python", "py", "domain", (reference,), index)
    assert len(relations) == 1
    assert relations[0].resolution_state == "resolved"
    assert relations[0].target_module_id == python_file.module_id


def test_javascript_import_resolves_into_a_typescript_module():
    typescript_file = _language_file_record(
        language="typescript", prefix="ts", path="src/shapes.ts",
        module_qualified_name="src.shapes",
    )
    javascript_file = _language_file_record(
        language="javascript", prefix="js", path="src/app.js",
        module_qualified_name="src.app",
    )
    index = SymbolIndex.from_parsed_files(
        (_empty_parsed(typescript_file), _empty_parsed(javascript_file))
    )
    reference = _module_reference(
        source_file_id=javascript_file.file_id, target_reference="src.shapes",
    )
    relations = resolve_references("javascript", "js", "domain", (reference,), index)
    assert relations[0].resolution_state == "resolved"
    assert relations[0].target_module_id == typescript_file.module_id


def test_unknown_language_is_not_filtered():
    other_file = _language_file_record(
        language="ruby", prefix="rb", path="src/thing.rb",
        module_qualified_name="src.thing",
    )
    index = SymbolIndex.from_parsed_files((_empty_parsed(other_file),))
    reference = _module_reference(
        source_file_id="rb-source", target_reference="src.thing",
    )
    relations = resolve_references("ruby", "rb", "domain", (reference,), index)
    assert relations[0].resolution_state == "resolved"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/codegraph/test_resolver.py -k "same_named_javascript or into_a_typescript or unknown_language" -v`
Expected: FAIL — the first test yields `ambiguous` with two candidates.

- [ ] **Step 3: Implement the scoping**

```python
LANGUAGE_FAMILIES = {
    "python": frozenset({"python"}),
    "typescript": frozenset({"typescript", "javascript"}),
    "javascript": frozenset({"javascript", "typescript"}),
}


def family_languages(language: str) -> frozenset[str] | None:
    """Languages whose declarations may satisfy ``language``'s references.

    ``None`` means "apply no filter": an unknown language keeps the
    pre-scoping behaviour, so nothing regresses for callers this map does
    not describe.
    """
    return LANGUAGE_FAMILIES.get(language)
```

`SymbolIndex` gains, immediately before `_adapter_evidence`:

```python
    languages_by_file_id: Mapping[str, str] = field(default_factory=dict)
```

`from_parsed_files` passes
`languages_by_file_id={item.file.file_id: item.file.language for item in parsed}`;
`from_symbols` passes nothing and gets the empty default.

`_symbol_candidates(reference, index, language)` — a symbol whose file language is unknown
is never filtered, because `from_symbols` builds no map:

```python
    allowed = family_languages(language)
    if allowed is not None:
        candidates = tuple(
            item for item in candidates
            if index.languages_by_file_id.get(item.file_id, "") in allowed
            or item.file_id not in index.languages_by_file_id
        )
```

The `exact_modules` lookup (`resolver.py:273-277`) filters on `FileRecord.language`
directly — this is the branch that decides the Python-vs-JavaScript ambiguity:

```python
        allowed = family_languages(language)
        exact_modules = tuple(
            item for item in index.modules_by_qualified.get(target, ())
            if allowed is None or item.language in allowed
        ) if reference.relation_type == "IMPORTS" and not force_unresolved else ()
```

`_module_prefix_candidates` filters **before** the longest match is chosen:

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

- [ ] **Step 4: Run the new tests, then the baselines**

Run: `uv run pytest tests/codegraph/test_resolver.py -v && uv run pytest tests/codegraph/test_typescript_golden.py tests/codegraph/test_mixed_language_baseline.py -q`
Expected: PASS. TypeScript cannot move: every TS reference is either `unresolved` or
file-scoped, and file scope already pins `item.file_id == reference.source_file_id`.

- [ ] **Step 5: Full suite, linter, commit**

```bash
uv run pytest -q && uv run flake8 src tests
git add src/iwiki_mcp/codegraph/resolver.py tests/codegraph/test_resolver.py
git commit -m "feat(codegraph): scope resolution candidates by language family"
```

---

### Task 5: JavaScript adapter — identity, module records, base declarations

**Files:**
- Create: `src/iwiki_mcp/codegraph/languages/javascript.py`
- Create: `tests/codegraph/test_javascript_adapter.py`

**Interfaces:**
- Consumes: everything Task 3 exported from `_ecmascript`;
  `resolver.declaration_relations`, `resolver.resolve_references`, `resolver.sort_relations`.
- Produces:
  - `javascript.JavaScriptAdapter(repository_id: str, source_paths: tuple[str, ...], *,
    parser_version: str = "tree-sitter-typescript")`, with `language = "javascript"`,
    `prefix = "js"`, `extensions = (".js", ".jsx", ".mjs", ".cjs")`.
  - `javascript.JAVASCRIPT_PROFILE` — the `LanguageProfile` instance.
  - `JavaScriptAdapter.parse_file(source: bytes, path: str) -> ParsedFile`
  - `JavaScriptAdapter.resolve_references(parsed, project_index) -> ResolutionResult`

- [ ] **Step 1: Write the failing tests**

`tests/codegraph/test_javascript_adapter.py`:

```python
import pytest

from iwiki_mcp.codegraph.languages.javascript import JavaScriptAdapter
from iwiki_mcp.codegraph.resolver import SymbolIndex


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


def test_private_names_are_marked_private():
    source = b"function _internal() {}\nclass A { #secret() {} }\n"
    parsed = _adapter().parse_file(source, "src/app.js")
    visibility = {s.local_name: s.visibility for s in parsed.symbols}
    assert visibility["_internal"] == "private"
    assert visibility["#secret"] == "private"


def test_anonymous_declarations_emit_no_symbol():
    source = b"export default function () { return 1; }\nconst C = class {};\n"
    parsed = _adapter().parse_file(source, "a.js")
    assert not any(symbol.kind == "class" for symbol in parsed.symbols)


def test_declares_relations_cover_module_and_class_ownership():
    source = b"export class Widget { render() {} }\nexport function go() {}\n"
    parsed = _adapter().parse_file(source, "src/app.js")
    index = SymbolIndex.from_parsed_files((parsed,))
    relations = _adapter().resolve_references(parsed, index).relations
    declares = {
        r.target_symbol_id for r in relations if r.relation_type == "DECLARES"
    }
    by_name = {s.qualified_name: s.symbol_id for s in parsed.symbols}
    assert by_name["src.app.Widget"] in declares
    assert by_name["src.app.go"] in declares
    assert by_name["src.app.Widget.render"] in declares
    method_declare = next(
        r for r in relations
        if r.relation_type == "DECLARES"
        and r.target_symbol_id == by_name["src.app.Widget.render"]
    )
    assert method_declare.source_symbol_id == by_name["src.app.Widget"]


def test_jsx_parses_without_error():
    source = b"export const App = () => <div className='x'><Child {...p} /></div>;\n"
    parsed = _adapter().parse_file(source, "src/App.jsx")
    assert any(symbol.qualified_name == "src.App.App" for symbol in parsed.symbols)


def test_source_must_be_bytes():
    with pytest.raises(TypeError):
        _adapter().parse_file("export const a = 1;", "a.js")
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/codegraph/test_javascript_adapter.py -v`
Expected: FAIL with `ModuleNotFoundError: iwiki_mcp.codegraph.languages.javascript`.

- [ ] **Step 3: Implement the adapter**

```python
JAVASCRIPT_PROFILE = _ecmascript.LanguageProfile(
    language="javascript",
    prefix="js",
    kind_by_node={},
    handles_interface=False,
    handles_namespace=False,
    object_literal_scope=True,
    declaration_hooks=(),          # Task 6 fills this in
)
```

`parse_file` mirrors `typescript.py`'s with three differences:

1. the grammar is always `tsx`: `_ecmascript.get_parser("tsx")`;
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
3. no tsc boost. `resolve_references` is exactly:

```python
    def resolve_references(self, parsed, project_index):
        declares = declaration_relations(
            self.language, self.prefix, self.repository_id, parsed,
        )
        resolved = resolve_references(
            self.language, self.prefix, self.repository_id,
            parsed.references, project_index,
        )
        return ResolutionResult(
            relations=sort_relations((*declares, *resolved)), warnings=(),
        )
```

   (Task 8 inserts specifier rewriting before the `resolve_references` call.) Read
   `typescript.py`'s current call site to confirm the exact argument order before copying.

- [ ] **Step 4: Run the tests, the baselines, the full suite, and the linter**

Run: `uv run pytest tests/codegraph/test_javascript_adapter.py -v && uv run pytest -q && uv run flake8 src tests`
Expected: PASS, no lint output.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/codegraph/languages/javascript.py tests/codegraph/test_javascript_adapter.py
git commit -m "feat(codegraph): add JavaScript adapter with module and base declarations"
```

---

### Task 6: Object-literal and prototype methods

**Files:**
- Modify: `src/iwiki_mcp/codegraph/languages/javascript.py`
- Test: `tests/codegraph/test_javascript_adapter.py`

**Interfaces:**
- Consumes: the hook contract from Task 3.
- Produces:
  - `javascript.object_pair_hook(node, owner_qualified, make_symbol, symbols) -> bool`
  - `javascript.prototype_method_hook(node, owner_qualified, make_symbol, symbols) -> bool`
  - `javascript.member_chain(node) -> tuple[str, ...] | None` — dotted parts of a chain of
    `identifier` / `property_identifier` nodes joined by non-computed `member_expression`s;
    `None` if any link is computed, a call, or a non-identifier. Tasks 9 and 10 reuse it.

Note on node shapes, confirmed by probing the tsx grammar: a shorthand method
(`get() {}`) is a `method_definition` the base walker already claims once
`object_literal_scope` scopes the recursion; a `pair` with a function or arrow value
(`post: function () {}`, `put: () => {}`) has **no** walker branch, which is why
`object_pair_hook` exists. A computed key is `pair › computed_property_name`.

- [ ] **Step 1: Write the failing tests**

```python
def test_object_literal_methods_are_scoped_under_the_declarator():
    source = (
        b"export const api = {\n"
        b"  get(u) { return u; },\n"
        b"  post: function (u) { return u; },\n"
        b"  put: (u) => u,\n"
        b"  'quoted': (u) => u,\n"
        b"  [dynamic]: (u) => u,\n"
        b"  ...spread,\n"
        b"  plain: 1,\n"
        b"};\n"
    )
    parsed = _adapter().parse_file(source, "src/api.js")
    names = {symbol.qualified_name for symbol in parsed.symbols}
    assert {
        "src.api.api.get", "src.api.api.post", "src.api.api.put", "src.api.api.quoted",
    } <= names
    assert not any(name.endswith(".plain") for name in names)
    assert not any("dynamic" in name or "spread" in name for name in names)


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


def test_async_object_literal_method_signature_marks_async():
    parsed = _adapter().parse_file(b"const api = { async get(a) { return a; } };\n", "s.js")
    symbol = next(item for item in parsed.symbols if item.local_name == "get")
    assert symbol.signature == "method|async(a)"


def test_duplicate_symbol_identity_warns():
    source = (
        b"if (x) { function dup() { return 1; } }\n"
        b"else { function dup() { return 2; } }\n"
    )
    parsed = _adapter().parse_file(source, "src/dup.js")
    assert "duplicate_symbol_identity" in parsed.warnings
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/codegraph/test_javascript_adapter.py -k "object_literal or prototype or destructuring or async_object or duplicate" -v`
Expected: FAIL — `pair`-valued members and the prototype method are missing.

- [ ] **Step 3: Implement `member_chain` and both hooks**

```python
def member_chain(node):
    """Dotted parts of a non-computed member chain, or None.

    Returns None for anything whose target is not decidable statically:
    a computed access (`a[k]`), a call in the chain (`f().b`), or any
    non-identifier link. Callers must not guess past a None.
    """
    parts = []
    current = node
    while current.type == "member_expression":
        prop = current.child_by_field_name("property")
        if prop is None or prop.type != "property_identifier":
            return None
        parts.append(prop)
        current = current.child_by_field_name("object")
        if current is None:
            return None
    if current.type != "identifier":
        return None
    parts.append(current)
    return tuple(
        part.text.decode("utf-8", "replace") for part in reversed(parts)
    )
```

`member_chain(node)` is the **only** spelling: the hook contract passes no `source`, so it
reads text through `node.text`. Tasks 9 and 10 consume it under exactly this name and
arity.

```python
def object_pair_hook(node, owner_qualified, make_symbol, symbols):
    """Claim `key: function () {}` / `key: () => {}` inside an object literal.

    Shorthand methods are already claimed by the walker's
    `method_definition` branch; only `pair` nodes need this. Computed keys
    and spread properties are skipped -- their target is not statically
    decidable.
    """
    if node.type != "pair" or owner_qualified is None:
        return False
    key = node.child_by_field_name("key")
    value = node.child_by_field_name("value")
    if key is None or value is None:
        return False
    if key.type not in ("property_identifier", "string"):
        return False
    if value.type not in ("function_expression", "arrow_function"):
        return False
    make_symbol(
        node, "method", key,
        owner_qualified=owner_qualified,
        params_node=value.child_by_field_name("parameters"),
        is_async=any(child.type == "async" for child in value.children),
        local_name=key.text.decode("utf-8", "replace").strip("\"'"),
    )
    return True
```

The `local_name` override is why Task 3 added that parameter: a `string` key's node text
arrives quoted (`b"'quoted'"`), and `make_symbol` would otherwise build the qualified name
`src.api.api.'quoted'`.

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
    parts = member_chain(left)
    if parts is None or len(parts) != 3 or parts[1] != "prototype":
        return False
    owner = next(
        (
            item for item in symbols
            if item.local_name == parts[0] and item.kind in ("class", "function")
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

Then set `declaration_hooks=(object_pair_hook, prototype_method_hook)` in
`JAVASCRIPT_PROFILE`.

- [ ] **Step 4: Run the JavaScript tests, then verify TypeScript did not move**

Run: `uv run pytest tests/codegraph/test_javascript_adapter.py -v && uv run pytest tests/codegraph/test_typescript_golden.py tests/codegraph/test_typescript_adapter.py -q`
Expected: PASS everywhere. A golden diff is a HUMAN CHECKPOINT hit.

- [ ] **Step 5: Full suite, linter, commit**

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
  - `javascript.require_references(source, root, *, file_record, symbols)
    -> tuple[ReferenceRecord, ...]`
  - `javascript.enclosing_symbol_id(symbols, node) -> str | None` — the innermost symbol
    whose byte range contains `node`. Tasks 9 and 10 reuse it.
  - `javascript.attribute(reference, symbols, file_record, node) -> ReferenceRecord`

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


def test_dynamic_bare_and_array_pattern_requires_produce_no_reference():
    source = (
        b"const a = require(name);\n"
        b"require('./side');\n"
        b"const [first] = require('./tuple');\n"
    )
    parsed = _adapter().parse_file(source, "src/app.js")
    assert parsed.references == ()


def test_module_level_reference_sets_module_id_not_symbol_id():
    parsed = _adapter().parse_file(b"import a from './b';\n", "src/app.js")
    reference = parsed.references[0]
    assert reference.source_symbol_id is None
    assert reference.source_module_id == parsed.file.module_id


def test_reference_inside_a_function_sets_symbol_id_not_module_id():
    source = b"function go() { const m = require('./m'); return m; }\n"
    parsed = _adapter().parse_file(source, "src/app.js")
    reference = parsed.references[0]
    go = next(s for s in parsed.symbols if s.local_name == "go")
    assert reference.source_symbol_id == go.symbol_id
    assert reference.source_module_id is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/codegraph/test_javascript_adapter.py -k "esm or require or side_effect or module_level or inside_a_function" -v`
Expected: FAIL — `parsed.references` is empty.

- [ ] **Step 3: Implement**

`parse_file` sets

```python
        references = (
            *_ecmascript.esm_import_references(source, root, file_record=file),
            *require_references(source, root, file_record=file, symbols=symbols),
        )
```

`esm_import_references` needs no attribution wrapper: it only walks `root.children`, so an
ESM import is always module-level and it already sets `source_symbol_id=None` +
`source_module_id=file.module_id` (`typescript.py:416-419`). `require_references` and
`call_references` (Task 10) call `attribute` themselves, because they have the node in hand
and a `require`/call can sit inside a function.

`require_references` walks `lexical_declaration` / `variable_declaration` nodes; for each
`variable_declarator` whose value is a `call_expression` whose `function` field is the
identifier `require` with a single `string` argument, it emits one `ReferenceRecord` per
binding:

- `identifier` name node → one binding, `implicit_binding`;
- `object_pattern` name node → one binding per `shorthand_property_identifier_pattern`
  (`implicit_binding`) and per `pair_pattern` (its value `identifier`, `explicit_alias`);
- `array_pattern` name node → no binding, no reference.

Each record: `relation_type="IMPORTS"`, raw specifier as `target_reference`,
`resolution_hint="unresolved"` (Task 8 rewrites it),
`binding_name_tokens_casefold=token_key(binding_name)`.

Attribution — applies to every JavaScript reference, import and call alike:

```python
def attribute(reference, symbols, file_record, node):
    """Point a reference at its innermost enclosing symbol.

    `source_module_id` must be None whenever `source_symbol_id` is set:
    schema.py enforces
    CHECK (source_module_id IS NULL OR source_symbol_id IS NULL)
    and the snapshot insert aborts otherwise.
    """
    symbol_id = enclosing_symbol_id(symbols, node)
    return dataclasses.replace(
        reference,
        source_symbol_id=symbol_id,
        source_file_id=file_record.file_id,
        source_module_id=file_record.module_id if symbol_id is None else None,
    )
```

- [ ] **Step 4: Run the tests, full suite, linter, commit**

```bash
uv run pytest tests/codegraph/test_javascript_adapter.py -v
uv run pytest -q && uv run flake8 src tests
git add src/iwiki_mcp/codegraph/languages/javascript.py tests/codegraph/test_javascript_adapter.py
git commit -m "feat(codegraph): extract ESM and CommonJS imports for JavaScript"
```

---

### Task 8: Relative-specifier resolution

**Files:**
- Modify: `src/iwiki_mcp/codegraph/languages/javascript.py`
- Test: `tests/codegraph/test_javascript_adapter.py`

**Interfaces:**
- Consumes: `SymbolIndex.modules_by_qualified`.
- Produces:
  - `javascript.dotted_candidate(importer_path: str, specifier: str) -> str | None`
  - `javascript.resolve_specifier(reference, importer_path, index) -> ReferenceRecord`

- [ ] **Step 1: Write the failing tests**

```python
from iwiki_mcp.codegraph.languages.javascript import dotted_candidate
from iwiki_mcp.codegraph.languages.typescript import TypeScriptAdapter


def _typescript_parsed(source, path):
    adapter = TypeScriptAdapter("domain", (), parser_version="test-parser")
    return adapter.parse_file(source, path)


def _imports(adapter, parsed, index):
    relations = adapter.resolve_references(parsed, index).relations
    return [item for item in relations if item.relation_type == "IMPORTS"]


def test_dotted_candidate_normalizes_and_strips_extensions():
    assert dotted_candidate("src/app.js", "./util") == "src.util"
    assert dotted_candidate("src/app.mjs", "./util.js") == "src.util"
    assert dotted_candidate("src/deep/app.js", "../shared/x.ts") == "src.shared.x"
    assert dotted_candidate("src/app.js", "react") is None
    assert dotted_candidate("src/app.js", "node:fs") is None
    assert dotted_candidate("src/app.js", "#alias/thing") is None
    assert dotted_candidate("src/app.js", "https://cdn/x.js") is None
    assert dotted_candidate("app.js", "../outside") is None


def test_relative_import_resolves_to_a_typescript_module():
    adapter = _adapter()
    javascript = adapter.parse_file(b"import s from './shapes';\n", "src/app.js")
    typescript = _typescript_parsed(b"export const a = 1;\n", "src/shapes.ts")
    index = SymbolIndex.from_parsed_files((javascript, typescript))
    imports = _imports(adapter, javascript, index)
    assert imports[0].resolution_state == "resolved"
    assert imports[0].target_module_id == typescript.file.module_id


def test_directory_import_falls_back_to_the_index_candidate():
    adapter = _adapter()
    javascript = adapter.parse_file(b"import s from './dir';\n", "src/app.js")
    target = adapter.parse_file(b"export const a = 1;\n", "src/dir/index.js")
    index = SymbolIndex.from_parsed_files((javascript, target))
    imports = _imports(adapter, javascript, index)
    assert imports[0].target_module_id == target.file.module_id


def test_same_dotted_name_in_js_and_ts_is_ambiguous():
    adapter = _adapter()
    javascript = adapter.parse_file(b"import s from './util';\n", "src/app.js")
    js_target = adapter.parse_file(b"export const a = 1;\n", "src/util.js")
    ts_target = _typescript_parsed(b"export const a = 1;\n", "src/util.ts")
    index = SymbolIndex.from_parsed_files((javascript, js_target, ts_target))
    imports = _imports(adapter, javascript, index)
    assert len(imports) == 2
    assert {item.resolution_state for item in imports} == {"ambiguous"}


def test_unmatched_relative_specifier_stays_unresolved_without_prefix_matching():
    adapter = _adapter()
    javascript = adapter.parse_file(b"import s from './missing';\n", "src/app.js")
    sibling = adapter.parse_file(b"export const a = 1;\n", "src/other.js")
    index = SymbolIndex.from_parsed_files((javascript, sibling))
    imports = _imports(adapter, javascript, index)
    assert imports[0].resolution_state == "unresolved"
    assert imports[0].target_reference == "./missing"


def test_bare_specifier_stays_unresolved():
    adapter = _adapter()
    javascript = adapter.parse_file(b"import react from 'react';\n", "src/app.js")
    index = SymbolIndex.from_parsed_files((javascript,))
    imports = _imports(adapter, javascript, index)
    assert imports[0].resolution_state == "unresolved"
    assert imports[0].target_reference == "react"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/codegraph/test_javascript_adapter.py -k "candidate or resolves_to_a_typescript or directory or ambiguous or unmatched or bare" -v`
Expected: FAIL — `dotted_candidate` does not exist; imports resolve `unresolved`.

- [ ] **Step 3: Implement**

```python
_SPECIFIER_EXTENSIONS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")


def dotted_candidate(importer_path: str, specifier: str) -> str | None:
    """Normalize a relative specifier into a project dotted module name.

    Returns None for anything whose target is not decidable from the path
    alone -- bare package names, subpath imports, URLs, Node builtins, and
    paths escaping the repository root. Guessing those would invent an edge.
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


def resolve_specifier(reference, importer_path, index):
    """Bind a relative specifier to a project module, or leave it alone."""
    candidate = dotted_candidate(importer_path, reference.target_reference or "")
    if candidate is None:
        return reference
    for probe in (candidate, f"{candidate}.index"):
        if probe in index.modules_by_qualified:
            return dataclasses.replace(
                reference,
                target_reference=probe,
                resolution_hint=None,
                resolution_scope="project",
                target_kind_hint="module",
            )
    return reference
```

Leaving an unmatched reference untouched keeps `resolution_hint="unresolved"`, which is
also what guarantees `resolver._module_prefix_candidates` is never consulted for it.

`JavaScriptAdapter.resolve_references` rebuilds the reference tuple before delegating —
`ReferenceRecord` is frozen and `ParsedFile` must not be mutated:

```python
        references = tuple(
            resolve_specifier(item, parsed.file.path, project_index)
            if item.relation_type == "IMPORTS" else item
            for item in parsed.references
        )
```

then passes `references` (not `parsed.references`) to `resolve_references`.

- [ ] **Step 4: Run the tests, full suite, linter, commit**

```bash
uv run pytest tests/codegraph/test_javascript_adapter.py -v
uv run pytest -q && uv run flake8 src tests
git add src/iwiki_mcp/codegraph/languages/javascript.py tests/codegraph/test_javascript_adapter.py
git commit -m "feat(codegraph): resolve relative JavaScript module specifiers"
```

---

### Task 9: Import aliases and INHERITS

**Files:**
- Modify: `src/iwiki_mcp/codegraph/languages/javascript.py`
- Test: `tests/codegraph/test_javascript_adapter.py`

**Interfaces:**
- Consumes: `_ecmascript.pending_heritage_references` with its `resolution_scope` and
  `target_rewriter` parameters (Task 3), `javascript.dotted_candidate` (Task 8).
- Produces:
  - `javascript.ImportAlias` — frozen dataclass `(specifier: str, imported_name: str | None)`
    where `imported_name` is the name in the exporting module for a named import
    (`{ helper }` → `"helper"`, `{ a as b }` → `"a"`), and `None` for a default,
    namespace, or whole-module `require` binding.
  - `javascript.import_aliases(source, root) -> Mapping[str, ImportAlias]` — keyed by the
    **local** binding name, covering both ESM clauses and `require` declarators.
  - `javascript.expand_alias(aliases, importer_path, parts) -> str | None` — dotted target
    for a member chain whose head is an alias, or `None` when the head is not an alias or
    the specifier is not resolvable by path arithmetic.

- [ ] **Step 1: Write the failing tests**

```python
def _inherits(parsed):
    return next(r for r in parsed.references if r.relation_type == "INHERITS")


def test_class_extends_local_base_resolves_in_file_scope():
    source = b"class Base {}\nclass Derived extends Base {}\n"
    parsed = _adapter().parse_file(source, "src/app.js")
    inherits = _inherits(parsed)
    assert inherits.target_reference == "src.app.Base"
    assert inherits.resolution_scope == "file"


def test_class_extends_imported_base_is_project_scoped():
    source = b"import { Base } from './base';\nclass Derived extends Base {}\n"
    parsed = _adapter().parse_file(source, "src/app.js")
    inherits = _inherits(parsed)
    assert inherits.target_reference == "src.base.Base"
    assert inherits.resolution_scope == "project"


def test_class_extends_required_base_is_project_scoped():
    source = b"const { Base } = require('./base');\nclass Derived extends Base {}\n"
    parsed = _adapter().parse_file(source, "src/app.js")
    inherits = _inherits(parsed)
    assert inherits.target_reference == "src.base.Base"
    assert inherits.resolution_scope == "project"


def test_class_extends_bare_import_stays_file_scoped():
    source = b"import { Base } from 'vendor';\nclass Derived extends Base {}\n"
    parsed = _adapter().parse_file(source, "src/app.js")
    inherits = _inherits(parsed)
    assert inherits.target_reference == "src.app.Base"
    assert inherits.resolution_scope == "file"


def test_inherits_resolves_across_files_to_a_typescript_class():
    adapter = _adapter()
    javascript = adapter.parse_file(
        b"import { Base } from './base';\nclass Derived extends Base {}\n", "src/app.js",
    )
    typescript = _typescript_parsed(b"export class Base {}\n", "src/base.ts")
    index = SymbolIndex.from_parsed_files((javascript, typescript))
    relations = adapter.resolve_references(javascript, index).relations
    inherits = next(r for r in relations if r.relation_type == "INHERITS")
    base = next(s for s in typescript.symbols if s.local_name == "Base")
    assert inherits.resolution_state == "resolved"
    assert inherits.target_symbol_id == base.symbol_id
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/codegraph/test_javascript_adapter.py -k "extends or inherits" -v`
Expected: FAIL — the imported-base cases produce `src.app.Base` with `"file"` scope.

- [ ] **Step 3: Implement**

`import_aliases` collects, per local binding name:

- ESM default import → `ImportAlias(specifier, None)`;
- ESM named import (`{ a }` or `{ a as b }`) → `ImportAlias(specifier, "a")`;
- ESM namespace (`* as ns`) → `ImportAlias(specifier, None)`;
- `const x = require("m")` → `ImportAlias("m", None)`;
- `const { a: b } = require("m")` → `ImportAlias("m", "a")`.

`expand_alias(aliases, importer_path, parts)` takes the member chain from
`member_chain`. If `parts[0]` is not an alias, return `None`. Otherwise compute
`module = dotted_candidate(importer_path, alias.specifier)`; a `None` module (bare
specifier) returns `None`. Then join: `alias.imported_name` when present, followed by
`parts[1:]`, all appended to `module`. So `helper()` with `{ helper } from './lib'` in
`src/app.js` gives `src.lib.helper`, and `ns.foo()` with `* as ns from './lib'` gives
`src.lib.foo`.

For heritage, `parse_file` builds the rewriter and hands it to the walker — that is the
only route to the `pending_heritage_references` call, which lives inside `extract_symbols`:

```python
        aliases = import_aliases(source, root)

        def heritage_rewriter(name):
            expanded = expand_alias(aliases, relative_path, (name,))
            if expanded is None:
                return None
            return (expanded, "project")

        symbols, pending_heritage = _ecmascript.extract_symbols(
            source, root,
            profile=JAVASCRIPT_PROFILE,
            repository_id=self.repository_id,
            relative_path=relative_path,
            file_record=file,
            module_dotted_name=module_dotted_name,
            heritage_rewriter=heritage_rewriter,
        )
```

Returning `None` falls through to the shared scope-candidate probe, which is what keeps
the local-base and bare-import cases at `"file"` scope with the module-qualified target.
Note `import_aliases` must run before `extract_symbols`; both read the same parsed tree.

- [ ] **Step 4: Run the tests, the golden baseline, the full suite, linter, commit**

```bash
uv run pytest tests/codegraph/test_javascript_adapter.py tests/codegraph/test_typescript_golden.py -v
uv run pytest -q && uv run flake8 src tests
git add src/iwiki_mcp/codegraph/languages/javascript.py tests/codegraph/test_javascript_adapter.py
git commit -m "feat(codegraph): resolve JavaScript inheritance through import aliases"
```

---

### Task 10: CALLS

**Files:**
- Modify: `src/iwiki_mcp/codegraph/languages/javascript.py`
- Test: `tests/codegraph/test_javascript_adapter.py`

**Interfaces:**
- Consumes: `javascript.member_chain` (Task 6), `javascript.import_aliases` /
  `javascript.expand_alias` (Task 9), `javascript.enclosing_symbol_id` /
  `javascript.attribute` (Task 7), `_ecmascript.heritage_scope_candidates` (Task 3).
- Produces: `javascript.call_references(source, root, *, file_record, symbols, aliases,
  importer_path, module_dotted_name) -> tuple[ReferenceRecord, ...]`.

- [ ] **Step 1: Write the failing tests**

```python
def _calls(parsed):
    return {ref.target_reference for ref in parsed.references if ref.relation_type == "CALLS"}


def test_calls_are_extracted_for_plain_and_member_callees():
    source = (
        b"import { helper } from './lib';\n"
        b"function local() { return 1; }\n"
        b"class Widget {}\n"
        b"export function run(o) {\n"
        b"  local();\n"
        b"  helper();\n"
        b"  o.a.b();\n"
        b"  new Widget();\n"
        b"  return o[key]();\n"
        b"}\n"
    )
    parsed = _adapter().parse_file(source, "src/app.js")
    targets = _calls(parsed)
    assert "src.app.local" in targets
    assert "src.lib.helper" in targets
    assert "o.a.b" in targets
    assert "src.app.Widget" in targets
    assert not any("key" in target for target in targets)


def test_call_scope_selection():
    source = (
        b"import { helper } from './lib';\n"
        b"function local() {}\n"
        b"function run() { helper(); local(); unknownThing(); }\n"
    )
    parsed = _adapter().parse_file(source, "src/app.js")
    by_target = {
        ref.target_reference: ref
        for ref in parsed.references if ref.relation_type == "CALLS"
    }
    assert by_target["src.lib.helper"].resolution_scope == "project"
    assert by_target["src.app.local"].resolution_scope == "file"
    assert by_target["unknownThing"].resolution_hint == "unresolved"
    assert by_target["unknownThing"].resolution_scope is None


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


def test_call_of_call_and_tagged_template_are_skipped():
    source = b"function run() { f()(); tag`text`; }\nfunction f() {}\n"
    parsed = _adapter().parse_file(source, "src/app.js")
    assert _calls(parsed) == {"src.app.f"}


def test_require_call_is_not_also_a_call_reference():
    parsed = _adapter().parse_file(b"const x = require('./y');\n", "src/app.js")
    assert _calls(parsed) == set()


def test_jsx_element_produces_no_relation():
    parsed = _adapter().parse_file(b"const A = () => <Child />;\n", "src/a.jsx")
    assert parsed.references == ()


def test_call_resolves_across_files_to_a_typescript_function():
    adapter = _adapter()
    javascript = adapter.parse_file(
        b"import { helper } from './lib';\nexport function run() { helper(); }\n",
        "src/app.js",
    )
    typescript = _typescript_parsed(b"export function helper() {}\n", "src/lib.ts")
    index = SymbolIndex.from_parsed_files((javascript, typescript))
    relations = adapter.resolve_references(javascript, index).relations
    call = next(r for r in relations if r.relation_type == "CALLS")
    helper = next(s for s in typescript.symbols if s.local_name == "helper")
    assert call.resolution_state == "resolved"
    assert call.target_symbol_id == helper.symbol_id
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/codegraph/test_javascript_adapter.py -k "call or type_arguments or jsx_element" -v`
Expected: FAIL — no `CALLS` references exist.

- [ ] **Step 3: Implement**

```python
def call_references(
    source, root, *, file_record, symbols, aliases, importer_path, module_dotted_name,
):
    """CALLS edges for statically decidable callees only.

    Skipped, deliberately:
      * a `type_arguments` child -- the tsx grammar parses the plain-JS
        comparison chain `a < b > (c)` as a call with type arguments, so
        extracting it would invent an edge that does not exist in JS;
      * a tagged template (``tag`text` ``), which the grammar also shapes
        as a call_expression, but whose `arguments` field is a
        template_string rather than an argument list;
      * a computed callee (`o[k]()`) and a callee that is itself a call
        (`f()()`) -- neither reaches member_chain;
      * `require(...)`, already modelled as IMPORTS.
    """
    references = []
    qualified_names = {symbol.qualified_name for symbol in symbols}
    for node in _walk(root):
        if node.type not in ("call_expression", "new_expression"):
            continue
        if any(child.type == "type_arguments" for child in node.children):
            continue
        arguments = node.child_by_field_name("arguments")
        if arguments is None or arguments.type == "template_string":
            continue
        field = "function" if node.type == "call_expression" else "constructor"
        callee = node.child_by_field_name(field)
        if callee is None or callee.type not in ("identifier", "member_expression"):
            continue
        parts = member_chain(callee)
        if parts is None or parts == ("require",):
            continue
        target, scope, hint = _call_target(
            parts,
            aliases=aliases,
            importer_path=importer_path,
            qualified_names=qualified_names,
            symbols=symbols,
            node=node,
            module_dotted_name=module_dotted_name,
        )
        references.append(attribute(
            _call_reference(node, target, scope, hint, file_record),
            symbols, file_record, node,
        ))
    return tuple(references)
```

`_call_target` applies the ladder:

1. `expand_alias(aliases, importer_path, parts)` returns a dotted target →
   `(target, "project", None)`;
2. otherwise probe the file's own scopes with
   `_ecmascript.heritage_scope_candidates(enclosing_qualified_name, module_dotted_name)`,
   innermost first: the first `f"{scope}.{'.'.join(parts)}"` present in `qualified_names`
   → `(candidate, "file", None)`. `enclosing_qualified_name` is the `qualified_name` of the
   symbol whose `symbol_id` `enclosing_symbol_id(symbols, node)` returns, or `None` at
   module level — `heritage_scope_candidates` already handles `None` by returning just the
   module scope;
3. otherwise `(".".join(parts), None, "unresolved")`.

`_walk(root)` is a depth-first generator over all descendants; `python.py` has one as a
method (`self._walk`, used at `:240,484,632`) to model it on. `_call_reference` builds a
`ReferenceRecord` with `relation_type="CALLS"`, the node's line/byte range, the given
`target` / `resolution_scope` / `resolution_hint`, and no binding fields; its source fields
are placeholders because `attribute` overwrites them immediately.

- [ ] **Step 4: Run the tests, the golden baseline, the full suite, linter, commit**

```bash
uv run pytest tests/codegraph/test_javascript_adapter.py tests/codegraph/test_typescript_golden.py -v
uv run pytest -q && uv run flake8 src tests
git add src/iwiki_mcp/codegraph/languages/javascript.py tests/codegraph/test_javascript_adapter.py
git commit -m "feat(codegraph): extract JavaScript call relations"
```

---

### Task 11: Configuration and server wiring

**Files:**
- Modify: `src/iwiki_mcp/codegraph/config.py:22` (`KNOWN_LANGUAGES`), `:100-102`
  (validation message)
- Modify: `src/iwiki_mcp/server.py:54` (import), `:97-102` (version constants), `:105-149`
  (factory registry)
- Test: `tests/codegraph/test_config_location_models.py`,
  `tests/codegraph/test_server_tools.py`, `tests/codegraph/test_indexer_runtime.py`

**Interfaces:**
- Consumes: `javascript.JavaScriptAdapter` (Task 5).
- Produces: `_code_graph_adapter_factories(...)["javascript"]` — an `AdapterFactory` with
  `extensions=(".js", ".jsx", ".mjs", ".cjs")` and
  `adapter_version="javascript-adapter-v1"`.

- [ ] **Step 1: Write the failing tests using the helpers each module really has**

`test_config_location_models.py` has no `_write_config`; its language tests use
`CodeGraphConfig.from_mapping({...})` (see `test_languages_accepts_typescript` at `:561`).
Follow that pattern:

```python
def test_languages_accepts_javascript():
    config = CodeGraphConfig.from_mapping({"languages": ["python", "javascript"]})
    assert config.languages == ("python", "javascript")


def test_languages_rejects_unknown_language_message_lists_javascript():
    with pytest.raises(CodeGraphConfigError) as excinfo:
        CodeGraphConfig.from_mapping({"languages": ["ruby"]})
    assert "python, typescript, javascript" in str(excinfo.value)
```

`test_indexer_runtime.py` has no `_fingerprint`; it imports `parser_fingerprint` from
`iwiki_mcp.codegraph.fingerprint`, whose signature is fully keyword-only
(`fingerprint.py:88-98`). Compare two direct calls with every other argument held constant:

```python
def test_adding_javascript_changes_the_configured_language_fingerprint():
    common = dict(
        schema_version=2,
        parser_version="p",
        grammar_version="g",
        adapter_version="a",
        resolver_version="r",
        normalizer_version="n",
        unicode_data_version="u",
    )
    without = parser_fingerprint(languages=("python",), **common)
    with_js = parser_fingerprint(languages=("python", "javascript"), **common)
    assert without != with_js
```

Read `fingerprint.py:88-98` and pass the real argument names/values; the point is that only
`languages` differs.

In `test_server_tools.py`:

```python
def test_javascript_factory_is_registered():
    factories = server._code_graph_adapter_factories("domain")
    factory = factories["javascript"]
    assert factory.extensions == (".js", ".jsx", ".mjs", ".cjs")
    assert factory.adapter_version == "javascript-adapter-v1"
    adapter = factory.create(())
    assert adapter.language == "javascript"
    assert adapter.parser_version.startswith("tree-sitter-typescript:")
```

The last assertion pins spec R3.2's version-string format on a factory-built adapter.

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

The tsx grammar artifact is shared with TypeScript, so the version strings are shared;
`language` / `prefix` / `adapter_version` are what distinguish the two.

- [ ] **Step 4: Run the tests, full suite, linter, commit**

```bash
uv run pytest -q && uv run flake8 src tests
git add src/iwiki_mcp/codegraph/config.py src/iwiki_mcp/server.py tests/codegraph
git commit -m "feat(codegraph): register JavaScript language in config and server"
```

---

### Task 12: Mixed-language indexing, search, and context

**Files:**
- Create: `tests/fixtures/codegraph/mixed_python_typescript_javascript/`
- Modify: `tests/codegraph/test_mixed_language_indexing.py`

**Interfaces:**
- Consumes: `_build_indexer` (same module) with its **default** factories
  (`server._code_graph_adapter_factories`) — pinned versions are a Task-2 baseline concern
  and are not needed here — and the module's existing search helpers (it already has
  `test_mixed_repo_search_returns_both_languages` and
  `test_single_language_filter_excludes_the_other_language` — mirror their construction).
- Produces:
  - `test_mixed_language_indexing.py::_ALL_LANGUAGES = ("python", "typescript", "javascript")`
  - `test_mixed_language_indexing.py::_build_tables(cache_base, *, languages)` — wraps
    `_build_indexer` and returns `build_rows().tables`. **`cache_base` must be unique per
    call**: `_build_indexer` does `(cache_base / _DOMAIN).mkdir(parents=True)` with no
    `exist_ok`, so two builds in one test need `tmp_path / "a"` and `tmp_path / "b"`.
  - `test_mixed_language_indexing.py::_rows_for_languages(tables, table, prefixes)` —
    filters by identifier prefix, **not** by a `language` column: only `files` rows carry
    `language`; `SymbolRecord` and `RelationRecord` do not. `symbol_id` and `relation_id`
    are `f"{prefix}:{kind}:{sha256}"` (`models.py:25-28`), so filter `symbols` on
    `row["symbol_id"].startswith(prefix + ":")` and `relations` on
    `row["relation_id"].startswith(prefix + ":")`, and `files` on `row["language"]`.

- [ ] **Step 1: Create the fixture**

```
tests/fixtures/codegraph/mixed_python_typescript_javascript/
  __init__.py           # package evidence, as the existing mixed fixture has
  service.py            # from .helpers import assist
  helpers.py            # def assist(): ...
  shared/__init__.py
  shared/utils.py       # dotted name "shared.utils" -- the collision partner
  shared/utils.js       # SAME dotted name -- the R6.1 guard
  shapes.ts             # export class Shape {}; export function build() {}
  app.js                # import { build, Shape } from './shapes';
                        #   both are imported: `build()` gives a resolved cross-file CALLS,
                        #   `class Panel extends Shape {}` a resolved cross-file INHERITS.
                        #   Also declares and calls a local function, for the file-scoped case.
  esm.mjs               # import { build } from './shapes.js';  -- extension-bearing specifier
  legacy.cjs            # const app = require('./app'); module.exports = {};
  widget.jsx            # export const Widget = () => <div />;
```

Mirror `tests/fixtures/codegraph/mixed_python_typescript/` for the Python side so the
Python adapter derives `shared.utils` the same way; confirm with the existing fixture's
layout before writing.

- [ ] **Step 2: Write the failing tests**

```python
def test_javascript_rows_carry_their_own_language(tmp_path):
    tables = _build_tables(tmp_path / "all", languages=_ALL_LANGUAGES)
    assert {row["language"] for row in tables["files"]} == {
        "python", "typescript", "javascript",
    }
    extensions = {
        row["path"].rsplit(".", 1)[-1]
        for row in tables["files"] if row["language"] == "javascript"
    }
    assert extensions == {"js", "jsx", "cjs", "mjs"}


def test_identifiers_do_not_collide_across_languages(tmp_path):
    tables = _build_tables(tmp_path / "all", languages=_ALL_LANGUAGES)
    for table, key in (("symbols", "symbol_id"), ("relations", "relation_id")):
        identifiers = [row[key] for row in tables[table]]
        assert len(identifiers) == len(set(identifiers))


def test_javascript_import_resolves_into_typescript(tmp_path):
    tables = _build_tables(tmp_path / "all", languages=_ALL_LANGUAGES)
    shapes_module_id = next(
        row["module_id"] for row in tables["files"] if row["path"].endswith("shapes.ts")
    )
    assert any(
        row["relation_id"].startswith("js:")
        and row["relation_type"] == "IMPORTS"
        and row["resolution_state"] == "resolved"
        and row["target_module_id"] == shapes_module_id
        for row in tables["relations"]
    )


def test_mjs_extension_bearing_specifier_resolves(tmp_path):
    tables = _build_tables(tmp_path / "all", languages=_ALL_LANGUAGES)
    esm_file_id = next(
        row["file_id"] for row in tables["files"] if row["path"].endswith("esm.mjs")
    )
    assert any(
        row["source_file_id"] == esm_file_id
        and row["relation_type"] == "IMPORTS"
        and row["resolution_state"] == "resolved"
        for row in tables["relations"]
    )


def test_python_and_typescript_rows_are_unchanged_by_adding_javascript(tmp_path):
    without = _build_tables(tmp_path / "without", languages=("python", "typescript"))
    with_js = _build_tables(tmp_path / "with", languages=_ALL_LANGUAGES)
    assert _rows_for_languages(without, "files", {"python", "typescript"}) == \
        _rows_for_languages(with_js, "files", {"python", "typescript"})
    for table, prefixes in (("symbols", ("py", "ts")), ("relations", ("py", "ts"))):
        assert _rows_for_languages(without, table, prefixes) == \
            _rows_for_languages(with_js, table, prefixes)
```

Plus the search and context coverage the intent's outcomes require — mirror the
construction of the module's existing search tests:

```python
def test_search_filtered_to_javascript_returns_only_javascript_symbols(tmp_path):
    results = _search(tmp_path / "search", query="Widget", languages=["javascript"])
    assert results
    assert all(item.symbol_id.startswith("js:") for item in results)


def test_search_reports_accurate_ranges_for_a_javascript_symbol(tmp_path):
    results = _search(tmp_path / "ranges", query="Widget", languages=["javascript"])
    widget = next(item for item in results if item.local_name == "Widget")
    source = (FIXTURES / "mixed_python_typescript_javascript" / "widget.jsx").read_bytes()
    assert source[widget.start_byte:widget.end_byte].startswith(b"Widget")
    assert widget.start_line >= 1 and widget.end_line >= widget.start_line


```

Context coverage is **Task 13**, not this task: `wiki_code_context` currently rejects
non-Python seeds (`context.py:22`), so it cannot be asserted here.

`_search` wraps the query entry point the module's existing search tests use:
`indexer.build(force=True)` → `with indexer.store.read_lease() as connection:` →
`CodeGraphQuery(_DOMAIN).search(connection, validate_search_request(query,
languages=[...], configured_languages=_ALL_LANGUAGES, limit=...))`. Read those tests and
reuse the same shape rather than inventing one.

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest tests/codegraph/test_mixed_language_indexing.py -k "javascript or mjs or unchanged or search" -v`
Expected: FAIL — the new fixture is not yet indexed with the JavaScript factory.

- [ ] **Step 4: Make them pass**

No production change is expected. If
`test_python_and_typescript_rows_are_unchanged_by_adding_javascript` fails, diff the two
row sets, identify which `_symbol_candidates` / `exact_modules` /
`_module_prefix_candidates` branch admitted the JavaScript candidate, fix `resolver.py`,
and re-run Task 4's tests plus both baselines. Never adjust the assertion, and never remove
the shared `shared.utils` dotted name from the fixture — that collision is the point.

- [ ] **Step 5: Run every baseline, the full suite, and the linter**

Run: `uv run pytest -q && uv run flake8 src tests`
Expected: PASS, no lint output.

- [ ] **Step 6: Commit**

```bash
git add tests/fixtures/codegraph/mixed_python_typescript_javascript tests/codegraph/test_mixed_language_indexing.py tests/codegraph/test_mixed_language_baseline.py
git commit -m "test(codegraph): cover mixed Python/TypeScript/JavaScript indexing"
```

---

### Task 13: Accept non-Python context seeds

`wiki_code_context` rejects every seed that is not `py:` (`context.py:22`), so the intent's
"`wiki_code_context` returns JavaScript relations" outcome is unreachable without this.
Widening the pattern to the registered language prefixes also unblocks TypeScript, which
has been silently unreachable in context since it shipped. Approved during design review as
an addition to the spec's §6.

**Files:**
- Modify: `src/iwiki_mcp/codegraph/context.py:22-24` (`_CANONICAL_ENTITY_ID`)
- Modify: `tests/codegraph/test_context.py` (the existing test that asserts a `ts:` seed is
  rejected must be updated — it encodes the old contract)
- Test: `tests/codegraph/test_context.py`, `tests/codegraph/test_mixed_language_indexing.py`

**Interfaces:**
- Consumes: the JavaScript relations from Tasks 7, 9, 10; the mixed fixture from Task 12.
- Produces: no new name — only a widened `_CANONICAL_ENTITY_ID`.

- [ ] **Step 1: Find the tests that encode the Python-only contract**

Run: `rg -n "CANONICAL_ENTITY_ID|typed entity ID|ts:symbol|py:symbol" src/iwiki_mcp/codegraph/context.py tests/codegraph/test_context.py`
Expected: the regex at `context.py:22-24`, its error path, and the existing test asserting a
non-`py:` seed is rejected.

- [ ] **Step 2: Write the failing tests**

In `tests/codegraph/test_context.py`, update the rejection test so it asserts what is still
true — a seed with an **unregistered** prefix (`rb:symbol:<64 hex>`) or a malformed id is
rejected — and add acceptance:

```python
def test_javascript_and_typescript_seeds_are_accepted():
    digest = "a" * 64
    request = validate_context_request([f"js:symbol:{digest}", f"ts:symbol:{digest}"])
    assert len(request.seeds) == 2


def test_unregistered_language_prefix_is_still_rejected():
    with pytest.raises(CodeGraphContextError):
        validate_context_request(["rb:symbol:" + "a" * 64])
```

In `tests/codegraph/test_mixed_language_indexing.py`, add the end-to-end assertion the
intent's outcome names — note `context()` returns a **dict** whose `"relations"` entries are
plain dicts (`context.py:684-687`), not objects:

```python
def test_context_for_a_javascript_symbol_returns_its_relations(tmp_path):
    result = _context_for(tmp_path / "context", path_suffix="app.js")
    kinds = {row["relation_type"] for row in result["relations"]}
    assert {"DECLARES", "IMPORTS", "CALLS", "INHERITS"} <= kinds
```

`_context_for` builds the mixed snapshot exactly as `_build_tables` does, reads the
`js:symbol:` id of a symbol in `app.js` from the `symbols` table, and calls
`CodeGraphContext(_DOMAIN).context(connection, validate_context_request([seed]))` inside the
same `read_lease()` block the search helper uses. Read `context.py`'s public entry point and
`test_context.py`'s existing construction before writing it.

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest tests/codegraph/test_context.py tests/codegraph/test_mixed_language_indexing.py -k "seed or context" -v`
Expected: FAIL — `CodeGraphContextError: seeds must contain typed entity IDs`.

- [ ] **Step 4: Implement**

```python
_CANONICAL_ENTITY_ID = re.compile(
    r"(?:py|ts|js):(?:file|module|symbol):[0-9a-f]{64}\Z"
)
```

Keep the prefix list in sync with the adapter prefixes registered in `server.py`; add a
comment saying so, in the style of the repository's existing "keep in sync" notes.

- [ ] **Step 5: Run the tests, every baseline, the full suite, and the linter**

Run: `uv run pytest -q && uv run flake8 src tests`
Expected: PASS, no lint output. The Python context path is unchanged — `py:` still matches.

- [ ] **Step 6: Commit**

```bash
git add src/iwiki_mcp/codegraph/context.py tests/codegraph/test_context.py tests/codegraph/test_mixed_language_indexing.py
git commit -m "feat(codegraph): accept TypeScript and JavaScript context seeds"
```

---

### Task 14: Documentation, wiki, and release

**Files:**
- Modify: `docs/architecture.md`, `README.md`, `docs/README.ru.md`, `pyproject.toml`
- Wiki: the bound iwiki page covering code-graph languages

**Interfaces:**
- Consumes: the shipped behaviour of Tasks 5–12.
- Produces: no code interface.

- [ ] **Step 1: Find every place that enumerates code-graph languages**

Run: `rg -n "typescript" README.md docs/README.ru.md docs/architecture.md`
Expected: the code-graph language lists that need JavaScript added.

- [ ] **Step 2: Update `docs/architecture.md`**

Document: the `_ecmascript.py` shared core and the `LanguageProfile` seam; JavaScript's
identity (`javascript` / `js` / four extensions / tsx grammar, no new dependency); every
JavaScript file being module-backed and why; relative-specifier resolution with the
`.index` candidate; language-family scoping in `resolver.py` and the collision it prevents.

- [ ] **Step 3: Update `README.md` and `docs/README.ru.md` identically**

Add JavaScript to the code-graph language list with its four extensions and its limits: no
type inference, no bundler/tsconfig alias resolution, JS→TS edges only (TypeScript imports
stay unresolved), dynamic `require` and computed member access not extracted. English in
`README.md`, Russian in `docs/README.ru.md`, same information.

- [ ] **Step 4: Bump the version**

Patch bump in `pyproject.toml`, per the repository's versioning rule.

- [ ] **Step 5: Update the bound wiki**

Apply the iwiki Project Binding protocol, update the code-graph language page with
`wiki_update_page` (pass the page's current `revision` as `expected_revision`), then run
`wiki_lint` and confirm it reports no new finding.

- [ ] **Step 6: Mechanical documentation check**

```bash
rg -c "javascript|JavaScript" README.md docs/README.ru.md docs/architecture.md
rg -n "\.mjs" README.md docs/README.ru.md docs/architecture.md
rg -n "^version" pyproject.toml
```

Expected: a non-zero count for every file; `.mjs` present in all three; the version line
one patch above the previous value.

- [ ] **Step 7: Final verification and commit**

```bash
uv run pytest -q && uv run flake8 src tests
git add docs README.md pyproject.toml
git commit -m "docs(codegraph): document JavaScript support and bump version"
```

---

## Task dependency order

1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12 → 13 → 14.

Tasks 1 and 2 **must** land before Task 3: they are the pre-refactor baselines, and Task 1
Step 5 verifies the working tree is clean before capturing. Task 4 is independent of
Tasks 5–10 in code but must precede Task 12, which asserts its guarantee. Task 11 must
precede Task 12, which needs the registered factory. Task 13 needs Task 12's fixture and
the relations from Tasks 7/9/10.

## Spec coverage map

| Spec | Implemented by | Tested by |
|---|---|---|
| R2.1, R2.2, R2.3 | 3 | 1, 3 |
| R2.4 | 3 | 1, 2 |
| R3.1 | 5 | 5, 11 |
| R3.2 | 5, 11 | 11 |
| R3.3 | 5 | 5, 12 |
| R4.1 | 5 | 5 |
| R4.2 | 3, 6 | 6 |
| R4.3 | 6 | 6 |
| R4.4 | 6 | 6 |
| R4.5 | 3 | 6 |
| R5.1 | 7 | 7 |
| R5.2 | 7 | 7 |
| R5.3 | 8 | 8, 12 |
| R5.4 | 3, 9 | 9 |
| R5.5 | 10 | 10 |
| R5.6 | 7 | 7 |
| R5.7 | 8, 9, 10 | 8, 10 |
| R6.1 | 4 | 4, 12 |
| R6.2, R6.3 | 11 | 11 |
| R6.4 | 11 | 11 |
| R7.1 | 1, 2 | 1, 2 |
| R7.2 | — | 5, 6, 7, 8, 9, 10 |
| R7.3 | — | 12 |
| R7.4 | 11 | 11, 12 |
| R7.5 | 5, 11 | 5, 11, 12 |
| R7.6 | 11 | 11 |
| R7.7 | — | every task's final step |
| R8 | 14 | 14 |
| Intent outcome: index `.js/.jsx/.mjs/.cjs` | 5, 11 | 12 |
| Intent outcome: search + language filter | 11 | 12 |
| Intent outcome: context relations | 7, 9, 10, 13 | 13 |
| Intent outcome: mixed repo, no collisions | 4, 11 | 12 |

Task 13 is an addition to the spec's §6, approved during design review: the intent's
context outcome is unreachable without it, because `wiki_code_context` accepts only `py:`
seeds today. Fold it back into the spec before `/check-chain result`.
