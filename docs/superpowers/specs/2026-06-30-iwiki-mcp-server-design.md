# iwiki MCP Server — Design

**Date:** 2026-06-30
**Status:** Approved for planning
**Supersedes:** the Claude Code plugin shape of `ai-wiki-plugin` (hooks/skills/commands)

## Purpose

Rework the existing `ai-wiki-plugin` (iwiki) into a standalone **MCP server** that
both **Codex** and **Claude Code** can attach to, for building and querying a wiki
knowledge base.

The current iwiki is a Claude-Code-only plugin: a clean, provider-agnostic Python
engine (`iwiki_engine`: `index | search | related | status | lint | validate`)
wrapped in Claude-specific hooks, skills, and slash commands, writing a per-project
`docs/wiki/`. This design keeps the engine core and replaces the Claude-specific
shell with an MCP tool surface, and changes the storage model from per-project
wikis to a single shared, git-synced base split into domains.

### Goals

- One install (`iwiki-mcp`) usable from both Codex and Claude Code over MCP/stdio.
- A single shared wiki base = a git-synced directory on the machine, split into
  free-form **domains** (namespaces).
- Each project binds to the base with a **read-set** (domains it searches) and a
  **write-target** (one domain it writes to).
- Search scoped either to the project's read-set or across the whole base
  ("general knowledge").
- Agent authors page content; server only stores, indexes, searches (unchanged
  authoring model — provider-agnostic).
- Embeddings via any OpenAI-compatible endpoint (existing `IWIKI_LLM_*` vars).
- Server auto-commits writes to the base; an explicit tool does pull/push.

### Non-goals

- No server-side LLM page generation (the calling agent authors pages).
- No Claude-Code hooks/automation in v1 (replaced by recommended AGENTS.md /
  CLAUDE.md snippets; the server is a passive tool provider).
- No HTTP/SSE transport in v1 (stdio only).
- No deletion of existing wiki pages via tools (STOP and ask, as today).

## Decisions (resolved during brainstorming)

1. **Topology:** one shared base = a dedicated directory on the machine that is
   itself a git repo (synced/shared via git). No per-project `docs/wiki/`.
2. **Domains:** free-form namespaces. A project declares a read-set and a single
   write-target. "General knowledge" = search across the whole base.
3. **Authoring:** the calling agent writes markdown; the server stores/indexes/
   searches. Server stays provider-agnostic, needs only an embeddings endpoint.
4. **Automation:** pure MCP tools + recommended snippets for `AGENTS.md` (Codex)
   and `CLAUDE.md` (Claude Code). No hooks.
5. **Embeddings:** OpenAI-compatible env vars (`IWIKI_LLM_BASE_URL`,
   `IWIKI_LLM_KEY`, optional `IWIKI_EMBED_MODEL`, `IWIKI_EMBED_DIMENSIONS`).
6. **Git sync:** server auto-commits on write to the base; an explicit `wiki_sync`
   tool does `git pull --rebase` + `push`.
7. **Engine reuse:** import `iwiki_engine` as an in-process library (approach A),
   not subprocess/CLI per call. The CLI stays as a dev tool.

## Repository layout

```
mcp-ai-wiki/
  pyproject.toml            # package iwiki_mcp; deps: mcp, httpx, pathspec
                            # [project.scripts] iwiki-mcp = "iwiki_mcp.server:main"
  src/iwiki_mcp/
    server.py               # FastMCP server: tool registration, stdio, main()
    base.py                 # resolve base dir + domains; project config (.iwiki.toml)
    sync.py                 # git: auto-commit, pull --rebase + push
    resources.py            # authoring rules exposed as MCP resource
    engine/                 # ported iwiki_engine core
      config.py             # embeddings config, decoupled from cwd/wiki-dir
      chunk.py  embed.py  store.py  search.py  related.py  lint.py  validate.py  links.py
  templates/
    AGENTS.md.snippet       # Codex automation instructions
    CLAUDE.md.snippet       # Claude Code automation instructions
  tests/
  docs/superpowers/specs/
  README.md                 # install, registration, env, binding, tools, quick start
```

## Storage topology

`IWIKI_BASE_DIR` points at a git repo. Each domain is a subdirectory indexed
independently; the index file is committed alongside pages for deterministic
reproduction on other machines.

