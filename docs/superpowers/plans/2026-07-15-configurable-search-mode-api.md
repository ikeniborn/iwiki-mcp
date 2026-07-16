---
review:
  plan_hash: 17b9504e19dd16c2
  last_run: 2026-07-16
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings: []
result_check:
  verdict: OK
  plan_hash: 17b9504e19dd16c2
  last_run: 2026-07-16
  reviewed: true
  docs_checked: true
chain:
  intent: docs/superpowers/intents/2026-07-15-configurable-search-mode-api-intent.md
  spec: docs/superpowers/specs/2026-07-15-configurable-search-mode-api-design.md
---
# Configurable Semantic Search and Reranking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose `hybrid`, `lexical`, and `semantic` read-search modes with an environment-selected default, fuse a broader deterministic candidate pool, and optionally rerank hydrated chunks through LiteLLM without weakening search availability.

**Architecture:** Keep write-intent lookup on its existing vector path. Build read-search as independent semantic-page, lexical-page, graph-page, semantic-chunk, and lexical-section signals; merge them with pure Reciprocal Rank Fusion, hydrate exact indexed chunks from current Markdown, then invoke an isolated fail-soft reranker boundary when configured. Resolve public mode precedence and response metadata in `server.py`, while keeping ranking primitives framework-free under `engine/`.

**Tech Stack:** Python 3.10+, FastMCP, `numpy`, `httpx`, LiteLLM-compatible `/v1/rerank`, pytest/pytest-asyncio, `uv`, flake8, iwiki MCP tools.

**Intent:** `docs/superpowers/intents/2026-07-15-configurable-search-mode-api-intent.md`

**Spec:** `docs/superpowers/specs/2026-07-15-configurable-search-mode-api-design.md`

---

## File Map

- `src/iwiki_mcp/engine/config.py`: parse and validate omitted-mode and reranker configuration.
- `src/iwiki_mcp/engine/fusion.py`: pure RRF over named ranked lists.
- `src/iwiki_mcp/engine/hier.py`: deterministic graph-page metadata and ordering.
- `src/iwiki_mcp/engine/grep.py`: untruncated internal lexical scan before page aggregation.
- `src/iwiki_mcp/engine/rerank.py`: one authenticated LiteLLM request, response validation, and sanitized fail-soft result.
- `src/iwiki_mcp/retrieval.py`: independent signal collection, facet-safe candidate assembly, fusion, and exact Markdown hydration.
- `src/iwiki_mcp/server.py`: optional public mode enum, precedence, reranker orchestration, and top-level metadata.
- `tests/engine/test_config.py`, `tests/engine/test_fusion.py`, `tests/engine/test_hier.py`, `tests/engine/test_rerank.py`: focused primitive contracts.
- `tests/test_retrieval.py`, `tests/test_retrieval_facets.py`, `tests/test_server_search.py`, `tests/test_server_search_facets.py`, `tests/test_server_search_write_intent.py`: read-pipeline and write-isolation integration coverage.
- `eval/hierarchical/fixtures.py`, `eval/hierarchical/harness.py`, `tests/eval/test_hierarchical_eval.py`, `docs/superpowers/evidence/configurable-search-mode-api-eval.md`: fixed offline quality gate and recorded evidence.
- `tests/test_mcp_smoke.py`: complete 18-tool registration and optional enum schema contract.
- `README.md`, `docs/README.ru.md`, `docs/reports/iwiki-mcp-server-report.html`, `templates/AGENTS.md.snippet`, `templates/CLAUDE.md.snippet`, `src/iwiki_mcp/resources.py`, `tests/test_resources.py`: synchronized public documentation and regression coverage.
- `pyproject.toml`, `src/iwiki_mcp/__init__.py`, `uv.lock`: `0.7.1` release metadata.
- iwiki pages `retrieval`, `mcp-server`, and `installation`: complete implemented read-search, public tool, and environment behavior.

## Requirement Traceability

| Spec requirement | Tasks |
|---|---|
| R1 Canonical modes | 2, 7, 8 |
| R2 Default-mode precedence | 2, 7 |
| R3 Reranker configuration | 2, 6, 7 |
| R4 Result contract | 5, 6, 7 |
| R5 Independent ranking signals | 4, 5 |
| R6 Reciprocal Rank Fusion | 3, 4, 5 |
| R7 Candidate text hydration | 5 |
| R8 Write-intent isolation | 7 |
| R9 Request contract | 6 |
| R10 Timeout and fail-soft behavior | 6, 7 |
| R11 Fixed offline evaluation | 1, 9 |
| R12 MCP and repository verification | 8, 11 |
| R13 Documentation consistency | 10 |
| R14 Release level | 11 |

### Task 1: Freeze the Expanded Offline Baseline

**Files:**
- Modify: `eval/hierarchical/fixtures.py`
- Modify: `eval/hierarchical/harness.py`
- Modify: `tests/eval/test_hierarchical_eval.py`
- Create: `docs/superpowers/evidence/configurable-search-mode-api-eval.md`

- [ ] **Step 1: Expand the fixed corpus and query contract**

Replace `VAULT` and `QUERIES` with a fixture whose query records name the exact relevant `(file, heading)` pairs and cover semantic phrasing, exact identifiers, links, lexical seeds, duplicates, a distractor, and a global chunk:

```python
VAULT = {
    "guide/auth.md": (
        "---\ndescription: credential lifecycle and session renewal\n---\n"
        "# Authentication\n\n## Rotation\nrefresh_token rotates credentials safely\n"
        "\n## Links\nSee [Deployment](guide/deploy.md).\n"
    ),
    "guide/deploy.md": (
        "---\ndescription: release rollout procedure\n---\n"
        "# Deployment\n\n## Rollback\nrestore the previous release atomically\n"
    ),
    "reference/config.md": (
        "---\ndescription: runtime configuration keys\n---\n"
        "# Configuration\n\n## Search Mode\nIWIKI_SEARCH_MODE selects retrieval behavior\n"
    ),
    "concept/semantic.md": (
        "---\ndescription: meaning based document discovery\n---\n"
        "# Semantic Discovery\n\n## Similarity\nfind passages with different wording\n"
    ),
    "concept/distractor.md": (
        "---\ndescription: release search credential configuration\n---\n"
        "# Distractor\n\n## Noise\nrelease search credential configuration\n"
    ),
    "runbook/orphan.md": (
        "---\ndescription: unrelated maintenance notes\n---\n"
        "# Orphan\n\n## Emergency Token\nbreak_glass_token recovery procedure\n"
    ),
}

QUERIES = [
    {"query": "renew login access", "relevant": [("guide/auth.md", "Rotation")]},
    {"query": "IWIKI_SEARCH_MODE", "relevant": [("reference/config.md", "Search Mode")]},
    {"query": "restore a bad release", "relevant": [("guide/deploy.md", "Rollback")]},
    {"query": "different words same meaning", "relevant": [("concept/semantic.md", "Similarity")]},
    {"query": "break_glass_token", "relevant": [("runbook/orphan.md", "Emergency Token")]},
    {"query": "refresh_token credentials", "relevant": [("guide/auth.md", "Rotation")]},
]
```

Keep the deterministic `embed()` vocabulary-based and add synonym buckets so semantic-only queries map to their relevant summaries without network access:

```python
_BUCKETS = (
    ("auth", "credential", "login", "renew", "refresh_token"),
    ("deploy", "release", "rollback", "restore"),
    ("config", "iwiki_search_mode", "runtime"),
    ("semantic", "meaning", "similarity", "wording"),
    ("break_glass_token", "emergency", "recovery"),
)


def embed(text: str) -> list[float]:
    lowered = text.lower()
    return [float(sum(lowered.count(term) for term in bucket)) for bucket in _BUCKETS]
```

- [ ] **Step 2: Add a baseline evaluator that preserves the old algorithm**

Import `contextmanager` from `contextlib` and `Path` from `pathlib`. Rename the existing evaluator to `run_baseline_eval`, accept `top_k`, and calculate section `recall_at_k` and `mrr_at_k` from exact `(file, heading)` identities:

```python
def _metrics(rankings: list[list[tuple[str, str]]], queries: list[dict]) -> dict:
    recalled = 0
    reciprocal_rank = 0.0
    for ranked, query in zip(rankings, queries):
        relevant = set(query["relevant"])
        recalled += int(any(item in relevant for item in ranked))
        for rank, item in enumerate(ranked, 1):
            if item in relevant:
                reciprocal_rank += 1.0 / rank
                break
    count = len(queries) or 1
    return {
        "recall_at_k": recalled / count,
        "mrr_at_k": reciprocal_rank / count,
    }


def run_baseline_eval(vault, queries, embed_fn, top_k: int = 8) -> dict:
    summaries, sections = _records(vault, embed_fn)
    rankings = []
    with _vault_dir(vault) as root:
        for query in queries:
            query_vec = embed_fn(query["query"])
            seeds = hier.seed_articles(query_vec, summaries, 5, 0.0)
            pool = hier.expand_graph([file for file, _ in seeds], root, 1, 10)
            ranked = hier.rank_sections(query_vec, sections, pool, top_k)
            rankings.append([(hit["file"], hit["heading"]) for hit in ranked])
    return _metrics(rankings, queries)
```

Implement `_vault_dir` as a `@contextmanager` that creates parent directories before writing nested fixture paths:

```python
@contextmanager
def _vault_dir(vault):
    with tempfile.TemporaryDirectory() as root:
        for file, markdown in vault.items():
            path = Path(root, file)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(markdown, encoding="utf-8")
        yield root
```

- [ ] **Step 3: Write the baseline characterization test**

