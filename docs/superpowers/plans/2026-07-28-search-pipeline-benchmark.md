---
chain:
  intent: docs/superpowers/intents/2026-07-28-search-pipeline-benchmark-intent.md
  spec: docs/superpowers/specs/2026-07-28-search-pipeline-benchmark-design.md
---

# Search Pipeline Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a live-first benchmark in `eval/search_pipeline/` that measures the current wiki search context pipeline, identifies bottlenecks with evidence, and emits JSON plus Markdown/HTML reports.

**Architecture:** Add a standalone eval package under `eval/search_pipeline/`. Keep production `wiki_search` behavior unchanged; instrumentation may call existing private retrieval helpers from eval code to collect stage evidence. Live benchmark runs are read-only and env-gated, while deterministic offline fixtures test metrics, classifier, reports, and safety guards.

**Tech Stack:** Python standard library, existing `iwiki_mcp` modules, pytest, local Markdown/HTML generation with no new runtime dependency.

---

## File Structure

- Create `eval/search_pipeline/__init__.py`: package marker and public version-free exports.
- Create `eval/search_pipeline/metrics.py`: quality, source-mix, and latency metrics.
- Create `eval/search_pipeline/fixtures.py`: live benchmark cases plus deterministic offline cases.
- Create `eval/search_pipeline/envfile.py`: operator env-file loader and safety checks.
- Create `eval/search_pipeline/instrumentation.py`: read-only stage tracing around retrieval, hydration, and optional rerank.
- Create `eval/search_pipeline/analyzer.py`: bottleneck classification and ranked backlog.
- Create `eval/search_pipeline/report.py`: sanitized JSON, Markdown, and HTML report generation.
- Create `eval/search_pipeline/runner.py`: benchmark orchestration for live and offline cases.
- Create `eval/search_pipeline/__main__.py`: CLI entry point.
- Create `tests/eval/test_search_pipeline_metrics.py`: metrics tests.
- Create `tests/eval/test_search_pipeline_envfile.py`: credential file and sanitization guard tests.
- Create `tests/eval/test_search_pipeline_analyzer.py`: bottleneck classifier tests.
- Create `tests/eval/test_search_pipeline_report.py`: JSON/Markdown/HTML schema tests.
- Create `tests/eval/test_search_pipeline_runner.py`: offline runner and CLI smoke tests.
- Modify `docs/TODO.md`: mark plan checked after validation.
- Modify `pyproject.toml`, `src/iwiki_mcp/__init__.py`, and `uv.lock`: patch version bump for the implementation change.

---

### Task 1: Metrics And Fixture Contracts

**Files:**
- Create: `eval/search_pipeline/__init__.py`
- Create: `eval/search_pipeline/metrics.py`
- Create: `eval/search_pipeline/fixtures.py`
- Test: `tests/eval/test_search_pipeline_metrics.py`

- [ ] **Step 1: Write failing metrics tests**

Create `tests/eval/test_search_pipeline_metrics.py`:

```python
import pytest

from eval.search_pipeline.fixtures import BenchmarkCase
from eval.search_pipeline.metrics import (
    identity,
    intent_coverage_at_k,
    latency_summary,
    mrr_at_k,
    ndcg_at_k,
    recall_at_k,
    source_mix,
)


def test_identity_uses_public_search_fields():
    assert identity({
        "domain": "iwiki-mcp",
        "file": "retrieval.md",
        "heading": "Hybrid search",
        "chunk": 0,
    }) == "iwiki-mcp/retrieval.md#Hybrid search:0"


def test_quality_metrics_use_case_relevance():
    case = BenchmarkCase(
        case_id="modes",
        domain="iwiki-mcp",
        query="search mode enum",
        relevant={
            "iwiki-mcp/mcp-server.md#Tool surface:0": 3,
            "iwiki-mcp/retrieval.md#Hybrid search:0": 2,
        },
        intents={
            "api": ["iwiki-mcp/mcp-server.md#Tool surface:0"],
            "retrieval": ["iwiki-mcp/retrieval.md#Hybrid search:0"],
        },
    )
    ranking = [
        "iwiki-mcp/retrieval.md#Hybrid search:0",
        "iwiki-mcp/indexing.md#Configuration:0",
        "iwiki-mcp/mcp-server.md#Tool surface:0",
    ]

    assert recall_at_k(ranking, case, 2) == 0.5
    assert mrr_at_k(ranking, case, 3) == 1.0
    assert ndcg_at_k(ranking, case, 3) == pytest.approx(0.730929, rel=1e-6)
    assert intent_coverage_at_k(ranking, case, 2) == 0.5


def test_source_mix_counts_hit_and_source_fields():
    mix = source_mix([
        {"hit": "both", "source": "seed"},
        {"hit": "lexical", "source": "lexical"},
        {"hit": "semantic", "source": "graph"},
    ])

    assert mix == {
        "hit": {"both": 1, "lexical": 1, "semantic": 1},
        "source": {"graph": 1, "lexical": 1, "seed": 1},
    }


def test_latency_summary_is_stable_and_rounded():
    assert latency_summary({"embed_ms": 1.23456, "fusion_ms": 0.1}) == {
        "embed_ms": 1.235,
        "fusion_ms": 0.1,
        "total_ms": 1.335,
    }
```

- [ ] **Step 2: Run metrics tests and verify they fail**

Run:

```bash
uv run pytest -q tests/eval/test_search_pipeline_metrics.py
```

Expected: import failure for `eval.search_pipeline`.

- [ ] **Step 3: Add fixture dataclass and default cases**

Create `eval/search_pipeline/__init__.py`:

```python
"""Live-first benchmark for iwiki search pipeline diagnostics."""
```

