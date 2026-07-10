---
review:
  stage: spec
  spec_hash: 037b7a2df1fbfedb
  last_run: 2026-07-10
  chain:
    intent: n/a
  phases:
    structure:
      status: passed
    coverage:
      status: passed
    clarity:
      status: passed
    consistency:
      status: passed
  findings:
    - id: F-001
      phase: clarity
      severity: INFO
      section: "## Goals and non-goals"
      section_hash: f53f27ac9815d542
      fragment: "`## Overview` is removed from the body model — no back-compat fallback (R-02, R-07)."
      text: >-
        The Goals bullet states "no back-compat fallback" unqualified, while build_frontmatter
        (Write path) and batch_sweep (Migration) both perform a transitional `## Overview` ->
        `description` derive. The spec does scope the "no fallback" to chunk.py (Body model:
        "no back-compat fallback in chunk.py"), so this is not a contradiction — but the
        unqualified Goals wording could be read as forbidding the transitional write/migration
        backfill too.
      fix: >-
        Optional: qualify the Goals bullet as "no chunk-time fallback" and note the one-time
        `## Overview` -> `description` backfill happens only in the write path / migration sweep.
      verdict: fixed
      verdict_at: 2026-07-10
    - id: F-002
      phase: clarity
      severity: INFO
      section: "## Authoring rules, README, versioning"
      section_hash: cd971c17add45542
      fragment: "resources.py authoring-rules + README EN/RU updates (documentation of the schema change)"
      text: >-
        The docs deliverables (resources.py authoring-rules, README EN/RU) have no explicit
        acceptance criterion in Success criteria / Testing — they are inspection-only. The new
        `status` field likewise has no dedicated Success Criterion (covered by the Testing
        section but not SC-1..SC-5).
      fix: >-
        Optional: add an inspection SC (authoring-rules mention description/status/reserved
        sections; README schema table updated), or accept as inspection-verifiable.
      verdict: open
      verdict_at: null
---
# OKF Frontmatter v2 — Design

