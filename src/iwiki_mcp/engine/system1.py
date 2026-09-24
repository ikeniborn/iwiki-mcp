"""Fail-open System One shadow decision for wiki page types."""
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
    """Return a validated shadow decision, never affecting the write caller."""
    if not cfg.system1_shadow:
        return None

    started = time.perf_counter()
    try:
        response = httpx.post(
            f"{cfg.system1_base_url}/systemone",
            json={
                "state": {"document": body[:_MAX_DOCUMENT_CHARS]},
                "questions": {
                    "page_type": {
                        "type": "choice",
                        "instructions": "Choose the dominant intent of this wiki page.",
                        "criteria": _CRITERIA,
                    }
                },
            },
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
