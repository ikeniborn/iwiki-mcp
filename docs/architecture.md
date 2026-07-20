# iwiki-mcp — Architecture

A structural map of the iwiki-mcp server: layers, module dependencies, the on-disk
model, the tool surface, and the core pipelines (write, retrieval, indexing, git
sync, OKF frontmatter). Diagrams are Mermaid, tuned for a dark Obsidian theme.

> Companion to the user-facing `README.md` (install / registration / env reference).
> This document is developer-facing: it explains *how* the pieces fit, not *how to
> set them up*.

## What it is

`iwiki-mcp` is a **stdio MCP server** — not a daemon. The MCP client (Claude Code,
Codex) spawns one process per session and talks to it over stdin/stdout. The server
fronts a shared, git-synced wiki **base** split into **domains**. Coding agents author
Markdown pages; the server validates structure, persists to disk, embeds and indexes
the content, and answers hybrid (vector + lexical + link-graph) search across the
domains a project is bound to.

Three nouns anchor everything:

- **Base** — a directory (intended to be a git repo) pointed at by `IWIKI_BASE_DIR`.
- **Domain** — an immediate subdirectory of the base; holds `*.md` pages plus a
  per-domain `index.jsonl` (vector store) and `log.jsonl` (ingest log).
- **Binding** — a project's `.iwiki.toml` declaring which domains it may `read`
  from and the single domain it may `write` to.

## Layered architecture

Two layers live under `src/iwiki_mcp/`. The **top layer** is MCP-aware and reaches
side effects (filesystem, git, HTTP embeddings). The **`engine/` core** is
framework-free and unit-testable without the MCP runtime — several of its modules
(`validate`, `lint`, `links`, `frontmatter`, `okf_artifacts`) are deliberately
kept `httpx`-free and stdlib-only so they import in any project.

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'background': '#1e1e2e', 'primaryColor': '#313244', 'primaryTextColor': '#cdd6f4', 'primaryBorderColor': '#89b4fa', 'lineColor': '#888888'}}}%%
flowchart TB
    Client["MCP client<br/>(Claude Code / Codex)"]
    Client -->|"stdio JSON-RPC"| Top

    Top["Top layer — MCP-aware<br/>server · base · indexer · retrieval<br/>okf · sync · ignore · lock · resources"]
    Engine["engine/ core — framework-free<br/>chunk · embed · store · fusion · hier · grep<br/>rerank · search · related · classify · section<br/>frontmatter · links · validate · lint · config"]

    Top --> Engine
    Top -->|"read/write pages, index, log"| fs["Filesystem<br/>base / domains"]
    Top -->|"commit · push · pull"| git["git<br/>base repo + remote"]
    Engine -.->|"store.save index.jsonl"| fs
    Engine -->|"embeddings / chat / rerank"| llm["OpenAI-compatible<br/>endpoint"]

    classDef topcls fill:#89b4fa,color:#1e1e2e,stroke:#74c7ec,stroke-width:1px
    classDef engcls fill:#a6e3a1,color:#1e1e2e,stroke:#40a02b
    classDef extcls fill:#f9e2af,color:#1e1e2e,stroke:#df8e1d
    class Top topcls
    class Engine engcls
    class fs,git,llm extcls
