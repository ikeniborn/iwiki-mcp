"""Unregistered, read-only unified search candidate."""

from collections.abc import Callable
from typing import Any


def _failed_branch() -> dict[str, Any]:
    return {"results": [], "error": {"code": "branch_failed"}}


def _call_branch(call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        value = call()
    except Exception:
        return _failed_branch()
    return value if isinstance(value, dict) else _failed_branch()


def _empty_degradation(reason: str | None = None) -> dict[str, Any]:
    return {"degraded": reason is not None, "reason": reason}


def _empty_context() -> dict[str, Any]:
    return {}


def _fresh_unique_seeds(code: dict[str, Any]) -> list[str]:
    if code.get("fresh") is not True or code.get("state") not in (None, "ready"):
        return []
    seeds: list[str] = []
    seen: set[str] = set()
    for result in code.get("results", []):
        if not isinstance(result, dict):
            continue
        entity_id = result.get("entity_id")
        if isinstance(entity_id, str) and entity_id and entity_id not in seen:
            seen.add(entity_id)
            seeds.append(entity_id)
            if len(seeds) == 3:
                break
    return seeds


def compose_unified_search(
    *,
    wiki_call: Callable[[], dict[str, Any]],
    code_call: Callable[[], dict[str, Any]],
    context_call: Callable[[list[str]], dict[str, Any]],
) -> dict[str, Any]:
    """Compose specialized responses without registering or mutating a tool."""
    wiki = _call_branch(wiki_call)
    code = _call_branch(code_call)
    wiki_failed = "error" in wiki and not wiki.get("results")
    code_failed = "error" in code and not code.get("results")
    seeds = _fresh_unique_seeds(code)

    if not seeds:
        code_reason = "failed" if code_failed else None
        if not code_failed and (
            code.get("fresh") is not True
            or code.get("state") not in (None, "ready")
        ):
            code_reason = code.get("state") or "not_fresh"
        not_run_reason = "not_run" if code_reason else None
        return {
            "wiki": wiki,
            "code": code,
            "associations": [],
            "context": _empty_context(),
            "degradation": {
                "wiki": _empty_degradation("failed" if wiki_failed else None),
                "code": _empty_degradation(code_reason),
                "context": _empty_degradation(not_run_reason),
                "associations": _empty_degradation(not_run_reason),
            },
        }

    try:
        raw_context = context_call(seeds)
        context = raw_context if isinstance(raw_context, dict) else None
    except Exception:
        context = None

    if context is None:
        return {
            "wiki": wiki,
            "code": code,
            "associations": [],
            "context": _empty_context(),
            "degradation": {
                "wiki": _empty_degradation("failed" if wiki_failed else None),
                "code": _empty_degradation("failed" if code_failed else None),
                "context": _empty_degradation("failed"),
                "associations": _empty_degradation("failed"),
            },
        }

    if context.get("revision") != code.get("revision"):
        reason = "revision_changed"
    elif context.get("fresh") is not True:
        reason = context.get("code")
        if not isinstance(reason, str) or not reason or len(reason) > 64:
            reason = "not_fresh"
    else:
        reason = None

    if reason is not None:
        failed_context = dict(context)
        failed_context.pop("wiki_pages", None)
        for key in ("seeds", "nodes", "relations", "files"):
            failed_context[key] = []
        return {
            "wiki": wiki,
            "code": code,
            "associations": [],
            "context": failed_context,
            "degradation": {
                "wiki": _empty_degradation("failed" if wiki_failed else None),
                "code": _empty_degradation("failed" if code_failed else None),
                "context": _empty_degradation(reason),
                "associations": _empty_degradation(reason),
            },
        }

    context = dict(context)
    wiki_pages = list(context.pop("wiki_pages", []))
    stale = bool(context.get("wiki_links_stale")) or "wiki_links_stale" in context.get("warnings", [])
    if stale:
        wiki_pages = []
    return {
        "wiki": wiki,
        "code": code,
        "associations": wiki_pages,
        "context": context,
        "degradation": {
            "wiki": _empty_degradation("failed" if wiki_failed else None),
            "code": _empty_degradation("failed" if code_failed else None),
            "context": _empty_degradation(None),
            "associations": _empty_degradation("wiki_links_stale" if stale else None),
        },
    }
