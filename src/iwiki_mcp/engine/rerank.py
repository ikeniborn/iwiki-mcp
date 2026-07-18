"""Fail-soft reranking through a LiteLLM-compatible endpoint."""

import math
import numbers

import httpx

from .config import Config


_TIMEOUT = 60.0
_WARNING = {"applied": False, "warning": "reranker unavailable"}


def rerank_candidates(cfg: Config, query: str,
                      candidates: list[dict],
                      top_n: int | None = None) -> tuple[list[dict], dict]:
    preliminary = [
        {key: value for key, value in candidate.items() if key != "text"}
        for candidate in candidates
    ]
    if not candidates:
        return preliminary, dict(_WARNING)
    result_count = len(candidates) if top_n is None else min(
        max(1, top_n), len(candidates)
    )

    try:
        response = httpx.post(
            f"{cfg.base_url}/rerank",
            json={
                "model": cfg.rerank_model,
                "query": query,
                "documents": [candidate["text"] for candidate in candidates],
                "top_n": result_count,
            },
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        rows = response.json().get("results")
    except (httpx.HTTPError, ValueError, AttributeError):
        return preliminary, dict(_WARNING)

    if not isinstance(rows, list):
        return preliminary, dict(_WARNING)

    scores = {}
    seen = set()
    duplicates = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        index = row.get("index")
        score = row.get("relevance_score")
        if (not isinstance(index, int) or isinstance(index, bool)
                or not 0 <= index < len(candidates)):
            continue
        if index in seen:
            duplicates.add(index)
            continue
        seen.add(index)
        if not isinstance(score, numbers.Real) or isinstance(score, bool):
            continue
        try:
            numeric_score = float(score)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(numeric_score):
            continue
        scores[index] = numeric_score

    for index in duplicates:
        scores.pop(index, None)
    if not scores:
        return preliminary, dict(_WARNING)

    ranked_indices = sorted(scores, key=lambda index: (-scores[index], index))
    ranked_indices.extend(index for index in range(len(candidates)) if index not in scores)
    ranked = []
    for index in ranked_indices:
        candidate = dict(preliminary[index])
        if index in scores:
            candidate["score"] = scores[index]
        ranked.append(candidate)
    metadata = {"applied": True}
    if top_n is not None:
        metadata["_scored_count"] = len(scores)
    return ranked, metadata