```

**Top-layer modules:** `server` (tool surface + guards), `base` (binding + path
resolve), `indexer` (ingest + index), `retrieval` (multi-signal query), `okf`
(frontmatter assembly), `sync` (git ops), `ignore` (`.iwikiignore` gate), `lock`
(cross-process lock), `resources` (authoring rules).

**Engine modules:** `chunk`, `embed`, `store`, `fusion`, `hier`, `grep`, `rerank`,
`search`, `related`, `classify`, `section`, `frontmatter`, `links`, `validate`,
`lint`, `config`.

### Layer contract

| Concern | Top layer | Engine core |
| --- | --- | --- |
| Knows about MCP / `FastMCP` | yes (`server.py`) | no |
| Reaches git | `sync.py` (write mutations) + `okf.py` (`git log` for timestamps) | no |
| Reaches the network | `okf.py`→`classify`, indexer/retrieval→`embed`, `server`→`rerank` | only `embed`/`classify`/`rerank` |
| Path-traversal guards | `server._validate_domain` / `_slug_parts` / `_page_path` / `_contains`, `okf._is_safe_type_segment`, `retrieval._domain_file_parts` (all top-layer) | — |
| Config-free / stdlib-only | — | `validate`, `lint`, `links`, `frontmatter`, `okf_artifacts`, `section`, `grep` |

## Module dependencies

Import direction is top → engine; the engine never imports the top layer. (A
`from .base import ...` inside `okf`/`indexer`/`retrieval` is not an exception —
those three are top-layer modules, not `engine/`.) The graph is split into three
views. Note the deliberate constant duplication: `OVERVIEW_HEADING`, `LEAD_MAX`,
and the `_H2` regex are copied across `chunk.py`, `validate.py`, `lint.py`,
`section.py`, and `okf.py` so the config-free modules never import `chunk`/`embed`
(keeping `httpx`, pulled in via `embed`, out of them).

### Top-layer composition

`server` drives the orchestration modules; `indexer`, `retrieval`, and `okf` share
`base` for path/binding resolution.

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'background': '#1e1e2e', 'primaryColor': '#313244', 'primaryTextColor': '#cdd6f4', 'primaryBorderColor': '#89b4fa', 'lineColor': '#888888'}}}%%
flowchart TB
    server["server"] --> base["base"] & indexer["indexer"] & retrieval["retrieval"] & okf["okf"] & sync["sync"] & ignore["ignore"]
    indexer --> base
    retrieval --> base
    okf --> base

    classDef hot fill:#f38ba8,color:#1e1e2e,stroke:#d20f39
    class server hot
```

### Ingest & query → engine primitives

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'background': '#1e1e2e', 'primaryColor': '#313244', 'primaryTextColor': '#cdd6f4', 'primaryBorderColor': '#89b4fa', 'lineColor': '#888888'}}}%%
flowchart TB
    indexer["indexer"] --> chunk["chunk"] & embed["embed"] & store["store"]
    retrieval["retrieval"] --> fusion["fusion"] & hier["hier"] & grep["grep"]
    retrieval --> chunk & embed & store

    classDef core fill:#94e2d5,color:#1e1e2e,stroke:#179299
    class store core
```

### Engine-internal core

The config-free cluster: `lint`/`validate` fold in `frontmatter`, `links`, and
`okf_artifacts`; `hier`/`related` build on `store` + `links`.

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'background': '#1e1e2e', 'primaryColor': '#313244', 'primaryTextColor': '#cdd6f4', 'primaryBorderColor': '#89b4fa', 'lineColor': '#888888'}}}%%
flowchart TB
    lint["lint"] --> validate["validate"] & links["links"] & fm["frontmatter"] & okf_art["okf_artifacts"]
    validate --> fm
    chunk["chunk"] --> fm
    hier["hier"] --> store["store"] & links & okf_art
    related["related"] --> store & links

    classDef core fill:#94e2d5,color:#1e1e2e,stroke:#179299
    class fm,store,links core
```

`frontmatter`, `store`, and `links` (highlighted) are the most-depended-on engine
primitives; `store.VectorStore` is the deliberate seam for a future
SQLite/sqlite-vec swap (callers depend only on `load`/`save`/`query`).

## On-disk model

The base is a git repo. Each non-`.`-prefixed subdirectory is a domain. Pages live
at `<type>/<slug>.md` (the frontmatter `type` doubles as the directory). Per-domain
`index.jsonl` and `log.jsonl` sit at the domain root; a legacy `.iwiki/` subdir is
migrated to the root on first touch (`base.migrate_store_location`). The base keeps
a single `.iwiki/lock` at its own root for the cross-process git lock — it is never
a domain.

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'background': '#1e1e2e', 'primaryColor': '#313244', 'primaryTextColor': '#cdd6f4', 'primaryBorderColor': '#89b4fa', 'lineColor': '#888888'}}}%%
flowchart TB
    subgraph baserepo["IWIKI_BASE_DIR (git repo)"]
        meta[".iwiki/lock<br/>(cross-process git lock)"]
        subgraph d1["domain: backend/"]
            b_arch["architecture/auth.md"]
            b_guide["guide/onboarding.md"]
            b_gen["index.jsonl · log.jsonl<br/>index.md/log.md (export-only)"]
        end
        subgraph d2["domain: frontend/"]
            f_all["concept/routing.md<br/>index.jsonl · log.jsonl"]
        end
    end

    subgraph proj["project root"]
        toml[".iwiki.toml<br/>read=[backend, frontend]<br/>write=backend"]
        iwignore[".iwikiignore<br/>(source gate)"]
    end

    toml -.->|"base ="| baserepo
    toml -.->|"read scope"| d1
    toml -.->|"read scope"| d2
    toml -.->|"write target"| d1

    classDef dom fill:#89b4fa,color:#1e1e2e,stroke:#74c7ec
    classDef gen fill:#585b70,color:#cdd6f4,stroke:#6c7086
    classDef cfg fill:#f9e2af,color:#1e1e2e,stroke:#df8e1d
    class b_gen,f_all,meta gen
    class toml,iwignore cfg
