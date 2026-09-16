# OKF compatibility

*Part of the [iwiki-mcp documentation](../README.md#documentation). Русская версия: [okf-compatibility.ru.md](okf-compatibility.ru.md).*

Every page carries a small YAML frontmatter block above the `# Title` H1, written automatically by `wiki_write_page` / `wiki_update_page` / `wiki_apply_okf`. Fields:

| Field | Meaning |
|---|---|
| `type` | Required. **Open** vocabulary: prefer `architecture`, `api`, `guide`, `reference`, `runbook`, `concept` (default), but any value is accepted (e.g. `person`); off-list values get only an advisory `unknown_type`. Also the page's directory: `wiki_write_page` places the file at `<type>/<slug>.md` under the domain root — a bare `slug` is prefixed with the resolved `type`, and a `slug` that already carries a leading segment must match it. |
| `title` | Derived from the page's `# Title` H1. |
| `description` | The authored article summary — the single source of the summary, stored as a separate summary vector for page seeding. It is never prefixed to section vectors and is stored in full (never truncated). Falls back to a `## Overview` section only transitionally (migration). |
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
