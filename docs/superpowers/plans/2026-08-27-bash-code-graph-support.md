---
review:
  plan_hash: bd151b75428cffda
  last_run: 2026-08-27
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings:
    - id: F-001
      phase: coverage
      severity: WARNING
      section: "Requirement coverage"
      section_hash: 8c67560da194d907
      fragment: "R4 — Mixed-language isolation"
      text: "R4 was only implicit in the R1-R7 range instead of mapped to a named task and proof."
      fix: "Add an explicit R1-R7 coverage matrix with implementing tasks and expected evidence."
      verdict: fixed
      verdict_at: 2026-08-27
    - id: F-002
      phase: verifiability
      severity: WARNING
      section: "Task 1: Parse .sh files and extract function declarations"
      section_hash: 230cf7c4117cce10
      fragment: "Implement the minimal declaration-only BashAdapter"
      text: "Several file-writing steps relied on a later test and lacked their own explicit expected output."
      fix: "Add a measurable Expected result to every checkbox step."
      verdict: fixed
      verdict_at: 2026-08-27
    - id: F-003
      phase: dependencies
      severity: WARNING
      section: "Task 4: Prove Bash-only, mixed-language, query, context, and safety behavior"
      section_hash: 8a6fbed1cf5e1c8d
      fragment: "Run the new integration tests"
      text: "The plan expected new integration tests to fail after Tasks 1-3 had already supplied production behavior."
      fix: "Expect the integration tests to pass and treat a failure as product or helper evidence."
      verdict: fixed
      verdict_at: 2026-08-27
    - id: F-004
      phase: verifiability
      severity: WARNING
      section: "Task 4: Prove Bash-only, mixed-language, query, context, and safety behavior"
      section_hash: 8a6fbed1cf5e1c8d
      fragment: "Write the non-execution sentinel test"
      text: "R5 named source and command-argument inspection without executable assertions over parsed and serialized records."
      fix: "Add source-body and command-argument markers with explicit negative assertions for ParsedFile data and every serialized graph table."
      verdict: fixed
      verdict_at: 2026-08-27
    - id: F-005
      phase: coverage
      severity: CRITICAL
      section: "Task 4: Prove Bash-only, mixed-language, query, context, and safety behavior"
      section_hash: 8a6fbed1cf5e1c8d
      fragment: "Build the mixed fixture once with `(\"python\",)`"
      text: "The explicit Python selection did not prove the automatic default when neither Bash opt-in path is used."
      fix: "Add a production CodeGraphConfig() path over the Bash fixture and assert a Python-only header with no .sh rows."
      verdict: fixed
      verdict_at: 2026-08-27
    - id: F-006
      phase: verifiability
      severity: WARNING
      section: "Task 5: Document Bash configuration, graph semantics, and limits"
      section_hash: f6779267c176381e
      fragment: "rg -n \"bash|\\.sh|same-file|source|sh:\""
      text: "One broad rg match could not prove all seven required documentation commitments or forbidden claims."
      fix: "Use per-document fixed-term checks plus an explicit seven-row required/forbidden checklist with zero omissions."
      verdict: fixed
      verdict_at: 2026-08-27
    - id: F-007
      phase: coverage
      severity: CRITICAL
      section: "Task 1: Parse .sh files and extract function declarations"
      section_hash: 230cf7c4117cce10
      fragment: "project version becomes `0.7.194`"
      text: "The plan changed distribution metadata but omitted the module __version__ and its exact regression, causing the full suite to fail."
      fix: "Synchronize pyproject.toml, iwiki_mcp.__version__, and the package-version regression at 0.7.194 and run both package-version tests."
      verdict: fixed
      verdict_at: 2026-08-27
    - id: F-008
      phase: verifiability
      severity: CRITICAL
      section: "Task 5: Document Bash configuration, graph semantics, and limits"
      section_hash: 999bf13f00636202
      fragment: "uv run flake8 src tests"
      text: "The plan required zero exit from repository-wide flake8 although unchanged origin/master tests/eval paths already fail that command."
      fix: "Keep the full diagnostic, require changed-path flake8 success, and prove every remaining diagnostic path is byte-identical to origin/master."
      verdict: fixed
      verdict_at: 2026-08-27
result_check:
  verdict: OK
  source: plan
  plan_hash: bd151b75428cffda
  last_run: 2026-08-27
  reviewed: true
  docs_checked: true
chain:
  intent: docs/superpowers/intents/2026-08-26-bash-code-graph-support-intent.md
  spec: docs/superpowers/specs/2026-08-27-bash-code-graph-support-design.md
---

# Bash Code Graph Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add explicitly selected `.sh` indexing with Bash file/function entities and
conservative same-file `CALLS` relations, without executing shell source or changing
existing-language records.

**Architecture:** A focused `BashAdapter` parses raw bytes with the packaged
`tree-sitter-bash` grammar and emits existing graph record types. Existing configuration,
factory, resolver-family, query, and context seams register Bash without schema changes.
Production-indexer integration tests prove Bash-only and Python-plus-Bash behavior.

**Tech Stack:** Python 3.10+, `tree-sitter>=0.26.0`, `tree-sitter-bash>=0.25.1`, pytest,
flake8, SQLite code-graph snapshots.

**Spec:** `docs/superpowers/specs/2026-08-27-bash-code-graph-support-design.md`

