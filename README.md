# iwiki-mcp

## What it is

iwiki-mcp is a shared, git-synced wiki base split into domains and queried over MCP from Codex and Claude Code. The agent authors Markdown pages; the stdio MCP server stores them in the base, builds indexes, searches across bound domains, and returns the matching wiki context.

## Install

Install once, globally:

```bash
uv tool install iwiki-mcp
# or
pipx install iwiki-mcp
```

iwiki-mcp requires an OpenAI-compatible embeddings endpoint. Set `IWIKI_LLM_BASE_URL` and `IWIKI_LLM_KEY` in the MCP client environment.

The MCP client spawns `iwiki-mcp` over stdio at session start. It is not a daemon; it lives for the client session.

## Register in Claude Code

Add this to `.mcp.json` in the project, or register the same command and env with `claude mcp add`:

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

Keep `IWIKI_LLM_KEY` in a user-level or local config, not in a committed project file.

## Register in Codex

Add this to `~/.codex/config.toml`:

```toml
[mcp_servers.iwiki]
command = "iwiki-mcp"
env = { IWIKI_LLM_BASE_URL = "https://.../v1", IWIKI_LLM_KEY = "...", IWIKI_BASE_DIR = "/home/user/wiki" }
```

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

`read` controls the default project search scope. `write` is the default target for tools that need one, such as `wiki_index` without a `domain` argument. `base` is optional and overrides `IWIKI_BASE_DIR` for this project.

You can also bind from the MCP tool surface:

```text
wiki_bind(read=["backend", "frontend"], write="backend")
```

`wiki_bind` validates that every provided read and write domain already exists. Create missing domains with `wiki_create_domain` first.

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
| `wiki_search` | Search bound domains with `hybrid`, `vector`, or `lexical` mode; accepts explicit domains, `k`, and threshold overrides. |
| `wiki_read_page` | Read one Markdown page by domain and slug. |
| `wiki_list_pages` | List page slugs and files in a domain. |
| `wiki_related` | Return related sections for a section id within one domain. |
| `wiki_write_page` | Validate and write a new page, index the domain, and auto-commit when the base is a git repo. |
| `wiki_index` | Rebuild one domain index, defaulting to the bound write domain when omitted. |
| `wiki_list_domains` | List visible domain directories in the base with index sizes. |
| `wiki_create_domain` | Create a domain directory with `.iwiki/` metadata and auto-commit when possible. |
| `wiki_bind` | Write or update `.iwiki.toml` for the current project after validating domains. |
| `wiki_status` | Show resolved base, project directory, read domains, write domain, and available domains. |
| `wiki_lint` | Report domain health, including broken links, orphans, stale pages, and section gaps. |
| `wiki_sync` | Run `git pull --rebase` and `git push` in the base. |

`wiki_write_page` refuses to overwrite an existing page in v1. For existing pages, read the current page first with `wiki_read_page`, confirm the intended replacement with the user, and then handle the edit deliberately outside the v1 overwrite path.

The server also exposes the MCP resource `iwiki://authoring-rules` for page-structure rules.

## Git sync of the base

When `IWIKI_BASE_DIR` is a git repository, `wiki_write_page` and `wiki_create_domain` stage and commit the base after successful changes. If the base is not a git repo, the tools return a warning and keep the filesystem changes local.

Use `wiki_sync` to share the base:

```text
wiki_sync()
```

`wiki_sync` runs `git pull --rebase` and then `git push` in the base. If a rebase conflict involves generated index files, treat the index as disposable: resolve by regenerating the affected domain with `wiki_index`, then run `wiki_sync` again.

## Quick start

1. Install `iwiki-mcp` and register it in Claude Code or Codex with `IWIKI_LLM_BASE_URL`, `IWIKI_LLM_KEY`, and `IWIKI_BASE_DIR`.
2. In the agent session, create a domain:

```text
wiki_create_domain(name="backend")
```

3. Bind the project:

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