```python
def test_baseline_metrics_are_fixed():
    metrics = harness.run_baseline_eval(
        fixtures.VAULT, fixtures.QUERIES, fixtures.embed, top_k=3
    )
    assert metrics == {
        "recall_at_k": 0.8333333333333334,
        "mrr_at_k": 0.75,
    }
```

- [ ] **Step 4: Run the baseline and record exact evidence**

```bash
uv run python -c 'from eval.hierarchical import fixtures, harness; print(harness.run_baseline_eval(fixtures.VAULT, fixtures.QUERIES, fixtures.embed, top_k=3))'
uv run pytest -q tests/eval/test_hierarchical_eval.py
```

Expected: command prints finite `recall_at_k` and `mrr_at_k`; pytest passes. Write the observed values verbatim into this complete evidence shape:

```markdown
# Configurable Search Mode API Evaluation

## Corpus

Six fixed queries over six pages cover semantic phrasing, exact identifiers, graph reachability, lexical seeds, a semantic/lexical duplicate, a high-similarity distractor, and a relevant global chunk outside the seed graph. Evaluation is network-free and uses `top_k = 3`.

## Baseline

Command: `uv run python -c 'from eval.hierarchical import fixtures, harness; print(harness.run_baseline_eval(fixtures.VAULT, fixtures.QUERIES, fixtures.embed, top_k=3))'`

Observed pre-change metrics: `recall@3 = 0.8333333333333334` and
`MRR@3 = 0.75`.

## Candidate configuration

The initial internal candidate ceiling is `32`; RRF uses `k = 60`. Acceptance requires preliminary `MRR@3` above baseline and preliminary `recall@3` at least baseline. The fake reranker must not reduce recall and must improve or preserve MRR over preliminary order.
```

- [ ] **Step 5: Commit the frozen baseline**

```bash
git add eval/hierarchical/fixtures.py eval/hierarchical/harness.py tests/eval/test_hierarchical_eval.py docs/superpowers/evidence/configurable-search-mode-api-eval.md
git commit -m "test: freeze expanded search quality baseline"
```

### Task 2: Add Search and Reranker Configuration

**Files:**
- Modify: `tests/engine/test_config.py`
- Modify: `src/iwiki_mcp/engine/config.py`

- [ ] **Step 1: Write failing configuration tests**

```python
@pytest.mark.parametrize("value", ["hybrid", "lexical", "semantic"])
def test_search_mode_accepts_canonical_values(monkeypatch, embedding_env, value):
    monkeypatch.setenv("IWIKI_SEARCH_MODE", f"  {value.upper()}  ")
    assert Config.load().search_mode == value


def test_search_mode_defaults_to_hybrid(monkeypatch, embedding_env):
    monkeypatch.delenv("IWIKI_SEARCH_MODE", raising=False)
    assert Config.load().search_mode == "hybrid"


@pytest.mark.parametrize("value", ["vector", "bogus", ""])
def test_search_mode_rejects_noncanonical_values(monkeypatch, embedding_env, value):
    monkeypatch.setenv("IWIKI_SEARCH_MODE", value)
    with pytest.raises(ConfigError, match="hybrid, lexical, semantic"):
        Config.load()


def test_rerank_model_is_optional_and_trimmed(monkeypatch, embedding_env):
    monkeypatch.delenv("IWIKI_RERANK_MODEL", raising=False)
    assert Config.load().rerank_model == ""
    monkeypatch.setenv("IWIKI_RERANK_MODEL", "  cohere-rerank-v3.5  ")
    assert Config.load().rerank_model == "cohere-rerank-v3.5"
```

- [ ] **Step 2: Verify RED**

```bash
uv run pytest -q tests/engine/test_config.py
```

Expected: new tests fail because `Config` has no `search_mode` or `rerank_model`.

- [ ] **Step 3: Implement validated parsing**

Add fields with defaults so existing direct test construction remains compatible:

```python
    search_mode: str = "hybrid"
    rerank_model: str = ""
```

Before the `Config(...)` return in `load()`:

```python
        search_mode = getenv("IWIKI_SEARCH_MODE", "hybrid").strip().lower()
        valid_modes = ("hybrid", "lexical", "semantic")
        if search_mode not in valid_modes:
            allowed = ", ".join(valid_modes)
            raise ConfigError(f"IWIKI_SEARCH_MODE must be one of: {allowed}.")
```

Pass these values into `Config`:

```python
            search_mode=search_mode,
            rerank_model=getenv("IWIKI_RERANK_MODEL", "").strip(),
```

- [ ] **Step 4: Verify GREEN and commit**

```bash
uv run pytest -q tests/engine/test_config.py
git add src/iwiki_mcp/engine/config.py tests/engine/test_config.py
git commit -m "feat: configure search mode and reranker"
```

Expected: config tests pass; invalid values name only the three allowed modes.

### Task 3: Implement Pure Reciprocal Rank Fusion

**Files:**
- Create: `tests/engine/test_fusion.py`
- Create: `src/iwiki_mcp/engine/fusion.py`

- [ ] **Step 1: Write failing RRF tests**

```python
from iwiki_mcp.engine.fusion import fuse_ranked


def _hit(file, heading, chunk=0, **extra):
    return {"domain": "d", "file": file, "heading": heading, "chunk": chunk, **extra}


def test_fusion_rewards_candidates_present_in_multiple_signals():
    signals = {
        "semantic": [_hit("a.md", "A"), _hit("b.md", "B")],
        "lexical": [_hit("b.md", "B"), _hit("c.md", "C")],
    }
    fused = fuse_ranked(signals, limit=3)
    assert [item["file"] for item in fused] == ["b.md", "a.md", "c.md"]
    assert fused[0]["signals"] == ["semantic", "lexical"]


def test_fusion_identity_includes_chunk_and_ties_are_stable():
    signals = {
        "one": [_hit("b.md", "S", 0), _hit("a.md", "S", 1)],
        "two": [_hit("a.md", "S", 0)],
    }
    fused = fuse_ranked(signals, limit=10)
    assert [(h["file"], h["chunk"]) for h in fused] == [
        ("a.md", 0), ("b.md", 0), ("a.md", 1)
    ]


def test_fusion_limit_can_be_smaller_than_requested_results():
    fused = fuse_ranked({"one": [_hit("a.md", "A"), _hit("b.md", "B")]}, limit=1)
    assert [h["file"] for h in fused] == ["a.md"]


def test_fusion_ignores_duplicate_identity_within_one_signal():
    duplicate = _hit("a.md", "A")
    fused = fuse_ranked({"one": [duplicate, dict(duplicate)]}, limit=5)
    assert len(fused) == 1
    assert fused[0]["score"] == 1 / 61
```

- [ ] **Step 2: Verify RED**

```bash
uv run pytest -q tests/engine/test_fusion.py
```

Expected: collection fails because `engine.fusion` does not exist.

- [ ] **Step 3: Implement the framework-free fusion module**

```python
"""Deterministic Reciprocal Rank Fusion for section-shaped search hits."""
from __future__ import annotations

_RRF_K = 60


def _identity(hit: dict) -> tuple:
    return (hit["domain"], hit["file"], hit["heading"], hit["chunk"])


def fuse_ranked(signals: dict[str, list[dict]], limit: int) -> list[dict]:
    if limit <= 0:
        return []
    merged: dict[tuple, dict] = {}
    seen_by_signal: dict[str, set[tuple]] = {}
    for signal, ranked in signals.items():
        seen = seen_by_signal.setdefault(signal, set())
        for rank, hit in enumerate(ranked, 1):
            key = _identity(hit)
            if key in seen:
                continue
            seen.add(key)
            item = merged.setdefault(key, {**hit, "score": 0.0, "signals": []})
            item["score"] += 1.0 / (_RRF_K + rank)
            item["signals"].append(signal)
    out = list(merged.values())
    out.sort(key=lambda h: (
        -h["score"], h["domain"], h["file"], h.get("ordinal", 0),
        h["heading"], h["chunk"],
    ))
    return out[:limit]
```

- [ ] **Step 4: Verify GREEN and commit**

```bash
uv run pytest -q tests/engine/test_fusion.py
git add src/iwiki_mcp/engine/fusion.py tests/engine/test_fusion.py
git commit -m "feat: add reciprocal rank fusion"
```

Expected: four fusion tests pass with deterministic order.

### Task 4: Add Ranked Graph Metadata

**Files:**
- Modify: `tests/engine/test_hier.py`
- Modify: `src/iwiki_mcp/engine/hier.py`

- [ ] **Step 1: Write failing graph-ranking tests**

```python
def test_rank_graph_pages_tracks_origin_distance_and_discovery(tmp_path):
    (tmp_path / "a.md").write_text("[B](b.md)\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("[C](c.md)\n", encoding="utf-8")
    (tmp_path / "c.md").write_text("", encoding="utf-8")
    ranked = hier.rank_graph_pages(
        [("a.md", "semantic", 1), ("b.md", "lexical", 1)],
        str(tmp_path), depth=2, cap=10,
    )
    assert ranked == [
        {"file": "a.md", "source": "seed", "seed_origins": ["semantic"], "distance": 0,
         "seed_rank": 1, "discovery": 0},
        {"file": "b.md", "source": "seed", "seed_origins": ["lexical"], "distance": 0,
         "seed_rank": 1, "discovery": 1},
        {"file": "c.md", "source": "graph", "seed_origins": ["lexical"], "distance": 1,
         "seed_rank": 1, "discovery": 2},
    ]


def test_rank_graph_pages_prefers_shorter_distance_then_seed_rank(tmp_path):
    (tmp_path / "a.md").write_text("[C](c.md)\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("[D](d.md)\n", encoding="utf-8")
    (tmp_path / "c.md").write_text("", encoding="utf-8")
    (tmp_path / "d.md").write_text("", encoding="utf-8")
    ranked = hier.rank_graph_pages(
        [("b.md", "semantic", 2), ("a.md", "semantic", 1)],
        str(tmp_path), depth=1, cap=10,
    )
    assert [row["file"] for row in ranked] == ["a.md", "b.md", "c.md", "d.md"]


def test_rank_graph_pages_merges_equal_distance_origins_and_zero_cap_is_unlimited(tmp_path):
    (tmp_path / "a.md").write_text("[C](c.md)\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("[C](c.md)\n", encoding="utf-8")
    (tmp_path / "c.md").write_text("", encoding="utf-8")
    ranked = hier.rank_graph_pages(
        [("a.md", "semantic", 1), ("b.md", "lexical", 1)],
        str(tmp_path), depth=1, cap=0,
    )
    graph = next(row for row in ranked if row["file"] == "c.md")
    assert graph["seed_origins"] == ["lexical", "semantic"]
```

