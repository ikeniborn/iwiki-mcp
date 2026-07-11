from eval.hierarchical import harness, fixtures


def test_eval_meets_floor():
    m = harness.run_eval(fixtures.VAULT, fixtures.QUERIES, fixtures.embed)
    assert m["article_recall"] >= 0.8
    assert m["section_recall"] >= 0.6
    assert m["mrr"] > 0.0
