"""Deterministic reciprocal rank fusion for ranked search signals."""

_RRF_K = 60


def _identity(hit: dict) -> tuple:
    return hit["domain"], hit["file"], hit["heading"], hit["chunk"]


def fuse_ranked(signals: dict[str, list[dict]], limit: int) -> list[dict]:
    if limit <= 0:
        return []

    merged = {}
    for signal, hits in signals.items():
        seen = set()
        for rank, hit in enumerate(hits, 1):
            identity = _identity(hit)
            if identity in seen:
                continue
            seen.add(identity)
            fused = merged.setdefault(identity, {**hit, "score": 0.0, "signals": []})
            fused["score"] += 1 / (_RRF_K + rank)
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
