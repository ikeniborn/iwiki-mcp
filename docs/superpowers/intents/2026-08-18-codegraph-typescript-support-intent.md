---
review:
  intent_hash: d94e2f4ae44fe73a
  last_run: 2026-08-18
  phases:
    structure:
      status: passed
    completeness:
      status: passed
    clarity:
      status: passed
    consistency:
      status: passed
    alignment:
      status: passed
  findings:
    - id: F-001
      phase: clarity
      severity: WARNING
      section: "Desired Outcomes"
      section_hash: eaaf1a83c3289779
      fragment: "single vs. separate-but-coexisting snapshots is an open design question for brainstorm/spec, not decided here"
      text: "Outcome 3 defers the mixed-repo snapshot model decision instead of stating an observable target."
      fix: "Acceptable — explicitly deferred; brainstorm/spec must resolve it before plan."
      verdict: open
      verdict_at: null
---
# Intent: codegraph-typescript-support

**Date:** 2026-08-18
**Status:** approved

## Objective

The code graph engine (`src/iwiki_mcp/codegraph/`) currently indexes Python only.
`LanguageAdapter` (protocol: `language`, `prefix`, `extensions`, `parse_file`,
`resolve_references`) and `discovery.py` are already language-neutral, and the identity
scheme (`concept/code-graph-identities`) derives its prefix from the adapter rather than
hard-coding Python. The remaining blocker is a hard `languages == ("python",)` gate
duplicated across `config.py`, `query.py`, `runtime.py`, and `sqlite_adapter.py`.

Add full parity for TypeScript/TSX with the Python adapter — declaration extraction,
reference resolution, member/mutation tracking — so agents building or auditing
TypeScript code can query the same graph (`wiki_code_search`, `wiki_code_context`)
they already use for Python.

## Desired Outcomes

- `wiki_code_index` indexes `.ts`/`.tsx` files alongside `.py` in the same run.
- `wiki_code_search` / `wiki_code_context` return correct TypeScript symbols and
  relations (imports, calls, class/interface members, type references).
- A mixed Python + TypeScript repository builds a working graph — single vs.
  separate-but-coexisting snapshots is an open design question for brainstorm/spec,
  not decided here.

## Health Metrics

- `reference/code-graph-benchmark` numbers do not regress after TypeScript support
  lands.
- Python-only repositories: indexing results, symbol/relation IDs, and search/resolve
  behavior are bit-for-bit unchanged before/after this change (regression gate, not a
  frozen numeric limit).
- `code_graph` numeric limits (`max_file_bytes`, `max_total_files`,
  `max_rebuild_seconds`, publication batch bounds) may be revisited for TypeScript
  workloads — they are not a health metric themselves.

## Strategic Context

- Interacts with: `code_graph.languages` in `.iwiki.toml` (public project contract),
  `config.py` / `query.py` / `runtime.py` / `sqlite_adapter.py` (python-only gates),
  hosted PostgreSQL publication (`wiki_code_publish_begin/_batch/_finalize`), MCP tools
  `wiki_code_search` / `wiki_code_context`, and every agent consuming the graph.
- Priority trade-off: **trust** (accurate type resolution) over speed or cost.

## Constraints

### Steering (behavioral guidance)

- Prefer the TS Compiler API / `tsc` (not an LSP/`tsserver` process) for the optional
  type-resolution boost — less state and integration complexity inside a stdio MCP
  server.
- Re-check the Python regression gate at every implementation step, not only at the
  end.

### Hard (architectural enforcement)

- Never execute, compile, or evaluate TS/JS source; static AST analysis only.
- A Tree-sitter-only baseline is mandatory and must work with zero external
  dependencies, mirroring `PythonAdapter`.
- An optional `tsc`/TS Compiler API boost runs as an isolated, opt-in step; its
  absence or failure never blocks or degrades the Tree-sitter baseline result.
- `LanguageAdapter` protocol is unchanged.
- The identity scheme (SHA-256 hash + adapter-supplied prefix) is unchanged.

## Autonomy Zones

- Full autonomy (reversible, low risk): the TS/TSX Tree-sitter adapter (declaration
  extraction, mirroring `PythonAdapter`), `.ts`/`.tsx` discovery extensions, tests.
- Guarded (log + confidence threshold): revising `.iwiki.toml` numeric limits
  (`max_file_bytes`, `max_total_files`, `max_rebuild_seconds`, publication batch
  bounds) for TypeScript workloads.
- Proposal-first (needs approval): lifting the python-only gate in `config.py` /
  `query.py` / `runtime.py` / `sqlite_adapter.py` (the public `code_graph.languages`
  contract); the snapshot-header design for mixed-language repos (single graph vs.
  coexisting per-language snapshots).
- No autonomy (human only): changing the `LanguageAdapter` protocol; enabling the
  `tsc` boost by default without explicit opt-in; any execution/compilation of source.

> These zones OVERRIDE subagent-driven-development's "continuous execution, don't
> pause" default. Any task touching proposal-first / no-go decisions is marked HUMAN
> CHECKPOINT in the plan.

## Stop Rules

- Halt if: a Python-only regression is detected (benchmark regression, or a changed
  symbol/relation ID, search result, or resolution outcome for an existing Python
  fixture).
- Escalate if: the mixed-repo snapshot design (single graph vs. coexisting snapshots)
  cannot be resolved within spec/brainstorm and blocks implementation.
- Done when: `wiki_code_index` indexes a real TypeScript/TSX fixture repo producing
  schema-v2 `FileRecord`/`SymbolRecord`/`RelationRecord` records; `wiki_code_search`
  and `wiki_code_context` return correct results against that fixture; a mixed
  Python+TypeScript repo indexes and queries successfully; the Python-only benchmark
  and fixture suite shows zero regression.