```

**Binding resolution** (`base.resolve_binding`): `base` comes from `.iwiki.toml`
`base` or `IWIKI_BASE_DIR`; `read`/`write` from `.iwiki.toml`. An empty/absent `read`
defaults the search scope to *all* domains. `write` must equal the current project
domain (the project directory's basename). `wiki_bind` protects an existing non-empty
`read` — it may only *append* the current project domain, never swap the scope.

## MCP tool surface

Every `wiki_*` handler is defined as a plain function, then registered separately
(`mcp.tool()(wiki_*)` at the bottom of `server.py`) so tests call the
implementations directly. Each is wrapped by `@_safe`: it **never raises** —
exceptions become `{"error", "hint"}` dicts.

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'background': '#1e1e2e', 'primaryColor': '#313244', 'primaryTextColor': '#cdd6f4', 'primaryBorderColor': '#89b4fa', 'lineColor': '#888888'}}}%%
mindmap
  root((wiki_* tools))
    Read:::read
      wiki_search
      wiki_read_page
      wiki_list_pages
      wiki_list_domains
      wiki_related
      wiki_status
    Write:::write
      wiki_write_page
      wiki_update_page
      wiki_delete_page
      wiki_index
      wiki_create_domain
    OKF:::okf
      wiki_migrate_okf
      wiki_apply_okf
      wiki_export_okf
    Health:::health
      wiki_lint
      wiki_remediation_plan
    Config:::cfg
      wiki_bind
      wiki_sync

  classDef read   fill:#89b4fa,color:#1e1e2e,stroke:#74c7ec
  classDef write  fill:#f38ba8,color:#1e1e2e,stroke:#d20f39
  classDef okf    fill:#a6e3a1,color:#1e1e2e,stroke:#40a02b
  classDef health fill:#f9e2af,color:#1e1e2e,stroke:#df8e1d
  classDef cfg    fill:#94e2d5,color:#1e1e2e,stroke:#179299
```

### Cross-cutting error model

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'background': '#1e1e2e', 'primaryColor': '#313244', 'primaryTextColor': '#cdd6f4', 'primaryBorderColor': '#89b4fa', 'lineColor': '#888888'}}}%%
flowchart TD
    call["wiki_* handler"] --> safe{"@_safe catch"}
    safe -- "BaseError" --> e1["{error, hint:<br/>set IWIKI_BASE_DIR<br/>or run wiki_bind}"]
    safe -- "ConfigError / EmbedError" --> e2["{error: HALT: ...,<br/>hint: set LLM env}"]
    safe -- "any Exception" --> e3["{error, hint:<br/>unexpected error}"]
    safe -- "ok" --> ok["result dict"]

    classDef halt fill:#f38ba8,color:#1e1e2e,stroke:#d20f39
    classDef good fill:#a6e3a1,color:#1e1e2e,stroke:#40a02b
    class e1,e2,e3 halt
    class ok good
```

`Config.load()` raises `ConfigError` when `IWIKI_LLM_BASE_URL`/`IWIKI_LLM_KEY` are
unset — surfaced as a `HALT:` error (the stop rule). Missing base/binding raises
`base.BaseError`.

## Startup / process lifecycle

`main()` runs *before* opening MCP stdio: it loads config and sends one probe
request to the embeddings endpoint (`probe_embedding_endpoint`, 10 s timeout, no
retries). A failure prints a redacted diagnostic to stderr and exits `1`.
`iwiki-mcp --help` stays offline (no probe).

```mermaid
%%{init: {'theme': 'dark'}}%%
sequenceDiagram
    participant C as MCP client
    participant M as main()
    participant E as embed endpoint
    participant R as mcp.run()

    C->>M: spawn iwiki-mcp (stdio)
    M->>M: Config.load()
    alt config missing
        M-->>C: stderr diagnostic + exit 1
    else config ok
        M->>E: probe_embedding_endpoint (10s)
        alt probe fails
            M-->>C: redacted diagnostic + exit 1
        else probe ok
            M->>R: mcp.run()
            R-->>C: stdio ready, wiki_* tools live
        end
    end