## Global constraints

- Bash identity is exactly `language = "bash"`, `prefix = "sh"`,
  `extensions = (".sh",)`, and `adapter_version = "bash-adapter-v1"`.
- No shell, subprocess, external analyzer, `source`, `eval`, expansion, or command
  substitution is executed during indexing.
- `source` and `.` create no graph relation and never enable cross-file resolution.
- Only a unique same-file function candidate resolves. Duplicate names are ambiguous;
  cross-file, external, and dynamic targets do not resolve.
- No schema, publication protocol, default language, or environment-variable change.
- Bash selection is explicit through persistent `code_graph.languages` or a one-shot
  `wiki_code_index(languages=["bash"])`; omitting both never scans Bash.
- Existing Python, TypeScript, and JavaScript golden/baseline files are immutable proof.
- Subagents edit only files assigned to their task. Parent alone updates iwiki, stages,
  commits, pushes, and opens the pull request.

### HUMAN CHECKPOINT — stop conditions

Stop and return to the approved spec before proceeding if implementation would:

1. change any existing Python, TypeScript, or JavaScript golden/baseline row;
2. modify `src/iwiki_mcp/codegraph/schema.py` or publication contracts;
3. resolve a call through `source`, `.`, an external file, or a dynamic command name;
4. store source bodies or command arguments in graph metadata;
5. add a runtime dependency other than the approved `tree-sitter-bash` binding.

## File ownership map

| Task | Owned files | Responsibility | Requirements |
| --- | --- | --- | --- |
| 1 | `bash.py`, adapter/package tests, `pyproject.toml`, `uv.lock` | Grammar, file/module identity, function symbols, syntax-error filtering | R2, R5, R6 |
| 2 | `bash.py`, adapter tests, `resolver.py`, resolver tests | Literal commands, source ownership, duplicate ambiguity, same-file resolution | R3, R5 |
| 3 | `config.py`, `application.py`, `context.py`, focused tests | Opt-in registration, language family, `sh:` context IDs | R1, R6, R7 |
| 4 | fixtures and `test_mixed_language_indexing.py` | Production-indexer, search/context, mixed baseline, non-execution proof | R1–R7 |
| 5 | `README.md`, `docs/README.ru.md` | User-facing configuration, scope, safety, limitations | R1, R3, R5, R7 |
| 6 | plan result metadata and PR | Chain reconciliation and reviewed delivery | R1–R7 and requested PR delivery |

Tasks are sequential because Task 2 extends Task 1's adapter, Task 3 registers that
adapter, Task 4 exercises production registration, Task 5 documents the verified
contract, and Task 6 reconciles the complete diff. Each implementation task gets a fresh
worker; parent performs review and the checkpoint commit before dispatching the next
worker.

## Plan validation protocol

Before approval, the parent performs three distinct reviews requested by the user:

1. writing-plans self-review for R1–R7 coverage, placeholders, path existence, and type/
   signature consistency;
2. formal `check-chain plan` phases for structure, coverage, dependencies,
   verifiability, and consistency;
3. independent read-only `chain-auditor` review for executable commands, expected
   evidence, ordering errors, hidden decisions, and drift from intent/spec.

Any finding is fixed in this English plan source before approval; a body change invalidates
later review hashes and requires the affected reviews to run again.

## Requirement coverage

| Spec requirement | Implementing tasks | Expected proof |
| --- | --- | --- |
| R1 — Explicit `.sh` discovery | Tasks 3, 4, 5 | Persistent and one-shot selection tests; Bash-only and Python-only fixture paths; docs |
| R2 — Bash functions are searchable | Tasks 1, 4 | Adapter ranges/IDs and filtered production search |
| R3 — Conservative call evidence | Tasks 2, 4, 5 | Resolved, ambiguous, unresolved, dynamic, and source-command cases |
| R4 — Mixed-language isolation | Task 4 | Python row equality, prefix isolation, no cross-language target |
| R5 — Static-only safety | Tasks 1, 2, 4, 5 | Sentinel absence and source-free record inspection |
| R6 — Existing-language compatibility | Tasks 1, 3, 4 | Unchanged defaults, baselines, and full suite |
| R7 — Query and context compatibility | Tasks 3, 4, 5 | `bash` filter, `sh:` seed, `DECLARES`/`CALLS` traversal |

---

### Task 1: Parse `.sh` files and extract function declarations

**Closes:** R2 (searchable functions), R5 (static-only parsing), R6 (approved dependency
and unchanged existing adapters).

**Files:**
- Create: `src/iwiki_mcp/codegraph/languages/bash.py`
- Create: `tests/codegraph/test_bash_adapter.py`
- Modify: `tests/codegraph/test_config_location_models.py:551-559`
- Modify: `src/iwiki_mcp/__init__.py:1-3`
- Modify: `tests/test_package.py:10-30`
- Modify: `pyproject.toml:1-25`
- Modify: `uv.lock`

- [ ] **Step 1: Write failing adapter identity, module, function, range, and warning tests**

Add these concrete cases to `tests/codegraph/test_bash_adapter.py`:

