"""Capture the pre-refactor run-level Python + TypeScript row baseline.

Run once, from unmodified source, in Task 2 of the JavaScript code-graph
plan. It is deliberately NOT imported by the test module: a test that can
rewrite its own baseline is not a baseline.
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from tests.codegraph.test_mixed_language_baseline import (  # noqa: E402
    BASELINE_LANGUAGES, BASELINE_PATH, baseline_rows, build_mixed_tables,
    pinned_factories,
)
from tests.codegraph.test_mixed_language_indexing import _DOMAIN  # noqa: E402

if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        tables = build_mixed_tables(
            Path(tmp) / "baseline",
            languages=BASELINE_LANGUAGES,
            factories=pinned_factories(_DOMAIN),
        )
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(json.dumps(baseline_rows(tables), indent=2, sort_keys=True) + "\n")
    print(f"wrote {BASELINE_PATH}")
