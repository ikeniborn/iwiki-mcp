---
review:
  plan_hash: 86ba7a0db3721e59
  last_run: 2026-07-19
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings:
    - id: F-001
      phase: coverage
      severity: CRITICAL
      section: "Task 4: Reuse materialized pages during hydration and reranking"
      section_hash: 0ce00e02ec1b2013
      fragment: "page = _materialize_page(cfg, base, domain, candidate[\"file\"], page_cache)"
      text: "Hydration trusts a cached page without checking whether the source file changed after candidate preparation, weakening the existing stale-chunk guard."
      fix: "Store a file fingerprint during materialization, reject a page changed during its read, revalidate the fingerprint once before cached hydration, and add a mutation-between-stages test."
      verdict: fixed
      verdict_at: 2026-07-19
    - id: F-002
      phase: verifiability
      severity: CRITICAL
      section: "Task 6: Run complete verification and close implementation evidence"
      section_hash: 64994aeb740a2d21
      fragment: "$check-chain result docs/superpowers/plans/2026-07-18-lexical-retrieval-chunk-scoring.md"
      text: "The plan commits all implementation work before result reconciliation, so check-chain result would inspect an empty git diff and could not reconcile the implementation."
      fix: "Invoke check-chain result with --since=master so it reviews the complete committed branch diff."
      verdict: fixed
      verdict_at: 2026-07-19
    - id: F-003
      phase: verifiability
      severity: WARNING
      section: "Task 4: Reuse materialized pages during hydration and reranking"
      section_hash: 0ce00e02ec1b2013
      fragment: "Update existing hydrate_candidates monkeypatch functions"
      text: "The plan names required test-double edits but does not provide the exact three replacements, contrary to the writing-plans no-placeholder contract."
      fix: "List each affected test and provide its complete replacement lambda signature with page_cache or kwargs."
      verdict: fixed
      verdict_at: 2026-07-19
    - id: F-004
      phase: dependencies
      severity: CRITICAL
      section: "Task 6: Run complete verification and close implementation evidence"
      section_hash: 64994aeb740a2d21
      fragment: "git commit -m \"docs(plan): record lexical chunk scoring execution\""
      text: "Execution checkbox updates change the plan body after its validated plan_hash, but the plan goes directly to result reconciliation with stale plan-stage validation."
      fix: "After committing checkbox evidence, rerun check-chain plan on this file, commit the refreshed frontmatter/TODO state, then run check-chain result with --since=master."
      verdict: fixed
      verdict_at: 2026-07-19
chain:
  intent: docs/superpowers/intents/2026-07-18-lexical-retrieval-chunk-scoring-intent.md
  spec: docs/superpowers/specs/2026-07-18-lexical-retrieval-chunk-scoring-design.md
---

# Lexical Retrieval Chunk Scoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox
> (`- [ ]`) syntax for tracking.

**Goal:** Make direct lexical retrieval score and hydrate exact hash-verified indexed
chunks while preserving repeated H2 sections with collision-free chunk identities.

**Architecture:** Canonical `chunk_markdown` remains the only windowing implementation.
Retrieval materializes each eligible page once per request, uses whole-H2 term frequency
only for unchanged page seeds, and uses a pure chunk scorer for verified direct lexical
signals. Repeated exact headings continue their chunk counter across occurrences; a
normal domain reindex migrates identities without a schema bump.

**Tech Stack:** Python 3.10+, pytest, pytest-asyncio, stdlib dataclasses/pathlib/regex,
existing JSONL `VectorStore`, existing RRF fusion, iwiki MCP documentation tools.

---

## Source documents

- Intent:
  `docs/superpowers/intents/2026-07-18-lexical-retrieval-chunk-scoring-intent.md`
- Design:
  `docs/superpowers/specs/2026-07-18-lexical-retrieval-chunk-scoring-design.md`
- Current retrieval docs: iwiki domain `iwiki-mcp`, pages `indexing` and `retrieval`

## File map

- Modify `src/iwiki_mcp/engine/chunk.py`: collision-free per-heading chunk numbering.
- Modify `src/iwiki_mcp/indexer.py`: refresh reused-record `ordinal`.
- Modify `src/iwiki_mcp/engine/grep.py`: pure whole-section and chunk scorers plus the
  existing filesystem adapter.
- Modify `src/iwiki_mcp/retrieval.py`: request-local materialization, collision-safe
  indexed identities, exact lexical mapping, shared hydration cache, compatibility
  wrapper.
- Modify `src/iwiki_mcp/server.py`: create and thread one request-local page cache.
- Modify `tests/engine/test_chunk.py`: repeated-heading numbering and preservation.
- Modify `tests/test_indexer.py`: incremental migration and positional metadata reuse.
- Modify `tests/test_grep.py`: pure scorer behavior and deterministic ordering.
- Modify `tests/test_retrieval.py`: exact late-window matches, stale/collision safety,
  unchanged page-seed behavior, compatibility wrapper.
- Modify `tests/test_server_search.py`: shared preparation/hydration materialization and
  updated test doubles.
- Modify `tests/test_server_search_facets.py` and `tests/test_robustness_fixes.py`:
  preparation test doubles accept the new internal cache keyword.
- Update iwiki pages `indexing` and `retrieval` through MCP tools after behavior ships.
- Verify existing version `0.7.3` in `pyproject.toml`, `src/iwiki_mcp/__init__.py`, and
  `uv.lock`; do not bump again within this topic.

## Requirement coverage

| Requirements | Plan tasks |
|---|---|
| R1–R3 repeated headings and reuse metadata | Task 1 |
| R5 pure scorers | Task 2 |
| R4, R6–R7 exact materialization and unchanged seeds/fusion | Task 3 |
| R8 shared hydration cache and freshness validation | Task 4 |
| R9 lexical compatibility path | Task 3 |
| R10 migration evidence | Tasks 1 and 5 |
| R11 documentation | Task 5 |
| Full acceptance and health metrics | Task 6 |

### Task 1: Make repeated-heading chunk identities collision-free

**Files:**

- Modify: `tests/engine/test_chunk.py`
- Modify: `tests/test_indexer.py`
- Modify: `src/iwiki_mcp/engine/chunk.py`
- Modify: `src/iwiki_mcp/indexer.py`

- [x] **Step 1: Add failing repeated-heading chunk tests**

Append these tests to `tests/engine/test_chunk.py`:

