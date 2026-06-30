# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`iwiki-mcp` is a **stdio MCP server** (not a daemon — it lives for the spawning client session). It fronts a shared, git-synced wiki *base* split into *domains*. Coding agents author Markdown pages; the server validates structure, persists, embeds, indexes, and runs hybrid (vector + lexical) search across the domains a project is bound to.

User-facing setup (install, MCP registration in Claude Code / Codex, env reference, base/domain/binding model) lives in `README.md`. Self-documenting wiki pages are under `docs/wiki/` (`architecture.md` is the entry point).

## Commands

```bash
uv sync --extra dev          # install runtime + dev (pytest) deps
uv run pytest -q             # full test suite
uv run pytest tests/test_server_write.py::test_create_domain   # single test
uv run iwiki-mcp             # run the server from the checkout (stdio)
iwiki-mcp --help             # after `uv tool install .` / `pipx install .`
```

- No linter/formatter is configured (no ruff/black/mypy in `pyproject.toml`) — match surrounding style by hand.
- `pyproject.toml` sets `pythonpath = ["src"]` and `asyncio_mode = "auto"`, so tests import `iwiki_mcp` directly and async tests need no `@pytest.mark.asyncio`.
- Tests never hit the network: they `monkeypatch` `indexer.embed_texts` and set dummy `IWIKI_*` env vars. Follow that pattern — see `tests/test_server_write.py::_seed`.

## Architecture

Two layers under `src/iwiki_mcp/`:

- **Top layer** (MCP-aware): `server.py` (tool surface), `base.py` (binding + path resolution), `indexer.py` (ingest/index), `retrieval.py` (query), `sync.py` (git), `resources.py` (authoring rules).
- **`engine/` core** (framework-free, unit-testable without the MCP runtime): `config`, `chunk`, `embed`, `store`, `search`, `grep`, `related`, `links`, `validate`, `lint`.

On-disk model: a *base* dir is a git repo; each immediate subdir is a *domain* holding `*.md` pages plus `.iwiki/index.jsonl` (embedding store) and `.iwiki/log.jsonl` (ingest log). A project's `.iwiki.toml` binds `read = [...]` domains and one `write` domain (`base.resolve_binding`).

## Conventions that aren't obvious from a single file

- **Fail-soft tool handlers.** Every `wiki_*` function is wrapped by `@_safe` (`server.py`): it never raises — exceptions become `{"error", "hint"}` dicts. Implementation functions are defined plain and registered separately (`mcp.tool()(wiki_*)` at the bottom) so tests call them directly. Keep this split when adding tools.
- **HALT stop rule.** `Config.load()` raises `ConfigError` if `IWIKI_LLM_BASE_URL`/`IWIKI_LLM_KEY` are unset; `@_safe` surfaces it as a `HALT:` error. Missing base/binding raises `base.BaseError`.
- **Path-traversal guards are load-bearing.** `_validate_domain`, `_slug_parts`, and `_contains` reject `.`-prefixed, absolute, drive-letter, and `..` paths before any filesystem access. Touch these carefully and keep the checks before path joins.
- **Transactional write.** `wiki_write_page` validates structure → writes file → appends ingest log → re-indexes, and rolls back (delete file, drop last log line via `_rollback_last_ingest_log`) if any step fails. Writes refuse to overwrite an existing page (guarded op). Tests assert no orphaned file/log/index record on failure.
- **Page structure rules.** `engine/validate.py` enforces them; the **blocking** subset is `{deep_heading, pre_h2_text}` (rejected on write), the rest are advisory (report-only). Authoring rules live in `resources.py` and are exposed as the MCP resource `iwiki://authoring-rules`.
- **Chunking model.** `engine/chunk.py` splits on `##` only. The first `## Overview` section is the article summary and is **excluded** from the index; every other section's sub-chunks are prefixed with title + summary + heading + lead so each vector carries whole-article context.
- **`OVERVIEW_HEADING` / `LEAD_MAX` / the `_H2` regex are duplicated** across `chunk.py`, `validate.py`, and `lint.py` — on purpose. `lint.py` and `validate.py` must stay **config-free / stdlib-only** (no `httpx`), so they must not import `chunk`/`embed`. If you change one constant or the heading regex, update all copies (the "keep in sync" comments mark them).
- **Vector store.** `engine/store.py` keeps int8-quantized vectors in JSONL. `VectorStore` is the deliberate seam for a future SQLite/sqlite-vec swap — callers depend only on `load`/`save`/`query`. Index `file` paths are domain-relative for machine portability. Hybrid ranking puts vector/both hits first (by cosine), then lexical (by term frequency), deduped by `(domain, file, heading)`.
- **Git is best-effort.** `sync.py` auto-commits on write and `wiki_sync` does `pull --rebase` + `push`; a non-repo, missing remote, or rebase conflict degrades to a warning/error dict, never an exception.

## Docs upkeep

This repo has a `docs/wiki/`. After changes that alter functionality, architecture, or behavior, update the affected page via the iwiki skills (`iwiki:iwiki-ingest <source>`, then `/iwiki-lint`) before responding. Skip only for typo/comment/formatting changes.

## Versioning

Bump the server version in `pyproject.toml` for every repository change. Use a patch bump by default, for example `0.1.0` to `0.1.1`. Use a minor or major bump only when the task explicitly requests that release level.
