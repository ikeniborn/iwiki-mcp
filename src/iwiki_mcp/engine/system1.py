"""Fail-open System One page-type decisions: shadow, write guidance, search boost."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
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
    if not (cfg.system1_shadow or cfg.system1_guidance or cfg.system1_assign_type):
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


def assigned_type(cfg: Config, decision: PageTypeDecision | None) -> str | None:
    """Type System One may assign to a page written without one."""
    return _actionable(cfg, decision) if cfg.system1_assign_type else None


def assignment_warning(decision: PageTypeDecision, page_type: str) -> str:
    return (
        f"type '{page_type}' assigned by System One "
        f"(p={decision.probabilities[page_type]:.2f}); pass `type` to override"
    )


def record_decision(
    cfg: Config,
    *,
    backend: str,
    domain: str,
    identity: str,
    body: str,
    requested_type: str | None,
    final_type: str,
    type_source: str,
    decision: PageTypeDecision | None,
    warned: bool,
) -> None:
    """Append one decision to the private JSONL log; never raises.

    The record references the page by domain, identity, and body hash only; it
    never contains the page body, so later training reads text from the wiki.
    """
    if not cfg.system1_decision_log or decision is None:
        return
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "backend": backend,
        "domain": domain,
        "identity": identity,
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "requested_type": fm.normalize_type(requested_type) if requested_type else None,
        "final_type": final_type,
        "type_source": type_source,
        "status": decision.status,
        "predicted": decision.page_type,
        "probabilities": decision.probabilities,
        "model": decision.model or cfg.system1_model or None,
        "latency_ms": round(decision.latency_ms, 1),
        "warned": warned,
    }
    line = json.dumps(record, sort_keys=True) + "\n"
    try:
        os.makedirs(os.path.dirname(cfg.system1_decision_log), mode=0o700, exist_ok=True)
        fd = os.open(cfg.system1_decision_log,
                     os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        return
