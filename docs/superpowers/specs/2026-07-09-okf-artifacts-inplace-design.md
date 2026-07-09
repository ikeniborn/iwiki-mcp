---
review:
  stage: spec
  spec_hash: 4475f4bf6a553619
  last_run: 2026-07-09
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
      phase: coverage
      severity: WARNING
      section: "## Mechanism — batch sweep (`wiki_export_okf(domain=None)`)"
      section_hash: a5d28d23c9d18be9
      fragment: "Regenerates index.md + log.md via refresh_artifacts. ... Re-indexes the domain and commits (all changes in one commit)."
      text: >-
        Internal ordering contradiction. The Call site rule states refresh_artifacts runs
        "after the re-index, before the auto-commit"; the batch-sweep sequence lists refresh
        (step 3) before re-index+commit (step 4). So inside wiki_export_okf refresh runs
        before, not after, the re-index — the two statements disagree.
      fix: >-
        Reconcile the orderings: either state the batch sweep re-indexes before its final
        refresh, or note the order is immaterial because reserved files are excluded from
        the index.
      verdict: fixed
      verdict_at: 2026-07-09
      resolution: >-
        Batch-sweep steps reordered — step 3 now re-indexes, step 4 regenerates via
        refresh_artifacts "after the re-index" then commits, matching the write-path
        call-site rule. Contradiction gone.
    - id: F-002
      phase: clarity
      severity: WARNING
      section: "## Reserved files, exclusion, and guards"
      section_hash: 751b65dcaaa310fb
      fragment: "extend the one shared page-enumeration/skip path (the place that already skips .iwiki/, alongside ignore.py) with RESERVED_OKF, so the exclusion is defined once."
      text: >-
        "Exclusion defined once" in a single shared skip path is in tension with Files
        touched, which lists indexer.py / validate.py / lint.py / retrieval.py each modified
        to exclude RESERVED_OKF. validate.py/lint.py are stdlib-only and apply a name check
        rather than a directory page-walk, so one shared skip path may not actually reach all
        four call sites.
      fix: >-
        Clarify whether RESERVED_OKF is a single shared constant applied at four sites or a
        genuine shared skip function all four route through; name the exact exclusion locus
        per module.
      verdict: fixed
      verdict_at: 2026-07-09
      resolution: >-
        Section rewritten: RESERVED_OKF is one shared constant; exclusion enforced only at
        page enumeration (indexer/retrieval page-walk + listing), lint's content map is built
        from the filtered set, and validate.py/lint.py stay stdlib-only and UNMODIFIED. Files
        touched updated to drop them. Locus is now unambiguous.
    - id: F-003
      phase: clarity
      severity: INFO
      section: "## Authoring rules, README, versioning"
      section_hash: c7cdba74acdbbdd5
      fragment: "`pyproject.toml`: **patch** bump, `0.2.3` → `0.2.4`."
      text: >-
        The resources.py authoring-rules note, README/README.ru updates, and the version
        bump have no explicit acceptance criterion in Testing/Verification (inspection-only).
      fix: >-
        Optionally add an inspection check to Verification (version == 0.2.4; authoring-rules
        mention the reserved slugs), or accept these as inspection-verifiable.
      verdict: fixed
      verdict_at: 2026-07-09
      resolution: >-
        Verification gained an Inspection bullet: pyproject version is 0.2.4, authoring-rules
        mention the reserved index/log slugs, and README (EN+RU) describes the no-dest sweep.
        Acceptance criteria now explicit.
---
# OKF Artifacts In-Place — Design

**Date:** 2026-07-09
**Status:** Approved (brainstorming)
**Topic:** `okf-artifacts-inplace`
**Supersedes:** the "Export bundle — `wiki_export_okf(domain, dest)`" section of
`2026-07-09-okf-frontmatter-adoption-design.md` (on-demand copy-to-dest export).

## Overview

Repurpose the on-demand OKF export into **deterministic, in-place maintenance**
of OKF artifacts. Two mechanisms, one goal — the domain directory is always both
the iwiki vector base (`.iwiki/`) and a live, fully-conformant OKF bundle:

1. **Incremental (write path).** Every mutating handler refreshes the reserved
   `index.md` / `log.md` in the domain directory, so steady-state stays fresh
   with no manual step. (Per-page frontmatter + markdown links are already
   written in-place on every write by the `okf-frontmatter-adoption` work.)
