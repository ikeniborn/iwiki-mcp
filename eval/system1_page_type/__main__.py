"""Command-line entry point for the System One page-type benchmark."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from iwiki_mcp.engine.config import Config, ConfigError

from .runner import BenchmarkError, load_corpus, run_benchmark, write_report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare the current page classifier with System One."
    )
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        cfg = Config.load()
        if not cfg.chat_model:
            raise BenchmarkError("IWIKI_CHAT_MODEL must identify the baseline model")
        if not cfg.system1_shadow:
            raise BenchmarkError("IWIKI_SYSTEM1_SHADOW must be enabled")
        report = run_benchmark(load_corpus(args.corpus), cfg)
        write_report(report, args.output)
    except (BenchmarkError, ConfigError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
