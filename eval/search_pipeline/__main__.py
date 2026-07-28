from __future__ import annotations

import argparse
from contextlib import nullcontext
from dataclasses import replace
import sys
from typing import Iterable

from iwiki_mcp.base import BaseError
from iwiki_mcp.engine.config import Config
from iwiki_mcp.engine.config import ConfigError

from .envfile import apply_env_file
from .envfile import validate_env_file_path
from .fixtures import BenchmarkCase
from .fixtures import DEFAULT_LIVE_CASES
from .report import write_reports
from .runner import run_live_traces
from .runner import run_pareto_experiment


_VALID_MODES = {"hybrid", "lexical", "semantic"}


def _parse_modes(value: str) -> list[str]:
    modes = [mode.strip().lower() for mode in value.split(",")]
    invalid = [mode for mode in modes if mode not in _VALID_MODES]
    if invalid:
        allowed = ", ".join(sorted(_VALID_MODES))
        raise argparse.ArgumentTypeError(
            f"invalid search mode: {', '.join(invalid)}; allowed values: {allowed}"
        )
    return modes


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--k must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("--k must be a positive integer")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.search_pipeline",
        description="Run the live-first iwiki search pipeline benchmark.",
    )
    parser.add_argument("--domain", default="iwiki-mcp")
    parser.add_argument("--out", required=True, help="Output directory for reports.")
    parser.add_argument("--env-file", help="Optional operator-created env file.")
    parser.add_argument(
        "--modes",
        type=_parse_modes,
        default=_parse_modes("hybrid,lexical,semantic"),
        help="Comma-separated modes: hybrid, lexical, semantic.",
    )
    parser.add_argument(
        "--k",
        type=_positive_int,
        help="Override k for live benchmark cases.",
    )
    parser.add_argument("--latency-ceiling-ms", type=float)
    parser.add_argument(
        "--pareto",
        action="store_true",
        help="Run the fixed live Pareto fusion and rerank experiment.",
    )
    return parser


def _with_k(cases: Iterable[BenchmarkCase], k: int | None) -> list[BenchmarkCase]:
    case_list = list(cases)
    if k is None:
        return case_list
    return [replace(case, k=k) for case in case_list]


def _warn_env_file(validation: dict) -> None:
    for warning in validation.get("warnings", []):
        print(f"warning: {warning}", file=sys.stderr)


def _reject_env_file(validation: dict) -> int | None:
    if validation.get("ok"):
        return None
    for error in validation.get("errors", []):
        print(f"error: {error}", file=sys.stderr)
    return 2


def _mark_latency_ceiling(evidence: dict, latency_ceiling_ms: float | None) -> None:
    if latency_ceiling_ms is None:
        return
    run_settings = evidence.setdefault("run_settings", {})
    run_settings["latency_ceiling_ms"] = latency_ceiling_ms
    mean_latency = (
        evidence.get("summary", {})
        .get("rollup", {})
        .get("latency_ms", 0.0)
    )
    if float(mean_latency) > latency_ceiling_ms:
        run_settings["latency_ceiling_exceeded"] = True
        print(
            "warning: mean latency exceeded latency ceiling",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    if args.pareto and args.modes != ["hybrid", "lexical", "semantic"]:
        print(
            "error: --pareto requires --modes hybrid,lexical,semantic",
            file=sys.stderr,
        )
        return 2

    if args.env_file:
        validation = validate_env_file_path(args.env_file, args.out)
        rejected = _reject_env_file(validation)
        if rejected is not None:
            return rejected
        _warn_env_file(validation)
        env_context = apply_env_file(args.env_file)
    else:
        env_context = nullcontext()

    try:
        with env_context:
            try:
                cfg = Config.load()
            except ValueError:
                print(
                    "error: invalid numeric configuration; check IWIKI_* "
                    "numeric environment variables.",
                    file=sys.stderr,
                )
                return 2
            cases = _with_k(DEFAULT_LIVE_CASES, args.k)
            if args.pareto:
                evidence = run_pareto_experiment(cfg, args.domain, cases)
            else:
                evidence = run_live_traces(
                    cfg,
                    args.domain,
                    args.modes,
                    cases,
                    latency_ceiling_ms=args.latency_ceiling_ms,
                )
            _mark_latency_ceiling(evidence, args.latency_ceiling_ms)
            paths = write_reports(evidence, args.out)
    except (ConfigError, BaseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ValueError:
        print(
            "error: invalid benchmark input; check CLI arguments and cases.",
            file=sys.stderr,
        )
        return 2
    except Exception:
        print(
            "error: benchmark failed unexpectedly; no raw provider details shown.",
            file=sys.stderr,
        )
        return 2

    for label, path in sorted(paths.items()):
        print(f"{label}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