Create `eval/search_pipeline/fixtures.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    domain: str
    query: str
    relevant: dict[str, int]
    intents: dict[str, list[str]] = field(default_factory=dict)
    k: int = 8


DEFAULT_LIVE_CASES = [
    BenchmarkCase(
        case_id="search-mode-api",
        domain="iwiki-mcp",
        query="IWIKI_SEARCH_MODE semantic lexical hybrid wiki_search mode enum",
        relevant={
            "iwiki-mcp/mcp-server.md#Tool surface:0": 3,
            "iwiki-mcp/retrieval.md#Hybrid search:0": 2,
        },
        intents={
            "api": ["iwiki-mcp/mcp-server.md#Tool surface:0"],
            "retrieval": ["iwiki-mcp/retrieval.md#Hybrid search:0"],
        },
    ),
    BenchmarkCase(
        case_id="chunking",
        domain="iwiki-mcp",
        query="Markdown chunking summary section chunk repeated heading",
        relevant={
            "iwiki-mcp/indexing.md#Markdown chunking:0": 3,
        },
        intents={
            "chunking": ["iwiki-mcp/indexing.md#Markdown chunking:0"],
        },
    ),
    BenchmarkCase(
        case_id="rerank-hydration",
        domain="iwiki-mcp",
        query="rerank candidates hydration stale provider top_n",
        relevant={
            "iwiki-mcp/retrieval.md#Hybrid search:0": 3,
            "iwiki-mcp/retrieval.md#Result shape:0": 2,
            "iwiki-mcp/mcp-server.md#Tool surface:0": 2,
        },
        intents={
            "rerank": ["iwiki-mcp/retrieval.md#Hybrid search:0"],
            "shape": ["iwiki-mcp/retrieval.md#Result shape:0"],
        },
    ),
]


OFFLINE_CASES = [
    BenchmarkCase(
        case_id="offline-symbol",
        domain="eval",
        query="refresh_token credentials",
        relevant={"eval/guide/auth.md#Rotation:0": 3},
        intents={"symbol": ["eval/guide/auth.md#Rotation:0"]},
        k=3,
    ),
    BenchmarkCase(
        case_id="offline-lost-before-top-k",
        domain="eval",
        query="needle late chunk",
        relevant={"eval/guide/long.md#Details:1": 3},
        intents={"chunk": ["eval/guide/long.md#Details:1"]},
        k=3,
    ),
]
```

- [ ] **Step 4: Add metrics implementation**

Create `eval/search_pipeline/metrics.py`:

```python
from __future__ import annotations

import math

from .fixtures import BenchmarkCase


def identity(result: dict) -> str:
    return (
        f"{result['domain']}/{result['file']}#"
        f"{result['heading']}:{result['chunk']}"
    )


def recall_at_k(ranking: list[str], case: BenchmarkCase, k: int) -> float:
    if k <= 0:
        return 0.0
    if not case.relevant:
        return 0.0
    selected = set(ranking[:k])
    return len(selected & set(case.relevant)) / len(case.relevant)


def mrr_at_k(ranking: list[str], case: BenchmarkCase, k: int) -> float:
    relevant = set(case.relevant)
    for rank, item in enumerate(ranking[:k], 1):
        if item in relevant:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(ranking: list[str], case: BenchmarkCase, k: int) -> float:
    def dcg(items: list[str]) -> float:
        return sum(
            (2 ** case.relevant.get(item, 0) - 1) / math.log2(rank + 2)
            for rank, item in enumerate(items[:k])
        )

    ideal = sorted(case.relevant.values(), reverse=True)[:k]
    ideal_score = sum(
        (2 ** grade - 1) / math.log2(rank + 2)
        for rank, grade in enumerate(ideal)
    )
    return dcg(ranking) / ideal_score if ideal_score else 0.0


def intent_coverage_at_k(ranking: list[str], case: BenchmarkCase, k: int) -> float:
    if not case.intents:
        return 0.0
    selected = set(ranking[:k])
    covered = sum(
        any(identity in selected for identity in identities)
        for identities in case.intents.values()
    )
    return covered / len(case.intents)


def source_mix(results: list[dict]) -> dict[str, dict[str, int]]:
    mix = {"hit": {}, "source": {}}
    for result in results:
        for field in mix:
            value = result.get(field, "unknown")
            mix[field][value] = mix[field].get(value, 0) + 1
    return {
        field: dict(sorted(counts.items()))
        for field, counts in mix.items()
    }


def latency_summary(stage_ms: dict[str, float]) -> dict[str, float]:
    rounded = {
        name: round(value, 3)
        for name, value in sorted(stage_ms.items())
    }
    rounded["total_ms"] = round(sum(stage_ms.values()), 3)
    return rounded
```

- [ ] **Step 5: Run metrics tests and commit**

Run:

```bash
uv run pytest -q tests/eval/test_search_pipeline_metrics.py
```

Expected: `4 passed`.

Commit:

```bash
git add eval/search_pipeline/__init__.py eval/search_pipeline/fixtures.py eval/search_pipeline/metrics.py tests/eval/test_search_pipeline_metrics.py
git commit -m "test(eval): add search pipeline metrics contract"
```

---

### Task 2: Env File And Sanitization Guards

**Files:**
- Create: `eval/search_pipeline/envfile.py`
- Test: `tests/eval/test_search_pipeline_envfile.py`

- [ ] **Step 1: Write failing env-file tests**

Create `tests/eval/test_search_pipeline_envfile.py`:

```python
from pathlib import Path

from eval.search_pipeline.envfile import (
    apply_env_file,
    load_env_file,
    safe_config_fingerprint,
    validate_env_file_path,
)
from iwiki_mcp.engine.config import Config


def test_load_env_file_accepts_comments_and_quoted_values(tmp_path):
    env = tmp_path / ".benchmark.env"
    env.write_text(
        "# local only\n"
        "IWIKI_LLM_KEY='secret key'\n"
        "IWIKI_RERANK_MODEL=rerank-model\n",
        encoding="utf-8",
    )

    assert load_env_file(env) == {
        "IWIKI_LLM_KEY": "secret key",
        "IWIKI_RERANK_MODEL": "rerank-model",
    }


def test_apply_env_file_restores_previous_values(tmp_path, monkeypatch):
    env = tmp_path / ".benchmark.env"
    env.write_text("IWIKI_LLM_KEY=file-secret\n", encoding="utf-8")
    monkeypatch.setenv("IWIKI_LLM_KEY", "shell-secret")

    with apply_env_file(env):
        assert __import__("os").environ["IWIKI_LLM_KEY"] == "file-secret"

    assert __import__("os").environ["IWIKI_LLM_KEY"] == "shell-secret"


def test_validate_env_file_rejects_output_tree(tmp_path):
    out = tmp_path / "evidence"
    out.mkdir()
    env = out / ".benchmark.env"
    env.write_text("IWIKI_LLM_KEY=secret\n", encoding="utf-8")

    result = validate_env_file_path(env, out)

    assert result["ok"] is False
    assert "inside output directory" in result["errors"][0]


def test_safe_config_fingerprint_redacts_key_and_base_url():
    cfg = Config(
        base_url="https://secret.example/v1",
        api_key="secret",
        embed_model="embed-model",
        dimensions=2,
        chunk_size=512,
        chunk_overlap=64,
        summary_max=400,
        top_k=8,
        score_threshold=0.2,
        graph_depth=2,
        ignore=None,
        rerank_model="rerank-model",
    )

    fingerprint = safe_config_fingerprint(cfg)

    assert fingerprint["embed_model"] == "embed-model"
    assert fingerprint["rerank_enabled"] is True
    assert "secret" not in repr(fingerprint)
    assert "base_url" not in fingerprint
    assert "api_key" not in fingerprint
```

- [ ] **Step 2: Run env-file tests and verify they fail**

Run:

```bash
uv run pytest -q tests/eval/test_search_pipeline_envfile.py
```

Expected: import failure for `eval.search_pipeline.envfile`.

- [ ] **Step 3: Implement env-file loader and fingerprinting**

Create `eval/search_pipeline/envfile.py`:

```python
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import shlex
import subprocess

from iwiki_mcp.engine.config import Config


def load_env_file(path: str | Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = shlex.split(value, posix=True)[0] if value.strip() else ""
    return values


@contextmanager
def apply_env_file(path: str | Path):
    values = load_env_file(path)
    previous = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update(values)
        yield values
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _is_tracked(path: Path) -> bool:
    try:
        subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def validate_env_file_path(path: str | Path, out_dir: str | Path) -> dict:
    env_path = Path(path).resolve()
    out_path = Path(out_dir).resolve()
    errors: list[str] = []
    warnings: list[str] = []
    if not env_path.is_file():
        errors.append("env file not found")
    try:
        env_path.relative_to(out_path)
        errors.append("env file is inside output directory")
    except ValueError:
        pass
    if _is_tracked(env_path):
        warnings.append("env file appears tracked by git")
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def safe_config_fingerprint(cfg: Config) -> dict:
    return {
        "embed_model": cfg.embed_model,
        "dimensions": cfg.dimensions,
        "chunk_size": cfg.chunk_size,
        "chunk_overlap": cfg.chunk_overlap,
        "summary_max": cfg.summary_max,
        "top_k": cfg.top_k,
        "score_threshold": cfg.score_threshold,
        "graph_depth": cfg.graph_depth,
        "seed_top_k": cfg.seed_top_k,
        "bfs_top_k": cfg.bfs_top_k,
        "seed_threshold": cfg.seed_threshold,
        "search_mode": cfg.search_mode,
        "rerank_enabled": bool(cfg.rerank_model),
        "rerank_model": cfg.rerank_model or None,
    }
```

- [ ] **Step 4: Run env-file tests and commit**

Run:

```bash
uv run pytest -q tests/eval/test_search_pipeline_envfile.py
```

Expected: `4 passed`.

Commit:

```bash
git add eval/search_pipeline/envfile.py tests/eval/test_search_pipeline_envfile.py
git commit -m "feat(eval): add benchmark env file guards"
```

---

### Task 3: Read-Only Stage Instrumentation

**Files:**
- Create: `eval/search_pipeline/instrumentation.py`
- Test: `tests/eval/test_search_pipeline_runner.py`

- [ ] **Step 1: Write failing offline trace tests**

Create `tests/eval/test_search_pipeline_runner.py` with this initial content:

```python
from pathlib import Path

from eval.search_pipeline.fixtures import BenchmarkCase
from eval.search_pipeline.instrumentation import trace_query
from iwiki_mcp import indexer, retrieval
from iwiki_mcp.engine.config import Config


def _cfg():
    return Config(
        base_url="http://offline.test/v1",
        api_key="offline",
        embed_model="offline",
        dimensions=2,
        chunk_size=3,
        chunk_overlap=0,
        summary_max=400,
        top_k=3,
        score_threshold=0.0,
        graph_depth=1,
        ignore=None,
        seed_top_k=2,
        bfs_top_k=10,
        seed_threshold=0.0,
    )


def _seed_domain(tmp_path, monkeypatch):
    base = tmp_path / "wiki"
    domain = base / "eval"
    domain.mkdir(parents=True)
    (domain / "guide" / "auth.md").parent.mkdir(parents=True)
    (domain / "guide" / "auth.md").write_text(
        "---\ndescription: credential lifecycle\n---\n"
        "# Auth\n\n## Rotation\nrefresh_token rotates credentials\n",
        encoding="utf-8",
    )
    (domain / "guide" / "long.md").write_text(
        "---\ndescription: late token page\n---\n"
        "# Long\n\n## Details\none two three needle five six\n",
        encoding="utf-8",
    )

    def embed(cfg, texts):
        out = []
        for text in texts:
            lowered = text.lower()
            out.append([
                float(lowered.count("refresh_token") + lowered.count("credentials")),
                float(lowered.count("needle")),
            ])
        return out

    monkeypatch.setattr(indexer, "embed_texts", embed)
    monkeypatch.setattr(retrieval, "embed_texts", embed)
    indexer.index_domain(_cfg(), str(base), "eval")
    return base


def test_trace_query_records_stage_counts_and_final_results(tmp_path, monkeypatch):
    base = _seed_domain(tmp_path, monkeypatch)
    case = BenchmarkCase(
        case_id="offline-symbol",
        domain="eval",
        query="refresh_token credentials",
        relevant={"eval/guide/auth.md#Rotation:0": 3},
        k=3,
    )

    trace = trace_query(_cfg(), str(base), case, mode="hybrid", rerank_enabled=False)

    assert trace["case_id"] == "offline-symbol"
    assert trace["mode"] == "hybrid"
    assert trace["metrics_input"]["ranking"][0] == "eval/guide/auth.md#Rotation:0"
    assert trace["stages"]["signals"]["counts"]["lexical_section"] >= 1
    assert trace["stages"]["fusion"]["candidate_count"] >= 1
    assert trace["stages"]["hydration"]["requested"] >= trace["stages"]["hydration"]["hydrated"]
    assert "total_ms" in trace["latency"]
```

- [ ] **Step 2: Run offline trace test and verify it fails**

Run:

```bash
uv run pytest -q tests/eval/test_search_pipeline_runner.py::test_trace_query_records_stage_counts_and_final_results
```

Expected: import failure for `trace_query`.

- [ ] **Step 3: Implement `trace_query`**

Create `eval/search_pipeline/instrumentation.py`:

```python
from __future__ import annotations

from time import perf_counter

import numpy as np

from iwiki_mcp import retrieval
from iwiki_mcp.engine import fusion, rerank
from iwiki_mcp.engine.config import Config

from .fixtures import BenchmarkCase
from .metrics import identity, latency_summary, source_mix


def _public_from_fused(fused: list[dict]) -> list[dict]:
    public = []
    for candidate in fused:
        item = dict(candidate)
        signal_names = set(item.pop("signals"))
        origins = set(item.get("seed_origins", []))
        semantic = bool(signal_names & {"semantic_page", "semantic_chunk"})
        lexical = bool(signal_names & {"lexical_page", "lexical_section"})
        if "graph_page" in signal_names:
            semantic = semantic or "semantic" in origins
            lexical = lexical or "lexical" in origins
        item["hit"] = (
            "both" if semantic and lexical else "semantic" if semantic else "lexical"
        )
        item.pop("ordinal", None)
        item.pop("seed_origins", None)
        public.append({
            key: item[key]
            for key in ("domain", "file", "heading", "chunk", "score", "hit", "source")
        })
    return public


def _timed(stage_ms: dict[str, float], name: str, fn):
    start = perf_counter()
    value = fn()
    stage_ms[name] = (perf_counter() - start) * 1000
    return value


def _ranked_with_rerank(cfg: Config, query: str, candidates: list[dict],
                        hydrated: list[dict], k: int) -> tuple[list[dict], dict]:
    ranked, metadata = rerank.rerank_candidates(cfg, query, hydrated, top_n=k)
    if not metadata["applied"]:
        return candidates[:k], metadata
    scored_count = metadata.pop("_scored_count", len(ranked))
    scored = ranked[:scored_count]
    scored_keys = {
        (item["domain"], item["file"], item["heading"], item["chunk"])
        for item in scored
    }
    unscored = [
        item for item in candidates
        if (item["domain"], item["file"], item["heading"], item["chunk"])
        not in scored_keys
    ]
    return (scored + unscored)[:k], metadata


def trace_query(cfg: Config, base: str, case: BenchmarkCase, mode: str,
                rerank_enabled: bool) -> dict:
    stage_ms: dict[str, float] = {}
    page_cache = {}
    limit = max(case.k, retrieval.CANDIDATE_LIMIT)

    query_vec = None
    if mode in ("semantic", "hybrid"):
        query_vec = _timed(
            stage_ms,
            "embedding_ms",
            lambda: list(
                np.asarray(retrieval.embed_texts(cfg, [case.query])[0], dtype=np.float32)
            ),
        )

    def build_signals():
        return retrieval._domain_signals(
            cfg,
            base,
            case.domain,
            case.query,
            query_vec,
            mode,
            limit,
            cfg.score_threshold,
            None,
            None,
            page_cache,
        )

    signals = _timed(stage_ms, "signals_ms", build_signals)
    for hits in signals.values():
        hits.sort(key=lambda hit: (
            hit["rank_key"], hit["domain"], hit["file"], hit["ordinal"], hit["chunk"]
        ))
        for hit in hits:
            hit.pop("rank_key")

    fused = _timed(stage_ms, "fusion_ms", lambda: fusion.fuse_ranked(signals, limit))
    candidates = _public_from_fused(fused)
    hydrated = _timed(
        stage_ms,
        "hydration_ms",
        lambda: retrieval.hydrate_candidates(cfg, base, candidates, page_cache=page_cache),
    )

    metadata = {"applied": False, "warning": "rerank disabled"}
    final = candidates[:case.k]
    if rerank_enabled and cfg.rerank_model:
        final, metadata = _timed(
            stage_ms,
            "rerank_ms",
            lambda: _ranked_with_rerank(cfg, case.query, candidates, hydrated, case.k),
        )
    else:
        stage_ms["rerank_ms"] = 0.0
        if rerank_enabled:
            metadata = {"applied": False, "warning": "rerank unavailable"}

    ranking = [identity(result) for result in final]
    candidate_identities = [identity(result) for result in candidates]
    hydrated_identities = [identity(result) for result in hydrated]
    return {
        "case_id": case.case_id,
        "domain": case.domain,
        "query": case.query,
        "mode": mode,
        "k": case.k,
        "latency": latency_summary(stage_ms),
        "stages": {
            "signals": {
                "counts": {name: len(hits) for name, hits in sorted(signals.items())},
            },
            "fusion": {
                "candidate_count": len(candidates),
                "candidate_identities": candidate_identities,
                "source_mix": source_mix(candidates),
            },
            "hydration": {
                "requested": len(candidates),
                "hydrated": len(hydrated),
                "dropped": len(candidates) - len(hydrated),
                "hydrated_identities": hydrated_identities,
            },
            "rerank": metadata,
        },
        "results": final,
        "metrics_input": {
            "ranking": ranking,
            "relevant": case.relevant,
            "intents": case.intents,
        },
    }
```