```
<base>/                     # git repo (synced)
  <domain>/
    <slug>.md               # wiki pages (markdown + [[refs]])
    .iwiki/index.jsonl      # int8-quantized vector index for this domain
    .iwiki/log.jsonl        # ingest log: {op, source, page, date, src_hash}
  .iwiki-base.toml          # optional auto-maintained domain registry
```

- `wiki_dir` for a domain = `<base>/<domain>` — the engine already takes this.
- **Whole-base search** ("general knowledge") = iterate the domains in the chosen
  scope, search each domain's index, merge and re-rank top-k across domains.
- `slug` = page filename without `.md` (lowercase, `-`-joined, no spaces/slashes).
  One slug = one page = one concept. The server appends `.md` and places it in the
  domain; the agent never handles paths.

## Data flow

- **Write:** agent composes markdown → `wiki_write_page(domain, slug, markdown)` →
  server runs `validate_page` (block deep heading / pre-H2 text) → writes file →
  `index` the domain → append `log.jsonl` record → git auto-commit. Returns
  `{page, indexed_chunks, bytes, committed}`.
- **Search:** `wiki_search(query, scope)` → embed query → cosine over each in-scope
  domain index → merge → top-k sections back to the agent.

This is the same one-way ingest-then-search pipeline as today, lifted to
multi-domain and driven by MCP tools instead of skills/hooks.

## Project binding

`.iwiki.toml` at the project root (committed to the project's git, so binding
travels with the code):

```toml
read  = ["backend", "shared-arch"]   # read-set: domains to search
write = "backend"                     # write-target: single domain to write
# base = "/home/user/wiki"            # optional override of IWIKI_BASE_DIR
```

Resolution order:

1. **base** — `.iwiki.toml` `base`, else env `IWIKI_BASE_DIR`. Neither set →
   structured error (`HALT`) with a hint.
2. **read** — list of domains. Empty / file absent → default to all domains in the
   base (= general knowledge).
3. **write** — single domain. Absent → structured error on any write attempt
   (never guessed).

Project directory is resolved from the server process `cwd` (default), overridable
via `IWIKI_PROJECT_DIR` or `--project DIR`.

A missing domain is never a silent failure: `wiki_search` skips an absent domain
with a warning; `wiki_write_page` / `wiki_bind` into an absent domain returns a
hint to call `wiki_create_domain`.

## MCP tool surface

All tools return JSON-serializable data; errors are returned as
`{error, hint}` structures, never raised out of the server (fail-soft).

**Search / read**

- `wiki_search(query, scope="project", domains=None, k=None, threshold=None)` →
  `[{domain, file, heading, chunk, score}]`. Scope: `project` (read-set), `all`
  (whole base), or explicit `domains=[...]`.
- `wiki_read_page(domain, slug)` → page markdown.
- `wiki_related(domain, section_id)` → neighbour sections (vector neighbours, with
  `[[refs]]` link-graph fallback).

**Write / authoring**

- `wiki_write_page(domain, slug, markdown)` → validate → write → index domain →
  log → git auto-commit. Deleting an existing page is refused (STOP and ask).
- `wiki_index(domain=None)` → reindex a domain (default: write-target). Returns
  index stats.

**Domains / binding**

- `wiki_list_domains()` → domains in the base with index sizes.
- `wiki_create_domain(name)` → create an empty domain (+commit).
- `wiki_bind(read=None, write=None)` → write/update the project's `.iwiki.toml`.
- `wiki_status()` → resolved base, read-set, write-target, which domains exist,
  sizes.

**Health / sync**

- `wiki_lint(domain=None)` → broken links, orphans, stale pages, section gaps.
- `wiki_sync()` → `git pull --rebase` + `push` in the base. Returns what was
  pulled/pushed, or a conflict structure.

**Resource:** `iwiki://authoring-rules` exposes the page-authoring rules (`##`-only
sections, first `## Overview` summary, ≤250-char section leads, standard section
names, backticked symbols, `[[file#Heading]]` cross-links) ported from the current
`iwiki-ingest` skill. The agent fetches it before writing.

The ingest `log.jsonl` record (with `src_hash = sha256(source)[:16]`) is written by
the server inside `wiki_write_page` — the agent no longer shells out for it.

## Git sync

