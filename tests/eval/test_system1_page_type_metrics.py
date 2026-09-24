import pytest

from eval.system1_page_type.metrics import compare, evaluate


def test_evaluate_reports_quality_latency_and_calibration():
    labels = ["guide", "api", "concept", "reference"]
    predictions = ["guide", "concept", "concept", "reference"]
    probabilities = [
        {
            "architecture": 0.02,
            "api": 0.03,
            "guide": 0.85,
            "reference": 0.03,
            "runbook": 0.02,
            "concept": 0.05,
        },
        {
            "architecture": 0.05,
            "api": 0.25,
            "guide": 0.05,
            "reference": 0.05,
            "runbook": 0.05,
            "concept": 0.55,
        },
        {
            "architecture": 0.02,
            "api": 0.02,
            "guide": 0.02,
            "reference": 0.02,
            "runbook": 0.02,
            "concept": 0.90,
        },
        {
            "architecture": 0.02,
            "api": 0.02,
            "guide": 0.02,
            "reference": 0.88,
            "runbook": 0.02,
            "concept": 0.04,
        },
    ]

    result = evaluate(labels, predictions, probabilities, [10, 20, 30, 100])

    assert result["count"] == 4
    assert result["accuracy"] == pytest.approx(0.75)
    assert result["macro_f1"] == pytest.approx((0 + 0 + 1 + 1 + 0 + 2 / 3) / 6)
    assert result["p50_latency_ms"] == 20
    assert result["p95_latency_ms"] == 100
    assert result["brier_score"] == pytest.approx(0.23305)
    assert result["ece"] == pytest.approx(0.23)


def test_evaluate_rejects_incomplete_or_misaligned_evidence():
    with pytest.raises(ValueError, match="same non-zero length"):
        evaluate(["guide"], [], [], [])

    with pytest.raises(ValueError, match="probability keys"):
        evaluate(["guide"], ["guide"], [{"guide": 1.0}], [1])


def test_compare_freezes_baseline_p95_before_scoring_system1():
    labels = ["guide", "api"]
    baseline_predictions = ["guide", "concept"]
    system1_predictions = ["guide", "api"]
    probabilities = [
        {
            "architecture": 0.02,
            "api": 0.02,
            "guide": 0.90,
            "reference": 0.02,
            "runbook": 0.02,
            "concept": 0.02,
        },
        {
            "architecture": 0.02,
            "api": 0.90,
            "guide": 0.02,
            "reference": 0.02,
            "runbook": 0.02,
            "concept": 0.02,
        },
    ]

    report = compare(
        labels,
        baseline_predictions,
        [10, 20],
        system1_predictions,
        probabilities,
        [8, 12],
    )

    assert report["frozen_system1_p95_threshold_ms"] == 20
    assert report["recommendation"] == "go"
    assert set(report["baseline"]) == {
        "count",
        "accuracy",
        "macro_f1",
        "p50_latency_ms",
        "p95_latency_ms",
    }
    assert set(report["system1"]) >= {"brier_score", "ece"}


def test_compare_recommends_calibration_for_fast_quality_regression():
    labels = ["guide", "api"]
    baseline_predictions = labels
    system1_predictions = ["concept", "api"]
    probabilities = [
        {
            "architecture": 0.02,
            "api": 0.02,
            "guide": 0.02,
            "reference": 0.02,
            "runbook": 0.02,
            "concept": 0.90,
        },
        {
            "architecture": 0.02,
            "api": 0.90,
            "guide": 0.02,
            "reference": 0.02,
            "runbook": 0.02,
            "concept": 0.02,
        },
    ]

    report = compare(
        labels,
        baseline_predictions,
        [20, 30],
        system1_predictions,
        probabilities,
        [8, 12],
    )

    assert report["recommendation"] == "fine-tune"


def test_compare_rejects_system1_when_it_misses_frozen_latency():
    labels = ["guide", "api"]
    probabilities = [
        {
            "architecture": 0.02,
            "api": 0.02,
            "guide": 0.90,
            "reference": 0.02,
            "runbook": 0.02,
            "concept": 0.02,
        },
        {
            "architecture": 0.02,
            "api": 0.90,
            "guide": 0.02,
            "reference": 0.02,
            "runbook": 0.02,
            "concept": 0.02,
        },
    ]

    report = compare(labels, labels, [10, 20], labels, probabilities, [8, 25])

    assert report["recommendation"] == "reject"
