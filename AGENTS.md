# Repository Guidelines

## Project Structure & Module Organization

This is a Python MCP server packaged from `src/iwiki_mcp`. Core modules include `server.py` for the stdio MCP entry point, `base.py` for project/base binding, `indexer.py` and `retrieval.py` for wiki indexing and search, plus `resources.py` and `sync.py` for MCP resources and git sync. Tests live in `tests/`; engine-style unit tests are grouped in `tests/engine/`. User-facing setup docs are in `README.md`, localized docs in `docs/README.ru.md`, design notes in `docs/wiki/`, and reusable agent snippets in `templates/`.

## Build, Test, and Development Commands

- `uv sync --extra dev` installs runtime and development dependencies.
- `uv run pytest -q` runs the full pytest suite.
- `uv run iwiki-mcp --help` verifies the local console script.
- `uv tool install .` installs the MCP server globally from this checkout for manual client testing.

The package requires Python `>=3.10`. Runtime configuration for embeddings is supplied through environment variables such as `IWIKI_LLM_BASE_URL`, `IWIKI_LLM_KEY`, and `IWIKI_BASE_DIR`.

## Coding Style & Naming Conventions

Use standard Python style: 4-space indentation, `snake_case` functions and modules, `PascalCase` classes, and descriptive names for MCP tools and config fields. Keep modules small and focused on existing responsibilities. Prefer typed, explicit data flow over hidden globals. No formatter is configured in this repository; keep diffs surgical and match surrounding style.

## Testing Guidelines

Tests use `pytest` with `pytest-asyncio`; configuration is in `pyproject.toml`. Add tests beside the behavior they cover: general server/package behavior in `tests/test_*.py`, lower-level indexing/config/link behavior in `tests/engine/test_*.py`. Name tests after observable behavior, for example `test_search_respects_project_scope`. Run `uv run pytest -q` before handing off changes.

## Commit & Pull Request Guidelines

Recent history uses short, direct commit messages such as `update name project` and `up`; prefer more descriptive imperative messages when possible, for example `fix search scope binding`. Pull requests should include a concise summary, test command output, linked issues if any, and notes about configuration or migration impacts. Include screenshots only when documentation or UI-rendered output changes.

## Versioning

Bump the server version in `pyproject.toml` for every repository change, including code, tests, docs, templates, and agent instruction changes. Use a patch bump by default for each change, for example `0.1.0` to `0.1.1`. Use a minor or major bump only when the task explicitly requests that release level.

## Security & Configuration Tips

Never commit real embedding keys or private base paths. Keep `IWIKI_LLM_KEY` in local shell, MCP client, or untracked config. When editing `.iwiki.toml` examples, use placeholder paths and domains.
