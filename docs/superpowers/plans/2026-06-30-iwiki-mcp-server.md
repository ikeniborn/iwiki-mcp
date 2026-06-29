---
chain:
  intent: null
  spec: docs/superpowers/specs/2026-06-30-iwiki-mcp-server-design.md
review:
  plan_hash: 4985fa4993586d63
  spec_hash: c01da1e37103f3d0
  last_run: 2026-06-30
  phases:
    structure:     { status: passed }
    coverage:      { status: passed }
    dependencies:  { status: passed }
    verifiability: { status: passed }
    consistency:   { status: passed }
  findings:
    - id: F-001
      phase: coverage
      severity: WARNING
      section: "Task 9 (main) / Task 4 (resolve_project_dir) / Global Constraints"
      section_hash: null
      fragment: "Project dir: process `cwd`, overridable by `IWIKI_PROJECT_DIR` / `--project DIR`."
      text: >-
        The spec (lines 174, 354) lists `--project DIR` as a supported override for
        the project directory alongside `IWIKI_PROJECT_DIR`. The plan mentions
        `--project DIR` only in Global Constraints prose; `resolve_project_dir`
        (Task 4) reads only the env var, and `main()` (Task 9) is `mcp.run()` with
        no argv/argparse handling. The CLI flag is never implemented.
      fix: >-
        Either add argparse handling in `main()` (and thread `--project` into
        `resolve_binding`), or drop `--project DIR` from the Global Constraints prose
        if env-only override is intended for v1.
      verdict: fixed
      verdict_at: 2026-06-30
      resolution: >-
        Verified in the new plan body: Task 9 `main()` now parses `--project` via
        argparse and writes it into `os.environ["IWIKI_PROJECT_DIR"]` before
        `mcp.run()`. Since every tool resolves the project dir through
        `resolve_project_dir` (which reads `IWIKI_PROJECT_DIR`), the flag propagates
        to all tool invocations. `import os` is present in the Task 9 imports.
    - id: F-002
      phase: coverage
      severity: INFO
      section: "Task 7 (retrieval.hybrid_search)"
      section_hash: null
      fragment: "merge → optional graph expansion (`related`). `wiki_search` takes a `mode`"
      text: >-
        The spec data-flow (lines 82, 146) describes hybrid search ending with an
        optional graph-expansion step (pull `related` neighbours of the top hit)
        inside `wiki_search`. The plan's `hybrid_search` does vector ∪ lexical merge
        only; graph access is provided as the separate `wiki_related` tool. The
        plan's Self-Review acknowledges this mapping (graph = `wiki_related`).
      fix: >-
        Acceptable as-is if intra-search graph expansion is deferred (spec marks it
        "optional"). If in-search expansion is intended, add a graph step to
        `hybrid_search` driven by `IWIKI_GRAPH_DEPTH`.
      verdict: open
      verdict_at: null
    - id: F-003
      phase: consistency
      severity: INFO
      section: "Task 10 (tests/test_server_search.py _seed)"
      section_hash: null
      fragment: 'monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", "2") if hasattr(monkeypatch, "setenv") else None'
      text: >-
        The `_seed` helper has a redundant ternary `setenv(...) if hasattr(...) else
        None` (line 1270) immediately followed by the same unconditional `setenv`
        call (line 1271). The `hasattr` guard is always true for a pytest
        monkeypatch fixture; the line is dead test cruft.
      fix: "Delete the line-1270 ternary; keep the unconditional setenv on line 1271."
      verdict: fixed
      verdict_at: 2026-06-30
      resolution: >-
        Verified in the new plan body: the `_seed` helper in Task 10 now has a
        single unconditional `monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", "2")`;
        the redundant `hasattr` ternary line is gone.
---
# iwiki MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone `iwiki-mcp` MCP server (stdio) that lets Codex and Claude Code build and query a shared, git-synced wiki base split into free-form domains.

**Architecture:** Reuse the existing `iwiki_engine` core (chunk/embed/store/search/related/lint/validate/links) as an in-process library under `src/iwiki_mcp/engine/`. New `iwiki_mcp` layer adds base/domain resolution, project binding (`.iwiki.toml`), domain-relative indexing, multi-domain numpy-merged vector search, a lexical (grep) path, hybrid retrieval, git sync, and a FastMCP tool surface. The agent authors page markdown; the server only stores/indexes/searches.

**Tech Stack:** Python ≥3.10, `mcp` (FastMCP), `httpx`, `pathspec`, `numpy`, `pytest`; `uv` for env/build; OpenAI-compatible embeddings.

**Spec:** `docs/superpowers/specs/2026-06-30-iwiki-mcp-server-design.md`

## Global Constraints

- Language for all code, comments, docstrings, commit messages, and docs: **English**.
- Embeddings via OpenAI-compatible endpoint; required env: `IWIKI_LLM_BASE_URL`, `IWIKI_LLM_KEY`; optional `IWIKI_EMBED_MODEL` (default `text-embedding-3-small`), `IWIKI_EMBED_DIMENSIONS` (default `1536`).
- Tuning env (defaults): `IWIKI_TOP_K=8`, `IWIKI_SCORE_THRESHOLD=0.2`, `IWIKI_GRAPH_DEPTH=2`, `IWIKI_CHUNK_SIZE=512`, `IWIKI_CHUNK_OVERLAP=64`, `IWIKI_SUMMARY_MAX_CHARS=400`.
- Base location: `IWIKI_BASE_DIR` env, overridable by `.iwiki.toml` `base`. Project dir: process `cwd`, overridable by `IWIKI_PROJECT_DIR` / `--project DIR`.
- Storage: per-domain JSONL at `<base>/<domain>/.iwiki/index.jsonl`; ingest log at `<base>/<domain>/.iwiki/log.jsonl`. The index `file` field is **domain-relative** (machine-portable).
- All MCP tools are **fail-soft**: catch exceptions and return `{"error": ..., "hint": ...}`; never crash the session.
- v1 limits: links/`related` resolve **within one domain**; vector search is numpy brute-force behind a `VectorStore` interface; no server-side LLM authoring; no deletion of existing pages (refuse and ask); stdio transport only.
- Package layout: source under `src/iwiki_mcp/`; engine ported under `src/iwiki_mcp/engine/` with imports rewritten from `iwiki_engine.*` to relative (`.`/`..`).
- Entry point: `iwiki-mcp = "iwiki_mcp.server:main"`.

---

### Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `src/iwiki_mcp/__init__.py`
- Create: `tests/__init__.py`
- Create: `.gitignore`

**Interfaces:**
- Consumes: nothing.
- Produces: an installable package `iwiki_mcp` and an `iwiki-mcp` console script stub; `pytest` runs.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "iwiki-mcp"
version = "0.1.0"
description = "MCP server for a shared, git-synced wiki knowledge base split into domains"
readme = "README.md"
requires-python = ">=3.10"
license = { text = "MIT" }
dependencies = [
    "mcp>=1.2.0",
    "httpx>=0.27",
    "pathspec>=0.12",
    "numpy>=1.26",
]

[project.scripts]
iwiki-mcp = "iwiki_mcp.server:main"

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/iwiki_mcp"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
```

- [ ] **Step 2: Create package init files**

`src/iwiki_mcp/__init__.py`:
```python
"""iwiki MCP server: a shared, git-synced wiki base split into domains."""
__version__ = "0.1.0"
```

`tests/__init__.py`: (empty file)

- [ ] **Step 3: Create `.gitignore`**

```gitignore
__pycache__/
*.pyc
.venv/
.pytest_cache/
dist/
build/
*.egg-info/
```

- [ ] **Step 4: Create the dev environment and verify it installs**

Run:
```bash
uv venv
uv pip install -e ".[dev]"
```
Expected: install succeeds; `numpy`, `httpx`, `pathspec`, `mcp`, `pytest` present.

- [ ] **Step 5: Verify pytest runs (no tests yet)**

Run: `uv run pytest -q`
Expected: `no tests ran` (exit 5) — confirms collection works.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/iwiki_mcp/__init__.py tests/__init__.py .gitignore
git commit -m "chore: scaffold iwiki-mcp package"
```

---

### Task 2: Port the engine core (with cwd-decoupled Config)

