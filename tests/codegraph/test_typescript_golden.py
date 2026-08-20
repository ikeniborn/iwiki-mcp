"""TypeScript adapter output must match the pre-refactor baseline byte for byte.

A failure here means the refactor changed TypeScript behaviour, which the
intent forbids. Fix the code. Regenerating the baseline is a stop-rule
violation (see the plan's HUMAN CHECKPOINT).
"""
import dataclasses
import json
from pathlib import Path

from iwiki_mcp.codegraph.languages.typescript import TypeScriptAdapter
from iwiki_mcp.codegraph.resolver import SymbolIndex

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "codegraph" / "typescript_golden"
GOLDEN_PATH = Path(__file__).resolve().parent / "fixtures" / "typescript_golden.json"
SOURCES = ("walker.ts", "shapes.ts", "view.tsx")


def _capture():
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


def test_typescript_output_matches_golden_baseline():
    assert _capture() == json.loads(GOLDEN_PATH.read_text())
