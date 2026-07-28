from __future__ import annotations

import math

from .fixtures import BenchmarkCase


def identity(result: dict) -> str:
    return (
        f"{result['domain']}/{result['file']}#"
        f"{result['heading']}:{result['chunk']}"
    )


def recall_at_k(ranking: list[str], case: BenchmarkCase, k: int) -> float:
    selected = set(ranking[:k])
    return 1.0 if selected & set(case.relevant) else 0.0


def mrr_at_k(ranking: list[str], case: BenchmarkCase, k: int) -> float:
    relevant = set(case.relevant)
    for rank, item in enumerate(ranking[:k], 1):
        if item in relevant:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(ranking: list[str], case: BenchmarkCase, k: int) -> float:
    def gain(grade: int) -> float:
        return 2 ** grade - 1

    def dcg(items: list[str]) -> float:
        return sum(
            gain(case.relevant.get(item, 0)) / math.log2(rank + 2)
            for rank, item in enumerate(items[:k])
        )

    ideal = sorted(case.relevant.values(), reverse=True)[:k]
    ideal_score = sum(
        gain(grade) / math.log2(rank + 2)
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
