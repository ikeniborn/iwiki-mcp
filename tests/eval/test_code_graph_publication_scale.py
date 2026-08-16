"""Generated-corpus publication scale and operator documentation evidence."""
from __future__ import annotations

from pathlib import Path

import pytest

from eval.code_graph.runner import measure_publication
from tests.codegraph.publication_contract_support import (
    generate_python_project,
)


README = Path("README.md")
README_RU = Path("docs/README.ru.md")


class _SqliteTarget:
    """Publish one generated corpus through the local SQLite target."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._runs = 0

    def __repr__(self) -> str:
        return "<sqlite publication scale target>"

    def index(self, project: Path, *, max_total_files: int = 100, **bounds):
        self._runs += 1
        return measure_publication(
            self._root / f"run-{self._runs}",
            project,
            target_mode="sqlite",
            max_total_files=max_total_files,
            **bounds,
        )


@pytest.fixture
def publication_target(tmp_path):
    return _SqliteTarget(tmp_path / "targets")


@pytest.mark.slow
def test_publication_supports_twenty_thousand_files(
    tmp_path, publication_target
):
    # 19,999 generated modules plus the package __init__ make 20,000 files.
    project = generate_python_project(tmp_path / "project", 19_999)

    result = publication_target.index(
        project.parent, max_total_files=20_000, max_rebuild_seconds=7_200
    )

    assert result["state"] == "ready"
    assert result["counts"]["files"] == 20_000
    assert result["publication_seconds"] > 0
    assert result["peak_python_heap_bytes"] > 0
    assert result["max_batch_rows_observed"] <= 1000
    assert result["max_batch_bytes_observed"] <= 1_000_000


def test_small_publication_report_is_mode_aware(tmp_path, publication_target):
    project = generate_python_project(tmp_path / "project", 2)

    result = publication_target.index(project.parent, max_total_files=8)

    assert result["target_mode"] == "sqlite"
    assert result["state"] == "ready"
    assert result["counts"]["files"] == 3
    assert result["batch_count"] > 0
    assert result["batch_bytes"] > 0
    assert result["generated_files"] == 8
    assert result["active_revision"].startswith("sha256:")


def test_publication_report_rejects_an_unknown_target_mode(tmp_path):
    with pytest.raises(ValueError):
        measure_publication(
            tmp_path / "root",
            tmp_path / "project",
            target_mode="carrier-pigeon",
            max_total_files=1,
        )


@pytest.mark.parametrize("document", [README, README_RU])
@pytest.mark.parametrize(
    "expected",
    [
        'publish_mode = "sqlite"',
        'read_mode = "sqlite"',
        "max_snapshot_age_seconds",
        "IWIKI_CODE_GRAPH_MCP_URL",
        "IWIKI_CODE_GRAPH_MCP_TOKEN",
        "IWIKI_DB_PASSWORD",
        "iwiki-mcp principal grant",
        "--hosted-principal",
        "schema rollback-v5-compat",
        "compat/postgres-v4-runtime-guard.json",
        "commit_uncertain",
        "source_unavailable",
        "code_graph_publication",
    ],
)
def test_operator_documentation_covers_the_deployment_contract(
    document, expected
):
    assert expected in document.read_text(encoding="utf-8")


@pytest.mark.parametrize("document", [README, README_RU])
def test_operator_documentation_states_no_silent_fallback(document):
    text = document.read_text(encoding="utf-8").lower()

    assert "fallback" in text
    assert "0 disables" in text or "0 отключает" in text
