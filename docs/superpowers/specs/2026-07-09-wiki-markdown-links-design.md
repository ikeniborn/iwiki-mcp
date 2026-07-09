# Wiki Markdown Link Graph — Design

**Date:** 2026-07-09
**Status:** Approved (brainstorming)
**Topic:** `wiki-markdown-links`

## Overview

Replace the Obsidian-style `[[slug#Heading]]` wiki-links currently used for
iwiki's page graph with **CommonMark relative links** (`[Heading](slug.md#anchor)`)
in the page sources. `[[...]]` is the classic *wiki* convention (MediaWiki →
Obsidian → Foam) but it is **not** CommonMark/GFM and **not** what OKF specifies;
OKF's interop surface is standard markdown links. This aligns the source body
with the adopted OKF standard, makes pages render as clickable links on GitHub,
and removes the need for any `[[...]]`→markdown conversion at export time.

Scope is deliberately narrow — this is sub-project **#1** of a three-part graph
effort surfaced during brainstorming:

- **#1 (this spec):** `[[...]]` → markdown links, page↔page graph.
- **#2 (deferred):** code-affinity edges derived from the `resource:` frontmatter
  field (pages sharing/nesting a source treated as related).
- **#3 (deferred, large):** a full code graph (symbols, import graph) — its own
  parser, storage, and freshness model. A separate product-sized effort.

Migration is **lazy, at write time** — no bulk migration tool. Pages convert to
markdown links as they are naturally edited; the parser reads **both** formats
during the transition so the graph never loses edges.

## Goals and non-goals

**Goals**
- Page sources use CommonMark relative links for cross-references.
- New writes are normalized to markdown links automatically; existing pages
  migrate lazily as they are edited.
- The page graph (`related.py`, `lint.py`) stays complete throughout the
  transition (parser reads `[[...]]` and markdown links).
- `related.py` behaviour is unchanged; `lint.py` changes are minimal.

**Non-goals**
- No bulk migration MCP tool. Conversion happens only through the existing
  write/update handlers.
- No code-affinity edges (`resource:`) — that is sub-project #2.
- No code graph (symbols/imports) — that is sub-project #3.
- No change to chunking/embedding. Links are body prose; chunking splits on `##`
  and is untouched.
- No cross-domain links (v1 stays within a single domain, as today).

## Background — current link mechanism

`[[...]]` is read in exactly three places; nothing else depends on it:

- `engine/links.py` — `parse_links(content)` regex-extracts `[[target]]` /
  `[[target|alias]]`, ignoring fenced (`_FENCE`) and inline (`_INLINE`) code, and
  returns the target part de-duplicated, order-preserving.
- `engine/related.py` — `_graph_neighbours` does a BFS over `parse_links`,
  taking the slug part (`link.split("#", 1)[0]`) and appending `.md`.