- [ ] **Step 2: Verify RED**

```bash
uv run pytest -q tests/engine/test_hier.py
```

Expected: new tests fail because `rank_graph_pages` is absent; old graph tests remain green.

- [ ] **Step 3: Implement ranked multi-origin expansion without changing `expand_graph`**

```python
def rank_graph_pages(seeds: list[tuple[str, str, int]], domain_dir: str,
                     depth: int, cap: int) -> list[dict]:
    adjacency = _adjacency(domain_dir)
    rows: dict[str, dict] = {}
    frontier: list[str] = []
    discovery = 0
    for file, origin, seed_rank in sorted(seeds, key=lambda s: (s[2], s[0], s[1])):
        if file in rows:
            rows[file]["seed_origins"] = sorted(
                set(rows[file]["seed_origins"]) | {origin}
            )
            rows[file]["seed_rank"] = min(rows[file]["seed_rank"], seed_rank)
            continue
        rows[file] = {"file": file, "source": "seed", "seed_origins": [origin],
                      "distance": 0, "seed_rank": seed_rank, "discovery": discovery}
        discovery += 1
        frontier.append(file)
    for distance in range(1, max(0, depth) + 1):
        next_frontier = []
        for file in frontier:
            parent = rows[file]
            for neighbor in sorted(adjacency.get(file, ())):
                if neighbor in rows:
                    if rows[neighbor]["distance"] == distance:
                        rows[neighbor]["seed_origins"] = sorted(
                            set(rows[neighbor]["seed_origins"])
                            | set(parent["seed_origins"])
                        )
                        rows[neighbor]["seed_rank"] = min(
                            rows[neighbor]["seed_rank"], parent["seed_rank"]
                        )
                    continue
                rows[neighbor] = {
                    "file": neighbor, "source": "graph",
                    "seed_origins": list(parent["seed_origins"]), "distance": distance,
                    "seed_rank": parent["seed_rank"], "discovery": discovery,
                }
                discovery += 1
                next_frontier.append(neighbor)
        frontier = next_frontier
    ranked = sorted(rows.values(), key=lambda row: (
        0 if row["source"] == "seed" else 1, row["distance"], row["seed_rank"],
        row["file"], row["discovery"],
    ))
    seed_count = sum(row["source"] == "seed" for row in ranked)
    return ranked[:seed_count + cap] if cap > 0 else ranked
```

Do not route `locate_target` through this function; its existing `expand_graph` contract remains unchanged.

- [ ] **Step 4: Verify GREEN and commit**

```bash
uv run pytest -q tests/engine/test_hier.py tests/engine/test_hier_adjacency.py
git add src/iwiki_mcp/engine/hier.py tests/engine/test_hier.py
git commit -m "feat: rank graph search candidates"
```

Expected: graph tests pass, including existing undirected/reserved-file behavior.

### Task 5: Build and Hydrate the Fused Read Candidate Pool

**Files:**
- Modify: `tests/test_retrieval.py`
- Modify: `tests/test_retrieval_facets.py`
- Modify: `tests/test_grep.py`
- Modify: `src/iwiki_mcp/engine/grep.py`
- Modify: `src/iwiki_mcp/retrieval.py`

- [ ] **Step 1: Write failing signal-selection tests**

Import `replace` with `from dataclasses import replace`, then add focused tests using spies around `embed_texts` and small indexed domains:

```python
@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
def test_semantic_modes_embed_query_once(tmp_path, monkeypatch, mode):
    base = _seed(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        retrieval, "embed_texts",
        lambda cfg, texts: calls.append(list(texts)) or [[1.0, 0.0]],
    )
    retrieval.search_read(_cfg(), base, ["a"], "alpha", 8, 0.0, mode)
    assert calls == [["alpha"]]


def test_lexical_mode_never_embeds(tmp_path, monkeypatch):
    base = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        retrieval, "embed_texts",
        lambda *args: (_ for _ in ()).throw(AssertionError("embedding called")),
    )
    hits = retrieval.search_read(_cfg(), base, ["a"], "refresh_token", 8, 0.0, "lexical")
    assert hits and all(hit["hit"] == "lexical" for hit in hits)


def test_hybrid_fuses_duplicate_as_both(tmp_path, monkeypatch):
    base = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(retrieval, "embed_texts", lambda cfg, texts: [[1.0, 0.0]])
    hits = retrieval.search_read(_cfg(), base, ["a"], "refresh_token", 8, 0.0, "hybrid")
    assert any(hit["hit"] == "both" for hit in hits)


def test_semantic_mode_includes_global_chunk_outside_seed_graph(tmp_path, monkeypatch):
    domain = tmp_path / "d"
    domain.mkdir()
    (domain / "seed.md").write_text(
        "---\ndescription: seed page\n---\n# Seed\n\n## Local\nlocal text\n",
        encoding="utf-8",
    )
    (domain / "global.md").write_text(
        "---\ndescription: unrelated page\n---\n# Global\n\n## Answer\nglobal answer\n",
        encoding="utf-8",
    )
    cfg = replace(_cfg(), seed_top_k=1, seed_threshold=0.1)

    def embed_fixture(cfg, texts):
        return [
            [1.0, 0.0] if "seed page" in text or "global answer" in text
            else [0.0, 1.0]
            for text in texts
        ]

    monkeypatch.setattr(indexer, "embed_texts", embed_fixture)
    indexer.index_domain(cfg, str(tmp_path), "d")
    monkeypatch.setattr(retrieval, "embed_texts", lambda cfg, texts: [[1.0, 0.0]])
    hits = retrieval.search_read(cfg, str(tmp_path), ["d"], "query", 8, 0.0, "semantic")
    global_hit = next(hit for hit in hits if hit["file"] == "global.md")
    assert global_hit["source"] == "global"


def test_lexical_seed_expands_graph_without_embedding(tmp_path, monkeypatch):
    domain = tmp_path / "d"
    domain.mkdir()
    (domain / "seed.md").write_text(
        "---\ndescription: seed\n---\n# Seed\n\n## Match\nneedle [Target](target.md)\n",
        encoding="utf-8",
    )
    (domain / "target.md").write_text(
        "---\ndescription: target\n---\n# Target\n\n## Details\nlinked details\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, texts: [[1.0, 0.0] for _ in texts])
    indexer.index_domain(_cfg(), str(tmp_path), "d")
    monkeypatch.setattr(
        retrieval, "embed_texts",
        lambda *args: (_ for _ in ()).throw(AssertionError("embedding called")),
    )
    hits = retrieval.search_read(
        _cfg(), str(tmp_path), ["d"], "needle", 8, 0.0, "lexical"
    )
    target = next(hit for hit in hits if hit["file"] == "target.md")
    assert target["source"] == "graph"
```

- [ ] **Step 2: Write failing facet and candidate-ceiling tests**

```python
def test_every_candidate_signal_respects_facets(tmp_path, monkeypatch):
    domain = tmp_path / "d"
    (domain / "guide").mkdir(parents=True)
    (domain / "api").mkdir()
    (domain / "guide" / "allowed.md").write_text(
        "---\ntype: guide\ntags: [safe]\ndescription: widget guide\n---\n"
        "# Allowed\n\n## Usage\nwidget safe usage [API](api/blocked.md)\n",
        encoding="utf-8",
    )
    (domain / "api" / "blocked.md").write_text(
        "---\ntype: api\ntags: [unsafe]\ndescription: widget api\n---\n"
        "# Blocked\n\n## Contract\nwidget unsafe contract\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, texts: [[1.0, 0.0] for _ in texts])
    monkeypatch.setattr(retrieval, "embed_texts", lambda cfg, texts: [[1.0, 0.0]])
    indexer.index_domain(_cfg(), str(tmp_path), "d")
    hits = retrieval.search_read(
        _cfg(), str(tmp_path), ["d"], "widget", 8, -1.0, "hybrid",
        type="guide", tags=["safe"],
    )
    assert hits
    assert {hit["file"] for hit in hits} == {"guide/allowed.md"}


def test_candidate_ceiling_never_reduces_requested_top_k(monkeypatch):
    monkeypatch.setattr(retrieval, "CANDIDATE_LIMIT", 2)
    assert retrieval._candidate_limit(5) == 5
    assert retrieval._candidate_limit(1) == 2


def test_grep_none_limit_returns_all_positive_sections(tmp_path):
    (tmp_path / "a.md").write_text(
        "# A\n\n## One\nneedle\n\n## Two\nneedle\n\n## Three\nneedle\n",
        encoding="utf-8",
    )
    assert len(grep_sections(str(tmp_path), "needle", None)) == 3
```

- [ ] **Step 3: Verify RED**

```bash
uv run pytest -q tests/test_retrieval.py tests/test_retrieval_facets.py
```

Expected: tests fail because `search_read`, broader signals, and `_candidate_limit` do not exist.

