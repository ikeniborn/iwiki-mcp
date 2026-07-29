---
review:
  intent_hash: 8808dbb01aee5570
  last_run: 2026-07-28
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings: []
---

# Intent: search-pipeline-benchmark

**Date:** 2026-07-28
**Status:** approved

## Objective

Build a benchmark that shows bottlenecks in the wiki search and answer-formation
pipeline. The benchmark must evaluate the full retrieval flow rather than only the
reranker, because low search effectiveness may come from embeddings, chunking,
section splitting, candidate fusion, hydration, reranking, or the way returned
sections are assembled for downstream answer generation. This is needed now because
the current reranker evaluation showed only a modest rerank gain and did not isolate
where quality is lost in the pipeline.

## Desired Outcomes

- Per-stage metrics are visible for every involved search pipeline stage.
- Bottlenecks are listed with concrete evidence.
- The output includes a ranked backlog of follow-up fixes or experiments.
- Search modes, chunk settings, and model settings can be compared.

## Health Metrics

- API keys and provider details remain safe: no key is persisted, printed, or written
  into benchmark artifacts.
- The public `wiki_search` API and response shape remain stable.
- Latency ceiling does not degrade unless a separate explicit decision approves the
  trade-off.
- The benchmark does not write to the wiki base unless a separate explicit action is
  requested.
- Benchmark results are reproducible from committed fixtures, judgments, and commands.

## Strategic Context

- Interacts with: `eval/`, `retrieval.py`, `indexer.py`, `engine/chunk.py`,
  `engine/store.py`, `server.wiki_search`, iwiki MCP behavior, and domain fixtures.
- Priority trade-off: trust first, speed second.

## Constraints

### Steering (behavioral guidance)

- Measure the current pipeline before proposing behavior changes.
- Keep deterministic offline evaluation separate from optional live provider smoke tests.
- Attribute quality and latency to pipeline stages explicitly enough to identify the
  likely bottleneck.
- Prefer small, inspectable fixtures and judgments over broad unreviewed corpora.
- Compare settings only when the comparison has stable inputs and named metrics.

### Hard (architectural enforcement)

- Do not log, persist, print, or commit API keys or raw provider secrets.
- Do not change `wiki_search` public API, response shape, or default behavior as part of
  benchmark scaffolding.
- Do not write to the wiki base during benchmark runs without a separate explicit user
  action.
- Do not change embedding model, rerank model, chunk defaults, index schema, or search
  defaults without a proposal-first decision.
- Do not report live-provider measurements as deterministic regression tests.

## Autonomy Zones

- Full autonomy (reversible, low risk): benchmark harness structure, deterministic
  fixtures, judgments format, per-stage metrics, offline reports, and unit/integration
  tests.
- Guarded (log + confidence threshold): optional read-only live smoke mode, as long as it
  writes no wiki data and records no secrets.
- Proposal-first (needs approval): changing embedding or rerank models, changing chunk
  defaults, changing search defaults, changing index schema, or changing `wiki_search`
  API/response shape.
- No autonomy (human only): saving keys, writing to the wiki base during benchmark runs,
  or publishing benchmark artifacts containing secrets or private provider payloads.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions
> is marked HUMAN CHECKPOINT in the plan.

## Stop Rules

- Halt if: benchmark execution would require storing API keys or writing to the wiki base
  without explicit approval.
- Escalate if: isolating the bottleneck requires changing public API behavior, model
  defaults, chunk defaults, or index schema.
- Done when: a reproducible benchmark reports per-stage quality and latency metrics,
  identifies bottlenecks with evidence, produces a ranked backlog, supports comparison of
  search modes, chunk settings, and model settings, and preserves the listed health
  metrics.
