# Tools

*Part of the [iwiki-mcp documentation](../README.md#documentation). Русская версия: [tools-reference.ru.md](tools-reference.ru.md).*

| Tool | What it does |
|---|---|
| `wiki_search` | Read modes are exactly `hybrid`, `lexical`, and `semantic`; an explicit mode overrides `IWIKI_SEARCH_MODE` (default `hybrid`), while `vector` is rejected as a public mode. Semantic page descriptions, lexical page matches, graph pages, global semantic chunks, and lexical sections are ranked independently and fused with RRF before final top-k. Results contain `hit` (`semantic`/`lexical`/`both`) and `source` (`seed`/`graph`/`global`/`lexical`). When `IWIKI_RERANK_MODEL` is set, exact current chunks from the full candidate ceiling are sent in one authenticated 60-second LiteLLM batch, while provider `top_n` is limited to requested final `k`; failure preserves preliminary order and returns only sanitized `rerank` metadata. `scope`, `domains`, `k`, `threshold`, `type`, and `tags` constrain read search. `intent="write"` remains the isolated summary-vector write-target lookup and ignores read mode/reranking; its target is the bound primary and falls back to the first named domain only when no primary is bound, and the hosted gate authorizes that same domain. On a hosted server every answer additionally carries `binding_source`, and one whose scope came from the binding rather than the call — a read without `domains`, or any write-intent lookup — adds `binding_defaulted` to `warnings`. |
| `wiki_read_page` | Read one Markdown page by domain and slug. With `heading`, return only that one `##` section (including its `section_hash`) instead of the whole page. |
| `wiki_list_pages` | List page slugs and files in a domain. |
| `wiki_related` | Return related sections for a section id within one domain; its `{"vector": [], "graph": []}` shape and domain-local fallback stay unchanged. |
| `wiki_write_page` | Validate and write a new page, index the domain, commit and push. |
| `wiki_update_page` | Update one existing page: section-only (`heading` + `new_body`), code-only (`code`), or combined atomically. With `new_heading`, rename that heading and atomically rewrite exact visible incoming links when their domains are writable. Accepts `expected_section_hash` for optimistic concurrency. |
| `wiki_insert_section` | Insert one new `##` section (positioned with `after_heading` / `before_heading`) without rewriting the rest of the page. |
| `wiki_delete_section` | Delete one existing `##` section without rewriting the rest of the page. Accepts `expected_section_hash`. |
| `wiki_move_section` | Reorder one existing `##` section (positioned with `after_heading` / `before_heading`) without rewriting its body. Accepts `expected_section_hash`. |
| `wiki_delete_page` | Delete one page by domain and slug: remove the file, append a `delete` log op, reindex the domain, commit and push. Rolls back on failure. |
| `wiki_index` | Rebuild one domain index (defaulting to the bound write domain when omitted), commit and push. |
| `wiki_list_domains` | List visible domain directories in the base with index sizes. |
| `wiki_create_domain` | Create an empty domain directory and return whether the base auto-commit succeeded; the domain's `index.jsonl` / `log.jsonl` are created lazily at the domain root on first write or index. |
| `wiki_bind` | Narrow PostgreSQL scope and, in a hosted HTTP session, optionally carry a project `project_policy` object (`specification_mode`, `max_snapshot_age_seconds`) for that session — `specification_mode` alone via the deprecated `specification_mode` parameter, never both; a later `wiki_bind` call **replaces** the whole `project_policy` object rather than merging it, so a bind that omits a field it previously set drops that field back to the hosted default; local PostgreSQL stdio rejects both parameters, while Git configuration changes return `project_config_manual_edit_required` and must be made manually. |
| `wiki_status` | Show resolved base, project directory, read domains, write domain, and available domains. |
| `wiki_lint` | Read-only Markdown-authoritative health report: broken/reserved/unavailable-domain links, orphans, stale pages, `missing_source`, and section gaps, plus an independent per-domain SQLite graph parity report (`state`, fingerprint, pages, edges, anchors). It never creates or rebuilds the cache; non-ready or mismatched graph state includes a `wiki_index` remediation hint. |
| `wiki_remediation_plan` | Group current lint findings into read-only update/delete remediation actions. |
| `wiki_migrate_okf` | Backfill OKF frontmatter and normalize type-directory layout, autonomously with a chat model or as a review plan without one. |
| `wiki_apply_okf` | Apply reviewed OKF metadata and layout decisions; a type-directory move atomically rewrites exact visible incoming links. |
| `wiki_export_okf` | Run the deterministic in-place OKF conformance sweep and regenerate root `index.md` / `log.md`. |
| `wiki_sync` | Run `git pull --rebase` and `git push` in the base. |

`wiki_write_page` refuses to overwrite an existing page in v1. `wiki_update_page` has three modes: section-only requires paired `heading` and `new_body`; code-only uses `code` and preserves the page body byte-for-byte; combined atomically performs both. The published root JSON Schema stays a plain object with `domain` and `slug` root-required: client tool validation rejects a root combinator, so a root `anyOf` would make clients drop the tool. Runtime validation enforces the mutually exclusive operations instead and rejects partial, no-op, or unsafe selectors before mutation. `new_heading` is optional with a section update: it rewrites exact incoming relative links in the page domain and exact `iwiki://` links from visible read domains. A nonempty valid `code` mapping completely replaces selectors (`code.symbols`, `code.files`, `code.source_globs`); `{}` or all-empty lists clears them; omitted or `null` `code` preserves them on a section update. A code-only response omits `heading` and adds no fields; section and combined responses retain `heading`.

```json
{"domain":"engineering","slug":"api/auth","code":{"symbols":[{"qualified_name":"auth.login"}],"files":[],"source_globs":[]}}
{"domain":"engineering","slug":"api/auth","heading":"API","new_body":"Updated login contract.","code":{"source_globs":["src/auth/**/*.py"]}}
```

Git retains its existing freshness and strict-spec transaction, then reindexes, commits, and refreshes the graph once. PostgreSQL uses the current `expected_revision` CAS in one revision and transaction; unchanged chunks reuse embeddings. Republish makes Code-graph Wiki links current. `wiki_apply_okf` applies the same transaction only when a type change moves a page. `wiki_insert_section` and `wiki_delete_section` add or remove one `##` section, and `wiki_move_section` reorders one, all without rewriting the rest of the page. `wiki_update_page`, `wiki_delete_section`, and `wiki_move_section` accept `expected_section_hash` (from a prior `wiki_read_page(..., heading=...)`) for optimistic concurrency: a stale hash is rejected with `section_conflict` instead of silently overwriting a concurrent edit.

The cross-domain operation starts only when every discovered visible referrer is in `write`; a visible read-only referrer blocks before any Markdown changes. Hidden domains are not inspected or reported, and are never rewritten. Results include `transaction_id`, `rewritten_pages`, `affected_domains`, and `rewritten_links` in addition to normal write fields.

Each cross-domain operation holds the base mutation lock, stages only affected Markdown plus domain-root `index.jsonl` / `log.jsonl`, and creates one local commit carrying `Iwiki-Transaction: <id>`. Its fsynced local journal is `.iwiki/transactions/<id>` and advances `prepared` → `applied` → `committed` → `finalized`. A pre-commit interruption restores snapshots; a post-commit interruption repairs/marks the derived graph and finalizes the journal before another overlapping mutation. Ambiguous recovery returns `manual_recovery_required`. Push remains fail-soft: a local commit and authoritative portable files are retained if publication fails.

`wiki_lint` reports `missing_source` pages whose ingest source has disappeared. Remove such a stale page explicitly with `wiki_delete_page` after confirming with the user; `wiki_sync` then propagates the deletion to the remote like any other commit.

Project-relative stale-source resolution remains a separate audited follow-up; this
release does not silently change that behavior.

The server also exposes the MCP resource `iwiki://authoring-rules` for page-structure rules.
