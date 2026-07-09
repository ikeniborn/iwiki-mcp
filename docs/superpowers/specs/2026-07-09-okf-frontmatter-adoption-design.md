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
| `type`        | required            | host agent classifies into a closed set | no (agent)    |
| `title`       | reference-parser    | `# H1` line                             | yes           |
| `description` | reference-parser    | `## Overview` body, ≤400 chars          | yes           |
| `resource`    | optional            | `source=` recorded in the ingest log    | yes           |
| `tags`        | optional            | host agent, reuse-biased + normalized   | no (agent)    |
| `timestamp`   | reference-parser    | git last-commit date of the file, or    | yes           |
|               |                     | the page's ingest-log `date`            |               |

Covering `type` + `title` + `description` + `timestamp` satisfies both the
spec's single required field and Google's reference parser (which additionally
rejects files missing `title`/`description`/`timestamp`).

**Who classifies — two paths, one governance.** The iwiki server runs inside a
host agent (Claude Code / Codex) that is itself an LLM, but it must not be
*hard-bound* to that host nor to any one external model. So classification of
`type`/`tags` has two sources, resolved by precedence:

1. **Explicit params.** If the caller passes `type`/`tags`, they win (the host
   agent authored the page and knows them).
2. **Optional server-side model.** Else, if `IWIKI_CHAT_MODEL` is set, the
   server classifies via an OpenAI-compatible chat endpoint (reusing
   `IWIKI_LLM_BASE_URL`/`IWIKI_LLM_KEY`), best-effort.
3. **Default.** Else `type` → `concept`, `tags` → `[]`, with a `warning`.

`IWIKI_CHAT_MODEL` is **optional and empty by default** — no server-side chat is
introduced unless the operator opts in. Either way the server *governs* the
result (see below). Whatever the source, `type`/`tags` are **governed** so the
vocabulary stays unified instead of drifting (the known OKF weakness where two
conformant bundles share no common vocabulary).

## Type and tag governance

Whoever fills `type`/`tags` — the host agent (explicit params) or the optional
server-side model — does so under rules the server enforces to keep the
vocabulary convergent across the whole base.

### `type` — closed vocabulary

A single hardcoded enum, global to every base and domain (maximum unification,
zero config). Defined once in `engine/frontmatter.py` as `OKF_TYPES`, with
`concept` as the default/fallback:

| type           | When                          | Body signal                     |
|----------------|-------------------------------|---------------------------------|
| `architecture` | system structure              | components, data flow, modules  |
| `api`          | a call/interface surface      | functions, endpoints, signatures|
| `guide`        | how to do something           | step-by-step, usage             |
| `reference`    | lookup material               | tables of keys, flags, configs  |
| `runbook`      | operational procedure         | deploy, incident, runbook steps |
| `concept`      | explains an idea/model        | **default / fallback**          |

- The classifier (host agent via `type=`, or the optional server-side model)
  **chooses from** `OKF_TYPES`; it never invents a type. The rubric guides both.
- Any value outside `OKF_TYPES` (or an omitted `type` with no server model) is
  clamped by the server to `concept` via `coerce_type`.
- The rubric text lives in `resources.py` (`iwiki://authoring-rules`), in the
  tool descriptions, and in the server-side classify prompt — one shared
  criterion across all sources.
- `validate.py` adds an advisory `unknown_type` finding when a page's `type` is
  not in `OKF_TYPES` (non-blocking; surfaces drift without rejecting).
- A per-domain override is intentionally deferred (YAGNI) — one global enum is
  what makes the base uniform.

### `tags` — open but disciplined

Tags stay open-ended (closing them into an enum would defeat their purpose), but
three rules prevent sprawl:

1. **Normalization (deterministic).** `frontmatter.normalize_tag`: lowercase,
   kebab-case, spaces/underscores → `-`, drop empties, cap at `MAX_TAGS = 5` per
   page. Kills case/format duplicates (`Config` / `config` / `configuration`
   collapse by form). Applied on both write and migrate.
2. **Reuse-first.** On write/migrate the classifier is given the domain's
   **current tag vocabulary** (collected from the index's `tags` across all
   records) — in the tool result for the host agent, or in the prompt for the
   server-side model — and is asked to reuse an existing tag where one fits,
   coining a new tag only when nothing matches. Biases toward convergence.
3. **Drift visibility.** `wiki_lint` reports near-duplicate tag pairs (shared
   prefix / small edit distance) as a `tag_drift` finding, so synonyms that slip
   through get curated by hand. Deterministic, stdlib-only (no embeddings).

The tag vocabulary is derived, not stored separately: it is simply the set of
`tags` already present across the domain's index records.

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
- Governance constants and helper (single source of truth, stdlib-only):
  `OKF_TYPES` (the closed type enum), `DEFAULT_TYPE = "concept"`,
  `MAX_TAGS = 5`, and `normalize_tag(s) -> str` (lowercase, kebab-case,
  spaces/underscores → `-`). `coerce_type(s) -> str` maps any value to a member
  of `OKF_TYPES`, falling back to `DEFAULT_TYPE`.

### Body-only processing in existing modules

All three operate on `body` (post-`split`), never the raw content:

- `chunk.py` — `chunk_markdown` calls `frontmatter.split`, chunks `body`.
  Frontmatter is not embedded (no vector bloat, no Overview duplication). It
  also stamps `type`/`tags` from `meta` onto each `Chunk` (for faceted search).