```python
def test_repeated_headings_continue_chunk_numbers_without_deduplication():
    md = (
        "## Setup\none two three four\n"
        "## Other\nmiddle\n"
        "## Setup\nfive six seven eight\n"
    )

    chunks = chunk_markdown("f.md", md, size=2, overlap=0)
    setup = [chunk for chunk in chunks if chunk.heading == "Setup"]

    assert [(chunk.chunk, chunk.ordinal) for chunk in setup] == [
        (0, 0), (1, 0), (2, 2), (3, 2)
    ]
    assert [chunk.text for chunk in setup] == [
        "## Setup\none two",
        "## Setup\nthree four",
        "## Setup\nfive six",
        "## Setup\nseven eight",
    ]


def test_identical_repeated_sections_remain_distinct_chunks():
    md = "## Setup\nsame body\n## Setup\nsame body\n"

    chunks = chunk_markdown("f.md", md, size=512, overlap=64)

    assert [(chunk.heading, chunk.chunk, chunk.ordinal) for chunk in chunks] == [
        ("Setup", 0, 0),
        ("Setup", 1, 1),
    ]
    assert chunks[0].hash == chunks[1].hash
```

- [x] **Step 2: Run the chunk tests and verify RED**

Run:

```bash
uv run pytest -q tests/engine/test_chunk.py::test_repeated_headings_continue_chunk_numbers_without_deduplication tests/engine/test_chunk.py::test_identical_repeated_sections_remain_distinct_chunks
```

Expected: both tests fail because the second `Setup` occurrence currently restarts at
`chunk=0`.

- [x] **Step 3: Implement per-heading continuous numbering**

In `src/iwiki_mcp/engine/chunk.py`, update the `Chunk.chunk` comment and replace the
section loop with:

```python
    chunk_offsets: dict[str, int] = {}
    for ordinal, (heading, body) in enumerate(secs):
        prefix = f"## {heading}"
        pieces = _split_section(body.split(), size, overlap)
        first_chunk = chunk_offsets.get(heading, 0)
        for offset, piece in enumerate(pieces):
            chunk_index = first_chunk + offset
            text = prefix + "\n" + " ".join(piece)
            out.append(Chunk(file=file, heading=heading, chunk=chunk_index, text=text,
                             hash=_hash(text), type=ptype, tags=list(ptags),
                             kind="section", ordinal=ordinal))
        chunk_offsets[heading] = first_chunk + len(pieces)
```

Change the dataclass comment to:

```python
    chunk: int           # window index within the same heading across the page
```

- [x] **Step 4: Run all chunk tests and verify GREEN**

Run:

```bash
uv run pytest -q tests/engine/test_chunk.py tests/test_chunk_frontmatter.py
```

Expected: all tests pass; unique-heading tests retain `0..N` numbering.

- [x] **Step 5: Add failing incremental reindex tests**

Append to `tests/test_indexer.py`:

```python
def test_reindex_migrates_repeated_heading_identity_without_schema_bump(
        tmp_path, monkeypatch):
    b = tmp_path / "wiki"
    domain = b / "backend"
    domain.mkdir(parents=True)
    content = "# Page\n\n## Setup\nfirst body\n\n## Other\nstable\n\n## Setup\nsecond body\n"
    (domain / "page.md").write_text(content, encoding="utf-8")
    cfg = _cfg()
    current = [
        chunk for chunk in __import__(
            "iwiki_mcp.engine.chunk", fromlist=["chunk_markdown"]
        ).chunk_markdown(
            "page.md", content, cfg.chunk_size, cfg.chunk_overlap, cfg.summary_max
        ) if chunk.kind == "section"
    ]
    first, other, second = current
    old_second = store.make_record(second, [0.0, 1.0])
    old_second.chunk = 0
    old_first = store.make_record(first, [1.0, 0.0])
    old_first.chunk = 0
    stable = store.make_record(other, [1.0, 0.0])
    store.save_index(
        base.index_path(str(b), "backend"),
        [old_first, stable, old_second],
    )
    embedded = []

    def fake_embed(cfg, texts):
        embedded.extend(texts)
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(indexer, "embed_texts", fake_embed)

    stats = indexer.index_domain(cfg, str(b), "backend")
    recs = store.load_index(base.index_path(str(b), "backend"))
    setup = [rec for rec in recs if rec.heading == "Setup"]

    assert [(rec.chunk, rec.ordinal) for rec in setup] == [(0, 0), (1, 2)]
    assert len(set((rec.file, rec.heading, rec.chunk) for rec in recs)) == len(recs)
    assert stats["reused"] == 1
    assert stats["embedded"] == 2
    assert embedded == ["## Setup\nfirst body", "## Setup\nsecond body"]
    assert all(rec.v == store.SCHEMA_VERSION for rec in recs)


def test_reused_record_refreshes_current_ordinal(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
    domain = b / "backend"
    domain.mkdir(parents=True)
    content = "# Page\n\n## Other\nfirst\n\n## Stable\nsame body\n"
    (domain / "page.md").write_text(content, encoding="utf-8")
    cfg = _cfg()
    chunks = __import__(
        "iwiki_mcp.engine.chunk", fromlist=["chunk_markdown"]
    ).chunk_markdown("page.md", content, cfg.chunk_size, cfg.chunk_overlap, cfg.summary_max)
    stable_chunk = next(chunk for chunk in chunks if chunk.heading == "Stable")
    old = store.make_record(stable_chunk, [1.0, 0.0])
    old.ordinal = 99
    store.save_index(base.index_path(str(b), "backend"), [old])
    monkeypatch.setattr(
        indexer, "embed_texts", lambda cfg, texts: [[1.0, 0.0] for _ in texts]
    )

    indexer.index_domain(cfg, str(b), "backend")

    recs = store.load_index(base.index_path(str(b), "backend"))
    stable = next(rec for rec in recs if rec.heading == "Stable")
    assert stable.ordinal == 1
```

- [x] **Step 6: Run the indexer tests and verify RED**

Run:

```bash
uv run pytest -q tests/test_indexer.py::test_reindex_migrates_repeated_heading_identity_without_schema_bump tests/test_indexer.py::test_reused_record_refreshes_current_ordinal
```

Expected: the migration test passes using the Task 1 chunk numbering, while
`test_reused_record_refreshes_current_ordinal` fails with `99`.

- [x] **Step 7: Refresh ordinal on record reuse**

In `src/iwiki_mcp/indexer.py`, extend the reuse branch:

```python
            prev.type = c.type
            prev.tags = list(c.tags)
            prev.ordinal = c.ordinal
            fresh.append(prev)
```