- [ ] **Step 4: Implement signal collection and fusion**

Set the evaluated starting ceiling and canonical modes:

```python
CANDIDATE_LIMIT = 32
_VALID_MODES = {"hybrid", "semantic", "lexical"}


def _candidate_limit(top_k: int) -> int:
    return max(top_k, CANDIDATE_LIMIT)
```

Change the lexical primitive to support an internal untruncated scan while preserving every existing integer-limit behavior:

```python
def grep_sections(domain_dir: str, query: str,
                  top_k: int | None) -> list[dict]:
    if top_k is not None and top_k <= 0:
        return []
    terms = _terms(query)
    if not terms:
        return []
    root = Path(domain_dir)
    out: list[dict] = []
    for md in sorted(root.rglob("*.md")):
        rel_path = md.relative_to(root)
        if rel_path.as_posix() in RESERVED_OKF:
            continue
        try:
            content = md.read_text(encoding="utf-8")
        except OSError:
            continue
        rel = rel_path.as_posix()
        matches = list(_H2.finditer(content))
        for index, match in enumerate(matches):
            heading = match.group(1).strip()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
            haystack = (heading + " " + content[match.end():end]).lower()
            score = sum(haystack.count(term) for term in terms)
            if score > 0:
                out.append({"file": rel, "heading": heading, "chunk": 0,
                            "score": score, "hit": "lexical"})
    out.sort(key=lambda hit: (-hit["score"], hit["file"], hit["heading"]))
    return out if top_k is None else out[:top_k]
```

Implement one per-domain collector that loads facet-filtered records once, optionally accepts one query vector, derives semantic summary seeds, lexical page ranks from positive `grep_sections` hits, graph metadata from their union, global semantic sections, and lexical sections. Convert page rank to section-shaped graph hits by iterating eligible section records in graph-page order. Use these exact signal names in RRF: `semantic_page`, `lexical_page`, `graph_page`, `semantic_chunk`, `lexical_section`.

Add these helpers; they fully define identity, source, graph-origin, lexical aggregation, and deterministic rank keys:

```python
def _internal_hit(domain: str, rec, source: str, rank_key: tuple,
                  seed_origins: list[str] | None = None) -> dict:
    return {
        "domain": domain, "file": rec.file, "heading": rec.heading,
        "chunk": rec.chunk, "score": 0.0, "hit": "semantic",
        "source": source, "ordinal": rec.ordinal, "rank_key": rank_key,
        "seed_origins": list(seed_origins or []),
    }


def _domain_signals(cfg: Config, base: str, domain: str, query: str,
                    query_vec: list | None, limit: int, threshold: float,
                    mode: str, type: str | None, tags: list | None) -> dict[str, list[dict]]:
    migrate_store_location(base, domain)
    records = [
        rec for rec in VectorStore(index_path(base, domain)).load()
        if _facet_ok(rec.type, rec.tags, type, tags)
        and (query_vec is None or rec.dim == len(query_vec))
    ]
    summaries = [rec for rec in records if rec.kind == "summary"]
    sections = [rec for rec in records if rec.kind == "section"]
    sections_by_file: dict[str, list] = {}
    for rec in sections:
        sections_by_file.setdefault(rec.file, []).append(rec)
    for page_sections in sections_by_file.values():
        page_sections.sort(key=lambda rec: (rec.ordinal, rec.chunk, rec.heading))

    semantic_seeds = []
    semantic_chunks = []
    if mode in ("hybrid", "semantic") and query_vec is not None:
        scored_pages = [
            (rec, round(hier.sim(query_vec, rec), 4)) for rec in summaries
        ]
        scored_pages = [row for row in scored_pages if row[1] >= cfg.seed_threshold]
        scored_pages.sort(key=lambda row: (-row[1], row[0].file))
        semantic_seeds = scored_pages[:cfg.seed_top_k]
        scored_sections = [
            (rec, round(hier.sim(query_vec, rec), 4)) for rec in sections
        ]
        scored_sections = [row for row in scored_sections if row[1] >= threshold]
        scored_sections.sort(
            key=lambda row: (-row[1], row[0].file, row[0].ordinal, row[0].chunk)
        )
        semantic_chunks = scored_sections[:limit]

    lexical_hits = []
    lexical_pages = []
    if mode in ("hybrid", "lexical"):
        eligible_files = set(sections_by_file)
        lexical_hits = [
            hit for hit in grep_sections(domain_dir(base, domain), query, None)
            if hit["file"] in eligible_files
        ]
        page_scores: dict[str, float] = {}
        for hit in lexical_hits:
            page_scores[hit["file"]] = page_scores.get(hit["file"], 0.0) + hit["score"]
        lexical_pages = sorted(page_scores.items(), key=lambda row: (-row[1], row[0]))
        lexical_pages = lexical_pages[:cfg.seed_top_k]

    graph_seeds = [
        (rec.file, "semantic", rank)
        for rank, (rec, _) in enumerate(semantic_seeds, 1)
    ] + [
        (file, "lexical", rank)
        for rank, (file, _) in enumerate(lexical_pages, 1)
    ]
    graph_pages = hier.rank_graph_pages(
        graph_seeds, domain_dir(base, domain), cfg.graph_depth, cfg.bfs_top_k
    )
    signals = {
        "semantic_page": [], "lexical_page": [], "graph_page": [],
        "semantic_chunk": [], "lexical_section": [],
    }
    for page_rank, (summary, _) in enumerate(semantic_seeds, 1):
        for rec in sections_by_file.get(summary.file, []):
            signals["semantic_page"].append(_internal_hit(
                domain, rec, "seed", (page_rank, rec.ordinal, rec.chunk, rec.file),
                ["semantic"],
            ))
    for page_rank, (file, _) in enumerate(lexical_pages, 1):
        for rec in sections_by_file.get(file, []):
            signals["lexical_page"].append(_internal_hit(
                domain, rec, "seed", (page_rank, rec.ordinal, rec.chunk, rec.file),
                ["lexical"],
            ))
    for page_rank, page in enumerate(graph_pages, 1):
        for rec in sections_by_file.get(page["file"], []):
            signals["graph_page"].append(_internal_hit(
                domain, rec, page["source"],
                (page_rank, rec.ordinal, rec.chunk, rec.file), page["seed_origins"],
            ))
    for rec, score in semantic_chunks:
        hit = _internal_hit(
            domain, rec, "global", (-score, rec.file, rec.ordinal, rec.chunk),
            ["semantic"],
        )
        hit["score"] = score
        signals["semantic_chunk"].append(hit)
    rec_by_heading = {
        (rec.file, rec.heading): rec for rec in sections if rec.chunk == 0
    }
    for rank, lexical in enumerate(lexical_hits, 1):
        rec = rec_by_heading.get((lexical["file"], lexical["heading"]))
        if rec is None:
            continue
        hit = _internal_hit(
            domain, rec, "lexical", (rank, rec.file, rec.ordinal, rec.chunk),
            ["lexical"],
        )
        hit["score"] = lexical["score"]
        signals["lexical_section"].append(hit)
    return {name: ranked for name, ranked in signals.items() if ranked}
```

The candidate-preparation entry point returns the full internal ceiling; the ordinary read wrapper applies final top-k only when no caller needs the broader pool:

```python
def prepare_read_candidates(cfg: Config, base: str, domains: list[str], query: str,
                            top_k: int, threshold: float, mode: str,
                            type: str | None = None,
                            tags: list | None = None) -> list[dict]:
    if mode not in _VALID_MODES:
        allowed = ", ".join(sorted(_VALID_MODES))
        raise ValueError(f"invalid search mode: {mode}; allowed values: {allowed}")
    if top_k <= 0 or not domains:
        return []
    query_vec = None
    if mode in ("hybrid", "semantic"):
        query_vec = list(np.asarray(embed_texts(cfg, [query])[0], dtype=np.float32))
    limit = _candidate_limit(top_k)
    signals: dict[str, list[dict]] = {}
    for domain in domains:
        domain_signals = _domain_signals(
            cfg, base, domain, query, query_vec, limit, threshold, mode, type, tags
        )
        for name, ranked in domain_signals.items():
            signals.setdefault(name, []).extend(ranked)
    for ranked in signals.values():
        ranked.sort(key=lambda hit: (
            hit["rank_key"], hit["domain"], hit["file"],
            hit.get("ordinal", 0), hit["chunk"],
        ))
        for hit in ranked:
            hit.pop("rank_key", None)
    fused = fusion.fuse_ranked(signals, limit)
    semantic_names = {"semantic_page", "semantic_chunk"}
    lexical_names = {"lexical_page", "lexical_section"}
    for hit in fused:
        names = set(hit.pop("signals"))
        graph_origins = set(hit.get("seed_origins", [])) if "graph_page" in names else set()
        has_semantic = bool(names & semantic_names) or "semantic" in graph_origins
        has_lexical = bool(names & lexical_names) or "lexical" in graph_origins
        hit["hit"] = "both" if has_semantic and has_lexical else (
            "semantic" if has_semantic else "lexical"
        )
        hit.pop("ordinal", None)
        hit.pop("seed_origins", None)
    return fused[:limit]


def search_read(cfg: Config, base: str, domains: list[str], query: str,
                top_k: int, threshold: float, mode: str,
                type: str | None = None, tags: list | None = None) -> list[dict]:
    return prepare_read_candidates(
        cfg, base, domains, query, top_k, threshold, mode, type, tags
    )[:top_k]
```

`_domain_signals` applies `_facet_ok` before every signal, discards graph pages with no eligible record, and keeps `ordinal`, `rank_key`, and `seed_origins` internal. `search_read` removes those internal fields, so public results contain exactly `domain`, `file`, `heading`, `chunk`, `score`, `hit`, and `source`; RRF replaces the preliminary score.

