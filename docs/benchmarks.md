# Benchmarks

*Part of the [iwiki-mcp documentation](../README.md#documentation). Русская версия: [benchmarks.ru.md](benchmarks.ru.md).*

Evaluation-only benchmarks. None of them changes production search behavior, fusion weights, or rerank settings.

## Code graph benchmark

Run the offline release evidence from the repository root:

```bash
uv run python -m eval.code_graph --fixture-root tests/fixtures/codegraph --output /tmp/iwiki-code-graph-evidence
```

It writes JSON and Markdown reports to the output directory. Every search case must
have a warm maximum below `<500 ms`; this is the blocking first-release gate. The
strict `<150 ms` comparison is reported as a non-blocking post-v1 target. Any other
blocking gate miss writes evidence and exits nonzero.

## Search pipeline benchmark

The bounded fusion benchmark under `eval/search_pipeline/` is evaluation-only: it does not change production search behavior, production fusion weights, or production rerank settings. Rerank-budget changes are deferred.

Replay existing evidence without credentials:

```bash
uv run python -m eval.search_pipeline --domain iwiki-mcp --out replace-with-report-dir --pareto --replay-evidence replace-with-evidence.json
```

After the replay passes, obtain operator confirmation before running a live benchmark. The live command uses an operator-created environment file; do not read or copy its credentials into the repository:

```bash
uv run python -m eval.search_pipeline --domain iwiki-mcp --out replace-with-report-dir --modes hybrid,lexical,semantic --pareto --env-file replace-with-operator-env-file
```

### Hard-negative gate

The bounded fusion decision derives hard-negative activation from the captured
baseline. It evaluates two reviewed hard-negative contracts; each is reported as
`active`, `unavailable`, or `invalid` from that baseline. A candidate can pass the
hard-negative gate only when at least two contracts are active.

`hard_negative_evidence_invalid` means one or more reviewed contracts have invalid
baseline evidence. `hard_negative_evidence_incomplete` means the evidence is valid
but fewer than two contracts are active. These diagnostic outcomes are distinct from
a candidate quality rejection after the gate is evaluated. Absolute ranks remain
diagnostic only; production search behavior, fusion weights, and rerank settings are
unchanged.

## Pareto benchmark

Run the evaluation-only Pareto experiment against a live, labeled search corpus:

```bash
uv run python -m eval.search_pipeline --domain iwiki-mcp --out ./pareto-evidence --env-file /path/to/operator.env --pareto
```

`--env-file` reads an operator-created environment file for that process only; it does
not create, modify, or write credentials back to the file. Keep the file outside the
report output directory and out of version control. The reports record sanitized
evidence only.

`--pareto` is an evaluation command, not a production configuration switch. Production
fusion weights or rerank batch constants are applied only after the report contains a
passing recommendation for the corresponding quality and latency gates. A
`needs_work` decision, including `no_passing_weight_map`, leaves production retrieval
behavior unchanged.