**Files:**
- Create: `src/iwiki_mcp/engine/__init__.py`
- Create: `src/iwiki_mcp/engine/{config,chunk,embed,store,search,related,lint,validate,links}.py` (ported)
- Create: `tests/engine/test_{chunk,embed,store,search,related,lint,validate,links,config}.py` (ported)
- Create: `tests/engine/__init__.py`

**Interfaces:**
- Consumes: nothing (self-contained engine).
- Produces:
  - `Config.load(load_ignore: bool = False) -> Config` with fields `base_url, api_key, embed_model, dimensions, chunk_size, chunk_overlap, summary_max, top_k, score_threshold, graph_depth, ignore`.
  - `chunk_markdown(file, content, size, overlap, summary_max=400) -> list[Chunk]`; `Chunk(file, heading, chunk, text, hash)` with `.id == f"{file}#{heading}"`.
  - `embed_texts(cfg, texts) -> list[list[float]]`; `EmbedError`.
  - `Record(id, file, heading, chunk, hash, dim, scale, q)`; `quantize`, `dequantize`, `cosine`, `make_record`, `load_index`, `save_index`, `index_bytes`.
  - `search(query_vec, recs, top_k, threshold) -> list[dict]` (`{id,file,heading,chunk,score}`).
  - `related(target_id, recs, top_k, graph_depth) -> {"vector": [...], "graph": [...]}`.
  - `lint(wiki_dir) -> dict`; `validate_page(content) -> list[dict]`; `parse_links(content) -> list[str]`.

- [ ] **Step 1: Copy engine modules verbatim, then rewrite the package name**

Copy these files from `/home/ikeniborn/Documents/Project/ai-wiki-plugin/engine/iwiki_engine/` into `src/iwiki_mcp/engine/`:
`config.py chunk.py embed.py store.py search.py related.py lint.py validate.py links.py`
Do **not** copy `__main__.py`. Create `src/iwiki_mcp/engine/__init__.py`:
```python
"""Ported iwiki engine core (chunk/embed/store/search/related/lint/validate/links)."""
```
The modules already use relative imports (`from .config import ...`), so no import rewrite is needed inside the package.

- [ ] **Step 2: Decouple `Config.load` from cwd**

In `src/iwiki_mcp/engine/config.py`, change the `load` signature and the ignore line so `.iwikiignore` is read only on request (base model does not read project cwd):

```python
    @staticmethod
    def load(load_ignore: bool = False) -> "Config":
        getenv = os.environ.get
        url_var, key_var = "IWIKI_LLM_BASE_URL", "IWIKI_LLM_KEY"
        base_url = getenv(url_var, "").strip()
        api_key = getenv(key_var, "").strip()
        if not base_url or not api_key:
            raise ConfigError(
                f"{url_var} and {key_var} must be set as environment variables. Halting."
            )
        if base_url.endswith("/"):
            base_url = base_url[:-1]
        return Config(
            base_url=base_url,
            api_key=api_key,
            embed_model=getenv("IWIKI_EMBED_MODEL", "text-embedding-3-small"),
            dimensions=int(getenv("IWIKI_EMBED_DIMENSIONS", "1536")),
            chunk_size=int(getenv("IWIKI_CHUNK_SIZE", "512")),
            chunk_overlap=int(getenv("IWIKI_CHUNK_OVERLAP", "64")),
            summary_max=int(getenv("IWIKI_SUMMARY_MAX_CHARS", "400")),
            top_k=int(getenv("IWIKI_TOP_K", "8")),
            score_threshold=float(getenv("IWIKI_SCORE_THRESHOLD", "0.2")),
            graph_depth=int(getenv("IWIKI_GRAPH_DEPTH", "2")),
            ignore=_load_ignore(".iwikiignore") if load_ignore else None,
        )
```

- [ ] **Step 3: Port the engine tests and fix imports**

Copy from `/home/ikeniborn/Documents/Project/ai-wiki-plugin/engine/tests/` into `tests/engine/`:
`test_chunk.py test_embed.py test_store.py test_search.py test_related.py test_lint.py test_validate.py test_links.py test_config.py`
Do **not** copy `test_config_ignore.py` or `test_iwiki_common.py` (cwd-ignore behavior and hook helpers are out of scope here). Create empty `tests/engine/__init__.py`. In every copied test, rewrite imports `from iwiki_engine.X import` → `from iwiki_mcp.engine.X import` (and `import iwiki_engine.X` similarly).

- [ ] **Step 4: Add a test for the decoupled Config**

Create `tests/engine/test_config_decouple.py`:
```python
import pytest
from iwiki_mcp.engine.config import Config, ConfigError


def test_load_requires_api(monkeypatch):
    monkeypatch.delenv("IWIKI_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("IWIKI_LLM_KEY", raising=False)
    with pytest.raises(ConfigError):
        Config.load()


def test_load_does_not_read_cwd_ignore(monkeypatch, tmp_path):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    (tmp_path / ".iwikiignore").write_text("*.md\n")
    monkeypatch.chdir(tmp_path)
    cfg = Config.load()                      # default load_ignore=False
    assert cfg.ignore is None
    assert cfg.base_url == "http://x/v1"
```

- [ ] **Step 5: Run the full engine suite**

Run: `uv run pytest tests/engine -q`
Expected: PASS (all ported tests + the new decouple test). `test_embed.py` mocks httpx, so no network is hit.

- [ ] **Step 6: Commit**

```bash
git add src/iwiki_mcp/engine tests/engine
git commit -m "feat: port iwiki engine core with cwd-decoupled Config"
```

---

### Task 3: VectorStore interface

**Files:**
- Modify: `src/iwiki_mcp/engine/store.py` (append a class)
- Create: `tests/test_store_interface.py`

**Interfaces:**
- Consumes: `load_index`, `save_index`, `Record`, `dequantize`, `search`.
- Produces: `VectorStore(index_path)` with `.load() -> list[Record]`, `.save(recs)`, `.query(query_vec, top_k, threshold) -> list[dict]`.

- [ ] **Step 1: Write the failing test**

`tests/test_store_interface.py`:
```python
from iwiki_mcp.engine.store import VectorStore, make_record
from iwiki_mcp.engine.chunk import Chunk


def _chunk(heading, text):
    return Chunk(file="a.md", heading=heading, chunk=0, text=text, hash="h"+heading)


def test_store_roundtrip_and_query(tmp_path):
    idx = str(tmp_path / ".iwiki" / "index.jsonl")
    store = VectorStore(idx)
    recs = [
        make_record(_chunk("One", "x"), [1.0, 0.0]),
        make_record(_chunk("Two", "y"), [0.0, 1.0]),
    ]
    store.save(recs)
    assert len(store.load()) == 2
    hits = store.query([1.0, 0.0], top_k=1, threshold=0.1)
    assert hits and hits[0]["heading"] == "One"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_store_interface.py -q`
Expected: FAIL with `ImportError: cannot import name 'VectorStore'`.

- [ ] **Step 3: Implement `VectorStore`**

Append to `src/iwiki_mcp/engine/store.py`:
```python
class VectorStore:
    """Thin per-index store over the JSONL backend. The seam for a later
    SQLite/sqlite-vec swap: callers depend only on load/save/query."""

    def __init__(self, index_path: str):
        self.index_path = index_path

    def load(self) -> list[Record]:
        return load_index(self.index_path)

    def save(self, recs: list[Record]) -> None:
        save_index(self.index_path, recs)

    def query(self, query_vec: list[float], top_k: int, threshold: float) -> list[dict]:
        from .search import search
        return search(query_vec, self.load(), top_k, threshold)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_store_interface.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/engine/store.py tests/test_store_interface.py
git commit -m "feat: add VectorStore interface over the JSONL index"
```

---

### Task 4: Base + domain resolution and project binding

**Files:**
- Create: `src/iwiki_mcp/base.py`
- Create: `tests/test_base.py`

**Interfaces:**
- Consumes: nothing from sibling tasks.
- Produces:
  - `Binding(base, read: tuple[str,...], write: str|None, project_dir)`; `BaseError`.
  - `resolve_binding(project_dir=None) -> Binding` (raises `BaseError` if base unresolved).
  - `domain_dir(base, domain)`, `index_path(base, domain)`, `log_path(base, domain)`, `pages_dir` helpers.
  - `domain_exists(base, domain) -> bool`, `list_domains(base) -> list[str]`.
  - `resolve_scope(binding, scope, domains) -> list[str]`.
  - `write_project_config(project_dir, read=None, write=None) -> None`.

- [ ] **Step 1: Write failing tests**

