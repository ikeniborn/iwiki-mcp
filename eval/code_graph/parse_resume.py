"""Measure cold, interrupted, resumed and independent clean SQLite CLI publication."""

import argparse
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time


def _worker(project: Path, output: Path, stop: int):
    from iwiki_mcp.admin import main
    from iwiki_mcp.codegraph.indexer import BuildControl, CodeGraphIndexer
    from iwiki_mcp.codegraph.languages.python import PythonAdapter

    calls = []
    accepted = 0
    parse_seconds = 0.0
    parse_started = 0.0
    cache = project.parent / "wiki" / ".iwiki"
    durable_bytes = sum(p.stat().st_size for p in cache.glob("parse-*/*"))
    peak_bytes = durable_bytes
    parse_file = PythonAdapter.parse_file
    parse_phase = CodeGraphIndexer._parse
    accept = BuildControl.accept_file
    replace = os.replace

    def record():
        output.write_text(json.dumps({
            "parser_calls": calls, "accepted_files": accepted,
            "parse_seconds": parse_seconds, "peak_scratch_bytes": peak_bytes,
        }))

    def traced_file(self, source, path):
        calls.append(path)
        return parse_file(self, source, path)

    def traced_parse(self, *args, **kwargs):
        nonlocal parse_started, parse_seconds
        parse_started = time.perf_counter()
        try:
            return parse_phase(self, *args, **kwargs)
        finally:
            parse_seconds = time.perf_counter() - parse_started

    def traced_accept(self):
        nonlocal accepted, parse_seconds
        accept(self)
        accepted += 1
        if accepted == stop:
            parse_seconds = time.perf_counter() - parse_started
            record()
            os._exit(73)

    def traced_replace(src, dst, **kwargs):
        nonlocal durable_bytes, peak_bytes
        if src == "record.tmp" and "src_dir_fd" in kwargs:
            incoming = os.stat(src, dir_fd=kwargs["src_dir_fd"]).st_size
            try:
                previous = os.stat(dst, dir_fd=kwargs["dst_dir_fd"]).st_size
            except FileNotFoundError:
                previous = 0
            peak_bytes = max(peak_bytes, durable_bytes + incoming)
            durable_bytes += incoming - previous
        return replace(src, dst, **kwargs)

    PythonAdapter.parse_file = traced_file
    CodeGraphIndexer._parse = traced_parse
    BuildControl.accept_file = traced_accept
    os.replace = traced_replace
    sys.argv = ["iwiki-mcp", "code", "publish", "--project", str(project), "--json"]
    try:
        main()
    finally:
        record()


def _run(project, checkout, output, stop):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(checkout / "src"), str(checkout)))}
    command = [sys.executable, __file__, "--worker", str(project), str(output), str(stop)]
    started = time.perf_counter()
    run = subprocess.run(command, cwd=project, env=env, capture_output=True, text=True,
                         timeout=1200)
    elapsed = time.perf_counter() - started
    if run.returncode != (73 if stop else 0):
        raise RuntimeError(f"publisher exit {run.returncode}: {run.stderr[-1000:]}")
    result = json.loads(output.read_text())
    result["wall_seconds"] = elapsed
    if not stop:
        result["revision"] = json.loads(run.stdout)["snapshot_revision"]
    return result


def measure(baseline: Path, output: Path, modules: int | None, repetitions: int):
    from tests.codegraph.publication_contract_support import generate_python_project
    from tests.codegraph.synthetic_wiki import create_sqlite_project

    source = Path(__file__).resolve().parents[2]
    rows = []
    with tempfile.TemporaryDirectory(prefix="parse-resume-") as temporary:
        root = Path(temporary)
        project = create_sqlite_project(root)
        if modules is None:
            sources = baseline / "src"
            for original in sources.rglob("*.py"):
                target = project / "src" / original.relative_to(sources)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, target)
        else:
            generate_python_project(project, modules)
        config = project / ".iwiki.toml"
        config.write_text(config.read_text().replace("max_full_rebuild_seconds = 30",
                                                     "max_full_rebuild_seconds = 900"))
        count = sum(1 for _ in project.rglob("*.py"))
        stop = count // 2
        subprocess.run(["git", "add", "."], cwd=project, check=True)
        subprocess.run(["git", "-c", "user.name=fixture", "-c", "user.email=fixture@localhost",
                        "commit", "-qm", "benchmark corpus"], cwd=project, check=True)
        for rep in range(repetitions):
            versions = [("baseline", baseline), ("candidate", source)]
            if rep % 2:
                versions.reverse()
            for version, checkout in versions:
                phases = {}
                for phase in ("cold", "interrupt", "restart", "clean"):
                    cache = root / "wiki" / ".iwiki"
                    if phase != "restart" and cache.exists():
                        shutil.rmtree(cache)
                    phases[phase] = _run(project, checkout, root / "metrics.json",
                                         stop if phase == "interrupt" else 0)
                    row = phases[phase]
                    assert row["accepted_files"] == (stop if phase == "interrupt" else count)
                    rows.append({"version": version, "repetition": rep, "phase": phase, **row})
                assert phases["cold"]["revision"] == phases["clean"]["revision"]
                assert phases["restart"]["revision"] == phases["clean"]["revision"]
                cold = phases["cold"]["parser_calls"]
                restarted = phases["restart"]["parser_calls"]
                assert len(set(cold)) == len(cold) == count
                if version == "candidate":
                    assert phases["interrupt"]["peak_scratch_bytes"] > 0
                    assert sorted(restarted + phases["interrupt"]["parser_calls"]) == sorted(cold)
                else:
                    assert sorted(restarted) == sorted(cold)
    medians = {}
    for row in rows:
        key = (row["version"], row["phase"])
        group = [r for r in rows if (r["version"], r["phase"]) == key]
        medians["/".join(key)] = {
            field: statistics.median(r[field] for r in group)
            for field in ("wall_seconds", "parse_seconds", "peak_scratch_bytes", "accepted_files")
        }
        medians["/".join(key)]["parser_calls"] = statistics.median(
            len(r["parser_calls"]) for r in group)
    output.write_text(json.dumps({"files": count, "medians": medians, "runs": rows}, indent=2))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--worker"]:
        _worker(Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4]))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--baseline", type=Path, required=True)
        parser.add_argument("--output", type=Path, required=True)
        parser.add_argument("--modules", type=int,
                            help="Use generated modules instead of the baseline Python sources.")
        parser.add_argument("--repetitions", type=int, default=3)
        arguments = parser.parse_args()
        measure(arguments.baseline.resolve(), arguments.output, arguments.modules,
                arguments.repetitions)