- **Auto-commit** inside `wiki_write_page` / `wiki_create_domain`:
  `git -C <base> add <changed> && git -C <base> commit -m "iwiki: <op> <domain>/<slug>"`.
  If the base is not a git repo → skip the commit with a warning (do not fail).
- **`wiki_sync()`**: `git -C <base> pull --rebase` then `push`. A rebase conflict is
  aborted and returned as `{error, hint}` (never leave the base half-rebased). No
  remote → local commit only, with a warning.
- The index (`.iwiki/index.jsonl`) is committed with pages so other machines
  reproduce search deterministically without re-embedding.

## Engine changes

The engine is otherwise ported unchanged. The one required change:

- `Config.load` currently couples embeddings config to cwd (defaults `docs/wiki`,
  reads `.iwikiignore` from cwd). Decouple: load embeddings config (`IWIKI_LLM_*`,
  model, dimensions, chunk/search tuning) independently of `wiki_dir`.
  `.iwikiignore` is optional and unused in the base model (kept only if a domain
  wants to scope its own pages; not required).

All other modules (`chunk`, `embed`, `store`, `search`, `related`, `lint`,
`validate`, `links`) are taken as-is; they already operate on an explicit
`wiki_dir`.

## Error handling (fail-soft)

- Missing embeddings config (`IWIKI_LLM_*`) → search/index tools return
  `{error: "HALT: ...", hint}`. Config-free tools (`wiki_status`, `wiki_lint`,
  `wiki_list_domains`) still work.
- Missing base (`IWIKI_BASE_DIR` and no `.iwiki.toml` `base`) → `{error, hint}`
  pointing at `wiki_bind` / the env var.
- Embedding API failure → bounded exponential-backoff retry (already in the
  engine), then `{error}`.
- Every tool wraps exceptions into a structured response; the server never crashes
  the session.

## Automation snippets

`templates/AGENTS.md.snippet` (Codex) and `templates/CLAUDE.md.snippet` (Claude
Code) carry recommended instructions the user pastes into their agent-instruction
file:

- On task start: `wiki_search` the topic (recall) before working.
- After changing functionality: `wiki_write_page` into the write-target.
- Periodically / at end: `wiki_sync`.
- Before writing: fetch `iwiki://authoring-rules`.

## Installation & launch

MCP model: the client (Codex / Claude Code) spawns `iwiki-mcp` over stdio at
session start. It is not a daemon; it lives for the session.

Install once, globally:

```bash
uv tool install iwiki-mcp        # or: pipx install iwiki-mcp
```

Register in Claude Code (`.mcp.json` in the project, or `claude mcp add`):

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

Register in Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.iwiki]
command = "iwiki-mcp"
env = { IWIKI_LLM_BASE_URL = "https://.../v1", IWIKI_LLM_KEY = "...", IWIKI_BASE_DIR = "/home/user/wiki" }
```

The client spawns the server with `cwd` = project root, so the server reads
`.iwiki.toml` from cwd; override with `IWIKI_PROJECT_DIR` / `--project DIR`. Keep
the secret key only in a user-level / `*.local` config, never committed.

## README

`README.md` is a deliverable in scope: install, registration for both Codex and
Claude Code, env-var reference tables (embeddings + tuning + base), project binding
(`.iwiki.toml`, read-set / write-target), tool overview, git-sync of the base, and
a quick start. Style mirrors the current iwiki README (Setup / variable tables /
Quick start), rewritten for MCP.

## Testing

TDD with pytest.

- **Engine regression:** port the 11 existing engine test files (chunk, embed,
  store, search, related, lint, validate, config, links, iwiki_common where
  applicable). Embeddings are mocked (as in `test_embed.py`).
- **New unit tests:**
  - `base.py` — base/domain resolution, `.iwiki.toml` parsing, defaults, absent
    domain handling.
  - `sync.py` — commit and sync against a temp git repo, including the conflict
    path.
  - `server.py` — each tool happy-path + error-path; multi-domain search merge;
    scope `project` vs `all`.
- **MCP smoke test:** drive the server over stdio with an MCP client — list tools,
  run one `wiki_search` / `wiki_status` end-to-end.

## Open questions

None blocking. Domain registry file (`.iwiki-base.toml`) is optional and may be
deferred if `wiki_list_domains` can enumerate subdirectories directly.