- [x] **Step 8: Run focused indexing tests and verify GREEN**

Run:

```bash
uv run pytest -q tests/engine/test_chunk.py tests/test_chunk_frontmatter.py tests/test_indexer.py
```

Expected: all focused chunk and indexer tests pass.

- [x] **Step 9: Commit Task 1**

```bash
git add src/iwiki_mcp/engine/chunk.py src/iwiki_mcp/indexer.py tests/engine/test_chunk.py tests/test_indexer.py
git commit -m "fix(indexing): make repeated heading chunks unique"
```

### Task 2: Add pure whole-section and canonical-chunk lexical scorers

**Files:**

- Modify: `tests/test_grep.py`
- Modify: `src/iwiki_mcp/engine/grep.py`

- [x] **Step 1: Add failing pure scorer tests**

Update the import and append tests in `tests/test_grep.py`:

```python
from iwiki_mcp.engine.chunk import chunk_markdown
from iwiki_mcp.engine.grep import grep_sections, score_chunks, score_sections


def test_score_sections_preserves_whole_h2_term_frequency():
    markdown = "## One\nneedle\n## Two\nneedle needle\n"

    hits = score_sections("page.md", markdown, "needle")

    assert [(hit["heading"], hit["score"]) for hit in hits] == [
        ("Two", 2), ("One", 1)
    ]


def test_score_chunks_returns_exact_late_window_only():
    chunks = chunk_markdown(
        "page.md",
        "## Long\none two three needle five six\n",
        size=3,
        overlap=0,
    )

    hits = score_chunks(chunks, "needle", top_k=None)

    assert [(hit["heading"], hit["chunk"], hit["score"]) for hit in hits] == [
        ("Long", 1, 1)
    ]


def test_score_chunks_orders_ties_by_file_heading_and_chunk():
    chunks = (
        chunk_markdown("b.md", "## Same\nneedle needle\n", 1, 0)
        + chunk_markdown("a.md", "## Zulu\nneedle\n## Alpha\nneedle\n", 1, 0)
    )

    hits = score_chunks(chunks, "needle", top_k=None)

    assert [(hit["file"], hit["heading"], hit["chunk"]) for hit in hits] == [
        ("a.md", "Alpha", 0),
        ("a.md", "Zulu", 0),
        ("b.md", "Same", 0),
        ("b.md", "Same", 1),
    ]
```

- [x] **Step 2: Run new grep tests and verify RED**

Run:

```bash
uv run pytest -q tests/test_grep.py::test_score_sections_preserves_whole_h2_term_frequency tests/test_grep.py::test_score_chunks_returns_exact_late_window_only tests/test_grep.py::test_score_chunks_orders_ties_by_file_heading_and_chunk
```

Expected: collection fails because `score_sections` and `score_chunks` do not exist.

- [x] **Step 3: Implement the pure scorers**

In `src/iwiki_mcp/engine/grep.py`, add:

```python
from .chunk import Chunk


def _score(terms: list[str], haystack: str) -> int:
    hay = haystack.lower()
    return sum(hay.count(term) for term in terms)


def _ordered(hits: list[dict], top_k: int | None) -> list[dict]:
    hits.sort(key=lambda hit: (
        -hit["score"], hit["file"], hit["heading"], hit["chunk"]
    ))
    return hits if top_k is None else hits[:top_k]


def score_sections(file: str, content: str, query: str) -> list[dict]:
    terms = _terms(query)
    if not terms:
        return []
    out = []
    matches = list(_H2.finditer(content))
    for index, match in enumerate(matches):
        heading = match.group(1).strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        score = _score(terms, heading + " " + content[match.end():end])
        if score > 0:
            out.append({
                "file": file,
                "heading": heading,
                "chunk": 0,
                "score": score,
                "hit": "lexical",
            })
    return _ordered(out, None)


def score_chunks(chunks: list[Chunk], query: str,
                 top_k: int | None) -> list[dict]:
    if top_k is not None and top_k <= 0:
        return []
    terms = _terms(query)
    if not terms:
        return []
    out = []
    for chunk in chunks:
        if chunk.kind != "section":
            continue
        score = _score(terms, chunk.text)
        if score > 0:
            out.append({
                "file": chunk.file,
                "heading": chunk.heading,
                "chunk": chunk.chunk,
                "score": score,
                "hit": "lexical",
            })
    return _ordered(out, top_k)
```

Refactor `grep_sections` so its file loop calls:

```python
        out.extend(score_sections(rel_path.as_posix(), content, query))
```

Remove its duplicated inline H2 scoring and return `_ordered(out, top_k)`. Keep reserved
OKF filtering and fail-soft file reads unchanged.

- [x] **Step 4: Run all grep tests and verify GREEN**

Run:

```bash
uv run pytest -q tests/test_grep.py
```

Expected: all existing filesystem-adapter tests and new pure-scorer tests pass.

- [x] **Step 5: Commit Task 2**

```bash
git add src/iwiki_mcp/engine/grep.py tests/test_grep.py
git commit -m "refactor(grep): add canonical chunk scoring"
```

### Task 3: Map direct lexical signals to unique hash-verified indexed chunks

**Files:**

- Modify: `tests/test_retrieval.py`
- Modify: `src/iwiki_mcp/retrieval.py`

- [x] **Step 1: Add a small long-page fixture helper**

Add to `tests/test_retrieval.py`:

```python
def _long_lexical_page(tmp_path, monkeypatch):
    base = tmp_path / "wiki"
    domain = base / "d"
    domain.mkdir(parents=True)
    (domain / "long.md").write_text(
        "---\ndescription: long page\ntags: [wanted]\n---\n# Long\n\n"
        "## Details\none two three needle five six\n",
        encoding="utf-8",
    )
    cfg = replace(_cfg(), chunk_size=3, chunk_overlap=0)
    monkeypatch.setattr(
        indexer, "embed_texts", lambda cfg, texts: [[1.0, 0.0] for _ in texts]
    )
    indexer.index_domain(cfg, str(base), "d")
    return cfg, str(base)
```

- [x] **Step 2: Add failing exact-direct-hit and stale/collision tests**

Append to `tests/test_retrieval.py`:

```python
def test_direct_lexical_signal_uses_exact_late_chunk(tmp_path, monkeypatch):
    cfg, base = _long_lexical_page(tmp_path, monkeypatch)

    signals = retrieval._domain_signals(
        cfg, base, "d", "needle", None, "lexical", 32, 0.0, None, None, {}
    )

    direct = signals["lexical_section"]
    assert [(hit["heading"], hit["chunk"]) for hit in direct] == [("Details", 1)]


def test_direct_lexical_signal_omits_changed_chunk_hash(tmp_path, monkeypatch):
    cfg, base = _long_lexical_page(tmp_path, monkeypatch)
    page = Path(base) / "d" / "long.md"
    page.write_text(
        "---\ndescription: long page\n---\n# Long\n\n"
        "## Details\none two three replacement five six\n",
        encoding="utf-8",
    )

    signals = retrieval._domain_signals(
        cfg, base, "d", "replacement", None, "lexical", 32, 0.0, None, None, {}
    )

    assert signals["lexical_section"] == []


def test_direct_lexical_signal_omits_ambiguous_old_index_identity(
        tmp_path, monkeypatch):
    cfg, base = _long_lexical_page(tmp_path, monkeypatch)
    path = wiki_base.index_path(base, "d")
    recs = store.load_index(path)
    section = next(rec for rec in recs if rec.kind == "section" and rec.chunk == 0)
    duplicate = replace(section, tags=["other"])
    store.save_index(path, recs + [duplicate])

    signals = retrieval._domain_signals(
        cfg, base, "d", "one", None, "lexical", 32, 0.0,
        None, ["wanted"], {},
    )

    assert signals["lexical_section"] == []


def test_direct_lexical_signal_selects_later_repeated_heading_occurrence(
        tmp_path, monkeypatch):
    base = tmp_path / "wiki"
    domain = base / "d"
    domain.mkdir(parents=True)
    (domain / "repeated.md").write_text(
        "---\ndescription: repeated\n---\n# Repeated\n\n"
        "## Setup\nfirst meaning\n\n"
        "## Setup\nsecond meaning contains needle\n",
        encoding="utf-8",
    )
    cfg = _cfg()
    monkeypatch.setattr(
        indexer, "embed_texts", lambda cfg, texts: [[1.0, 0.0] for _ in texts]
    )
    indexer.index_domain(cfg, str(base), "d")

    signals = retrieval._domain_signals(
        cfg, str(base), "d", "needle", None, "lexical", 32, 0.0, None, None, {}
    )

    direct = signals["lexical_section"]
    assert [(hit["heading"], hit["chunk"]) for hit in direct] == [("Setup", 1)]


def test_materialization_omits_page_changed_during_read(tmp_path, monkeypatch):
    base = tmp_path / "wiki"
    domain = base / "d"
    domain.mkdir(parents=True)
    (domain / "page.md").write_text("## Details\nneedle\n", encoding="utf-8")
    stamps = iter([
        (1, 2, 10, 100),
        (1, 2, 20, 200),
    ])
    monkeypatch.setattr(
        retrieval, "_file_stamp", lambda path: next(stamps), raising=False
    )
    cache = {}

    page = retrieval._materialize_page(
        _cfg(), str(base), "d", "page.md", cache
    )

    assert page is None
    assert cache[("d", "page.md")] is None
```

- [x] **Step 3: Add a failing unchanged-page-seed overlap test**

Append:

```python
def test_lexical_page_seed_uses_whole_section_score_not_overlap_count(
        tmp_path, monkeypatch):
    base = tmp_path / "wiki"
    domain = base / "d"
    domain.mkdir(parents=True)
    (domain / "overlap.md").write_text(
        "---\ndescription: overlap\n---\n# Overlap\n\n"
        "## Details\none needle two three four\n",
        encoding="utf-8",
    )
    (domain / "twice.md").write_text(
        "---\ndescription: twice\n---\n# Twice\n\n"
        "## Details\nneedle needle\n",
        encoding="utf-8",
    )
    cfg = replace(_cfg(), chunk_size=3, chunk_overlap=2, seed_top_k=1)
    monkeypatch.setattr(
        indexer, "embed_texts", lambda cfg, texts: [[1.0, 0.0] for _ in texts]
    )
    indexer.index_domain(cfg, str(base), "d")

    signals = retrieval._domain_signals(
        cfg, str(base), "d", "needle", None, "lexical", 32, 0.0, None, None, {}
    )

    seeded_files = {hit["file"] for hit in signals["lexical_page"]}
    assert seeded_files == {"twice.md"}
```

This proves overlap does not inflate `overlap.md` above the page containing two actual
source occurrences.

- [x] **Step 4: Run new retrieval tests and verify RED**

Run:

```bash
uv run pytest -q tests/test_retrieval.py::test_direct_lexical_signal_uses_exact_late_chunk tests/test_retrieval.py::test_direct_lexical_signal_omits_changed_chunk_hash tests/test_retrieval.py::test_direct_lexical_signal_omits_ambiguous_old_index_identity tests/test_retrieval.py::test_direct_lexical_signal_selects_later_repeated_heading_occurrence tests/test_retrieval.py::test_materialization_omits_page_changed_during_read tests/test_retrieval.py::test_lexical_page_seed_uses_whole_section_score_not_overlap_count
```

Expected: `_domain_signals` rejects the extra cache argument, `_materialize_page` is
absent, and current direct lexical mapping still selects only `chunk=0`, including for
the later repeated `Setup`.

- [x] **Step 5: Add request-local materialization primitives**

At the top of `src/iwiki_mcp/retrieval.py`, import:

```python
from dataclasses import dataclass

from .engine.chunk import Chunk, chunk_markdown
from .engine.grep import score_chunks, score_sections
```

Keep the existing `from pathlib import Path`. Replace the existing `chunk_markdown` and
`grep_sections` imports, then add:

```python
FileStamp = tuple[int, int, int, int]


@dataclass
class _MaterializedPage:
    path: Path
    stamp: FileStamp
    markdown: str
    chunks: dict[tuple[str, int], Chunk]


PageCache = dict[tuple[str, str], _MaterializedPage | None]


def _file_stamp(path: Path) -> FileStamp | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _materialize_page(cfg: Config, base: str, domain: str, file: str,
                      cache: PageCache) -> _MaterializedPage | None:
    key = domain, file
    if key in cache:
        return cache[key]
    path = _domain_file_path(base, domain, file)
    if path is None:
        cache[key] = None
        return None
    before = _file_stamp(path)
    if before is None:
        cache[key] = None
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            markdown = fh.read()
    except OSError:
        cache[key] = None
        return None
    after = _file_stamp(path)
    if after is None or before != after:
        cache[key] = None
        return None
    chunks = chunk_markdown(
        file, markdown, cfg.chunk_size, cfg.chunk_overlap, cfg.summary_max
    )
    page = _MaterializedPage(
        path=path,
        stamp=after,
        markdown=markdown,
        chunks={
            (chunk.heading, chunk.chunk): chunk
            for chunk in chunks if chunk.kind == "section"
        },
    )
    cache[key] = page
    return page


def _unique_sections(records) -> dict[tuple[str, str, int], object | None]:
    indexed = {}
    for rec in records:
        key = rec.file, rec.heading, rec.chunk
        indexed[key] = None if key in indexed else rec
    return indexed
```

