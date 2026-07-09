# OKF Frontmatter Adoption — Design

**Date:** 2026-07-09
**Status:** Approved (brainstorming)
**Topic:** `okf-frontmatter-adoption`

## Overview

Adopt the *container* of Google Cloud's Open Knowledge Format (OKF v0.1) into
iwiki-mcp — YAML frontmatter, a required `type` field, and portable OKF bundle
export — while keeping iwiki's stricter page-body rules untouched. The goal is
twofold: raise internal documentation quality and unify metadata (faceted
retrieval, freshness, GitHub-renderable frontmatter), and make any iwiki base
portable to external OKF consumers **without manual page rework**. Existing
pages are made conformant by an idempotent auto-backfill migration; new pages
are written with frontmatter natively.

OKF is deliberately adopted only as far as its interoperability surface (the
frontmatter container and bundle conventions). iwiki's body model — `##`-only
sections, a leading `## Overview` summary, section lead paragraphs, and
`[[slug#Heading]]` cross-links — is **retained as-is**, because that structure
powers iwiki's whole-article-context chunking and hybrid retrieval. OKF's
"free-form body" is intentionally **not** adopted.

Reference: OKF v0.1, `GoogleCloudPlatform/knowledge-catalog` (SPEC.md),
published 2026-06-12.

## Goals and non-goals

**Goals**
- Every page carries OKF-conformant YAML frontmatter written into the file.
- Existing pages backfilled automatically (idempotent), no manual editing.
- Full OKF conformance (standard markdown links, `index.md`/`log.md`) produced
  on demand by an export tool, without mutating source files.
- Faceted retrieval by `type` and `tags`.

**Non-goals**
- Not adopting OKF's free-form body model. iwiki body rules stay.
- Not rewriting `[[slug#Heading]]` links in source files (body is untouched);
  link conversion happens only in exported bundle copies.
- Not renaming iwiki's internal `.iwiki/index.jsonl` / `.iwiki/log.jsonl`.
- Not a public web-discovery format (OKF itself is an internal bundle format).

## Frontmatter schema

Written into each `.md` page, directly above the `# Title` H1:

```yaml
---
type: architecture
title: Base binding model
description: How .iwiki.toml binds read/write domains and resolves paths…
resource: src/iwiki_mcp/base.py
tags: [binding, config]
timestamp: 2026-07-09
---
```

Field sources and determinism:

| Field         | OKF role            | Source in iwiki                         | Deterministic |
|---------------|---------------------|-----------------------------------------|---------------|
| `type`        | required            | LLM classification of body              | no (LLM)      |
| `title`       | reference-parser    | `# H1` line                             | yes           |
| `description` | reference-parser    | `## Overview` body, ≤400 chars          | yes           |
| `resource`    | optional            | `source=` recorded in the ingest log    | yes           |
| `tags`        | optional            | LLM-suggested                           | no (LLM)      |
| `timestamp`   | reference-parser    | git last-commit date of the file, or    | yes           |
|               |                     | the page's ingest-log `date`            |               |

Covering `type` + `title` + `description` + `timestamp` satisfies both the
spec's single required field and Google's reference parser (which additionally
rejects files missing `title`/`description`/`timestamp`).

## Engine changes (stdlib-only core)

### New module `engine/frontmatter.py`

The single seam for frontmatter handling. Stdlib-only — it must be importable
by `validate.py` and `lint.py`, which are required to stay config-free (no
`httpx`). Therefore a minimal YAML-subset parser, **not** a `pyyaml`
dependency.

- `split(content: str) -> tuple[dict, str]` — strip a leading `---\n…\n---\n`
  block, return `(meta, body)`. No frontmatter → `({}, content)`.
- `render(meta: dict) -> str` — emit a frontmatter block.
- Parses only what iwiki writes: scalar `key: value` lines and
  `tags: [a, b]` inline lists. Deterministic; tolerant of absent/malformed
  blocks (fail-soft to `{}`).

### Body-only processing in existing modules

All three operate on `body` (post-`split`), never the raw content:

- `chunk.py` — `chunk_markdown` calls `frontmatter.split`, chunks `body`.
  Frontmatter is not embedded (no vector bloat, no Overview duplication). It
  also stamps `type`/`tags` from `meta` onto each `Chunk` (for faceted search).
- `validate.py` — `validate_page` validates `body`, so `pre_h2_text` no longer
  false-fires on frontmatter lines. Body rules (`##`-only, Overview, lead) are
  unchanged. Adds advisory findings `missing_type`, `missing_description`
  (non-blocking, so legacy pages are not rejected before migration).
- `lint.py` — reads `body` for section/link checks; also reports pages lacking
  frontmatter as `wiki_migrate_okf` candidates.

The blocking validation subset (`deep_heading`, `pre_h2_text`) is mirrored by
the `iwiki-validate` PreToolUse hook noted in `validate.py`'s docstring — that
mirror must also strip frontmatter before the `pre_h2_text` check.