```python
from iwiki_mcp.codegraph.languages.bash import BashAdapter


def _adapter():
    return BashAdapter("domain", (), parser_version="test-parser")


def test_identity_and_module_fields():
    parsed = _adapter().parse_file(b"run() { :; }\n", "scripts/lib.sh")
    assert _adapter().language == "bash"
    assert _adapter().prefix == "sh"
    assert _adapter().extensions == (".sh",)
    assert parsed.file.language == "bash"
    assert parsed.file.module_key == "scripts/lib.sh"
    assert parsed.file.module_local_name == "lib"
    assert parsed.file.module_qualified_name == "scripts.lib"
    assert parsed.file.file_id.startswith("sh:file:")
    assert parsed.file.module_id.startswith("sh:module:")


def test_both_function_forms_have_exact_ranges_and_no_source_metadata():
    source = b"first() { :; }\nfunction second { :; }\n"
    parsed = _adapter().parse_file(source, "bin/main.sh")
    by_name = {item.local_name: item for item in parsed.symbols}
    assert set(by_name) == {"first", "second"}
    assert all(item.kind == "function" for item in by_name.values())
    assert by_name["first"].qualified_name == "bin.main.first"
    assert by_name["second"].qualified_name == "bin.main.second"
    assert source[by_name["first"].start_byte:by_name["first"].end_byte] == \
        b"first() { :; }"
    assert by_name["first"].start_line == 1
    assert by_name["second"].start_line == 2
    assert all(item.signature is None for item in parsed.symbols)
    assert all("first()" not in item.metadata_json for item in parsed.symbols)


def test_syntax_error_keeps_file_and_suppresses_overlapping_entities():
    parsed = _adapter().parse_file(
        b"good() { :; }\nbroken() { if; }\n", "broken.sh",
    )
    assert parsed.file.path == "broken.sh"
    assert "parse_error" in parsed.warnings
    assert {item.local_name for item in parsed.symbols} == {"good"}
```

Expected: the new test module contains three deterministic tests covering R2/R5 fields,
ranges, metadata, and malformed syntax without importing any test-only parser.

- [ ] **Step 2: Run the tests and verify the missing adapter failure**

Run: `uv run pytest tests/codegraph/test_bash_adapter.py -v`

Expected: collection fails with
`ModuleNotFoundError: No module named 'iwiki_mcp.codegraph.languages.bash'`.

- [ ] **Step 3: Add the approved parser dependency and patch version**

Edit `pyproject.toml` and `src/iwiki_mcp/__init__.py` so both distribution and module
versions become `0.7.194`. Update the exact package-version regression in
`tests/test_package.py` to the same value. Add this dependency to `pyproject.toml`:

```toml
"tree-sitter-bash>=0.25.1",
```

Run: `uv lock`

Expected: exit `0`; `uv.lock` contains `tree-sitter-bash`, and the local project record
contains version `0.7.194`.

Extend `test_tree_sitter_packages_are_available` in
`tests/codegraph/test_config_location_models.py`:

```python
import tree_sitter_bash

assert tree_sitter_bash is not None
```

Expected: dependency manifest, lockfile, package-availability test, and project patch
version all describe the same installed `tree-sitter-bash` release family. Run
`uv run pytest -q tests/test_package.py::test_package_version_matches_distribution_metadata tests/test_package.py::test_code_graph_benchmark_package_version`; both tests must pass
and prove module, distribution, and regression metadata all equal `0.7.194`.

- [ ] **Step 4: Implement the minimal declaration-only `BashAdapter`**

Implement these exact private seams in `bash.py`:

```python
def _walk(node):
    yield node
    for child in node.named_children:
        yield from _walk(child)


def _error_ranges(root):
    return tuple(
        (node.start_byte, node.end_byte)
        for node in _walk(root)
        if node.type == "ERROR" or node.is_missing
    )


def _intersects_error(node, ranges):
    return any(
        (start < end and node.start_byte < end and node.end_byte > start)
        or (start == end and node.start_byte <= start <= node.end_byte)
        for start, end in ranges
    )


class BashAdapter:
    language = "bash"
    prefix = "sh"
    extensions = (".sh",)

    def _get_parser(self):
        if self._parser is None:
            from tree_sitter import Language, Parser
            import tree_sitter_bash

            self._parser = Parser(Language(tree_sitter_bash.language()))
        return self._parser
```

Add `_relative_path` by copying the safe POSIX validation contract from `python.py`, and
derive module names by removing the final `.sh` suffix and dot-joining path parts. The
constructor validates a non-empty NUL-free repository ID, stores `parser_version`, and
sets `self._parser = None`. `parse_file` validates `bytes`, creates all `FileRecord`
fields, parses once, and returns sorted symbols/references/warnings. Use only
`FileRecord`, `SymbolRecord`, and stable ID helpers from `models.py`. Function visibility
and signature are `None`. Metadata is canonical JSON containing only
`{"module": module_qualified_name}` using sorted keys and compact separators.
`resolve_references` initially returns only `declaration_relations` in a
`ResolutionResult`.

Use a two-pass declaration build. Count each public qualified name first. A singleton
passes `""` as the private normalized signature to `symbol_id`; duplicates pass
`f"occurrence:{node.start_byte}"`, retaining all duplicate records. Suppress nodes
intersecting `ERROR` or missing-node ranges and emit one `parse_error` warning.

Expected: `bash.py` imports without parser initialization; calling `parse_file` produces
module-backed records and both function forms while syntax-error intersections produce no
symbol.

- [ ] **Step 5: Run focused declaration tests**

