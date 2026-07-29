"""Deterministic reciprocal rank fusion for ranked search signals."""

from math import isfinite

_RRF_K = 60


def _identity(hit: dict) -> tuple:
    return hit["domain"], hit["file"], hit["heading"], hit["chunk"]


def fuse_ranked(
    signals: dict[str, list[dict]],
    limit: int,
    signal_weights: dict[str, float] | None = None,
    *,
    rrf_k: int = _RRF_K,
) -> list[dict]:
    if isinstance(rrf_k, bool) or not isinstance(rrf_k, int) or rrf_k <= 0:
        raise ValueError("rrf_k must be a positive integer")

    if limit <= 0:
        return []

    signal_weights = signal_weights or {}
    for weight in signal_weights.values():
        if (
            isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or not isfinite(weight)
            or weight <= 0
        ):
            raise ValueError("signal weight must be a finite positive number")

    merged = {}
    for signal, hits in signals.items():
        weight = signal_weights.get(signal, 1.0)
        seen = set()
        for rank, hit in enumerate(hits, 1):
            identity = _identity(hit)
            if identity in seen:
                continue
            seen.add(identity)
            fused = merged.setdefault(identity, {**hit, "score": 0.0, "signals": []})
            fused["score"] += weight / (rrf_k + rank)
            fused["signals"].append(signal)

    return sorted(
        merged.values(),
        key=lambda hit: (
            -hit["score"],
            hit["domain"],
            hit["file"],
            hit.get("ordinal", 0),
            hit["heading"],
            hit["chunk"],
        ),
    )[:limit]
