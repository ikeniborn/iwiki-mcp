# codegraph-typescript-support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add TypeScript/TSX support to the code graph engine — a new `TypeScriptAdapter`
plus lifting four python-only literal gates — so `wiki_code_index` indexes `.ts`/`.tsx`
alongside `.py` in one unified graph, and `wiki_code_search`/`wiki_code_context` return
correct results for both languages.

**Architecture:** Approach A (single unified graph, registry-driven languages) — see
spec §0. The indexer (`indexer.py::CodeGraphIndexer`) is already language-registry-driven
(`adapter_factories: Mapping[str, AdapterFactory]`); most of the pipeline needs zero
change. The real work is: (1) a new `TypeScriptAdapter` implementing the existing
`LanguageAdapter` protocol, reusing the language-neutral `resolver.py` functions the
same way `PythonAdapter` does; (2) lifting four literal `("python",)`/`!= "python"`
gates to a shared `KNOWN_LANGUAGES` registry check; (3) generalizing `query.py`'s
single-language search request to a language set.

**Tech Stack:** Python 3.10+, `tree-sitter` + `tree-sitter-language-pack` (online grammar)
with a packaged `tree-sitter-typescript` offline fallback (mirrors `tree-sitter-python`),
`pytest`.

**Spec:** [docs/superpowers/specs/2026-08-18-codegraph-typescript-support-design.md](../specs/2026-08-18-codegraph-typescript-support-design.md)

## Global Constraints

- Tree-sitter-only static AST analysis — never execute, compile, or evaluate TS/JS
  source (intent hard constraint).
- The Tree-sitter baseline must work with zero external dependencies, exactly like
  `PythonAdapter` (intent hard constraint).
- The optional type-resolution boost (`code_graph.typescript_type_boost`) defaults to
  `False` and is never enabled by default; its absence/failure/timeout never blocks or
  degrades the Tree-sitter baseline result (intent hard/no-go constraints).
- `LanguageAdapter` protocol (`languages/base.py`) is unchanged.
- The identity scheme (SHA-256 hash + adapter-supplied prefix, `models.py`) is
  unchanged; `TypeScriptAdapter.prefix = "ts"`.
- Every existing Python-only test must stay green unmodified — this is the health-metric
  regression gate from the intent. Run `uv run pytest -q` after every task.
- `flake8` (`max-line-length = 100`) must stay clean: `uv run flake8 src tests`.
- Bump `pyproject.toml` `version` (patch bump) once, in the final docs/versioning task
  (see `CLAUDE.md` Versioning).

---

### Task 1: `KNOWN_LANGUAGES` registry, config generalization, and the TypeScript grammar dependency

**Files:**
- Modify: `pyproject.toml` (dependency list, ~line 22)
- Modify: `src/iwiki_mcp/codegraph/config.py`
- Test: `tests/codegraph/test_config.py` (existing file — add cases; if it does not
  exist, check `tests/` for the actual config test file name with
  `grep -rl "_languages\|CodeGraphConfig" tests/` before creating a new one)

**Interfaces:**
- Produces: `KNOWN_LANGUAGES: frozenset[str]` in `config.py`, importable by
  `runtime.py`, `sqlite_adapter.py`, `query.py`. Value: `frozenset({"python", "typescript"})`.
- Produces: `CodeGraphConfig.typescript_type_boost: bool = False` (new dataclass field).

- [ ] **Step 1: Confirm the actual config test file**

```bash
grep -rl "_languages\|CodeGraphConfig" tests/ | grep -v __pycache__
```

Use the file this prints (there is exactly one) for every test step below instead of
`tests/codegraph/test_config.py` if the name differs.

- [ ] **Step 2: Write the failing tests**

```python
def test_languages_accepts_typescript():
    config = CodeGraphConfig.from_mapping({"languages": ["python", "typescript"]})
    assert config.languages == ("python", "typescript")


def test_languages_rejects_unknown_language():
    with pytest.raises(CodeGraphConfigError, match="languages"):
        CodeGraphConfig.from_mapping({"languages": ["python", "ruby"]})


def test_typescript_type_boost_defaults_false():
    config = CodeGraphConfig.from_mapping({})
    assert config.typescript_type_boost is False


def test_typescript_type_boost_rejects_non_bool():
    with pytest.raises(CodeGraphConfigError, match="typescript_type_boost"):
        CodeGraphConfig.from_mapping({"typescript_type_boost": "yes"})
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/codegraph/test_config.py -k typescript -v`
Expected: FAIL — `typescript` is rejected by the current `_languages()` literal check,
and `typescript_type_boost` is an unknown field.

- [ ] **Step 4: Implement**

In `src/iwiki_mcp/codegraph/config.py`:

```python
KNOWN_LANGUAGES = frozenset({"python", "typescript"})
```

Replace `_languages()`:

```python
def _languages(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise CodeGraphConfigError("code_graph.languages must be a non-empty array")
    result = tuple(value)
    if any(
        type(item) is not str or item not in KNOWN_LANGUAGES
        for item in result
    ):
        raise CodeGraphConfigError(
            "code_graph.languages supports only python, typescript"
        )
    return result
```

Add `"typescript_type_boost"` to `_FIELDS`. Add the field to `CodeGraphConfig`
(after `read_mode`, before `max_snapshot_age_seconds` — grouping is cosmetic, exact
position does not matter): `typescript_type_boost: bool = False`. In `__post_init__`,
validate it the same way as `enabled`:

```python
object.__setattr__(
    self,
    "typescript_type_boost",
    _bool(self.typescript_type_boost, "typescript_type_boost"),
)
```

In `pyproject.toml`, add the offline-fallback grammar dependency next to
`tree-sitter-python`:

```toml
    "tree-sitter-python>=0.25.0",
    "tree-sitter-typescript>=0.23.0",
```

- [ ] **Step 5: Run `uv sync` then the tests to verify they pass**

```bash
uv sync --extra dev
uv run pytest tests/codegraph/test_config.py -v
```

Expected: PASS, all tests including the pre-existing python-only ones.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/iwiki_mcp/codegraph/config.py tests/codegraph/test_config.py
git commit -m "feat(codegraph): accept typescript in code_graph.languages"
```

---

### Task 2: `TypeScriptAdapter` skeleton — grammar dispatch and empty-file parity

**Files:**
- Create: `src/iwiki_mcp/codegraph/languages/typescript.py`
- Create: `tests/codegraph/test_typescript_adapter.py`
- Create: `tests/fixtures/codegraph/typescript_basic/` (empty dir placeholder is fine
  for this task; later tasks add fixture files)

**Interfaces:**
- Consumes: `iwiki_mcp.codegraph.models.{FileRecord, ParsedFile, ResolutionResult,
  SymbolRecord, ReferenceRecord, compact_casefold, file_id, module_id, symbol_id,
  token_key}` (unchanged, from Task-1-independent existing code).
- Consumes: `iwiki_mcp.codegraph.resolver.{declaration_relations, resolve_references,
  sort_relations}` (unchanged, existing language-neutral helpers `PythonAdapter`
  already uses the same way).
- Produces: `TypeScriptAdapter` class with `language = "typescript"`, `prefix = "ts"`,
  `extensions = (".ts", ".tsx")`, `parse_file(source: bytes, path: str) -> ParsedFile`,
  `resolve_references(parsed, project_index) -> ResolutionResult` — the exact
  `LanguageAdapter` protocol shape from `languages/base.py`.

- [ ] **Step 1: Write the failing tests**

```python
"""Contract tests for static TypeScript/TSX declaration extraction."""
from __future__ import annotations

import pytest

from iwiki_mcp.codegraph.languages.typescript import TypeScriptAdapter
from iwiki_mcp.codegraph.models import file_id


def test_adapter_identity():
    assert TypeScriptAdapter.language == "typescript"
    assert TypeScriptAdapter.prefix == "ts"
    assert TypeScriptAdapter.extensions == (".ts", ".tsx")


def test_empty_file_produces_valid_file_record():
    adapter = TypeScriptAdapter("domain", ("a.ts",), parser_version="test")

    parsed = adapter.parse_file(b"", "a.ts")

    expected_file_id = file_id("typescript", "ts", "domain", "a.ts")
    assert parsed.file.file_id == expected_file_id
    assert parsed.file.repository_id == "domain"
    assert parsed.file.language == "typescript"
    assert parsed.file.path == "a.ts"
    assert parsed.file.start_line == 1
    assert parsed.file.end_line == 1
    assert parsed.file.module_id is None  # no import/export -> not an ES module
    assert parsed.symbols == ()
    assert parsed.references == ()