Run: `uv run pytest tests/codegraph/test_bash_adapter.py -v`

Expected: all Task 1 tests pass; no network request or parser download occurs.

- [ ] **Step 6: Run unchanged adapter baselines**

Run: `uv run pytest tests/codegraph/test_python_adapter.py tests/codegraph/test_typescript_golden.py tests/codegraph/test_mixed_language_baseline.py -q`

Expected: exit `0`; committed baseline files remain byte-identical and the existing
Python adapter module stays green. Do not change any baseline fixture.

- [ ] **Step 7: Parent review and checkpoint commit**

Review requirement coverage and changed-file scope, then run:

```bash
git add pyproject.toml uv.lock src/iwiki_mcp/__init__.py src/iwiki_mcp/codegraph/languages/bash.py tests/codegraph/test_bash_adapter.py tests/codegraph/test_config_location_models.py tests/test_package.py
git commit -m "feat(codegraph): add Bash declaration adapter"
```

Expected: one commit containing only Task 1 paths.

---

### Task 2: Extract and conservatively resolve Bash calls

**Closes:** R3 (resolved/ambiguous/unresolved calls), R5 (dynamic and source commands
never evaluated).

**Files:**
- Modify: `src/iwiki_mcp/codegraph/languages/bash.py`
- Modify: `src/iwiki_mcp/codegraph/resolver.py:91-95`
- Modify: `tests/codegraph/test_bash_adapter.py`
- Modify: `tests/codegraph/test_resolver.py:130-165`

- [ ] **Step 1: Write failing literal, dynamic, ownership, source, and duplicate tests**

Add tests with these assertions:

```python
from iwiki_mcp.codegraph.resolver import SymbolIndex


def _calls(adapter, parsed):
    index = SymbolIndex.from_parsed_files((parsed,))
    return tuple(
        item for item in adapter.resolve_references(parsed, index).relations
        if item.relation_type == "CALLS"
    )


def test_unique_same_file_function_resolves_and_tracks_enclosing_owner():
    adapter = _adapter()
    parsed = adapter.parse_file(
        b"helper() { :; }\nrun() { helper; external-tool; }\n", "app.sh",
    )
    symbols = {item.local_name: item for item in parsed.symbols}
    calls = _calls(adapter, parsed)
    helper = next(item for item in calls if item.target_symbol_id)
    external = next(item for item in calls if item.target_reference == "external-tool")
    assert helper.source_symbol_id == symbols["run"].symbol_id
    assert helper.target_symbol_id == symbols["helper"].symbol_id
    assert helper.resolution_state == "resolved"
    assert external.resolution_state == "unresolved"


def test_dynamic_outer_command_is_omitted_but_literal_subcommand_is_kept():
    parsed = _adapter().parse_file(
        b"inner() { :; }\nrun() { $COMMAND; $(inner); }\n", "app.sh",
    )
    targets = {item.target_reference for item in parsed.references}
    assert "inner" in targets
    assert "$COMMAND" not in targets
    assert "$(inner)" not in targets


def test_source_and_dot_create_no_reference_or_cross_file_resolution():
    parsed = _adapter().parse_file(
        b"source ./lib.sh\n. ./more.sh\nrun() { helper; }\n", "app.sh",
    )
    targets = {item.target_reference for item in parsed.references}
    assert "source" not in targets
    assert "." not in targets
    assert "./lib.sh" not in targets
    assert "helper" in targets


def test_duplicate_function_targets_are_ambiguous():
    adapter = _adapter()
    parsed = adapter.parse_file(
        b"dup() { :; }\ndup() { :; }\ndup\n", "dup.sh",
    )
    calls = _calls(adapter, parsed)
    ambiguous = [item for item in calls if item.resolution_state == "ambiguous"]
    assert len(ambiguous) == 2
    assert len({item.target_symbol_id for item in ambiguous}) == 2
```

Expected: the test module contains direct assertions for all R3 resolution states,
source ownership, dynamic omission, and source-command exclusion.

- [ ] **Step 2: Run call tests and verify they fail for missing references**

Run: `uv run pytest tests/codegraph/test_bash_adapter.py -k "call or dynamic or source or duplicate" -v`

Expected: failures show missing `CALLS` references/relations, not parser execution.

- [ ] **Step 3: Implement literal-command extraction**

Add a `_literal_command_name(command)` helper that returns `(text, command_name_node)`
only when the `command_name` field contains one leaf `word` and no expansion or command
substitution. Walk descendants so literal commands inside a command substitution remain
eligible even when the outer command is omitted. Skip `source` and `.` exactly.

Build `ReferenceRecord` with:

```python
ReferenceRecord(
    source_symbol_id=enclosing_symbol_id,
    source_file_id=file.file_id,
    source_module_id=None if enclosing_symbol_id else file.module_id,
    relation_type="CALLS",
    target_reference=literal_name,
    source_line=name_node.start_point[0] + 1,
    source_end_line=name_node.end_point[0] + 1,
    source_byte=name_node.start_byte,
    source_end_byte=name_node.end_byte,
)
```

Do not store command arguments or a reconstructed command string.

Expected: `ParsedFile.references` contains exact command-name ranges and correct
module/function ownership, while `source`, `.`, and dynamic outer names are absent.

- [ ] **Step 4: Implement same-file qualification and resolver isolation**

