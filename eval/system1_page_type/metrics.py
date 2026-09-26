"""Deterministic quality, latency, and calibration metrics."""
from __future__ import annotations

import math
from typing import Sequence

from iwiki_mcp.engine.frontmatter import CLASSIFIABLE_TYPES


def _nearest_rank(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _macro_f1(labels: Sequence[str], predictions: Sequence[str]) -> float:
    scores = []
    for page_type in CLASSIFIABLE_TYPES:
        true_positive = sum(
            label == page_type and prediction == page_type
            for label, prediction in zip(labels, predictions)
        )
        false_positive = sum(
            label != page_type and prediction == page_type
            for label, prediction in zip(labels, predictions)
        )
        false_negative = sum(
            label == page_type and prediction != page_type
            for label, prediction in zip(labels, predictions)
        )
        denominator = 2 * true_positive + false_positive + false_negative
        scores.append(2 * true_positive / denominator if denominator else 0.0)
    return sum(scores) / len(scores)


def _brier_score(labels: Sequence[str], probabilities: Sequence[dict[str, float]]) -> float:
    total = 0.0
    for label, vector in zip(labels, probabilities):
        total += sum(
            (vector[page_type] - float(label == page_type)) ** 2
            for page_type in CLASSIFIABLE_TYPES
        )
    return total / len(labels)


def _ece(
    labels: Sequence[str],
    predictions: Sequence[str],
    probabilities: Sequence[dict[str, float]],
    bins: int,
) -> float:
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for label, prediction, vector in zip(labels, predictions, probabilities):
        confidence = max(vector.values())
        bucket = min(int(confidence * bins), bins - 1)
        buckets[bucket].append((confidence, label == prediction))
    total = len(labels)
    return sum(
        len(bucket) / total
        * abs(
            sum(correct for _confidence, correct in bucket) / len(bucket)
            - sum(confidence for confidence, _correct in bucket) / len(bucket)
        )
        for bucket in buckets
        if bucket
    )


def evaluate_predictions(
    labels: Sequence[str],
    predictions: Sequence[str],
    latencies_ms: Sequence[float],
) -> dict[str, float | int]:
    """Evaluate classification and latency without probability metrics."""
    count = len(labels)
    if not count or not (count == len(predictions) == len(latencies_ms)):
        raise ValueError("inputs must have the same non-zero length")
    allowed = set(CLASSIFIABLE_TYPES)
    if any(label not in allowed for label in labels):
        raise ValueError("labels must use the governed page types")
    if any(prediction not in allowed for prediction in predictions):
        raise ValueError("predictions must use the governed page types")
    if any(not math.isfinite(value) or value < 0 for value in latencies_ms):
        raise ValueError("latencies must be finite non-negative values")
    return {
        "count": count,
        "accuracy": sum(
            label == prediction for label, prediction in zip(labels, predictions)
        )
        / count,
        "macro_f1": _macro_f1(labels, predictions),
        "p50_latency_ms": _nearest_rank(latencies_ms, 0.50),
        "p95_latency_ms": _nearest_rank(latencies_ms, 0.95),
    }


def evaluate(
    labels: Sequence[str],
    predictions: Sequence[str],
    probabilities: Sequence[dict[str, float]],
    latencies_ms: Sequence[float],
    *,
    bins: int = 10,
) -> dict[str, float | int]:
    """Evaluate aligned System One decisions over the governed taxonomy."""
    base = evaluate_predictions(labels, predictions, latencies_ms)
    count = len(labels)
    if count != len(probabilities):
        raise ValueError("inputs must have the same non-zero length")
    if bins <= 0:
        raise ValueError("bins must be positive")
    allowed = set(CLASSIFIABLE_TYPES)
    for vector in probabilities:
        if set(vector) != allowed:
            raise ValueError("probability keys must match the governed page types")
        values = tuple(vector.values())
        if any(not math.isfinite(value) or not 0 <= value <= 1 for value in values):
            raise ValueError("probabilities must be finite values from zero to one")
        if not math.isclose(sum(values), 1.0, abs_tol=1e-3):
            raise ValueError("probabilities must sum to one")
    return {
        **base,
        "brier_score": _brier_score(labels, probabilities),
        "ece": _ece(labels, predictions, probabilities, bins),
    }


def compare(
    labels: Sequence[str],
    baseline_predictions: Sequence[str],
    baseline_latencies_ms: Sequence[float],
    system1_predictions: Sequence[str],
    system1_probabilities: Sequence[dict[str, float]],
    system1_latencies_ms: Sequence[float],
) -> dict[str, object]:
    """Compare System One with the current classifier using baseline p95 as gate."""
    baseline = evaluate_predictions(
        labels, baseline_predictions, baseline_latencies_ms
    )
    frozen_p95 = baseline["p95_latency_ms"]
    candidate = evaluate(
        labels,
        system1_predictions,
        system1_probabilities,
        system1_latencies_ms,
    )
    if candidate["p95_latency_ms"] > frozen_p95:
        recommendation = "reject"
    elif (
        candidate["accuracy"] < baseline["accuracy"]
        or candidate["macro_f1"] < baseline["macro_f1"]
    ):
        recommendation = "fine-tune"
    else:
        recommendation = "go"
    return {
        "frozen_system1_p95_threshold_ms": frozen_p95,
        "baseline": baseline,
        "system1": candidate,
        "recommendation": recommendation,
    }
