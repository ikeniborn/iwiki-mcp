# Env reference

*Part of the [iwiki-mcp documentation](../README.md#documentation). Русская версия: [env-reference.ru.md](env-reference.ru.md).*

**Required**

| Variable | Default | Meaning |
|---|---|---|
| `IWIKI_LLM_BASE_URL` | none | Base URL for an OpenAI-compatible embeddings endpoint, usually ending in `/v1`. |
| `IWIKI_LLM_KEY` | none | API key for the embeddings endpoint. |

**Embedding model**

| Variable | Default | Meaning |
|---|---|---|
| `IWIKI_EMBED_MODEL` | `text-embedding-3-small` | Embedding model name. |
| `IWIKI_EMBED_DIMENSIONS` | `1536` | Vector size. Must match the configured embedding model. |

**Chat model**

| Variable | Default | Meaning |
|---|---|---|
| `IWIKI_CHAT_MODEL` | empty | Optional chat model name for server-side `type`/`tags` classification. Reuses `IWIKI_LLM_BASE_URL` and `IWIKI_LLM_KEY`. When unset, frontmatter defaults to `type="concept"` with no tags. |

**System One shadow**

| Variable | Default | Meaning |
|---|---|---|
| `IWIKI_SYSTEM1_SHADOW` | disabled | Enables evaluation-only page-type observation during `wiki_write_page`. Accepted true values are `1`, `true`, `yes`, and `on`. The decision never changes frontmatter, paths, tags, write results, or errors. |
| `IWIKI_SYSTEM1_BASE_URL` | empty | Separate base URL for the local GPU System One service. Required when the shadow is enabled; iwiki calls `<base>/v1/systemone`. |
| `IWIKI_SYSTEM1_KEY` | empty | Separate bearer credential for the System One service. Required when the shadow is enabled and never reused from `IWIKI_LLM_KEY`. |

**Server lifecycle**

| Variable | Default | Meaning |
|---|---|---|
| `IWIKI_IDLE_TIMEOUT_SECONDS` | `0` | End a stdio MCP process after this many seconds with no incoming MCP activity. `0`, the default, disables the limit: a stdio server already exits when its client closes stdin, so the timer only ever removes a server whose client is still alive. Set a positive value for a headless run that must stop by itself. Active tool calls are allowed to finish, and so is a code-graph build started by `wiki_code_index` whose caller already took its job handle. A query-time auto-rebuild does not hold the process open. A client that needs the server later must reconnect or start a new MCP process. |

**Search tuning**

| Variable | Default | Meaning |
|---|---|---|
| `IWIKI_TOP_K` | `8` | Default maximum results for search and related-section lookup. |
| `IWIKI_SCORE_THRESHOLD` | `0.2` | Default minimum vector similarity for a returned section hit. |
| `IWIKI_SEARCH_MODE` | `hybrid` | Omitted `wiki_search.mode` default. Values are `hybrid`, `lexical`, or `semantic`; whitespace/case are normalized and an explicit mode wins. |
| `IWIKI_RERANK_MODEL` | empty | Optional LiteLLM-compatible reranker model. Reuses `IWIKI_LLM_BASE_URL` / `IWIKI_LLM_KEY`, scores one full candidate batch with a 60-second timeout, limits only the provider response rows to the final result count, and fails soft with sanitized metadata. |
| `IWIKI_GRAPH_DEPTH` | `2` | Wiki-link hop depth for the retrieval graph-expansion and related-section lookup. |
| `IWIKI_SEED_TOP_K` | `5` | How many articles the summary-vector pass seeds before graph expansion. |
| `IWIKI_BFS_TOP_K` | `10` | Cap on graph-expanded (non-seed) articles added to the candidate pool. |
| `IWIKI_SEED_THRESHOLD` | `0.15` | Minimum summary-vector similarity for an article to seed the search. |
| `IWIKI_WRITE_SEED_THRESHOLD` | `0.35` | Minimum summary-vector similarity to seed the precise write-target locate path used by `wiki_search(intent="write")`. Higher than `IWIKI_SEED_THRESHOLD` so an unrelated page is not offered as an upsert target. |

**Indexing**

| Variable | Default | Meaning |
|---|---|---|
| `IWIKI_CHUNK_SIZE` | `512` | Target token count per indexed chunk. |
| `IWIKI_CHUNK_OVERLAP` | `64` | Token overlap between adjacent chunks. |
| `IWIKI_SUMMARY_MAX_CHARS` | `400` | Maximum page summary length. |

**Location**

| Variable | Default | Meaning |
|---|---|---|
| `IWIKI_BASE_DIR` | none | Shared wiki base directory. Can be overridden by `.iwiki.toml` `base`. |
| `IWIKI_PROJECT_DIR` | process `cwd` | Project directory used to read `.iwiki.toml`. Can be overridden with `--project DIR`. |