`tests/test_base.py`:
```python
import pytest
from iwiki_mcp import base


def _mkbase(tmp_path, *domains):
    b = tmp_path / "wiki"
    for d in domains:
        (b / d / ".iwiki").mkdir(parents=True)
        (b / d / "page.md").write_text("# P\n## Overview\nx\n")
    b.mkdir(exist_ok=True)
    return str(b)


def test_resolve_from_env(tmp_path, monkeypatch):
    b = _mkbase(tmp_path, "backend", "shared")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text('read = ["backend"]\nwrite = "backend"\n')
    monkeypatch.setenv("IWIKI_BASE_DIR", b)
    bind = base.resolve_binding(str(proj))
    assert bind.base == b
    assert bind.read == ("backend",)
    assert bind.write == "backend"


def test_missing_base_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("IWIKI_BASE_DIR", raising=False)
    proj = tmp_path / "proj"
    proj.mkdir()
    with pytest.raises(base.BaseError):
        base.resolve_binding(str(proj))


def test_empty_read_defaults_to_all_domains(tmp_path, monkeypatch):
    b = _mkbase(tmp_path, "a", "b")
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("IWIKI_BASE_DIR", b)
    bind = base.resolve_binding(str(proj))
    assert set(base.resolve_scope(bind, "project", None)) == {"a", "b"}


def test_scope_all_vs_explicit(tmp_path, monkeypatch):
    b = _mkbase(tmp_path, "a", "b", "c")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text('read = ["a"]\nwrite = "a"\n')
    monkeypatch.setenv("IWIKI_BASE_DIR", b)
    bind = base.resolve_binding(str(proj))
    assert base.resolve_scope(bind, "project", None) == ["a"]
    assert set(base.resolve_scope(bind, "all", None)) == {"a", "b", "c"}
    assert base.resolve_scope(bind, "project", ["b", "c"]) == ["b", "c"]


def test_write_project_config_roundtrip(tmp_path, monkeypatch):
    b = _mkbase(tmp_path, "x")
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("IWIKI_BASE_DIR", b)
    base.write_project_config(str(proj), read=["x"], write="x")
    bind = base.resolve_binding(str(proj))
    assert bind.write == "x"
    assert bind.read == ("x",)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_base.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'iwiki_mcp.base'`.

- [ ] **Step 3: Implement `base.py`**

`src/iwiki_mcp/base.py`:
```python
"""Resolve the shared wiki base, its domains, and the project's binding.

The base is a git-synced directory (`IWIKI_BASE_DIR`, overridable by the
project's `.iwiki.toml` `base`). A project declares a read-set (domains it
searches) and a single write-target. Domains are subdirectories of the base.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass


class BaseError(RuntimeError):
    """Raised when the base or a required binding field cannot be resolved."""


@dataclass(frozen=True)
class Binding:
    base: str
    read: tuple[str, ...]
    write: str | None
    project_dir: str


def resolve_project_dir(explicit: str | None = None) -> str:
    return os.path.abspath(
        explicit or os.environ.get("IWIKI_PROJECT_DIR") or os.getcwd()
    )


def load_project_config(project_dir: str) -> dict:
    path = os.path.join(project_dir, ".iwiki.toml")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except Exception:
        return {}


def resolve_binding(project_dir: str | None = None) -> Binding:
    pdir = resolve_project_dir(project_dir)
    cfg = load_project_config(pdir)
    base_dir = (cfg.get("base") or os.environ.get("IWIKI_BASE_DIR") or "").strip()
    if not base_dir:
        raise BaseError(
            "no wiki base configured: set IWIKI_BASE_DIR or add `base` to .iwiki.toml"
        )
    base_dir = os.path.abspath(base_dir)
    read = tuple(cfg.get("read") or ())
    write = cfg.get("write") or None
    return Binding(base=base_dir, read=read, write=write, project_dir=pdir)


def domain_dir(base: str, domain: str) -> str:
    return os.path.join(base, domain)


def index_path(base: str, domain: str) -> str:
    return os.path.join(base, domain, ".iwiki", "index.jsonl")


def log_path(base: str, domain: str) -> str:
    return os.path.join(base, domain, ".iwiki", "log.jsonl")


def domain_exists(base: str, domain: str) -> bool:
    return os.path.isdir(os.path.join(base, domain))


def list_domains(base: str) -> list[str]:
    if not os.path.isdir(base):
        return []
    out = []
    for name in sorted(os.listdir(base)):
        full = os.path.join(base, name)
        if os.path.isdir(full) and not name.startswith("."):
            out.append(name)
    return out


def resolve_scope(binding: Binding, scope: str, domains: list[str] | None) -> list[str]:
    if domains:
        return list(domains)
    if scope == "all":
        return list_domains(binding.base)
    # scope == "project": the read-set, defaulting to all domains when empty
    return list(binding.read) if binding.read else list_domains(binding.base)


def write_project_config(project_dir: str, read: list[str] | None = None,
                         write: str | None = None) -> None:
    cfg = load_project_config(project_dir)
    if read is not None:
        cfg["read"] = list(read)
    if write is not None:
        cfg["write"] = write
    lines = []
    if "read" in cfg:
        items = ", ".join(f'"{d}"' for d in cfg["read"])
        lines.append(f"read = [{items}]")
    if cfg.get("write"):
        lines.append(f'write = "{cfg["write"]}"')
    if cfg.get("base"):
        lines.append(f'base = "{cfg["base"]}"')
    os.makedirs(project_dir, exist_ok=True)
    with open(os.path.join(project_dir, ".iwiki.toml"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_base.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/base.py tests/test_base.py
git commit -m "feat: base/domain resolution and project binding"
```

---

### Task 5: Domain indexing (domain-relative paths) + ingest log

**Files:**
- Create: `src/iwiki_mcp/indexer.py`
- Create: `tests/test_indexer.py`

**Interfaces:**
- Consumes: `Config`, `chunk_markdown`, `embed_texts`, `Record`/`make_record`/`VectorStore`, `base.index_path`, `base.log_path`.
- Produces:
  - `index_domain(cfg, base, domain) -> dict` (`{"indexed_chunks", "reused", "embedded", "bytes", "over_cap"}`), storing **domain-relative** `file` paths.
  - `src_hash(path) -> str | None`.
  - `append_log(base, domain, op, source, page, src_hash) -> None`.

- [ ] **Step 1: Write the failing test (embeddings mocked)**

`tests/test_indexer.py`:
```python
import json
from iwiki_mcp import indexer, base
from iwiki_mcp.engine.config import Config


def _cfg():
    return Config(base_url="http://x/v1", api_key="k", embed_model="m",
                  dimensions=2, chunk_size=512, chunk_overlap=64, summary_max=400,
                  top_k=8, score_threshold=0.2, graph_depth=2, ignore=None)


def test_index_domain_stores_relative_paths(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
    (b / "backend" / ".iwiki").mkdir(parents=True)
    (b / "backend" / "auth.md").write_text(
        "# Auth\n## Overview\nsummary\n## Flow\nlogin then token\n")
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, texts: [[1.0, 0.0] for _ in texts])
    stats = indexer.index_domain(_cfg(), str(b), "backend")
    assert stats["indexed_chunks"] >= 1
    recs = [json.loads(l) for l in open(base.index_path(str(b), "backend"))]
    assert all(r["file"] == "auth.md" for r in recs)   # domain-relative, portable


def test_append_log_writes_record(tmp_path):
    b = tmp_path / "wiki"
    (b / "backend" / ".iwiki").mkdir(parents=True)
    indexer.append_log(str(b), "backend", "ingest", "src/auth.py", "auth.md", "abc123")
    line = open(base.log_path(str(b), "backend")).read().strip()
    rec = __import__("json").loads(line)
    assert rec["op"] == "ingest" and rec["page"] == "auth.md" and rec["src_hash"] == "abc123"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_indexer.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'iwiki_mcp.indexer'`.

- [ ] **Step 3: Implement `indexer.py`**