- [ ] **Step 4: Run trace test**

Run:

```bash
uv run pytest -q tests/eval/test_search_pipeline_runner.py::test_trace_query_records_stage_counts_and_final_results
```

Expected after implementation: `1 passed`.

- [ ] **Step 5: Commit instrumentation**

Commit:

```bash
git add eval/search_pipeline/instrumentation.py tests/eval/test_search_pipeline_runner.py
git commit -m "feat(eval): trace search pipeline stages"
```

---

### Task 4: Analyzer And Ranked Backlog

**Files:**
- Create: `eval/search_pipeline/analyzer.py`
- Test: `tests/eval/test_search_pipeline_analyzer.py`

- [ ] **Step 1: Write failing analyzer tests**

Create `tests/eval/test_search_pipeline_analyzer.py`:

```python
from eval.search_pipeline.analyzer import analyze_trace, ranked_backlog
from eval.search_pipeline.fixtures import BenchmarkCase


def _case():
    return BenchmarkCase(
        case_id="case",
        domain="iwiki-mcp",
        query="query",
        relevant={"iwiki-mcp/retrieval.md#Hybrid search:0": 3},
        k=2,
    )


def test_analyze_trace_marks_missing_candidate_pool():
    trace = {
        "metrics_input": {"ranking": [], "relevant": _case().relevant},
        "stages": {
            "fusion": {"candidate_identities": []},
            "hydration": {"hydrated_identities": []},
            "rerank": {"applied": False},
        },
    }

    finding = analyze_trace(_case(), trace)[0]

    assert finding["class"] == "missing_from_candidate_pool"
    assert finding["severity"] == "high"


def test_analyze_trace_marks_lost_after_top_k():
    trace = {
        "metrics_input": {
            "ranking": ["iwiki-mcp/noise.md#Noise:0"],
            "relevant": _case().relevant,
        },
        "stages": {
            "fusion": {
                "candidate_identities": [
                    "iwiki-mcp/noise.md#Noise:0",
                    "iwiki-mcp/retrieval.md#Hybrid search:0",
                ],
            },
            "hydration": {"hydrated_identities": ["iwiki-mcp/retrieval.md#Hybrid search:0"]},
            "rerank": {"applied": False},
        },
    }

    finding = analyze_trace(_case(), trace)[0]

    assert finding["class"] == "lost_after_fusion_topk"
    assert finding["evidence"]["candidate_rank"] == 2


def test_ranked_backlog_groups_findings():
    findings = [
        {"class": "missing_from_candidate_pool", "severity": "high"},
        {"class": "missing_from_candidate_pool", "severity": "high"},
        {"class": "hydration_drop", "severity": "medium"},
    ]

    backlog = ranked_backlog(findings)

    assert backlog[0]["class"] == "missing_from_candidate_pool"
    assert backlog[0]["count"] == 2
```

- [ ] **Step 2: Run analyzer tests and verify they fail**

Run:

```bash
uv run pytest -q tests/eval/test_search_pipeline_analyzer.py
```

Expected: import failure for `analyzer`.

- [ ] **Step 3: Implement analyzer**

Create `eval/search_pipeline/analyzer.py`:

```python
from __future__ import annotations

from collections import Counter

from .fixtures import BenchmarkCase


_SEVERITY_WEIGHT = {"high": 3, "medium": 2, "low": 1}


def analyze_trace(case: BenchmarkCase, trace: dict) -> list[dict]:
    findings = []
    relevant = set(case.relevant)
    ranking = trace["metrics_input"]["ranking"]
    candidates = trace["stages"]["fusion"].get("candidate_identities", [])
    hydrated = set(trace["stages"]["hydration"].get("hydrated_identities", []))
    selected = set(ranking[:case.k])

    missing = relevant - set(candidates)
    for identity in sorted(missing):
        findings.append({
            "case_id": case.case_id,
            "class": "missing_from_candidate_pool",
            "severity": "high",
            "identity": identity,
            "evidence": {"candidate_count": len(candidates)},
        })

    for identity in sorted((relevant & set(candidates)) - selected):
        findings.append({
            "case_id": case.case_id,
            "class": "lost_after_fusion_topk",
            "severity": "medium",
            "identity": identity,
            "evidence": {"candidate_rank": candidates.index(identity) + 1},
        })

    for identity in sorted((relevant & set(candidates)) - hydrated):
        if trace["stages"]["hydration"].get("requested", 0):
            findings.append({
                "case_id": case.case_id,
                "class": "hydration_drop",
                "severity": "medium",
                "identity": identity,
                "evidence": trace["stages"]["hydration"],
            })

    if trace["stages"].get("rerank", {}).get("applied") and relevant & set(candidates):
        candidate_best = min(candidates.index(item) for item in relevant & set(candidates))
        ranked_best = min(
            [ranking.index(item) for item in relevant & set(ranking)]
            or [len(ranking)]
        )
        if ranked_best > candidate_best:
            findings.append({
                "case_id": case.case_id,
                "class": "rerank_worsened_order",
                "severity": "medium",
                "identity": candidates[candidate_best],
                "evidence": {
                    "candidate_rank": candidate_best + 1,
                    "reranked_rank": ranked_best + 1,
                },
            })

    if not findings and not (selected & relevant):
        findings.append({
            "case_id": case.case_id,
            "class": "unknown_quality_loss",
            "severity": "low",
            "identity": None,
            "evidence": {"ranking": ranking},
        })
    return findings


def ranked_backlog(findings: list[dict]) -> list[dict]:
    counts = Counter(finding["class"] for finding in findings)
    severity = {}
    for finding in findings:
        current = severity.get(finding["class"], "low")
        if _SEVERITY_WEIGHT[finding["severity"]] > _SEVERITY_WEIGHT[current]:
            severity[finding["class"]] = finding["severity"]
    return [
        {"class": name, "count": count, "severity": severity.get(name, "low")}
        for name, count in sorted(
            counts.items(),
            key=lambda item: (-_SEVERITY_WEIGHT[severity.get(item[0], "low")], -item[1], item[0]),
        )
    ]
```