Before invoking the generic resolver, rebuild a reference with
`dataclasses.replace`:

- matching local function exists in `parsed.symbols` → target
  `<module_qualified_name>.<literal_name>`, `resolution_scope="file"`;
- no matching local function → retain literal target and
  `resolution_hint="unresolved"`.

Add this map entry to `resolver.LANGUAGE_FAMILIES`:

```python
"bash": frozenset({"bash"}),
```

Return sorted `declaration_relations` plus generic `resolve_references`. Multiple Bash
symbols sharing the qualified target must flow unchanged into generic resolver ambiguity.

Expected: the resolver receives only Bash-family same-file qualified targets; unique,
duplicate, and absent candidates map respectively to resolved, ambiguous, and unresolved.

- [ ] **Step 5: Run adapter and resolver tests**

Run: `uv run pytest tests/codegraph/test_bash_adapter.py tests/codegraph/test_resolver.py -q`

Expected: exit `0`; unique local calls are resolved, duplicate calls ambiguous, external
calls unresolved, and source/dynamic forms create no guessed target.

- [ ] **Step 6: Parent review and checkpoint commit**

```bash
git add src/iwiki_mcp/codegraph/languages/bash.py src/iwiki_mcp/codegraph/resolver.py tests/codegraph/test_bash_adapter.py tests/codegraph/test_resolver.py
git commit -m "feat(codegraph): resolve static Bash calls"
```

Expected: one commit containing only Task 2 paths.

---

### Task 3: Register Bash in configuration, factories, and context

**Closes:** R1 (explicit `.sh` discovery), R6 (unchanged defaults/contracts), R7
(search/context compatibility).

**Files:**
- Modify: `src/iwiki_mcp/codegraph/config.py:18-68`
- Modify: `src/iwiki_mcp/codegraph/application.py:1-225`
- Modify: `src/iwiki_mcp/codegraph/context.py:20-29`
- Modify: `tests/codegraph/test_config_location_models.py:551-585`
- Modify: `tests/codegraph/test_context.py:70-110`
- Modify: `tests/codegraph/test_server_tools.py:220-250`

- [ ] **Step 1: Write failing configuration, factory, and context validation tests**

```python
def test_languages_accepts_bash():
    config = CodeGraphConfig.from_mapping({"languages": ["python", "bash"]})
    assert config.languages == ("python", "bash")


def test_unknown_language_message_lists_bash():
    with pytest.raises(CodeGraphConfigError) as excinfo:
        CodeGraphConfig.from_mapping({"languages": ["ruby"]})
    assert "python, typescript, javascript, bash" in str(excinfo.value)
```

Add a server-handler assertion that `wiki_code_index(languages=["bash"])` forwards
`["bash"]` as the explicit one-shot selection, while an omitted `languages` argument
continues to use the persistent/default configuration path.

Add an application-factory assertion to
`tests/codegraph/test_config_location_models.py`:

```python
from iwiki_mcp.codegraph import application


factory = application.code_graph_adapter_factories("domain")["bash"]
assert factory.extensions == (".sh",)
assert factory.adapter_version == "bash-adapter-v1"
assert factory.bind(("run.sh",)).adapter.language == "bash"
```

Add a context validator assertion using canonical-width hashes:

```python
request = validate_context_request(["sh:symbol:" + "a" * 64])
assert request.seeds == ("sh:symbol:" + "a" * 64,)
```

Expected: focused tests explicitly pin persistent and one-shot selection, unchanged
defaults, production factory identity, and canonical `sh:symbol` validation before
production registration changes.

- [ ] **Step 2: Run focused wiring tests and verify rejection**

Run: `uv run pytest tests/codegraph/test_config_location_models.py tests/codegraph/test_context.py tests/codegraph/test_server_tools.py -k "bash or known_language" -v`

Expected: Bash config/filter/seed cases fail because Bash is not registered.

- [ ] **Step 3: Register Bash without changing defaults**

Make these exact changes:

```python
KNOWN_LANGUAGES = frozenset({"python", "typescript", "javascript", "bash"})
```

Keep `CodeGraphConfig.languages = ("python",)`. Update the validation message to list
`python, typescript, javascript, bash`.

In `application.py`, import `bash`, define `_BASH_PARSER_VERSION` from
`_distribution_version("tree-sitter-bash")`, create `BashAdapter`, and register an
`AdapterFactory` whose extensions, parser version, grammar-version members, and adapter
version match the spec. The grammar string includes installed `tree-sitter` and
`tree-sitter-bash` versions; it does not include `tree-sitter-language-pack`.

Update context prefix validation to:

```python
_CANONICAL_ENTITY_ID = re.compile(
    r"(?:py|ts|js|sh):(?:file|module|symbol):[0-9a-f]{64}\Z"
)
```

Update the nearby synchronization comment to list `"sh"`.

Expected: Bash appears in the shared known-language set, production factory map,
fingerprint version inputs, and context ID validator while the Python-only default stays
unchanged and one-shot selection follows the existing known-language handler contract.

- [ ] **Step 4: Run all wiring tests**

Run: `uv run pytest tests/codegraph/test_config_location_models.py tests/codegraph/test_context.py tests/codegraph/test_server_tools.py tests/codegraph/test_runtime.py -q`

