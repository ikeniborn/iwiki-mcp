# iwiki-mcp

*Русская версия: [docs/README.ru.md](docs/README.ru.md).*

## What it is

iwiki-mcp is a shared, git-synced wiki base split into domains and queried over MCP from Codex and Claude Code. The agent authors Markdown pages; the stdio MCP server stores them in the base, builds indexes, searches across bound domains, and returns the matching wiki context.

## Install

Requires Python `>=3.10`. The recommended tool is [`uv`](https://docs.astral.sh/uv/); `pipx` works as a drop-in alternative.

### As a global tool (recommended for use)

iwiki-mcp is **not published to PyPI yet**, so install from a local checkout. Clone the repo and run this from the repo root:

```bash
git clone https://github.com/ikeniborn/iwiki-mcp.git
cd iwiki-mcp
uv tool install .
# or
pipx install .
```

This puts an `iwiki-mcp` executable on your `PATH` (e.g. `~/.local/bin/iwiki-mcp`), which is what the MCP client spawns. Verify with `iwiki-mcp --help`.

Once the package is published, a global install will be a one-liner — `uv tool install iwiki-mcp` (or `pipx install iwiki-mcp`). Until then those commands fail with `No matching distribution found for iwiki-mcp`; use the local-checkout install above.

### From source (development)

Clone, sync dependencies (including the `dev` extra), and run the tests:

```bash
git clone https://github.com/ikeniborn/iwiki-mcp.git
cd iwiki-mcp
uv sync --extra dev
uv run pytest -q
```

`uv run iwiki-mcp` then runs the server from the checkout without a global install.

### Requirements

iwiki-mcp requires an OpenAI-compatible embeddings endpoint. Set `IWIKI_LLM_BASE_URL` and `IWIKI_LLM_KEY` in the MCP client environment (see [Register in Claude Code](#register-in-claude-code) / [Register in Codex](#register-in-codex)).

The MCP client spawns `iwiki-mcp` over stdio at session start. It is not a daemon; it lives for the client session. Before opening MCP stdio, normal startup sends one minimal request to the configured embeddings endpoint, with a 10-second timeout and no retries. Missing or invalid configuration, an unavailable endpoint, or an invalid response blocks startup and prints an actionable diagnostic to stderr; any literal configured API key in diagnostic values is redacted. `iwiki-mcp --help` remains offline and does not run the probe.

## Register in Claude Code

Step by step:

1. **Confirm the executable resolves.** `iwiki-mcp --help` should print usage. If not, the global install did not land on `PATH` — reinstall (`uv tool install .`) or use `uv run iwiki-mcp` as the command.
2. **Register the server.** Either run the CLI from the project root:

   ```bash
   claude mcp add iwiki \
     --env IWIKI_LLM_BASE_URL=https://.../v1 \
     --env IWIKI_LLM_KEY=... \
     --env IWIKI_BASE_DIR=/home/user/wiki \
     -- iwiki-mcp
   ```

   or add the same block to `.mcp.json` in the project root by hand:

   ```json
   {
     "mcpServers": {
       "iwiki": {
         "command": "iwiki-mcp",
         "env": {
           "IWIKI_LLM_BASE_URL": "https://.../v1",
           "IWIKI_LLM_KEY": "...",
           "IWIKI_BASE_DIR": "/home/user/wiki"
         }
       }
     }
   }
   ```

3. **Verify.** Run `claude mcp list` — `iwiki` should show as connected. Inside a session, `/mcp` lists the `wiki_*` tools.
4. **Keep secrets out of git.** Put `IWIKI_LLM_KEY` (and usually `IWIKI_LLM_BASE_URL`) in a user-level or `.local` config, not in a committed `.mcp.json`.

The client launches the server with `cwd` at the project root, so `.iwiki.toml` (see [Bind a project](#bind-a-project)) is picked up automatically.

## Register in Codex

Step by step:

1. **Confirm the executable resolves:** `iwiki-mcp --help`.
2. **Add the server** to `~/.codex/config.toml`:

   ```toml
   [mcp_servers.iwiki]
   command = "iwiki-mcp"
   env = { IWIKI_LLM_BASE_URL = "https://.../v1", IWIKI_LLM_KEY = "...", IWIKI_BASE_DIR = "/home/user/wiki" }
   ```

   To run from a source checkout instead of a global install, use `command = "uv"` with `args = ["run", "iwiki-mcp", "--project", "/abs/path/to/project"]`.
3. **Restart Codex** so it re-reads `config.toml`, then start a session in the project. The `wiki_*` tools become available.

Codex does not set the server `cwd` to your project, so pass `iwiki-mcp --project /abs/path/to/project` (or set `IWIKI_PROJECT_DIR` in `env`) when the project root differs from where Codex launches — that is how `.iwiki.toml` is resolved.

## The base and domains

`IWIKI_BASE_DIR` points at the shared wiki base. The base is intended to be a git repository, so writes can be committed and synced between machines or projects.

Each domain is a subdirectory under the base. A page's identity is its domain-relative `<type>/<slug>` path: `wiki_write_page` places the file under a directory named for its (resolved) frontmatter `type`, and that same `<type>/<slug>` value — without the `.md` suffix — is what `wiki_list_pages` returns and what `wiki_read_page` / `wiki_update_page` / `wiki_delete_page` expect as `slug`. Each domain's vector store (`index.jsonl`) and ingest log (`log.jsonl`) live at the domain root; a legacy `.iwiki/index.jsonl` / `.iwiki/log.jsonl` domain is migrated to the root automatically the first time any tool touches it. (The base itself keeps a separate `.iwiki/lock` at its own root for the cross-process git lock — unrelated to per-domain storage.)

```text
/home/user/wiki/
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

Use one base across projects. Bind each project to the domains it should read from and the domain it should write to.

## Bind a project

The server resolves project binding from `.iwiki.toml` in the project root. The client normally starts the server with `cwd` set to the project root; override that with `IWIKI_PROJECT_DIR` or `iwiki-mcp --project DIR`.

```toml
# .iwiki.toml
read = ["backend", "frontend"]
write = "backend"
# base = "/home/user/wiki"
```

`read` controls the default project search scope. To read from **every** domain in the base, set `read = []` or omit the line entirely — an empty or absent `read` falls back to all domains. `read = ["all"]` is **not** a wildcard; it is treated as a literal domain named `all`. `write` is the default target for tools that need one, such as `wiki_index` without a `domain` argument. `base` is optional and overrides `IWIKI_BASE_DIR` for this project.

You can also bind from the MCP tool surface:

```text
wiki_bind(read=["backend", "frontend"], write="backend")
```

`wiki_bind` validates that every provided read and write domain already exists. For an existing non-empty `read`, the tool preserves configured domains and may only append the current project domain. `write` must match the current project domain, derived from the project directory name. Create missing domains with `wiki_create_domain` as an explicit manual setup step before binding.

## Teach the agent to use iwiki

Registering the server exposes the tools, but the agent still needs instructions on *when* to call them. The repo ships ready-made snippets in [`templates/`](templates):

- `templates/CLAUDE.md.snippet` — append to the project's `CLAUDE.md` (Claude Code).
- `templates/AGENTS.md.snippet` — append to the project's `AGENTS.md` (Codex).

Both carry the same guidance: search before a task, do not mutate binding during ordinary startup, author pages after functionality changes, and `wiki_sync` at end of session. Append the matching snippet once per project:

```bash
cat templates/CLAUDE.md.snippet >> CLAUDE.md   # Claude Code
cat templates/AGENTS.md.snippet >> AGENTS.md   # Codex
```

The snippets reference `.iwiki.toml`, so bind the project (above) first.

## Env reference

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

**Search tuning**

| Variable | Default | Meaning |
|---|---|---|
| `IWIKI_TOP_K` | `8` | Default maximum results for search and related-section lookup. |
| `IWIKI_SCORE_THRESHOLD` | `0.2` | Default minimum vector similarity for a returned section hit. |
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

## Tools

| Tool | What it does |
|---|---|
| `wiki_search` | **Two-level hierarchical search.** A per-page `summary` vector (from the frontmatter `description`) seeds candidate articles, the wiki-link graph expands the candidate pool, and clean `section` vectors (heading + body, no article prefix) are ranked inside it; each hit carries its `source` (`seed`/`graph`). Modes: `hybrid` (default), `vector`, `lexical`. `scope` selects domains: `project` (default, the bound `read` set) or `all`; an explicit `domains` list overrides `scope`. Accepts `k` and threshold overrides. **Existing domains need a one-time re-index (`wiki_index` per domain, or `wiki_export_okf`) to build the two-level index.** A page with no `description` produces no summary vector, so it is not reachable by `vector`-mode seeding (it remains findable via `lexical`/`hybrid` grep); a lexical seed fallback is a planned follow-up. Pass `intent="write"` for the precise write-target mode: it seeds with the higher `IWIKI_WRITE_SEED_THRESHOLD`, keeps only an exact (case-insensitive) heading match when `heading` is given, and returns a single `target` for a write tool to decide create-vs-update — `{domain, file, heading, score, exists: true}` on a hit, or just `{domain, exists: false}` on a miss; `scope`/`mode`/`k`/`threshold`/`type`/`tags` are ignored in this mode, and the target domain is the bound `write` domain (or the first entry of an explicit `domains` list when there is no write binding). A page needs a frontmatter `description` (it seeds the summary vector) to be locatable this way at all. |
| `wiki_read_page` | Read one Markdown page by domain and slug. |
| `wiki_list_pages` | List page slugs and files in a domain. |
| `wiki_related` | Return related sections for a section id within one domain. |
| `wiki_write_page` | Validate and write a new page, index the domain, commit and push. |
| `wiki_update_page` | Replace the body of one `##` section of an existing page, reindex the changed section, commit and push. |
| `wiki_delete_page` | Delete one page by domain and slug: remove the file, append a `delete` log op, reindex the domain, commit and push. Rolls back on failure. |
| `wiki_index` | Rebuild one domain index (defaulting to the bound write domain when omitted), commit and push. |
| `wiki_list_domains` | List visible domain directories in the base with index sizes. |
| `wiki_create_domain` | Create an empty domain directory and return whether the base auto-commit succeeded; the domain's `index.jsonl` / `log.jsonl` are created lazily at the domain root on first write or index. |
| `wiki_bind` | Write or update `.iwiki.toml` for the current project after validating domains. |
| `wiki_status` | Show resolved base, project directory, read domains, write domain, and available domains. |
| `wiki_lint` | Report domain health: broken links, orphans, stale pages, `missing_source` (pages whose ingest source no longer exists on disk — deletion candidates), and section gaps. |
| `wiki_sync` | Run `git pull --rebase` and `git push` in the base. |

`wiki_write_page` refuses to overwrite an existing page in v1. To update a single section of an existing page, use `wiki_update_page(domain, slug, heading, new_body, source=None)` — it replaces only the named `##` section and leaves the rest of the page intact. For a full-page rewrite, read the current page first with `wiki_read_page`, confirm the intended replacement with the user, and then handle the edit deliberately outside the v1 overwrite path.

`wiki_lint` reports `missing_source` pages whose ingest source has disappeared. Remove such a stale page explicitly with `wiki_delete_page` after confirming with the user; `wiki_sync` then propagates the deletion to the remote like any other commit.

The server also exposes the MCP resource `iwiki://authoring-rules` for page-structure rules.

## OKF compatibility

Every page carries a small YAML frontmatter block above the `# Title` H1, written automatically by `wiki_write_page` / `wiki_update_page` / `wiki_apply_okf`. Fields:

| Field | Meaning |
|---|---|
| `type` | Required. **Open** vocabulary: prefer `architecture`, `api`, `guide`, `reference`, `runbook`, `concept` (default), but any value is accepted (e.g. `person`); off-list values get only an advisory `unknown_type`. Also the page's directory: `wiki_write_page` places the file at `<type>/<slug>.md` under the domain root — a bare `slug` is prefixed with the resolved `type`, and a `slug` that already carries a leading segment must match it. |
| `title` | Derived from the page's `# Title` H1. |
| `description` | The authored article summary — the single source of the summary, embedded as each section's context prefix. Stored in full (never truncated). Falls back to a `## Overview` section only transitionally (migration). |
| `resource` | The `source` passed to the write tool, if any; `wiki_apply_okf` and `wiki_migrate_okf` fall back to the page's last logged ingest source when none is given. The stored path is project-relative — an absolute path under the project is relativized, and any path (absolute or relative, e.g. `../../etc/hosts`) that resolves outside the project is rejected. |
| `tags` | Lowercase kebab-case labels, at most 5 per page. |
| `status` | Optional iwiki extension: `stub` (default), `developing`, `stable`, `deprecated`. |
| `timestamp` | On create (`wiki_write_page`, `wiki_apply_okf`, `wiki_migrate_okf`): the page file's last git-commit date, or today's date if not yet committed. On edit (`wiki_update_page`): always today's date. |

The reserved OKF files `index.md` (navigation) and `log.md` (history) are export-only: `wiki_write_page` / `wiki_update_page` / `wiki_delete_page` no longer regenerate them on every change. Run `wiki_export_okf` to (re)generate current `index.md` / `log.md` in the domain root before treating the domain as a complete OKF bundle for an external consumer. `index`/`log` stay reserved only at the domain **root**, and `wiki_write_page` rejects those two full identities specifically — a type-dir slug like `concept/index` is a distinct, ordinary page and is allowed.

Pages no longer carry a `## Overview` section: the summary lives in `description`.
Relationship links go in two reserved `##` sections — `## Outgoing links` (Markdown
links) and `## External links` (bare URLs) — which are excluded from the search index
but still feed the link graph. Run `wiki_export_okf` once to migrate legacy pages
(it strips `## Overview`, backfills `description`, and defaults `status`).

`type` and `tags` are resolved with this precedence: an **explicit** `type`/`tags` argument on the write tool wins; otherwise, when `IWIKI_CHAT_MODEL` is set, the server classifies the page body with that chat model; otherwise it defaults to `type="concept"` with no tags.

Faceted search narrows `wiki_search` to a `type` and/or a set of `tags`; the query values are normalized the same way as stored frontmatter (case-insensitive `type`, kebab-case `tags`), so `type="API"` still matches a page whose frontmatter says `type: api`:

```text
wiki_search(query="deploy steps", type="runbook", tags=["ci"])
```

Tools for adopting OKF frontmatter on an existing domain:

| Tool | What it does |
|---|---|
| `wiki_migrate_okf(domain=None)` | Backfill frontmatter for every page missing it. Dual-mode: **autonomous** (writes frontmatter directly) when `IWIKI_CHAT_MODEL` is set; otherwise returns a **plan** — a list of candidates with derived title/description/timestamp and the domain's existing tag vocabulary — for the calling agent to classify and apply. In autonomous mode, each page's `resource` falls back to its last logged ingest source, and tags coined for one page are reused as vocabulary for later pages in the same run. In both modes it also deterministically moves any flat page (a bare `<slug>.md` at the domain root) that already carries a frontmatter `type` under `<type>/<slug>.md`, rewriting intra-domain links; a page whose move target already exists is skipped and reported under `layout_collisions` instead of being clobbered, and a page whose frontmatter `type` doesn't resolve to a safe single path segment (e.g. contains `/` or `..`) is left in place and reported under `layout_skipped_unsafe`. |
| `wiki_apply_okf(domain, slug, type, tags)` | Apply agent-classified `type`/`tags` (plus derived fields) as frontmatter to one page, reindex, commit and push. Omitting `tags` preserves the page's existing tags instead of clearing them; the existing `description` and `status` are always carried over unchanged. |
| `wiki_export_okf(domain=None)` | Whole-domain, in-place OKF conformance sweep (no copy, no `dest`): converts any residual `[[wikilink]]` to Markdown links and guarantees frontmatter on every page (deterministic `type: concept` where missing; existing `type`/`tags` preserved), then regenerates the reserved `index.md` / `log.md`. Deterministic — never calls the chat model. Returns `fixed_links`, `added_frontmatter`, and `still_missing_frontmatter` / `still_legacy_wikilink`, with a `next_steps` hint to `wiki_migrate_okf` for better `type`/`tags`. The domain directory is itself the OKF bundle. It also migrates each page to the v2 body model: strips a `## Overview` section, backfilling `description` from it when empty, and defaults `status` to `stub`. |

`IWIKI_CHAT_MODEL` (default: empty) is optional; leaving it unset disables server-side classification and `wiki_migrate_okf` falls back to plan mode.

## Git sync of the base

When `IWIKI_BASE_DIR` is a git repository, every mutating tool — `wiki_write_page`, `wiki_update_page`, `wiki_create_domain`, and `wiki_index` — stages, commits, and pushes the base after successful changes (fail-soft: push errors are reported but do not roll back the write). Before writing, each mutating tool first fetches and fast-forwards the base when it is cleanly behind its remote, so the change lands on the current tip and the push is a fast-forward. If the base has genuinely diverged (local unpushed commits *and* the remote moved ahead), the tool refuses with `base diverged from remote` and a hint to run `wiki_sync` (or resolve the conflict in the base repo) before retrying — it does not stack another commit onto the divergence. If the base is not a git repo, the write or create still succeeds on disk and the tool response returns `committed: false`. Use `wiki_sync`, `wiki_status`, or git commands in the base repo to diagnose repository and remote setup.

Use `wiki_sync` to share the base:

```text
wiki_sync()
```

`wiki_sync` runs `git pull --rebase` and then `git push` in the base. Recoverable remote failures (`non_fast_forward`, `credential_unavailable`, and `transport_unavailable`) retry the standard Git pull/push path up to three sync attempts, with a 250 ms delay between attempts. Responses include `sync_attempts` and `push_attempts`; classified pull/push failures also include `failure_class`. That field can be absent for outcomes before a remote attempt, including a non-repository base, missing remote, or lock timeout. Failed pushes remain fail-soft warnings and preserve the local commit. The server does not change client Git configuration, source shell profiles, search for authentication sockets, or broker credentials.

Git runs non-interactively (`GIT_TERMINAL_PROMPT=0`, closed stdin), so credentials must already be available to the MCP server process through standard Git mechanisms. A credential helper configured in an interactive shell does not by itself prove that the MCP process can use it. If credentials are unavailable, configure a non-interactive helper for the server account and transport, launch the MCP server from an environment that already has the required credential context, or perform `wiki_sync` from a trusted terminal with that context. Do not put tokens, passwords, remote URLs with embedded credentials, or authentication socket paths in MCP configuration or logs.

If `pull --rebase` conflicts, `wiki_sync` aborts the rebase and returns `conflict: true`, `failure_class: rebase_conflict`, attempt metadata, and a hint. Conflicts are never retried automatically: resolve them manually in the base repo. If generated index files are involved, regenerate the affected domain indexes with `wiki_index`, commit the regenerated files in the base repo if needed, then run `wiki_sync` again.

## Quick start

1. Install `iwiki-mcp` and register it in Claude Code or Codex with `IWIKI_LLM_BASE_URL`, `IWIKI_LLM_KEY`, and `IWIKI_BASE_DIR`.
2. In the agent session, create a domain:

```text
wiki_create_domain(name="backend")
```

3. Bind the project, and append the agent snippet (see [Teach the agent to use iwiki](#teach-the-agent-to-use-iwiki)):

```text
wiki_bind(read=["backend"], write="backend")
```

4. Write the first page:

```text
wiki_write_page(
  domain="backend",
  slug="auth",
  markdown="# Auth\n\n## Purpose\nAuth verifies users and protects private routes.\n",
  description="Token authentication flow.",
  type="architecture"
)
```

This writes `backend/architecture/auth.md`; pass that same `architecture/auth` identity as `slug` to `wiki_read_page` / `wiki_update_page` / `wiki_delete_page`.

5. Search it:

```text
wiki_search(query="how does auth work?")
```

## Limitations (v1)

- Wiki links are intra-domain: use `[Heading](<type>/<slug>.md#heading)` — the page's domain-relative `<type>/<slug>` identity — within the same domain.
- Vector search uses numpy brute force, not an external vector database.
- Staleness checks are project-local and depend on available source paths and ingest logs.