```

## Write pipeline

`wiki_write_page` is transactional: validate → write file → append ingest log →
re-index, with rollback (delete file, drop the last log line) if any later step
fails. Writes **refuse to overwrite** an existing page (a guarded op). Every mutating
handler first runs `sync.ensure_fresh(base)` — a `diverged` base makes it return
`base diverged from remote` with **zero** side effects. The flow splits into a
guard phase and a transaction phase; both funnel failures into one `@_safe` error
dict.

### Guard phase

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'background': '#1e1e2e', 'primaryColor': '#313244', 'primaryTextColor': '#cdd6f4', 'primaryBorderColor': '#89b4fa', 'lineColor': '#888888'}}}%%
flowchart TD
    A["wiki_write_page(...)"] --> B["_validate_domain"]
    B --> C{"ensure_fresh:<br/>diverged?"}
    C -->|"yes"| ERR["return error dict<br/>(@_safe)"]
    C -->|"no"| D{"domain exists?"}
    D -->|"no"| ERR
    D -->|"yes"| E["to_markdown_links"]
    E --> F{"validate_page:<br/>blocking finding?"}
    F -->|"yes"| ERR
    F -->|"no"| G{"source in<br/>.iwikiignore?"}
    G -->|"yes"| ERR
    G -->|"no"| H["_normalize_source →<br/>build phase"]

    classDef stop fill:#f38ba8,color:#1e1e2e,stroke:#d20f39
    class ERR stop
```

### Transaction phase

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'background': '#1e1e2e', 'primaryColor': '#313244', 'primaryTextColor': '#cdd6f4', 'primaryBorderColor': '#89b4fa', 'lineColor': '#888888'}}}%%
flowchart TD
    H["build_frontmatter →<br/>_resolve_identity (&lt;type&gt;/&lt;slug&gt;)"] --> K{"reserved slug<br/>or page exists?"}
    K -->|"yes"| ERR["return error dict"]
    K -->|"no"| T1["write file"]
    T1 --> T2["append ingest log"]
    T2 --> T3["index_domain (embed + store)"]
    T3 -->|"exception"| RB["rollback: remove file +<br/>drop last log line → raise"]
    T3 -->|"ok"| L["commit_and_push (pathspec=domain)"]
    L --> M["return {page, indexed_chunks,<br/>committed, pushed, warning}"]

    classDef stop fill:#f38ba8,color:#1e1e2e,stroke:#d20f39
    classDef good fill:#a6e3a1,color:#1e1e2e,stroke:#40a02b
    classDef tx fill:#f9e2af,color:#1e1e2e,stroke:#df8e1d
    class ERR,RB stop
    class M good
    class T1,T2,T3 tx
```

`wiki_update_page` follows the same skeleton but edits **one** `##` section
in-place (`section.replace_section`, which rejects an ambiguous/missing heading),
does a whole-file ingest-log upsert (`upsert_ingest_log` keeps one record per page),
and rolls back by restoring the original bytes. `wiki_delete_page` removes the file,
appends a `delete` log op, reindexes, and rolls back by rewriting the file.

## Indexing pipeline

`indexer.index_domain` re-chunks every page, then **reuses** existing vectors whose
`(hash, dim, schema-version)` still match — only changed/new chunks are embedded. New
vectors are int8-quantized before landing in `index.jsonl`.

### index_domain flow

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'background': '#1e1e2e', 'primaryColor': '#313244', 'primaryTextColor': '#cdd6f4', 'primaryBorderColor': '#89b4fa', 'lineColor': '#888888'}}}%%
flowchart TD
    A["index_domain(cfg, base, domain)"] --> B["load existing index.jsonl"]
    B --> C["rglob *.md (skip index.md/log.md)"]
    C --> D["chunk_markdown per page (see below)"]
    D --> E{"per chunk: hash + dim<br/>+ schema match prev?"}
    E -->|"yes"| F["reuse vector<br/>(refresh type/tags/ordinal)"]
    E -->|"no"| G["embed_texts (batched, retried)"]
    G --> H["quantize int8 → make_record"]
    F --> I["sort + store.save"]
    H --> I
    I --> J["return {indexed_chunks, reused,<br/>embedded, bytes, over_cap}"]

    classDef net fill:#f9e2af,color:#1e1e2e,stroke:#df8e1d
    classDef good fill:#a6e3a1,color:#1e1e2e,stroke:#40a02b
    class G net
    class J good
