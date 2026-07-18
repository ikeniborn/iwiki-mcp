# Context

Topic: `reranker-effectiveness-evaluation`

## Facts

- The project is Python 3.10+ with pytest and flake8.
- Read search builds up to 32 candidates from five independently ranked signals
  and fuses them with Reciprocal Rank Fusion.
- When `IWIKI_RERANK_MODEL` is configured, current Markdown chunks are hydrated
  and sent in one request to `${IWIKI_LLM_BASE_URL}/rerank`.
- The current request sets `top_n` to the full hydrated candidate count. The
  current implementation therefore asks the provider to return scores for all
  candidates even when final `wiki_search` output is smaller.
- The current rerank document is only chunk text. Domain, file, and heading are
  available before the request but are not included in the model input.
- Valid rerank scores replace RRF scores. Rows missing from a partial provider
  response follow in preliminary order.
- Reranker failure is fail-soft and preserves preliminary retrieval order.
- Existing deterministic evaluation proves the preliminary RRF pipeline on six
  synthetic queries, but its fake reranker does not measure the live model.
- The supplied call contains 32 documents and a request content length of
  approximately 43.6 KB. Several document strings shown in the captured payload
  contain LiteLLM database-log truncation markers.
- The supplied query is a long keyword bundle spanning cleanup, duplicate
  services, LiteLLM failures, logs, Prometheus, reboot, closure, and rollback.
- The supplied material does not include the rerank response, original
  preliminary order identities, relevance scores, request duration, token
  counts, or human relevance judgments. Effectiveness cannot be concluded from
  the request payload alone.
- The configured deployment is reported as
  `hosted_vllm/bge-reranker-v2-m3` behind a local LiteLLM-compatible endpoint.
- On 2026-07-18 the user approved the direct read-only evaluation endpoint
  `https://homelab.ikeniborn.ru/llm/v1/rerank` and model
  `lemonade-reranker-bge-reranker-v2-m3`.
- The user approved adding `src/iwiki_mcp/__init__.py` to mutable scope so the
  mandatory package version metadata can be synchronized.

## Initial Hypotheses

- Sending all 32 documents may improve recall opportunity but waste latency when
  final top-k is small.
- A natural-language intent statement or bounded subqueries may rank a
  multi-intent keyword bundle better than the raw query.
- Prefixing each chunk with stable file and heading context may improve
  discrimination among operational sections.
- Provider/log truncation must be distinguished from actual request truncation;
  copied truncated log strings are unsuitable as ground-truth model inputs.
- The strongest setting is likely query-class dependent, so one example is a
  seed case rather than sufficient evidence for a global change.

## Constraints

- Never persist API keys, authorization headers, user auth metadata, private
  host paths, or unsanitized request metadata.
- Do not modify LiteLLM, Lemonade, vLLM, systemd, Traefik, Prometheus, or any
  external deployment.
- Local model calls are read-only and limited to explicitly allowed loop
  endpoints supplied through environment variables.
- Do not weaken fail-soft search behavior.
- Do not change write-intent lookup, embedding behavior, indexing schema, public
  MCP response shape, or unrelated retrieval behavior.
- No auto-merge, release, push, or deployment.
- Product changes require deterministic tests, live evidence when the endpoint
  is available, documentation consistency, and repository verification.
- A missing live endpoint, missing full untruncated fixture, or insufficient
  relevance judgments causes handoff or report-only output, not speculative
  auto-fix.

## Mutable Scope

- `docs/loen/reranker-effectiveness-evaluation/**`
- `eval/reranker/**`
- `tests/eval/**`
- `tests/engine/test_rerank.py`
- focused rerank orchestration tests under `tests/test_server_search.py`
- `src/iwiki_mcp/engine/rerank.py`
- `src/iwiki_mcp/__init__.py`
- focused rerank settings in `src/iwiki_mcp/engine/config.py`
- rerank orchestration in `src/iwiki_mcp/server.py`
- candidate hydration or representation in `src/iwiki_mcp/retrieval.py`
- rerank documentation in `README.md`, `docs/README.ru.md`, and the bound
  `iwiki-mcp` wiki domain when functionality changes
- `pyproject.toml`

## Protected Scope

- Secrets, environment files, credentials, and captured auth metadata
- Live LiteLLM, Lemonade, vLLM, Traefik, Prometheus, and systemd configuration
- Deployment, merge, release, push, and protected branches
- Index format and stored wiki content
- Write-intent lookup and unrelated search/indexing behavior
- Files outside the mutable scope

## Existing Health

- `origin/master` and local `master` both pointed to `5853614` at loop start.
- The project wiki reported no broken references, stale pages, missing sources,
  or missing frontmatter. Existing advisory lint findings include one orphan,
  long section leads, and tag drift; they predate this topic.