- [ ] **Step 4: Run analyzer tests and commit**

Run:

```bash
uv run pytest -q tests/eval/test_search_pipeline_analyzer.py
```

Expected: `3 passed`.

Commit:

```bash
git add eval/search_pipeline/analyzer.py tests/eval/test_search_pipeline_analyzer.py
git commit -m "feat(eval): classify search pipeline bottlenecks"
```

---

### Task 5: Runner Aggregation And Reports

**Files:**
- Create: `eval/search_pipeline/report.py`
- Create: `eval/search_pipeline/runner.py`
- Test: `tests/eval/test_search_pipeline_report.py`
- Modify: `tests/eval/test_search_pipeline_runner.py`

- [ ] **Step 1: Add failing runner/report tests**

Append to `tests/eval/test_search_pipeline_runner.py`:

```python
from eval.search_pipeline.runner import summarize_trace, run_offline_traces


def test_summarize_trace_computes_quality_metrics(tmp_path, monkeypatch):
    base = _seed_domain(tmp_path, monkeypatch)
    case = BenchmarkCase(
        case_id="offline-symbol",
        domain="eval",
        query="refresh_token credentials",
        relevant={"eval/guide/auth.md#Rotation:0": 3},
        intents={"symbol": ["eval/guide/auth.md#Rotation:0"]},
        k=3,
    )
    trace = trace_query(_cfg(), str(base), case, mode="hybrid", rerank_enabled=False)

    summary = summarize_trace(case, trace)

    assert summary["recall_at_k"] == 1.0
    assert summary["mrr_at_k"] == 1.0
    assert summary["intent_coverage_at_k"] == 1.0


def test_run_offline_traces_returns_evidence(tmp_path, monkeypatch):
    base = _seed_domain(tmp_path, monkeypatch)
    case = BenchmarkCase(
        case_id="offline-symbol",
        domain="eval",
        query="refresh_token credentials",
        relevant={"eval/guide/auth.md#Rotation:0": 3},
        k=3,
    )

    evidence = run_offline_traces(_cfg(), str(base), [case], modes=["hybrid"])

    assert evidence["kind"] == "offline"
    assert evidence["cases"][0]["case_id"] == "offline-symbol"
    assert "backlog" in evidence
```

Create `tests/eval/test_search_pipeline_report.py`:

```python
import json

from eval.search_pipeline.report import render_html, render_markdown, write_reports


def _evidence():
    return {
        "kind": "live",
        "timestamp": "2026-07-28T00:00:00+00:00",
        "config": {"embed_model": "embed", "rerank_enabled": False},
        "summary": {"mean_recall_at_k": 0.5, "mean_mrr_at_k": 0.25},
        "backlog": [{"class": "missing_from_candidate_pool", "count": 2, "severity": "high"}],
        "cases": [],
    }


def test_render_markdown_includes_metrics_and_backlog():
    text = render_markdown(_evidence())

    assert "# Search Pipeline Benchmark" in text
    assert "mean_recall_at_k" in text
    assert "missing_from_candidate_pool" in text


def test_render_html_is_standalone_and_escaped():
    html = render_html({**_evidence(), "backlog": [{"class": "<x>", "count": 1, "severity": "high"}]})

    assert "<html" in html
    assert "&lt;x&gt;" in html


def test_write_reports_writes_json_markdown_and_html(tmp_path):
    written = write_reports(_evidence(), tmp_path)

    assert json.loads(written["json"].read_text(encoding="utf-8"))["kind"] == "live"
    assert written["markdown"].read_text(encoding="utf-8").startswith("# Search Pipeline Benchmark")
    assert "<html" in written["html"].read_text(encoding="utf-8")
```

- [ ] **Step 2: Run runner/report tests and verify they fail**

Run:

```bash
uv run pytest -q tests/eval/test_search_pipeline_runner.py tests/eval/test_search_pipeline_report.py
```

Expected: import failures for `runner` and `report`.

- [ ] **Step 3: Implement runner aggregation**