```

### chunk_markdown

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'background': '#1e1e2e', 'primaryColor': '#313244', 'primaryTextColor': '#cdd6f4', 'primaryBorderColor': '#89b4fa', 'lineColor': '#888888'}}}%%
flowchart TD
    P["chunk_markdown(page)"] --> S1["split frontmatter"]
    S1 --> S2["description → 1 summary chunk<br/>(ordinal -1, excluded from ##)"]
    S2 --> S3["split body on ## only"]
    S3 --> S4["drop Overview + reserved<br/>link sections"]
    S4 --> S5["word-split long sections<br/>(chunk_size / overlap)"]
    S5 --> S6["emit section chunks:<br/>text = '## h' + body, hash = sha256[:16]"]
```

**Chunking model** (`chunk.py`): the frontmatter `description` becomes a single
`kind="summary"` vector (the article seed); every other `##` section becomes one or
more `kind="section"` vectors carrying only that section's own text. `## Overview`
and the reserved link sections (`## Outgoing links` / `## External links`) are never
indexed. Records are int8-quantized (`store.quantize`, per-vector scale) so
`index.jsonl` stays compact; `CAP_BYTES = 8 MiB` flags an `over_cap` domain.

## Retrieval pipeline

`wiki_search` (read intent) runs a **broad multi-signal gather** per domain, fuses
the ranked signals with deterministic Reciprocal Rank Fusion (RRF), then optionally
reranks the hydrated pool through a LiteLLM endpoint. Five independent signals feed
the fusion; each is a ranked list, and RRF rewards a candidate that surfaces in more
than one. The flow is decomposed into four views below.

### Query routing

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'background': '#1e1e2e', 'primaryColor': '#313244', 'primaryTextColor': '#cdd6f4', 'primaryBorderColor': '#89b4fa', 'lineColor': '#888888'}}}%%
flowchart TD
    Q["wiki_search(query, scope, mode, k, ...)"] --> R{"intent == write?"}
    R -->|"yes"| W["locate_target<br/>(precise upsert,<br/>write_seed_threshold)"]
    R -->|"no"| MODE["resolve mode<br/>(hybrid / semantic / lexical)"]
    MODE --> EMB["embed query<br/>(semantic / hybrid only)"]
    EMB --> GATHER["per-domain _domain_signals<br/>→ fusion (next)"]
```

### Signals & fusion

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'background': '#1e1e2e', 'primaryColor': '#313244', 'primaryTextColor': '#cdd6f4', 'primaryBorderColor': '#89b4fa', 'lineColor': '#888888'}}}%%
flowchart TD
    PER["_domain_signals (per domain)"] --> S1["semantic_page<br/>summary-vector seeds"]
    PER --> S2["semantic_chunk<br/>section-vector global"]
    PER --> S3["lexical_page<br/>term-freq page seeds"]
    PER --> S4["lexical_section<br/>term-freq sections"]
    PER --> S5["graph_page<br/>link-graph BFS from seeds"]
    S1 --> FUSE
    S2 --> FUSE
    S3 --> FUSE
    S4 --> FUSE
    S5 --> FUSE["RRF fuse (k=60) + dedup →<br/>label hit (semantic/lexical/both)"]

    classDef sem fill:#89b4fa,color:#1e1e2e,stroke:#74c7ec
    classDef lex fill:#a6e3a1,color:#1e1e2e,stroke:#40a02b
    classDef gph fill:#94e2d5,color:#1e1e2e,stroke:#179299
    class S1,S2 sem
    class S3,S4 lex
    class S5 gph
```

### Rerank & top-k

