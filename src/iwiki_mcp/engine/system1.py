"""Fail-open System One page-type decisions: shadow, write guidance, search boost."""
from __future__ import annotations

from dataclasses import dataclass
import math
import time

import httpx

from .config import Config
from . import frontmatter as fm

_TIMEOUT = 2.0
_MAX_DOCUMENT_CHARS = 6000
_CRITERIA = {
    "architecture": "system structure, components, data flow, or modules",
    "api": "call or interface surface, including functions, endpoints, or signatures",
    "guide": "step-by-step instructions or usage guidance",
    "reference": "lookup material such as keys, flags, configuration, or tables",
    "runbook": "operational procedure such as deployment or incident response",
    "concept": "explanation of an idea or model",
}


@dataclass(frozen=True)
class PageTypeDecision:
    status: str
    page_type: str | None
    probabilities: dict[str, float]
    latency_ms: float
    model: str | None = None


def _invalid(started: float, status: str) -> PageTypeDecision:
    return PageTypeDecision(
        status=status,
        page_type=None,
        probabilities={},
        latency_ms=(time.perf_counter() - started) * 1000,
    )


def classify_page_type(cfg: Config, body: str) -> PageTypeDecision | None:
    """Return a validated decision for a page body; None when System One is off."""
    if not (cfg.system1_shadow or cfg.system1_guidance):
        return None
    return _decide(cfg, body)


def _decide(cfg: Config, body: str) -> PageTypeDecision:
    started = time.perf_counter()
    request = {"model": cfg.system1_model} if cfg.system1_model else {}
    request["state"] = {"document": body[:_MAX_DOCUMENT_CHARS]}
    request["questions"] = {
        "page_type": {
            "type": "choice",
            "instructions": "Choose the dominant intent of this wiki page.",
            "criteria": _CRITERIA,
        }
    }
    try:
        response = httpx.post(
            f"{cfg.system1_base_url}/systemone",
            json=request,
            headers={"Authorization": f"Bearer {cfg.system1_api_key}"},
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        answer = payload["answers"]["page_type"]
        if answer["type"] != "choice":
            raise ValueError("unexpected answer type")
        page_type = answer["choice"]
        probabilities = answer["probabilities"]
        if page_type not in fm.CLASSIFIABLE_TYPES:
            raise ValueError("unexpected page type")
        if set(probabilities) != set(fm.CLASSIFIABLE_TYPES):
            raise ValueError("incomplete probability vector")
        normalized = {key: float(probabilities[key]) for key in fm.CLASSIFIABLE_TYPES}
        if any(not math.isfinite(value) or not 0 <= value <= 1 for value in normalized.values()):
            raise ValueError("invalid probability")
        if not math.isclose(sum(normalized.values()), 1.0, abs_tol=1e-3):
            raise ValueError("probabilities do not sum to one")
        model = payload.get("model")
        if model is not None and not isinstance(model, str):
            raise ValueError("invalid model")
        return PageTypeDecision(
            status="ok",
            page_type=page_type,
            probabilities=normalized,
            latency_ms=(time.perf_counter() - started) * 1000,
            model=model,
        )
    except httpx.HTTPError:
        return _invalid(started, "unavailable")
    except (KeyError, TypeError, ValueError, AttributeError):
        return _invalid(started, "invalid")
    except Exception:
        return _invalid(started, "invalid")


# Classes whose held-out recall was too low to act on (runbook 2/8, guide 0/2).
_WEAK_TYPES = frozenset({"runbook", "guide"})
_RRF_K = 60


def _actionable(cfg: Config, decision: PageTypeDecision | None) -> str | None:
    """The decided type when it is confident enough and not a weak class."""
    if decision is None or decision.status != "ok" or decision.page_type is None:
        return None
    if decision.page_type in _WEAK_TYPES:
        return None
    if decision.probabilities[decision.page_type] < cfg.system1_min_confidence:
        return None
    return decision.page_type


def type_guidance_warning(
    cfg: Config, decision: PageTypeDecision | None, explicit_type: str | None
) -> str | None:
    """Advisory warning when a confident decision disagrees with an explicit type."""
    if not cfg.system1_guidance or explicit_type is None:
        return None
    authored = fm.normalize_type(explicit_type)
    suggested = _actionable(cfg, decision)
    if authored not in fm.CLASSIFIABLE_TYPES or suggested in (None, authored):
        return None
    return (
        f"System One suggests type '{suggested}' "
        f"(p={decision.probabilities[suggested]:.2f}); explicit type '{authored}' kept"
    )


def _file_type(file: str) -> str | None:
    head = file.split("/", 1)[0]
    return head if "/" in file and head in fm.CLASSIFIABLE_TYPES else None


def boost_by_query_type(cfg: Config, query: str, ordered: list[dict]) -> list[dict]:
    """Reorder an existing ranking toward pages of the query's predicted type.

    Membership is unchanged: each item keeps its reciprocal-rank position score
    and gains ``system1_search_boost`` when its page type matches a confident,
    non-weak prediction for the query.
    """
    if cfg.system1_search_boost <= 0 or len(ordered) < 2:
        return ordered
    predicted = _actionable(cfg, _decide(cfg, query))
    if predicted is None:
        return ordered
    scored = [
        (
            1 / (_RRF_K + index + 1)
            + (cfg.system1_search_boost if _file_type(item["file"]) == predicted else 0),
            index,
            item,
        )
        for index, item in enumerate(ordered)
    ]
    scored.sort(key=lambda entry: (-entry[0], entry[1]))
    return [item for _score, _index, item in scored]