## Faceted retrieval by `type` / `tags`

### Index carries metadata (`store.py`)

- `Record` gains `type: str | None = None` and `tags: list[str] = []`, both with
  defaults so pre-existing JSONL indices load unchanged (back-compat).
- `make_record` copies `type`/`tags` from the `Chunk`. Re-index on write/migrate
  populates them automatically.

### Retrieval filters (`retrieval.py`)

- `vector_search` / `lexical_search` / `hybrid_search` gain optional
  `type: str | None = None`, `tags: list[str] | None = None`.
- A record passes iff `(type is None or rec.type == type)` **and**
  `(not tags or set(tags) & set(rec.tags))`.
- Vector hits filter directly on `Record`. Lexical hits (grep has no
  frontmatter) resolve `type`/`tags` by reading the target file's frontmatter;
  bounded by `top_k`, so cheap. Shared `_facet_ok` helper.

### Tool surface (`server.py`)

- `wiki_search` gains optional `type=` / `tags=`, passed through to
  `hybrid_search`. Omitting them preserves current behaviour exactly.

## Write path (`server.py`, top layer)

- `wiki_write_page` / `wiki_update_page`: after body validation, derive
  `title` / `description` / `timestamp` / `resource` deterministically and write
  the frontmatter block above the body. `type` / `tags` come from one LLM
  classification of the body. An optional `type=` argument lets the author set
  it explicitly and skip the LLM call.
- Transactional write is preserved: frontmatter is part of the written file, so
  the existing rollback (delete file, drop last ingest-log line) still covers it.

## Backfill migration — `wiki_migrate_okf(domain)`

New MCP tool. Makes an existing base conformant with no manual rework.

- Iterates pages lacking frontmatter. Derives deterministic fields from
  H1 / Overview / ingest-log / git. `type` / `tags` from LLM (batched).
- Writes the frontmatter block into each file, then re-indexes the domain.
- Idempotent: pages that already have frontmatter are skipped; re-runs are safe.

## Export bundle — `wiki_export_okf(domain, dest)`

New MCP tool. Pure serialization, no LLM. Produces a fully OKF-conformant bundle
in `dest/` without touching source files.

- Copies pages, guaranteeing frontmatter on each.
- Converts `[[slug#Heading]]` → `[Heading](slug.md)` (standard markdown links,
  so external tools can build the graph). **Conversion happens only in the
  copy**; sources keep their `[[refs]]`.
- Generates OKF reserved files: `index.md` (navigation from the domain
  structure) and `log.md` (history from `.iwiki/log.jsonl`).
- Reserves the `index` / `log` slugs (edge case: a page already named that).

## Authoring rules, README, versioning

- `resources.py` (`AUTHORING_RULES`, exposed as `iwiki://authoring-rules`):
  add a frontmatter-fields section.
- `README.md` + `docs/README.ru.md`: document the new tools and OKF
  compatibility (kept in sync, EN + RU).
- `pyproject.toml`: **minor** version bump (new feature), `0.1.x` → `0.2.0`.

## Testing

Follow the repo pattern: monkeypatch `indexer.embed_texts`, dummy `IWIKI_*`
env, no network.

- `frontmatter.split` / `render`: round-trip, absent block, malformed block,
  `tags` list parsing.
- `chunk_markdown` with frontmatter: frontmatter excluded from chunks; `type`/
  `tags` stamped on chunks.
- `validate_page`: frontmatter does not trigger `pre_h2_text`; `missing_type` /
  `missing_description` are advisory only.
- `wiki_migrate_okf`: derives fields; idempotent on re-run; skips already-
  migrated pages.
- `wiki_export_okf`: `[[refs]]` converted to markdown links in the copy;
  sources untouched; `index.md` / `log.md` generated.
- Faceted search: `type` narrows results; `tags` intersection; a pre-existing
  index without the new fields loads; facets combine with hybrid mode.
- `store.Record`: old JSONL (no `type`/`tags`) loads via defaults.

## Files touched

- New: `src/iwiki_mcp/engine/frontmatter.py`
- Modified: `engine/chunk.py`, `engine/validate.py`, `engine/lint.py`,
  `engine/store.py`, `retrieval.py`, `indexer.py`, `server.py`, `resources.py`,
  `pyproject.toml`, `README.md`, `docs/README.ru.md`
- New tests under `tests/`
- PreToolUse `iwiki-validate` hook mirror: strip frontmatter before
  `pre_h2_text`.

## Risks and mitigations

- **Chunking/retrieval regression** — mitigated by stripping frontmatter before
  the `##` split; body is untouched.
- **Index back-compat** — `Record` new fields default, so old JSONL loads.
- **LLM non-determinism for `type`** — accepted per decision; deterministic
  fields cover the rest; `type=` override available.
- **OKF v0.1 is a draft** — we target its stable core (`type` + the
  reference-parser fields); export is a thin adapter that can track spec drift.
```
