# Configurable Search Mode API Evaluation Evidence

## Corpus

The offline corpus fixes six queries across six nested pages and evaluates at
`top_k=3`. Coverage includes semantic phrasing, exact identifiers, graph traversal,
lexical matching, duplicate and distractor behavior, and a global chunk.

## Baseline

Command:

```bash
uv run python -c 'from eval.hierarchical import fixtures, harness; print(harness.run_baseline_eval(fixtures.VAULT, fixtures.QUERIES, fixtures.embed, top_k=3))'
```

Observed metrics:

```text
{'recall_at_k': 0.8333333333333334, 'mrr_at_k': 0.75}
```

The focused characterization test also passes:

```bash
uv run pytest -q tests/eval/test_hierarchical_eval.py
```

```text
1 passed in 0.02s
```

## Candidate configuration

The initial candidate ceiling is 32 and the RRF constant is `k=60`. Preliminary
acceptance requires MRR above the baseline and recall at least equal to the baseline.
A fake reranker must cause no recall loss and must improve or preserve MRR.
