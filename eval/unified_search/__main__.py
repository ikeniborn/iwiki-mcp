from __future__ import annotations

import argparse
import asyncio
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
        raise argparse.ArgumentTypeError("--runs must be an integer >=3") from exc
    if parsed < 3:
        raise argparse.ArgumentTypeError("--runs must be an integer >=3")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m eval.unified_search")
    parser.add_argument("--output-dir", required=True, metavar="PATH")
    parser.add_argument("--runs", type=_runs, default=3)
    parser.add_argument("--model", default=os.getenv("IWIKI_CHAT_MODEL", "").strip(), metavar="MODEL")
    return parser


def _public_registry_contains_tool() -> bool:
    from iwiki_mcp.server import mcp

    listed = asyncio.run(mcp.list_tools())
    tools = getattr(listed, "tools", listed)
    return any(getattr(tool, "name", None) == "wiki_unified_search" for tool in tools)


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
            def post_factory(_case, _arm, _run):
                return lambda path, json: client.post(path, json=json).json()
            evidence = build_evidence(FIXED_CASES, runs=args.runs, model=model,
                                      transport_configured=True, post_factory=post_factory,
                                      public_registry_contains_tool=registry_contains_tool)
    else:
        evidence = build_evidence(FIXED_CASES, runs=args.runs, model=model,
                                  transport_configured=False, post_factory=None,
                                  public_registry_contains_tool=registry_contains_tool)
    write_reports(evidence, args.output_dir)
    if not configured:
        print("error: IWIKI_LLM_BASE_URL, IWIKI_LLM_KEY, and IWIKI_CHAT_MODEL/--model are required", file=sys.stderr)
    return {"implement": 0, "do_not_implement": 1, "blocked": 2}[evidence["decision"]]


if __name__ == "__main__":
    raise SystemExit(main())