Expected: exit `0`; default config remains Python-only, explicit persistent and one-shot
Bash selections plus Bash filters/seeds are accepted, and existing language cases remain
green.

- [ ] **Step 5: Parent review and checkpoint commit**

```bash
git add src/iwiki_mcp/codegraph/config.py src/iwiki_mcp/codegraph/application.py src/iwiki_mcp/codegraph/context.py tests/codegraph/test_config_location_models.py tests/codegraph/test_context.py tests/codegraph/test_server_tools.py
git commit -m "feat(codegraph): register Bash language support"
```

Expected: one commit containing only Task 3 paths.

---

### Task 4: Prove Bash-only, mixed-language, query, context, and safety behavior

**Closes:** R1–R7 through the production `CodeGraphIndexer`, SQLite query, and context
paths.

**Files:**
- Create: `tests/fixtures/codegraph/bash_basic/main.sh`
- Create: `tests/fixtures/codegraph/bash_basic/lib.sh`
- Create: `tests/fixtures/codegraph/bash_basic/ignored.bash`
- Create: `tests/fixtures/codegraph/bash_basic/entrypoint`
- Create: `tests/fixtures/codegraph/mixed_python_bash/service.py`
- Create: `tests/fixtures/codegraph/mixed_python_bash/scripts/main.sh`
- Modify: `tests/codegraph/test_mixed_language_indexing.py`

- [ ] **Step 1: Add deterministic Bash fixtures**

`bash_basic/main.sh`:

```bash
source ./lib.sh

helper() { printf '%s\n' ok; }
run() {
    helper
    external-tool
    "$DYNAMIC_COMMAND"
}
run
```

`bash_basic/lib.sh`:

```bash
helper() { printf '%s\n' other; }
library_only() { :; }
```

`ignored.bash` and extensionless `entrypoint` each contain a uniquely named function
that assertions can prove absent. The mixed fixture contains one Python function and one
Bash function both named `shared_name`, plus a Bash `run` function calling its local
`shared_name`.

Expected: both fixture trees are deterministic, contain no executable test harness, and
exercise included `.sh`, excluded suffix/shebang cases, local calls, and name collisions.

- [ ] **Step 2: Write failing production-indexer tests**

Extend existing `_build_indexer`, query, and context helpers. Add assertions that:

```python
rows = _build_indexer(
    tmp_path / "bash", FIXTURES / "bash_basic", languages=("bash",),
).build_rows()
assert {row["path"] for row in rows.tables["files"]} == {"main.sh", "lib.sh"}
assert {row["language"] for row in rows.tables["files"]} == {"bash"}
assert {row["local_name"] for row in rows.tables["symbols"]} >= {
    "helper", "run", "library_only",
}
```

Build the mixed fixture once with `("python",)` and once with `("python", "bash")`.
Filter file rows by `language == "python"`, and symbol/relation rows by `py:` prefix;
assert those lists are byte-for-byte equal. In the Python-only build, explicitly assert
that no file path ends with `.sh`. Assert all Bash IDs use `sh:` and the shared name does
not resolve across language families.

Extend `_build_indexer` so its `languages` argument may be omitted. When omitted, build
the production indexer with `CodeGraphConfig()` rather than supplying a language tuple.
Build `bash_basic` through this no-argument path and assert the snapshot header uses only
the Python default and no file row ends with `.sh`. Keep the explicit `("python",)` mixed
comparison above as separate existing-language compatibility evidence. This test proves
the automatic path with neither persistent Bash configuration nor a one-shot language
override, not merely another explicit language selection.

Build and search with `languages=["bash"]`; assert results exist and every entity ID
starts with `sh:`. Seed `CodeGraphContext` with the Bash module ID at depth `2`; assert
`DECLARES` and `CALLS` appear and returned data has no `source` field by default.

Expected: new integration tests cover every R1–R7 row/query/context assertion using the
production factory and no custom adapter map.

- [ ] **Step 3: Write the non-execution sentinel test**

Create a temporary project whose script bytes contain the concrete sentinel path:

```python
sentinel = tmp_path / "must-not-exist"
source_marker = "SOURCE_BODY_MARKER_MUST_NOT_PERSIST"
argument_marker = "COMMAND_ARGUMENT_MARKER_MUST_NOT_PERSIST"
project = tmp_path / "project"
project.mkdir()
(project / "danger.sh").write_bytes(
    f"# {source_marker}\n".encode()
    + b"source ./missing.sh\n"
    + f"touch {sentinel}\n".encode()
    + f"printf '%s' {argument_marker}\n".encode()
    + b"eval 'touch evaluated'\n"
    + b"$(touch substituted)\n"
    + b"safe() { :; }\n"
)
indexer = _build_indexer(
    tmp_path / "cache", project, languages=("bash",),
)
rows = indexer.build_rows()
assert not sentinel.exists()
assert rows.tables["files"]
serialized_rows = json.dumps(rows.tables, sort_keys=True, default=str)
assert str(sentinel) not in serialized_rows
assert source_marker not in serialized_rows
assert argument_marker not in serialized_rows
```

Parse the same bytes directly with `BashAdapter`, serialize `file`, `symbols`,
`references`, and `warnings` through `dataclasses.asdict`, and repeat all three negative
marker assertions against that JSON. Explicitly assert that `touch` and `printf` occur
only as normalized `ReferenceRecord.target_reference` command names; their argument text,
the source-only comment marker, and the sentinel path never occur in any parsed or indexed
record.

