"""Capture the pre-refactor TypeScript adapter baseline.

Run once, from unmodified source, in Task 1 of the JavaScript code-graph
plan. It is deliberately NOT importable by the test module: a test that
can rewrite its own baseline is not a baseline.
"""
import dataclasses
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from iwiki_mcp.codegraph.languages.typescript import TypeScriptAdapter  # noqa: E402
from iwiki_mcp.codegraph.resolver import SymbolIndex  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "codegraph" / "typescript_golden"
GOLDEN_PATH = ROOT / "codegraph" / "fixtures" / "typescript_golden.json"
SOURCES = ("walker.ts", "shapes.ts", "view.tsx")


def capture():
    adapter = TypeScriptAdapter("golden-domain", (), parser_version="golden-parser")
    parsed = {
        name: adapter.parse_file((FIXTURES / name).read_bytes(), name)
        for name in SOURCES
    }
    index = SymbolIndex.from_parsed_files(parsed.values())
    captured = {}
    for name, item in parsed.items():
        result = adapter.resolve_references(item, index)
        captured[name] = {
            "file": dataclasses.asdict(item.file),
            "symbols": [dataclasses.asdict(symbol) for symbol in item.symbols],
            "references": [dataclasses.asdict(ref) for ref in item.references],
            "relations": [dataclasses.asdict(rel) for rel in result.relations],
            "parse_warnings": list(item.warnings),
            "resolve_warnings": list(result.warnings),
        }
    return captured


if __name__ == "__main__":
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(json.dumps(capture(), indent=2, sort_keys=True) + "\n")
    print(f"wrote {GOLDEN_PATH}")
