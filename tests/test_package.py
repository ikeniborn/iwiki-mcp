import dataclasses
from importlib.metadata import version
from pathlib import Path

import iwiki_mcp
from iwiki_mcp.codegraph.config import CodeGraphConfig


def test_package_version_matches_distribution_metadata():
    assert iwiki_mcp.__version__ == version("iwiki-mcp")


def test_code_graph_benchmark_package_version():
    assert iwiki_mcp.__version__ == "0.7.93"


def test_user_docs_describe_python_code_graph_mvp_contract():
    text = Path("README.md").read_text(encoding="utf-8")

    assert all(
        name in text
        for name in (
            "wiki_code_status",
            "wiki_code_index",
            "wiki_code_search",
            "wiki_code_context",
        )
    )
    assert "Incremental indexing is not part of the Python MVP" in text
    assert "TypeScript is not part of the Python MVP" in text
    assert "deterministic full rebuild" in text
    assert "schema-v1" in text
    assert "uv run python -m eval.code_graph" in text
    assert "<500 ms" in text
    assert "<150 ms" in text
    assert "incremental" not in {field.name for field in dataclasses.fields(CodeGraphConfig)}
    assert not Path("src/iwiki_mcp/codegraph/languages/typescript.py").exists()