Expected: explicit parsed-record and serialized-table assertions fail on source-body,
command-argument, or sentinel-path persistence; the sentinel remains absent and the static
snapshot remains non-empty.

- [ ] **Step 4: Run the new integration tests**

Run: `uv run pytest tests/codegraph/test_mixed_language_indexing.py -k "bash" -v`

Expected: exit `0` because Tasks 1–3 already supply production behavior. A failure must
identify a product defect or an incorrect integration-helper assumption; fix the smallest
source consistent with the spec and do not add test-only adapter wiring.

- [ ] **Step 5: Run the complete mixed-language module**

Run: `uv run pytest tests/codegraph/test_mixed_language_indexing.py -q`

Expected: exit `0`; Python/TypeScript/JavaScript cases remain unchanged and Bash cases
prove opt-in discovery, isolation, search, context, and non-execution.

- [ ] **Step 6: Run immutable existing-language baselines**

Run: `uv run pytest tests/codegraph/test_typescript_golden.py tests/codegraph/test_mixed_language_baseline.py tests/codegraph/test_javascript_adapter.py -q`

Expected: exit `0`; no committed baseline JSON changes. Any diff is a HUMAN CHECKPOINT.

- [ ] **Step 7: Parent review and checkpoint commit**

```bash
git add tests/fixtures/codegraph/bash_basic tests/fixtures/codegraph/mixed_python_bash tests/codegraph/test_mixed_language_indexing.py
git commit -m "test(codegraph): cover Bash indexing integration"
```

Expected: one commit containing only Task 4 paths.

---

### Task 5: Document Bash configuration, graph semantics, and limits

**Closes:** R1 (explicit persistent or one-shot selection), R3 (same-file-only calls),
R5 (static-only and source-free graph), R7 (search/context usage).

**Files:**
- Modify: `README.md:370-455`
- Modify: `docs/README.ru.md:370-455`

- [ ] **Step 1: Update English user documentation**

In `README.md`, add `bash` to the accepted language list and example, then add a Bash
subsection stating exactly:

- only `.sh` is discovered; Bash must be listed in persistent configuration or an
  explicit one-shot `wiki_code_index` request;
- both Bash function declaration forms become function symbols;
- literal commands become `CALLS`; only a unique same-file function resolves;
- external commands remain unresolved; dynamic command names are omitted;
- `source` and `.` emit no `IMPORTS` and never enable cross-file resolution;
- parsing never invokes a shell or stores source bodies in graph metadata;
- `wiki_code_context` accepts `sh:` IDs and still excludes source by default.

Expected: English documentation states all seven points beside current code-graph
configuration and does not describe a broader suffix or resolution contract.

- [ ] **Step 2: Mirror the contract in Russian documentation**

Update `docs/README.ru.md` with the same technical contract. Preserve identifiers and
configuration names exactly; translate explanatory prose only.

Expected: Russian documentation has the same seven technical commitments and examples as
the English source, with no semantic drift.

- [ ] **Step 3: Verify documentation consistency**

Run these fixed-term checks for both documents:

```bash
for doc in README.md docs/README.ru.md; do rg -n 'code_graph\.languages|wiki_code_index' "$doc"; rg -n '\.sh' "$doc"; rg -n 'CALLS' "$doc"; rg -n 'source|IMPORTS' "$doc"; rg -n 'shell|оболоч' "$doc"; rg -n 'wiki_code_context|sh:' "$doc"; done
```

Then compare each matched Bash subsection against the seven-row checklist from Steps 1
and 2: persistent plus one-shot opt-in, `.sh`-only scope, both declaration forms,
same-file-only `CALLS`, unresolved external plus omitted dynamic commands, no source
imports, and static/source-free context behavior. Record zero missing rows and zero claims
of `.bash` or shebang-only discovery, cross-file resolution, emitted `IMPORTS`, or shell
execution.

Expected: every fixed-term command exits `0`; checklist review records all seven required
rows and no forbidden claim in either language.

- [ ] **Step 4: Run focused and full verification**

Run each command independently:

```bash
uv run pytest tests/codegraph/test_bash_adapter.py tests/codegraph/test_resolver.py tests/codegraph/test_config_location_models.py tests/codegraph/test_context.py tests/codegraph/test_mixed_language_indexing.py -q
uv run pytest -q
uv run flake8 src tests
uv run flake8 src/iwiki_mcp/__init__.py src/iwiki_mcp/codegraph/application.py src/iwiki_mcp/codegraph/config.py src/iwiki_mcp/codegraph/context.py src/iwiki_mcp/codegraph/languages/bash.py src/iwiki_mcp/codegraph/resolver.py tests/codegraph/test_bash_adapter.py tests/codegraph/test_config_location_models.py tests/codegraph/test_context.py tests/codegraph/test_mixed_language_indexing.py tests/codegraph/test_resolver.py tests/codegraph/test_server_tools.py tests/fixtures/codegraph/mixed_python_bash/service.py tests/test_package.py
git diff --exit-code origin/master -- tests/eval
uv run iwiki-mcp --help
git diff --check
git diff --check origin/master...HEAD
```