`src/iwiki_mcp/indexer.py`:
```python
"""Index a single domain into its JSONL store, with machine-portable
(domain-relative) `file` paths, and append ingest-log records."""
from __future__ import annotations

import datetime as _dt
import glob
import hashlib
import json
import os

from .base import index_path, log_path
from .engine.config import Config
from .engine.chunk import chunk_markdown
from .engine.embed import embed_texts
from .engine.store import VectorStore, make_record, index_bytes

CAP_BYTES = 8 * 1024 * 1024


def src_hash(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:16]
    except OSError:
        return None


def index_domain(cfg: Config, base: str, domain: str) -> dict:
    dom_dir = os.path.join(base, domain)
    idx = index_path(base, domain)
    store = VectorStore(idx)
    existing = {f"{r.id}#{r.chunk}": r for r in store.load()}
    files = sorted(glob.glob(os.path.join(dom_dir, "**", "*.md"), recursive=True))
    files = [f for f in files if "/.iwiki/" not in f]
    chunks = []
    for md in files:
        rel = os.path.relpath(md, dom_dir)          # domain-relative, portable
        content = open(md, encoding="utf-8").read()
        chunks.extend(chunk_markdown(rel, content, cfg.chunk_size,
                                     cfg.chunk_overlap, cfg.summary_max))
    fresh, reused, to_embed = [], 0, []
    for c in chunks:
        key = f"{c.id}#{c.chunk}"
        prev = existing.get(key)
        if prev and prev.hash == c.hash:
            fresh.append(prev)
            reused += 1
        else:
            to_embed.append(c)
    if to_embed:
        vecs = embed_texts(cfg, [c.text for c in to_embed])
        fresh.extend(make_record(c, v) for c, v in zip(to_embed, vecs))
    fresh.sort(key=lambda r: (r.file, r.heading, r.chunk))
    store.save(fresh)
    size = index_bytes(idx)
    return {"indexed_chunks": len(fresh), "reused": reused,
            "embedded": len(to_embed), "bytes": size, "over_cap": size > CAP_BYTES}


def append_log(base: str, domain: str, op: str, source: str, page: str,
               src_hash_val: str | None) -> None:
    path = log_path(base, domain)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rec = {"op": op, "source": source, "page": page,
           "date": _dt.date.today().isoformat(), "src_hash": src_hash_val}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_indexer.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/indexer.py tests/test_indexer.py
git commit -m "feat: domain indexing with portable paths and ingest log"
```

---

### Task 6: Lexical (grep) search

**Files:**
- Create: `src/iwiki_mcp/engine/grep.py`
- Create: `tests/test_grep.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `grep_sections(domain_dir, query, top_k) -> list[dict]` (`{file, heading, chunk:0, score, hit:"lexical"}`, `file` domain-relative).

- [ ] **Step 1: Write the failing test**

`tests/test_grep.py`:
```python
from iwiki_mcp.engine.grep import grep_sections


def test_grep_finds_exact_symbol(tmp_path):
    (tmp_path / ".iwiki").mkdir()
    (tmp_path / "auth.md").write_text(
        "# Auth\n## Overview\ngeneral\n## Token\nthe refresh_token rotates\n")
    (tmp_path / "ui.md").write_text("# UI\n## Layout\nbuttons and panels\n")
    hits = grep_sections(str(tmp_path), "refresh_token", top_k=5)
    assert hits and hits[0]["file"] == "auth.md"
    assert hits[0]["heading"] == "Token"
    assert hits[0]["hit"] == "lexical"


def test_grep_empty_for_no_terms(tmp_path):
    assert grep_sections(str(tmp_path), "a", top_k=5) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_grep.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `grep.py`**

`src/iwiki_mcp/engine/grep.py`:
```python
"""Lexical search over a domain's .md pages: section-level term-frequency
scoring. Complements vector search by catching exact symbol/identifier matches
that embeddings blur. Returns the same section-shaped hits for merging."""
from __future__ import annotations

import glob
import os
import re

_H2 = re.compile(r"^##\s+(.*?)\s*$", re.MULTILINE)


def _terms(query: str) -> list[str]:
    return [t.lower() for t in re.findall(r"\w+", query) if len(t) > 2]


def grep_sections(domain_dir: str, query: str, top_k: int) -> list[dict]:
    terms = _terms(query)
    if not terms:
        return []
    out: list[dict] = []
    for md in glob.glob(os.path.join(domain_dir, "**", "*.md"), recursive=True):
        if "/.iwiki/" in md:
            continue
        try:
            content = open(md, encoding="utf-8").read()
        except OSError:
            continue
        rel = os.path.relpath(md, domain_dir)
        ms = list(_H2.finditer(content))
        for i, m in enumerate(ms):
            heading = m.group(1).strip()
            end = ms[i + 1].start() if i + 1 < len(ms) else len(content)
            hay = (heading + " " + content[m.end():end]).lower()
            score = sum(hay.count(t) for t in terms)
            if score > 0:
                out.append({"file": rel, "heading": heading, "chunk": 0,
                            "score": score, "hit": "lexical"})
    out.sort(key=lambda d: d["score"], reverse=True)
    return out[:top_k]
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_grep.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/engine/grep.py tests/test_grep.py
git commit -m "feat: lexical grep search over domain pages"
```

---

### Task 7: Multi-domain hybrid retrieval

**Files:**
- Create: `src/iwiki_mcp/retrieval.py`
- Create: `tests/test_retrieval.py`

**Interfaces:**
- Consumes: `Config`, `embed_texts`, `VectorStore`/`dequantize`, `base.index_path`/`domain_dir`, `grep_sections`, numpy.
- Produces:
  - `vector_search(cfg, base, domains, query, top_k, threshold) -> list[dict]` (`{domain,file,heading,chunk,score,hit:"vector"}`).
  - `hybrid_search(cfg, base, domains, query, top_k, threshold, mode="hybrid") -> list[dict]` (adds `hit ∈ {"vector","lexical","both"}`).

- [ ] **Step 1: Write the failing test (embeddings mocked)**

`tests/test_retrieval.py`:
```python
from iwiki_mcp import retrieval, indexer, base
from iwiki_mcp.engine.config import Config


def _cfg():
    return Config(base_url="http://x/v1", api_key="k", embed_model="m",
                  dimensions=2, chunk_size=512, chunk_overlap=64, summary_max=400,
                  top_k=8, score_threshold=0.0, graph_depth=2, ignore=None)


def _seed(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
    for d, body in (("a", "alpha refresh_token here"), ("b", "beta gamma")):
        (b / d / ".iwiki").mkdir(parents=True)
        (b / d / "p.md").write_text(f"# P\n## Overview\no\n## S\n{body}\n")
    monkeypatch.setattr(indexer, "embed_texts",
                        lambda cfg, texts: [[1.0, 0.0] for _ in texts])
    indexer.index_domain(_cfg(), str(b), "a")
    indexer.index_domain(_cfg(), str(b), "b")
    return str(b)


def test_vector_search_merges_domains(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(retrieval, "embed_texts",
                        lambda cfg, texts: [[1.0, 0.0]])
    hits = retrieval.vector_search(_cfg(), b, ["a", "b"], "q", top_k=10, threshold=0.0)
    assert {h["domain"] for h in hits} == {"a", "b"}
    assert all(h["hit"] == "vector" for h in hits)


def test_hybrid_adds_lexical(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(retrieval, "embed_texts",
                        lambda cfg, texts: [[0.0, 1.0]])   # orthogonal → no vector hits
    hits = retrieval.hybrid_search(_cfg(), b, ["a", "b"], "refresh_token",
                                   top_k=10, threshold=0.99, mode="hybrid")
    assert any(h["hit"] == "lexical" and h["domain"] == "a" for h in hits)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_retrieval.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `retrieval.py`**

`src/iwiki_mcp/retrieval.py`:
```python
"""Multi-domain retrieval: numpy-merged vector search across the in-scope
domains' indices, plus a lexical (grep) path, combined into hybrid results.

Vector and lexical scores live on different scales, so hybrid ranks vector/both
hits first (by cosine), then lexical hits (by term-frequency), deduped by
(domain, file, heading)."""
from __future__ import annotations

import numpy as np

from .base import domain_dir, index_path
from .engine.config import Config
from .engine.embed import embed_texts
from .engine.store import VectorStore, dequantize
from .engine.grep import grep_sections


def vector_search(cfg: Config, base: str, domains: list[str], query: str,
                  top_k: int, threshold: float) -> list[dict]:
    qv = np.asarray(embed_texts(cfg, [query])[0], dtype=np.float32)
    qnorm = float(np.linalg.norm(qv)) or 1.0
    hits: list[dict] = []
    for d in domains:
        recs = VectorStore(index_path(base, d)).load()
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
    hits.sort(key=lambda h: h["score"], reverse=True)
    return hits[:top_k]


