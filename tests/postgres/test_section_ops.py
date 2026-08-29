"""wiki_insert_section against a disposable pgvector-backed store."""
from __future__ import annotations

import pytest

from iwiki_mcp.specification_store import ResolutionAttempt
from iwiki_mcp.storage import PostgresBinding


pytestmark = pytest.mark.postgres_integration


def _markdown():
    return (
        "---\n"
        "type: concept\n"
        "title: Auth\n"
        "description: auth flow\n"
        "tags: [fixture]\n"
        "status: stable\n"
        "---\n"
        "## Overview\nsum\n"
        "## Flow\nflow body\n"
    )


def _specification_markdown():
    return (
        "---\n"
        "type: specification\n"
        "title: Move behavior\n"
        "description: observable move behavior\n"
        "tags: [fixture]\n"
        "status: developing\n"
        "---\n"
        "# Move behavior\n\n"
        "## Overview\n\nSummary.\n\n"
        "## Scenario\n\n"
        "```iwiki-gwt\n"
        'id = "move-id"\n'
        'title = "Move behavior"\n'
        "given = []\n"
        'when = { role = "command", name = "Move" }\n'
        'then = [{ role = "event", name = "Moved" }]\n'
        "code = [\n"
        '  { relation = "implements", symbol = "app.move" },\n'
        '  { relation = "verifies", symbol = "tests.test_move" }\n'
        "]\n"
        "```\n"
    )


@pytest.fixture
def postgres_section_ops(store_factory, monkeypatch):
    from iwiki_mcp import server

    store = store_factory()
    store.write_page("docs", "concept/auth", _markdown())
    binding = PostgresBinding(
        host="db.invalid",
        port=5432,
        database="fixture",
        user="fixture",
        sslmode="prefer",
        iwiki_id="wiki-a",
        read=("docs",),
        write=("docs",),
        primary="docs",
        project_dir="/not-used",
        embed_model="fixture-model",
        embed_dimensions=3,
        rerank_model="",
        password="fixture-secret",
    )
    monkeypatch.setattr(server, "_LOCAL_POSTGRES_BINDING", None)
    monkeypatch.setattr(server.base, "resolve_binding", lambda: binding)
    monkeypatch.setattr(server, "_postgres_store_for_binding", lambda _binding: store)
    return server, store


def test_insert_section_adds_new_section_from_postgres(postgres_section_ops):
    server, store = postgres_section_ops
    revision = store.read_page("docs", "concept/auth")["revision"]

    out = server.wiki_insert_section(
        "docs", "concept/auth", "New", "new body",
        after_heading="Flow", expected_revision=revision,
    )

    assert "error" not in out
    read = store.read_page("docs", "concept/auth")
    assert "## New\n\nnew body" in read["markdown"]
    assert read["markdown"].index("## Flow") < read["markdown"].index("## New")


def test_insert_section_missing_page_returns_error_from_postgres(postgres_section_ops):
    server, _store = postgres_section_ops

    out = server.wiki_insert_section(
        "docs", "concept/nope", "New", "body", expected_revision=1
    )

    assert "not found" in out["error"]


def test_insert_section_without_expected_revision_returns_error_from_postgres(
    postgres_section_ops,
):
    server, _store = postgres_section_ops

    out = server.wiki_insert_section("docs", "concept/auth", "New", "body")

    assert "error" in out


def test_insert_section_rejects_anchor_collision_from_postgres(postgres_section_ops):
    server, store = postgres_section_ops
    revision = store.read_page("docs", "concept/auth")["revision"]

    out = server.wiki_insert_section(
        "docs", "concept/auth", "Flow", "body", expected_revision=revision
    )

    assert "error" in out
    assert "collides" in out["error"]


def test_delete_section_removes_target_section_from_postgres(postgres_section_ops):
    server, store = postgres_section_ops
    revision = store.read_page("docs", "concept/auth")["revision"]

    out = server.wiki_delete_section(
        "docs", "concept/auth", "Flow", expected_revision=revision
    )

    assert "error" not in out
    read = store.read_page("docs", "concept/auth")
    assert "## Flow" not in read["markdown"]


def test_move_section_reorders_target_from_postgres(postgres_section_ops):
    server, store = postgres_section_ops
    store.write_page(
        "docs",
        "concept/moveme",
        "---\n"
        "type: concept\n"
        "title: Move\n"
        "description: move flow\n"
        "tags: [fixture]\n"
        "status: stable\n"
        "---\n"
        "## Overview\nsum\n## Flow\nflow body\n## Notes\nkeep\n",
    )
    revision = store.read_page("docs", "concept/moveme")["revision"]

    out = server.wiki_move_section(
        "docs", "concept/moveme", "Notes", before_heading="Overview",
        expected_revision=revision,
    )

    assert "error" not in out
    read = store.read_page("docs", "concept/moveme")
    assert read["markdown"].index("## Notes") < read["markdown"].index("## Overview")


def test_update_page_section_hash_mismatch_returns_conflict_from_postgres(
    postgres_section_ops,
):
    server, store = postgres_section_ops
    revision = store.read_page("docs", "concept/auth")["revision"]

    out = server.wiki_update_page(
        "docs", "concept/auth", "Flow", "new body",
        expected_revision=revision, expected_section_hash="0000000000000000",
    )

    assert out["error"] == "section_conflict"
    assert "current_section_hash" in out
    read = store.read_page("docs", "concept/auth")
    assert "flow body" in read["markdown"]


def test_move_specification_section_preserves_identity_and_evidence(
    postgres_section_ops,
):
    server, store = postgres_section_ops
    store.specification_mode = "strict"
    store.write_page(
        "docs", "specification/move", _specification_markdown()
    )
    before = store.specification_context("docs", "move-id")
    assert before is not None
    binding = before.bindings[0]
    attempt = ResolutionAttempt(
        binding_id=binding.binding_id,
        domain="docs",
        scenario_id="move-id",
        state="unresolved",
        targets=(),
        unresolved_reference=binding.selector,
        graph_revision="graph-1",
        graph_state_fingerprint="sha256:" + "1" * 64,
        specification_source_hash=before.scenario.source_hash,
        checked_at="2026-08-29T12:00:00Z",
        reason=None,
    )
    store.record_specification_resolution(attempt)

    result = server.wiki_move_section(
        "docs",
        "specification/move",
        "Scenario",
        before_heading="Overview",
        expected_revision=1,
    )

    assert "error" not in result
    after = store.specification_context("docs", "move-id")
    assert after is not None
    assert after.scenario.scenario_id == before.scenario.scenario_id
    assert after.bindings == before.bindings
    assert after.evidence == (attempt,)