- [x] **Step 6: Replace whole-section-to-chunk-zero mapping**

At the start of `_domain_signals`, retain the unfiltered records for global collision
detection while leaving the existing eligibility filter unchanged:

```python
    loaded_records = VectorStore(index_path(base, domain)).load()
    records = [
        rec for rec in loaded_records
        if _domain_file_path(base, domain, rec.file) is not None
        and _facet_ok(rec.type, rec.tags, type, tags)
        and (query_vec is None or rec.dim == len(query_vec))
    ]
    loaded_sections = [rec for rec in loaded_records if rec.kind == "section"]
```

Change the `_domain_signals` signature to:

```python
def _domain_signals(cfg: Config, base: str, domain: str, query: str,
                    query_vec: list[float] | None, mode: str, limit: int,
                    threshold: float, type: str | None, tags: list | None,
                    page_cache: PageCache) -> dict[str, list[dict]]:
```

Replace its lexical block with:

```python
    lexical_hits: list[dict] = []
    lexical_seeds: list[tuple[str, int]] = []
    lexical_map = {}
    if mode in ("lexical", "hybrid"):
        indexed = _unique_sections(loaded_sections)
        eligible_identities = {
            (rec.file, rec.heading, rec.chunk) for rec in sections
        }
        headings_by_file: dict[str, set[str]] = {}
        for rec in sections:
            headings_by_file.setdefault(rec.file, set()).add(rec.heading)

        page_scores: dict[str, int] = {}
        verified_chunks = []
        for file in sorted(sections_by_file):
            page = _materialize_page(cfg, base, domain, file, page_cache)
            if page is None:
                continue
            section_hits = score_sections(file, page.markdown, query)
            for hit in section_hits:
                if hit["heading"] in headings_by_file.get(file, set()):
                    page_scores[file] = page_scores.get(file, 0) + hit["score"]
            for chunk in page.chunks.values():
                key = file, chunk.heading, chunk.chunk
                rec = indexed.get(key)
                if (key in eligible_identities and rec is not None
                        and rec.hash == chunk.hash):
                    verified_chunks.append(chunk)
                    lexical_map[key] = rec

        lexical_hits = score_chunks(verified_chunks, query, None)
        ranked_pages = sorted(page_scores.items(), key=lambda item: (-item[1], item[0]))
        lexical_seeds = ranked_pages[:cfg.seed_top_k]
```

Build `lexical_section` with the full identity:

```python
    for rank, hit in enumerate(lexical_hits):
        rec = lexical_map[(hit["file"], hit["heading"], hit["chunk"])]
        signals["lexical_section"].append(
            _internal_hit(
                domain, rec, "lexical", (rank, rec.file, rec.ordinal, rec.chunk),
                ["lexical"],
            )
        )
```

- [x] **Step 7: Thread an optional cache through candidate preparation**

Extend `prepare_read_candidates`:

```python
def prepare_read_candidates(cfg: Config, base: str, domains: list[str], query: str,
                            top_k: int, threshold: float, mode: str = "hybrid",
                            type: str | None = None, tags: list | None = None,
                            page_cache: PageCache | None = None) -> list[dict]:
```

Immediately before `query_vec = None`, insert:

```python
    if page_cache is None:
        page_cache = {}
```

Pass `page_cache` as the last argument to every `_domain_signals` call. Leave
`search_read` callers working through the default `None` cache.

- [x] **Step 8: Run focused retrieval tests and verify GREEN**

Run:

```bash
uv run pytest -q tests/test_retrieval.py tests/test_grep.py tests/engine/test_fusion.py
```

Expected: exact late-window, stale hash, old collision, unchanged page seed, existing
fusion, path safety, and lexical no-embedding tests all pass.

- [x] **Step 9: Update and test the internal lexical compatibility wrapper**

Add this test to `tests/test_retrieval.py`:

```python
def test_lexical_search_compatibility_wrapper_returns_exact_chunk(
        tmp_path, monkeypatch):
    cfg, base = _long_lexical_page(tmp_path, monkeypatch)

    hits = retrieval.lexical_search(
        cfg, base, ["d"], "needle", 10, type=None, tags=None
    )

    direct = next(hit for hit in hits if hit["heading"] == "Details")
    assert direct["chunk"] == 1
```

Replace `lexical_search` in `src/iwiki_mcp/retrieval.py` with:

```python
def lexical_search(cfg: Config, base: str, domains: list[str], query: str, top_k: int,
                   type: str | None = None, tags: list | None = None) -> list[dict]:
    return search_read(
        cfg, base, domains, query, top_k, cfg.score_threshold,
        "lexical", type, tags,
    )
```

Delete `_hit_facets`; the compatibility wrapper no longer reads frontmatter directly.
Remove the now-unused `import os`.

Run:

```bash
uv run pytest -q tests/test_retrieval.py::test_lexical_search_compatibility_wrapper_returns_exact_chunk
```

Expected: PASS with `chunk == 1`.

- [x] **Step 10: Commit Task 3**

```bash
git add src/iwiki_mcp/retrieval.py tests/test_retrieval.py
git commit -m "fix(retrieval): map lexical hits to verified chunks"
```

### Task 4: Reuse materialized pages during hydration and reranking

**Files:**

- Modify: `tests/test_retrieval.py`
- Modify: `tests/test_server_search.py`
- Modify: `tests/test_server_search_facets.py`
- Modify: `tests/test_robustness_fixes.py`
- Modify: `src/iwiki_mcp/retrieval.py`
- Modify: `src/iwiki_mcp/server.py`

- [x] **Step 1: Add failing hydration-cache tests**

Append to `tests/test_retrieval.py`:

```python
def test_hydration_reuses_prepared_page_materialization(tmp_path, monkeypatch):
    cfg, base = _long_lexical_page(tmp_path, monkeypatch)
    cache = {}
    chunk_calls = []
    page_reads = []
    real_chunk_markdown = retrieval.chunk_markdown
    real_file_stamp = retrieval._file_stamp
    real_open = builtins.open
    page_path = (Path(base) / "d" / "long.md").resolve()
    stamp_calls = []

    def counted(*args, **kwargs):
        chunk_calls.append(args[0])
        return real_chunk_markdown(*args, **kwargs)

    def counted_open(file, *args, **kwargs):
        if Path(file).resolve() == page_path:
            page_reads.append(str(file))
        return real_open(file, *args, **kwargs)

    def counted_stamp(path):
        stamp_calls.append(path)
        return real_file_stamp(path)

    monkeypatch.setattr(retrieval, "chunk_markdown", counted)
    monkeypatch.setattr(retrieval, "_file_stamp", counted_stamp)
    monkeypatch.setattr(builtins, "open", counted_open)
    candidates = retrieval.prepare_read_candidates(
        cfg, base, ["d"], "needle", 10, 0.0,
        mode="lexical", page_cache=cache,
    )
    hydrated = retrieval.hydrate_candidates(
        cfg, base, candidates, page_cache=cache
    )

    matched = next(hit for hit in hydrated if hit["chunk"] == 1)
    assert matched["text"] == "## Details\nneedle five six"
    assert len(page_reads) == 1
    assert chunk_calls == ["long.md"]
    assert stamp_calls == [page_path, page_path, page_path]


def test_hydration_omits_cached_page_changed_after_preparation(
        tmp_path, monkeypatch):
    cfg, base = _long_lexical_page(tmp_path, monkeypatch)
    cache = {}
    candidates = retrieval.prepare_read_candidates(
        cfg, base, ["d"], "needle", 10, 0.0,
        mode="lexical", page_cache=cache,
    )
    page_path = Path(base) / "d" / "long.md"
    page_path.write_text(
        "---\ndescription: changed\n---\n# Long\n\n"
        "## Details\nreplacement content with a different length\n",
        encoding="utf-8",
    )

    hydrated = retrieval.hydrate_candidates(
        cfg, base, candidates, page_cache=cache
    )

    assert not any(
        hit["file"] == "long.md" and hit["heading"] == "Details"
        for hit in hydrated
    )


def test_hydration_omits_ambiguous_index_identity(tmp_path, monkeypatch):
    cfg, base = _long_lexical_page(tmp_path, monkeypatch)
    cache = {}
    candidates = retrieval.prepare_read_candidates(
        cfg, base, ["d"], "needle", 10, 0.0,
        mode="lexical", page_cache=cache,
    )
    target = next(
        hit for hit in candidates
        if hit["file"] == "long.md"
        and hit["heading"] == "Details"
        and hit["chunk"] == 1
    )
    path = wiki_base.index_path(base, "d")
    recs = store.load_index(path)
    duplicate = next(
        rec for rec in recs
        if rec.file == "long.md"
        and rec.heading == "Details"
        and rec.chunk == 1
    )
    store.save_index(path, recs + [duplicate])

    hydrated = retrieval.hydrate_candidates(
        cfg, base, [target], page_cache=cache
    )

    assert hydrated == []
```

- [x] **Step 2: Run the hydration-cache test and verify RED**

Run:

```bash
uv run pytest -q tests/test_retrieval.py::test_hydration_reuses_prepared_page_materialization tests/test_retrieval.py::test_hydration_omits_cached_page_changed_after_preparation tests/test_retrieval.py::test_hydration_omits_ambiguous_index_identity
```

Expected: all three tests fail because `hydrate_candidates` rejects `page_cache`. After
its signature is extended, the mutation and collision assertions remain the safety
gates that prevent blind cached-text reuse and last-record-wins identity hydration.

- [x] **Step 3: Make hydration use the shared materializer**

Extend `hydrate_candidates`:

```python
def hydrate_candidates(cfg: Config, base: str, candidates: list[dict],
                       page_cache: PageCache | None = None) -> list[dict]:
    if page_cache is None:
        page_cache = {}
```

Change the `indexes` annotation and build collision-safe per-domain index hashes:

```python
    indexes: dict[
        str, dict[tuple[str, str, int], str | None]
    ] = {}

            identities = _unique_sections([
                rec for rec in VectorStore(index_path(base, domain)).load()
                if rec.kind == "section"
            ])
            indexes[domain] = {
                key: rec.hash if rec is not None else None
                for key, rec in identities.items()
            }
```

Before the candidate loop, add a per-hydration validation map:

```python
    validated_pages: dict[
        tuple[str, str], _MaterializedPage | None
    ] = {}
```

Replace hydration's separate page read/re-chunk block with:

```python
        page_key = domain, candidate["file"]
        if page_key not in validated_pages:
            page = _materialize_page(
                cfg, base, domain, candidate["file"], page_cache
            )
            if page is not None and _file_stamp(page.path) != page.stamp:
                page = None
            validated_pages[page_key] = page
        page = validated_pages[page_key]
        chunk_key = candidate["heading"], candidate["chunk"]
        if page is None or chunk_key not in page.chunks:
            continue
        chunk = page.chunks[chunk_key]
        indexed_hash = indexes[domain].get(
            (candidate["file"], candidate["heading"], candidate["chunk"])
        )
        if indexed_hash != chunk.hash:
            continue
        hydrated.append({**candidate, "text": chunk.text})
```

Remove the old local `pages` map; `page_cache` replaces it. The validation map ensures
one fingerprint recheck per page during hydration while every candidate for that page
uses the same decision.

- [x] **Step 4: Run hydration and path-safety tests**

Run:

```bash
uv run pytest -q tests/test_retrieval.py
```

Expected: all retrieval and hydration tests pass, including traversal and symlink
guards.

- [x] **Step 5: Add a failing server cache-threading test**

Append to `tests/test_server_search.py`:

```python
def test_search_threads_one_page_cache_to_prepare_and_hydrate(
        tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setenv("IWIKI_RERANK_MODEL", "model")
    seen = {}

    def prepare(*args, page_cache=None, **kwargs):
        seen["prepare"] = page_cache
        return [{
            "domain": "backend", "file": "auth.md", "heading": "Token", "chunk": 0,
            "score": 0.5, "hit": "lexical", "source": "lexical",
        }]

    def hydrate(cfg, base, candidates, page_cache=None):
        seen["hydrate"] = page_cache
        return [{**candidates[0], "text": "## Token\nrefresh_token rotates"}]

    monkeypatch.setattr(server.retrieval, "prepare_read_candidates", prepare)
    monkeypatch.setattr(server.retrieval, "hydrate_candidates", hydrate)
    monkeypatch.setattr(
        server.rerank, "rerank_candidates",
        lambda cfg, query, candidates, top_n: (
            [{key: value for key, value in candidates[0].items() if key != "text"}],
            {"applied": True},
        ),
    )

    server.wiki_search("refresh_token", mode="lexical")

    assert seen["prepare"] is seen["hydrate"]
    assert seen["prepare"] == {}
```

