# OKF Frontmatter Adoption Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every iwiki page OKF-conformant YAML frontmatter (governed `type` + `tags`), make the base OKF-portable via an export tool, and add faceted retrieval — without changing iwiki's page-body rules.

**Architecture:** Adopt only the OKF *container*. A new stdlib-only `engine/frontmatter.py` splits/renders frontmatter and holds the governance vocabulary; existing engine modules (`chunk`, `validate`, `lint`, `store`) operate on the body after splitting. `type`/`tags` are supplied by the host agent (explicit params) or, when `IWIKI_CHAT_MODEL` is set, by an optional server-side `engine/classify.py`; either way the server governs them. `server.py` gains frontmatter-writing on the write path plus new tools (`wiki_migrate_okf`, `wiki_apply_okf`, `wiki_export_okf`) and faceted `wiki_search`.

**Tech Stack:** Python 3.10+, `httpx` (chat/embeddings), `pytest` (`asyncio_mode=auto`, `pythonpath=["src"]`), MCP FastMCP. No YAML dependency — a minimal frontmatter subset parser keeps the engine stdlib-only.

## Global Constraints

- **Version bump:** `pyproject.toml` `0.1.x` → `0.2.0` (minor; new feature).
- **Engine stays stdlib-only where it already is:** `frontmatter.py`, `validate.py`, `lint.py` must NOT import `httpx`/`chunk`/`embed` (config-free contract). `classify.py` MAY use `httpx` (it is the LLM seam, like `embed.py`).
- **Keep-in-sync constants:** `OVERVIEW_HEADING`, `LEAD_MAX`, and the `_H2` regex are duplicated across `chunk.py`, `validate.py`, `lint.py` on purpose. Do not consolidate them.
- **Tests never hit the network:** monkeypatch `indexer.embed_texts` and `classify.classify_page`; set dummy `IWIKI_*` env. Follow `tests/test_server_write.py::_seed`.
- **Fail-soft tools:** every `wiki_*` handler is defined plain and wrapped by `@_safe`; registered via `mcp.tool()(wiki_*)` at the bottom of `server.py`. New tools follow this split.
- **Governance vocabulary (single source of truth in `frontmatter.py`):**
  `OKF_TYPES = ("architecture", "api", "guide", "reference", "runbook", "concept")`,
  `DEFAULT_TYPE = "concept"`, `MAX_TAGS = 5`.
- **Type/tags precedence (never a hard model binding):** (1) explicit `type`/`tags` params win; (2) else, if `IWIKI_CHAT_MODEL` is set, the server classifies via an OpenAI-compatible chat endpoint (reusing `IWIKI_LLM_BASE_URL`/`IWIKI_LLM_KEY`); (3) else default `concept` / `[]` with a `warning`.
- **`IWIKI_CHAT_MODEL` is optional, empty by default:** no server-side chat unless the operator opts in. The server keeps working with only its embeddings dependency.
- **Server-side classification is best-effort:** a chat failure or off-vocabulary result falls back to `DEFAULT_TYPE` with empty tags and a threaded `warning`; it must NEVER fail a write.

---

## File Structure

- **Create** `src/iwiki_mcp/engine/frontmatter.py` — split/render + governance constants/helpers (`OKF_TYPES`, `DEFAULT_TYPE`, `MAX_TAGS`, `normalize_tag`, `coerce_type`, `derive_title`, `derive_description`).
- **Create** `src/iwiki_mcp/engine/classify.py` — OpenAI-compatible chat client `classify_page(cfg, body, existing_tags)`, called only when `IWIKI_CHAT_MODEL` is set.
- **Create** `src/iwiki_mcp/okf.py` — top-layer frontmatter assembly (`build_frontmatter`, `domain_tag_vocab`, `git_last_commit_date`).
- **Create** `src/iwiki_mcp/export.py` — OKF bundle serialization (`convert_wikilinks`, `export_domain`).
- **Modify** `src/iwiki_mcp/engine/chunk.py` — strip frontmatter before splitting; stamp `type`/`tags` onto `Chunk`.
- **Modify** `src/iwiki_mcp/engine/store.py` — `Record` gains `type`/`tags` (defaulted); `make_record` copies them.
- **Modify** `src/iwiki_mcp/indexer.py` — refresh reused records' `type`/`tags` without re-embedding.
- **Modify** `src/iwiki_mcp/engine/validate.py` — validate body; advisory `missing_type`/`missing_description`/`unknown_type`.
- **Modify** `src/iwiki_mcp/engine/lint.py` — body-only checks; `missing_frontmatter` + `tag_drift`.
- **Modify** `src/iwiki_mcp/retrieval.py` — facet filters (`type`/`tags`) + `_facet_ok`.
- **Modify** `src/iwiki_mcp/engine/config.py` — add `chat_model` (`IWIKI_CHAT_MODEL`, default `""`).
- **Modify** `src/iwiki_mcp/server.py` — faceted `wiki_search`; frontmatter on write path; new `wiki_migrate_okf`, `wiki_apply_okf`, `wiki_export_okf`; register them.
- **Modify** `src/iwiki_mcp/resources.py` — authoring rules: frontmatter section + type rubric.
- **Modify** `README.md`, `docs/README.ru.md`, `pyproject.toml`.
- **Create** tests under `tests/` per task.

---

## Task 1: `engine/frontmatter.py` — split/render + governance

**Files:**
- Create: `src/iwiki_mcp/engine/frontmatter.py`
- Test: `tests/test_frontmatter.py`

**Interfaces:**
- Produces:
  - `OKF_TYPES: tuple[str, ...]`, `DEFAULT_TYPE: str`, `MAX_TAGS: int`
  - `split(content: str) -> tuple[dict, str]`
  - `render(meta: dict) -> str`
  - `normalize_tag(s: str) -> str`
  - `normalize_tags(tags: list[str]) -> list[str]` (normalize, drop empties, dedupe, cap `MAX_TAGS`)
  - `coerce_type(s: str | None) -> str`
  - `derive_title(body: str, slug: str) -> str`
  - `derive_description(body: str, max_chars: int = 400) -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_frontmatter.py
from iwiki_mcp.engine import frontmatter as fm


def test_split_extracts_meta_and_body():
    content = "---\ntype: api\ntitle: X\ntags: [a, b]\n---\n# X\n\n## Overview\nhi\n"
    meta, body = fm.split(content)
    assert meta["type"] == "api"
    assert meta["title"] == "X"
    assert meta["tags"] == ["a", "b"]
    assert body.startswith("# X")


def test_split_no_frontmatter_returns_empty_meta():
    content = "# X\n\n## Overview\nhi\n"
    meta, body = fm.split(content)
    assert meta == {}
    assert body == content


def test_split_malformed_is_failsoft():
    content = "---\nnot closed\n# X\n"
    meta, body = fm.split(content)
    assert meta == {}
    assert body == content


def test_render_round_trips():
    meta = {"type": "api", "title": "X", "tags": ["a", "b"]}
    meta2, _ = fm.split(fm.render(meta) + "# body\n")
    assert meta2["type"] == "api"
    assert meta2["tags"] == ["a", "b"]


def test_normalize_tag_kebab_lowercase():
    assert fm.normalize_tag("  Data Flow ") == "data-flow"
    assert fm.normalize_tag("Config_Key") == "config-key"


def test_normalize_tags_dedupe_and_cap():
    tags = fm.normalize_tags(["A", "a", "b", "c", "d", "e", "f"])
    assert tags[:2] == ["a", "b"]
    assert len(tags) == fm.MAX_TAGS


def test_coerce_type_clamps_offvocab():
    assert fm.coerce_type("api") == "api"
    assert fm.coerce_type("weird") == fm.DEFAULT_TYPE
    assert fm.coerce_type(None) == fm.DEFAULT_TYPE


def test_derive_title_from_h1_then_slug():
    assert fm.derive_title("# Base binding\n\n## Overview\nx", "b") == "Base binding"
    assert fm.derive_title("## Overview\nx", "my-slug") == "my slug"


def test_derive_description_from_overview_capped():
    body = "# T\n\n## Overview\n" + "word " * 200 + "\n\n## Other\nx"
    desc = fm.derive_description(body, max_chars=50)
    assert len(desc) <= 50
    assert desc.startswith("word")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_frontmatter.py -v`
Expected: FAIL with `ModuleNotFoundError: iwiki_mcp.engine.frontmatter`

- [ ] **Step 3: Write minimal implementation**

