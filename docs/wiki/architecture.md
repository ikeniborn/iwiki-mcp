# iwiki-mcp architecture

## Overview
`iwiki-mcp` is a stdio MCP server that fronts a shared, git-synced wiki base split into domains. The agent authors Markdown pages; the server validates, stores, embeds, and searches them. This page maps the package layout, the on-disk base model, the `iwiki-mcp` entry point, and the request lifecycle that ties [[mcp-server#Tool surface]], [[base-binding#Binding model]], [[indexing#Index domain]], and [[retrieval#Hybrid search]] together.

## Purpose
Give coding agents (Claude Code, Codex) a queryable, persistent knowledge base. Pages are plain Markdown committed to a git repo so knowledge syncs across machines and projects, while the server adds embedding-based recall over a structure the agent can write.

## Package layout
The `src/iwiki_mcp/` package has a thin top layer and an `engine/` core. Top layer: `server.py` (MCP tools), `base.py` (binding/paths), `indexer.py` (ingest), `retrieval.py` (query), `sync.py` (git), `resources.py` (authoring rules). The `engine/` holds reusable, mostly framework-free modules: `config`, `chunk`, `embed`, `store`, `search`, `grep`, `related`, `links`, `validate`, `lint`. The split keeps `engine/` unit-testable without the MCP runtime.

## Wiki base and domains
A wiki base is a directory (ideally a git repo) whose immediate subdirectories are domains. Each domain holds `*.md` pages plus a `.iwiki/` folder with `index.jsonl` (the embedding store) and `log.jsonl` (the ingest log). A project binds to domains it reads from and one domain it writes to. See [[base-binding#Domains and paths]].

## Data flow
On write, `wiki_write_page` validates structure, persists the page, appends an ingest-log record, then re-indexes the domain (chunk → embed → quantize → JSONL). On query, `wiki_search` resolves the in-scope domains and runs hybrid vector + lexical retrieval merged across them. Both paths fail soft and return JSON. See [[indexing#Index domain]] and [[retrieval#Hybrid search]].

## Entry point
`pyproject.toml` declares the console script `iwiki-mcp = iwiki_mcp.server:main`. `main()` parses an optional `--project DIR` (which sets `IWIKI_PROJECT_DIR`) and then calls `mcp.run()`, a `FastMCP("iwiki")` instance speaking MCP over stdio. The server is not a daemon; it lives for the client session that spawned it.

## Dependencies
Requires Python `>=3.10`. Runtime deps: `mcp>=1.2.0` (FastMCP), `httpx` (embeddings HTTP), `pathspec` (`.iwikiignore`), `numpy` (vector math), and `tomli` only on Python `<3.11` (else stdlib `tomllib`). Build backend is `hatchling`; tests use `pytest` with `pytest-asyncio`.