- `engine/lint.py` — iterates `parse_links(c)`, `ref.partition("#")`, resolves
  the slug to a file, records `referenced_by`, and flags broken refs (missing
  target file, or a `#heading` absent from the target's heading set).

`resources.py` line 19 documents the authoring rule as `[[slug#Heading]]`.
`export.py` does **not** exist yet (OKF export is planned, unwritten), so no
export-time conversion needs changing.

The links are **not** load-bearing for chunking or embedding — the change
surface is small.

## Parser — dual-read (`engine/links.py`)

`parse_links` is extended to read **both** link formats and return a single
normalized shape, so `related.py`/`lint.py` keep their contract.

- **Markdown links.** Regex matches inline `[text](target)` and keeps only
  wiki-page edges:
  - Reject images (`![...](...)` — leading `!`).
  - Reject external (`://`, `mailto:`), absolute (`/…`), and bare same-page
    anchors (`#…`).
  - Keep only targets whose path ends in `.md` (optionally `#anchor`).
  - Reference-style links (`[text][ref]`) are **not** supported (iwiki never
    writes them) — documented, not handled.
- **Legacy `[[...]]`.** The existing `_LINK` regex still runs, so un-migrated
  pages remain in the graph. Once every page is migrated this branch simply
  finds nothing (harmless).
- **Code is still stripped** (`_strip_code`) before matching, for both formats —
  a link-like token inside a code sample (e.g. bash `[[ $# -gt 0 ]]`, or a
  markdown example ``[t](base.md)``) is an illustration, not a graph edge.
- **Normalized return.** For every hit, strip a leading `./` and the `.md`
  suffix from the slug, and **slugify the heading** (see below) so both formats
  yield the same `"slug#heading-slug"` string (or `"slug"` when no heading).
  De-duplicated, order-preserving — same contract as today.

`related.py` uses only the slug part → unaffected. `lint.py` compares the
(now slugified) heading against a slugified heading set → one-line change.

**Contract change.** For legacy `[[slug#Heading]]`, the heading portion of the
return shifts from raw text (`"slug#Heading"`, today's behaviour) to a slug
(`"slug#heading-slug"`). `related.py` ignores the heading portion; `lint.py`
slugifies its heading set to match. Existing `test_links.py` assertions on the
returned heading form are updated accordingly.

## Heading slug helper

`[[slug#Heading]]` carried the heading **text** (`Related sections`); a markdown
anchor is a **slug** (`related-sections`). A single stdlib-only
`slugify_heading(s)` in `engine/links.py` (importable by `lint.py`, which must
stay config-free) produces the anchor:

- Target GitHub's heading-anchor algorithm: lowercase; drop characters other than
  word-chars, spaces, and hyphens; collapse whitespace to `-`; collapse repeated
  `-`. This keeps anchors resolvable when a page renders on GitHub (the Overview
  goal), and — because the **same** function feeds parser, rewrite, and lint —
  keeps the three internally consistent.
- Residual gap: GitHub de-duplicates repeated headings with `-1`/`-2` suffixes;
  iwiki's `##`-only structure makes duplicate headings rare, so v1 does not
  handle it (a duplicate-heading anchor jumps to the first occurrence). Deferred.

Used by: the parser (heading → slug on extraction), write-time normalization
(heading → anchor), and `lint.py` (heading set → slugs for the `#anchor` check).

## Write-time normalization (`engine/links.py` + `server.py`)

No migration tool. A pure engine function converts on write:

- `to_markdown_links(body: str) -> str` (in `engine/links.py`, stdlib-only):
  masks code spans (fenced + inline), rewrites the four `[[...]]` forms outside
  code, restores code verbatim. Bash/markdown examples inside fences are never
  touched.
  - `[[slug]]` → `[slug](slug.md)`
  - `[[slug#Heading]]` → `[Heading](slug.md#heading-slug)`
  - `[[slug|Alias]]` → `[Alias](slug.md)`
  - `[[slug#Heading|Alias]]` → `[Alias](slug.md#heading-slug)`
  - Link text = alias ∨ heading ∨ slug. Anchor = `slugify_heading(heading)`.
- **Wired into the write handlers.** `wiki_write_page` and `wiki_update_page`
  run `to_markdown_links` on the incoming body/`new_body` before validation and
  persistence, so what is stored (and re-indexed) already carries markdown links.
- **Lazy migration falls out for free.** A whole new page (`wiki_write_page`) is
  normalized in full; `wiki_update_page` normalizes the one `##` section it
  rewrites, so existing pages migrate section-by-section as they are edited. No
  page is force-touched.
- **Idempotent.** A body with no `[[...]]` is returned unchanged; markdown links
  are never rewritten, so re-running is a no-op.
- **Transactional write is preserved.** Normalization only shapes the body text;
  the existing validate → write → ingest-log → re-index flow and its rollback
  cover it unchanged.

## `lint.py` and `related.py`

- **`related.py` — unchanged.** The parser contract (`"slug#heading-slug"`,
  slug-first) is preserved; `_graph_neighbours` needs no edit.
- **`lint.py` — minimal.** The broken-ref loop now covers markdown links via the
  dual-read parser. The `#heading` existence check slugifies the target's
  heading set (`slugify_heading`) so it compares slug-to-slug. Add an advisory
  `legacy_wikilink` finding listing pages that still contain `[[...]]` outside
  code — a **lazy-migration progress indicator** ("not yet edited"), not a broken
  finding.

## Authoring rules, docs, versioning

- `resources.py` (`iwiki://authoring-rules`, line 19): change the cross-link rule
  from `[[slug#Heading]]` to `` `[Heading](slug.md#heading)` `` (within the same
  domain in v1).
- `server.py`: update any tool description mentioning `[[...]]` to the markdown
  form.
- `README.md` + `docs/README.ru.md`: if link syntax is documented, update both
  (EN + RU, kept in sync).
- `pyproject.toml`: **patch** bump (no new tool; changed write behaviour + parser
  — repo default is patch). This branch is cut from `master` (`0.1.x`); if the
  OKF branch (which bumps to `0.2.0`) merges first, reconcile the version line at
  merge time.

## Testing

Repo pattern: monkeypatch `indexer.embed_texts`, dummy `IWIKI_*` env, no network.

- **Parser:** markdown `.md` links parsed; images / external / absolute /
  same-page-anchor / non-`.md` rejected; `.md` suffix and `./` stripped; heading
  slugified; code (fenced + inline) ignored; legacy `[[...]]` still parsed;
  de-dup and order preserved.
- **`slugify_heading`:** determinism; punctuation stripped; whitespace collapsed;
  the two consumers agree on the same input.
- **`to_markdown_links`:** all four `[[...]]` forms rewritten with correct text
  and anchor; `[[ $# ]]` inside a bash fence untouched; a markdown-link example
  inside a fence untouched; idempotent on already-markdown bodies.
- **Write handlers:** `wiki_write_page` normalizes a `[[...]]` body before
  persist; `wiki_update_page` normalizes the edited section; the stored file and
  index carry markdown links.
- **`lint`:** a broken markdown link is flagged; a valid `#anchor` matches via
  slug; `legacy_wikilink` lists a page with `[[...]]` and omits fully-migrated
  pages.
- **`related`:** regression — graph neighbours identical for a page whether its
  edges are `[[...]]` or markdown links.

## Files touched

- Modified: `src/iwiki_mcp/engine/links.py` (dual-read parser +
  `slugify_heading` + `to_markdown_links`), `src/iwiki_mcp/engine/lint.py`
  (slugified heading check + `legacy_wikilink`), `src/iwiki_mcp/server.py`
  (wire normalization into write/update + tool descriptions),
  `src/iwiki_mcp/resources.py`, `pyproject.toml`, `README.md`,
  `docs/README.ru.md`
- Unchanged (verified): `src/iwiki_mcp/engine/related.py`
- Tests: extend `tests/engine/test_links.py`; add write-normalization and lint
  cases following existing patterns.

## Out of scope

- **OKF export (`export.py`).** Not written yet. When built, no `[[...]]`→markdown
  conversion is needed — sources are already CommonMark. Recorded as a future
  simplification.
- **Code-affinity edges (#2)** and **code graph (#3)** — separate specs.

## Risks and mitigations

- **Phantom edges from code samples** — mitigated by `_strip_code` before both
  parser and normalization; masking preserves code verbatim on rewrite.
- **Heading-anchor mismatch** (`Related sections` vs `related-sections`) — one
  `slugify_heading` targeting GitHub's algorithm is the shared normalization
  point for parser, rewrite, and lint; the only residual gap is GitHub's `-N`
  de-duplication of repeated headings (rare under `##`-only structure, deferred).
- **Incomplete graph during transition** — mitigated by dual-read; the parser
  reads legacy `[[...]]` until a page is edited. `legacy_wikilink` surfaces
  remaining pages.
- **Malformed markdown** (nested brackets in link text) — the regex targets the
  simple `[text](target)` iwiki actually writes; reference-style and exotic forms
  are explicitly unsupported and documented.