Make these exact replacements in the existing tests:

```python
# test_reranker_can_promote_candidate_below_preliminary_top_k
lambda cfg, base, items, page_cache=None: [
    {**item, "text": item["file"]} for item in items
]

# test_partial_rerank_preserves_all_unscored_preliminary_order
lambda cfg, base, items, page_cache=None: [
    {**item, "text": item["file"]} for item in items[1:]
]

# test_configured_reranker_with_no_hydrated_candidates_fails_soft
lambda *args, **kwargs: []
```

Also add `page_cache=None` after `tags=None` in all three explicit
`prepare_read_candidates` test doubles:

```python
# tests/test_robustness_fixes.py::test_wiki_search_whitespace_type_becomes_no_filter
def fake_candidates(cfg, base, doms, query, top_k, threshold, mode,
                    type=None, tags=None, page_cache=None):
    captured.update(type=type, tags=tags)
    return []

# tests/test_server_search_facets.py::test_wiki_search_passes_facets
def fake_candidates(cfg, base, doms, query, top_k, threshold, mode,
                    type=None, tags=None, page_cache=None):
    captured.update(type=type, tags=tags)
    return []

# tests/test_server_search_facets.py::test_wiki_search_normalizes_facets
def fake_candidates(cfg, base, doms, query, top_k, threshold, mode,
                    type=None, tags=None, page_cache=None):
    captured.update(type=type, tags=tags)
    return []
```

- [x] **Step 6: Run the server cache test and verify RED**

Run:

```bash
uv run pytest -q tests/test_server_search.py::test_search_threads_one_page_cache_to_prepare_and_hydrate
```

Expected: preparation and hydration currently receive no shared cache.

- [x] **Step 7: Thread one cache through `wiki_search`**

In `src/iwiki_mcp/server.py`, before candidate preparation:

```python
    page_cache = {}
```

Pass it to preparation:

```python
            page_cache=page_cache,
```

Pass the same object to hydration:

```python
        hydrated = retrieval.hydrate_candidates(
            cfg, bind.base, candidates, page_cache=page_cache
        )
```

- [x] **Step 8: Run server and retrieval suites**

Run:

```bash
uv run pytest -q tests/test_retrieval.py tests/test_server_search.py tests/test_server_search_facets.py tests/test_robustness_fixes.py
```

Expected: all tests pass; reranker ordering and fail-soft metadata remain unchanged.

- [x] **Step 9: Commit Task 4**

```bash
git add src/iwiki_mcp/retrieval.py src/iwiki_mcp/server.py tests/test_retrieval.py tests/test_server_search.py tests/test_server_search_facets.py tests/test_robustness_fixes.py
git commit -m "perf(search): reuse lexical page materialization"
```

### Task 5: Document behavior and one-time reindex

**Files:**

- Update iwiki page: domain `iwiki-mcp`, slug `indexing`, heading `Markdown chunking`
- Update iwiki page: domain `iwiki-mcp`, slug `retrieval`, heading `Lexical search`
- No repository Markdown source is changed by this task; iwiki MCP writes auto-index and
  auto-commit the external wiki base.
- Reindex through iwiki MCP after the upgraded server process is installed and
  restarted; record per-domain migration results.

- [x] **Step 1: Update indexing documentation through iwiki MCP**

Call `wiki_update_page` with:

- `domain="iwiki-mcp"`
- `slug="indexing"`
- `heading="Markdown chunking"`
- `source="src/iwiki_mcp/engine/chunk.py"`
- `new_body` equal to:

```markdown
`chunk_markdown` produces a two-level set of chunks per page. One `summary` chunk
(`kind="summary"`, `heading=""`, `ordinal=-1`) carries the full whitespace-collapsed
frontmatter `description` as its embed text. Every non-reserved H2 section produces
clean `section` chunks whose embed text is only `## {heading}\n{body window}`.
`## Overview`, `## Outgoing links`, and `## External links` remain excluded.

Section bodies use word windows configured by `chunk_size` and `chunk_overlap`.
Chunk numbering is zero-based and continuous across every exact repeated heading in
one page: later occurrences continue after the earlier occurrence's last window.
Different headings keep independent counters, and pages with unique headings retain
their prior numbering. Every source occurrence is preserved; heading equality, equal
hashes, or semantic similarity never deduplicate a section.