```python
# src/iwiki_mcp/engine/frontmatter.py
"""OKF frontmatter: split/render a minimal YAML subset (stdlib-only, no pyyaml),
plus the governed type/tag vocabulary. Importable by validate/lint (config-free).
"""
from __future__ import annotations
import os
import re

OKF_TYPES = ("architecture", "api", "guide", "reference", "runbook", "concept")
DEFAULT_TYPE = "concept"
MAX_TAGS = 5

OVERVIEW_HEADING = "overview"   # keep in sync with chunk.OVERVIEW_HEADING
_H1 = re.compile(r"^#\s+(.*?)\s*$", re.MULTILINE)
_H2 = re.compile(r"^##\s+(.*?)\s*$", re.MULTILINE)
_FM = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def split(content: str) -> tuple[dict, str]:
    """Strip a leading ``---\\n…\\n---\\n`` block. Fail-soft: no/broken block -> ({}, content)."""
    m = _FM.match(content)
    if not m:
        return {}, content
    meta: dict = {}
    for line in m.group(1).splitlines():
        line = line.rstrip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if val.startswith("[") and val.endswith("]"):
            items = [x.strip() for x in val[1:-1].split(",")]
            meta[key] = [x for x in items if x]
        else:
            meta[key] = val
    return meta, content[m.end():]


def render(meta: dict) -> str:
    """Emit a frontmatter block in a stable key order. Lists render inline."""
    order = ["type", "title", "description", "resource", "tags", "timestamp"]
    keys = [k for k in order if k in meta] + [k for k in meta if k not in order]
    lines = ["---"]
    for k in keys:
        v = meta[k]
        if isinstance(v, list):
            lines.append(f"{k}: [{', '.join(v)}]")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---\n")
    return "\n".join(lines)


def normalize_tag(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s


def normalize_tags(tags: list[str]) -> list[str]:
    out: list[str] = []
    for t in tags:
        n = normalize_tag(t)
        if n and n not in out:
            out.append(n)
    return out[:MAX_TAGS]


def coerce_type(s: str | None) -> str:
    return s if s in OKF_TYPES else DEFAULT_TYPE


def derive_title(body: str, slug: str) -> str:
    head = body[:_H2.search(body).start()] if _H2.search(body) else body
    m = _H1.search(head)
    if m and m.group(1).strip():
        return m.group(1).strip()
    stem = os.path.basename(slug)
    return stem.replace("-", " ").replace("_", " ").strip()


def derive_description(body: str, max_chars: int = 400) -> str:
    ms = list(_H2.finditer(body))
    for i, m in enumerate(ms):
        if m.group(1).strip().lower() != OVERVIEW_HEADING:
            continue
        end = ms[i + 1].start() if i + 1 < len(ms) else len(body)
        return " ".join(body[m.end():end].split())[:max_chars]
    return ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_frontmatter.py -v`
Expected: PASS (all 9 tests)

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/engine/frontmatter.py tests/test_frontmatter.py
git commit -m "feat(frontmatter): add OKF frontmatter split/render + governance"
```

---

## Task 2: `chunk.py` — strip frontmatter, stamp type/tags

**Files:**
- Modify: `src/iwiki_mcp/engine/chunk.py`
- Test: `tests/test_chunk_frontmatter.py`

**Interfaces:**
- Consumes: `frontmatter.split`, `frontmatter.normalize_tags`, `frontmatter.coerce_type`
- Produces: `Chunk` gains `type: str | None = None`, `tags: list = []`; `chunk_markdown` stamps them from the page frontmatter.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_chunk_frontmatter.py
from iwiki_mcp.engine.chunk import chunk_markdown


PAGE = (
    "---\ntype: api\ntags: [binding, config]\n---\n"
    "# Title\n\n## Overview\nsummary here\n\n## Body\nreal content words\n"
)


def test_frontmatter_excluded_from_chunk_text():
    chunks = chunk_markdown("p.md", PAGE, size=512, overlap=64)
    assert chunks, "expected at least one chunk"
    for c in chunks:
        assert "type: api" not in c.text
        assert "---" not in c.text


def test_type_and_tags_stamped_on_chunks():
    chunks = chunk_markdown("p.md", PAGE, size=512, overlap=64)
    assert all(c.type == "api" for c in chunks)
    assert all(c.tags == ["binding", "config"] for c in chunks)


def test_page_without_frontmatter_defaults():
    plain = "# T\n\n## Overview\ns\n\n## B\nwords\n"
    chunks = chunk_markdown("p.md", plain, size=512, overlap=64)
    assert all(c.type is None for c in chunks)
    assert all(c.tags == [] for c in chunks)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_chunk_frontmatter.py -v`
Expected: FAIL — `Chunk` has no attribute `type` / frontmatter text present.

- [ ] **Step 3: Write minimal implementation**

In `src/iwiki_mcp/engine/chunk.py`, extend the import line to `from dataclasses import dataclass, field`, add `from . import frontmatter as _fm`, add the two fields, and split frontmatter at the top of `chunk_markdown`:

```python
from dataclasses import dataclass, field
from . import frontmatter as _fm
```

```python
@dataclass
class Chunk:
    file: str
    heading: str
    chunk: int
    text: str
    hash: str
    type: str | None = None
    tags: list = field(default_factory=list)
```

```python
def chunk_markdown(file: str, content: str, size: int, overlap: int,
                   summary_max: int = 400) -> list[Chunk]:
    meta, content = _fm.split(content)
    ptype = _fm.coerce_type(meta.get("type")) if meta.get("type") else None
    ptags = _fm.normalize_tags(meta.get("tags", [])) if meta.get("tags") else []
    out: list[Chunk] = []
    title = _page_title(content, file)
    secs = _sections(content)
    article_summary = ""
    if secs and secs[0][0].lower() == OVERVIEW_HEADING:
        article_summary = " ".join(secs[0][1].split())[:summary_max]
        secs = secs[1:]
    for heading, body in secs:
        lead = _lead(body)
        prefix = "\n".join(
            ln for ln in (f"# {title}", article_summary, f"## {heading}", lead) if ln
        )
        for ci, piece in enumerate(_split_section(body.split(), size, overlap)):
            text = prefix + "\n\n" + " ".join(piece)
            out.append(Chunk(file=file, heading=heading, chunk=ci, text=text,
                             hash=_hash(text), type=ptype, tags=list(ptags)))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_chunk_frontmatter.py tests/ -k chunk -v`
Expected: PASS; existing chunk tests still pass.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/engine/chunk.py tests/test_chunk_frontmatter.py
git commit -m "feat(chunk): strip frontmatter and stamp type/tags on chunks"
```

---

## Task 3: `store.py` — Record carries type/tags (back-compat)

**Files:**
- Modify: `src/iwiki_mcp/engine/store.py`
- Test: `tests/test_store_facets.py`

**Interfaces:**
- Produces: `Record` gains `type: str | None = None`, `tags: list = []` (defaulted, so old JSONL loads); `make_record(c, vec)` copies `c.type` / `c.tags`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store_facets.py
import json
from iwiki_mcp.engine.store import make_record, load_index


class _C:
    id = "p.md#H"
    file = "p.md"
    heading = "H"
    chunk = 0
    hash = "abc"
    type = "api"
    tags = ["x", "y"]


def test_make_record_copies_type_tags():
    r = make_record(_C(), [0.1, 0.2])
    assert r.type == "api"
    assert r.tags == ["x", "y"]


def test_old_jsonl_without_facets_loads(tmp_path):
    p = tmp_path / "index.jsonl"
    rec = {"id": "p.md#H", "file": "p.md", "heading": "H", "chunk": 0,
           "hash": "abc", "dim": 2, "scale": 1.0, "q": [1, 2]}
    p.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    recs = load_index(str(p))
    assert recs[0].type is None
    assert recs[0].tags == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_store_facets.py -v`
Expected: FAIL — `Record.__init__` rejects extra args / missing `type`.

- [ ] **Step 3: Write minimal implementation**

In `src/iwiki_mcp/engine/store.py`, extend the dataclass import to include `field`, extend `Record`, and extend `make_record`:

```python
from dataclasses import dataclass, asdict, field

@dataclass
class Record:
    id: str
    file: str
    heading: str
    chunk: int
    hash: str
    dim: int
    scale: float
    q: list[int]
    type: str | None = None
    tags: list = field(default_factory=list)
```

```python
def make_record(c, vec: list[float]) -> Record:
    scale, q = quantize(vec)
    return Record(id=c.id, file=c.file, heading=c.heading, chunk=c.chunk,
                  hash=c.hash, dim=len(vec), scale=scale, q=q,
                  type=getattr(c, "type", None), tags=list(getattr(c, "tags", [])))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_store_facets.py tests/ -k store -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/engine/store.py tests/test_store_facets.py
git commit -m "feat(store): carry type/tags on records with back-compat defaults"
```

---

## Task 4: `indexer.py` — refresh reused records' facets

**Files:**
- Modify: `src/iwiki_mcp/indexer.py:44-56`
- Test: `tests/test_indexer_facets.py`