The fused pool holds up to `max(top_k, 32)` candidates; rerank scores the **full**
pool, then the result is sliced to `top_k`.

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'background': '#1e1e2e', 'primaryColor': '#313244', 'primaryTextColor': '#cdd6f4', 'primaryBorderColor': '#89b4fa', 'lineColor': '#888888'}}}%%
flowchart TD
    C["fused candidates<br/>(pool ≤ max(top_k, 32))"] --> RR{"IWIKI_RERANK_MODEL set?"}
    RR -->|"no"| K1["slice to top_k"]
    K1 --> O1["return {results}"]
    RR -->|"yes"| HY["hydrate full pool<br/>(re-read, verify hash, attach text)"]
    HY --> RK["rerank_candidates<br/>(LiteLLM /rerank, 60s, fail-soft)"]
    RK --> K2["merge scored + unscored,<br/>slice to top_k"]
    K2 --> O2["return {results, rerank}"]

    classDef good fill:#f9e2af,color:#1e1e2e,stroke:#df8e1d
    class O1,O2 good
```

### Two-level semantic retrieval

The semantic side is hierarchical (`hier.py`), ported from obsidian-ai-wiki for
parity: summary vectors **seed** articles above `seed_threshold`, an undirected wiki
link-graph BFS (`graph_depth`, `bfs_top_k`) **expands** those seeds into a candidate
pool, and section vectors are ranked *inside* that pool. This lets a broad query
match a page by its whole-article summary even when no single section vector scores
well. The read path (`_domain_signals`) scores summaries/sections inline and expands
the graph with `hier.rank_graph_pages`; the write-target locate (`intent="write"`,
`retrieval.locate_target`) calls the `hier.py` helpers directly —
`seed_articles` → `expand_graph` → `rank_sections`.

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'background': '#1e1e2e', 'primaryColor': '#313244', 'primaryTextColor': '#cdd6f4', 'primaryBorderColor': '#89b4fa', 'lineColor': '#888888'}}}%%
flowchart TB
    QV["query vector"] --> SEED["summary seeds<br/>(sim ≥ seed_threshold,<br/>top seed_top_k)"]
    SEED --> EXP["graph expand<br/>(BFS depth, cap bfs_top_k)"]
    EXP --> POOL["candidate page pool"]
    POOL --> RANK["rank sections inside pool"]
    RANK --> RES["ranked section hits"]

    classDef step fill:#89b4fa,color:#1e1e2e,stroke:#74c7ec
    class SEED,EXP,RANK step
```

Data-integrity guards in the read path are load-bearing: `retrieval` re-opens each
page under `O_NOFOLLOW`, stamps it (`st_dev`, `st_ino`, `st_size`, `st_mtime_ns`),
and only trusts a lexical/hydrated hit when the live chunk hash still matches the
indexed record — a stale index never leaks wrong text into results. A shared
`page_cache` avoids re-reading a page across signals within one query.

## Git sync & freshness

`sync.py` is best-effort: a non-repo, missing remote, or rebase conflict degrades to
a `warning`/`error` dict, never an exception. Two entry points matter:
`ensure_fresh` (pre-write freshness) and `sync` / `commit_and_push` (publish). All
git mutations serialize through a cross-process `FileLock` at `base/.iwiki/lock`
(`lock.py`) so many client sessions can share one base. Remote URLs and SSH targets
are scrubbed from any surfaced git output (`_sanitize_git_output`).

### `ensure_fresh` state machine

```mermaid
%%{init: {'theme': 'dark'}}%%
stateDiagram-v2
    [*] --> check
    check --> no_repo : not a git repo
    check --> no_remote : no remote
    check --> offline : fetch failed
    check --> no_upstream : no upstream branch
    check --> compare : fetch ok
    compare --> up_to_date : behind 0, ahead 0
    compare --> ahead : behind 0, ahead nonzero
    compare --> diverged : behind and ahead both nonzero
    compare --> dirty : behind, tree dirty
    compare --> updated : behind, clean, ff-only

    diverged --> refuse : write REFUSED
    no_repo --> proceed : write proceeds
    no_remote --> proceed
    offline --> proceed
    no_upstream --> proceed
    up_to_date --> proceed
    ahead --> proceed
    dirty --> proceed
    updated --> proceed
    refuse --> [*]
    proceed --> [*]
```

Only `diverged` (local unpushed commits **and** remote moved ahead) blocks the
write; every other state proceeds, threading any `warning` onto the result.

### Publish path (`commit_and_push` → `sync`)