**Date:** 2026-07-10
**Status:** Approved (brainstorming)
**Topic:** `okf-frontmatter-v2`
**Builds on:** `okf-artifacts-inplace` (PR #10, same branch `dev-okf-artifacts-inplace`) — the
migration lands in that work's `wiki_export_okf` in-place sweep.
**Revises:** `okf-frontmatter-adoption` (the `description`-from-Overview + closed-`type` model).

## Overview

Make the frontmatter `description` the single source of the article summary — authored
directly, carried into the retrieval vectors as each section's context prefix — and drop
the `## Overview` body section that previously held it. Align the frontmatter schema with
real OKF v0.1 (open `type`, inline `tags`, scalar `resource`) plus one curated extension,
`status`. Move relationship links into two reserved body sections (`## Outgoing links`,
`## External links`) that are excluded from the vectors and serve the link graph.

Reference: OKF v0.1 (`GoogleCloudPlatform/knowledge-catalog` SPEC.md) — one required field
`type`; recommended `title` / `description` / `resource` / `tags` / `timestamp`; `tags`
inline `[a, b]`; `resource` a scalar URI; links are standard markdown links in the body;
producers may add extra keys. `status` and the reserved link sections are iwiki extensions.

## Goals and non-goals

**Goals**
- `description` is the single, authored source of the article summary (R-01, R-05).
- The summary reaches retrieval via each prose section's chunk prefix (R-06).
- `## Overview` is removed from the body and **excluded from the index** like a reserved
  section — no back-compat fallback, no transitional double-index (R-02, R-07).
- Frontmatter matches OKF core: open `type`, inline `tags`, scalar `resource`; plus a
  `status` field (R-03, R-04, R-08).
- Relationship links live in reserved `## Outgoing links` / `## External links` sections,
  excluded from the vectors, feeding the existing link graph (R-09, R-10, R-11).
- Existing pages migrated by the `wiki_export_okf` sweep (R-14).

**Non-goals**
- No YAML-parser change: `tags` stay inline `[a, b]`, `resource` stays scalar (the current
  `frontmatter.split` handles both). No block-list support added.
- No `outgoing_links` / `external_links` **frontmatter** fields (the Obsidian shape is
  rejected — links belong in the body per OKF).
- No new relationship-search tool — the existing graph (`wiki_related`, `lint`) is the
  link-search surface.
- Body `[[...]]` → markdown link conversion is unchanged (kept from `wiki-markdown-links`).
- `parse_links` stays whole-body: the graph naturally includes the reserved-section links;
  the "not in vectors" guarantee is delivered by `chunk.py` exclusion alone (so no lossy
  link-moving migration is needed).

## Frontmatter schema

```yaml
type: person                 # free-form; OKF_TYPES is advisory only
title: Alice Cooper          # derived from the # H1 (unchanged)
description: "Alice Cooper — lead billing engineer. Covers: AR ledger, refunds. Terms: chargeback, net-30."
resource: src/billing/ar.py  # scalar ingest source (unchanged staleness semantics)
tags: [person, team/billing] # inline list; hierarchical '/' preserved
status: developing           # stub | developing | stable | deprecated (default stub)
timestamp: 2026-07-09        # derived (git last-commit date, else today)
```

| Field | Requirement | Source | Change vs current |
|-------|-------------|--------|-------------------|
| `type` | required | authored param | **open** — no longer clamped to a closed set |
| `title` | recommended | `# H1` | unchanged |
| `description` | recommended | authored `description=` param | **single summary source**; feeds chunk prefix |
| `resource` | optional | ingest-log source | unchanged (scalar; staleness) |
| `tags` | optional | authored param | inline; hierarchical `a/b` allowed |
| `status` | optional (iwiki ext.) | authored `status=` param | **new** field + vocabulary |
| `timestamp` | recommended | git date / today | unchanged |

No `outgoing_links` / `external_links` keys.

### `type` — open vocabulary (R-03)

`build_frontmatter` no longer clamps `type` to `OKF_TYPES`. The value is stored as authored
(faceted search already matches case-insensitively). `OKF_TYPES` remains an **advisory**
vocabulary: `validate` / `lint` emit a non-blocking `unknown_type` when `type` is outside it.
`coerce_type` is replaced by a `normalize_type` that only trims/normalizes for matching (no
clamping). `DEFAULT_TYPE = "concept"` is still used when no `type` is given.

### `status` — new field (R-04)

`STATUS_VOCAB = ("stub", "developing", "stable", "deprecated")`, `DEFAULT_STATUS = "stub"`.
`normalize_status(s)` lowercases/trims. On write, `status=` is normalized; when omitted the
field defaults to `stub`. A value outside `STATUS_VOCAB` is kept as-is and flagged by
`validate` / `lint` as advisory `unknown_status` (non-blocking).

## Body model

### `## Overview` removed (R-02, R-07)

Pages no longer carry a `## Overview` section; the summary lives only in `description`.
`chunk.py` **excludes `## Overview` from the index** exactly like the reserved link
sections, so an un-migrated page's Overview never enters the vectors — there is **no**
back-compat fallback and **no** transitional double-index. The migration sweep
additionally strips `## Overview` from the body. `validate` drops the `missing_overview`
finding.

### Reserved link sections (R-09, R-10)

Two reserved `##` sections, **authored** by the host agent:

```markdown
## Outgoing links
- [Stripe webhooks](wiki_fin_stripe.md)
- [Dunning](wiki_fin_dunning.md)

## External links
- https://stripe.com/docs/webhooks
```

- Reserved headings (case-insensitive): `RESERVED_SECTIONS = ("outgoing links", "external links")`.
- **Excluded from chunking / embedding** (R-10) — like `## Overview` used to be — so link
  lists never enter the vectors or pollute semantic search.
- `validate` exempts reserved sections from the `missing_lead` rule (they are link lists,
  not prose).

## `description` → chunk (`engine/chunk.py`, R-05, R-06)

- `article_summary = meta.get("description", "")` — the **only** source; no `## Overview`
  fallback.
- Sections whose heading (lower-cased) is in `RESERVED_SECTIONS` **or equals `overview`
  (`OVERVIEW_HEADING`)** are dropped before chunking (excluded from the index), so a stray or
  un-migrated `## Overview` never enters the vectors.
- Each remaining prose section's sub-chunks are prefixed with `# {title}` + `description` +
  `## {heading}` + `lead`, as today — only the summary source changes. No standalone summary
  chunk.
- **Invariant change:** the frontmatter `description` now participates in embedding (as the
  prefix). Documented; only `description` enters the prefix, not the whole frontmatter block.

## Links + graph (`engine/links.py`, `wiki_related`, `lint`)

- `parse_links` stays whole-body, so internal edges include the authored `## Outgoing links`
  section (and any inline prose link). No change to `parse_links` is required (R-11).
- `## External links` URLs are non-`.md`/external, so `parse_links` already ignores them for
  internal-edge/orphan purposes — they are display/graph-only, no staleness or broken-link
  check.
- `wiki_related` and `lint` (broken / orphans) are unchanged mechanically; their graph now
  naturally reflects the authored link sections. No new tool.

## `validate.py` (R-12)

- Remove `missing_overview` (Overview no longer required).
- Add advisory `unknown_status` (status outside `STATUS_VOCAB`, only when `status` present).
- Keep `missing_description` (frontmatter present, no description) — now the primary summary
  nudge.
- Exempt `RESERVED_SECTIONS` from `missing_lead`.
- `pre_h2_text`, `deep_heading` unchanged (blocking subset intact).

## Write path (`server.py`, `okf.py`, R-01, R-04, R-13)

- `wiki_write_page` / `wiki_update_page` gain `description: str | None = None` and
  `status: str | None = None` parameters, passed through to `build_frontmatter`.
- `build_frontmatter`:
  - `description`: explicit param wins; else (transitional) derive from a `## Overview` body
    if present; else empty + a warning. No chat model.
  - `status`: normalized `status` param; else `DEFAULT_STATUS` (`stub`).
  - `type`: stored as authored (normalized for match, not clamped); default `concept` +
    warning when absent.
- Transactional write, freshness guard, and the in-place reserved-file refresh
  (`okf-artifacts-inplace`) are all preserved.

## Migration (`okf.batch_sweep` inside `wiki_export_okf`, R-14)

The existing deterministic in-place sweep gains a per-page upgrade (no chat model):
1. If the body has a `## Overview` section: when frontmatter `description` is empty, set it
   from the Overview body; then **remove the `## Overview` section** from the body.
2. If frontmatter has no `status`, set `status: stub`.
3. `type` / `tags` / `resource` untouched. Links untouched (graph is whole-body).
Idempotent: a migrated page (no `## Overview`, has `status`) is a no-op.

## Authoring rules, README, versioning

- `resources.py` (`iwiki://authoring-rules`): `description` is the authored summary (write it
  rich — include `Covers:` / `Terms:` keyword lines for retrieval); no `## Overview` section;
  `status` vocabulary; open `type`; relationship links go in `## Outgoing links` /
  `## External links` (excluded from search).
- `README.md` + `docs/README.ru.md` (EN/RU equivalent): the frontmatter schema table
  (add `status`, open `type`), the description-as-source model, the reserved link sections,
  and the removal of `## Overview`.
- `pyproject.toml` + `src/iwiki_mcp/__init__.py`: **minor** bump `0.2.4` → `0.3.0` (schema +
  chunking behavior change).

## Files touched

- `engine/frontmatter.py` — `normalize_type` (no clamp), `STATUS_VOCAB` / `DEFAULT_STATUS` /
  `normalize_status`, `RESERVED_SECTIONS`.
- `engine/chunk.py` — `article_summary` from `meta["description"]`; exclude `RESERVED_SECTIONS`
  **and `## Overview`** from the index; drop the Overview-as-summary fallback.
- `engine/validate.py` — drop `missing_overview`; add `unknown_status`; exempt reserved
  sections from `missing_lead`; keep `missing_description`.
- `okf.py` — `build_frontmatter` (`description` / `status` / open `type` precedence);
  `batch_sweep` (strip `## Overview`, set `description`, default `status`).
- `server.py` — `description=` / `status=` params on `wiki_write_page` / `wiki_update_page`.
- `resources.py`, `README.md`, `docs/README.ru.md`, `pyproject.toml`,
  `src/iwiki_mcp/__init__.py`.
- Tests.

## Success criteria

- **SC-1:** `uv run pytest -q` green; `uv run flake8 src tests` clean.
- **SC-2:** a page authored with `description=` and no `## Overview` chunks correctly — each
  prose section's vector text starts with the `description` summary; no `## Overview` needed.
- **SC-3:** `## Outgoing links` / `## External links` sections are absent from the index /
  `wiki_search` results, and their internal links still appear in the `wiki_related` /
  `lint` graph.
- **SC-4:** `wiki_export_okf` migrates a legacy page (removes `## Overview`, sets
  `description` from it when empty, defaults `status`), and is idempotent on re-run.
- **SC-5:** version is `0.3.0` in `pyproject.toml` and `__init__.py`; `type` accepts a
  free-form value (e.g. `person`) with only an advisory `unknown_type`.

## Testing

- `frontmatter`: `normalize_type` (no clamp), `normalize_status`, `RESERVED_SECTIONS`.
- `chunk_markdown`: summary from `description`; no `## Overview` fallback; `## Overview` and
  reserved link sections excluded from the index; prose sections carry the description prefix.
- `validate_page`: no `missing_overview`; `unknown_status` advisory; reserved sections don't
  trigger `missing_lead`; `missing_description` still fires.
- Write: `wiki_write_page(description=, status=)` writes the fields; missing `status` →
  `stub`; missing `description` → warning; free-form `type` kept.
- `batch_sweep`: strips `## Overview` and backfills `description` + `status`; idempotent;
  preserves `type` / `tags` / links.
- Search/graph: reserved link sections not indexed; `wiki_related` still resolves an outgoing
  link authored in `## Outgoing links`.

## Risks and mitigations

- **Un-migrated `## Overview` in the body** — `chunk.py` excludes it from the index (like a
  reserved section), so it never enters the vectors even before migration; the sweep then
  strips it from the body. No transitional double-index.
- **`description` now embedded** — intentional; only the `description` string enters the
  prefix, mirroring the old Overview-prefix behavior, so vector size is comparable.
- **Open `type` drift** — mitigated by the advisory `unknown_type` lint and the retained
  `OKF_TYPES` vocabulary as guidance.
- **Authored link sections drift from prose** — accepted; links are authored in one place
  (the reserved sections), and the graph reads the whole body so nothing silently breaks.
