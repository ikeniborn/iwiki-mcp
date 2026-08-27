---
review:
  intent_hash: a4dbe4195cc1accc
  last_run: 2026-08-27
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings: []
workflow:
  route: chain
  continuation: full
---

# Intent: bash-code-graph-support

**Date:** 2026-08-26
**Status:** approved

## Objective

Extend the code graph to support Bash so shell-only projects and Bash components in
mixed-language repositories can be indexed and explored. This is needed now because
the graph cannot currently represent code implemented in Bash scripts.

## Desired Outcomes

- A project that explicitly enables Bash through persistent configuration or a one-shot
  indexing request discovers supported Bash source files and exposes their file and
  function records through code-graph search and context queries.
- Statically provable Bash function calls are represented as graph references;
  ambiguous dynamic calls remain unresolved rather than guessed.
- A mixed Python-and-Bash project indexes both languages in one graph without losing
  either language's records.

## Health Metrics

- Existing Python, TypeScript, and JavaScript code-graph tests continue to pass with
  byte-stable records for unchanged fixtures.
- A project that neither configures nor explicitly requests Bash does not discover or
  parse Bash files.
- Indexing never executes project source, including Bash scripts or their referenced
  files.

## Strategic Context

- Interacts with: LanguageAdapter factories, code-graph language configuration,
  discovery, indexing, storage, search, context queries, and users of shell-only or
  mixed-language repositories.
- Priority trade-off: trust over speed or breadth; emit only syntax-backed evidence.

## Constraints

### Steering (behavioral guidance)

- Prefer conservative, deterministic extraction over speculative Bash resolution.
- Keep Bash isolated behind explicit opt-in: persistent `code_graph.languages`
  configuration or a one-shot `wiki_code_index(languages=["bash"])` request. Defaults
  never include Bash.
- Match the existing adapter protocol and graph record contracts where Bash syntax
  permits it.

### Hard (architectural enforcement)

- Parse raw source bytes statically; never execute scripts, `source` files, `eval`,
  command substitutions, or shell expansions.
- Never persist source text in graph metadata, consistent with existing language
  adapters.
- Do not create a resolved graph edge for a dynamic or ambiguous Bash invocation.

## Autonomy Zones

- Full autonomy (reversible, low risk): adapter implementation, focused fixtures,
  tests, documentation, and non-breaking configuration wiring.
- Guarded (log + confidence threshold): static extraction and reference resolution;
  keep evidence only when syntax and local scope prove it, otherwise record an
  unresolved reference or omit it.
- Proposal-first (needs approval): a public configuration incompatibility, a graph
  schema change, or any change that weakens the static-only security boundary.
- No autonomy (human only): executing project Bash code, accessing external project
  resources through scripts, or destructive data operations.

## Stop Rules

- Halt if: Bash support requires execution, evaluation, or source-text persistence to
  extract the promised evidence, or existing-language regressions remain.
- Escalate if: a required Bash construct has several equally plausible graph meanings
  or needs a public-contract, schema, or security-boundary change.
- Done when: a persistently configured or explicitly one-shot-selected Bash fixture
  exposes file/function records and syntax-proven call evidence through search and
  context; a mixed-language fixture preserves both languages; defaults exclude Bash;
  and the full test suite passes without executing source.