**Interfaces:**
- Consumes: `Chunk.type`/`tags` (Task 2), `Record.type`/`tags` (Task 3).
- Produces: after re-index, a reused (unchanged-hash) chunk's record reflects the *current* frontmatter `type`/`tags` without re-embedding.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_indexer_facets.py
import iwiki_mcp.indexer as indexer
from iwiki_mcp.engine.config import Config
from iwiki_mcp.engine.store import VectorStore
from iwiki_mcp.base import index_path


def _cfg():
    return Config(base_url="x", api_key="x", embed_model="m", chat_model="",
                  dimensions=2, chunk_size=512, chunk_overlap=64, summary_max=400,
                  top_k=8, score_threshold=0.2, graph_depth=2, ignore=None)


def test_reindex_refreshes_facets_without_reembed(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, texts: [[0.1, 0.2] for _ in texts])
    base = tmp_path
    (base / "d" / ".iwiki").mkdir(parents=True)
    page = base / "d" / "p.md"
    page.write_text("---\ntype: api\ntags: [a]\n---\n# T\n\n## Overview\ns\n\n## B\nwords here\n", encoding="utf-8")
    indexer.index_domain(_cfg(), str(base), "d")
    # change only the frontmatter (body/hash unchanged)
    page.write_text("---\ntype: guide\ntags: [b]\n---\n# T\n\n## Overview\ns\n\n## B\nwords here\n", encoding="utf-8")
    stats = indexer.index_domain(_cfg(), str(base), "d")
    recs = VectorStore(index_path(str(base), "d")).load()
    assert recs and all(r.type == "guide" for r in recs)
    assert all(r.tags == ["b"] for r in recs)
    assert stats["reused"] >= 1  # body unchanged -> not re-embedded
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_indexer_facets.py -v`
Expected: FAIL — reused record keeps stale `type == "api"`.

- [ ] **Step 3: Write minimal implementation**

In `index_domain`, when reusing a record by matching hash, copy the fresh chunk's facets onto it:

```python
    fresh, reused, to_embed = [], 0, []
    for c in chunks:
        key = f"{c.id}#{c.chunk}"
        prev = existing.get(key)
        if prev and prev.hash == c.hash and prev.dim == cfg.dimensions:
            prev.type = c.type          # refresh facets without re-embedding
            prev.tags = list(c.tags)
            fresh.append(prev)
            reused += 1
        else:
            to_embed.append(c)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_indexer_facets.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/indexer.py tests/test_indexer_facets.py
git commit -m "feat(indexer): refresh reused records' facets on reindex"
```

---

## Task 5: `validate.py` — validate body + advisory frontmatter findings

**Files:**
- Modify: `src/iwiki_mcp/engine/validate.py`
- Test: `tests/test_validate_frontmatter.py`

**Interfaces:**
- Consumes: `frontmatter.split`, `frontmatter.OKF_TYPES`.
- Produces: `validate_page` strips frontmatter before body checks; adds advisory `missing_type`, `missing_description`, `unknown_type`. The blocking subset stays `{deep_heading, pre_h2_text}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_validate_frontmatter.py
from iwiki_mcp.engine.validate import validate_page


def _types(findings):
    return {f["type"] for f in findings}


def test_frontmatter_does_not_trigger_pre_h2_text():
    page = "---\ntype: api\n---\n# T\n\n## Overview\ns\n\n## B\nbody\n"
    assert "pre_h2_text" not in _types(validate_page(page))


def test_missing_type_and_description_are_advisory():
    page = "# T\n\n## B\nbody without overview\n"
    findings = validate_page(page)
    types = _types(findings)
    assert "missing_type" in types
    assert "missing_description" in types
    assert all(f["severity"] == "advisory"
               for f in findings if f["type"] in {"missing_type", "missing_description"})


def test_unknown_type_flagged_advisory():
    page = "---\ntype: bogus\n---\n# T\n\n## Overview\ns\n\n## B\nbody\n"
    findings = validate_page(page)
    assert "unknown_type" in _types(findings)


def test_valid_typed_page_has_no_frontmatter_findings():
    page = "---\ntype: api\n---\n# T\n\n## Overview\ns\n\n## B\nbody\n"
    types = _types(validate_page(page))
    assert not ({"missing_type", "unknown_type", "missing_description"} & types)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_validate_frontmatter.py -v`
Expected: FAIL — `pre_h2_text` fires on frontmatter; new finding types absent.

- [ ] **Step 3: Write minimal implementation**

In `src/iwiki_mcp/engine/validate.py`, add `from . import frontmatter as _fm`, then rewrite `validate_page` to split frontmatter, run body checks on `body`, and append frontmatter advisories:

```python
def validate_page(content: str) -> list[dict]:
    meta, body = _fm.split(content)
    findings: list[dict] = []

    if _DEEP.search(body):
        findings.append({"type": "deep_heading", "severity": "block",
                         "text": "heading deeper than ## (###+); flatten to ##"})

    h2 = _H2.search(body)
    pre = body[:h2.start()] if h2 else body
    if any(ln.strip() and not _H1_LINE.match(ln) for ln in pre.splitlines()):
        findings.append({"type": "pre_h2_text", "severity": "block",
                         "text": "indexable text before the first ## (only a single # H1 allowed)"})

    secs = _sections(body)
    if not secs or secs[0][0].lower() != OVERVIEW_HEADING:
        findings.append({"type": "missing_overview", "severity": "advisory",
                         "text": "first ## section is not 'Overview'"})

    for heading, sbody in secs:
        lead = _lead(sbody)
        if not lead:
            findings.append({"type": "missing_lead", "severity": "advisory",
                             "text": f"section '{heading}' has no lead paragraph"})
        elif len(lead) > LEAD_MAX:
            findings.append({"type": "long_lead", "severity": "advisory",
                             "text": f"section '{heading}' lead exceeds {LEAD_MAX} chars"})

    if not meta.get("type"):
        findings.append({"type": "missing_type", "severity": "advisory",
                         "text": "frontmatter has no 'type' (run wiki_migrate_okf)"})
    elif meta["type"] not in _fm.OKF_TYPES:
        findings.append({"type": "unknown_type", "severity": "advisory",
                         "text": f"type '{meta['type']}' not in the OKF vocabulary"})
    if not meta.get("description"):
        findings.append({"type": "missing_description", "severity": "advisory",
                         "text": "frontmatter has no 'description'"})
    return findings
```

Note: the section-body loop variable is renamed to `sbody` to avoid shadowing the module-level `body`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_validate_frontmatter.py tests/ -k validate -v`
Expected: PASS; existing validate tests still pass.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/engine/validate.py tests/test_validate_frontmatter.py
git commit -m "feat(validate): body-only checks + advisory frontmatter findings"
```

---

## Task 6: `lint.py` — body-only checks + missing_frontmatter + tag_drift

**Files:**
- Modify: `src/iwiki_mcp/engine/lint.py`
- Test: `tests/test_lint_frontmatter.py`

**Interfaces:**
- Consumes: `frontmatter.split`, `frontmatter.normalize_tag`.
- Produces: `lint(...)` return dict gains `missing_frontmatter: list[str]` (page paths) and `tag_drift: list[dict]` (near-duplicate tag pairs). Existing keys unchanged. `_headings`/link parsing run on the body.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_lint_frontmatter.py
from iwiki_mcp.engine.lint import lint


def _wiki(tmp_path, pages):
    d = tmp_path / "d"
    (d / ".iwiki").mkdir(parents=True)
    for slug, text in pages.items():
        (d / f"{slug}.md").write_text(text, encoding="utf-8")
    return str(d)


def test_missing_frontmatter_reported(tmp_path):
    wiki = _wiki(tmp_path, {"a": "# A\n\n## Overview\ns\n\n## B\nx\n"})
    rep = lint(wiki)
    assert any("a.md" in p for p in rep["missing_frontmatter"])


def test_tag_drift_flags_near_duplicates(tmp_path):
    wiki = _wiki(tmp_path, {
        "a": "---\ntype: api\ntags: [config]\n---\n# A\n\n## Overview\ns\n\n## B\nx\n",
        "b": "---\ntype: api\ntags: [configs]\n---\n# B\n\n## Overview\ns\n\n## C\ny\n",
    })
    rep = lint(wiki)
    pairs = {tuple(sorted(d["tags"])) for d in rep["tag_drift"]}
    assert ("config", "configs") in pairs


def test_no_drift_for_distinct_tags(tmp_path):
    wiki = _wiki(tmp_path, {
        "a": "---\ntype: api\ntags: [config]\n---\n# A\n\n## Overview\ns\n\n## B\nx\n",
        "b": "---\ntype: api\ntags: [binding]\n---\n# B\n\n## Overview\ns\n\n## C\ny\n",
    })
    assert lint(wiki)["tag_drift"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_lint_frontmatter.py -v`