2. **Batch (`wiki_export_okf`).** The existing tool is repurposed from an
   on-demand copy-to-`dest` into a **whole-domain, in-place conformance sweep**:
   it fixes frontmatter and `[[...]]` links across all pages at once and
   regenerates the reserved files. This is the repair / first-adoption path.

An external OKF consumer reads the git-synced domain directory directly. There
is no separate copy artifact.

Motivation: with frontmatter and markdown links already written into source on
every write, the only OKF outputs still missing from the live domain are the
reserved `index.md` / `log.md` files, which today exist only inside an on-demand
`wiki_export_okf(domain, dest)` copy. That copy is a lossy documentation
snapshot — it cannot carry the vector index, so it never reproduced the server's
search. The functional-portability path is already git (`.iwiki/` travels with
clone/sync). So the copy's value reduces to the reserved files, and its
`convert_wikilinks` / `_git_date` / `_derive_meta` re-implement logic the write
path now owns (`links.to_markdown_links`, `okf.git_last_commit_date`,
`okf.build_frontmatter`). This design moves reserved-file generation in-place and
incremental, deletes the duplicated `export.py` logic, and re-expresses the batch
sweep on top of the existing helpers.

## Goals and non-goals

**Goals**
- `index.md` / `log.md` maintained **in the domain directory**, refreshed
  deterministically at the end of every successful mutating handler.
- The domain directory is always a valid OKF bundle.
- `wiki_export_okf` repurposed into `wiki_export_okf(domain=None)` — an
  in-place, whole-domain conformance sweep (bulk-fix frontmatter + links) plus
  reserved-file regeneration; **no `dest`**.
- Kill `export.py`'s duplication of write-path logic (the batch sweep reuses the
  existing helpers instead).

**Non-goals**
- No `dest` copy / detached bundle (no consumer; the domain dir is the bundle).
- Not moving vectors out of `.iwiki/`; not renaming `.iwiki/index.jsonl` /
  `.iwiki/log.jsonl` (kept internal, as before).
- Not changing body rules, chunking, or retrieval semantics.
- The sweep is **deterministic** — it never calls the chat model. It guarantees
  frontmatter *presence* (defaulting `type: concept`, empty `tags`) and converts
  links; it does **not** re-classify `type` / `tags`. Smart classification stays
  `wiki_migrate_okf` (see the relationship note below).
- Not a store-format change (JSONL → SQLite/sqlite-vec is a separate topic).

## Mechanism — incremental (write path)

### Pure generators (engine, stdlib-only)

New module `src/iwiki_mcp/engine/okf_artifacts.py`, stdlib-only and
deterministic (same domain state → identical bytes):

- `render_index(slugs: list[str]) -> str` — the OKF `index.md`: a heading plus a
  sorted markdown-link list of page slugs (reserved files excluded).
- `render_log(records: list[dict]) -> str` — the OKF `log.md`: a heading plus one
  line per ingest-log record (`date`, `op`, `page`).
- `RESERVED_OKF = ("index.md", "log.md")` — the single reserved-name constant.

These are the two useful generators relocated from the deleted `export.py`; the
frontmatter/link logic is **not** relocated (it lives in `links` / `okf` /
`frontmatter` already).

### Top-layer orchestration (`okf.py`)

- `refresh_artifacts(base, domain) -> str | None` — walk the domain's pages
  (excluding `RESERVED_OKF` and `.iwiki/`), read `.iwiki/log.jsonl`, write
  `index.md` + `log.md` into the domain root via the pure generators. Returns a
  `warning` string (e.g. an authored page colliding with a reserved name) or
  `None`. Never raises.

### Call site (`server.py`)

`refresh_artifacts` is called at the end of every successful mutating handler —
`wiki_write_page`, `wiki_update_page`, `wiki_delete_page`, `wiki_apply_okf`,
`wiki_migrate_okf`, and `wiki_export_okf` itself — **after** the re-index,
**before** the auto-commit, so a single commit captures
`page(s) + .iwiki/* + index.md + log.md` as one consistent snapshot. Any returned
`warning` is threaded onto the handler result.

Best-effort: a failure inside `refresh_artifacts` becomes a `warning` on the
result and never rolls back the page write. The reserved files are derived and
repairable with `wiki_export_okf`; page content is the source of truth.