def lexical_search(base: str, domains: list[str], query: str,
                   top_k: int) -> list[dict]:
    hits: list[dict] = []
    for d in domains:
        for h in grep_sections(domain_dir(base, d), query, top_k):
            hits.append({"domain": d, **h})
    hits.sort(key=lambda h: h["score"], reverse=True)
    return hits[:top_k]


def hybrid_search(cfg: Config, base: str, domains: list[str], query: str,
                  top_k: int, threshold: float, mode: str = "hybrid") -> list[dict]:
    vec = (vector_search(cfg, base, domains, query, top_k, threshold)
           if mode in ("hybrid", "vector") else [])
    lex = (lexical_search(base, domains, query, top_k)
           if mode in ("hybrid", "lexical") else [])
    merged: dict[tuple, dict] = {}
    for h in vec:
        merged[(h["domain"], h["file"], h["heading"])] = h
    for h in lex:
        key = (h["domain"], h["file"], h["heading"])
        if key in merged:
            merged[key]["hit"] = "both"
        else:
            merged[key] = h
    out = list(merged.values())
    out.sort(key=lambda h: (0 if h["hit"] in ("vector", "both") else 1, -h["score"]))
    return out[:top_k]
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_retrieval.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/retrieval.py tests/test_retrieval.py
git commit -m "feat: multi-domain hybrid retrieval (numpy vector + grep)"
```

---

### Task 8: Git sync

**Files:**
- Create: `src/iwiki_mcp/sync.py`
- Create: `tests/test_sync.py`

**Interfaces:**
- Consumes: nothing from sibling tasks (shells out to `git`).
- Produces:
  - `is_git_repo(base) -> bool`.
  - `auto_commit(base, message) -> dict` (`{"committed": bool, "warning"?: str}`).
  - `sync(base) -> dict` (`{"pulled": bool, "pushed": bool, "warning"?/"error"?: str}`).

- [ ] **Step 1: Write the failing tests**

`tests/test_sync.py`:
```python
import subprocess
from iwiki_mcp import sync


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")


def test_auto_commit_in_repo(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "x.md").write_text("hi")
    res = sync.auto_commit(str(tmp_path), "iwiki: test")
    assert res["committed"] is True
    log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path,
                         capture_output=True, text=True).stdout
    assert "iwiki: test" in log


def test_auto_commit_non_repo_warns(tmp_path):
    (tmp_path / "x.md").write_text("hi")
    res = sync.auto_commit(str(tmp_path), "iwiki: test")
    assert res["committed"] is False
    assert "warning" in res


def test_sync_no_remote_warns(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "x.md").write_text("hi")
    sync.auto_commit(str(tmp_path), "iwiki: c")
    res = sync.sync(str(tmp_path))
    assert res.get("pushed") is False
    assert "warning" in res or "error" in res
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_sync.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `sync.py`**

`src/iwiki_mcp/sync.py`:
```python
"""Git operations on the shared base: auto-commit on write, and an explicit
sync (pull --rebase + push). Fail-soft: a non-repo or missing remote degrades
to a warning, never an exception."""
from __future__ import annotations

import subprocess


def _run(base: str, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", base, *args], capture_output=True,
                          text=True, timeout=timeout)


def is_git_repo(base: str) -> bool:
    try:
        r = _run(base, "rev-parse", "--is-inside-work-tree")
        return r.returncode == 0 and r.stdout.strip() == "true"
    except Exception:
        return False


def auto_commit(base: str, message: str) -> dict:
    if not is_git_repo(base):
        return {"committed": False, "warning": "base is not a git repo; not committing"}
    try:
        _run(base, "add", "-A")
        status = _run(base, "status", "--porcelain")
        if not status.stdout.strip():
            return {"committed": False, "warning": "nothing to commit"}
        r = _run(base, "commit", "-m", message)
        return {"committed": r.returncode == 0,
                **({} if r.returncode == 0 else {"warning": r.stderr.strip()})}
    except Exception as e:
        return {"committed": False, "warning": str(e)}


def _has_remote(base: str) -> bool:
    r = _run(base, "remote")
    return bool(r.stdout.strip())


def sync(base: str) -> dict:
    if not is_git_repo(base):
        return {"pulled": False, "pushed": False, "error": "base is not a git repo"}
    if not _has_remote(base):
        return {"pulled": False, "pushed": False,
                "warning": "no git remote configured; commits stay local"}
    try:
        pull = _run(base, "pull", "--rebase")
        if pull.returncode != 0:
            _run(base, "rebase", "--abort")
            return {"pulled": False, "pushed": False,
                    "error": "pull --rebase conflict (aborted)",
                    "hint": "resolve in the base repo, or re-run index to regenerate "
                            "a conflicted .iwiki/index.jsonl, then sync again"}
        push = _run(base, "push")
        return {"pulled": True, "pushed": push.returncode == 0,
                **({} if push.returncode == 0 else {"warning": push.stderr.strip()})}
    except Exception as e:
        return {"pulled": False, "pushed": False, "error": str(e)}
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_sync.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/sync.py tests/test_sync.py
git commit -m "feat: git auto-commit and sync for the shared base"
```

---

### Task 9: MCP server scaffold + read/status tools

**Files:**
- Create: `src/iwiki_mcp/server.py`
- Create: `tests/test_server_read.py`

**Interfaces:**
- Consumes: `base.*`, `indexer.*`, `retrieval.*`, `sync.*`, `Config`.
- Produces:
  - `_safe(fn)` decorator → returns `{"error","hint"}` on exception.
  - A FastMCP app `mcp` and `main()` (stdio).
  - Tool impl functions (registered + unit-testable): `wiki_status()`, `wiki_list_domains()`, `wiki_list_pages(domain)`, `wiki_read_page(domain, slug)`.

- [ ] **Step 1: Write the failing tests (call impl functions directly)**

`tests/test_server_read.py`:
```python
from iwiki_mcp import server, base


def _seed(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
    (b / "backend" / ".iwiki").mkdir(parents=True)
    (b / "backend" / "auth.md").write_text("# Auth\n## Overview\no\n## Flow\nx\n")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text('read = ["backend"]\nwrite = "backend"\n')
    monkeypatch.setenv("IWIKI_BASE_DIR", str(b))
    monkeypatch.setenv("IWIKI_PROJECT_DIR", str(proj))
    return str(b)


def test_status(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_status()
    assert out["write"] == "backend"
    assert "backend" in out["domains"]


def test_list_domains_and_pages(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    assert "backend" in server.wiki_list_domains()["domains"]
    pages = server.wiki_list_pages("backend")["pages"]
    assert any(p["slug"] == "auth" for p in pages)


def test_read_page(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    md = server.wiki_read_page("backend", "auth")["markdown"]
    assert "## Flow" in md


def test_read_missing_page(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_read_page("backend", "nope")
    assert "error" in out


def test_status_no_base(monkeypatch):
    monkeypatch.delenv("IWIKI_BASE_DIR", raising=False)
    monkeypatch.setenv("IWIKI_PROJECT_DIR", "/tmp/does-not-exist-iwiki")
    out = server.wiki_status()
    assert "error" in out
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_server_read.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'iwiki_mcp.server'`.

- [ ] **Step 3: Implement the scaffold + read tools**