Expected: FAIL — keys `missing_frontmatter` / `tag_drift` absent.

- [ ] **Step 3: Write minimal implementation**

In `src/iwiki_mcp/engine/lint.py`, add `from . import frontmatter as _fm`. Add these helpers above `lint`:

```python
def _edit_distance_le1(a: str, b: str) -> bool:
    """True if a and b differ by at most one insert/delete/substitution."""
    if a == b:
        return False
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long = (a, b) if la < lb else (b, a)
    i = j = 0
    skipped = False
    while i < len(short) and j < len(long):
        if short[i] == long[j]:
            i += 1
            j += 1
        elif skipped:
            return False
        else:
            skipped = True
            j += 1
    return True


def _tag_drift(all_tags: set) -> list:
    tags = sorted(all_tags)
    out = []
    for i, a in enumerate(tags):
        for b in tags[i + 1:]:
            if a != b and (b.startswith(a) or a.startswith(b) or _edit_distance_le1(a, b)):
                out.append({"tags": [a, b]})
    return out
```

In `lint`, split frontmatter for the body-derived structures and build the two reports. Replace the `content`/`headings` construction:

```python
    raw = {p: _read(p) for p in pages}
    meta_body = {p: _fm.split(c) for p, c in raw.items()}
    content = {p: mb[1] for p, mb in meta_body.items()}   # body only
    headings = {p: _headings(c) for p, c in content.items()}

    missing_frontmatter = [p for p, (meta, _) in meta_body.items() if not meta]
    all_tags = set()
    for meta, _ in meta_body.values():
        for t in meta.get("tags", []) or []:
            all_tags.add(_fm.normalize_tag(t))
    all_tags.discard("")
```

(The broken-link / orphan / section loops already read from `content`, which is now the body — leave them unchanged.) Extend the return dict:

```python
    return {"wiki_present": True, "pages": len(pages),
            "broken": broken, "orphans": orphans, "stale": _stale(wiki_dir),
            "missing_source": _missing_source(wiki_dir, project_dir),
            "sections": sections,
            "missing_frontmatter": missing_frontmatter,
            "tag_drift": _tag_drift(all_tags)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_lint_frontmatter.py tests/ -k lint -v`
Expected: PASS; existing lint tests still pass.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/engine/lint.py tests/test_lint_frontmatter.py
git commit -m "feat(lint): report missing frontmatter and tag drift"
```

---

## Task 7: `retrieval.py` — faceted filters

**Files:**
- Modify: `src/iwiki_mcp/retrieval.py`
- Test: `tests/test_retrieval_facets.py`

**Interfaces:**
- Consumes: `Record.type`/`tags`.
- Produces: `_facet_ok(rtype, rtags, want_type, want_tags) -> bool`; `vector_search`, `lexical_search`, `hybrid_search` gain trailing kwargs `type: str | None = None`, `tags: list | None = None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_retrieval_facets.py
from iwiki_mcp.retrieval import _facet_ok


