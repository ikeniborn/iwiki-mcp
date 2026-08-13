---
review:
  intent_hash: 42d4d324998d40e3
  last_run: 2026-08-13
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

# Intent: iwiki-mcp-code-graph

**Date:** 2026-08-09
**Status:** approved

## Objective

Give MCP clients precise, bounded structural context about a bound project's source code while preserving the existing Git-backed Wiki as the durable documentation system. The code graph must complement, not replace or destabilize, Markdown indexing and retrieval. The detailed source requirements are recorded in `docs/superpowers/intents/iwiki-mcp-code-graph-technical-requirements-final.md`.

## Desired Outcomes

- An MCP client can locate Python modules, files, classes, functions, and methods by qualified or local name and receive project-relative paths and source ranges.
- An MCP client can request a bounded neighborhood containing declarations, imports, basic calls, inheritance, and resolved Wiki links, with explicit freshness, truncation, and unresolved-reference signals.
- Operators can inspect, build, disable, and recover the per-domain code graph without a full build during server startup.
- Wiki authors can declare symbol-, file-, and source-scope selectors, detect stale or unsafe selectors, and retain human control over suggested links.
- Existing Wiki tools and `wiki_search` retain their current contracts and remain usable when code-graph dependencies, parsing, storage, or rebuilding fail.
- The architecture can add TypeScript support after the Python MVP without moving language-specific rules into the core.

## Health Metrics

- Existing Wiki test suite regression count remains `0`, and code-graph failures block `0` Wiki tool calls.
- Startup overhead without rebuild remains below `100 ms`; no-op freshness check remains below `200 ms`.
- Python top-level declaration and method extraction each reach at least `98%`; local import resolution reaches at least `95%`; statically resolvable calls reach at least `75%`; falsely resolved calls remain below `5%` on the approved benchmark corpus.
- Repeated full builds from identical source, configuration, schema, adapters, and resolver inputs produce identical graph content and fingerprint in `100%` of benchmark runs.
- For the first release, every unified-search case remains below `500 ms` warm maximum on the documented 100,000-entity benchmark environment. The prior `<150 ms` value remains a non-blocking post-v1 optimization target. Depth-1 traversal of at most 50 nodes remains below `300 ms`, indexing 1,000 Python files remains below `15 s`, database size remains below three times source text size, and indexing 10,000 files remains below `1 GiB` memory.
- Source text, credentials, and secret-like files appear in `0` external embedding requests and `0` logs.

## Strategic Context

- Interacts with: project/domain binding, `.iwiki.toml`, ignore rules, existing SQLite Wiki graph, Markdown frontmatter and lint, MCP server registration and fail-soft handlers, code discovery, language adapters, indexing, symbol/reference resolution, bounded context composition, benchmarks, operators, Wiki authors, and MCP clients.
- Priority trade-off: trust first, bounded latency second, implementation cost third. False certainty, source exposure, Wiki regressions, and partially published graph state are unacceptable optimizations for speed.

## Constraints

### Steering (behavioral guidance)

- Treat source code as truth and every code graph as a rebuildable, derived cache.
- Preserve unresolved and ambiguous references with confidence and resolution state instead of discarding them or presenting them as resolved.
- Keep core indexing and query services language-neutral; isolate parsing and language-specific resolution behind adapters.
- Prefer a full atomic rebuild plus fingerprint no-op for MVP; defer incremental invalidation until benchmark and correctness evidence justify it.
- Bound graph depth, nodes, files, source bytes, file size, total file count, lock wait, and auto-rebuild duration, and report truncation or staleness explicitly.
- Keep authoritative Wiki-to-code links in Markdown selectors; treat automatic matches as non-authoritative suggestions.
- Validate performance and extraction-quality targets against a documented benchmark corpus before treating provisional targets as release gates.

### Hard (architectural enforcement)

- Use a separate per-domain SQLite `CodeGraphStore`; do not mix AST/code nodes into existing Wiki `pages` or `edges`, and do not require an external graph database.
- Do not store a complete AST/CST, run a full index at MCP startup, change the `wiki_search` contract in MVP, add a background daemon, or add runtime tracing.
- Do not read outside resolved `project_dir`, follow unsafe symlinks, index dependency/generated/secret-like paths, expose absolute paths in portable IDs, log source or credentials, or send source to external embeddings.
- Publish full rebuilds atomically under a separate bounded writer lock; readers must never observe a temporary or partially built database.
- Code-graph errors must remain fail-soft and must not block existing Wiki tools.
- Stable identifiers must be independent of absolute paths and random/time-based values while distinguishing repository, language, module path, qualified name, nesting, and overload/signature identity.
- MVP supports Python module/file/class/function/method nodes and `DECLARES`, `IMPORTS`, basic `CALLS`, `INHERITS`, and resolved `DOCUMENTED_BY` relations. Impact analysis, hybrid RRF integration, cross-repository graphs, and Java/Go/C# are outside MVP.
- Every repository change, including design artifacts, must bump the package version according to project policy.

## Autonomy Zones

- Full autonomy (reversible, low risk): organize the approved requirements into traceable English intent/spec/plan sections; choose internal names and focused test-fixture layout when they preserve fixed contracts and boundaries.
- Guarded (log + confidence threshold): tune query budgets, benchmark fixtures, confidence reporting, and fail-soft diagnostics within approved hard limits, with measured evidence and explicit warnings.
- Proposal-first (needs approval): select final SQL schema, canonical symbol-ID format, MCP JSON contracts, stale/rebuild state transitions, resolver semantics, dependency packaging, delivery slices, or any change to MVP scope and provisional quality/performance gates.
- No autonomy (human only): weaken containment, secret exclusion, no-external-embedding, atomic publication, Wiki compatibility, fail-soft isolation, or human authority over suggested links; introduce external graph infrastructure or expand language/scope commitments beyond approved requirements.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions
> is marked HUMAN CHECKPOINT in the plan.

## Stop Rules

- Halt if: a design cannot preserve source containment, secret exclusion, atomic publication, stable portable IDs, or Wiki-tool availability.
- Halt if: the proposed spec silently changes `wiki_search`, makes suggested links authoritative, requires a startup full build, or cannot represent unresolved and ambiguous references.
- Escalate if: final schema, identifier, MCP contract, resolver, stale-state, dependency, or delivery-boundary alternatives cannot be resolved from the technical requirements and current repository contracts.
- Escalate if: benchmark evidence contradicts a provisional performance or quality target, or supporting Python and TypeScript in one delivery would compromise the Python MVP boundary.
- Done when: the checked architecture specification traces every MVP outcome and hard constraint to explicit requirements and acceptance criteria, identifies all remaining human checkpoints, and decomposes implementation into reviewable sequential delivery units without gaps or out-of-scope extras.