`src/iwiki_mcp/server.py`:
```python
"""iwiki MCP server (stdio). Tools are fail-soft: every handler returns a
JSON-serializable dict, and exceptions become {"error","hint"} structures."""
from __future__ import annotations

import functools
import os

from mcp.server.fastmcp import FastMCP

from . import base, indexer, retrieval, sync
from .engine.config import Config, ConfigError
from .engine.embed import EmbedError

mcp = FastMCP("iwiki")


def _safe(fn):
    @functools.wraps(fn)
    def wrap(*a, **k):
        try:
            return fn(*a, **k)
        except base.BaseError as e:
            return {"error": str(e), "hint": "set IWIKI_BASE_DIR or run wiki_bind"}
        except (ConfigError, EmbedError) as e:
            return {"error": f"HALT: {e}",
                    "hint": "set IWIKI_LLM_BASE_URL / IWIKI_LLM_KEY"}
        except Exception as e:                       # fail-soft catch-all
            return {"error": str(e), "hint": "unexpected error; see server logs"}
    return wrap


def _page_path(b: str, domain: str, slug: str) -> str:
    return os.path.join(base.domain_dir(b, domain), f"{slug}.md")


@_safe
def wiki_status() -> dict:
    bind = base.resolve_binding()
    return {"base": bind.base, "read": list(bind.read), "write": bind.write,
            "project_dir": bind.project_dir, "domains": base.list_domains(bind.base)}


@_safe
def wiki_list_domains() -> dict:
    bind = base.resolve_binding()
    out = []
    for d in base.list_domains(bind.base):
        out.append({"domain": d,
                    "index_bytes": _index_bytes(base.index_path(bind.base, d))})
    return {"domains": [d["domain"] for d in out], "detail": out}


def _index_bytes(path: str) -> int:
    return os.path.getsize(path) if os.path.exists(path) else 0


@_safe
def wiki_list_pages(domain: str) -> dict:
    bind = base.resolve_binding()
    dom = base.domain_dir(bind.base, domain)
    if not os.path.isdir(dom):
        return {"error": f"domain '{domain}' not found",
                "hint": "create it with wiki_create_domain"}
    pages = []
    for root, _dirs, files in os.walk(dom):
        if ".iwiki" in root.split(os.sep):
            continue
        for f in sorted(files):
            if f.endswith(".md"):
                rel = os.path.relpath(os.path.join(root, f), dom)
                pages.append({"slug": rel[:-3], "file": rel})
    return {"domain": domain, "pages": pages}


@_safe
def wiki_read_page(domain: str, slug: str) -> dict:
    bind = base.resolve_binding()
    path = _page_path(bind.base, domain, slug)
    if not os.path.isfile(path):
        return {"error": f"page '{domain}/{slug}' not found",
                "hint": "list pages with wiki_list_pages"}
    return {"domain": domain, "slug": slug,
            "markdown": open(path, encoding="utf-8").read()}


# --- MCP registration (thin wrappers; impls above stay unit-testable) --------
mcp.tool()(wiki_status)
mcp.tool()(wiki_list_domains)
mcp.tool()(wiki_list_pages)
mcp.tool()(wiki_read_page)


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(prog="iwiki-mcp")
    p.add_argument("--project",
                   help="project dir (overrides cwd / IWIKI_PROJECT_DIR)")
    args = p.parse_args()
    if args.project:
        os.environ["IWIKI_PROJECT_DIR"] = os.path.abspath(args.project)
    mcp.run()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_server_read.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/server.py tests/test_server_read.py
git commit -m "feat: MCP server scaffold with read/status tools"
```

---

### Task 10: Search + related tools

**Files:**
- Modify: `src/iwiki_mcp/server.py`
- Create: `tests/test_server_search.py`

**Interfaces:**
- Consumes: `retrieval.hybrid_search`, `Config.load`, `base.resolve_scope`, engine `related`/`VectorStore`.
- Produces:
  - `wiki_search(query, scope="project", mode="hybrid", domains=None, k=None, threshold=None) -> dict` (`{"results":[...]}`).
  - `wiki_related(domain, section_id) -> dict` (`{"vector":[...], "graph":[...]}`).

- [ ] **Step 1: Write the failing test (embeddings mocked)**

`tests/test_server_search.py`:
```python
from iwiki_mcp import server, indexer, retrieval


def _seed(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
    (b / "backend" / ".iwiki").mkdir(parents=True)
    (b / "backend" / "auth.md").write_text(
        "# Auth\n## Overview\no\n## Token\nrefresh_token rotates\n")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text('read = ["backend"]\nwrite = "backend"\n')
    monkeypatch.setenv("IWIKI_BASE_DIR", str(b))
    monkeypatch.setenv("IWIKI_PROJECT_DIR", str(proj))
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", "2")
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, t: [[1.0, 0.0] for _ in t])
    monkeypatch.setattr(retrieval, "embed_texts", lambda cfg, t: [[1.0, 0.0]])
    indexer.index_domain(__import__("iwiki_mcp.engine.config", fromlist=["Config"]).Config.load(),
                         str(b), "backend")
    return str(b)


def test_search_returns_results(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_search("token", scope="project", threshold=0.0)
    assert "results" in out and out["results"]
    assert out["results"][0]["domain"] == "backend"


def test_search_lexical_mode(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_search("refresh_token", mode="lexical")
    assert any(r["hit"] == "lexical" for r in out["results"])
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_server_search.py -q`
Expected: FAIL with `AttributeError: module 'iwiki_mcp.server' has no attribute 'wiki_search'`.

- [ ] **Step 3: Add the tools to `server.py`**

Insert after `wiki_read_page` (before the MCP registration block):
```python
@_safe
def wiki_search(query: str, scope: str = "project", mode: str = "hybrid",
                domains: list[str] | None = None, k: int | None = None,
                threshold: float | None = None) -> dict:
    bind = base.resolve_binding()
    cfg = Config.load()
    doms = base.resolve_scope(bind, scope, domains)
    if not doms:
        return {"results": [], "hint": "no domains in scope"}
    results = retrieval.hybrid_search(
        cfg, bind.base, doms, query,
        top_k=k or cfg.top_k,
        threshold=cfg.score_threshold if threshold is None else threshold,
        mode=mode)
    return {"results": results}


@_safe
def wiki_related(domain: str, section_id: str) -> dict:
    from .engine.related import related
    from .engine.store import VectorStore
    bind = base.resolve_binding()
    cfg = Config.load()
    recs = VectorStore(base.index_path(bind.base, domain)).load()
    return related(section_id, recs, cfg.top_k, cfg.graph_depth)
```
Add to the registration block:
```python
mcp.tool()(wiki_search)
mcp.tool()(wiki_related)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_server_search.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/server.py tests/test_server_search.py
git commit -m "feat: wiki_search (hybrid) and wiki_related tools"
```

---

### Task 11: Write/authoring + domain + bind tools

**Files:**
- Modify: `src/iwiki_mcp/server.py`
- Create: `tests/test_server_write.py`

**Interfaces:**
- Consumes: `validate_page`, `indexer.index_domain`/`append_log`/`src_hash`, `sync.auto_commit`, `base.*`.
- Produces:
  - `wiki_write_page(domain, slug, markdown, source=None) -> dict`.
  - `wiki_index(domain=None) -> dict`.
  - `wiki_create_domain(name) -> dict`.
  - `wiki_bind(read=None, write=None) -> dict`.

- [ ] **Step 1: Write the failing tests (embeddings mocked)**

`tests/test_server_write.py`:
```python
import os
from iwiki_mcp import server, indexer


def _seed(tmp_path, monkeypatch, with_domain=True):
    b = tmp_path / "wiki"
    b.mkdir()
    if with_domain:
        (b / "backend" / ".iwiki").mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text('read = ["backend"]\nwrite = "backend"\n')
    monkeypatch.setenv("IWIKI_BASE_DIR", str(b))
    monkeypatch.setenv("IWIKI_PROJECT_DIR", str(proj))
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", "2")
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, t: [[1.0, 0.0] for _ in t])
    return str(b), str(proj)


def test_write_page_indexes_and_logs(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch)
    md = "# Auth\n## Overview\nsummary\n## Flow\nlogin then token\n"
    out = server.wiki_write_page("backend", "auth", md)
    assert out["page"] == "backend/auth.md"
    assert out["indexed_chunks"] >= 1
    assert os.path.isfile(os.path.join(b, "backend", "auth.md"))
    assert os.path.isfile(os.path.join(b, "backend", ".iwiki", "log.jsonl"))


def test_write_rejects_deep_heading(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_write_page("backend", "bad", "# T\n### Too Deep\nx\n")
    assert "error" in out


def test_write_refuses_overwrite_without_force(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    md = "# Auth\n## Overview\no\n## Flow\nx\n"
    server.wiki_write_page("backend", "auth", md)
    out = server.wiki_write_page("backend", "auth", md)
    assert "error" in out and "exists" in out["error"]


def test_create_domain(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch, with_domain=False)
    out = server.wiki_create_domain("backend")
    assert out["created"] == "backend"
    assert os.path.isdir(os.path.join(b, "backend"))


def test_bind_writes_config(tmp_path, monkeypatch):
    _b, proj = _seed(tmp_path, monkeypatch)
    out = server.wiki_bind(read=["backend", "shared"], write="backend")
    assert out["read"] == ["backend", "shared"]
    assert 'write = "backend"' in open(os.path.join(proj, ".iwiki.toml")).read()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_server_write.py -q`
