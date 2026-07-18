"""Pure metrics used by the live reranker experiment."""
from __future__ import annotations

import math


def ndcg_at_k(ranking, grades, k):
    def dcg(items):
        return sum(
            (2 ** grades.get(item, 0) - 1) / math.log2(rank + 2)
            for rank, item in enumerate(items[:k])
        )

    actual = dcg(ranking)
    ideal_grades = sorted(grades.values(), reverse=True)[:k]
    ideal = sum(
        (2 ** grade - 1) / math.log2(rank + 2)
        for rank, grade in enumerate(ideal_grades)
    )
    return actual / ideal if ideal else 0.0


def intent_recall_at_k(ranking, intents, k):
    if not intents:
        return 0.0
    selected = set(ranking[:k])
    covered = sum(
        any(item in selected and grade > 0 for item, grade in grades.items())
        for grades in intents.values()
    )
    return covered / len(intents)


def rrf(rankings, constant=60):
    scores = {}
    discovery = {}
    for ranking in rankings:
        seen = set()
        for rank, item in enumerate(ranking, 1):
            if item in seen:
                continue
            seen.add(item)
            discovery.setdefault(item, len(discovery))
            scores[item] = scores.get(item, 0.0) + 1 / (constant + rank)
    return sorted(scores, key=lambda item: (-scores[item], discovery[item]))
