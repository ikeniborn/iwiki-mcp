"""wiki_read_page(heading=...) against a disposable pgvector-backed store."""
from __future__ import annotations

import pytest

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
        "## Notes\nkeep\n"
    )


@pytest.fixture
def postgres_read_page(store_factory, monkeypatch):
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
    return server


def test_read_page_with_heading_returns_section_from_postgres(postgres_read_page):
    out = postgres_read_page.wiki_read_page("docs", "concept/auth", heading="Flow")

    assert out["heading"] == "Flow"
    assert out["body"] == "flow body"
    assert "section_hash" in out
    assert "markdown" not in out


def test_read_page_with_missing_heading_returns_error_from_postgres(postgres_read_page):
    out = postgres_read_page.wiki_read_page("docs", "concept/auth", heading="Nope")

    assert "error" in out
    assert "not found" in out["error"]


def test_read_page_without_heading_is_unchanged_from_postgres(postgres_read_page):
    out = postgres_read_page.wiki_read_page("docs", "concept/auth")

    assert set(out) == {"domain", "slug", "markdown", "revision"}