Treat indexed `file` values as untrusted at the read boundary. Accept only relative
POSIX paths that resolve inside the selected domain; reject absolute paths, dot
segments, backslashes, traversal, and symlink escapes before a record can enter any
signal. Re-check the same containment invariant before hydration opens Markdown.

- [ ] **Step 5: Implement exact candidate hydration**

```python
def hydrate_candidates(cfg: Config, base: str, candidates: list[dict]) -> list[dict]:
    page_chunks: dict[tuple[str, str], dict[tuple[str, int], str] | None] = {}
    for candidate in candidates:
        page_key = (candidate["domain"], candidate["file"])
        if page_key not in page_chunks:
            path = os.path.join(domain_dir(base, page_key[0]), page_key[1])
            try:
                markdown = open(path, encoding="utf-8").read()
            except OSError:
                page_chunks[page_key] = None
            else:
                page_chunks[page_key] = {
                    (chunk.heading, chunk.chunk): chunk.text
                    for chunk in chunk_markdown(
                        page_key[1], markdown, cfg.chunk_size,
                        cfg.chunk_overlap, cfg.summary_max,
                    )
                    if chunk.kind == "section"
                }
    hydrated = []
    for candidate in candidates:
        chunks = page_chunks[(candidate["domain"], candidate["file"])]
        text = chunks.get((candidate["heading"], candidate["chunk"])) if chunks else None
        if text is not None:
            hydrated.append({**candidate, "text": text})
    return hydrated
```

Hydration must also load the existing section-record hashes once per domain and compare
the indexed `Record.hash` with the current `Chunk.hash`. A candidate is hydrated only
when its `(file, heading, chunk)` tuple exists under current chunk settings and the hashes
match; changed same-tuple chunks remain in preliminary order but are omitted from the
reranker request. Add focused regressions for changed same-tuple content and poisoned
traversal/symlink paths, while preserving normal nested-path hydration.

Add these exact hydration tests:

```python
def test_hydration_selects_exact_chunk_without_leakage_and_preserves_order(
    tmp_path, monkeypatch
):
    domain = tmp_path / "d"
    domain.mkdir()
    (domain / "a.md").write_text(
        "---\ndescription: secret summary\n---\n# A\n\n## Long\none two three four five six\n"
        "\n## Other\nunrelated body\n",
        encoding="utf-8",
    )
    (domain / "b.md").write_text("# B\n\n## First\nbeta body\n", encoding="utf-8")
    cfg = replace(_cfg(), chunk_size=3, chunk_overlap=0)
    monkeypatch.setattr(
        indexer, "embed_texts", lambda cfg, texts: [[1.0, 0.0] for _ in texts]
    )
    indexer.index_domain(cfg, str(tmp_path), "d")
    candidates = [
        {"domain": "d", "file": "b.md", "heading": "First", "chunk": 0},
        {"domain": "d", "file": "a.md", "heading": "Long", "chunk": 1},
    ]
    hydrated = retrieval.hydrate_candidates(cfg, str(tmp_path), candidates)
    assert [(item["file"], item["chunk"]) for item in hydrated] == [
        ("b.md", 0), ("a.md", 1)
    ]
    assert hydrated[1]["text"] == "## Long\nfour five six"
    assert "secret summary" not in hydrated[1]["text"]
    assert "unrelated body" not in hydrated[1]["text"]


def test_hydration_omits_changed_or_missing_tuple(tmp_path, monkeypatch):
    domain = tmp_path / "d"
    domain.mkdir()
    (domain / "a.md").write_text("# A\n\n## Current\nbody\n", encoding="utf-8")
    monkeypatch.setattr(
        indexer, "embed_texts", lambda cfg, texts: [[1.0, 0.0] for _ in texts]
    )
    indexer.index_domain(_cfg(), str(tmp_path), "d")
    candidates = [
        {"domain": "d", "file": "a.md", "heading": "Stale", "chunk": 0},
        {"domain": "d", "file": "missing.md", "heading": "Gone", "chunk": 0},
    ]
    assert retrieval.hydrate_candidates(_cfg(), str(tmp_path), candidates) == []
```

- [ ] **Step 6: Keep compatibility wrappers internal and verify GREEN**

Make `hybrid_search` delegate to `search_read`, and retain `vector_search` only for internal callers/tests by delegating with `mode="semantic"`; returned hits say `semantic`, never `vector`. Do not alter `locate_target`:

```python
def vector_search(cfg: Config, base: str, domains: list[str], query: str,
                  top_k: int, threshold: float,
                  type: str | None = None, tags: list | None = None) -> list[dict]:
    return search_read(
        cfg, base, domains, query, top_k, threshold, "semantic", type, tags
    )


def hybrid_search(cfg: Config, base: str, domains: list[str], query: str,
                  top_k: int, threshold: float, mode: str = "hybrid",
                  type: str | None = None, tags: list | None = None) -> list[dict]:
    return search_read(cfg, base, domains, query, top_k, threshold, mode, type, tags)
```

```bash
uv run pytest -q tests/test_grep.py tests/test_retrieval.py tests/test_retrieval_facets.py tests/engine/test_hier.py tests/engine/test_fusion.py
```

Expected: all focused retrieval tests pass; lexical mode records zero embedding calls; every returned result is JSON serializable.

- [ ] **Step 7: Commit the preliminary pipeline**

```bash
git add src/iwiki_mcp/engine/grep.py src/iwiki_mcp/retrieval.py tests/test_grep.py tests/test_retrieval.py tests/test_retrieval_facets.py
git commit -m "feat: fuse broad search candidates"
```

### Task 6: Add the LiteLLM Reranker Boundary

**Files:**
- Create: `tests/engine/test_rerank.py`
- Create: `src/iwiki_mcp/engine/rerank.py`

- [ ] **Step 1: Write the successful request and mapping tests**

```python
import httpx
import pytest

from iwiki_mcp.engine import rerank
from iwiki_mcp.engine.config import Config


def _cfg():
    return Config(
        base_url="https://litellm.test/v1", api_key="secret", embed_model="embed",
        dimensions=2, chunk_size=512, chunk_overlap=64, summary_max=400,
        top_k=8, score_threshold=0.2, graph_depth=2, ignore=None,
        rerank_model="rerank-model",
    )


def _candidates():
    return [
        {"domain": "d", "file": "a.md", "heading": "A", "chunk": 0,
         "score": 0.1, "hit": "semantic", "source": "global", "text": "alpha"},
        {"domain": "d", "file": "b.md", "heading": "B", "chunk": 0,
         "score": 0.2, "hit": "lexical", "source": "lexical", "text": "beta"},
    ]


def test_rerank_posts_one_authenticated_batch_and_maps_scores(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [
                {"index": 0, "relevance_score": 0.3},
                {"index": 1, "relevance_score": 0.9},
            ]}

    monkeypatch.setattr(
        rerank.httpx, "post",
        lambda url, **kwargs: calls.append((url, kwargs)) or Response(),
    )
    ranked, metadata = rerank.rerank_candidates(_cfg(), "query", _candidates())
    assert calls == [("https://litellm.test/v1/rerank", {
        "json": {"model": "rerank-model", "query": "query",
                 "documents": ["alpha", "beta"], "top_n": 2},
        "headers": {"Authorization": "Bearer secret"}, "timeout": 60.0,
    })]
    assert [item["file"] for item in ranked] == ["b.md", "a.md"]
    assert [item["score"] for item in ranked] == [0.9, 0.3]
    assert ranked[0]["hit"] == "lexical" and ranked[0]["source"] == "lexical"
    assert metadata == {"applied": True}
```

- [ ] **Step 2: Write all fail-soft validation tests**

```python
@pytest.mark.parametrize(
    "payload",
    [
        {}, {"results": "bad"},
        {"results": [{"index": True, "relevance_score": 0.5}]},
        {"results": [{"index": 9, "relevance_score": 0.5}]},
        {"results": [{"index": 0, "relevance_score": True}]},
        {"results": [{"index": 0, "relevance_score": float("nan")}]},
        {"results": [
            {"index": 0, "relevance_score": 0.8},
            {"index": 0, "relevance_score": 0.7},
        ]},
        {"results": [
            {"index": 0, "relevance_score": True},
            {"index": 0, "relevance_score": 0.8},
        ]},
        {"results": [{"index": 0, "relevance_score": 10 ** 10000}]},
    ],
)
def test_invalid_response_preserves_preliminary_order(monkeypatch, payload):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    monkeypatch.setattr(rerank.httpx, "post", lambda *args, **kwargs: Response())
    candidates = _candidates()
    ranked, metadata = rerank.rerank_candidates(_cfg(), "query", candidates)
    assert [item["file"] for item in ranked] == ["a.md", "b.md"]
    assert metadata == {"applied": False, "warning": "reranker unavailable"}


@pytest.mark.parametrize(
    "error",
    [httpx.TimeoutException("secret timeout"), httpx.ConnectError("secret transport")],
)
def test_transport_failure_is_sanitized(monkeypatch, error):
    monkeypatch.setattr(rerank.httpx, "post", lambda *args, **kwargs: (_ for _ in ()).throw(error))
    ranked, metadata = rerank.rerank_candidates(_cfg(), "query", _candidates())
    assert [item["file"] for item in ranked] == ["a.md", "b.md"]
    assert metadata == {"applied": False, "warning": "reranker unavailable"}
    assert "secret" not in str(metadata)


def test_http_error_and_malformed_json_are_sanitized(monkeypatch):
    request = httpx.Request("POST", "https://litellm.test/v1/rerank")
    response = httpx.Response(500, request=request, text="secret provider body")
    monkeypatch.setattr(rerank.httpx, "post", lambda *args, **kwargs: response)
    _, metadata = rerank.rerank_candidates(_cfg(), "query", _candidates())
    assert metadata == {"applied": False, "warning": "reranker unavailable"}
    assert "secret provider body" not in str(metadata)

    class Malformed:
        def raise_for_status(self):
            return None

        def json(self):
            raise ValueError("secret malformed body")

    monkeypatch.setattr(rerank.httpx, "post", lambda *args, **kwargs: Malformed())
    _, metadata = rerank.rerank_candidates(_cfg(), "query", _candidates())
    assert metadata == {"applied": False, "warning": "reranker unavailable"}


def test_partial_response_keeps_unranked_candidates_in_preliminary_order(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{"index": 1, "relevance_score": 0.7}]}

    monkeypatch.setattr(rerank.httpx, "post", lambda *args, **kwargs: Response())
    ranked, metadata = rerank.rerank_candidates(_cfg(), "query", _candidates())
    assert [item["file"] for item in ranked] == ["b.md", "a.md"]
    assert metadata == {"applied": True}
```

