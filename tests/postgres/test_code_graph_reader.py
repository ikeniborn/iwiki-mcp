"""Bounded PostgreSQL reads over the active code-graph snapshot."""
from __future__ import annotations

import pytest

from iwiki_mcp.codegraph.query import MATCH_RANK


pytestmark = pytest.mark.postgres_integration


EXPECTED_MATCHES = [
    "qualified_exact",
    "local_exact",
    "alias_exact",
    "canonical_prefix",
    "alias_prefix",
    "canonical_lexical",
    "alias_lexical",
    "signature",
    "path",
]


def test_declared_ranks_stay_the_single_authoritative_rank_table():
    assert list(MATCH_RANK) == EXPECTED_MATCHES


@pytest.mark.parametrize("call", ["status", "search", "context"])
def test_missing_snapshot_answers_every_read_without_graph_rows(
    pg_ranked_graph, call
):
    reader = pg_ranked_graph.reader()
    requests = {
        "status": (),
        "search": (pg_ranked_graph.search_request,),
        "context": (pg_ranked_graph.context_request(),),
    }

    result = getattr(reader, call)(*requests[call])

    assert result["state"] == "missing"
    assert result["fresh"] is False
    assert result["error"] == "missing_snapshot"
    assert result.get("results", []) == []
    assert result.get("nodes", []) == []
    assert result.get("relations", []) == []


def test_staging_snapshot_stays_invisible_to_readers(pg_ranked_graph):
    pg_ranked_graph.complete_session()

    assert pg_ranked_graph.reader().status()["state"] == "missing"
    assert pg_ranked_graph.reader().search(
        pg_ranked_graph.search_request
    )["results"] == []


def test_ready_status_reports_stored_revisions_and_declared_counts(
    pg_ready_graph
):
    status = pg_ready_graph.reader().status()

    assert status["state"] == "ready"
    assert status["fresh"] is True
    assert status["domain"] == pg_ready_graph.domain
    assert status["snapshot_revision"].startswith("sha256:")
    assert status["revision"] == status["snapshot_revision"]
    assert status["graph_payload_revision"] == (
        pg_ready_graph.header.graph_payload_revision
    )
    assert status["markdown_revision"].startswith("sha256:")
    assert status["stored_markdown_revision"] == status["markdown_revision"]
    assert status["stored_markdown_generation"] == status[
        "current_markdown_generation"
    ]
    assert status["wiki_links_stale"] is False
    assert status["counts"] == pg_ready_graph.expected_counts
    assert status["max_snapshot_age_seconds"] == 0
    assert status["age_seconds"] >= 0
    assert status["warnings"] == []


def test_specification_snapshot_uses_one_active_ready_postgres_revision(
    pg_ready_graph,
):
    reader = pg_ready_graph.reader()

    snapshot = reader.specification_snapshot()

    assert snapshot is not None
    assert snapshot.revision == reader.status()["snapshot_revision"]
    assert tuple(row["file_id"] for row in snapshot.files) == tuple(sorted(
        row["file_id"] for row in pg_ready_graph.rows["files"]
    ))
    assert tuple(row["symbol_id"] for row in snapshot.symbols) == tuple(sorted(
        row["symbol_id"] for row in pg_ready_graph.rows["symbols"]
    ))


def test_postgres_reader_rejects_age_but_zero_disables_rejection(
    pg_ready_graph
):
    stale = pg_ready_graph.reader(
        max_snapshot_age_seconds=1, now=pg_ready_graph.indexed_at_plus(2)
    )
    result = stale.search(pg_ready_graph.search_request)
    assert result["state"] == "ready"
    assert result["fresh"] is False
    assert result["error"] == "stale_snapshot"
    assert result["results"] == []

    allowed = pg_ready_graph.reader(
        max_snapshot_age_seconds=0, now=pg_ready_graph.indexed_at_plus(2)
    )
    assert allowed.search(pg_ready_graph.search_request)["fresh"] is True


def test_search_returns_every_declared_rank_once(pg_ready_graph):
    results = pg_ready_graph.reader().search(
        pg_ready_graph.search_request
    )["results"]

    assert [item["match"] for item in results] == EXPECTED_MATCHES
    assert {item["entity_type"] for item in results} == {
        "file", "module", "symbol"
    }
    assert all(item["entity_id"].startswith("py:") for item in results)


def test_sqlite_and_postgres_return_identical_ranked_results(
    ranked_graph_pair
):
    sqlite_results = ranked_graph_pair.sqlite.search(ranked_graph_pair.request)
    postgres_results = ranked_graph_pair.postgres.search(
        ranked_graph_pair.request
    )

    assert postgres_results == sqlite_results
    assert [item["match"] for item in postgres_results["results"]] == (
        EXPECTED_MATCHES
    )


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"limit": 4}, EXPECTED_MATCHES[:4]),
        ({"kinds": ["file"]}, ["qualified_exact", "path"]),
        ({"path": "typed/prefix"}, ["canonical_prefix"]),
    ],
)
def test_search_filters_match_the_sqlite_reader(
    ranked_graph_pair, overrides, expected
):
    from iwiki_mcp.codegraph.query import validate_search_request

    request = validate_search_request("needle", **{"limit": 20, **overrides})

    postgres_results = ranked_graph_pair.postgres.search(request)
    assert postgres_results == ranked_graph_pair.sqlite.search(request)
    assert [item["match"] for item in postgres_results["results"]] == expected


def test_context_traverses_bounded_relations_from_typed_seeds(pg_ready_graph):
    result = pg_ready_graph.reader().context(pg_ready_graph.context_request())

    assert result["state"] == "ready"
    assert [item["relation_type"] for item in result["relations"]] == (
        ["IMPORTS"] * 3
    )
    assert len(result["nodes"]) == 4
    assert result["truncated"] is False
    assert result["limits"]["depth"] == 1


def test_context_budgets_truncate_and_report_exhaustion(pg_ready_graph):
    result = pg_ready_graph.reader().context(
        pg_ready_graph.context_request(max_nodes=2)
    )

    assert result["truncated"] is True
    assert "max_nodes_exhausted" in result["warnings"]
    assert len(result["nodes"]) <= 2


def test_postgres_context_never_returns_source(pg_ready_graph):
    result = pg_ready_graph.reader().context(
        pg_ready_graph.context_request(include_source=True)
    )

    assert result["source_unavailable"] is True
    assert all("source" not in item for item in result["files"])
    assert "source_unavailable" in result["warnings"]


def test_stale_wiki_links_suppress_context_pages_and_flag_status(
    pg_ready_graph
):
    pg_ready_graph.write_markdown_page(
        "guide", "# Guide\n\n## Body\ntext\n"
    )

    status = pg_ready_graph.reader().status()
    assert status["wiki_links_stale"] is True
    assert status["stored_markdown_generation"] != status[
        "current_markdown_generation"
    ]

    result = pg_ready_graph.reader().context(
        pg_ready_graph.context_request(include_wiki=True)
    )
    assert result["wiki_pages"] == []
    assert "wiki_links_stale" in result["warnings"]


def test_reads_stay_inside_the_bound_domain(pg_ready_graph):
    other = pg_ready_graph.for_domain("private")

    assert other.reader().status()["state"] == "missing"
    assert other.reader().search(
        pg_ready_graph.search_request
    )["results"] == []
