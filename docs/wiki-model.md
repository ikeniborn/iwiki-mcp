# Wiki base, domains, and project binding

*Part of the [iwiki-mcp documentation](../README.md#documentation). Русская версия: [wiki-model.ru.md](wiki-model.ru.md).*

How the shared wiki base is laid out on disk, how a project binds to its domains, and how the base is synced over Git.

## The base and domains

`IWIKI_BASE_DIR` points at the shared wiki base. The base is intended to be a git repository, so writes can be committed and synced between machines or projects.

Each domain is a subdirectory under the base. A page's identity is its domain-relative `<type>/<slug>` path: `wiki_write_page` places the file under a directory named for its (resolved) frontmatter `type`, and that same `<type>/<slug>` value — without the `.md` suffix — is what `wiki_list_pages` returns and what `wiki_read_page` / `wiki_update_page` / `wiki_delete_page` expect as `slug`. Each domain's portable vector store (`index.jsonl`) and ingest log (`log.jsonl`) live at the domain root; a legacy `.iwiki/index.jsonl` / `.iwiki/log.jsonl` domain is migrated to the root automatically the first time any tool touches it. The base-local `.iwiki/graph.sqlite3` is a separate rebuildable SQLite cache, excluded from Git alongside its WAL/SHM files. The base also keeps `.iwiki/lock` for the cross-process git lock.

```text
/home/user/wiki/
  .iwiki/
    graph.sqlite3        # local derived cache, not committed
    lock
  backend/
    architecture/
      auth.md
    guide/
      onboarding.md
    index.jsonl
    log.jsonl
  frontend/
    concept/
      routing.md
    index.jsonl
    log.jsonl
```

### Choosing `concept` or `reference`

The type records the question a page answers, not its formatting. `concept` answers "why / how does this work": an idea, model, mechanism, methodology, or rationale meant to be read top to bottom. `reference` answers "what exactly is X": facts looked up by name — keys, flags, schemas, field lists, limits, inventories, contracts, changelogs, ledgers — even when they are written as prose. A page that does both should be split; otherwise choose the question most readers arrive with. Table density does not decide it: on the hosted wiki the two classes differ by only 0.02 versus 0.07 table lines per line. A write may return `System One suggests type ...` as a second opinion; keep the authored type when this rule supports it.

Use one base across projects. Bind each project to the domains it should read from and the domain it should write to.

## Graph cache and links

The graph cache stores directed page links and heading anchors in SQLite, but search walks it as a bounded undirected neighbourhood inside the visible read scope. It never mixes code search with wiki dependencies. After clone, pull, corruption, or a fingerprint mismatch, the server rebuilds the affected local cache from Markdown without embedding calls; while unavailable it safely falls back to Markdown traversal. A graph-refresh failure never discards the committed Markdown, `index.jsonl`, or `log.jsonl` mutation: affected domains are marked `dirty` and a fingerprint-checked Markdown fallback remains authoritative until local repair succeeds.

Use relative Markdown links within one domain: `[Auth](architecture/auth.md#flow)`. For a page in another visible domain use the canonical URI: `[Routing](iwiki://frontend/concept/routing#flow)`. Root `index.md` and `log.md` are generated OKF artifacts, never graph pages or traversal targets; `wiki_lint` reports an authored link to either as `reserved_target`.

## Bind a project

The server resolves project binding from `.iwiki.toml` in the project root. The client normally starts the server with `cwd` set to the project root; override that with `IWIKI_PROJECT_DIR` or `iwiki-mcp --project DIR`.

When `.iwiki.toml` is missing or contains only whitespace, the server creates a
commented template with Git, PostgreSQL, and `code_graph` examples. It does the
same for `.iwikiignore`, whose template covers secrets and common project noise
and is optionally extended with the current `.gitignore`. Once either file has
non-whitespace content, server operations leave its bytes unchanged. Edit both
files manually after initialization.

```toml
# .iwiki.toml
read = ["backend", "frontend"]
write = ["backend", "frontend"]
primary = "backend"
# base = "/home/user/wiki"
```

`read` controls the default project search scope. To read from **every** domain in the base, set `read = []` or omit the line entirely — an empty or absent `read` falls back to all domains. `read = ["all"]` is **not** a wildcard; it is treated as a literal domain named `all`. `write` is the list of domains mutating tools may change. `primary` selects the default target for tools such as `wiki_index` without a `domain` argument and must belong to `write`. Every write domain must also belong to `read`. `base` is optional and overrides `IWIKI_BASE_DIR` for this project.

For Git storage, `wiki_bind` does not write project configuration. An attempted
automatic binding change returns a controlled response and leaves the file
unchanged:

```json
{"error":"project configuration cannot be changed automatically","code":"project_config_manual_edit_required","hint":"edit .iwiki.toml manually; populated configuration is never rewritten automatically"}
```

PostgreSQL `wiki_bind` remains session-only: local stdio may narrow the configured
maximum scope and never widen it, while a hosted session may select any subset of the
token's current grants — narrowing once is not a ceiling, so a session can return to a
domain it still holds, including one it just created. In a hosted HTTP session it can also carry the project's
`[specifications].mode` as `specification_mode`; local PostgreSQL stdio rejects that
parameter. It never changes `.iwiki.toml` or persists the mode. `wiki_create_domain` may bootstrap an empty
missing Git domain outside the current write list; it creates no page, index, or
log. Add that domain to `.iwiki.toml` manually before writing to it.

## Git sync of the base

When `IWIKI_BASE_DIR` is a git repository, every mutating tool — `wiki_write_page`, `wiki_update_page`, `wiki_create_domain`, and `wiki_index` — stages, commits, and pushes the base after successful changes (fail-soft: push errors are reported but do not roll back the write). Before writing, each mutating tool first fetches and fast-forwards the base when it is cleanly behind its remote, so the change lands on the current tip and the push is a fast-forward. If the base has genuinely diverged (local unpushed commits *and* the remote moved ahead), the tool refuses with `base diverged from remote` and a hint to run `wiki_sync` (or resolve the conflict in the base repo) before retrying — it does not stack another commit onto the divergence. If the base is not a git repo, the write or create still succeeds on disk and the tool response returns `committed: false`. Use `wiki_sync`, `wiki_status`, or git commands in the base repo to diagnose repository and remote setup.

Use `wiki_sync` to share the base:

```text
wiki_sync()
```

`wiki_sync` runs `git pull --rebase` and then `git push` in the base. Recoverable remote failures (`non_fast_forward`, `credential_unavailable`, and `transport_unavailable`) retry the standard Git pull/push path up to three sync attempts, with a 250 ms delay between attempts. Responses include `sync_attempts` and `push_attempts`; classified pull/push failures also include `failure_class`. That field can be absent for outcomes before a remote attempt, including a non-repository base, missing remote, or lock timeout. Failed pushes remain fail-soft warnings and preserve the local commit. The server does not change client Git configuration, source shell profiles, search for authentication sockets, or broker credentials.

Git runs non-interactively (`GIT_TERMINAL_PROMPT=0`, closed stdin), so credentials must already be available to the MCP server process through standard Git mechanisms. A credential helper configured in an interactive shell does not by itself prove that the MCP process can use it. If credentials are unavailable, configure a non-interactive helper for the server account and transport, launch the MCP server from an environment that already has the required credential context, or perform `wiki_sync` from a trusted terminal with that context. Do not put tokens, passwords, remote URLs with embedded credentials, or authentication socket paths in MCP configuration or logs.

If `pull --rebase` conflicts, `wiki_sync` aborts the rebase and returns `conflict: true`, `failure_class: rebase_conflict`, attempt metadata, and a hint. Conflicts are never retried automatically: resolve them manually in the base repo. If generated index files are involved, regenerate the affected domain indexes with `wiki_index`, commit the regenerated files in the base repo if needed, then run `wiki_sync` again.