- [ ] **Step 3: Verify RED**

```bash
uv run pytest -q tests/engine/test_rerank.py
```

Expected: collection fails because `engine.rerank` does not exist.

- [ ] **Step 4: Implement one-batch fail-soft reranking**

```python
"""Fail-soft LiteLLM-compatible reranking for hydrated search candidates."""
from __future__ import annotations

import math
import numbers

import httpx

from .config import Config

_TIMEOUT = 60.0
_WARNING = {"applied": False, "warning": "reranker unavailable"}


def rerank_candidates(cfg: Config, query: str,
                      candidates: list[dict]) -> tuple[list[dict], dict]:
    preliminary = [{k: v for k, v in item.items() if k != "text"} for item in candidates]
    if not candidates:
        return preliminary, dict(_WARNING)
    payload = {
        "model": cfg.rerank_model, "query": query,
        "documents": [item["text"] for item in candidates],
        "top_n": len(candidates),
    }
    try:
        response = httpx.post(
            f"{cfg.base_url}/rerank", json=payload,
            headers={"Authorization": f"Bearer {cfg.api_key}"}, timeout=_TIMEOUT,
        )
        response.raise_for_status()
        rows = response.json().get("results")
    except (httpx.HTTPError, ValueError, AttributeError):
        return preliminary, dict(_WARNING)
    if not isinstance(rows, list):
        return preliminary, dict(_WARNING)
    scores: dict[int, float] = {}
    seen = set()
    duplicate = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        index = row.get("index")
        score = row.get("relevance_score")
        if (not isinstance(index, int) or isinstance(index, bool)
                or not 0 <= index < len(preliminary)):
            continue
        if index in seen:
            duplicate.add(index)
            continue
        seen.add(index)
        if not isinstance(score, numbers.Real) or isinstance(score, bool):
            continue
        try:
            numeric_score = float(score)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(numeric_score):
            continue
        scores[index] = numeric_score
    for index in duplicate:
        scores.pop(index, None)
    if not scores:
        return preliminary, dict(_WARNING)
    ranked_indices = sorted(scores, key=lambda index: (-scores[index], index))
    ranked_indices.extend(index for index in range(len(preliminary)) if index not in scores)
    ranked = []
    for index in ranked_indices:
        item = dict(preliminary[index])
        if index in scores:
            item["score"] = scores[index]
        ranked.append(item)
    return ranked, {"applied": True}
```

- [ ] **Step 5: Verify GREEN and commit**

```bash
uv run pytest -q tests/engine/test_rerank.py
git add src/iwiki_mcp/engine/rerank.py tests/engine/test_rerank.py
git commit -m "feat: add fail-soft LiteLLM reranking"
```

Expected: exact request test and every failure-class test pass; output never includes URL, model, key, response body, or raw exception.

### Task 7: Wire Public Mode Precedence, Reranking, and Write Isolation

**Files:**
- Modify: `tests/test_server_search.py`
- Modify: `tests/test_server_search_facets.py`
- Modify: `tests/test_server_search_write_intent.py`
- Modify: `tests/test_robustness_fixes.py`
- Modify: `src/iwiki_mcp/server.py`

- [ ] **Step 1: Write failing public mode and precedence tests**

Add `import pytest` to `tests/test_server_search.py`, then add:

```python
@pytest.mark.parametrize("mode", ["hybrid", "lexical", "semantic"])
def test_search_accepts_canonical_modes(tmp_path, monkeypatch, mode):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_search("token", mode=mode, threshold=0.0)
    assert "error" not in out
    assert "results" in out


def test_search_rejects_vector_with_allowed_values(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_search("token", mode="vector")
    assert "error" in out
    assert "hybrid, lexical, semantic" in out["error"]


@pytest.mark.parametrize("configured", ["lexical", "semantic"])
def test_omitted_mode_uses_environment_default(tmp_path, monkeypatch, configured):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setenv("IWIKI_SEARCH_MODE", configured)
    captured = {}

    def capture(*args, mode, **kwargs):
        captured["mode"] = mode
        return []

    monkeypatch.setattr(
        server.retrieval, "prepare_read_candidates", capture,
    )
    server.wiki_search("token")
    assert captured["mode"] == configured


def test_explicit_mode_overrides_environment_default(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setenv("IWIKI_SEARCH_MODE", "lexical")
    captured = {}

    def capture(*args, mode, **kwargs):
        captured["mode"] = mode
        return []

    monkeypatch.setattr(
        server.retrieval, "prepare_read_candidates", capture,
    )
    server.wiki_search("token", mode="semantic")
    assert captured["mode"] == "semantic"
```

- [ ] **Step 2: Write failing reranker response-shape tests**

Cover disabled shape, successful metadata, sanitized failure metadata, and stale hydration:

```python
def test_disabled_reranker_keeps_existing_top_level_shape(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.delenv("IWIKI_RERANK_MODEL", raising=False)
    monkeypatch.setattr(
        server.rerank, "rerank_candidates",
        lambda *args: (_ for _ in ()).throw(AssertionError("reranker called")),
    )
    out = server.wiki_search("token", threshold=0.0)
    assert set(out) == {"results"}


def test_configured_reranker_adds_metadata(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setenv("IWIKI_RERANK_MODEL", "model")
    monkeypatch.setattr(
        server.rerank, "rerank_candidates",
        lambda cfg, query, candidates: (
            [{k: v for k, v in item.items() if k != "text"} for item in reversed(candidates)],
            {"applied": True},
        ),
    )
    out = server.wiki_search("token", threshold=0.0)
    assert out["rerank"] == {"applied": True}
    assert all("text" not in item for item in out["results"])


def test_reranker_can_promote_candidate_below_preliminary_top_k(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setenv("IWIKI_RERANK_MODEL", "model")
    candidates = [
        {"domain": "backend", "file": f"{name}.md", "heading": "H", "chunk": 0,
         "score": score, "hit": "semantic", "source": "global"}
        for name, score in (("first", 0.3), ("second", 0.2), ("promoted", 0.1))
    ]
    monkeypatch.setattr(
        server.retrieval, "prepare_read_candidates", lambda *args, **kwargs: candidates,
    )
    monkeypatch.setattr(
        server.retrieval, "hydrate_candidates",
        lambda cfg, base, items: [{**item, "text": item["file"]} for item in items],
    )
    monkeypatch.setattr(
        server.rerank, "rerank_candidates",
        lambda cfg, query, items: (
            [{key: value for key, value in item.items() if key != "text"}
             for item in reversed(items)],
            {"applied": True},
        ),
    )
    out = server.wiki_search("token", k=2)
    assert [item["file"] for item in out["results"]] == ["promoted.md", "second.md"]


def test_reranker_failure_preserves_complete_preliminary_order(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setenv("IWIKI_RERANK_MODEL", "model")
    preliminary = [
        {"domain": "backend", "file": "auth.md", "heading": "Token", "chunk": 0,
         "score": 0.7, "hit": "both", "source": "seed"},
        {"domain": "backend", "file": "stale.md", "heading": "Missing", "chunk": 0,
         "score": 0.6, "hit": "semantic", "source": "global"},
    ]
    monkeypatch.setattr(
        server.retrieval, "prepare_read_candidates",
        lambda *args, **kwargs: preliminary,
    )
    monkeypatch.setattr(
        server.rerank, "rerank_candidates",
        lambda cfg, query, candidates: (
            [{key: value for key, value in item.items() if key != "text"}
             for item in candidates],
            {"applied": False, "warning": "reranker unavailable"},
        ),
    )
    out = server.wiki_search("token")
    assert out["results"] == preliminary
    assert out["rerank"] == {"applied": False, "warning": "reranker unavailable"}
```

For configured reranking with zero hydrated candidates, assert preliminary results remain returned and metadata is the sanitized warning.

- [ ] **Step 3: Prove write intent never selects modes or reranking**

```python
def test_search_write_intent_ignores_search_mode_and_reranker(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch, "d")
    monkeypatch.setenv("IWIKI_SEARCH_MODE", "lexical")
    monkeypatch.setenv("IWIKI_RERANK_MODEL", "model")
    monkeypatch.setattr(
        server.rerank, "rerank_candidates",
        lambda *args: (_ for _ in ()).throw(AssertionError("reranker called")),
    )
    server.wiki_write_page(
        "d", "retrieval", "# Retrieval\n\n## Purpose\nBody.\n",
        type="architecture", description="purpose of retrieval",
    )
    result = server.wiki_search(
        "purpose of retrieval", intent="write", mode="semantic", heading="Purpose"
    )
    assert result["target"]["exists"] is True
```

- [ ] **Step 4: Verify RED**