def test_tsx_extension_uses_tsx_grammar():
    adapter = TypeScriptAdapter("domain", ("a.tsx",), parser_version="test")

    parsed = adapter.parse_file(b"", "a.tsx")

    assert parsed.file.language == "typescript"


def test_source_must_be_bytes():
    adapter = TypeScriptAdapter("domain", (), parser_version="test")
    with pytest.raises(TypeError):
        adapter.parse_file("not bytes", "a.ts")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/codegraph/test_typescript_adapter.py -v`
Expected: FAIL — `iwiki_mcp.codegraph.languages.typescript` does not exist yet.

- [ ] **Step 3: Implement the skeleton**

```python
"""Tree-sitter-only TypeScript/TSX declaration extraction."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from ..models import (
    FileRecord,
    ParsedFile,
    ResolutionResult,
    compact_casefold,
    file_id,
    module_id,
    token_key,
)
from ..resolver import declaration_relations, resolve_references, sort_relations


_PARSERS: dict[str, Any] = {}


def _relative_path(path: str) -> str:
    """Keep only a safe POSIX source-relative spelling, never an absolute path."""
    if not isinstance(path, str) or not path or "\\" in path:
        raise ValueError("invalid source path")
    windows_path = PureWindowsPath(path)
    posix_path = PurePosixPath(path)
    if (
        windows_path.is_absolute()
        or windows_path.drive
        or posix_path.is_absolute()
        or ".." in posix_path.parts
    ):
        raise ValueError("invalid source path")
    return "/".join(part for part in posix_path.parts if part != ".")


def _grammar_name(path: str) -> str:
    return "tsx" if path.casefold().endswith(".tsx") else "typescript"


def _get_parser(grammar: str) -> Any:
    parser = _PARSERS.get(grammar)
    if parser is not None:
        return parser
    from tree_sitter import Language, Parser

    try:
        from tree_sitter_language_pack import get_parser
        parser = get_parser(grammar)
    except Exception:
        import tree_sitter_typescript as ts_typescript

        capsule = (
            ts_typescript.language_tsx()
            if grammar == "tsx"
            else ts_typescript.language_typescript()
        )
        parser = Parser(Language(capsule))
    _PARSERS[grammar] = parser
    return parser


@dataclass(frozen=True)
class _TypeScriptParsedFile(ParsedFile):
    pass


class TypeScriptAdapter:
    language = "typescript"
    prefix = "ts"
    extensions = (".ts", ".tsx")

    def __init__(
        self,
        repository_id: str,
        source_paths: tuple[str, ...],
        *,
        parser_version: str = "tree-sitter-typescript",
    ) -> None:
        if not isinstance(repository_id, str) or not repository_id:
            raise ValueError("invalid repository id")
        self.repository_id = repository_id
        self.parser_version = parser_version

    def parse_file(self, source: bytes, path: str) -> ParsedFile:
        if not isinstance(source, bytes):
            raise TypeError("source must be bytes")
        relative_path = _relative_path(path)
        content_hash = hashlib.sha256(source).hexdigest()
        stable_file_id = file_id(
            self.language, self.prefix, self.repository_id, relative_path,
        )
        parser = _get_parser(_grammar_name(relative_path))
        tree = parser.parse(source)
        root = tree.root_node

        is_module = any(
            child.type in ("import_statement", "export_statement")
            for child in root.children
        )
        module_local_name = PurePosixPath(relative_path).stem if is_module else None
        stable_module_id = (
            module_id(
                self.language, self.prefix, self.repository_id,
                relative_path, relative_path,
            )
            if is_module else None
        )

        file = FileRecord(
            file_id=stable_file_id,
            repository_id=self.repository_id,
            path=relative_path,
            path_casefold=compact_casefold(relative_path),
            file_local_name=PurePosixPath(relative_path).name,
            file_name_tokens_casefold=token_key(PurePosixPath(relative_path).name),
            language=self.language,
            content_hash=content_hash,
            parser_version=self.parser_version,
            size_bytes=len(source),
            start_line=1,
            end_line=max(1, source.count(b"\n") + 1),
            start_byte=0,
            end_byte=len(source),
            module_key=relative_path,
            module_id=stable_module_id,
            module_qualified_name=relative_path if is_module else None,
            module_local_name=module_local_name,
            module_name_tokens_casefold=(
                token_key(module_local_name) if module_local_name else None
            ),
        )
        return _TypeScriptParsedFile(
            file=file, symbols=(), references=(), warnings=(),
        )

    def resolve_references(self, parsed, project_index) -> ResolutionResult:
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

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/codegraph/test_typescript_adapter.py -v`
Expected: PASS. If `_get_parser` fails on both the language-pack path and the
`tree_sitter_typescript` fallback in this sandbox (no network for the pack, and the
fallback package was only added as a dependency in Task 1 — confirm
`uv run python3 -c "import tree_sitter_typescript"` succeeds first; if it does not,
`uv sync` did not pick it up and Task 1 Step 5 must be re-run), fix the fallback import
before proceeding — every later task depends on a working parser here.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/codegraph/languages/typescript.py tests/codegraph/test_typescript_adapter.py
git commit -m "feat(codegraph): add TypeScriptAdapter skeleton with grammar dispatch"
```

---

### Task 3: Declaration extraction — functions, classes, interfaces, type aliases, enums, methods

**Files:**
- Modify: `src/iwiki_mcp/codegraph/languages/typescript.py`
- Modify: `tests/codegraph/test_typescript_adapter.py`
- Modify: `src/iwiki_mcp/codegraph/models.py` (widen `SymbolRecord.kind` and
  `SearchResult`/`ContextNode.kind` `Literal` to add `"interface"`, `"type_alias"`,
  `"enum"` — cosmetic type-annotation widening only, no runtime behavior change)
- Modify: `src/iwiki_mcp/codegraph/query.py` (`KNOWN_ENTITY_KINDS` gains the three
  new kind strings — this one is load-bearing: `validate_search_request` rejects an
  unknown `kind` filter against this set)

**Interfaces:**
- Consumes: `TypeScriptAdapter.parse_file` from Task 2 (extends its body, same
  signature).
- Produces: `parsed.symbols` populated with `SymbolRecord` entries of `kind` in
  `{"function", "async_function", "method", "class", "interface", "type_alias", "enum"}`.
  Signature format mirrors Python's `"kind|modifier(params)->returntype"` pattern,
  e.g. `"function|(name:string)->string"`, `"method|(value:number)->void"`; interfaces/
  type aliases/enums have `signature=None` (no call signature).

- [ ] **Step 1: Write the failing tests**

```python
def test_function_declaration_extracted():
    source = b"export function greet(name: string): string {\n  return name;\n}\n"
    adapter = TypeScriptAdapter("domain", ("a.ts",), parser_version="test")

    parsed = adapter.parse_file(source, "a.ts")

    by_name = {symbol.qualified_name: symbol for symbol in parsed.symbols}
    assert "a.ts/greet" in by_name
    symbol = by_name["a.ts/greet"]
    assert symbol.kind == "function"
    assert symbol.local_name == "greet"
    assert symbol.visibility == "public"


def test_class_with_method_extracted():
    source = (
        b"export class Animal {\n"
        b"  speak(sound: string): void {}\n"
        b"}\n"
    )
    adapter = TypeScriptAdapter("domain", ("a.ts",), parser_version="test")

    parsed = adapter.parse_file(source, "a.ts")

    kinds = {symbol.qualified_name: symbol.kind for symbol in parsed.symbols}
    assert kinds["a.ts/Animal"] == "class"
    assert kinds["a.ts/Animal.speak"] == "method"


def test_interface_type_alias_enum_extracted():
    source = (
        b"interface Named { name: string }\n"
        b"type Alias = string | number;\n"
        b"enum Color { Red, Green, Blue }\n"
    )
    adapter = TypeScriptAdapter("domain", ("a.ts",), parser_version="test")

    parsed = adapter.parse_file(source, "a.ts")

    kinds = {symbol.qualified_name: symbol.kind for symbol in parsed.symbols}
    assert kinds["a.ts/Named"] == "interface"
    assert kinds["a.ts/Alias"] == "type_alias"
    assert kinds["a.ts/Color"] == "enum"


def test_arrow_function_const_extracted():
    source = b"export const add = (a: number, b: number): number => a + b;\n"
    adapter = TypeScriptAdapter("domain", ("a.ts",), parser_version="test")

    parsed = adapter.parse_file(source, "a.ts")

    by_name = {symbol.qualified_name: symbol for symbol in parsed.symbols}
    assert "a.ts/add" in by_name
    assert by_name["a.ts/add"].kind == "function"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/codegraph/test_typescript_adapter.py -k "declaration or class_with or interface or arrow" -v`
Expected: FAIL — extraction is not implemented yet, `parsed.symbols` is always `()`.

- [ ] **Step 3: Implement**

Widen the `Literal` annotations first (cosmetic, do this before the extraction code so
nothing has to be reverted):

In `src/iwiki_mcp/codegraph/models.py`, change:
```python
kind: Literal["class", "function", "async_function", "method"]
```
(on `SymbolRecord`) to:
```python
kind: Literal[
    "class", "function", "async_function", "method",
    "interface", "type_alias", "enum",
]
```
Apply the same widening to the `kind` field on `SearchResult` and `ContextNode`
(currently `Literal["file", "module", "class", "function", "async_function", "method"]`).

In `src/iwiki_mcp/codegraph/query.py`, add the three kinds to `KNOWN_ENTITY_KINDS`:
```python
KNOWN_ENTITY_KINDS = frozenset({
    "async_function",
    "class",
    "enum",
    "file",
    "function",
    "interface",
    "method",
    "module",
    "type_alias",
})
```

In `typescript.py`, add a symbol-walking pass to `parse_file` (called after computing
`file`, before constructing the return value):

```python
from ..models import SymbolRecord, symbol_id


_KIND_BY_NODE = {
    "function_declaration": "function",
    "interface_declaration": "interface",
    "type_alias_declaration": "type_alias",
    "enum_declaration": "enum",
}


def _text(source: bytes, node) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _param_signature(source: bytes, params_node) -> str:
    if params_node is None:
        return "()"
    return _text(source, params_node)


def _return_type_signature(source: bytes, return_type_node) -> str:
    if return_type_node is None:
        return ""
    return "->" + _text(source, return_type_node).lstrip(":").strip()


def _visibility(name: str) -> str:
    return "private" if name.startswith("_") or name.startswith("#") else "public"


def _extract_symbols(
    source: bytes,
    root,
    *,
    language: str,
    prefix: str,
    repository_id: str,
    relative_path: str,
):
    symbols: list[SymbolRecord] = []

    def make_symbol(node, kind, name_node, *, owner_qualified=None,
                     params_node=None, return_type_node=None, is_async=False):
        local_name = _text(source, name_node)
        qualified = (
            f"{owner_qualified}.{local_name}" if owner_qualified
            else f"{relative_path}/{local_name}"
        )
        record_kind = kind
        signature = None
        if kind in ("function", "method"):
            record_kind = "async_function" if is_async and kind == "function" else kind
            signature = (
                f"{record_kind}|{'async' if is_async else ''}"
                f"{_param_signature(source, params_node)}"
                f"{_return_type_signature(source, return_type_node)}"
            )
        stable_id = symbol_id(
            language, prefix, repository_id, relative_path,
            qualified, signature or "",
        )
        symbols.append(SymbolRecord(
            symbol_id=stable_id,
            file_id=file_id(language, prefix, repository_id, relative_path),
            kind=record_kind,
            qualified_name=qualified,
            local_name=local_name,
            name_tokens_casefold=token_key(local_name),
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            start_byte=node.start_byte,
            end_byte=node.end_byte,
            signature=signature,
            signature_casefold=compact_casefold(signature),
            visibility=_visibility(local_name),
            content_hash=hashlib.sha256(
                source[node.start_byte:node.end_byte]
            ).hexdigest(),
            metadata_json="{}",
        ))
        return qualified, stable_id

    def walk(node, owner_qualified=None):
        for child in node.children:
            ctype = child.type
            if ctype in _KIND_BY_NODE:
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    make_symbol(child, _KIND_BY_NODE[ctype], name_node,
                                owner_qualified=owner_qualified)
                walk(child, owner_qualified)
            elif ctype == "class_declaration":
                name_node = child.child_by_field_name("name")
                if name_node is None:
                    walk(child, owner_qualified)
                    continue
                qualified, _stable_id = make_symbol(
                    child, "class", name_node, owner_qualified=owner_qualified,
                )
                body = child.child_by_field_name("body")
                if body is not None:
                    walk(body, qualified)
            elif ctype == "method_definition":
                name_node = child.child_by_field_name("name")
                if name_node is not None and owner_qualified is not None:
                    make_symbol(
                        child, "method", name_node,
                        owner_qualified=owner_qualified,
                        params_node=child.child_by_field_name("parameters"),
                        return_type_node=child.child_by_field_name("return_type"),
                        is_async=any(
                            grandchild.type == "async"
                            for grandchild in child.children
                        ),
                    )
                walk(child, owner_qualified)
            elif ctype == "function_declaration":
                pass  # handled by _KIND_BY_NODE above; keep branch order documented
            elif ctype in ("lexical_declaration", "variable_declaration"):
                for declarator in child.children:
                    if declarator.type != "variable_declarator":
                        continue
                    value = declarator.child_by_field_name("value")
                    name_node = declarator.child_by_field_name("name")
                    if (
                        value is not None
                        and value.type in ("arrow_function", "function_expression")
                        and name_node is not None
                    ):
                        make_symbol(
                            declarator, "function", name_node,
                            owner_qualified=owner_qualified,
                            params_node=value.child_by_field_name("parameters"),
                            return_type_node=value.child_by_field_name("return_type"),
                            is_async=any(
                                grandchild.type == "async"
                                for grandchild in value.children
                            ),
                        )
                walk(child, owner_qualified)
            else:
                walk(child, owner_qualified)

    walk(root)
    return tuple(symbols)
```

Fix the `function_declaration` handling: it is already covered by the generic
`_KIND_BY_NODE` branch above, but that branch does not pass `params_node`/
`return_type_node`/`is_async`, so plain `function_declaration` symbols get
`signature=None` too. Fix by giving `function_declaration` its own branch instead of
routing it through `_KIND_BY_NODE`:

```python
_KIND_BY_NODE = {
    "interface_declaration": "interface",
    "type_alias_declaration": "type_alias",
    "enum_declaration": "enum",
}
```

and add, alongside the `class_declaration`/`method_definition` branches in `walk`:

```python
elif ctype == "function_declaration":
    name_node = child.child_by_field_name("name")
    if name_node is not None:
        make_symbol(
            child, "function", name_node,
            owner_qualified=owner_qualified,
            params_node=child.child_by_field_name("parameters"),
            return_type_node=child.child_by_field_name("return_type"),
            is_async=any(
                grandchild.type == "async" for grandchild in child.children
            ),
        )
    walk(child, owner_qualified)
```

(delete the now-redundant `elif ctype == "function_declaration": pass` branch).

Wire `_extract_symbols` into `parse_file`, replacing `symbols=()`:

```python
symbols = _extract_symbols(
    source, root,
    language=self.language, prefix=self.prefix,
    repository_id=self.repository_id, relative_path=relative_path,
)
...
return _TypeScriptParsedFile(
    file=file, symbols=symbols, references=(), warnings=(),
)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/codegraph/test_typescript_adapter.py -v
uv run pytest tests/codegraph -v   # confirm PythonAdapter tests still pass unmodified
```

Expected: PASS. If a node/field name in the grammar walk above does not match the
tree-sitter-typescript version actually installed (the exact grammar could not be
verified against a live parser while writing this plan — no network in the authoring
sandbox), the test failure will name the missing symbol; use
`uv run python3 -c "from tree_sitter_language_pack import get_parser; ..."` to dump the
real AST for the failing fixture (print `node.type` / `node.children` recursively) and
adjust the `_KIND_BY_NODE` keys / `child_by_field_name` arguments to match — the
approach (walk root children, dispatch by `node.type`, read fields by name) stays the
same regardless of minor grammar version differences.

- [ ] **Step 5: Run flake8**

```bash
uv run flake8 src/iwiki_mcp/codegraph/languages/typescript.py src/iwiki_mcp/codegraph/models.py src/iwiki_mcp/codegraph/query.py
```

Expected: no output.

- [ ] **Step 6: Commit**

```bash
git add src/iwiki_mcp/codegraph/languages/typescript.py src/iwiki_mcp/codegraph/models.py src/iwiki_mcp/codegraph/query.py tests/codegraph/test_typescript_adapter.py
git commit -m "feat(codegraph): extract TS declarations (functions, classes, interfaces, type aliases, enums, methods)"
```

---

### Task 4: Imports/exports and `resolve_references`

**Files:**
- Modify: `src/iwiki_mcp/codegraph/languages/typescript.py`
- Modify: `tests/codegraph/test_typescript_adapter.py`

**Interfaces:**
- Consumes: `ReferenceRecord` (unchanged, from `models.py`).
- Produces: `parsed.references` populated for `import` statements
  (`relation_type="IMPORTS"`, `target_reference` = the import specifier text, e.g.
  `"./foo"`), consumed unchanged by the already-implemented
  `TypeScriptAdapter.resolve_references` from Task 2 (which calls the shared
  `resolver.resolve_references`).

- [ ] **Step 1: Write the failing tests**

```python
def test_import_statement_produces_reference():
    source = b"import { foo } from \"./foo\";\nexport function use() { return foo; }\n"
    adapter = TypeScriptAdapter("domain", ("a.ts",), parser_version="test")

    parsed = adapter.parse_file(source, "a.ts")

    assert len(parsed.references) == 1
    reference = parsed.references[0]
    assert reference.relation_type == "IMPORTS"
    assert reference.target_reference == "./foo"
    assert reference.source_file_id == parsed.file.file_id


def test_resolve_references_produces_declares_and_import_relations():
    from iwiki_mcp.codegraph.resolver import SymbolIndex

    source = b"import { foo } from \"./foo\";\n"
    adapter = TypeScriptAdapter("domain", ("a.ts",), parser_version="test")
    parsed = adapter.parse_file(source, "a.ts")
    index = SymbolIndex.from_parsed_files((parsed,))

    result = adapter.resolve_references(parsed, index)

    assert any(rel.relation_type == "IMPORTS" for rel in result.relations)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/codegraph/test_typescript_adapter.py -k import -v`
Expected: FAIL — `parsed.references` is always `()`.

- [ ] **Step 3: Implement**

Add a reference-walking pass, mirroring the symbol walk's shape:

```python
from ..models import ReferenceRecord


def _extract_references(source: bytes, root, *, file_record: FileRecord):
    references: list[ReferenceRecord] = []
    for child in root.children:
        if child.type != "import_statement":
            continue
        source_node = child.child_by_field_name("source")
        if source_node is None:
            continue
        specifier = _text(source, source_node).strip("\"'")
        references.append(ReferenceRecord(
            source_symbol_id=None,
            source_file_id=file_record.file_id,
            source_module_id=file_record.module_id,
            relation_type="IMPORTS",
            target_reference=specifier,
            source_line=child.start_point[0] + 1,
            source_byte=child.start_byte,
            source_end_line=child.end_point[0] + 1,
            source_end_byte=child.end_byte,
            resolution_hint="unresolved",
        ))
    return tuple(references)
```

`resolution_hint="unresolved"` is deliberate for this task: TypeScript import
specifiers (`"./foo"`, `"lodash"`) are relative/package paths, not the dotted
qualified names `resolver.resolve_references`'s `_module_prefix_candidates` expects
from Python — resolving them to a project file needs path-relative lookup logic that
is explicitly out of scope for this plan (see spec "Out of scope"; TS import relations
persist as structurally-valid `IMPORTS` edges with `resolution_state="unresolved"`,
still queryable and visible in `wiki_code_context`, just not auto-linked to a target
symbol yet).

Wire into `parse_file`, replacing `references=()`:

```python
references = _extract_references(source, root, file_record=file)
...
return _TypeScriptParsedFile(
    file=file, symbols=symbols, references=references, warnings=(),
)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/codegraph/test_typescript_adapter.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/codegraph/languages/typescript.py tests/codegraph/test_typescript_adapter.py
git commit -m "feat(codegraph): extract TS import references"
```

---

### Task 5: `extends`/`implements` as `INHERITS` relations

**Files:**
- Modify: `src/iwiki_mcp/codegraph/languages/typescript.py`
- Modify: `tests/codegraph/test_typescript_adapter.py`

**Interfaces:**
- Consumes: the symbol walk from Task 3 (adds a reference emission alongside
  `class_declaration`/`interface_declaration` handling, does not change their
  symbol-emission behavior).
- Produces: additional `parsed.references` entries with
  `relation_type="INHERITS"` for `class X extends Y`, `class X implements Y`, and
  `interface X extends Y` — reusing the existing `RelationRecord.relation_type`
  Literal (`"DECLARES" | "IMPORTS" | "CALLS" | "INHERITS"`, unchanged) rather than
  inventing new `EXTENDS`/`IMPLEMENTS` relation-type strings, since the schema-v2
  contract in `models.py` is closed to those four values and Python's class
  inheritance already uses `INHERITS` for the same semantic edge.

- [ ] **Step 1: Write the failing test**

```python
def test_class_extends_produces_inherits_reference():
    source = (
        b"class Base {}\n"
        b"class Derived extends Base {}\n"
    )
    adapter = TypeScriptAdapter("domain", ("a.ts",), parser_version="test")

    parsed = adapter.parse_file(source, "a.ts")

    inherits = [r for r in parsed.references if r.relation_type == "INHERITS"]
    assert len(inherits) == 1
    assert inherits[0].target_reference == "a.ts/Base"


def test_interface_extends_and_class_implements_produce_inherits_references():
    source = (
        b"interface Base { }\n"
        b"interface Derived extends Base { }\n"
        b"class Impl implements Derived { }\n"
    )
    adapter = TypeScriptAdapter("domain", ("a.ts",), parser_version="test")

    parsed = adapter.parse_file(source, "a.ts")

    inherits_targets = {
        r.target_reference for r in parsed.references
        if r.relation_type == "INHERITS"
    }
    assert "a.ts/Base" in inherits_targets
    assert "a.ts/Derived" in inherits_targets
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/codegraph/test_typescript_adapter.py -k extends -v`
Expected: FAIL — no `INHERITS` references are emitted yet.

- [ ] **Step 3: Implement**

Add a helper and call it from the `class_declaration`/`interface_declaration` walk
branches (both already exist from Task 3 — this task only adds a reference emission
alongside the existing symbol emission, it does not change symbol extraction):

```python
def _heritage_references(source: bytes, node, *, owner_symbol_id: str, file_record):
    references = []
    heritage = next(
        (child for child in node.children if child.type == "class_heritage"), None
    )
    clauses = []
    if heritage is not None:
        clauses.extend(heritage.children)
    extends_type = next(
        (child for child in node.children if child.type == "extends_type_clause"),
        None,
    )
    if extends_type is not None:
        clauses.append(extends_type)
    for clause in clauses:
        if clause.type not in ("extends_clause", "implements_clause", "extends_type_clause"):
            continue
        for target in clause.children:
            if target.type not in ("identifier", "type_identifier", "nested_type_identifier"):
                continue
            name = _text(source, target)
            references.append(ReferenceRecord(
                source_symbol_id=owner_symbol_id,
                source_file_id=file_record.file_id,
                source_module_id=file_record.module_id,
                relation_type="INHERITS",
                target_reference=f"{file_record.path}/{name}",
                source_line=clause.start_point[0] + 1,
                source_byte=clause.start_byte,
                source_end_line=clause.end_point[0] + 1,
                source_end_byte=clause.end_byte,
                resolution_scope="file",
            ))
    return tuple(references)
```

`_extract_symbols` (Task 3) returns only `tuple(symbols)` today; extend it to also
collect heritage references and return both, since `make_symbol` already returns
`(qualified, stable_id)` (Task 3) and the `class_declaration` branch already captures
`qualified, _stable_id = make_symbol(...)`. In `_extract_symbols`:

1. Add `references: list[ReferenceRecord] = []` next to the existing
   `symbols: list[SymbolRecord] = []` at the top of `_extract_symbols`.
2. In the `class_declaration` branch, rename the unused `_stable_id` capture to
   `stable_id` and append after computing `qualified`:
   ```python
   references.extend(_heritage_references(
       source, child, owner_symbol_id=stable_id, file_record=file_record,
   ))
   ```
   (`_extract_symbols` needs a `file_record: FileRecord` keyword-only parameter added
   to its signature for this — `parse_file` already has `file` in scope at the call
   site from Task 2, so pass `file_record=file`.)
3. Give `interface_declaration` its own branch (currently routed through the generic
   `_KIND_BY_NODE` dispatch — same pattern as `function_declaration` in Task 3),
   capturing `qualified, stable_id = make_symbol(child, "interface", name_node,
   owner_qualified=owner_qualified)` and the same `references.extend(_heritage_references(...))`
   call, then remove `"interface_declaration"` from `_KIND_BY_NODE`.
4. Change `_extract_symbols`'s final line from `return tuple(symbols)` to
   `return tuple(symbols), tuple(references)`.

Update `_extract_symbols`'s call site in `parse_file` (Task 2/3) to unpack both and
merge with the import references from Task 4:

```python
symbols, heritage_references = _extract_symbols(
    source, root,
    language=self.language, prefix=self.prefix,
    repository_id=self.repository_id, relative_path=relative_path,
    file_record=file,
)
references = (*_extract_references(source, root, file_record=file), *heritage_references)
...
return _TypeScriptParsedFile(
    file=file, symbols=symbols, references=references, warnings=(),
)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/codegraph/test_typescript_adapter.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/codegraph/languages/typescript.py tests/codegraph/test_typescript_adapter.py
git commit -m "feat(codegraph): emit INHERITS relations for TS extends/implements"
```

---

### Task 6: Register `TypeScriptAdapter` in the composition root

**Files:**
- Modify: `src/iwiki_mcp/server.py` (~line 96-122, `_code_graph_adapter_factories`)
- Test: `tests/test_server_code_graph.py` or the actual file covering
  `_code_graph_adapter_factories` — confirm the name with
  `grep -rl "_code_graph_adapter_factories" tests/`

**Interfaces:**
- Consumes: `TypeScriptAdapter` from Task 2-5, `AdapterFactory` (unchanged, from
  `indexer.py`).
- Produces: `_code_graph_adapter_factories(repository_id)` returns a dict with both
  `"python"` and `"typescript"` keys.

- [ ] **Step 1: Write the failing test**

```python
def test_adapter_factories_include_typescript():
    factories = _code_graph_adapter_factories("domain")

    assert "typescript" in factories
    assert factories["typescript"].extensions == (".ts", ".tsx")
    adapter = factories["typescript"].create(("a.ts",))
    assert adapter.language == "typescript"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_server_code_graph.py -k typescript -v` (adjust path per
Step 1's `grep`)
Expected: FAIL — `"typescript"` key is absent.

- [ ] **Step 3: Implement**

In `src/iwiki_mcp/server.py`, add the import and grammar version alongside the
existing Python ones (~line 96):

```python
_TYPESCRIPT_PARSER_VERSION = (
    "tree-sitter-typescript:" + _distribution_version("tree-sitter-typescript")
)
```

Extend `_code_graph_adapter_factories`:

```python
def _code_graph_adapter_factories(repository_id):
    def create_python_adapter(source_paths):
        return _codegraph_python.PythonAdapter(
            repository_id,
            source_paths,
            parser_version=_PYTHON_PARSER_VERSION,
        )

    def create_typescript_adapter(source_paths):
        return _codegraph_typescript.TypeScriptAdapter(
            repository_id,
            source_paths,
            parser_version=_TYPESCRIPT_PARSER_VERSION,
        )

    return {
        "python": _codegraph_indexer.AdapterFactory(
            create=create_python_adapter,
            extensions=(".py",),
            parser_version=_PYTHON_PARSER_VERSION,
            grammar_version=";".join((
                "tree-sitter:" + _distribution_version("tree-sitter"),
                "tree-sitter-language-pack:"
                + _distribution_version("tree-sitter-language-pack"),
                _PYTHON_PARSER_VERSION,
            )),
            adapter_version="python-adapter-v2",
        ),
        "typescript": _codegraph_indexer.AdapterFactory(
            create=create_typescript_adapter,
            extensions=(".ts", ".tsx"),
            parser_version=_TYPESCRIPT_PARSER_VERSION,
            grammar_version=";".join((
                "tree-sitter:" + _distribution_version("tree-sitter"),
                "tree-sitter-language-pack:"
                + _distribution_version("tree-sitter-language-pack"),
                _TYPESCRIPT_PARSER_VERSION,
            )),
            adapter_version="typescript-adapter-v1",
        ),
    }
```

Add the module import near the other `_codegraph_*` imports at the top of
`server.py` (find the existing `from .codegraph.languages import python as
_codegraph_python`-style import with `grep -n "_codegraph_python" src/iwiki_mcp/server.py`
and add its sibling):

```python
from .codegraph.languages import typescript as _codegraph_typescript
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_server_code_graph.py -v
uv run pytest -q   # full suite, confirm zero regressions
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/server.py tests/test_server_code_graph.py
git commit -m "feat(codegraph): register TypeScriptAdapter in the composition root"
```

---

### Task 7: Fix `runtime.py`'s two python-only gates

**Files:**
- Modify: `src/iwiki_mcp/codegraph/runtime.py` (~line 724 `snapshot()`, ~line 1031 `index()`)
- Test: `tests/codegraph/test_runtime.py` (confirm the actual name with
  `grep -rl "class CodeGraphRuntime\|def test.*snapshot\|def test.*index" tests/`)

**Interfaces:**
- Consumes: `KNOWN_LANGUAGES` from Task 1 (`config.py`).
- Produces: no signature change — `snapshot()`'s `SnapshotHeader.languages` now
  reflects the languages actually present in the stored graph; `index(languages=...)`
  now accepts any subset of `KNOWN_LANGUAGES` instead of only `["python"]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_index_accepts_typescript_language_argument():
    # Using the existing runtime test fixture/monkeypatch setup already present
    # in this file for `index()` — call it with languages=["typescript"] instead
    # of the python-only value the existing tests use, and assert it is not
    # rejected by the `_invalid_config()` guard (existing tests already assert the
    # shape of that rejection for an unknown language — reuse the same assertion
    # helper against ["typescript"] and ["python", "typescript"] instead of ["ruby"]).
    ...


def test_snapshot_header_languages_reflects_stored_files_not_a_literal():
    # Build a snapshot whose stored `files` rows include both a python and a
    # typescript row (reuse this file's existing snapshot-building test helper,
    # inserting one row of each language), then assert
    # `runtime.snapshot()[0].languages == ("python", "typescript")` — i.e. it is
    # NOT the literal `("python",)` regardless of `self.config.languages`.
    ...
```

Write these against the actual fixture/monkeypatch helpers already present in
`tests/codegraph/test_runtime.py` (read that file first — it has an established
pattern for building a fake ready snapshot; do not invent a new one). The two
assertions above are the exact behavior to encode; adapt them to the file's existing
helper names.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/codegraph/test_runtime.py -k "typescript or languages" -v`
Expected: FAIL against the current `!= "python"` guard and the literal
`languages=("python",)`.

- [ ] **Step 3: Implement**

Add the import at the top of `runtime.py`:

```python
from .config import KNOWN_LANGUAGES
```

At `snapshot()` (~line 724), replace:

```python
languages=("python",),
```

with:

```python
languages=tuple(sorted({row["language"] for row in rows["files"]})),
```

(`rows` is already the dict of stored table rows fetched earlier in that method — the
`"files"` key holds the same rows the `expected_counts` computation right below
already iterates; no new query needed.)

At `index()` (~line 1031), replace:

```python
if languages is not None and (
    not languages or any(language != "python" for language in languages)
):
    return _invalid_config()
```

with:

```python
if languages is not None and (
    not languages
    or any(language not in KNOWN_LANGUAGES for language in languages)
):
    return _invalid_config()
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/codegraph/test_runtime.py -v
uv run pytest -q
```

Expected: PASS, full suite green.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/codegraph/runtime.py tests/codegraph/test_runtime.py
git commit -m "fix(codegraph): derive snapshot languages from stored files, accept typescript in index()"
```

---

### Task 8: Fix `sqlite_adapter.py`'s `begin()` gate

**Files:**
- Modify: `src/iwiki_mcp/codegraph/sqlite_adapter.py` (~line 450 `begin()`)
- Test: `tests/codegraph/test_sqlite_adapter.py` (confirm name with
  `grep -rl "def begin" tests/`)

**Interfaces:**
- Consumes: `KNOWN_LANGUAGES` from Task 1.
- Produces: no signature change — `begin(header)` now accepts any non-empty subset
  of `KNOWN_LANGUAGES` in `header.languages` instead of requiring the literal
  `("python",)` tuple.

- [ ] **Step 1: Write the failing test**

```python
def test_begin_accepts_mixed_language_header():
    header = replace(READY_PYTHON_HEADER, languages=("python", "typescript"))
    # READY_PYTHON_HEADER: reuse this file's existing valid-header test fixture,
    # replacing only `languages`.

    result = publisher.begin(header)

    assert "error" not in result
```

Write this against the existing valid-header fixture already used by this file's
other `begin()` tests (read the file first for its exact fixture name).

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/codegraph/test_sqlite_adapter.py -k mixed_language -v`
Expected: FAIL — `("python", "typescript") != ("python",)` trips `snapshot_incomplete`.

- [ ] **Step 3: Implement**

Add the import:

```python
from .config import KNOWN_LANGUAGES
```

Replace:

```python
if (
    header.protocol_version != 1
    or header.schema_version != SCHEMA_VERSION
    or tuple(header.languages) != ("python",)
):
    return {"error": "snapshot_incomplete"}
```

with:

```python
if (
    header.protocol_version != 1
    or header.schema_version != SCHEMA_VERSION
    or not header.languages
    or not set(header.languages) <= KNOWN_LANGUAGES
):
    return {"error": "snapshot_incomplete"}
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/codegraph/test_sqlite_adapter.py -v
uv run pytest -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/codegraph/sqlite_adapter.py tests/codegraph/test_sqlite_adapter.py
git commit -m "fix(codegraph): accept any registered language subset in sqlite_adapter begin()"
```

---

### Task 9: Multi-language search — `query.py` and `mcp_adapter.py`

**Files:**
- Modify: `src/iwiki_mcp/codegraph/query.py`
- Modify: `src/iwiki_mcp/codegraph/mcp_adapter.py` (~line 230)
- Test: `tests/codegraph/test_query.py` (confirm name with
  `grep -rl "validate_search_request\|ValidatedSearchRequest" tests/`)

**Interfaces:**
- Consumes: `KNOWN_LANGUAGES` from Task 1.
- Produces: `ValidatedSearchRequest.languages: tuple[str, ...]` (was `.language: str`)
  — **breaking rename**, every internal caller updated in this task.
  `validate_search_request(query, ..., languages: list[str] | None = None,
  configured_languages: tuple[str, ...] = ("python",))` — `configured_languages` is
  new, defaults to today's only value so every existing call site that does not pass
  it keeps exact current behavior; the real call site (wherever `validate_search_request`
  is invoked from the runtime/reader layer — find it with
  `grep -rn "validate_search_request(" src/iwiki_mcp/codegraph/`) is updated to pass
  `self.config.languages`.

- [ ] **Step 1: Write the failing tests**

```python
def test_default_languages_is_configured_languages_not_all_known():
    request = validate_search_request(
        "foo", configured_languages=("python",),
    )
    assert request.languages == ("python",)


def test_explicit_languages_filter_validated_against_known_languages():
    request = validate_search_request(
        "foo", languages=["typescript"], configured_languages=("python", "typescript"),
    )
    assert request.languages == ("typescript",)


def test_unregistered_language_rejected():
    with pytest.raises(CodeGraphQueryError, match="unsupported language"):
        validate_search_request("foo", languages=["ruby"])


def test_multi_language_query_filters_both_languages_in_sql():
    request = validate_search_request(
        "foo", languages=["python", "typescript"],
        configured_languages=("python", "typescript"),
    )
    sql, params = _canonical_rank_query("domain", request, "qualified_exact", ())
    assert "f.language IN (?, ?)" in sql
    assert "python" in params and "typescript" in params
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/codegraph/test_query.py -k "language" -v`
Expected: FAIL — `languages` keyword does not exist yet, `.language` is singular.

- [ ] **Step 3: Implement**

Add the import: `from .config import KNOWN_LANGUAGES`.

Replace the `ValidatedSearchRequest` dataclass field:
```python
language: str
```
with:
```python
languages: tuple[str, ...]
```

Replace `validate_search_request`'s language handling:

```python
def validate_search_request(
    query: str,
    *,
    kinds: list[str] | None = None,
    path: str | None = None,
    languages: list[str] | None = None,
    configured_languages: tuple[str, ...] = ("python",),
    limit: int = 20,
) -> ValidatedSearchRequest:
    ...  # existing query/kinds validation unchanged above this point
    if languages is None:
        normalized_languages = tuple(configured_languages)
    elif (
        not isinstance(languages, list)
        or not languages
        or any(language not in KNOWN_LANGUAGES for language in languages)
    ):
        raise CodeGraphQueryError("unsupported language")
    else:
        normalized_languages = tuple(sorted(set(languages)))
    if type(limit) is not int or not 1 <= limit <= 100:
        raise CodeGraphQueryError("limit must be between 1 and 100")
    if path is not None:
        try:
            _validated_relative_posix(path)
        except ValueError as exc:
            raise CodeGraphQueryError(
                "path must be a safe project-relative prefix"
            ) from exc
    return ValidatedSearchRequest(
        query=query,
        kinds=normalized_kinds,
        path=path,
        languages=normalized_languages,
        limit=limit,
        tokens=query_tokens,
    )
```

Every `request.language` reference in `_canonical_rank_query` (the `"common"` /
`"common_parameters"` entries for the `file`, `module`, and `symbol` branch specs) and
`_alias_rank_query` changes from an equality predicate to an `IN` predicate. For each
of the three `"common": "... AND f.language = ?"` occurrences, change to:

```python
"common": (
    "f.repository_id = ? AND f.language IN "
    f"({', '.join('?' for _ in request.languages)})"
    # (append any additional existing predicate text for that branch, e.g.
    # the module branch's "AND f.module_id IS NOT NULL", after this)
),
"common_parameters": [domain, *request.languages],
```

Apply the same `f.language = ?` → `f.language IN (...)` change, with
`*request.languages` splicing, everywhere else `request.language` appears in
`_alias_rank_query`'s two branches (module and symbol).

Find the actual call site that constructs a `validate_search_request(...)` call from
live runtime state (not a test) with:
```bash
grep -rn "validate_search_request(" src/iwiki_mcp/codegraph/ | grep -v test
```
Add `configured_languages=self.config.languages` (or the equivalent attribute name at
that call site — read the surrounding function to get the exact binding name) to that
call.

In `src/iwiki_mcp/codegraph/mcp_adapter.py` (~line 230), replace:

```python
"languages": [request.language],
```

with:

```python
"languages": list(request.languages),
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/codegraph/test_query.py -v
uv run pytest -q
uv run flake8 src/iwiki_mcp/codegraph/query.py src/iwiki_mcp/codegraph/mcp_adapter.py
```

Expected: PASS, full suite green, no lint findings. This is the change with the
highest regression risk (health-metric gate) — if any pre-existing python-only search
test fails, the default-`configured_languages=("python",)` behavior did not
faithfully reproduce the old hardcoded `f.language = 'python'` predicate; fix before
proceeding to Task 10.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/codegraph/query.py src/iwiki_mcp/codegraph/mcp_adapter.py tests/codegraph/test_query.py
git commit -m "feat(codegraph): generalize search to a language set instead of python-only"
```

---

### Task 10: Mixed-repo fixture, end-to-end integration test, benchmark regression check

**Files:**
- Create: `tests/fixtures/codegraph/mixed_python_typescript/` (a small repo: 2 `.py`
  files, 2 `.ts` files, one importing the other, one class extending another)
- Create: `tests/codegraph/test_mixed_language_indexing.py`
- Modify: `tests/fixtures/codegraph/typescript_basic/` (populate with the same
  fixture content used across Tasks 2-5's inline `source` byte strings, as real files
  — later tests and any future TS-adapter work read from here instead of inline
  bytes)

**Interfaces:**
- Consumes: `CodeGraphIndexer` (unchanged, `indexer.py`), the adapter registry from
  Task 6, `KNOWN_LANGUAGES` from Task 1.
- Produces: no new production code — this task is pure verification.

- [ ] **Step 1: Create the fixture repo**

```
tests/fixtures/codegraph/mixed_python_typescript/
  service.py
  __init__.py
  client.ts
  base.ts
```

`service.py`:
```python
def process(value: int) -> int:
    return value * 2
```

`__init__.py`: empty.

`base.ts`:
```typescript
export class Base {
  name: string = "base";
}
```

`client.ts`:
```typescript
import { Base } from "./base";

export class Client extends Base {
  connect(): void {}
}
```

- [ ] **Step 2: Write the failing integration test**

```python
"""End-to-end: one graph covers both Python and TypeScript sources."""
from __future__ import annotations

from pathlib import Path

import pytest

# Follow this file's sibling test modules (e.g. test_runtime.py or
# test_server_code_graph.py) for the exact CodeGraphIndexer construction
# pattern already used by this test suite — cache_base/project_dir/domain/
# config/paths/adapter_factories/resolver_version/wiki_selector_resolver.
# Reuse that helper instead of re-deriving construction here.

FIXTURES = Path(__file__).parents[1] / "fixtures" / "codegraph"


def test_mixed_repo_builds_one_snapshot_with_both_languages(tmp_path, monkeypatch):
    # 1. Point project_dir at FIXTURES / "mixed_python_typescript".
    # 2. Build an indexer with config.languages = ("python", "typescript") via
    #    this suite's existing indexer-construction helper.
    # 3. built = indexer.build_rows()
    # 4. assert set(built.header.languages) == {"python", "typescript"}
    # 5. assert {row["language"] for row in built.tables["files"]} == {"python", "typescript"}
    ...


def test_mixed_repo_search_returns_both_languages(tmp_path, monkeypatch):
    # Build as above, then use this suite's existing CodeGraphQuery /
    # validate_search_request test helper to search a term appearing in both
    # a Python and a TypeScript symbol name (e.g. name both a Python function
    # and a TS class "Process"/"process") with languages=None (defaults to
    # configured) and confirm results include entities from both languages.
    ...


def test_python_only_repo_search_unaffected(tmp_path, monkeypatch):
    # Regression check (health metric): build with config.languages = ("python",)
    # only, using the pre-existing tests/fixtures/codegraph/python_basic fixture,
    # and confirm search results are identical to this suite's existing
    # python-only search assertions (byte-for-byte same entity_ids).
    ...
```

- [ ] **Step 3: Run the tests to verify they fail (or pass, revealing gaps)**

```bash
uv run pytest tests/codegraph/test_mixed_language_indexing.py -v
```

Expected: FAIL initially only if the indexer-construction helper pattern needs
adjustment for two languages; if the two-language indexer builds correctly first try,
this step still confirms it — either way, do not proceed to Step 4 until all three
tests pass for a real reason, not a stub.

- [ ] **Step 4: Fill in the test bodies for real, run, verify pass**

```bash
uv run pytest tests/codegraph/test_mixed_language_indexing.py -v
```

Expected: PASS, 3/3.

- [ ] **Step 5: Rerun the full suite and the code-graph benchmark**

```bash
uv run pytest -q
```

Find the benchmark test/script referenced by `reference/code-graph-benchmark` with:
```bash
grep -rln "benchmark" tests/codegraph/ | grep -v __pycache__
```
Run it and compare its reported numbers to the wiki page's current authoritative
result (`wiki_read_page(domain="iwiki-mcp", slug="reference/code-graph-benchmark")`) —
zero regression is the health-metric gate from the intent. If the benchmark script
takes `--languages`, run it once with `python` only (must match the pre-change
number) and once with `python,typescript` (informational, no prior baseline to
compare against).

- [ ] **Step 6: Commit**

```bash
git add tests/fixtures/codegraph/mixed_python_typescript tests/fixtures/codegraph/typescript_basic tests/codegraph/test_mixed_language_indexing.py
git commit -m "test(codegraph): add mixed Python+TypeScript integration fixture and regression check"
```

---

### Task 11: Optional `tsc` type-resolution boost (opt-in, isolated, non-blocking)

**Files:**
- Modify: `src/iwiki_mcp/codegraph/languages/typescript.py`
- Modify: `src/iwiki_mcp/codegraph/indexer.py` (`KNOWN_WARNING_CODES`)
- Modify: `src/iwiki_mcp/server.py` (thread `type_boost_enabled` through the
  composition root — Step 5)
- Test: `tests/codegraph/test_typescript_adapter.py`

**Interfaces:**
- Produces: `indexer.py::KNOWN_WARNING_CODES` gains `"typescript_boost_unavailable"`
  (spec §4's required warning code — surfaced once per build via the existing
  set-based dedup in `indexer._metadata`, no new dedup logic needed).
- Consumes: `CodeGraphConfig.typescript_type_boost` from Task 1 — **not** read
  directly by `TypeScriptAdapter` (the adapter has no `CodeGraphConfig` reference by
  contract); instead passed as a constructor keyword by the composition root
  (`server.py`, extend Task 6's `create_typescript_adapter` closure to read
  `binding`'s resolved config and pass `type_boost_enabled=config.typescript_type_boost`
  — find how `_code_runtime` already obtains `config` with
  `grep -n "config" src/iwiki_mcp/server.py` around `_code_runtime`).
- Produces: `TypeScriptAdapter.__init__(..., type_boost_enabled: bool = False)`; when
  `True`, `resolve_references` attempts one bounded subprocess call and silently
  degrades on any failure — never raises past `resolve_references`.

- [ ] **Step 1: Write the failing tests**

```python
def test_type_boost_disabled_by_default_no_subprocess_call(monkeypatch):
    called = []
    monkeypatch.setattr(
        "iwiki_mcp.codegraph.languages.typescript._run_tsc_boost",
        lambda *a, **k: called.append(1) or None,
    )
    adapter = TypeScriptAdapter("domain", ("a.ts",), parser_version="test")
    parsed = adapter.parse_file(b"const x = 1;\n", "a.ts")
    from iwiki_mcp.codegraph.resolver import SymbolIndex

    adapter.resolve_references(parsed, SymbolIndex.from_parsed_files((parsed,)))

    assert called == []


def test_type_boost_failure_degrades_silently_and_warns(monkeypatch):
    monkeypatch.setattr(
        "iwiki_mcp.codegraph.languages.typescript._run_tsc_boost",
        lambda *a, **k: None,  # None == "boost unavailable" per its own contract
    )
    adapter = TypeScriptAdapter(
        "domain", ("a.ts",), parser_version="test", type_boost_enabled=True,
    )
    parsed = adapter.parse_file(b"const x = 1;\n", "a.ts")
    from iwiki_mcp.codegraph.resolver import SymbolIndex

    result = adapter.resolve_references(parsed, SymbolIndex.from_parsed_files((parsed,)))

    assert result is not None  # did not raise
    assert result.warnings == ("typescript_boost_unavailable",)


def test_type_boost_success_emits_no_warning(monkeypatch):
    monkeypatch.setattr(
        "iwiki_mcp.codegraph.languages.typescript._run_tsc_boost",
        lambda *a, **k: {},  # any non-None return == boost succeeded
    )
    adapter = TypeScriptAdapter(
        "domain", ("a.ts",), parser_version="test", type_boost_enabled=True,
    )
    parsed = adapter.parse_file(b"const x = 1;\n", "a.ts")
    from iwiki_mcp.codegraph.resolver import SymbolIndex

    result = adapter.resolve_references(parsed, SymbolIndex.from_parsed_files((parsed,)))

    assert result.warnings == ()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/codegraph/test_typescript_adapter.py -k type_boost -v`
Expected: FAIL — `type_boost_enabled` keyword and `_run_tsc_boost` do not exist yet.

- [ ] **Step 3: Implement**

```python
def _run_tsc_boost(source: bytes, path: str, *, timeout_seconds: float = 5.0):
    """Best-effort type info from the project's own `typescript` package.

    Returns None on any failure (missing node, missing typescript package,
    timeout, non-zero exit, malformed output) — callers must treat None as
    "no boost available" and continue with the Tree-sitter-only result.
    """
    import json
    import subprocess

    try:
        completed = subprocess.run(
            ["node", "-e", _TSC_BOOST_SCRIPT, path],
            input=source,
            capture_output=True,
            timeout=timeout_seconds,
            check=True,
        )
        return json.loads(completed.stdout.decode("utf-8"))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


_TSC_BOOST_SCRIPT = """
// Minimal TS Compiler API probe: emit {} until a real type-resolution
// payload is implemented in a follow-up task; this establishes the
// subprocess boundary and its failure contract only.
process.stdout.write("{}");
"""
```

Extend `TypeScriptAdapter.__init__`:

```python
def __init__(
    self,
    repository_id: str,
    source_paths: tuple[str, ...],
    *,
    parser_version: str = "tree-sitter-typescript",
    type_boost_enabled: bool = False,
) -> None:
    if not isinstance(repository_id, str) or not repository_id:
        raise ValueError("invalid repository id")
    self.repository_id = repository_id
    self.parser_version = parser_version
    self.type_boost_enabled = type_boost_enabled
```

Add `"typescript_boost_unavailable"` to `indexer.py::KNOWN_WARNING_CODES`
(alphabetical position, next to `"symlink_excluded"`) — this is the set
`sanitize_warning_codes` filters against, so an unregistered code would be silently
dropped from the build's `warnings` metadata field.

Replace `resolve_references` (it already exists from Task 2 — this task only adds the
boost call and warning, the `declares`/`resolved` computation itself is unchanged):

```python
def resolve_references(self, parsed, project_index) -> ResolutionResult:
    warnings: tuple[str, ...] = ()
    if self.type_boost_enabled:
        boost_result = _run_tsc_boost(parsed.file.content_hash.encode(), parsed.file.path)
        if boost_result is None:
            warnings = ("typescript_boost_unavailable",)
        # boost_result payload wiring into relation confidence/resolution_state is
        # out of scope for this plan (spec: best-effort, opt-in; the Tree-sitter
        # baseline already satisfies the intent's Trust priority on its own) —
        # this call proves the non-blocking subprocess contract end-to-end and
        # surfaces its own availability, nothing more.
    declares = declaration_relations(
        self.language, self.prefix, self.repository_id, parsed,
    )
    resolved = resolve_references(
        self.language, self.prefix, self.repository_id,
        parsed.references, project_index,
    )
    return ResolutionResult(
        relations=sort_relations((*declares, *resolved)), warnings=warnings,
    )
```

(`_run_tsc_boost`'s first positional argument was `source: bytes` in Step 3's
signature — `resolve_references` only has `parsed: ParsedFile` in scope, not the
original source bytes, so it passes `parsed.file.content_hash.encode()` as a stable
placeholder payload; wiring the actual source bytes through requires threading
`source` from `parse_file` into `_TypeScriptParsedFile`, which is unnecessary for this
task's non-blocking-contract scope and left for the follow-up that implements the real
boost payload.)

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/codegraph/test_typescript_adapter.py -v
uv run pytest -q
```

Expected: PASS.

- [ ] **Step 5: Wire the config flag through the composition root**

In `server.py`'s `create_typescript_adapter` (Task 6), read the resolved
`CodeGraphConfig` in scope at that closure (confirm exact variable name with
`grep -n "config" src/iwiki_mcp/server.py` near `_code_runtime`/
`_code_graph_adapter_factories` — if `config` is not yet in scope at that closure,
thread it in as a parameter to `_code_graph_adapter_factories(repository_id, config)`
and update its one call site) and pass
`type_boost_enabled=config.typescript_type_boost`.

- [ ] **Step 6: Commit**

```bash
git add src/iwiki_mcp/codegraph/languages/typescript.py src/iwiki_mcp/codegraph/indexer.py src/iwiki_mcp/server.py tests/codegraph/test_typescript_adapter.py
git commit -m "feat(codegraph): add opt-in, non-blocking tsc boost subprocess contract"
```

---

### Task 12: Docs, README, and version bump

**Files:**
- Modify: `README.md` (~line 372-378)
- Modify: `docs/README.ru.md` (~line 372-378)
- Modify: `pyproject.toml` (`version`)
- Wiki (via iwiki MCP tools, not a repo file): `concept/code-graph-configuration`,
  `concept/code-graph-identities` (or a new `concept/code-graph-typescript-extraction`
  page mirroring `concept/code-graph-python-extraction`)

**Interfaces:** none (documentation only).

- [ ] **Step 1: Update README.md**

Change (~line 372):
```
are the defaults. `languages` accepts only `python`. `exclude` entries must be safe
```
to:
```
are the defaults. `languages` accepts `python` and/or `typescript`. `exclude` entries
must be safe
```

Change (~line 378):
```toml
languages = ["python"]
```
to:
```toml
languages = ["python", "typescript"]
```

Add one sentence after the existing `max_rebuild_seconds`/`max_full_rebuild_seconds`
paragraph documenting the new field:
```
`typescript_type_boost` (default `false`) opts into an isolated, best-effort
TypeScript Compiler API subprocess for type resolution; its absence or failure never
blocks indexing — the Tree-sitter baseline always runs.
```

- [ ] **Step 2: Apply the same three changes to `docs/README.ru.md`** (translated)

```
ниже приведены defaults. `languages` принимает `python` и/или `typescript`; значения
`exclude` должны быть безопасными относительными путями.
```
```toml
languages = ["python", "typescript"]
```
```
`typescript_type_boost` (по умолчанию `false`) включает изолированный,
best-effort-подпроцесс TypeScript Compiler API для резолвинга типов; его
отсутствие или сбой никогда не блокирует индексацию — Tree-sitter baseline
всегда выполняется.
```

- [ ] **Step 3: Bump the version**

In `pyproject.toml`, bump `version = "0.7.147"` to `version = "0.7.148"` (patch bump
per `CLAUDE.md` Versioning — this plan's total change is one feature addition plus
internal fixes, not a breaking or minor-worthy release per that project's own
convention of defaulting to patch).

- [ ] **Step 4: Update the wiki (iwiki MCP tools)**

Apply the iwiki Project Binding protocol from `CLAUDE.md` (bind `read`/`write`/
`primary` from `.iwiki.toml`, confirm with `wiki_status`), then:

- `wiki_update_page(domain="iwiki-mcp", slug="concept/code-graph-configuration",
  heading="Purpose", ...)` — update "The optional Python code graph" to "The optional
  Python/TypeScript code graph", and the Configuration section's "existing Python-only
  discovery" to "discovery" (drop the now-inaccurate "Python-only"). Read the page
  first with `wiki_read_page` to get `revision`/`expected_revision` per the CAS
  requirement.
- `wiki_write_page(domain="iwiki-mcp", slug="concept/code-graph-typescript-extraction",
  type="concept", tags=["code-graph", "typescript", "parser"], ...)` — new page
  mirroring `concept/code-graph-python-extraction`'s structure (Parser boundary, Lazy
  grammar loading, Extraction result sections), describing `TypeScriptAdapter`'s
  actual scope from Tasks 2-5 (functions/classes/interfaces/type_alias/enum/methods,
  ESM imports as unresolved `IMPORTS` references, extends/implements as `INHERITS`)
  and the opt-in `tsc` boost contract from Task 11 — do not describe capabilities this
  plan did not implement (no member-mutation tracking, no CommonJS `require`
  resolution).
- `wiki_lint()` — confirm no new finding.

- [ ] **Step 5: Final full-suite verification and commit**

```bash
uv run pytest -q
uv run flake8 src tests
git add README.md docs/README.ru.md pyproject.toml
git commit -m "docs: document TypeScript code graph support, bump version to 0.7.148"
```

---

## Definition of Done (traces to spec Acceptance)

- [ ] `wiki_code_index` indexes `.ts`/`.tsx` alongside `.py` in one run — Task 10.
- [ ] `wiki_code_search`/`wiki_code_context` return correct TypeScript results — Tasks
      3-5, 9, 10.
- [ ] Mixed Python+TypeScript repo builds one working graph (Approach A) — Task 10.
- [ ] Zero regression on the python-only benchmark/fixture suite — Tasks 7-10 each
      rerun `uv run pytest -q`; Task 10 Step 5 reruns the benchmark explicitly.
- [ ] `/check-chain result docs/superpowers/plans/2026-08-18-codegraph-typescript-support.md`
      run after Task 12, reconciling this plan's steps against the final `git diff`.