Expected: FAIL with `AttributeError: ... has no attribute 'wiki_write_page'`.

- [ ] **Step 3: Add the tools to `server.py`**

Add imports near the top (with the other `from .` imports):
```python
from .engine.validate import validate_page
```
Insert before the registration block:
```python
_BLOCKING = {"deep_heading", "pre_h2_text"}


@_safe
def wiki_write_page(domain: str, slug: str, markdown: str,
                    source: str | None = None) -> dict:
    bind = base.resolve_binding()
    if not base.domain_exists(bind.base, domain):
        return {"error": f"domain '{domain}' not found",
                "hint": "create it with wiki_create_domain"}
    blocking = [f for f in validate_page(markdown) if f.get("kind") in _BLOCKING]
    if blocking:
        return {"error": "section structure invalid", "findings": blocking,
                "hint": "use only ## headings; no text before the first ##"}
    path = os.path.join(base.domain_dir(bind.base, domain), f"{slug}.md")
    if os.path.exists(path):
        return {"error": f"page '{domain}/{slug}' exists",
                "hint": "editing an existing page is a guarded op; confirm with the user"}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(markdown)
    cfg = Config.load()
    stats = indexer.index_domain(cfg, bind.base, domain)
    page_rel = f"{domain}/{slug}.md"
    indexer.append_log(bind.base, domain, "ingest", source or "",
                       f"{slug}.md", indexer.src_hash(source) if source else None)
    commit = sync.auto_commit(bind.base, f"iwiki: ingest {page_rel}")
    return {"page": page_rel, "indexed_chunks": stats["indexed_chunks"],
            "bytes": stats["bytes"], "over_cap": stats["over_cap"],
            "committed": commit.get("committed", False)}


@_safe
def wiki_index(domain: str | None = None) -> dict:
    bind = base.resolve_binding()
    target = domain or bind.write
    if not target:
        return {"error": "no domain given and no write-target bound",
                "hint": "pass domain= or set write in .iwiki.toml via wiki_bind"}
    cfg = Config.load()
    stats = indexer.index_domain(cfg, bind.base, target)
    return {"domain": target, **stats}


@_safe
def wiki_create_domain(name: str) -> dict:
    bind = base.resolve_binding()
    if base.domain_exists(bind.base, name):
        return {"error": f"domain '{name}' already exists"}
    os.makedirs(os.path.join(base.domain_dir(bind.base, name), ".iwiki"),
                exist_ok=True)
    commit = sync.auto_commit(bind.base, f"iwiki: create domain {name}")
    return {"created": name, "committed": commit.get("committed", False)}


@_safe
def wiki_bind(read: list[str] | None = None, write: str | None = None) -> dict:
    bind = base.resolve_binding()
    base.write_project_config(bind.project_dir, read=read, write=write)
    new = base.resolve_binding()
    return {"read": list(new.read), "write": new.write,
            "project_dir": new.project_dir}
```
Add to the registration block:
```python
mcp.tool()(wiki_write_page)
mcp.tool()(wiki_index)
mcp.tool()(wiki_create_domain)
mcp.tool()(wiki_bind)
```

- [ ] **Step 4: Verify `validate_page` finding keys**

Run: `uv run python -c "from iwiki_mcp.engine.validate import validate_page; print(validate_page('# T\n### Deep\nx'))"`
Expected: a list of dicts. Confirm each finding's type field is named `kind` (used by `_BLOCKING`). If the key is named differently (e.g. `rule` or `type`), update `_BLOCKING`'s `f.get("kind")` to match the actual key.

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_server_write.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/iwiki_mcp/server.py tests/test_server_write.py
git commit -m "feat: write/index/create-domain/bind tools"
```

---

### Task 12: Lint + sync tools

**Files:**
- Modify: `src/iwiki_mcp/server.py`
- Create: `tests/test_server_lint_sync.py`

**Interfaces:**
- Consumes: engine `lint`, `sync.sync`, `base.*`.
- Produces:
  - `wiki_lint(domain=None) -> dict` (lint over one domain or all in read-set).
  - `wiki_sync() -> dict`.

- [ ] **Step 1: Write the failing tests**

`tests/test_server_lint_sync.py`:
```python
import subprocess
from iwiki_mcp import server


