"""Fail-soft reranking through a LiteLLM-compatible endpoint."""

import math
import numbers

import httpx

from .config import Config


_TIMEOUT = 60.0
_WARNING = {"applied": False, "warning": "reranker unavailable"}


def rerank_candidates(cfg: Config, query: str,
                      candidates: list[dict]) -> tuple[list[dict], dict]:
    preliminary = [
        {key: value for key, value in candidate.items() if key != "text"}
        for candidate in candidates
    ]
    if not candidates:
        return preliminary, dict(_WARNING)

    try:
        response = httpx.post(
            f"{cfg.base_url}/rerank",
            json={
                "model": cfg.rerank_model,
                "query": query,
                "documents": [candidate["text"] for candidate in candidates],
                "top_n": len(candidates),
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
    duplicates = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        index = row.get("index")
        score = row.get("relevance_score")
        if (not isinstance(index, int) or isinstance(index, bool)
                or not 0 <= index < len(candidates)):
            continue
        if (not isinstance(score, numbers.Real) or isinstance(score, bool)
                or not math.isfinite(float(score))):
            continue
        if index in scores or index in duplicates:
            scores.pop(index, None)
            duplicates.add(index)
            continue
        scores[index] = float(score)

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
    return ranked, {"applied": True}
