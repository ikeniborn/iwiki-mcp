"""CLI: python -m eval.system1_decisions --log LOG --page-ids IDS [--training-out OUT]."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .report import summarize, training_candidates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.system1_decisions")
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--page-ids", type=Path, required=True,
                        help="one current page id (domain/type/slug) per line")
    parser.add_argument("--training-out", type=Path)
    args = parser.parse_args(argv)
    records = [json.loads(line) for line in args.log.read_text().splitlines() if line]
    page_ids = {line.strip() for line in args.page_ids.read_text().splitlines()
                if line.strip()}
    print(json.dumps(summarize(records, page_ids), indent=2))
    if args.training_out:
        fd = os.open(args.training_out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            for row in training_candidates(records, page_ids):
                handle.write(json.dumps(row) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
