from eval.hierarchical import fixtures, harness


def test_baseline_metrics_are_fixed():
    metrics = harness.run_baseline_eval(
        fixtures.VAULT, fixtures.QUERIES, fixtures.embed, top_k=3
    )

    assert metrics == {"recall_at_k": 0.8333333333333334, "mrr_at_k": 0.75}


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
