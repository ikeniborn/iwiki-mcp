# iwiki-mcp

*Русская версия: [docs/README.ru.md](docs/README.ru.md).*

## What it is

iwiki-mcp is a shared wiki service split into domains and queried over MCP from Codex
and Claude Code. It supports a Git-synced local base or tenant-isolated PostgreSQL,
over stdio or hosted Streamable HTTP, as described in
[Storage and transport modes](docs/storage-modes.md).

The supported container deployment runs hosted iwiki MCP, nginx, and the
[Telegram bot service](docs/telegram-bot.md) together. Allowlisted employees can
select domains, ask text or voice questions, and confirm page changes; typing `/` lists
the bot commands and `/menu` opens an inline action menu. A domain selection is sticky
for the life of the bot process, a question sent before any domain is chosen is answered
as soon as one is, and each request reports its progress with a reaction, a typing
action, and a status message edited per stage. With a tool-calling inference provider
the bot answers through an agentic search/read loop over the wiki, falling back
automatically to single-pass retrieval when the provider does not support tools. See
the [deployment runbook](docs/deployment.md) for the operator path and migration steps.

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

## Requirements

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

The client launches the server with `cwd` at the project root, so `.iwiki.toml` (see [Bind a project](docs/wiki-model.md#bind-a-project)) is picked up automatically.

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

## Quick start

1. Install `iwiki-mcp` and register it in Claude Code or Codex with `IWIKI_LLM_BASE_URL`, `IWIKI_LLM_KEY`, and `IWIKI_BASE_DIR`.
2. In the agent session, create a domain:

```text
wiki_create_domain(name="backend")
```

3. Edit the initialized `.iwiki.toml` manually (see [Bind a project](docs/wiki-model.md#bind-a-project)), then append the agent snippet (see [Teach the agent to use iwiki](#teach-the-agent-to-use-iwiki)):

```toml
read = ["backend"]
write = ["backend"]
primary = "backend"
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

## Teach the agent to use iwiki

Registering the server exposes the tools, but the agent still needs instructions on *when* to call them. The repo ships ready-made snippets in [`templates/`](templates):

- `templates/CLAUDE.md.snippet` — append to the project's `CLAUDE.md` (Claude Code).
- `templates/AGENTS.md.snippet` — append to the project's `AGENTS.md` (Codex).

Both carry the same guidance: search before a task, do not mutate binding during ordinary startup, author pages after functionality changes, and `wiki_sync` at end of session. Append the matching snippet once per project:

```bash
cat templates/CLAUDE.md.snippet >> CLAUDE.md   # Claude Code
cat templates/AGENTS.md.snippet >> AGENTS.md   # Codex
```

The snippets reference `.iwiki.toml`, so [bind the project](docs/wiki-model.md#bind-a-project) first.

## Documentation

Everything beyond installation and registration lives in `docs/`. Each page has a Russian
sibling named `<page>.ru.md`.

| Page | What it covers |
|---|---|
| [Storage and transport modes](docs/storage-modes.md) | Git stdio, PostgreSQL stdio, hosted Streamable HTTP, the supported container, session lifetime, and the PostgreSQL MCP tool contract. |
| [PostgreSQL provisioning](docs/postgres-setup.md) | Least-privilege roles, admin CLI, backup/restore, migration v4/v5, and rollback. |
| [Wiki base, domains, and binding](docs/wiki-model.md) | Base layout, the graph cache and links, `.iwiki.toml` binding, and Git sync of the base. |
| [Python code graph](docs/code-graph.md) | The optional local code graph: configuration, build jobs, read tools, and per-language coverage. |
| [Code graph publication](docs/code-graph-publishing.md) | Distributed publication modes, the publisher CLI, scheduling, and SQLite snapshot profiles. |
| [Given-When-Then specifications](docs/specifications.md) | Specification modes, scenario grammar, the semantic tool surface, and lint findings. |
| [Tools](docs/tools-reference.md) | Every `wiki_*` tool and its contract. |
| [Env reference](docs/env-reference.md) | Every `IWIKI_*` environment variable and its default. |
| [OKF compatibility](docs/okf-compatibility.md) | Frontmatter fields, reserved files, and the OKF adoption tools. |
| [Benchmarks](docs/benchmarks.md) | Code graph, search pipeline, and Pareto evaluation runs. |
| [Architecture](docs/architecture.md) | Internal module map and data flow. |
| [Deployment runbook](docs/deployment.md) | Operator path: container deployment, migration, cutover, rollback. |
| [Telegram bot](docs/telegram-bot.md) | The bundled Telegram bot service. |

## Limitations (v1)

- Within one domain use `[Heading](<type>/<slug>.md#heading)`; across domains use `iwiki://<domain>/<page-id>#<anchor>`.
- `.iwiki/graph.sqlite3` is a local derived cache, not a portable vector/log replacement and not a code-dependency graph.
- Git storage uses numpy brute-force vector search over portable JSONL indexes;
  PostgreSQL storage uses tenant/domain-scoped pgvector cosine candidates before the
  shared lexical fusion, deduplication, and optional reranking stages.
- Staleness checks are project-local and depend on available source paths and ingest logs.