This identity is consumed by [Hybrid search](retrieval.md#hybrid-search). After
upgrading, run `wiki_index(domain)` once for every bound domain. The migration does not
bump the JSONL schema: stable identities reuse embeddings, moved repeated-heading
identities are embedded, and obsolete collisions disappear when the fresh record set
is saved.
```

Expected tool result: page updated and reindexed without an error.

- [x] **Step 2: Update retrieval documentation through iwiki MCP**

Call `wiki_update_page` with:

- `domain="iwiki-mcp"`
- `slug="retrieval"`
- `heading="Lexical search"`
- `source="src/iwiki_mcp/retrieval.py"`
- `new_body` equal to:

```markdown
`lexical_search(cfg, base, domains, query, top_k, type=None, tags=None)` remains an
internal compatibility wrapper and delegates to the canonical lexical `search_read`
flow. Lexical mode performs no query-embedding request.

Retrieval materializes each eligible Markdown page at most once per request using the
canonical [Markdown chunking](indexing.md#markdown-chunking). Whole-H2 term frequency
is retained only to aggregate lexical page-seed scores, so overlap does not inflate
page selection. Direct `lexical_section` signals score current canonical chunks.

A direct chunk hit requires one unique indexed `(file, heading, chunk)` identity and an
equal current/index hash. Unsafe paths, unreadable pages, missing or stale chunks, and
ambiguous pre-migration identities are omitted without falling back to `chunk=0`.
Candidate preparation and optional reranker hydration share the same request-local
materialization. Materialization rejects a file changed during its read; hydration
revalidates the cached file fingerprint once per page and retains the indexed-hash
equality gate. A page changed after preparation is omitted without a second content
read.

Positive whole-H2 page totals still select lexical page seeds; those seeds still expand
graph pages and contribute separate `lexical_page`, `graph_page`, and
`lexical_section` lists to Reciprocal Rank Fusion. Pure lexical candidates keep
`hit="lexical"` and direct chunks keep `source="lexical"`.
```

Expected tool result: page updated and reindexed without an error.

- [x] **Step 3: Validate iwiki health**

Call `wiki_lint(domain="iwiki-mcp")`.

Expected:

- `broken: []`
- `stale: []` for `indexing.md` and `retrieval.md`
- no new missing source, legacy wikilink, or missing-frontmatter finding
- pre-existing advisory long-lead/orphan/tag-drift entries are permitted to remain
  unchanged because this task did not introduce them

- [x] **Step 4: Verify the migration implementation**

Run the existing focused migration test:

```bash
uv run pytest -q tests/test_indexer.py::test_reindex_migrates_repeated_heading_identity_without_schema_bump
```

Expected: PASS, proving the ordinary index path performs the migration without changing
`SCHEMA_VERSION`.

- [x] **Step 5: HUMAN CHECKPOINT — restart the upgraded MCP server**

Do not run the operational reindex through an MCP process that imported the pre-change
chunker. Install or otherwise activate this branch's `0.7.3` server, restart the MCP
process, and obtain explicit confirmation that the restarted connector is active.

Expected: user confirms the upgraded MCP process is active. If restart/deployment is
outside the current session, stop with a rollout handoff; do not claim migration
complete.

- [x] **Step 6: Reindex every currently bound domain through iwiki MCP**

Call `wiki_status`, form the distinct union of its current `read` domains and non-null
`write` domain, then call `wiki_index(domain=<domain>)` exactly once for each. Use the
runtime binding list rather than copying the planning-time list, because bindings may
change before deployment.

Expected for every domain: no `error`, returned `domain` matches the requested domain,
and `indexed_chunks`, `reused`, and `embedded` are recorded in result evidence. Run
`wiki_lint(domain="iwiki-mcp")` again; `broken` and `stale` remain empty. Reindexing may
make external wiki-base commits, as already authorized; do not call `wiki_sync` unless
the user separately requests remote publication.

Execution evidence (2026-07-19; `indexed_chunks` / `reused` / `embedded`):
`iwiki-mcp` 65/65/0; `personal-ai-wiki` 149/149/0; `obsidian-ai-wiki` 62/62/0;
`icodex` 97/97/0; `iclaude` 25/0/25; `okf` 62/62/0. The initial `iclaude` call
failed with 502; the first separately user-authorized retry also failed with 502; the
second separately user-authorized retry succeeded with 25/0/25. Thus exactly one
successful migration occurred across three invocations. No `wiki_sync` was run. The
`iwiki-mcp` lint scope was clean (`broken: []`, `stale: []`).

### Task 6: Run complete verification and close implementation evidence

**Files:**

- Verify: `pyproject.toml`
- Verify: `src/iwiki_mcp/__init__.py`
- Verify: `uv.lock`
- Update during execution tracking:
  `docs/superpowers/plans/2026-07-18-lexical-retrieval-chunk-scoring.md`

- [x] **Step 1: Run focused acceptance tests**

```bash
uv run pytest -q tests/engine/test_chunk.py tests/test_grep.py tests/test_indexer.py tests/test_retrieval.py tests/test_server_search.py
```

Expected: all focused tests pass, including exact late-window, repeated-heading,
no-deduplication, cache, migration, and reranker hydration cases.

- [x] **Step 2: Run the complete test suite**

```bash
uv run pytest -q
```

Expected: all tests pass with zero failures.

- [x] **Step 3: Run lint**

```bash
uv run flake8 src tests
```

Expected: exit status 0 and no output.

- [x] **Step 4: Run CLI smoke**

```bash
uv run iwiki-mcp --help
```

Expected: exit status 0 and help output without traceback.

- [x] **Step 5: Verify version consistency**

```bash
rg -n 'version = "0.7.3"|__version__ = "0.7.3"' pyproject.toml src/iwiki_mcp/__init__.py uv.lock
```

Expected: `0.7.3` appears in all three version-bearing files and no task-local version
drift exists.

- [x] **Step 6: Verify diff scope and whitespace**

```bash
git status --short
git diff --check master...HEAD
git diff --stat master...HEAD
```

Expected: only planned implementation, tests, chain artifacts, TODO/version files, and
approved documentation evidence are present; the branch diff from `master` is clean
under `git diff --check master...HEAD`.

Execution evidence (2026-07-19): focused acceptance tests passed 78 tests; the full
suite passed 532 tests; flake8 exited 0 with no output; CLI help exited 0; version
`0.7.3` appeared in all three version-bearing files; `git diff --check master...HEAD`
was clean; the changed-file scope matched the plan.

- [x] **Step 7: Commit any remaining planned repository changes**

If Task 6 changed only checkbox tracking in this plan, stage only that file:

```bash
git add docs/superpowers/plans/2026-07-18-lexical-retrieval-chunk-scoring.md
git commit -m "docs(plan): record lexical chunk scoring execution"
```

Expected: clean working tree after the commit. Do not create an empty commit.

Before staging, mark Step 7 complete in this plan so every implementation checkbox is
recorded in the commit.

## Post-plan chain gates

Run these only after every task checkbox above is complete and committed. They are
chain validation and result-recording actions, not implementation tasks.

1. Revalidate the fully checked plan:

```text
$check-chain plan docs/superpowers/plans/2026-07-18-lexical-retrieval-chunk-scoring.md
```

Expected: plan verdict `OK`, refreshed `review.plan_hash`, and TODO `Plan: ✓`. Commit the
frontmatter and TODO update:

```bash
git add docs/TODO.md docs/superpowers/plans/2026-07-18-lexical-retrieval-chunk-scoring.md
git commit -m "docs(plan): validate lexical chunk scoring execution"
```

2. Reconcile the complete committed branch diff:

```text
$check-chain result docs/superpowers/plans/2026-07-18-lexical-retrieval-chunk-scoring.md --since=master
```

Expected: `Result: OK`, TODO row closed, no missing plan step, no unfixed review
finding, verification evidence recorded, and final result report generated. Stage and
commit the result artifacts:

```bash
git add docs/TODO.md docs/superpowers/plans/2026-07-18-lexical-retrieval-chunk-scoring.md docs/superpowers/reports/lexical-retrieval-chunk-scoring-results.html
git commit -m "docs(result): record lexical chunk scoring outcome"
```

Expected: commit succeeds after `check-chain result` returns `OK`; `git status --short`
is empty.
