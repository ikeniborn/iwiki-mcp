# Wiki base and project binding

## Overview
`base.py` resolves where the wiki lives and which domains a project may read and write. It loads `.iwiki.toml`, merges it with environment variables into a frozen `Binding`, computes domain and `.iwiki/` paths, lists domains, resolves search scope, and rewrites the project config while preserving unknown keys. It is the foundation [[mcp-server#Tool surface]] calls before every operation.

## Binding model
`Binding` is a frozen dataclass with four fields: `base` (absolute base dir), `read` (tuple of readable domains), `write` (the default write domain or `None`), and `project_dir` (where `.iwiki.toml` was read). It is the single resolved context object every tool consumes, so binding logic lives in one place.

## Resolving the binding
`resolve_binding` reads `.iwiki.toml` from the resolved project dir, then derives `base` from the config's `base` key or `IWIKI_BASE_DIR`, raising `BaseError` if neither is set. `resolve_project_dir` picks the explicit arg, else `IWIKI_PROJECT_DIR`, else `cwd`. `load_project_config` is fail-soft: a missing or malformed TOML returns `{}`. `read` is coerced via `_as_str_tuple`, accepting a string or list.

## Domains and paths
A domain is a subdirectory of the base. `domain_dir` joins base + domain; `index_path` and `log_path` point at `<domain>/.iwiki/index.jsonl` and `.../log.jsonl`. `list_domains` returns sorted subdirectories whose names do not start with `.`, so `.iwiki` and hidden dirs never count as domains. `domain_exists` checks the same dot rule plus `isdir`.

## Search scope
`resolve_scope(binding, scope, domains)` decides which domains a search touches. Explicit `domains` win. Otherwise `scope == "all"` returns every domain in the base; any other scope returns the bound `read` list, falling back to all domains when `read` is empty. This is how [[retrieval#Hybrid search]] knows where to look.

## Writing .iwiki.toml
`write_project_config` updates `read`/`write` (and optionally `base`) without clobbering the rest of the file. `_write_preserving_unknown_config` keeps unknown top-level keys and all `[table]` sections, rewriting only the core keys. Helpers handle multi-line strings and arrays (`_preserved_top_level_lines`, `_core_assignment_closed`) so hand-authored config survives a `wiki_bind`.