```mermaid
%%{init: {'theme': 'dark'}}%%
sequenceDiagram
    participant H as write handler
    participant AC as auto_commit
    participant SY as sync
    participant L as base_lock
    participant G as git

    H->>AC: commit_and_push(msg, pathspec=domain)
    AC->>L: acquire FileLock (15s)
    AC->>G: git add -- domain
    AC->>G: git status --porcelain
    alt nothing to commit
        AC-->>H: committed=false, warning
    else changes staged
        AC->>G: git commit -m msg
        AC->>SY: sync(base)
        SY->>L: acquire FileLock
        loop up to 3 attempts
            SY->>G: git pull --rebase
            alt rebase conflict
                SY->>G: git rebase --abort
                SY-->>H: conflict=true, failure_class=rebase_conflict
            else pulled
                SY->>G: git push
                alt push ok
                    SY-->>H: pushed=true
                else recoverable (non_fast_forward / creds / transport)
                    Note over SY: sleep 250ms, retry
                end
            end
        end
    end
```

## OKF frontmatter pipeline

Every page carries a YAML frontmatter block above the `# Title` H1
(`frontmatter.py`, a stdlib-only YAML subset — no pyyaml). The write tools fill it.
`type`/`tags` follow a strict precedence, and `type` doubles as the page's directory
segment.

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'background': '#1e1e2e', 'primaryColor': '#313244', 'primaryTextColor': '#cdd6f4', 'primaryBorderColor': '#89b4fa', 'lineColor': '#888888'}}}%%
flowchart TD
    A["okf.build_frontmatter(...)"] --> B{"explicit type<br/>argument?"}
    B -->|"yes"| C["use explicit type + normalize tags"]
    B -->|"no"| D{"IWIKI_CHAT_MODEL set?"}
    D -->|"yes"| E["classify.classify_page<br/>(chat endpoint, fail-soft)"]
    D -->|"no"| F["default type='concept'<br/>+ warning"]
    C --> G["assemble meta"]
    E --> G
    F --> G
    G --> H["title ← derive_title (H1 / slug)<br/>description ← explicit / Overview<br/>resource ← source<br/>status ← explicit / stub<br/>timestamp ← git last-commit / today"]
    H --> I["fm.render → frontmatter block"]

    classDef pri fill:#89b4fa,color:#1e1e2e,stroke:#74c7ec
    classDef def fill:#f9e2af,color:#1e1e2e,stroke:#df8e1d
    class C pri
    class F def
```

### OKF adoption & layout tools

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'background': '#1e1e2e', 'primaryColor': '#313244', 'primaryTextColor': '#cdd6f4', 'primaryBorderColor': '#89b4fa', 'lineColor': '#888888'}}}%%
flowchart TB
    subgraph mig["wiki_migrate_okf (dual-mode)"]
        M1{"chat model set?"}
        M1 -->|"yes"| M2["autonomous:<br/>backfill frontmatter<br/>+ migrate_layout"]
        M1 -->|"no"| M3["plan:<br/>candidates + layout move only"]
    end
    subgraph app["wiki_apply_okf"]
        A1["move_page &lt;type&gt;/&lt;slug&gt;<br/>+ rewrite links + rekey log"]
        A1 --> A2["write frontmatter, reindex"]
    end
    subgraph exp["wiki_export_okf"]
        E1["batch_sweep:<br/>[[wikilinks]] → md,<br/>strip Overview,<br/>guarantee frontmatter"]
        E1 --> E2["refresh index.md / log.md"]
    end

    classDef auto fill:#a6e3a1,color:#1e1e2e,stroke:#40a02b
    classDef plan fill:#f9e2af,color:#1e1e2e,stroke:#df8e1d
    class M2 auto
    class M3 plan
```

`migrate_layout` moves each flat `<slug>.md` that carries a frontmatter `type` under
`<type>/<slug>.md`, rewriting intra-domain links (`move_page` →
`links.rewrite_link_targets`) and re-keying the ingest log. A target collision is
**skipped and reported**, never clobbered; an unsafe `type` (containing `/`, `..`,
leading `.`) is left in place under `layout_skipped_unsafe`.

## Health checks (`lint`)