## Mechanism — batch sweep (`wiki_export_okf(domain=None)`)

Name retained by explicit choice. `dest` dropped. Pure serialization, **no
LLM**. Mirrors `wiki_migrate_okf(domain=None)` in shape. It:

1. **Fixes links in bulk, in-place.** For each page, converts residual `[[...]]`
   to CommonMark links via the existing `links.to_markdown_links` (deterministic,
   idempotent). Writes back only when the body changed.
2. **Ensures frontmatter in bulk, in-place.** For each page lacking a
   frontmatter block, derives the deterministic fields (title / description /
   timestamp / resource) via the existing `okf` / `frontmatter` derivation and
   writes a block with `type: concept`, empty `tags`. Pages that already have
   frontmatter keep their `type` / `tags` untouched (only normalized/deduped);
   nothing is re-classified.
3. **Re-indexes** the domain (the pages it just modified).
4. **Regenerates** `index.md` + `log.md` via `refresh_artifacts` — after the
   re-index, matching the write-path call-site rule — then commits all changes in
   one commit.
5. **Returns** a report: `fixed_links`, `added_frontmatter`, `artifacts`,
   `warnings`, and a `next_steps` hint pointing pages still defaulted to
   `type: concept` at `wiki_migrate_okf` for better classification.

Idempotent: a second run over a clean domain changes nothing.

Use cases: first-time adoption, and repair after out-of-band edits (markdown
changed on disk without a tool) or a sync conflict in the derived files.

### Relationship to `wiki_migrate_okf`

They compose, they do not duplicate:
- `wiki_export_okf` — **deterministic guarantee**. No LLM. Ensures every page has
  *some* conformant frontmatter (`type: concept` fallback) and markdown links.
  Never overwrites an existing `type` / `tags`.
- `wiki_migrate_okf` — **quality classification**. Optional chat model (or agent
  plan mode) assigns meaningful `type` / `tags`. Improves on the `concept`
  default.

So the workflow is: `wiki_export_okf` to make the whole domain conformant now,
`wiki_migrate_okf` when you want better `type` / `tags` than the deterministic
default.

## Reserved files, exclusion, and guards

### Exclusion from the wiki surface

The generated reserved files are artifacts, not authored pages, and must never
appear in the indexed/searchable surface. `RESERVED_OKF` is one shared constant
(in `engine/okf_artifacts.py`); exclusion is enforced where pages are
**enumerated**, not inside the stdlib engine checkers:

- The indexer / retrieval page-walk and the domain page listing route through a
  single shared skip that consults `RESERVED_OKF` — the same place that already
  skips `.iwiki/`, alongside `ignore.py`. This keeps reserved files out of
  chunking, embedding, faceted search, and listings.
- `lint`'s content map is built from that already-filtered page set, so `lint.py`
  never sees a reserved file and stays stdlib-only (**unmodified**).
- `validate.py` is unaffected: it runs per-page on write, and reserved files are
  never written through the authoring path (the write guard blocks the slugs; the
  files are produced only by `refresh_artifacts`).

### Write-time collision guard

`wiki_write_page` rejects a slug of `index` or `log` (it would collide with a
generated file) before any filesystem access, returning `{error, hint}` — the
same shape as the other path guards.

### Pre-existing authored `index` / `log`

If a domain already contains an authored `index.md` / `log.md`,
`refresh_artifacts` / `wiki_export_okf` must **not** silently clobber it: it
skips generation for that name and emits a `warning` naming the collision. Rare;
surfaced, never destructive.

## Transaction and sync

- Artifacts (and, for the batch sweep, the fixed pages) join the same auto-commit
  as the triggering operation (consistent snapshot; no separate commit).
- `index.md` / `log.md` are deterministic, so a `wiki_sync` merge conflict in
  these derived files is resolved by **regenerate-wins**: run `wiki_export_okf`
  to overwrite them from the merged state. Documented as the recovery step; no
  bespoke merge logic is added. This mirrors the existing "run `wiki_index` to
  rebuild after out-of-band edits" idiom.

## Authoring rules, README, versioning

- `resources.py` (`iwiki://authoring-rules`): note the reserved `index` / `log`
  slugs and that `index.md` / `log.md` are generated OKF artifacts.
