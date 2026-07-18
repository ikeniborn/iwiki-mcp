# Act

Topic: `reranker-effectiveness-evaluation`

## Action

Completed two bounded passes:

1. Measured the connected iwiki reranker in `hybrid`, `semantic`, and `lexical`
   modes without persisting credentials.
2. Ran a 32-call direct matrix against the approved endpoint and model. The
   matrix varied pool size, query form, document representation, decomposed
   query fusion, and provider `top_n`.
3. Added deterministic nDCG@k, intent-recall@k, and RRF evaluation helpers.
4. Kept the 32-candidate safety ceiling because later candidates include judged
   secondary-intent relevance.
5. Applied one bounded product fix: pass requested final `k` as provider
   `top_n` while still submitting the complete hydrated candidate batch.
6. Preserved every provider-unscored, stale, and unhydrated candidate in full
   preliminary order after the scored prefix.
7. Synchronized package version `0.7.2`, user documentation, and project wiki.
8. Ran focused tests, full tests, flake8, CLI help, and wiki lint.

No model, deployment, secret, index schema, or external service was changed.

## Changed Paths

- `docs/loen/reranker-effectiveness-evaluation/**`
- `eval/reranker/**`
- `tests/eval/**`
- `src/iwiki_mcp/engine/rerank.py`
- `src/iwiki_mcp/server.py`
- `src/iwiki_mcp/__init__.py`
- `tests/engine/test_rerank.py`
- `tests/test_server_search.py`
- `README.md`
- `docs/README.ru.md`
- `pyproject.toml`
- `uv.lock`

## Commands

```bash
uv run pytest -q tests/eval tests/engine/test_rerank.py tests/test_server_search.py tests/test_package.py
uv run pytest -q
uv run flake8 src tests eval
uv run iwiki-mcp --help
```

Direct measurements used only the user-approved endpoint and model. The API key
was read from runtime environment and was never printed or persisted.
