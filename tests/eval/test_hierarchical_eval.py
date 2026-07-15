from eval.hierarchical import fixtures, harness


def test_baseline_metrics_are_fixed():
    metrics = harness.run_baseline_eval(
        fixtures.VAULT, fixtures.QUERIES, fixtures.embed, top_k=3
    )

    assert metrics == {"recall_at_k": 0.8333333333333334, "mrr_at_k": 0.75}
