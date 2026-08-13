"""Command-line entry point for the code graph benchmark gate."""
from __future__ import annotations

import argparse
import shlex
import sys

from .runner import BenchmarkGateError, run_benchmark


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run isolated code graph quality and performance gates.",
    )
    parser.add_argument(
        "--fixture-root",
        default="tests/fixtures/codegraph",
        help="Approved golden-fixture root.",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Directory for JSON and Markdown evidence.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    effective_argv = sys.argv[1:] if argv is None else argv
    command = "uv run python -m eval.code_graph " + " ".join(
        shlex.quote(value) for value in effective_argv
    )
    try:
        run_benchmark(
            output=arguments.output,
            fixture_root=arguments.fixture_root,
            command=command,
        )
    except BenchmarkGateError as exc:
        print(f"code graph benchmark gate failed: {exc}", file=sys.stderr)
        return 1
    print(f"code graph benchmark evidence: {arguments.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
