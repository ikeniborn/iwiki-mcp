"""Aggregate System One decision logs and export reviewed training labels.

Inputs are the private decision log (``IWIKI_SYSTEM1_DECISION_LOG``) and the list
of current page ids (``domain/type/slug``). Outputs hold ids, types, and counts
only; page bodies are read from the wiki when a training corpus is built.
"""
from __future__ import annotations

from collections import Counter
import math
from typing import Iterable

from iwiki_mcp.engine.frontmatter import CLASSIFIABLE_TYPES


def _tail(identity: str) -> str:
    return identity.split("/", 1)[1] if "/" in identity else identity


def _nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def latest_by_page(records: Iterable[dict]) -> dict[tuple[str, str], dict]:
    """Last decision per (domain, slug tail), so a retyped page maps to one entry."""
    latest: dict[tuple[str, str], dict] = {}
    for record in sorted(records, key=lambda r: r["ts"]):
        latest[(record["domain"], _tail(record["identity"]))] = record
    return latest


def warning_outcome(record: dict, page_ids: set[str]) -> str:
    """accepted, kept, or missing for a warned write, judged by the current page."""
    tail = _tail(record["identity"])
    if f"{record['domain']}/{record['predicted']}/{tail}" in page_ids:
        return "accepted"
    if f"{record['domain']}/{record['identity']}" in page_ids:
        return "kept"
    return "missing"


def summarize(records: list[dict], page_ids: set[str]) -> dict:
    ok = [r for r in records if r["status"] == "ok"]
    explicit = [r for r in ok if r["type_source"] == "explicit"
                and r["requested_type"] in CLASSIFIABLE_TYPES]
    warned = [r for r in records if r["warned"]]
    outcomes = Counter(warning_outcome(r, page_ids) for r in warned)
    latencies = [r["latency_ms"] for r in ok]
    return {
        "decisions": len(records),
        "by_status": dict(sorted(Counter(r["status"] for r in records).items())),
        "by_type_source": dict(sorted(Counter(r["type_source"] for r in records).items())),
        "system1_assigned_types": dict(sorted(Counter(
            r["final_type"] for r in records if r["type_source"] == "system1").items())),
        "explicit_agreement": (
            round(sum(r["predicted"] == r["requested_type"] for r in explicit)
                  / len(explicit), 4) if explicit else None),
        "explicit_classifiable": len(explicit),
        "warnings": len(warned),
        "warning_outcomes": dict(sorted(outcomes.items())),
        "warning_acceptance": (
            round(outcomes["accepted"] / (outcomes["accepted"] + outcomes["kept"]), 4)
            if outcomes["accepted"] + outcomes["kept"] else None),
        "latency_p50_ms": _nearest_rank(latencies, 0.5),
        "latency_p95_ms": _nearest_rank(latencies, 0.95),
    }


def training_candidates(records: list[dict], page_ids: set[str]) -> list[dict]:
    """Human-confirmed labels for pages that still exist.

    A warned write whose author moved the page to the suggested type yields that
    type (``accepted_warning``); one kept after the warning yields the authored
    type (``kept_after_warning``). Unwarned explicit writes yield the authored
    type (``explicit``). System One assignments are excluded: they are the
    model's own output, not a reviewed label.
    """
    out = []
    for (domain, tail), record in sorted(latest_by_page(records).items()):
        if record["status"] != "ok" or record["type_source"] == "system1":
            continue
        if record["warned"]:
            outcome = warning_outcome(record, page_ids)
            if outcome == "accepted":
                label, source = record["predicted"], "accepted_warning"
            elif outcome == "kept":
                label, source = record["requested_type"], "kept_after_warning"
            else:
                continue
        elif (record["type_source"] == "explicit"
              and record["requested_type"] in CLASSIFIABLE_TYPES
              and f"{domain}/{record['identity']}" in page_ids):
            label, source = record["requested_type"], "explicit"
        else:
            continue
        out.append({"id": f"{domain}/{label}/{tail}", "label": label,
                    "label_source": source, "predicted": record["predicted"]})
    return out