```bash
uv run pytest -q tests/test_server_search.py tests/test_server_search_facets.py tests/test_server_search_write_intent.py tests/test_robustness_fixes.py
```

Expected: new tests fail because mode is required/defaulted to `hybrid` in the signature and reranking is not wired.

- [ ] **Step 5: Implement optional enum mode and read-only reranking**

Import `Literal`, `retrieval`, and `engine.rerank`. Change only the public mode annotation/default:

```python
    mode: Literal["hybrid", "lexical", "semantic"] | None = None,
```

After the write-intent early return, resolve and validate the read mode:

```python
    resolved_mode = cfg.search_mode if mode is None else mode.strip().lower()
    allowed_modes = ("hybrid", "lexical", "semantic")
    if resolved_mode not in allowed_modes:
        return {"error": "invalid search mode; allowed values: hybrid, lexical, semantic"}
```

Replace `hybrid_search` with `prepare_read_candidates`, passing `mode=resolved_mode`. Resolve `requested_top_k` once, keep `candidates` at the internal ceiling, and derive the disabled/failure preliminary result before optional reranking:

```python
    requested_top_k = cfg.top_k if k is None else k
    candidates = retrieval.prepare_read_candidates(
        cfg, bind.base, doms, query, top_k=requested_top_k,
        threshold=cfg.score_threshold if threshold is None else threshold,
        mode=resolved_mode, type=q_type, tags=q_tags,
    )
    results = candidates[:requested_top_k]
    response = {"results": results}
    if cfg.rerank_model:
        hydrated = retrieval.hydrate_candidates(cfg, bind.base, candidates)
        ranked, metadata = rerank.rerank_candidates(cfg, query, hydrated)
        if metadata["applied"]:
            hydrated_keys = {
                (item["domain"], item["file"], item["heading"], item["chunk"])
                for item in hydrated
            }
            stale = [
                item for item in candidates
                if (item["domain"], item["file"], item["heading"], item["chunk"])
                not in hydrated_keys
            ]
            results = (ranked + stale)[:requested_top_k]
        response = {"results": results, "rerank": metadata}
    return response
```

Preserve the `intent="write"` branch above all read-mode validation and reranker work. A failed/fully invalid reranker response returns preliminary top-k byte-for-byte; successful reranking considers the full candidate ceiling, places hydrated candidates in reranker order, appends unhydrated candidates in their original relative order, and only then truncates to final top-k. Update facet/robustness fakes to patch `prepare_read_candidates` and accept its keyword arguments.

- [ ] **Step 6: Verify GREEN and embedding-error visibility**

Add this test, importing `EmbedError` from `iwiki_mcp.engine.embed`:

```python
def test_embedding_error_remains_visible_when_reranker_is_configured(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setenv("IWIKI_RERANK_MODEL", "model")
    monkeypatch.setattr(
        server.retrieval, "prepare_read_candidates",
        lambda *args, **kwargs: (_ for _ in ()).throw(EmbedError("embedding unavailable")),
    )
    monkeypatch.setattr(
        server.rerank, "rerank_candidates",
        lambda *args: (_ for _ in ()).throw(AssertionError("reranker called")),
    )
    out = server.wiki_search("token")
    assert out == {"error": "embedding unavailable"}
```

```bash
uv run pytest -q tests/test_server_search.py tests/test_server_search_facets.py tests/test_server_search_write_intent.py tests/test_robustness_fixes.py tests/engine/test_rerank.py
```

Expected: focused server tests pass; write intent makes no reranker request; retrieval failures remain visible.

- [ ] **Step 7: Commit server integration**

```bash
git add src/iwiki_mcp/server.py tests/test_server_search.py tests/test_server_search_facets.py tests/test_server_search_write_intent.py tests/test_robustness_fixes.py
git commit -m "feat: expose semantic search and reranking"
```

### Task 8: Strengthen the MCP Tool and Schema Smoke

**Files:**
- Modify: `tests/test_mcp_smoke.py`

- [ ] **Step 1: Write the complete registration and schema assertions**

Replace the subset assertion with the exact public set and inspect `wiki_search` recursively for the optional enum:

```python
EXPECTED_TOOLS = {
    "wiki_status", "wiki_list_domains", "wiki_list_pages", "wiki_read_page",
    "wiki_search", "wiki_related", "wiki_write_page", "wiki_update_page",
    "wiki_delete_page", "wiki_index", "wiki_create_domain", "wiki_bind",
    "wiki_lint", "wiki_remediation_plan", "wiki_migrate_okf", "wiki_apply_okf",
    "wiki_export_okf", "wiki_sync",
}


def _enum_values(schema):
    if isinstance(schema, dict):
        values = set(schema.get("enum", []))
        for value in schema.values():
            values.update(_enum_values(value))
        return values
    if isinstance(schema, list):
        values = set()
        for value in schema:
            values.update(_enum_values(value))
        return values
    return set()
```

Inside the MCP session:

```python
listed = (await session.list_tools()).tools
tools = {tool.name: tool for tool in listed}
assert set(tools) == EXPECTED_TOOLS
search_schema = tools["wiki_search"].inputSchema
assert "mode" not in search_schema.get("required", [])
assert _enum_values(search_schema["properties"]["mode"]) == {
    "hybrid", "lexical", "semantic"
}
```

- [ ] **Step 2: Verify the smoke catches the old schema**

```bash
uv run pytest -q tests/test_mcp_smoke.py
```

Expected before Task 7: FAIL because old schema contains no canonical enum. Expected after Task 7: PASS with exactly 18 tools.

- [ ] **Step 3: Commit the smoke contract**

```bash
git add tests/test_mcp_smoke.py
git commit -m "test: verify complete MCP search schema"
```

### Task 9: Prove the Quality Gate and Record the Selected Ceiling

**Files:**
- Modify: `eval/hierarchical/harness.py`
- Modify: `tests/eval/test_hierarchical_eval.py`
- Modify: `docs/superpowers/evidence/configurable-search-mode-api-eval.md`

- [ ] **Step 1: Add preliminary and fake-reranker evaluation paths**

Use `retrieval.search_read` against a temporary indexed domain with deterministic fixture embeddings. Return the same `_metrics` shape as baseline. Add a fake reranker that moves relevant identities first while preserving preliminary order within relevant/non-relevant groups.

```python
def fake_rerank(ranked: list[tuple[str, str]], relevant: set[tuple[str, str]]):
    ordered = sorted(
        enumerate(ranked), key=lambda pair: (0 if pair[1] in relevant else 1, pair[0])
    )
    return [item for _, item in ordered]


def _run_current_pipeline(vault, queries, embed_fn, top_k: int,
                          rerank_fn=None) -> dict:
    with tempfile.TemporaryDirectory() as root:
        domain = Path(root, "eval")
        domain.mkdir()
        summaries, sections = _records(vault, embed_fn)
        for file, markdown in vault.items():
            path = domain / file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(markdown, encoding="utf-8")
        VectorStore(str(domain / "index.jsonl")).save(summaries + sections)
        dimensions = len(embed_fn("dimension probe"))
        cfg = Config(
            base_url="http://offline.test/v1", api_key="offline",
            embed_model="offline", dimensions=dimensions,
            chunk_size=512, chunk_overlap=64, summary_max=400,
            top_k=top_k, score_threshold=0.0, graph_depth=1, ignore=None,
            seed_top_k=5, bfs_top_k=10, seed_threshold=0.0,
        )
        rankings = []
        with patch(
            "iwiki_mcp.retrieval.embed_texts",
            side_effect=lambda cfg, texts: [embed_fn(text) for text in texts],
        ):
            for query in queries:
                candidates = retrieval.prepare_read_candidates(
                    cfg, root, ["eval"], query["query"], top_k, 0.0, "hybrid"
                )
                ranked = [(hit["file"], hit["heading"]) for hit in candidates]
                if rerank_fn is not None:
                    ranked = rerank_fn(ranked, set(query["relevant"]))
                rankings.append(ranked[:top_k])
        return _metrics(rankings, queries)
```

Import `Path`, `patch`, `retrieval`, `Config`, and `VectorStore` in the harness. This path writes the same deterministic `Record` objects used by baseline into the real JSONL store and patches only query embedding; it makes no HTTP request.

Expose:

```python
def run_preliminary_eval(vault, queries, embed_fn, top_k: int = 3) -> dict:
    return _run_current_pipeline(vault, queries, embed_fn, top_k, rerank_fn=None)


def run_fake_reranker_eval(vault, queries, embed_fn, top_k: int = 3) -> dict:
    return _run_current_pipeline(vault, queries, embed_fn, top_k, rerank_fn=fake_rerank)
```

- [ ] **Step 2: Add the acceptance assertions**

```python
def test_preliminary_pipeline_improves_mrr_without_recall_loss():
    baseline = harness.run_baseline_eval(
        fixtures.VAULT, fixtures.QUERIES, fixtures.embed, top_k=3
    )
    preliminary = harness.run_preliminary_eval(
        fixtures.VAULT, fixtures.QUERIES, fixtures.embed, top_k=3
    )
    assert preliminary["recall_at_k"] >= baseline["recall_at_k"]
    assert preliminary["mrr_at_k"] > baseline["mrr_at_k"]


def test_fake_reranker_improves_or_preserves_preliminary_quality():
    preliminary = harness.run_preliminary_eval(
        fixtures.VAULT, fixtures.QUERIES, fixtures.embed, top_k=3
    )
    reranked = harness.run_fake_reranker_eval(
        fixtures.VAULT, fixtures.QUERIES, fixtures.embed, top_k=3
    )
    assert reranked["recall_at_k"] >= preliminary["recall_at_k"]
    assert reranked["mrr_at_k"] >= preliminary["mrr_at_k"]
```

