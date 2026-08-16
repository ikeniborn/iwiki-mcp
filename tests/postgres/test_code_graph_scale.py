"""PostgreSQL publication scale within the configured server ceilings."""
from __future__ import annotations

import pytest

from eval.code_graph.runner import measure_publication
from tests.codegraph.publication_contract_support import (
    generate_python_project,
)


pytestmark = pytest.mark.postgres_integration


class _PostgresScaleTarget:
    """Publish one generated corpus through the real PostgreSQL publisher."""

    def __init__(self, graph, root):
        self._graph = graph
        self._root = root

    def __repr__(self):
        return "<redacted PostgreSQL publication scale target>"

    def index(self, project, *, max_total_files, **bounds):
        return measure_publication(
            self._root,
            project,
            target_mode="postgres",
            max_total_files=max_total_files,
            publisher=self._graph.store,
            **bounds,
        )

    def search(self, query, *, limit=20):
        from iwiki_mcp.codegraph.query import validate_search_request

        return self._graph.reader().search(
            validate_search_request(query, limit=limit)
        )


@pytest.fixture
def postgres_scale_target(pg_ranked_graph, tmp_path):
    return _PostgresScaleTarget(pg_ranked_graph, tmp_path / "scale")


def test_postgres_publication_respects_server_ceilings(
    tmp_path, postgres_scale_target
):
    generate_python_project(tmp_path / "project", 2_000)

    result = postgres_scale_target.index(
        tmp_path / "project",
        max_total_files=2_001,
        max_batch_rows=100,
        max_batch_bytes=100_000,
    )

    assert result["state"] == "ready"
    assert result["counts"]["files"] == 2_001
    assert result["max_batch_rows_observed"] <= 100
    assert result["max_batch_bytes_observed"] <= 100_000
    assert result["publication_seconds"] > 0
    assert result["peak_python_heap_bytes"] > 0
    assert len(postgres_scale_target.search("value", limit=20)["results"]) <= 20