def _seed(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
    (b / "backend" / ".iwiki").mkdir(parents=True)
    (b / "backend" / "auth.md").write_text("# Auth\n## Overview\no\n## Flow\nx\n")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text('read = ["backend"]\nwrite = "backend"\n')
    monkeypatch.setenv("IWIKI_BASE_DIR", str(b))
    monkeypatch.setenv("IWIKI_PROJECT_DIR", str(proj))
    return str(b)


def test_lint_one_domain(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_lint("backend")
    assert "backend" in out["domains"]


def test_sync_no_repo(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_sync()
    assert "error" in out or "warning" in out
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_server_lint_sync.py -q`
Expected: FAIL with `AttributeError: ... 'wiki_lint'`.

- [ ] **Step 3: Add the tools to `server.py`**

Insert before the registration block:
```python
@_safe
def wiki_lint(domain: str | None = None) -> dict:
    from .engine.lint import lint
    bind = base.resolve_binding()
    targets = [domain] if domain else base.resolve_scope(bind, "project", None)
    reports = {d: lint(base.domain_dir(bind.base, d)) for d in targets}
    return {"domains": list(reports.keys()), "reports": reports}


@_safe
def wiki_sync() -> dict:
    bind = base.resolve_binding()
    return sync.sync(bind.base)
```
Add to the registration block:
```python
mcp.tool()(wiki_lint)
mcp.tool()(wiki_sync)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_server_lint_sync.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/server.py tests/test_server_lint_sync.py
git commit -m "feat: wiki_lint and wiki_sync tools"
```

---

### Task 13: Authoring-rules resource

**Files:**
- Create: `src/iwiki_mcp/resources.py`
- Modify: `src/iwiki_mcp/server.py`
- Create: `tests/test_resources.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `AUTHORING_RULES: str`; resource `iwiki://authoring-rules` registered on `mcp`.

- [ ] **Step 1: Write the failing test**

`tests/test_resources.py`:
```python
from iwiki_mcp.resources import AUTHORING_RULES


def test_authoring_rules_cover_section_format():
    text = AUTHORING_RULES.lower()
    assert "## overview" in text
    assert "[[" in AUTHORING_RULES          # cross-link guidance
    assert "##" in AUTHORING_RULES
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_resources.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `resources.py`**

`src/iwiki_mcp/resources.py`:
```python
"""Page-authoring rules, exposed as an MCP resource the agent fetches before
writing. Ported from the iwiki-ingest skill's section-formation rules."""

AUTHORING_RULES = """\
# iwiki page authoring rules

- Use **only `##`** for sections — never `###` or deeper. Deeper headings are not
  indexed as separate units; flatten them into the `##` section's prose.
- Put **no content before the first `##`** except a single `# Title` H1.
- Lead with `# Title`, then a first `## Overview` section summarizing all of the
  page's sections in <=400 characters. The Overview is NOT indexed as its own
  section; it gives every other section whole-article context.
- One `##` section per concept; lead each section with a <=250-char paragraph
  stating what it covers and why it matters (intent, not just mechanics).
- Prefer a standard section name where one fits: `## Purpose`, `## Interface`,
  `## API`, `## Dependencies`, `## Data flow`, `## Errors`, `## Usage`.
- Wrap every code symbol (function, path, flag, command, config key) in backticks.
- Cross-link related pages with `[[slug#Heading]]` (within the same domain in v1).
- Write accurate English prose grounded in the real source; do not invent.
"""
```

- [ ] **Step 4: Register the resource in `server.py`**

Add near the imports:
```python
from .resources import AUTHORING_RULES
```
Add after the tool registration block:
```python
@mcp.resource("iwiki://authoring-rules")
def authoring_rules() -> str:
    return AUTHORING_RULES
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_resources.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/iwiki_mcp/resources.py src/iwiki_mcp/server.py tests/test_resources.py
git commit -m "feat: authoring-rules MCP resource"
```

---

### Task 14: MCP stdio smoke test

**Files:**
- Create: `tests/test_mcp_smoke.py`

**Interfaces:**
- Consumes: the running `iwiki-mcp` server over stdio via the `mcp` client.
- Produces: an end-to-end check that tools list and a config-free tool returns.

- [ ] **Step 1: Write the smoke test**

`tests/test_mcp_smoke.py`:
```python
import os
import sys
import pytest

mcp_client = pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters       # noqa: E402
from mcp.client.stdio import stdio_client                  # noqa: E402


@pytest.mark.asyncio
async def test_lists_tools_and_status(tmp_path):
    base = tmp_path / "wiki"
    (base / "backend" / ".iwiki").mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    env = dict(os.environ)
    env["IWIKI_BASE_DIR"] = str(base)
    env["IWIKI_PROJECT_DIR"] = str(proj)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "iwiki_mcp.server"], env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            assert {"wiki_status", "wiki_search", "wiki_write_page"} <= tools
            res = await session.call_tool("wiki_status", {})
            assert res.content
```

- [ ] **Step 2: Ensure async test support**

Run: `uv pip install pytest-asyncio`
Then add to `pyproject.toml` under `[tool.pytest.ini_options]`:
```toml
asyncio_mode = "auto"
```
And add `"pytest-asyncio>=0.23"` to the `dev` extra in `pyproject.toml`.

- [ ] **Step 3: Run the smoke test**

Run: `uv run pytest tests/test_mcp_smoke.py -q`
Expected: PASS (server starts over stdio, lists tools, `wiki_status` returns content).

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_mcp_smoke.py pyproject.toml
git commit -m "test: MCP stdio smoke test"
```

---

### Task 15: Automation snippets

**Files:**
- Create: `templates/AGENTS.md.snippet`
- Create: `templates/CLAUDE.md.snippet`

**Interfaces:**
- Consumes: nothing.
- Produces: paste-in instructions guiding recall, bootstrap (init), write, and sync via the MCP tools.

- [ ] **Step 1: Write `templates/CLAUDE.md.snippet`**

```markdown
## iwiki (shared wiki via MCP)

This project is bound to a shared wiki base (see `.iwiki.toml`: `read` = domains to
search, `write` = domain to author into). Use the `iwiki` MCP tools:

- **Before starting a task:** call `wiki_search` with the topic to recall existing
  knowledge (scope `project` for the read-set, `all` for the whole base).
- **Bootstrapping a new write-target:** detect source areas (entry points; immediate
  subdirs of `src/`/`lib/`/`app/`/`packages/`/`cmd/`/`internal/`; skip
  `node_modules`/`dist`/`.venv`/tests). For each area, fetch `iwiki://authoring-rules`,
  then `wiki_write_page(<write-target>, <slug>, <markdown>, source=<path>)`.
- **After changing functionality:** update the relevant page with `wiki_write_page`.
- **Periodically / at end of session:** call `wiki_sync` to pull/push the base.
- Editing an existing page is guarded — `wiki_write_page` refuses to overwrite; confirm
  with the user and proceed deliberately.
```

- [ ] **Step 2: Write `templates/AGENTS.md.snippet`**

Same content as `CLAUDE.md.snippet` (Codex reads `AGENTS.md`). Copy it verbatim into `templates/AGENTS.md.snippet`.

- [ ] **Step 3: Commit**

```bash
git add templates/AGENTS.md.snippet templates/CLAUDE.md.snippet
git commit -m "docs: AGENTS.md/CLAUDE.md automation snippets"
```

---

### Task 16: README

**Files:**
- Create/Modify: `README.md`

**Interfaces:**
- Consumes: the finished tool surface and env contract.
- Produces: install, registration (Codex + Claude Code), env tables, binding, tool overview, git-sync, quick start.

- [ ] **Step 1: Write `README.md`**

Cover, in this order (mirroring the iwiki README style — sections + variable tables):
1. **What it is** — one-paragraph: shared, git-synced wiki base split into domains, queried via MCP from Codex and Claude Code; the agent authors pages, the server stores/indexes/searches.
2. **Install** — `uv tool install iwiki-mcp` (or `pipx install iwiki-mcp`); requires an OpenAI-compatible embeddings endpoint.
3. **Register in Claude Code** — the `.mcp.json` block from the spec's Installation section (verbatim env keys).
4. **Register in Codex** — the `~/.codex/config.toml` `[mcp_servers.iwiki]` block from the spec.
5. **The base and domains** — `IWIKI_BASE_DIR` is a git repo; domains are subdirs; one base shared across projects.
6. **Bind a project** — `.iwiki.toml` with `read`/`write`/optional `base`; or call `wiki_bind`.
7. **Env reference tables** — Required (`IWIKI_LLM_BASE_URL`, `IWIKI_LLM_KEY`); Embedding model (`IWIKI_EMBED_MODEL`, `IWIKI_EMBED_DIMENSIONS`); Search tuning (`IWIKI_TOP_K`, `IWIKI_SCORE_THRESHOLD`, `IWIKI_GRAPH_DEPTH`); Indexing (`IWIKI_CHUNK_SIZE`, `IWIKI_CHUNK_OVERLAP`, `IWIKI_SUMMARY_MAX_CHARS`); Location (`IWIKI_BASE_DIR`, `IWIKI_PROJECT_DIR`) — copy defaults from this plan's Global Constraints.
8. **Tools** — a table: `wiki_search`, `wiki_read_page`, `wiki_list_pages`, `wiki_related`, `wiki_write_page`, `wiki_index`, `wiki_list_domains`, `wiki_create_domain`, `wiki_bind`, `wiki_status`, `wiki_lint`, `wiki_sync` — one-line each.
9. **Git sync of the base** — auto-commit on write; `wiki_sync` for pull --rebase + push; index conflicts resolved by regeneration.
10. **Quick start** — register, `wiki_create_domain`, `wiki_bind`, write a page, `wiki_search`.
11. **Limitations (v1)** — intra-domain links; numpy brute-force; project-local staleness.

- [ ] **Step 2: Verify the README renders and links are consistent**

Run: `uv run python -c "print(open('README.md').read()[:200])"`
Expected: prints the header; manually confirm every env var and tool name matches the implementation.

- [ ] **Step 3: Final full-suite run**

Run: `uv run pytest -q`
Expected: all tests PASS.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: README for iwiki-mcp"
```

---

## Self-Review

**Spec coverage check:**
- One install for Codex + Claude Code over stdio → Tasks 1, 9, 14, 16. ✓
- Shared git-synced base + domains → Tasks 4, 8. ✓
- Read-set / write-target binding → Tasks 4, 11 (`wiki_bind`). ✓
- Scope project vs all → Task 4 (`resolve_scope`), Task 10. ✓
- Agent authors; server stores/indexes/searches → Tasks 5, 11. ✓
- OpenAI-compatible embeddings, cwd-decoupled Config → Task 2. ✓
- Auto-commit on write + explicit sync → Tasks 8, 11, 12. ✓
- Engine reuse in-process → Task 2. ✓
- Per-domain JSONL + numpy merge behind VectorStore → Tasks 3, 5, 7. ✓
- Hybrid retrieval (vector + grep + graph) → Tasks 6, 7, 10 (`wiki_related` = graph). ✓
- Tool surface (12 tools) → Tasks 9–12. ✓
- `wiki_list_pages` → Task 9. ✓
- Domain-relative portable index → Task 5. ✓
- Authoring-rules resource → Task 13. ✓
- Init as agent workflow + snippets → Task 15. ✓
- Staleness/gaps project-local fail-soft → engine `lint` ported (Task 2) + `wiki_lint` (Task 12); source-absent skip is inherited from `lint._fresh`. ✓
- 8 MB cap per domain → Task 5 (`over_cap`). ✓
- README → Task 16. ✓
- Fail-soft tools → Task 9 (`_safe`). ✓
- No deletion of pages → Task 11 (overwrite refused). ✓
- Intra-domain links limitation → Task 10 (`wiki_related` per-domain), Task 13 (rules note). ✓

**Placeholder scan:** No "TBD"/"handle edge cases"/"similar to Task N". Task 16 step 1 is a structured content outline (acceptable for a docs deliverable, with exact var/tool names enumerated).

**Type consistency:** `Config.load(load_ignore=False)`, `index_domain(cfg, base, domain)`, `hybrid_search(..., mode=)`, hit shapes `{domain,file,heading,chunk,score,hit}`, `Binding(base,read,write,project_dir)`, `_safe` envelope `{error,hint}` — used consistently across tasks. Task 11 step 4 explicitly verifies the real `validate_page` finding key before relying on `_BLOCKING`.