- `README.md` + `docs/README.ru.md` (kept in sync, EN + RU): update the
  `wiki_export_okf` row — it is now an in-place, whole-domain conformance sweep
  (no `dest`); document that OKF artifacts are maintained in-place on every write
  and that the domain directory is the bundle.
- `pyproject.toml`: **patch** bump, `0.2.3` → `0.2.4`.

## Files touched

- **New:** `src/iwiki_mcp/engine/okf_artifacts.py` (pure `render_index` /
  `render_log`, `RESERVED_OKF`).
- **Deleted:** `src/iwiki_mcp/export.py` (duplicated logic dropped; the two
  generators relocated; the batch sweep re-expressed on existing helpers).
- **Modified:** `src/iwiki_mcp/okf.py` (`refresh_artifacts`; the batch-sweep
  helper), `server.py` (call `refresh_artifacts` in every mutating handler;
  rewrite `wiki_export_okf` as the in-place sweep; reserved-slug write guard;
  build `lint`'s content map from the filtered page set), the shared
  page-walk / skip path used by `indexer.py` and `retrieval.py` (exclude
  `RESERVED_OKF` via the shared constant), `resources.py`, `README.md`,
  `docs/README.ru.md`, `pyproject.toml`. (`validate.py` and `lint.py` stay
  stdlib-only and unmodified — they receive already-filtered input.)
- **Rewritten test:** `tests/test_export_okf.py` (same name; now covers the
  in-place sweep + reserved-file maintenance instead of copy-to-dest).

## Testing

Follow the repo pattern: monkeypatch `indexer.embed_texts`, dummy `IWIKI_*` env,
no network.

- `render_index` / `render_log`: deterministic bytes for a given state; reserved
  files excluded from `index.md`; empty domain / empty log handled.
- `refresh_artifacts`: writes both files into the domain root; returns a warning
  on a reserved-name collision; never raises.
- Write path: `wiki_write_page` / `wiki_update_page` / `wiki_delete_page` leave
  `index.md` / `log.md` fresh and staged in the same commit as the page.
- Batch sweep `wiki_export_okf`: converts residual `[[...]]` in-place; adds
  frontmatter (`type: concept`) to a page missing it; preserves an existing
  page's `type` / `tags`; regenerates both reserved files; idempotent on a second
  run; report lists `fixed_links` / `added_frontmatter`; `next_steps` points at
  `wiki_migrate_okf`.
- Exclusion: `index.md` / `log.md` are not indexed, not returned by search, and
  produce no `validate` / `lint` findings.
- Write guard: `wiki_write_page` rejects slug `index` / `log` with `{error,
  hint}`; no file/log/index side effects.
- Sync recovery: after a simulated conflict, `wiki_export_okf` regenerates the
  derived files from current state.

## Verification

- `uv run pytest -q` green; `uv run flake8 src tests` clean.
- `grep -rn 'export_domain\|convert_wikilinks' src tests` returns nothing (the
  duplicated logic is gone; `wiki_export_okf` remains, repurposed).
- Inspection: `pyproject.toml` version is `0.2.4`; `iwiki://authoring-rules`
  mentions the reserved `index` / `log` slugs; `README.md` + `docs/README.ru.md`
  describe the in-place sweep with no `dest`.
- Manual: write a page, confirm `index.md` / `log.md` appear and update in the
  domain directory and are absent from `wiki_search` results; run
  `wiki_export_okf` on a domain with a legacy page and confirm it gains
  frontmatter + markdown links in place.

## Risks and mitigations

- **Reserved files polluting search** — mitigated by the single-source
  `RESERVED_OKF` exclusion in the shared page-walk; asserted by tests.
- **Batch sweep mutating good pages** — the sweep is idempotent, preserves
  existing `type` / `tags`, and writes back only changed bodies; a clean domain
  is a no-op.
- **Name collision with an authored `index` / `log`** — write-guard rejects new
  ones; the sweep warns and skips for pre-existing ones (never clobbers).
- **Derived-file merge conflicts on sync** — deterministic regeneration;
  documented `wiki_export_okf` recovery; no custom merge logic.
- **Standing write-path cost for a still-hypothetical consumer** — accepted: the
  incremental refresh is a domain walk plus two small files per write,
  deterministic and best-effort; it removes the lossy separate-export wart.
  Revisit only if the cost shows up in practice.
