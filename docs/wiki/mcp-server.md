# MCP server and tool surface

## Overview
`server.py` defines the `FastMCP("iwiki")` server and its twelve `wiki_*` tools. Each tool is a plain function (unit-testable) wrapped by the `_safe` decorator and registered with `mcp.tool()`. The module also centralizes path-safety checks for domains and slugs, the guarded write path with rollback, and the `iwiki://authoring-rules` resource. See [[architecture#Entry point]] for how `main()` launches it.

## Tool surface
Twelve tools cover status, read, search, and write. Read/discovery: `wiki_status`, `wiki_list_domains`, `wiki_list_pages`, `wiki_read_page`, `wiki_related`. Search: `wiki_search` (modes `hybrid`/`vector`/`lexical`). Write/maintenance: `wiki_write_page`, `wiki_index`, `wiki_create_domain`, `wiki_bind`, `wiki_lint`, `wiki_sync`. Each returns a JSON-serializable dict. `wiki_index` defaults its target to the bound write domain when `domain` is omitted.

## FastMCP wiring
The implementation functions are defined first, then registered with thin wrappers: `mcp.tool()(wiki_status)`, etc. Keeping registration separate from definition lets tests import and call the raw functions without the MCP layer. `main()` runs `mcp.run()` over stdio after optionally setting `IWIKI_PROJECT_DIR` from `--project`.

## Error handling
The `_safe` decorator makes every handler fail-soft: it catches exceptions and returns an `{error, hint}` dict instead of raising. `base.BaseError` → hint to set `IWIKI_BASE_DIR` or run `wiki_bind`. `ConfigError`/`EmbedError` → an `error` prefixed `HALT:` with a hint to set `IWIKI_LLM_BASE_URL`/`IWIKI_LLM_KEY` (see [[indexing#Configuration]]). Any other exception → its message plus a generic hint.

## Path safety
User-supplied domains and slugs are sandboxed inside the base. `_validate_domain` rejects empty names, a leading `.`, `/` or `\`, `.`/`..`, absolute paths, and Windows drives. `_domain_path` resolves the domain and asserts `_contains(base, dom)`. `_slug_parts` and `_page_path` apply the same rules to page slugs (supporting nested `sub/page` slugs) and confirm the resolved `.md` file stays under the domain.

## Write path
`wiki_write_page` is guarded: it refuses to overwrite an existing page, and rejects structure with blocking findings (`deep_heading`, `pre_h2_text`) from [[authoring-and-linting#Section validation]]. On success it writes the file, appends an `ingest` log record, and re-indexes the domain. If indexing throws, it rolls back: removes the file and strips the matching last log line via `_rollback_last_ingest_log`. Finally it calls [[git-sync#Auto-commit on write]] and reports `committed`.

## Authoring-rules resource
Beyond tools, the server exposes one MCP resource, `iwiki://authoring-rules`, returning the `AUTHORING_RULES` text from `resources.py`. Agents fetch it before writing so pages follow the section format the indexer and validator expect. See [[authoring-and-linting#Authoring rules]].
