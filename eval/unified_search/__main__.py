from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

import httpx

from .fixtures import FIXED_CASES
from .report import write_reports
from .runner import build_evidence


def _runs(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--runs must be exactly 20") from exc
    if parsed != 20:
        raise argparse.ArgumentTypeError("--runs must be exactly 20")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m eval.unified_search")
    parser.add_argument("--output-dir", required=True, metavar="PATH")
    parser.add_argument("--runs", type=_runs, default=20, help="Exactly 20 included pairs per fixed scenario.")
    parser.add_argument("--model", default=os.getenv("IWIKI_CHAT_MODEL", "").strip(), metavar="MODEL")
    return parser


def _public_registry_contains_tool() -> bool:
    from iwiki_mcp.server import mcp

    listed = asyncio.run(mcp.list_tools())
    tools = getattr(listed, "tools", listed)
    return any(getattr(tool, "name", None) == "wiki_unified_search" for tool in tools)


def _tool_calling_preflight(client: httpx.Client, model: str) -> dict[str, Any]:
    """Prove tool-call initiation without retaining provider response content."""
    payload = {"model": model, "messages": [{"role": "user", "content": "Call the supplied tool."}],
               "tools": [{"type": "function", "function": {"name": "preflight", "description": "Capability probe",
                          "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}],
               "tool_choice": {"type": "function", "function": {"name": "preflight"}}, "temperature": 0}
    try:
        raw_response = client.post("/chat/completions", json=payload)
        if hasattr(raw_response, "raise_for_status"):
            raw_response.raise_for_status()
        response = raw_response.json()
        calls = response["choices"][0]["message"]["tool_calls"]
        available = False
        if isinstance(calls, list):
            for call in calls:
                function = call.get("function") if isinstance(call, dict) else None
                arguments = function.get("arguments") if isinstance(function, dict) else None
                if (isinstance(call, dict) and isinstance(call.get("id"), str) and call["id"] and
                        call.get("type") == "function" and isinstance(function, dict) and
                        function.get("name") == "preflight" and isinstance(arguments, str)):
                    parsed = json.loads(arguments)
                    if parsed == {}:
                        available = True
                        break
        return {"available": available, "status": "supported" if available else "failed_response"}
    except httpx.HTTPError:
        return {"available": False, "status": "failed_transport"}
    except Exception:
        return {"available": False, "status": "failed_response"}


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    endpoint, key, model = (os.getenv("IWIKI_LLM_BASE_URL", "").strip(),
                            os.getenv("IWIKI_LLM_KEY", "").strip(), args.model.strip())
    configured = bool(endpoint and key and model)
    registry_contains_tool = _public_registry_contains_tool()
    if configured:
        with httpx.Client(base_url=endpoint, headers={"Authorization": f"Bearer {key}"}, timeout=30.0) as client:
            preflight = _tool_calling_preflight(client, model)
            tool_calling_available = preflight["available"]
            def post_factory(_case, _arm, _run):
                def post(path, *, json):
                    response = client.post(path, json=json)
                    response.raise_for_status()
                    try:
                        return response.json()
                    except ValueError:
                        return {}
                return post
            evidence = build_evidence(FIXED_CASES, runs=args.runs, model=model,
                                      transport_configured=True, post_factory=post_factory if tool_calling_available else None,
                                      public_registry_contains_tool=registry_contains_tool,
                                      tool_calling_available=tool_calling_available, preflight=preflight)
    else:
        evidence = build_evidence(FIXED_CASES, runs=args.runs, model=model,
                                  transport_configured=False, post_factory=None,
                                  public_registry_contains_tool=registry_contains_tool,
                                  tool_calling_available=False, preflight={"available": False, "status": "missing_configuration"})
    write_reports(evidence, args.output_dir)
    if not configured:
        print("error: IWIKI_LLM_BASE_URL, IWIKI_LLM_KEY, and IWIKI_CHAT_MODEL/--model are required", file=sys.stderr)
    return {"implement": 0, "do_not_implement": 1, "blocked": 2}[evidence["decision"]]


if __name__ == "__main__":
    raise SystemExit(main())
