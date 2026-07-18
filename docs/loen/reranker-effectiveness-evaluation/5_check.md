# Check

Topic: `reranker-effectiveness-evaluation`

## Evidence

```text
Preflight:
- approved run contract: OK
- approved plan hash: 8caffc3f7f43d791
- approved endpoint/model: reachable
- credentials persisted: no
- synthetic relevant/noise score separation: 14.7855

Live 32-call matrix:
- supplied preliminary order: nDCG@8 0.648482, intent recall@8 1.0
- highest seed-case quality: natural/plain pool 16, nDCG@8 0.670004
- raw/plain pool 16: nDCG@8 0.665485, 0.6862 s, 21825 bytes
- raw/plain pool 32: nDCG@8 0.577617, 1.3319 s, 43601 bytes
- context prefixes: no quality gain
- decomposed query plus RRF: nDCG@8 0.544568, 3.8330 s
- same 32 documents, five runs:
  top_n=8 median 1.3320 s, 8 returned rows
  top_n=32 median 1.4081 s, 32 returned rows
- all measured variants retained intent recall@8 1.0

Focused verifier:
uv run pytest -q tests/eval tests/engine/test_rerank.py tests/test_server_search.py tests/test_package.py
exit 0 — 46 passed in 0.52s

Full verifier:
uv run pytest -q
exit 0 — 510 passed in 4.06s

Lint:
uv run flake8 src tests eval
exit 0 — no findings

CLI:
uv run iwiki-mcp --help
exit 0

Wiki lint:
- broken: none
- stale: none after retrieval page refresh
- missing source: none
- pre-existing orphan, long-lead advisories, and tag drift remain
```

## Result

Pass. The accepted fix reduces provider response rows without shrinking the
model-scored document batch or weakening the full preliminary fallback order.
No evidence supports replacing the model, adding document prefixes, decomposing
this query, or globally shrinking the candidate ceiling.

Evidence files:

- `evidence/live-matrix.json`
- `evidence/latest-test.json`
- `evidence/latest-test.log`
- `evidence/latest-full-test.log`
- `evidence/latest-lint.log`