- `validate.py` — `validate_page` validates `body`, so `pre_h2_text` no longer
  false-fires on frontmatter lines. Body rules (`##`-only, Overview, lead) are
  unchanged. Adds advisory findings `missing_type`, `missing_description`, and
  `unknown_type` (type not in `OKF_TYPES`) — all non-blocking, so legacy pages
  are not rejected before migration.
- `lint.py` — reads `body` for section/link checks; reports pages lacking
  frontmatter as `wiki_migrate_okf` candidates, and near-duplicate tag pairs as
  `tag_drift` (shared prefix / small edit distance; deterministic, stdlib-only).

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
  the frontmatter block above the body. `type` / `tags` follow the precedence
  above: explicit params win; else the optional server-side model classifies
  (when `IWIKI_CHAT_MODEL` is set); else default `concept` / `[]` with a warning.
  The server governs the result — `type` `coerce_type`-clamped to `OKF_TYPES`,
  `tags` `normalize_tag`-normalized, deduped, capped at `MAX_TAGS`.
  `wiki_update_page` preserves existing `type`/`tags` and only refreshes the
  deterministic fields unless new values are passed.
- The tool descriptions carry the type rubric and the domain's current tag
  vocabulary hint so the host agent fills `type`/`tags` consistently when it is
  the classifier.
- Transactional write is preserved: frontmatter is part of the written file, so
  the existing rollback (delete file, drop last ingest-log line) still covers it.

## Backfill migration — dual-mode, plan + apply

Two MCP tools whose behavior adapts to whether a server-side model is configured.
Either way this is "no manual rework" — a machine does the work, not the human.

- **`wiki_migrate_okf(domain)`.**
  - *With `IWIKI_CHAT_MODEL` set:* autonomously classifies every page lacking
    frontmatter (server-side, best-effort, reuse-first against the growing
    domain vocabulary), writes the frontmatter block into each file, and
    re-indexes. Returns `migrated` / `skipped` / `warnings`.
  - *Without it:* runs as a **plan** (no writes) — for each page lacking
    frontmatter returns `slug`, the page `body`, the deterministically derived
    fields, the domain's current tag vocabulary, the `OKF_TYPES` rubric,
    `authoring_rules`, and `next_steps` pointing at `wiki_apply_okf`. Mirrors the
    existing `wiki_remediation_plan` idiom.
  - Idempotent either way: pages that already have frontmatter are skipped.
- **`wiki_apply_okf(domain, slug, type, tags=None)` (apply, one page).** The
  host agent, having classified a page from the plan, calls this. The server
  derives the deterministic fields, clamps `type`, normalizes `tags`, writes the
  frontmatter block into the file, and re-indexes. Transactional like the other
  write tools; re-running just refreshes the page's frontmatter (safe).

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
- Governance helpers: `coerce_type` clamps off-vocab → `concept` and keeps valid
  members; `normalize_tag` lowercases/kebab-cases; `MAX_TAGS` cap applied.
- `chunk_markdown` with frontmatter: frontmatter excluded from chunks; `type`/
  `tags` stamped on chunks.
- `validate_page`: frontmatter does not trigger `pre_h2_text`; `missing_type` /
  `missing_description` / `unknown_type` are advisory only.
- `lint`: `tag_drift` flags a near-duplicate tag pair; distinct tags don't.
- `classify` (server-side path): parses/governs a chat response; off-vocab →
  `concept`; endpoint failure is best-effort (default + warning, never raises).
  Gated on `IWIKI_CHAT_MODEL`; monkeypatched in tests (no network).
- Write precedence: explicit `type`/`tags` win; with no params and no chat model,
  defaults to `concept`/`[]` + warning.
- `wiki_migrate_okf`: plan mode (no chat model) returns candidates + rubric;
  autonomous mode (chat model set) writes frontmatter; idempotent; skips
  already-migrated pages. `wiki_apply_okf`: applies agent-classified type/tags.
- `wiki_export_okf`: `[[refs]]` converted to markdown links in the copy;
  sources untouched; `index.md` / `log.md` generated.
- Faceted search: `type` narrows results; `tags` intersection; a pre-existing
  index without the new fields loads; facets combine with hybrid mode.
- `store.Record`: old JSONL (no `type`/`tags`) loads via defaults.

## Files touched

- New: `src/iwiki_mcp/engine/frontmatter.py`, `src/iwiki_mcp/engine/classify.py`
  (server-side classify, gated on `IWIKI_CHAT_MODEL`), `src/iwiki_mcp/okf.py`
  (top-layer frontmatter assembly), `src/iwiki_mcp/export.py`
- Modified: `engine/chunk.py`, `engine/validate.py`, `engine/lint.py`,
  `engine/store.py`, `engine/config.py` (add `chat_model`), `retrieval.py`,
  `indexer.py`, `server.py`, `resources.py`, `pyproject.toml`, `README.md`,
  `docs/README.ru.md`
- New tests under `tests/`
- PreToolUse `iwiki-validate` hook mirror: strip frontmatter before
  `pre_h2_text`.

## Risks and mitigations

- **Chunking/retrieval regression** — mitigated by stripping frontmatter before
  the `##` split; body is untouched.
- **Index back-compat** — `Record` new fields default, so old JSONL loads.
- **Classifier non-determinism for `type`** — accepted; deterministic fields
  cover the rest; explicit `type=` always overrides; `coerce_type` clamps.
- **No hard model binding** — server-side classification is optional
  (`IWIKI_CHAT_MODEL`); unset means the host agent classifies or the field
  defaults. The server keeps working with only its embeddings dependency.
- **OKF v0.1 is a draft** — we target its stable core (`type` + the
  reference-parser fields); export is a thin adapter that can track spec drift.
```