def test_facet_ok_type_and_tags():
    assert _facet_ok("api", ["a", "b"], None, None)
    assert _facet_ok("api", ["a"], "api", None)
    assert not _facet_ok("guide", ["a"], "api", None)
    assert _facet_ok("api", ["a", "b"], None, ["b"])
    assert not _facet_ok("api", ["a"], None, ["z"])
    assert not _facet_ok(None, [], "api", None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_retrieval_facets.py -v`
Expected: FAIL — `_facet_ok` not defined.

- [ ] **Step 3: Write minimal implementation**

In `src/iwiki_mcp/retrieval.py` add the helper after the imports:

```python
def _facet_ok(rtype, rtags, want_type, want_tags) -> bool:
    if want_type is not None and rtype != want_type:
        return False
    if want_tags and not (set(want_tags) & set(rtags or [])):
        return False
    return True


def _hit_facets(base, domain, file):
    from .engine import frontmatter as fm
    path = os.path.join(domain_dir(base, domain), file)
    try:
        meta, _ = fm.split(open(path, encoding="utf-8").read())
    except OSError:
        return None, []
    return meta.get("type"), fm.normalize_tags(meta.get("tags", []) or [])
```

Add `import os` at the top if not present. Thread facet kwargs through the three functions. `vector_search` filters records before scoring:

```python
def vector_search(cfg, base, domains, query, top_k, threshold,
                  type=None, tags=None):
    if top_k <= 0 or not domains:
        return []
    qv = np.asarray(embed_texts(cfg, [query])[0], dtype=np.float32)
    qnorm = float(np.linalg.norm(qv)) or 1.0
    hits = []
    for d in domains:
        recs = [r for r in VectorStore(index_path(base, d)).load()
                if r.dim == qv.size and _facet_ok(r.type, r.tags, type, tags)]
        if not recs:
            continue
        mat = np.asarray([dequantize(r.scale, r.q) for r in recs], dtype=np.float32)
        norms = np.linalg.norm(mat, axis=1)
        norms[norms == 0] = 1.0
        sims = (mat @ qv) / (norms * qnorm)
        for r, s in zip(recs, sims):
            if s >= threshold:
                hits.append({"domain": d, "file": r.file, "heading": r.heading,
                             "chunk": r.chunk, "score": round(float(s), 4),
                             "hit": "vector"})
    hits.sort(key=lambda h: (-h["score"], h["domain"], h["file"],
                             h["heading"], h["chunk"]))
    return hits[:top_k]


def lexical_search(base, domains, query, top_k, type=None, tags=None):
    if top_k <= 0:
        return []
    hits = []
    for d in domains:
        for h in grep_sections(domain_dir(base, d), query, top_k):
            if type is not None or tags:
                rt, rtags = _hit_facets(base, d, h["file"])
                if not _facet_ok(rt, rtags, type, tags):
                    continue
            hits.append({"domain": d, **h})
    hits.sort(key=lambda h: (-h["score"], h["domain"], h["file"], h["heading"]))
    return hits[:top_k]
```

In `hybrid_search`, add `type=None, tags=None` params and forward them:

```python
def hybrid_search(cfg, base, domains, query, top_k, threshold, mode="hybrid",
                  type=None, tags=None):
    if mode not in _VALID_MODES:
        raise ValueError(f"invalid search mode: {mode}")
    if top_k <= 0:
        return []
    vec = (vector_search(cfg, base, domains, query, top_k, threshold, type, tags)
           if mode in ("hybrid", "vector") else [])
    lex = (lexical_search(base, domains, query, top_k, type, tags)
           if mode in ("hybrid", "lexical") else [])
    # ... existing merge/sort body unchanged ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_retrieval_facets.py tests/ -k retrieval -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/retrieval.py tests/test_retrieval_facets.py
git commit -m "feat(retrieval): faceted filtering by type and tags"
```

---

## Task 8: `wiki_search` — expose type/tags

**Files:**
- Modify: `src/iwiki_mcp/server.py` (`wiki_search`)
- Test: `tests/test_server_search_facets.py`

**Interfaces:**
- Consumes: `retrieval.hybrid_search(..., type=, tags=)`.
- Produces: `wiki_search(query, scope, mode, domains, k, threshold, type=None, tags=None)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server_search_facets.py
import iwiki_mcp.server as server


def test_wiki_search_passes_facets(monkeypatch):
    captured = {}

    def fake_hybrid(cfg, base, doms, query, top_k, threshold, mode, type=None, tags=None):
        captured.update(type=type, tags=tags)
        return []

    monkeypatch.setattr(server.retrieval, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(server.base, "resolve_binding",
                        lambda: server.base.Binding(base="/b", read=("d",), write="d", project_dir="/p"))
    monkeypatch.setattr(server.base, "resolve_scope", lambda bind, scope, doms: ["d"])
    monkeypatch.setattr(server.Config, "load", staticmethod(lambda: object()))

    server.wiki_search("q", type="api", tags=["x"])
    assert captured == {"type": "api", "tags": ["x"]}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_server_search_facets.py -v`
Expected: FAIL — `wiki_search` has no `type`/`tags` params.

- [ ] **Step 3: Write minimal implementation**

Edit `wiki_search` in `server.py` to add the two params and forward them:

```python
@_safe
def wiki_search(
    query: str,
    scope: str = "project",
    mode: str = "hybrid",
    domains: list[str] | None = None,
    k: int | None = None,
    threshold: float | None = None,
    type: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    bind = base.resolve_binding()
    cfg = Config.load()
    doms = [_validate_domain(d) for d in base.resolve_scope(bind, scope, domains)]
    if not doms:
        return {"results": [], "hint": "no domains in scope"}
    results = retrieval.hybrid_search(
        cfg, bind.base, doms, query,
        top_k=cfg.top_k if k is None else k,
        threshold=cfg.score_threshold if threshold is None else threshold,
        mode=mode, type=type, tags=tags,
    )
    return {"results": results}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_server_search_facets.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/server.py tests/test_server_search_facets.py
git commit -m "feat(server): expose type/tags facets on wiki_search"
```

---

## Task 9: `config.py` — optional chat model

**Files:**
- Modify: `src/iwiki_mcp/engine/config.py:29-67`
- Test: `tests/test_config_chat.py`

**Interfaces:**
- Produces: `Config` gains `chat_model: str` from `IWIKI_CHAT_MODEL` (default `""` — empty means server-side classification is disabled).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_chat.py
from iwiki_mcp.engine.config import Config


def test_chat_model_default_empty(monkeypatch):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.delenv("IWIKI_CHAT_MODEL", raising=False)
    assert Config.load().chat_model == ""


def test_chat_model_override(monkeypatch):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setenv("IWIKI_CHAT_MODEL", "my-model")
    assert Config.load().chat_model == "my-model"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config_chat.py -v`
Expected: FAIL — `Config` has no `chat_model`.

- [ ] **Step 3: Write minimal implementation**

Add the field to the `Config` dataclass (after `embed_model`) and populate it in `load`:

```python
    embed_model: str
    chat_model: str
```

```python
            embed_model=getenv("IWIKI_EMBED_MODEL", "text-embedding-3-small"),
            chat_model=getenv("IWIKI_CHAT_MODEL", "").strip(),
```

Note: any test that constructs `Config(...)` positionally must add `chat_model`. The `_cfg()` helpers in the new tests use keywords. Grep for other direct `Config(` constructions in `tests/` and add `chat_model=""`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config_chat.py tests/ -k config -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/engine/config.py tests/test_config_chat.py
git commit -m "feat(config): add optional IWIKI_CHAT_MODEL"
```

---

## Task 10: `engine/classify.py` — optional best-effort chat classifier

**Files:**
- Create: `src/iwiki_mcp/engine/classify.py`
- Test: `tests/test_classify.py`

**Interfaces:**
- Consumes: `Config` (`base_url`, `api_key`, `chat_model`), `frontmatter.OKF_TYPES`/`coerce_type`/`normalize_tags`.
- Produces: `classify_page(cfg, body: str, existing_tags: list) -> dict` returning `{"type": <OKF type>, "tags": [...], "warning": str | None}`. Best-effort: any HTTP/parse failure returns `{"type": DEFAULT_TYPE, "tags": [], "warning": "classification unavailable: ..."}`. Only ever called when `cfg.chat_model` is truthy (the caller gates it).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_classify.py
import iwiki_mcp.engine.classify as classify
from iwiki_mcp.engine.config import Config


def _cfg():
    return Config(base_url="http://x", api_key="k", embed_model="e", chat_model="c",
                  dimensions=2, chunk_size=512, chunk_overlap=64, summary_max=400,
                  top_k=8, score_threshold=0.2, graph_depth=2, ignore=None)


def test_classify_parses_and_governs(monkeypatch):
    monkeypatch.setattr(classify, "_chat", lambda cfg, prompt: '{"type": "api", "tags": ["Config", "config"]}')
    out = classify.classify_page(_cfg(), "body", existing_tags=[])
    assert out["type"] == "api"
    assert out["tags"] == ["config"]          # normalized + deduped
    assert out["warning"] is None


def test_classify_offvocab_falls_back(monkeypatch):
    monkeypatch.setattr(classify, "_chat", lambda cfg, prompt: '{"type": "nonsense", "tags": []}')
    out = classify.classify_page(_cfg(), "body", existing_tags=[])
    assert out["type"] == "concept"


def test_classify_failure_is_best_effort(monkeypatch):
    def boom(cfg, prompt):
        raise RuntimeError("endpoint down")
    monkeypatch.setattr(classify, "_chat", boom)
    out = classify.classify_page(_cfg(), "body", existing_tags=[])
    assert out["type"] == "concept"
    assert out["tags"] == []
    assert "classification unavailable" in out["warning"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_classify.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# src/iwiki_mcp/engine/classify.py
"""Optional server-side page classification via an OpenAI-compatible chat
endpoint. Fills the governed ``type``/``tags`` frontmatter when IWIKI_CHAT_MODEL
is configured. Any failure degrades to the default type with no tags and a
warning — classification must never fail a write.
"""
from __future__ import annotations
import json
import httpx
from .config import Config
from . import frontmatter as fm

_TIMEOUT = 60.0
_PROMPT = """You classify a documentation page.

Return ONLY compact JSON: {{"type": "<one-of>", "tags": ["...", ...]}}.

type MUST be exactly one of: {types}.
Pick by the dominant intent:
- architecture: system structure, components, data flow, modules
- api: a call/interface surface — functions, endpoints, signatures
- guide: how to do something — step-by-step, usage
- reference: lookup material — tables of keys, flags, configs
- runbook: operational procedure — deploy, incident steps
- concept: explains an idea/model (default when unsure)

tags: up to {max_tags} short lowercase topic tags. PREFER reusing an existing
tag from this list when one fits; only coin a new tag if none match:
{existing}

PAGE:
{body}
"""


def _chat(cfg: Config, prompt: str) -> str:
    url = f"{cfg.base_url}/chat/completions"
    payload = {"model": cfg.chat_model,
               "messages": [{"role": "user", "content": prompt}],
               "temperature": 0}
    headers = {"Authorization": f"Bearer {cfg.api_key}"}
    resp = httpx.post(url, json=payload, headers=headers, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def classify_page(cfg: Config, body: str, existing_tags: list) -> dict:
    prompt = _PROMPT.format(
        types=", ".join(fm.OKF_TYPES), max_tags=fm.MAX_TAGS,
        existing=", ".join(existing_tags) or "(none yet)", body=body[:6000],
    )
    try:
        raw = _chat(cfg, prompt)
        data = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
        return {"type": fm.coerce_type(data.get("type")),
                "tags": fm.normalize_tags(data.get("tags", []) or []),
                "warning": None}
    except Exception as e:
        return {"type": fm.DEFAULT_TYPE, "tags": [],
                "warning": f"classification unavailable: {e}"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_classify.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/engine/classify.py tests/test_classify.py
git commit -m "feat(classify): optional best-effort chat classifier for type/tags"
```

---

## Task 11: `okf.py` + write path — build + write frontmatter

**Files:**
- Create: `src/iwiki_mcp/okf.py`
- Modify: `src/iwiki_mcp/server.py` (`wiki_write_page`, `wiki_update_page`)
- Test: `tests/test_server_write_frontmatter.py`

**Interfaces:**
- Consumes: `frontmatter` (split/render/derive/normalize/coerce), `classify.classify_page`, `store.VectorStore`, `base.index_path`.
- Produces:
  - `okf.git_last_commit_date(base_dir: str, path: str) -> str | None`
  - `okf.domain_tag_vocab(base_dir: str, domain: str) -> list`
  - `okf.build_frontmatter(cfg, base_dir, domain, slug, body, *, source, explicit_type, explicit_tags, timestamp_path) -> tuple[str, str | None]` → `(frontmatter_block, warning)` following the precedence: explicit params → server-side classify (`cfg.chat_model`) → default `concept`.
  - `wiki_write_page(domain, slug, markdown, source=None, type=None, tags=None)` writes `frontmatter + body`.
  - `wiki_update_page` preserves existing `type`/`tags`, refreshes `description`/`timestamp`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server_write_frontmatter.py
import iwiki_mcp.server as server
import iwiki_mcp.indexer as indexer
from iwiki_mcp.engine import frontmatter as fm


def _bind(tmp_path):
    (tmp_path / "d" / ".iwiki").mkdir(parents=True)
    return server.base.Binding(base=str(tmp_path), read=("d",), write="d",
                               project_dir=str(tmp_path))


def _patch(monkeypatch, tmp_path):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setattr(server.base, "resolve_binding", lambda: _bind(tmp_path))
    monkeypatch.setattr(server.sync, "ensure_fresh", lambda b: {"state": "clean"})
    monkeypatch.setattr(server.sync, "commit_and_push", lambda *a, **k: {"committed": True, "pushed": False})
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, texts: [[0.1, 0.2] for _ in texts])


def test_write_with_explicit_type_and_tags(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    body = "# Base binding\n\n## Overview\nHow binding works.\n\n## Detail\nwords here\n"
    res = server.wiki_write_page("d", "base", body, source=None, type="api", tags=["Binding"])
    assert "error" not in res
    meta, rest = fm.split((tmp_path / "d" / "base.md").read_text(encoding="utf-8"))
    assert meta["type"] == "api"
    assert meta["title"] == "Base binding"
    assert meta["description"].startswith("How binding works")
    assert meta["tags"] == ["binding"]          # normalized
    assert rest.startswith("# Base binding")


def test_write_without_type_and_no_chat_model_defaults_concept(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)   # IWIKI_CHAT_MODEL unset -> default path
    body = "# T\n\n## Overview\nsumm\n\n## B\nwords\n"
    res = server.wiki_write_page("d", "p", body, source=None)
    meta, _ = fm.split((tmp_path / "d" / "p.md").read_text(encoding="utf-8"))
    assert meta["type"] == "concept"
    assert "warning" in res


def test_write_without_type_uses_server_classifier_when_configured(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    monkeypatch.setenv("IWIKI_CHAT_MODEL", "chat-x")
    from iwiki_mcp import okf
    monkeypatch.setattr(okf.classify, "classify_page",
                        lambda cfg, body, existing_tags: {"type": "guide", "tags": ["x"], "warning": None})
    body = "# T\n\n## Overview\nsumm\n\n## B\nwords\n"
    server.wiki_write_page("d", "q", body, source=None)
    meta, _ = fm.split((tmp_path / "d" / "q.md").read_text(encoding="utf-8"))
    assert meta["type"] == "guide"
    assert meta["tags"] == ["x"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_server_write_frontmatter.py -v`
Expected: FAIL — `iwiki_mcp.okf` missing / `wiki_write_page` has no `type`/`tags` params / no frontmatter written.

- [ ] **Step 3: Write minimal implementation**

Create `src/iwiki_mcp/okf.py`:

```python
# src/iwiki_mcp/okf.py
"""Top-layer OKF frontmatter assembly: deterministic field derivation plus the
type/tags precedence (explicit params -> optional server-side classify ->
default). Kept out of the engine because it reaches git and the index."""
from __future__ import annotations
import datetime as _dt
import subprocess

from .engine import classify, frontmatter as fm
from .engine.store import VectorStore
from . import base as _base


def git_last_commit_date(base_dir: str, path: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", base_dir, "log", "-1", "--format=%cs", "--", path],
            capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:
        return None


def domain_tag_vocab(base_dir: str, domain: str) -> list:
    seen: list = []
    for r in VectorStore(_base.index_path(base_dir, domain)).load():
        for t in r.tags or []:
            if t not in seen:
                seen.append(t)
    return seen


def build_frontmatter(cfg, base_dir, domain, slug, body, *, source,
                      explicit_type, explicit_tags, timestamp_path):
    """Return (frontmatter_block, warning). Precedence: explicit -> classify -> default."""
    warning = None
    if explicit_type is not None:
        mtype = fm.coerce_type(explicit_type)
        mtags = fm.normalize_tags(explicit_tags or [])
    elif cfg.chat_model:
        r = classify.classify_page(cfg, body, domain_tag_vocab(base_dir, domain))
        mtype, mtags, warning = r["type"], r["tags"], r["warning"]
    else:
        mtype = fm.DEFAULT_TYPE
        mtags = fm.normalize_tags(explicit_tags or [])
        warning = "type not given and IWIKI_CHAT_MODEL unset; defaulted to concept"

    meta: dict = {"type": mtype, "title": fm.derive_title(body, slug)}
    desc = fm.derive_description(body, cfg.summary_max)
    if desc:
        meta["description"] = desc
    if source:
        meta["resource"] = source
    if mtags:
        meta["tags"] = mtags
    meta["timestamp"] = (git_last_commit_date(base_dir, timestamp_path)
                         or _dt.date.today().isoformat())
    return fm.render(meta), warning
```

In `server.py`, add `okf` to the `from . import ...` line and `from .engine import frontmatter as _fm`. Change `wiki_write_page` signature and body. Keep every check up to the existing `cfg = Config.load()`, then:

```python
@_safe
def wiki_write_page(
    domain: str, slug: str, markdown: str, source: str | None = None,
    type: str | None = None, tags: list[str] | None = None,
) -> dict:
    # ... unchanged: bind/fresh/domain/blocking/ignore/path/exists checks ...
    cfg = Config.load()
    page_file = PurePosixPath(*_slug_parts(slug)).as_posix() + ".md"
    fm_block, warn = okf.build_frontmatter(
        cfg, bind.base, valid_domain, slug, markdown,
        source=source, explicit_type=type, explicit_tags=tags,
        timestamp_path=f"{valid_domain}/{page_file}")
    full_md = fm_block + markdown
    log_source = source or ""
    log_src_hash = indexer.src_hash(source) if source else None
    log_appended = False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(full_md)
        indexer.append_log(bind.base, valid_domain, "ingest", log_source, page_file, log_src_hash)
        log_appended = True
        stats = indexer.index_domain(cfg, bind.base, valid_domain)
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        if log_appended:
            _rollback_last_log(bind.base, valid_domain, "ingest", page_file, log_source, log_src_hash)
        raise
    page_rel = f"{valid_domain}/{page_file}"
    commit = sync.commit_and_push(bind.base, f"iwiki: ingest {page_rel}", pathspec=valid_domain)
    result = {
        "page": page_rel,
        "indexed_chunks": stats["indexed_chunks"],
        "bytes": stats["bytes"],
        "over_cap": stats["over_cap"],
        "committed": commit.get("committed", False),
        "pushed": commit.get("pushed", False),
        **_fresh_warn(fresh),
    }
    if warn:
        result.setdefault("warning", warn)
    return result
```

For `wiki_update_page`, the on-disk file now begins with frontmatter. Split it, run `replace_section` on the body, refresh `description`/`timestamp`, preserve `type`/`tags`. Compute `page_file` early, then replace the body-building block:

```python
    page_file = PurePosixPath(*_slug_parts(slug)).as_posix() + ".md"
    original_full = open(path, encoding="utf-8").read()
    meta, original_body = _fm.split(original_full)
    try:
        new_body = replace_section(original_body, heading, new_body)
    except SectionError as e:
        return {"error": str(e), "hint": "check the heading with wiki_read_page"}
    blocking = [f for f in validate_page(new_body) if f.get("type") in _BLOCKING]
    if blocking:
        return {"error": "section structure invalid", "findings": blocking,
                "hint": "new_body must use only ## headings; no ###+, no pre-## text"}
    cfg = Config.load()
    if meta:
        desc = _fm.derive_description(new_body, cfg.summary_max)
        if desc:
            meta["description"] = desc
        meta["timestamp"] = (okf.git_last_commit_date(bind.base, f"{valid_domain}/{page_file}")
                             or __import__("datetime").date.today().isoformat())
        new_md = _fm.render(meta) + new_body
    else:
        new_md = new_body
```

The existing write/index/rollback block already writes `new_md`; leave it. Ensure the earlier `page_file` assignment (further down in the original) is removed to avoid a duplicate.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_server_write_frontmatter.py tests/test_server_write.py tests/test_server_update.py -v`
Expected: PASS. If any pre-existing write/update test asserts the on-disk file equals the raw body, update it to `fm.split` first (frontmatter is now prepended).

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/okf.py src/iwiki_mcp/server.py tests/test_server_write_frontmatter.py
git commit -m "feat(server): write governed OKF frontmatter on create/update"
```

---

## Task 12: `wiki_migrate_okf` (dual-mode) + `wiki_apply_okf`

**Files:**
- Modify: `src/iwiki_mcp/server.py` (two new handlers + registration)
- Test: `tests/test_server_migrate.py`

**Interfaces:**
- Consumes: `okf.build_frontmatter`, `okf.domain_tag_vocab`, `frontmatter.split`/`derive_title`/`derive_description`, `indexer.index_domain`.
- Produces:
  - `wiki_migrate_okf(domain=None)`:
    - `cfg.chat_model` set → autonomous: classify + write frontmatter for every page lacking it; returns `{"domain", "mode": "autonomous", "migrated", "skipped", "warnings"}`.
    - unset → plan: `{"domain", "mode": "plan", "candidates": [{slug, body, derived, tag_vocab}], "type_vocabulary", "authoring_rules", "next_steps"}`, no writes.
  - `wiki_apply_okf(domain, slug, type, tags=None)`: derive deterministic fields, clamp `type`, normalize `tags`, write frontmatter, re-index. Returns `{"page", "type", "tags", ...}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server_migrate.py
import iwiki_mcp.server as server
import iwiki_mcp.indexer as indexer
from iwiki_mcp.engine import frontmatter as fm


def _bind(tmp_path):
    (tmp_path / "d" / ".iwiki").mkdir(parents=True)
    return server.base.Binding(base=str(tmp_path), read=("d",), write="d",
                               project_dir=str(tmp_path))


def _patch(monkeypatch, tmp_path):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setattr(server.base, "resolve_binding", lambda: _bind(tmp_path))
    monkeypatch.setattr(server.sync, "ensure_fresh", lambda b: {"state": "clean"})
    monkeypatch.setattr(server.sync, "commit_and_push", lambda *a, **k: {"committed": True, "pushed": False})
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, texts: [[0.1, 0.2] for _ in texts])


def test_migrate_plan_mode_lists_candidates(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)   # no IWIKI_CHAT_MODEL
    (tmp_path / "d" / "a.md").write_text("# A\n\n## Overview\ns\n\n## B\nwords\n", encoding="utf-8")
    res = server.wiki_migrate_okf("d")
    assert res["mode"] == "plan"
    slugs = [c["slug"] for c in res["candidates"]]
    assert "a" in slugs
    assert (tmp_path / "d" / "a.md").read_text(encoding="utf-8").startswith("# A")  # no write


def test_apply_okf_writes_frontmatter(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    (tmp_path / "d" / "a.md").write_text("# A\n\n## Overview\ns\n\n## B\nwords\n", encoding="utf-8")
    res = server.wiki_apply_okf("d", "a", "guide", tags=["Flow"])
    assert "error" not in res
    meta, _ = fm.split((tmp_path / "d" / "a.md").read_text(encoding="utf-8"))
    assert meta["type"] == "guide"
    assert meta["tags"] == ["flow"]


def test_migrate_autonomous_mode(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    monkeypatch.setenv("IWIKI_CHAT_MODEL", "chat-x")
    from iwiki_mcp import okf
    monkeypatch.setattr(okf.classify, "classify_page",
                        lambda cfg, body, existing_tags: {"type": "guide", "tags": ["x"], "warning": None})
    (tmp_path / "d" / "a.md").write_text("# A\n\n## Overview\ns\n\n## B\nwords\n", encoding="utf-8")
    res = server.wiki_migrate_okf("d")
    assert res["mode"] == "autonomous"
    assert "a" in res["migrated"]
    meta, _ = fm.split((tmp_path / "d" / "a.md").read_text(encoding="utf-8"))
    assert meta["type"] == "guide"
    # idempotent
    res2 = server.wiki_migrate_okf("d")
    assert res2["migrated"] == [] and "a" in res2["skipped"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_server_migrate.py -v`
Expected: FAIL — `wiki_migrate_okf` / `wiki_apply_okf` not defined.

- [ ] **Step 3: Write minimal implementation**

Add both handlers to `server.py` and register them. Helper to iterate migratable pages:

```python
def _unmigrated_pages(dom_path):
    """Yield (slug, page_file, body) for pages lacking frontmatter."""
    for path in sorted(dom_path.rglob("*.md")):
        rel = path.relative_to(dom_path)
        if ".iwiki" in rel.parts:
            continue
        meta, body = _fm.split(path.read_text(encoding="utf-8"))
        yield rel.with_suffix("").as_posix(), rel.as_posix(), body, bool(meta)


@_safe
def wiki_migrate_okf(domain: str | None = None) -> dict:
    bind = base.resolve_binding()
    target = _validate_domain(domain or bind.write or "")
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    dom_path = _domain_path(bind.base, target)
    if not dom_path.is_dir():
        return {"error": f"domain '{target}' not found",
                "hint": "create it with wiki_create_domain"}
    cfg = Config.load()
    if cfg.chat_model:
        migrated, skipped, warnings = [], [], []
        for slug, page_file, body, has_fm in _unmigrated_pages(dom_path):
            if has_fm:
                skipped.append(slug)
                continue
            fm_block, warn = okf.build_frontmatter(
                cfg, bind.base, target, slug, body,
                source=None, explicit_type=None, explicit_tags=None,
                timestamp_path=f"{target}/{page_file}")
            (dom_path / page_file).write_text(fm_block + body, encoding="utf-8")
            migrated.append(slug)
            if warn:
                warnings.append({"slug": slug, "warning": warn})
        stats = indexer.index_domain(cfg, bind.base, target)
        commit = sync.commit_and_push(bind.base, f"iwiki: migrate okf {target}", pathspec=target)
        return {"domain": target, "mode": "autonomous", "migrated": migrated,
                "skipped": skipped, "warnings": warnings,
                "indexed_chunks": stats["indexed_chunks"],
                "committed": commit.get("committed", False),
                "pushed": commit.get("pushed", False), **_fresh_warn(fresh)}
    # plan mode: no writes
    vocab = okf.domain_tag_vocab(bind.base, target)
    candidates = []
    for slug, page_file, body, has_fm in _unmigrated_pages(dom_path):
        if has_fm:
            continue
        candidates.append({
            "slug": slug,
            "body": body,
            "derived": {
                "title": _fm.derive_title(body, slug),
                "description": _fm.derive_description(body, cfg.summary_max),
                "timestamp": okf.git_last_commit_date(bind.base, f"{target}/{page_file}"),
            },
            "tag_vocab": vocab,
            "recommended_tools": ["wiki_apply_okf"],
        })
    return {"domain": target, "mode": "plan", "candidates": candidates,
            "type_vocabulary": list(_fm.OKF_TYPES),
            "authoring_rules": AUTHORING_RULES,
            "next_steps": ["Classify each candidate's type (from type_vocabulary) "
                           "and tags (reuse tag_vocab first), then call "
                           "wiki_apply_okf(domain, slug, type, tags).",
                           "Run wiki_lint to confirm no missing_frontmatter remains."],
            **_fresh_warn(fresh)}


@_safe
def wiki_apply_okf(domain: str, slug: str, type: str,
                   tags: list[str] | None = None) -> dict:
    bind = base.resolve_binding()
    valid_domain = _validate_domain(domain)
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    dom_path = _domain_path(bind.base, valid_domain)
    if not dom_path.is_dir():
        return {"error": f"domain '{valid_domain}' not found",
                "hint": "create it with wiki_create_domain"}
    path = _page_path(bind.base, valid_domain, slug)
    if not os.path.isfile(path):
        return {"error": f"page '{valid_domain}/{slug}' not found",
                "hint": "list pages with wiki_list_pages"}
    cfg = Config.load()
    page_file = PurePosixPath(*_slug_parts(slug)).as_posix() + ".md"
    _, body = _fm.split(open(path, encoding="utf-8").read())
    fm_block, _ = okf.build_frontmatter(
        cfg, bind.base, valid_domain, slug, body,
        source=None, explicit_type=type, explicit_tags=tags,
        timestamp_path=f"{valid_domain}/{page_file}")
    original = open(path, encoding="utf-8").read()
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(fm_block + body)
        stats = indexer.index_domain(cfg, bind.base, valid_domain)
    except Exception:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(original)
        raise
    page_rel = f"{valid_domain}/{page_file}"
    commit = sync.commit_and_push(bind.base, f"iwiki: apply okf {page_rel}", pathspec=valid_domain)
    meta, _ = _fm.split(fm_block + body)
    return {"page": page_rel, "type": meta.get("type"), "tags": meta.get("tags", []),
            "indexed_chunks": stats["indexed_chunks"],
            "committed": commit.get("committed", False),
            "pushed": commit.get("pushed", False), **_fresh_warn(fresh)}
```

Register both near the bottom:

```python
mcp.tool()(wiki_migrate_okf)
mcp.tool()(wiki_apply_okf)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_server_migrate.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/server.py tests/test_server_migrate.py
git commit -m "feat(server): add dual-mode wiki_migrate_okf and wiki_apply_okf"
```

---

## Task 13: `wiki_export_okf` — conformant bundle

**Files:**
- Create: `src/iwiki_mcp/export.py`
- Modify: `src/iwiki_mcp/server.py` (new handler + registration)
- Test: `tests/test_export_okf.py`

**Interfaces:**
- Consumes: `frontmatter.split`/`render`, the ingest log at `.iwiki/log.jsonl`.
- Produces:
  - `convert_wikilinks(body: str) -> str` — `[[t#H]]`→`[H](t.md)`, `[[t|a]]`→`[a](t.md)`, `[[t]]`→`[t](t.md)`.
  - `export_domain(dom_path: str, dest: str) -> dict` — copies pages (frontmatter preserved, links converted), writes `index.md` + `log.md`; returns `{"pages", "dest"}`.
  - `wiki_export_okf(domain: str, dest: str) -> dict`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_export_okf.py
from iwiki_mcp.export import convert_wikilinks, export_domain


def test_convert_wikilinks_forms():
    assert convert_wikilinks("see [[base#Purpose]]") == "see [Purpose](base.md)"
    assert convert_wikilinks("see [[base|the base]]") == "see [the base](base.md)"
    assert convert_wikilinks("see [[base]]") == "see [base](base.md)"
    assert convert_wikilinks("sub [[a/b#H]]") == "sub [H](a/b.md)"


def test_export_writes_bundle(tmp_path):
    dom = tmp_path / "d"
    (dom / ".iwiki").mkdir(parents=True)
    (dom / ".iwiki" / "log.jsonl").write_text(
        '{"op":"ingest","page":"a.md","source":"a.py","date":"2026-07-01"}\n', encoding="utf-8")
    (dom / "a.md").write_text(
        "---\ntype: api\n---\n# A\n\n## Overview\ns\n\n## B\nsee [[a#B]]\n", encoding="utf-8")
    dest = tmp_path / "out"
    res = export_domain(str(dom), str(dest))
    exported = (dest / "a.md").read_text(encoding="utf-8")
    assert "[B](a.md)" in exported          # wikilink converted
    assert "type: api" in exported          # frontmatter preserved
    assert (dest / "index.md").exists()
    assert (dest / "log.md").exists()
    assert res["pages"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_export_okf.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# src/iwiki_mcp/export.py
"""Serialize a domain into a fully OKF-conformant bundle: standard markdown
links plus reserved index.md / log.md. Sources are never mutated — only copies."""
from __future__ import annotations
import json
import os
import re

from .engine import frontmatter as fm

_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#([^\]|]+))?(?:\|([^\]]+))?\]\]")


def convert_wikilinks(body: str) -> str:
    def repl(m):
        target, heading, alias = m.group(1).strip(), m.group(2), m.group(3)
        text = (alias or heading or target).strip()
        return f"[{text}]({target}.md)"
    return _WIKILINK.sub(repl, body)


def _pages(dom_path: str) -> list:
    out = []
    for root, _, files in os.walk(dom_path):
        if ".iwiki" in root.split(os.sep):
            continue
        for f in files:
            if f.endswith(".md"):
                out.append(os.path.relpath(os.path.join(root, f), dom_path))
    return sorted(out)


def _read_log(dom_path: str) -> list:
    path = os.path.join(dom_path, ".iwiki", "log.jsonl")
    recs = []
    if os.path.isfile(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line:
                try:
                    recs.append(json.loads(line))
                except Exception:
                    pass
    return recs


def export_domain(dom_path: str, dest: str) -> dict:
    rels = _pages(dom_path)
    for rel in rels:
        meta, body = fm.split(open(os.path.join(dom_path, rel), encoding="utf-8").read())
        block = fm.render(meta) if meta else ""
        out_path = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(block + convert_wikilinks(body))
    os.makedirs(dest, exist_ok=True)
    index = "# Index\n\n" + "".join(
        f"- [{rel[:-3]}]({rel})\n" for rel in rels if rel not in ("index.md", "log.md"))
    with open(os.path.join(dest, "index.md"), "w", encoding="utf-8") as fh:
        fh.write(index)
    log_md = "# Log\n\n" + "".join(
        f"- {r.get('date','')} {r.get('op','')} {r.get('page','')}\n" for r in _read_log(dom_path))
    with open(os.path.join(dest, "log.md"), "w", encoding="utf-8") as fh:
        fh.write(log_md)
    return {"pages": len(rels), "dest": dest}
```

Add the handler to `server.py` and register it:

```python
@_safe
def wiki_export_okf(domain: str, dest: str) -> dict:
    from . import export
    bind = base.resolve_binding()
    valid_domain = _validate_domain(domain)
    dom_path = _domain_path(bind.base, valid_domain)
    if not dom_path.is_dir():
        return {"error": f"domain '{valid_domain}' not found",
                "hint": "create it with wiki_create_domain"}
    if not dest:
        return {"error": "dest is required", "hint": "pass an output directory path"}
    result = export.export_domain(str(dom_path), os.path.abspath(os.path.expanduser(dest)))
    return {"domain": valid_domain, **result}
```

```python
mcp.tool()(wiki_export_okf)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_export_okf.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/export.py src/iwiki_mcp/server.py tests/test_export_okf.py
git commit -m "feat(server): add wiki_export_okf conformant bundle export"
```

---

## Task 14: docs, authoring rules, version bump

**Files:**
- Modify: `src/iwiki_mcp/resources.py`
- Modify: `README.md`, `docs/README.ru.md`
- Modify: `pyproject.toml`
- Test: `tests/test_resources_frontmatter.py`

**Interfaces:**
- Produces: `AUTHORING_RULES` mentions the frontmatter fields + type rubric; version is `0.2.0`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_resources_frontmatter.py
from iwiki_mcp.resources import AUTHORING_RULES


def test_authoring_rules_mention_frontmatter_and_types():
    assert "frontmatter" in AUTHORING_RULES.lower()
    for t in ("architecture", "api", "guide", "reference", "runbook", "concept"):
        assert t in AUTHORING_RULES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_resources_frontmatter.py -v`
Expected: FAIL — rules don't mention frontmatter/types.

- [ ] **Step 3: Write minimal implementation**

Append an OKF-frontmatter section to `AUTHORING_RULES` in `resources.py` (before the closing `"""`):

```python
## OKF frontmatter

- Every page carries a YAML frontmatter block above the `# Title` H1. The write
  tools fill it; you rarely hand-author it. Fields: `type` (required), `title`,
  `description`, `resource`, `tags`, `timestamp`.
- `type` MUST be one of the closed vocabulary — `architecture`, `api`, `guide`,
  `reference`, `runbook`, `concept` (default). Pick by dominant intent:
  architecture = structure/data flow; api = call surface; guide = how-to;
  reference = lookup tables; runbook = ops procedure; concept = an idea/model.
- `tags` are lowercase kebab-case, <=5 per page; reuse an existing domain tag
  before coining a new one.
```

Also update the pre-`##` rule line to allow the frontmatter block ("Put no content before the first `##` except the frontmatter block and a single `# Title` H1").

Bump `[project] version` in `pyproject.toml` from `0.1.x` to `0.2.0`.

Add a short "OKF compatibility" subsection to `README.md`: the new tools (`wiki_migrate_okf`, `wiki_apply_okf`, `wiki_export_okf`), faceted `wiki_search` (`type=`/`tags=`), the frontmatter schema, and the optional `IWIKI_CHAT_MODEL` env var (with the precedence: explicit params → server model → default). Mirror the same content in `docs/README.ru.md` (Russian).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_resources_frontmatter.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/resources.py README.md docs/README.ru.md pyproject.toml tests/test_resources_frontmatter.py
git commit -m "docs: document OKF frontmatter, tools, env, and bump to 0.2.0"
```

---

## Task 15: full suite + PreToolUse hook mirror note

**Files:**
- Verify: whole `tests/` suite
- Modify (if changed): `docs/TODO.md`

- [ ] **Step 1: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS. Fix any pre-existing test that asserted exact page bytes (now frontmatter-prefixed) by splitting frontmatter first, and any positional `Config(...)` construction missing `chat_model`.

- [ ] **Step 2: Re-verify the new surfaces together**

Run: `uv run pytest tests/test_server_write_frontmatter.py tests/test_server_migrate.py tests/test_export_okf.py tests/test_classify.py -v`
Expected: PASS.

- [ ] **Step 3: Record the hook follow-up**

The `iwiki-validate` PreToolUse hook referenced in `validate.py`'s docstring lives outside this repo. Its `pre_h2_text` mirror must strip frontmatter before checking (same as Task 5). Add a one-line note to `docs/TODO.md` under this topic's row so the follow-up is tracked. No code change here.

- [ ] **Step 4: Commit (if TODO.md changed)**

```bash
git add docs/TODO.md
git commit -m "docs(todo): note iwiki-validate hook frontmatter follow-up"
```

---

## Self-Review Notes

- **Spec coverage:** frontmatter schema (Tasks 1, 11), body-untouched processing (Tasks 2, 5, 6), governance type/tags (Tasks 1, 10, 11, 12), no-hard-model-binding precedence (Tasks 9, 10, 11), faceted retrieval (Tasks 3, 4, 7, 8), write path (Task 11), dual-mode backfill + apply (Task 12), export (Task 13), authoring rules/README/version (Task 14), testing (every task + Task 15). All spec sections map to a task.
- **Type consistency:** `classify_page`, `build_frontmatter`, `domain_tag_vocab`, `git_last_commit_date`, `coerce_type`, `normalize_tags`, `derive_title`, `derive_description`, `convert_wikilinks`, `export_domain`, `_facet_ok`, `chat_model` are used identically across tasks.
- **Ordering:** Tasks 1→10 are bottom-up (engine before server); Tasks 11-13 depend on 1/10; Tasks 14-15 finalize. Each task is independently testable.
- **Env:** the only new env var is `IWIKI_CHAT_MODEL` (optional, empty default); `IWIKI_LLM_BASE_URL`/`IWIKI_LLM_KEY` are reused for the chat endpoint.
```