Expected: focused and full pytest exit `0`, and the full test count has zero failures.
Repository-wide flake8 is inspected in full; its only accepted non-zero result is the
known `tests/eval/*` baseline, which must remain byte-identical to `origin/master`.
Flake8 over every changed Python path exits `0`; CLI help prints usage; both working-tree
and committed diff checks report no whitespace errors.

- [ ] **Step 5: Parent update of bound iwiki documentation**

Parent reads current revisions and updates the `Languages` section of
`concept/code-graph-configuration` through `wiki_update_page`, then creates
`concept/code-graph-bash-extraction` through `wiki_write_page`. The new page documents
grammar loading, `.sh` identity, function extraction, same-file calls, source-command
exclusion, syntax-error behavior, and the static-only boundary. The wiki text must match
README scope and may identify Bash as implemented only after Step 4 passes. Run
`wiki_lint(domain="iwiki-mcp")`.

Expected: successful hosted PostgreSQL revisions; no broken/stale/task-page
finding attributable to this change. Do not call Git-only `wiki_sync`.

- [ ] **Step 6: Parent documentation checkpoint commit**

```bash
git add README.md docs/README.ru.md
git commit -m "docs(codegraph): document Bash support"
```

Expected: one commit containing only Task 5 repository paths. Wiki revisions are
durable external evidence and are not staged.

---

### Task 6: Reconcile the chain result and open the pull request

**Closes:** R1–R7 acceptance evidence and the user's requested reviewed PR delivery.

**Files:**
- Modify: `docs/superpowers/plans/2026-08-27-bash-code-graph-support.md` frontmatter
  through `check-chain result`
- Optional create: `docs/superpowers/reports/bash-code-graph-support-results.html` only
  after explicit user acceptance

- [ ] **Step 1: Review the complete branch diff against the plan**

Run:

```bash
git status --short --branch
git diff --stat origin/master...HEAD
git diff --name-status origin/master...HEAD
```

Expected: branch is `dev-bash-code-graph-support`; every changed path maps to Tasks 1–5;
there are no schema/publication changes, baseline fixture rewrites, secrets, or unrelated
files.

- [ ] **Step 2: Run plan-backed result reconciliation**

Invoke `check-chain result` for
`docs/superpowers/plans/2026-08-27-bash-code-graph-support.md` using
`origin/master` as the diff base. Review every changed implementation, test, dependency,
lockfile, and documentation path; map R1–R7 and Tasks 1–5 to concrete verification
evidence.

Expected: `result_check.verdict: OK`, current plan hash, `reviewed: true`,
`docs_checked: true`, successful task-ledger close evidence, and no missing, partial, or
excess commitment. A non-OK verdict stays in Task 6 until corrected and reverified.

- [ ] **Step 3: Offer the optional final HTML report**

After result verdict, ask the user whether to generate the result-only report. Generate
it through `html-report` only on acceptance; declining leaves no HTML artifact.

Expected: explicit user decision recorded; report path exists only when accepted.

- [ ] **Step 4: Commit result metadata when it changed**

```bash
git add docs/superpowers/plans/2026-08-27-bash-code-graph-support.md
git commit -m "docs(chain): record Bash code graph result"
```

Expected: result metadata is committed; skip this commit only when the tracked plan is
already byte-identical after reconciliation. When Step 3 generated the optional report,
stage that report in the same commit.

- [ ] **Step 5: Push and open the PR against `master`**

Use `git-workflow` for the push and GitHub PR creation. PR title:
`feat(codegraph): add Bash language support`. PR body contains summary, exact verification
commands/results, `.sh` and same-file-only limitations, no-schema statement, iwiki lint
evidence, and the linked task topic.

Expected: `origin/dev-bash-code-graph-support` is updated and GitHub returns one PR URL
targeting `master`; no direct merge or push to `master` occurs.

---

## Expected implementation outputs

After execution, expected repository output is one new focused Bash adapter, one new
runtime grammar dependency, config/factory/resolver/context registration, Bash adapter
tests, Bash-only and mixed fixtures, production integration coverage, bilingual docs,
lockfile refresh, and patch version `0.7.194`. No schema or publication file is expected
to change.

## Problems closed

- Shell-only repositories become searchable when Bash is explicitly selected through
  persistent configuration or a one-shot indexing request (R1, R2).
- Mixed repositories expose Python and Bash without identifier or resolution collisions
  (R4, R6).
- Literal same-file calls are visible while duplicate, external, dynamic, and cross-file
  cases never become guessed resolved edges (R3).
- Indexing remains static and source-free at the graph-storage boundary (R5).
- Existing search and context APIs accept Bash without new public tools (R7).

## Verification evidence expected

Result reconciliation must map every task to its changed paths and successful focused
command. It must also contain successful full pytest, changed-path flake8, CLI-help,
diff-check, and iwiki-lint evidence; the inspected repository-wide flake8 diagnostic and
proof that every reported `tests/eval/*` path is unchanged from `origin/master`;
immutable baseline status; sentinel absence; config-disabled `.sh` absence; mixed Python
row equality; Bash search/context results; and version/lockfile evidence. These are
expected outputs, not claims about current implementation state.

## Remaining user decisions

None. The approved spec fixes `.sh`-only discovery, native `tree-sitter-bash`, no source
imports, same-file-only resolution, no schema change, and static-only safety. Any pressure
to cross a HUMAN CHECKPOINT returns to the earliest affected chain artifact.