Create `eval/search_pipeline/runner.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone
from statistics import mean

from iwiki_mcp.engine.config import Config

from .analyzer import analyze_trace, ranked_backlog
from .fixtures import BenchmarkCase
from .instrumentation import trace_query
from .metrics import (
    intent_coverage_at_k,
    mrr_at_k,
    ndcg_at_k,
    recall_at_k,
)


def summarize_trace(case: BenchmarkCase, trace: dict) -> dict:
    ranking = trace["metrics_input"]["ranking"]
    return {
        "case_id": case.case_id,
        "mode": trace["mode"],
        "recall_at_k": recall_at_k(ranking, case, case.k),
        "mrr_at_k": mrr_at_k(ranking, case, case.k),
        "ndcg_at_k": ndcg_at_k(ranking, case, case.k),
        "intent_coverage_at_k": intent_coverage_at_k(ranking, case, case.k),
        "latency": trace["latency"],
    }


def _rollup(summaries: list[dict]) -> dict:
    if not summaries:
        return {
            "mean_recall_at_k": 0.0,
            "mean_mrr_at_k": 0.0,
            "mean_ndcg_at_k": 0.0,
            "mean_intent_coverage_at_k": 0.0,
        }
    return {
        "mean_recall_at_k": mean(item["recall_at_k"] for item in summaries),
        "mean_mrr_at_k": mean(item["mrr_at_k"] for item in summaries),
        "mean_ndcg_at_k": mean(item["ndcg_at_k"] for item in summaries),
        "mean_intent_coverage_at_k": mean(
            item["intent_coverage_at_k"] for item in summaries
        ),
    }


def run_offline_traces(cfg: Config, base: str, cases: list[BenchmarkCase],
                       modes: list[str]) -> dict:
    traces = []
    summaries = []
    findings = []
    for case in cases:
        for mode in modes:
            trace = trace_query(cfg, base, case, mode, rerank_enabled=False)
            traces.append(trace)
            summaries.append(summarize_trace(case, trace))
            findings.extend(analyze_trace(case, trace))
    return {
        "kind": "offline",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": _rollup(summaries),
        "backlog": ranked_backlog(findings),
        "cases": traces,
    }
```

- [ ] **Step 4: Implement reports**

Create `eval/search_pipeline/report.py`:

```python
from __future__ import annotations

import html
import json
from pathlib import Path


def render_markdown(evidence: dict) -> str:
    lines = [
        "# Search Pipeline Benchmark",
        "",
        f"- kind: `{evidence.get('kind')}`",
        f"- timestamp: `{evidence.get('timestamp')}`",
        "",
        "## Summary",
        "",
    ]
    for key, value in sorted(evidence.get("summary", {}).items()):
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Ranked Backlog", ""])
    backlog = evidence.get("backlog", [])
    if not backlog:
        lines.append("- No bottleneck findings.")
    for item in backlog:
        lines.append(
            f"- `{item['severity']}` `{item['class']}`: {item['count']} case(s)"
        )
    lines.extend(["", "## Cases", ""])
    for case in evidence.get("cases", []):
        lines.append(f"- `{case['case_id']}` mode `{case['mode']}`")
    return "\n".join(lines) + "\n"


def render_html(evidence: dict) -> str:
    markdown = html.escape(render_markdown(evidence))
    return (
        "<!doctype html>\n"
        "<html><head><meta charset=\"utf-8\"><title>Search Pipeline Benchmark</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;"
        "line-height:1.45}pre{white-space:pre-wrap;background:#f6f8fa;padding:1rem}</style>"
        "</head><body><pre>"
        f"{markdown}"
        "</pre></body></html>\n"
    )


def write_reports(evidence: dict, out_dir: str | Path) -> dict[str, Path]:
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": root / "search-pipeline-benchmark.json",
        "markdown": root / "search-pipeline-benchmark.md",
        "html": root / "search-pipeline-benchmark.html",
    }
    paths["json"].write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    paths["markdown"].write_text(render_markdown(evidence), encoding="utf-8")
    paths["html"].write_text(render_html(evidence), encoding="utf-8")
    return paths
```

- [ ] **Step 5: Run runner/report tests and commit**

Run:

```bash
uv run pytest -q tests/eval/test_search_pipeline_runner.py tests/eval/test_search_pipeline_report.py
```

Expected: all tests in those files pass.

Commit:

```bash
git add eval/search_pipeline/runner.py eval/search_pipeline/report.py tests/eval/test_search_pipeline_runner.py tests/eval/test_search_pipeline_report.py
git commit -m "feat(eval): aggregate benchmark evidence and reports"
```

---

### Task 6: CLI And Live-First Execution

**Files:**
- Create: `eval/search_pipeline/__main__.py`
- Modify: `eval/search_pipeline/runner.py`
- Modify: `tests/eval/test_search_pipeline_runner.py`

- [ ] **Step 1: Add failing CLI tests**

Append to `tests/eval/test_search_pipeline_runner.py`:

```python
import subprocess
import sys


def test_cli_help_exits_successfully():
    result = subprocess.run(
        [sys.executable, "-m", "eval.search_pipeline", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--domain" in result.stdout
    assert "--env-file" in result.stdout


def test_cli_requires_live_config_without_offline_flag(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "eval.search_pipeline",
            "--domain",
            "iwiki-mcp",
            "--out",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={},
    )

    assert result.returncode == 2
    assert "IWIKI_LLM_BASE_URL" in result.stderr
    assert "secret" not in result.stderr.lower()
```

- [ ] **Step 2: Run CLI tests and verify they fail**

Run:

```bash
uv run pytest -q tests/eval/test_search_pipeline_runner.py::test_cli_help_exits_successfully tests/eval/test_search_pipeline_runner.py::test_cli_requires_live_config_without_offline_flag
```

Expected: module execution failure for missing `__main__.py`.

- [ ] **Step 3: Add live runner function**

Append to `eval/search_pipeline/runner.py`:

```python
from iwiki_mcp import base as wiki_base
from .envfile import safe_config_fingerprint


def run_live_traces(cfg: Config, domain: str, modes: list[str],
                    cases: list[BenchmarkCase]) -> dict:
    binding = wiki_base.resolve_binding()
    traces = []
    summaries = []
    findings = []
    for case in cases:
        if case.domain != domain:
            continue
        for mode in modes:
            trace = trace_query(
                cfg,
                binding.base,
                case,
                mode,
                rerank_enabled=bool(cfg.rerank_model),
            )
            traces.append(trace)
            summaries.append(summarize_trace(case, trace))
            findings.extend(analyze_trace(case, trace))
    return {
        "kind": "live",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": safe_config_fingerprint(cfg),
        "summary": _rollup(summaries),
        "backlog": ranked_backlog(findings),
        "cases": traces,
    }
```

- [ ] **Step 4: Add CLI entry point**

Create `eval/search_pipeline/__main__.py`:

```python
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from iwiki_mcp.engine.config import Config, ConfigError

from .envfile import apply_env_file, validate_env_file_path
from .fixtures import DEFAULT_LIVE_CASES
from .report import write_reports
from .runner import run_live_traces


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run iwiki search pipeline benchmark")
    parser.add_argument("--domain", default="iwiki-mcp")
    parser.add_argument("--out", required=True)
    parser.add_argument("--env-file")
    parser.add_argument("--modes", default="hybrid,lexical,semantic")
    return parser


def _run(args) -> int:
    out_dir = Path(args.out)
    modes = [mode.strip() for mode in args.modes.split(",") if mode.strip()]
    context = apply_env_file(args.env_file) if args.env_file else None
    if args.env_file:
        validation = validate_env_file_path(args.env_file, out_dir)
        for warning in validation["warnings"]:
            print(f"WARNING: {warning}", file=sys.stderr)
        if not validation["ok"]:
            print("; ".join(validation["errors"]), file=sys.stderr)
            return 2
    try:
        if context is None:
            cfg = Config.load()
            evidence = run_live_traces(cfg, args.domain, modes, DEFAULT_LIVE_CASES)
        else:
            with context:
                cfg = Config.load()
                evidence = run_live_traces(cfg, args.domain, modes, DEFAULT_LIVE_CASES)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    paths = write_reports(evidence, out_dir)
    print(f"wrote {paths['json']}")
    print(f"wrote {paths['markdown']}")
    print(f"wrote {paths['html']}")
    return 0


def main() -> int:
    return _run(_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run CLI tests and commit**

Run:

```bash
uv run pytest -q tests/eval/test_search_pipeline_runner.py
```

Expected: all runner/CLI tests pass.

Commit:

```bash
git add eval/search_pipeline/__main__.py eval/search_pipeline/runner.py tests/eval/test_search_pipeline_runner.py
git commit -m "feat(eval): add live search pipeline benchmark CLI"
```

---

### Task 7: Version, Focused Verification, And Docs State

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`
- Modify: `docs/TODO.md`

- [ ] **Step 1: Bump package version**

Patch all package-version locations from the current version to the next patch version.
After the checked plan commit, the implementation target version is `0.7.8`:

```python
# src/iwiki_mcp/__init__.py
__version__ = "0.7.8"
```

```toml
# pyproject.toml
version = "0.7.8"
```

```toml
# uv.lock package block
name = "iwiki-mcp"
version = "0.7.8"
```

- [ ] **Step 2: Update task log**

Update `docs/TODO.md` row:

```markdown
| search-pipeline-benchmark | in-progress | ✓ | ✓ | ✓ | – | 2026-07-28 |  | plan OK — implementation complete pending result gate |
```

- [ ] **Step 3: Run focused verification**

Run:

```bash
uv run pytest -q tests/eval/test_search_pipeline_metrics.py tests/eval/test_search_pipeline_envfile.py tests/eval/test_search_pipeline_analyzer.py tests/eval/test_search_pipeline_report.py tests/eval/test_search_pipeline_runner.py tests/test_package.py
```

Expected: all selected tests pass.

- [ ] **Step 4: Run existing focused search verification**

Run:

```bash
uv run pytest -q tests/eval tests/engine/test_rerank.py tests/test_server_search.py
```

Expected: all selected tests pass.

- [ ] **Step 5: Run CLI help**

Run:

```bash
uv run python -m eval.search_pipeline --help
```

Expected: exit 0 and output includes `--domain`, `--out`, and `--env-file`.

- [ ] **Step 6: Run full test suite and record current status**

Run:

```bash
uv run pytest -q
```

Expected: either full pass or the known unrelated repository failures documented before this plan: `tests/test_resources.py::test_repository_server_report_lists_current_search_modes_and_tool_surface` and `tests/test_sync_parallel.py::test_sync_aborts_true_rebase_conflict_without_retry_or_commit_loss` if still present. Any new failure in `eval/search_pipeline` or search/rerank tests blocks completion.

- [ ] **Step 7: Check wiki lint**

Run through MCP:

```text
wiki_lint(domain="iwiki-mcp")
```

Expected: no broken refs, no stale pages, no missing source. Pre-existing orphan, long-lead advisory, or tag drift may remain and must be reported as pre-existing.

- [ ] **Step 8: Commit implementation**

Commit:

```bash
git add eval/search_pipeline tests/eval pyproject.toml src/iwiki_mcp/__init__.py uv.lock docs/TODO.md
git commit -m "feat(eval): add live search pipeline benchmark"
```

---

## Self-Review

- Spec coverage: Tasks 1-6 cover live-first benchmark, per-stage metrics, bottleneck evidence, ranked backlog, comparison modes, credential file flow, read-only safety, and search-context-only scope. Task 7 covers versioning and verification.
- Placeholder scan: no task contains unspecified implementation placeholders; every new file has concrete content.
- Type consistency: `BenchmarkCase`, `trace_query`, `summarize_trace`, `run_live_traces`, `write_reports`, and CLI flags use consistent names across tasks.