- [ ] **Step 3: Run the gate**

```bash
uv run pytest -q tests/eval/test_hierarchical_eval.py
uv run python -c 'from eval.hierarchical import fixtures, harness; print({"baseline": harness.run_baseline_eval(fixtures.VAULT, fixtures.QUERIES, fixtures.embed, 3), "preliminary": harness.run_preliminary_eval(fixtures.VAULT, fixtures.QUERIES, fixtures.embed, 3), "fake_reranker": harness.run_fake_reranker_eval(fixtures.VAULT, fixtures.QUERIES, fixtures.embed, 3)})'
```

Expected: tests pass; preliminary MRR is strictly higher, recall does not decrease, and fake reranking preserves recall while improving or preserving MRR.

**HUMAN CHECKPOINT:** If `CANDIDATE_LIMIT = 32` fails either preliminary assertion, stop. Record the failing metrics and ask before changing scoring, fixture relevance, RRF constant, or candidate ceiling; do not weaken acceptance.

- [ ] **Step 4: Complete the evidence with observed metrics**

Append exact command output plus:

```markdown
## Result

`CANDIDATE_LIMIT = 32` and `RRF k = 60` are accepted only because preliminary `MRR@3` is above baseline and preliminary `recall@3` is not below baseline. The deterministic fake reranker demonstrates ordering improvement without a live provider dependency.

## Reproduction

Run `uv run pytest -q tests/eval/test_hierarchical_eval.py` and the metric command recorded above. Both are offline and deterministic.
```

- [ ] **Step 5: Commit evaluation evidence**

```bash
git add eval/hierarchical/harness.py tests/eval/test_hierarchical_eval.py docs/superpowers/evidence/configurable-search-mode-api-eval.md
git commit -m "test: verify fused search quality"
```

### Task 10: Synchronize Repository and iwiki Documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/README.ru.md`
- Modify: `docs/reports/iwiki-mcp-server-report.html`
- Modify: `templates/AGENTS.md.snippet`
- Modify: `templates/CLAUDE.md.snippet`
- Modify: `src/iwiki_mcp/resources.py`
- Modify: `tests/test_resources.py`
- Update iwiki: all search-contract sections of `iwiki-mcp/retrieval`
- Update iwiki: `iwiki-mcp/mcp-server` section `Tool surface`

- [ ] **Step 1: Write documentation contract tests**

```python
def test_authoring_rules_describe_current_search_and_update_tools():
    assert "hybrid`, `lexical`, and `semantic" in AUTHORING_RULES
    assert "IWIKI_SEARCH_MODE" in AUTHORING_RULES
    assert "IWIKI_RERANK_MODEL" in AUTHORING_RULES
    assert "wiki_update_page" in AUTHORING_RULES
    assert "wiki_remediation_plan" in AUTHORING_RULES
```

Add a repository-text test, importing `Path`:

```python
def test_agent_snippets_use_supported_existing_page_update_path():
    root = Path(__file__).parents[1]
    for relative in ("templates/AGENTS.md.snippet", "templates/CLAUDE.md.snippet"):
        text = (root / relative).read_text(encoding="utf-8")
        assert "wiki_update_page" in text
        assert "Do not imply the tool can update existing pages directly" not in text
```

Add regressions that read both READMEs and the standalone server report. They
must reject the old description-as-section-prefix claim, require separate
summary-vector wording, reject `Hybrid / vector / lexical`, and assert that the
report's tool table contains the exact 18-tool surface.

- [ ] **Step 2: Verify RED**

```bash
uv run pytest -q tests/test_resources.py
```

Expected: new assertions fail against current resource/snippets.

- [ ] **Step 3: Update English and Russian public docs**

In both READMEs, document:

- public modes exactly `hybrid`, `lexical`, `semantic`; `vector` remains only an internal implementation term;
- `IWIKI_SEARCH_MODE` default/normalization/explicit precedence;
- optional `IWIKI_RERANK_MODEL`, reused URL/key, one 60-second batch, sanitized fail-soft metadata;
- independent semantic page, lexical page, graph, global semantic chunk, and lexical section signals fused before final top-k;
- exact result `hit` and `source` values;
- `description` is a separate page-summary vector and is never prefixed to section vectors;
- graph-reachable pages do not require their own description to enter through graph expansion;
- complete 18-tool surface including `wiki_remediation_plan`;
- `wiki_update_page` as the supported existing-section edit path;
- reserved root `index.md`/`log.md` behavior under type directories;
- the project-relative stale-source issue as an explicitly separate follow-up.

Keep the two language versions semantically equivalent.

- [ ] **Step 4: Update templates and MCP authoring resource**

Replace the stale template instruction with:

```markdown
- **After changing functionality:** use `wiki_write_page` for a new page,
  `wiki_update_page` for one existing `##` section, and `wiki_delete_page` only
  when the documented source was removed. Run `wiki_lint` after the write.
```

Add a compact search section to `AUTHORING_RULES` with the canonical modes, environment default, optional reranking, complete write/update/delete guidance, and remediation-plan name. Keep internal vector terminology only where it describes embeddings.

- [ ] **Step 5: Verify repository terminology**

```bash
uv run pytest -q tests/test_resources.py
rg -n 'mode.?=.?("|`)?vector|Modes:.*vector|Режимы:.*vector|Hybrid / vector / lexical' README.md docs/README.ru.md docs/reports/iwiki-mcp-server-report.html templates src/iwiki_mcp/resources.py
rg -n 'wiki_remediation_plan|wiki_update_page|IWIKI_SEARCH_MODE|IWIKI_RERANK_MODEL' README.md docs/README.ru.md docs/reports/iwiki-mcp-server-report.html templates src/iwiki_mcp/resources.py
```

Expected: first search exits 1 with no public vector-mode claim; second finds each required term in the appropriate English/Russian/template/resource surfaces.

- [ ] **Step 6: Update the bound iwiki pages through MCP**

Read the complete `retrieval` and `mcp-server` pages before updating them through
`wiki_update_page`. `retrieval/Hybrid search`, `Vector search`, `Lexical search`,
and `Result shape` must agree on canonical modes, five independent signals, RRF,
exact `hit`/`source`, hydration, internal-only vector terminology, and fail-soft
reranking. `mcp-server/Tool surface` must list all 18 tools and the optional
`wiki_search.mode` enum/default precedence. Refresh `installation/Required
environment` whenever a README edit changes its source hash. Use the changed
source paths `src/iwiki_mcp/retrieval.py`, `src/iwiki_mcp/server.py`, and
`README.md` respectively.

Run `wiki_lint(domain="iwiki-mcp")`.

Expected: no broken or stale findings introduced. The pre-existing `architecture.md` orphan and advisory long-lead/tag-drift findings may remain; record them as pre-existing rather than editing unrelated pages.

- [ ] **Step 7: Commit documentation**

```bash
git add README.md docs/README.ru.md docs/reports/iwiki-mcp-server-report.html templates/AGENTS.md.snippet templates/CLAUDE.md.snippet src/iwiki_mcp/resources.py tests/test_resources.py
git commit -m "docs: document semantic search and reranking"
```

### Task 11: Release and Verify the Complete Change

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`
- Verify: all changed implementation, tests, docs, templates, evidence, and iwiki pages

- [ ] **Step 1: Set the intentional minor release**

Change package metadata from `0.6.10` to `0.7.1`:

```toml
version = "0.7.1"
```

Set the package constant to the same value:

```python
__version__ = "0.7.1"
```

Refresh only lock metadata:

```bash
uv lock
```

- [ ] **Step 2: Run focused verification**

```bash
uv run pytest -q tests/engine/test_config.py tests/engine/test_fusion.py tests/engine/test_hier.py tests/engine/test_rerank.py tests/test_retrieval.py tests/test_retrieval_facets.py tests/test_server_search.py tests/test_server_search_facets.py tests/test_server_search_write_intent.py tests/test_mcp_smoke.py tests/eval/test_hierarchical_eval.py tests/test_resources.py
```

Expected: all focused tests pass.

- [ ] **Step 3: Run mandatory repository verification**

```bash
uv run pytest -q
uv run flake8 src tests eval
uv run iwiki-mcp --help
uv run python -c 'import iwiki_mcp; assert iwiki_mcp.__version__ == "0.7.1"'
```

Expected: full pytest and flake8 exit 0; help exits 0 without a model request; version assertion exits 0.

- [ ] **Step 4: Run read-only live mode checks**

Call the bound MCP `wiki_search` with the same harmless query in explicit `hybrid`, `lexical`, and `semantic` modes. Record result counts, returned `hit`/`source` values, and whether configured reranking reports `applied` or the sanitized warning. If provider credentials/model are not already configured, record the exact external blocker; do not modify deployment, credentials, or acceptance.

Expected: each canonical mode returns a normal `results` response; `vector` is rejected through MCP validation; no write tool is called.

- [ ] **Step 5: Check final diff scope and docs health**

```bash
git status --short
git diff --check
git diff --stat
```

Run `wiki_lint(domain="iwiki-mcp")` again.

Expected: no whitespace errors, no unrelated implementation paths, no broken/stale wiki findings introduced, and all planned artifacts appear in the diff/history.

- [ ] **Step 6: Commit the release metadata**

```bash
git add pyproject.toml src/iwiki_mcp/__init__.py uv.lock
git commit -m "chore: release version 0.7.1"
```

- [ ] **Step 7: Run chain reconciliation before handoff**

Invoke `$check-chain result docs/superpowers/plans/2026-07-15-configurable-search-mode-api.md` and resolve every critical finding before claiming completion.

Expected: result verdict `OK`, all R1-R14 mapped to implementation/test/docs evidence, and the `configurable-search-mode-api` task-log row closes.