`lint.py` is config-free and never embeds — a pure deterministic report used by
`wiki_lint` and `wiki_remediation_plan`. An absent/empty domain is a clean
`{"wiki_present": false}` no-op.

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'background': '#1e1e2e', 'primaryColor': '#313244', 'primaryTextColor': '#cdd6f4', 'primaryBorderColor': '#89b4fa', 'lineColor': '#888888'}}}%%
flowchart LR
    L["lint(wiki_dir)"] --> B["broken links<br/>(parse_links vs files/anchors)"]
    L --> O["orphans<br/>(unreferenced pages)"]
    L --> S["stale<br/>(src_hash / mtime vs log)"]
    L --> MS["missing_source<br/>(ingest source gone)"]
    L --> LW["legacy_wikilink"]
    L --> SEC["sections<br/>(validate_page findings)"]
    L --> MF["missing_frontmatter"]
    L --> TD["tag_drift<br/>(near-duplicate tags)"]

    classDef actionable fill:#f38ba8,color:#1e1e2e,stroke:#d20f39
    classDef other fill:#f9e2af,color:#1e1e2e,stroke:#df8e1d
    class B,MS actionable
    class O,S,LW,SEC,MF,TD other
```

Every `lint` finding is report-only — none blocks a write (that is `validate_page`'s
job); broken links and `missing_source` are highlighted only as the primary
repair/delete candidates. `wiki_remediation_plan` groups `stale` findings into
`update_candidates` (source
changed, page still valid) and `missing_source` into `delete_candidates`, guarding
each source against `.iwikiignore` and path-escape before reading it.

## Structure validation

`validate_page` enforces the section-formation rules. The **blocking** subset
(`deep_heading`, `pre_h2_text`) is rejected on write; the rest are advisory
(report-only, surfaced by lint).

| Finding | Severity | Rule |
| --- | --- | --- |
| `deep_heading` | block | no `###`+ headings — flatten to `##` |
| `pre_h2_text` | block | no indexable text before the first `##` (only a single `# H1`) |
| `missing_lead` / `long_lead` | advisory | each `##` leads with a ≤250-char paragraph |
| `missing_type` / `unknown_type` | advisory | frontmatter `type` present and in the OKF vocab |
| `missing_description` | advisory | frontmatter has a `description` |
| `unknown_status` | advisory | `status` in `{stub, developing, stable, deprecated}` |

## Configuration & dependencies

Runtime config is entirely env-driven (`engine/config.py`, `Config.load()`); see the
`README.md` **Env reference** for the full table. Key knobs: embeddings
(`IWIKI_EMBED_MODEL`, `IWIKI_EMBED_DIMENSIONS`), search tuning (`IWIKI_TOP_K`,
`IWIKI_SCORE_THRESHOLD`, `IWIKI_SEARCH_MODE`, `IWIKI_SEED_*`, `IWIKI_GRAPH_DEPTH`),
indexing (`IWIKI_CHUNK_SIZE`, `IWIKI_CHUNK_OVERLAP`), and optional
`IWIKI_CHAT_MODEL` / `IWIKI_RERANK_MODEL`.

**External dependencies** (`pyproject.toml`):

| Package | Role |
| --- | --- |
| `mcp` | FastMCP stdio server + tool registration |
| `httpx` | embeddings / chat / rerank HTTP client |
| `numpy` | query-embedding array (float32 cast); cosine itself is pure-Python in `store.py` |
| `pathspec` | gitignore-style `.iwikiignore` matching |
| `filelock` | cross-process git lock on the base |
| `tomli` | `.iwiki.toml` parsing on Python 3.10 (`tomllib` on ≥3.11) |

Dev extra: `pytest`, `pytest-asyncio`, `flake8` (max-line-length 100). Tests never
hit the network — they monkeypatch `indexer.embed_texts` and set dummy `IWIKI_*`
env vars.

## Design invariants (quick reference)

- **Fail-soft handlers.** `@_safe` guarantees a JSON-serializable dict; git and
  embedding failures degrade, never crash.
- **Path-traversal guards run before any filesystem join** — `_validate_domain`,
  `_slug_parts`, `_page_path`, `_contains`, `okf._is_safe_type_segment`,
  `retrieval._domain_file_parts`.
- **Transactional writes** roll back file + log + index on any step failure; writes
  refuse to overwrite.
- **Pre-write freshness** fast-forwards a cleanly-behind base and refuses a
  `diverged` one with zero side effects.
- **Constant duplication is intentional** — `OVERVIEW_HEADING`, `LEAD_MAX`, the
  `_H2` regex, and `RESERVED_*` are copied so config-free modules avoid importing
  `chunk`/`embed`. Change one, change all (the "keep in sync" comments mark them).
- **`VectorStore` is the storage seam** — a future SQLite/sqlite-vec backend only
  needs `load`/`save`/`query`.
- **Domain-relative `file` paths** in the index keep the store machine-portable
  across a shared git base.
