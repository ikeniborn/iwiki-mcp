"""Live benchmark runner with aggregate-only evidence output."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Callable, Sequence

from iwiki_mcp.engine import classify, system1
from iwiki_mcp.engine.config import Config
from iwiki_mcp.engine.frontmatter import CLASSIFIABLE_TYPES

from .metrics import compare, evaluate_predictions


class BenchmarkError(RuntimeError):
    """Raised when evidence cannot support a comparison."""


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    label: str
    body: str


def load_corpus(path: Path) -> list[BenchmarkCase]:
    cases = []
    seen = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            case = BenchmarkCase(
                case_id=item["id"],
                label=item["label"],
                body=item["body"],
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise BenchmarkError(f"invalid corpus record at line {line_number}") from exc
        if (
            not isinstance(case.case_id, str)
            or not case.case_id
            or not isinstance(case.label, str)
            or case.label not in CLASSIFIABLE_TYPES
            or not isinstance(case.body, str)
            or not case.body
        ):
            raise BenchmarkError(f"invalid corpus record at line {line_number}")
        if case.case_id in seen:
            raise BenchmarkError(f"duplicate corpus id at line {line_number}")
        seen.add(case.case_id)
        cases.append(case)
    if not cases:
        raise BenchmarkError("corpus must contain at least one case")
    return cases


def _baseline_decider(cfg: Config, body: str) -> tuple[str, float]:
    started = time.perf_counter()
    result = classify.classify_page(cfg, body, [])
    latency_ms = (time.perf_counter() - started) * 1000
    if result["warning"]:
        raise BenchmarkError("baseline classifier unavailable")
    return result["type"], latency_ms


def run_benchmark(
    cases: Sequence[BenchmarkCase],
    cfg: Config,
    *,
    baseline_decider: Callable[[Config, str], tuple[str, float]] = _baseline_decider,
    system1_decider: Callable[
        [Config, str], system1.PageTypeDecision | None
    ] = system1.classify_page_type,
) -> dict[str, object]:
    """Run baseline first, freeze its p95, then score System One."""
    if not cases:
        raise BenchmarkError("corpus must contain at least one case")
    labels = [case.label for case in cases]
    baseline_predictions = []
    baseline_latencies = []
    for case in cases:
        prediction, latency_ms = baseline_decider(cfg, case.body)
        baseline_predictions.append(prediction)
        baseline_latencies.append(latency_ms)

    baseline = evaluate_predictions(labels, baseline_predictions, baseline_latencies)
    frozen_p95 = baseline["p95_latency_ms"]

    system1_predictions = []
    system1_probabilities = []
    system1_latencies = []
    models: Counter[str] = Counter()
    for case in cases:
        decision = system1_decider(cfg, case.body)
        if decision is None or decision.status != "ok" or decision.page_type is None:
            raise BenchmarkError("System One decision unavailable or invalid")
        system1_predictions.append(decision.page_type)
        system1_probabilities.append(decision.probabilities)
        system1_latencies.append(decision.latency_ms)
        models[decision.model or "unspecified"] += 1

    report = compare(
        labels,
        baseline_predictions,
        baseline_latencies,
        system1_predictions,
        system1_probabilities,
        system1_latencies,
    )
    report["frozen_system1_p95_threshold_ms"] = frozen_p95
    report["corpus"] = {
        "count": len(cases),
        "labels": dict(sorted(Counter(labels).items())),
    }
    report["system1_models"] = dict(sorted(models.items()))
    return report


def write_report(report: dict[str, object], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
