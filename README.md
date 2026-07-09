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

The MCP client spawns `iwiki-mcp` over stdio at session start. It is not a daemon; it lives for the client session.

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

Each domain is a subdirectory under the base:

```text
/home/user/wiki/
  backend/
    auth.md
    .iwiki/
      index.jsonl
      log.jsonl
  frontend/
    routing.md
    .iwiki/
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
| `IWIKI_SCORE_THRESHOLD` | `0.2` | Default minimum vector similarity for search results. |
| `IWIKI_GRAPH_DEPTH` | `2` | Link-hop depth used by related-section expansion. |

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
| `wiki_search` | Search with `hybrid` (default), `vector`, or `lexical` mode. `scope` selects domains: `project` (default, the bound `read` set) or `all` (every domain in the base); an explicit `domains` list overrides `scope`. Accepts `k` and threshold overrides. |
| `wiki_read_page` | Read one Markdown page by domain and slug. |
| `wiki_list_pages` | List page slugs and files in a domain. |
| `wiki_related` | Return related sections for a section id within one domain. |
| `wiki_write_page` | Validate and write a new page, index the domain, commit and push. |
| `wiki_update_page` | Replace the body of one `##` section of an existing page, reindex the changed section, commit and push. |
| `wiki_delete_page` | Delete one page by domain and slug: remove the file, append a `delete` log op, reindex the domain, commit and push. Rolls back on failure. |
| `wiki_index` | Rebuild one domain index (defaulting to the bound write domain when omitted), commit and push. |
| `wiki_list_domains` | List visible domain directories in the base with index sizes. |
| `wiki_create_domain` | Create a domain directory with `.iwiki/` metadata and return whether the base auto-commit succeeded. |
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
| `type` | Required. Closed vocabulary: `architecture`, `api`, `guide`, `reference`, `runbook`, `concept` (default). |
| `title` | Derived from the page's `# Title` H1. |
| `description` | Derived from the `## Overview` section, truncated to `IWIKI_SUMMARY_MAX_CHARS`. |
| `resource` | The `source` passed to the write tool, if any; `wiki_apply_okf` and `wiki_migrate_okf` fall back to the page's last logged ingest source when none is given. |
| `tags` | Lowercase kebab-case labels, at most 5 per page. |
| `timestamp` | On create (`wiki_write_page`, `wiki_apply_okf`, `wiki_migrate_okf`): the page file's last git-commit date, or today's date if not yet committed. On edit (`wiki_update_page`): always today's date. |

`type` and `tags` are resolved with this precedence: an **explicit** `type`/`tags` argument on the write tool wins; otherwise, when `IWIKI_CHAT_MODEL` is set, the server classifies the page body with that chat model; otherwise it defaults to `type="concept"` with no tags.

Faceted search narrows `wiki_search` to a `type` and/or a set of `tags`; the query values are normalized the same way as stored frontmatter (case-insensitive `type`, kebab-case `tags`), so `type="API"` still matches a page whose frontmatter says `type: api`:

```text
wiki_search(query="deploy steps", type="runbook", tags=["ci"])
```

Tools for adopting OKF frontmatter on an existing domain:

| Tool | What it does |
|---|---|
| `wiki_migrate_okf(domain=None)` | Backfill frontmatter for every page missing it. Dual-mode: **autonomous** (writes frontmatter directly) when `IWIKI_CHAT_MODEL` is set; otherwise returns a **plan** — a list of candidates with derived title/description/timestamp and the domain's existing tag vocabulary — for the calling agent to classify and apply. In autonomous mode, each page's `resource` falls back to its last logged ingest source, and tags coined for one page are reused as vocabulary for later pages in the same run. |
| `wiki_apply_okf(domain, slug, type, tags)` | Apply agent-classified `type`/`tags` (plus derived fields) as frontmatter to one page, reindex, commit and push. Omitting `tags` preserves the page's existing tags instead of clearing them. |
| `wiki_export_okf(domain, dest)` | Export a domain into a fully OKF-conformant bundle at `dest`: every page carries frontmatter (existing pages keep theirs, tags deduped; pages with none get a deterministic `type: concept` / title / description / git-date timestamp), `[[wikilink]]` syntax is rewritten to standard Markdown links, and reserved `index.md` / `log.md` files are generated. Returns a `warnings` list flagging any source page whose name collides with those reserved files. Sources are never mutated, only copied. |

`IWIKI_CHAT_MODEL` (default: empty) is optional; leaving it unset disables server-side classification and `wiki_migrate_okf` falls back to plan mode.

## Git sync of the base

When `IWIKI_BASE_DIR` is a git repository, every mutating tool — `wiki_write_page`, `wiki_update_page`, `wiki_create_domain`, and `wiki_index` — stages, commits, and pushes the base after successful changes (fail-soft: push errors are reported but do not roll back the write). Before writing, each mutating tool first fetches and fast-forwards the base when it is cleanly behind its remote, so the change lands on the current tip and the push is a fast-forward. If the base has genuinely diverged (local unpushed commits *and* the remote moved ahead), the tool refuses with `base diverged from remote` and a hint to run `wiki_sync` (or resolve the conflict in the base repo) before retrying — it does not stack another commit onto the divergence. If the base is not a git repo, the write or create still succeeds on disk and the tool response returns `committed: false`. Use `wiki_sync`, `wiki_status`, or git commands in the base repo to diagnose repository and remote setup.

Use `wiki_sync` to share the base:

```text
wiki_sync()
```

`wiki_sync` runs `git pull --rebase` and then `git push` in the base. If `pull --rebase` conflicts, `wiki_sync` aborts the rebase and returns an error with a hint. Resolve the conflict manually in the base repo. If generated index files are involved, regenerate the affected domain indexes with `wiki_index`, commit the regenerated files in the base repo if needed, then run `wiki_sync` again.

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
  markdown="# Auth\n\n## Overview\nToken authentication flow.\n\n## Purpose\nAuth verifies users and protects private routes.\n"
)
```

5. Search it:

```text
wiki_search(query="how does auth work?")
```

## Limitations (v1)

- Wiki links are intra-domain: use `[[slug#Heading]]` within the same domain.
- Vector search uses numpy brute force, not an external vector database.
- Staleness checks are project-local and depend on available source paths and ingest logs.
