import pytest

from eval.reranker.experiment import intent_recall_at_k, ndcg_at_k, rrf


def test_ndcg_rewards_better_graded_order():
    grades = {"best": 3, "useful": 2, "weak": 1, "noise": 0}

    ideal = ndcg_at_k(["best", "useful", "weak", "noise"], grades, 4)
    degraded = ndcg_at_k(["noise", "weak", "useful", "best"], grades, 4)

    assert ideal == 1.0
    assert degraded == pytest.approx(0.547831)


def test_intent_recall_counts_distinct_covered_intents():
    intents = {
        "cleanup": {"purge": 3},
        "monitoring": {"prometheus": 3},
        "gateway": {"edge": 3},
    }

    assert intent_recall_at_k(["purge", "edge", "noise"], intents, 3) == 2 / 3


def test_rrf_fuses_subquery_rankings_with_stable_ties():
    rankings = [
        ["cleanup", "rollback", "noise-a"],
        ["prometheus", "rollback", "noise-b"],
    ]

    assert rrf(rankings) == [
        "rollback", "cleanup", "prometheus", "noise-a", "noise-b"
    ]
