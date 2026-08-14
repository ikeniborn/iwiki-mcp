"""PostgreSQL page-store contract against a disposable pgvector database."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from iwiki_mcp.engine.config import Config


pytestmark = pytest.mark.postgres_integration


def _cfg():
    return Config(
        base_url="http://example.invalid/v1",
        api_key="test",
        embed_model="test-embedding",
        dimensions=3,
        chunk_size=512,
        chunk_overlap=64,
        summary_max=400,
        top_k=8,
        score_threshold=0.0,
        graph_depth=2,
        ignore=None,
        seed_top_k=2,
        bfs_top_k=10,
        seed_threshold=0.0,
    )


def _embed(_cfg, texts):
    vectors = []
    for text in texts:
        lowered = text.lower()
        if "alpha" in lowered:
            vectors.append([1.0, 0.0, 0.0])
        elif "beta" in lowered:
            vectors.append([0.0, 1.0, 0.0])
        else:
            vectors.append([0.0, 0.0, 1.0])
    return vectors


def _markdown(title, description, heading, body, link=None):
    suffix = f"\n\n[Linked]({link}.md)" if link else ""
    return (
        "---\n"
        "type: concept\n"
        f"title: {title}\n"
        f"description: {description}\n"
        "tags: [fixture]\n"
        "status: stable\n"
        "---\n"
        f"# {title}\n\n"
        f"## {heading}\n{body}{suffix}\n"
    )


@pytest.fixture
def store_factory(clean_postgres):
    from iwiki_mcp.postgres.migrations import MigrationSettings, run_migrations
    from iwiki_mcp.postgres.store import PostgresStore

    cfg = _cfg()
    run_migrations(
        MigrationSettings(
            dsn=clean_postgres,
            embed_model=cfg.embed_model,
            embed_dimensions=cfg.dimensions,
            statement_timeout_ms=30_000,
            lock_timeout_ms=5_000,
        )
    )

    def factory(iwiki_id="wiki-a", *, embedder=_embed):
        store = PostgresStore(
            clean_postgres,
            iwiki_id,
            cfg,
            embedder=embedder,
        )
        store.create_wiki(iwiki_id)
        store.create_domain("docs")
        return store

    return factory


def test_create_list_and_read_page_return_numeric_revision(store_factory):
    store = store_factory()
    markdown = _markdown("Alpha", "alpha summary", "Details", "alpha body")

    created = store.write_page("docs", "concept/alpha", markdown)

    assert created == {
        "page": "docs/concept/alpha.md",
        "revision": 1,
        "indexed_chunks": 2,
    }
    assert store.list_domains() == ["docs"]
    assert store.list_pages("docs") == ["concept/alpha"]
    assert store.read_page("docs", "concept/alpha") == {
        "domain": "docs",
        "slug": "concept/alpha",
        "markdown": markdown,
        "revision": 1,
    }
    assert store.write_page("docs", "concept/alpha", markdown) == {
        "error": "page_exists",
        "hint": "read the page before updating it",
    }


def test_update_requires_revision_conflicts_and_preserves_old_state_on_failure(
    store_factory, monkeypatch,
):
    store = store_factory()
    original = _markdown("Alpha", "alpha summary", "Details", "alpha body")
    changed = _markdown("Alpha", "beta summary", "Details", "beta body")
    store.write_page("docs", "concept/alpha", original)

    assert store.update_page("docs", "concept/alpha", changed, None) == {
        "error": "expected_revision_required",
        "hint": "read the page and retry with its revision",
    }
    updated = store.update_page("docs", "concept/alpha", changed, 1)
    assert updated["revision"] == 2
    assert store.update_page("docs", "concept/alpha", original, 1) == {
        "error": "conflict",
        "current_revision": 2,
        "hint": "read the page and retry against the current revision",
    }

    def failing_embedder(_cfg, _texts):
        raise RuntimeError("fixture embedding failure")

    failing = store.with_embedder(failing_embedder)
    with pytest.raises(RuntimeError, match="fixture embedding failure"):
        failing.update_page("docs", "concept/alpha", original, 2)

    assert store.read_page("docs", "concept/alpha")["markdown"] == changed
    assert store.read_page("docs", "concept/alpha")["revision"] == 2

    def fail_derived(*_args, **_kwargs):
        raise RuntimeError("fixture derived-data failure")

    transaction_failing = store.with_embedder(_embed)
    monkeypatch.setattr(transaction_failing, "_replace_derived", fail_derived)
    with pytest.raises(RuntimeError, match="fixture derived-data failure"):
        transaction_failing.update_page(
            "docs", "concept/alpha", original, 2
        )

    assert store.read_page("docs", "concept/alpha")["markdown"] == changed
    assert store.read_page("docs", "concept/alpha")["revision"] == 2


def test_two_clients_with_same_revision_produce_one_success_and_one_conflict(
    store_factory,
):
    store = store_factory()
    store.write_page(
        "docs",
        "concept/alpha",
        _markdown("Alpha", "alpha summary", "Details", "alpha body"),
    )
    first = _markdown("Alpha", "beta first", "Details", "beta first")
    second = _markdown("Alpha", "gamma second", "Details", "gamma second")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda markdown: store.update_page(
                    "docs", "concept/alpha", markdown, 1
                ),
                (first, second),
            )
        )

    assert sorted(result.get("error", "success") for result in results) == [
        "conflict",
        "success",
    ]
    assert store.read_page("docs", "concept/alpha")["revision"] == 2


def test_delete_requires_current_revision(store_factory):
    store = store_factory()
    store.write_page(
        "docs",
        "concept/alpha",
        _markdown("Alpha", "alpha summary", "Details", "alpha body"),
    )

    assert store.delete_page("docs", "concept/alpha", None)["error"] == (
        "expected_revision_required"
    )
    assert store.delete_page("docs", "concept/alpha", 99) == {
        "error": "conflict",
        "current_revision": 1,
        "hint": "read the page and retry against the current revision",
    }
    assert store.delete_page("docs", "concept/alpha", 1) == {
        "page": "docs/concept/alpha.md",
        "deleted": True,
    }
    assert store.read_page("docs", "concept/alpha") is None


def test_wrong_dimension_and_invalid_markdown_do_not_mutate(store_factory):
    store = store_factory()
    original = _markdown("Alpha", "alpha summary", "Details", "alpha body")
    store.write_page("docs", "concept/alpha", original)

    wrong_dimension = store.with_embedder(
        lambda _cfg, texts: [[1.0, 0.0] for _text in texts]
    )
    with pytest.raises(ValueError, match="embedding dimension mismatch"):
        wrong_dimension.update_page("docs", "concept/alpha", original, 1)

    with pytest.raises(ValueError, match="section structure invalid"):
        store.update_page("docs", "concept/alpha", "text before a section", 1)

    assert store.read_page("docs", "concept/alpha")["revision"] == 1


def test_search_and_graph_are_domain_and_wiki_scoped(store_factory):
    first = store_factory("wiki-a")
    first.create_domain("private")
    second = store_factory("wiki-b")
    first.write_page(
        "docs",
        "concept/target",
        _markdown("Target", "beta target", "Target", "beta details"),
    )
    first.write_page(
        "docs",
        "concept/seed",
        _markdown(
            "Seed", "alpha seed", "Match", "alpha exact_token", "concept/target"
        ),
    )
    first.write_page(
        "private",
        "concept/hidden",
        _markdown("Hidden", "alpha hidden", "Hidden", "alpha exact_token"),
    )
    second.write_page(
        "docs",
        "concept/seed",
        _markdown("Other", "alpha other", "Other", "alpha exact_token"),
    )

    results = first.search(
        ["docs"], "alpha exact_token", top_k=8, threshold=0.0, mode="hybrid"
    )
    identities = [(item["domain"], item["file"]) for item in results]

    assert identities
    assert set(identities) <= {
        ("docs", "concept/seed.md"),
        ("docs", "concept/target.md"),
    }
    assert identities[0] == ("docs", "concept/seed.md")
    assert all(item["heading"] != "Other" for item in results)
    hydrated = first.hydrate_candidates(results)
    assert len(hydrated) == len(results)
    assert all(item["text"].startswith("## ") for item in hydrated)
    assert first.graph_neighbors(["docs"], "docs/concept/seed.md", depth=1) == [
        "docs/concept/target.md"
    ]
    related = first.related("docs", "concept/seed.md#Match")
    assert related["vector"]
    assert related["vector"][0]["id"] == "concept/target.md#Target"


def test_composite_foreign_key_rejects_cross_wiki_link(
    store_factory, clean_postgres
):
    import psycopg

    first = store_factory("wiki-a")
    second = store_factory("wiki-b")
    first.write_page(
        "docs",
        "concept/source",
        _markdown("Source", "alpha", "Source", "alpha"),
    )
    second.write_page(
        "docs",
        "concept/target",
        _markdown("Target", "beta", "Target", "beta"),
    )

    with psycopg.connect(clean_postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT p.iwiki_id, p.page_id FROM iwiki.pages p "
                "JOIN iwiki.domains d ON d.iwiki_id = p.iwiki_id "
                "AND d.domain_id = p.domain_id "
                "WHERE (p.iwiki_id = 'wiki-a' AND p.slug = 'concept/source') "
                "OR (p.iwiki_id = 'wiki-b' AND p.slug = 'concept/target')"
            )
            pages = dict(cursor.fetchall())
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                cursor.execute(
                    "INSERT INTO iwiki.links ("
                    "iwiki_id, source_page_id, target_page_id, "
                    "target_domain, target_slug"
                    ") VALUES ('wiki-a', %s, %s, 'docs', 'concept/target')",
                    (pages["wiki-a"], pages["wiki-b"]),
                )


def test_link_written_before_target_resolves_when_target_appears(store_factory):
    store = store_factory()
    store.write_page(
        "docs",
        "concept/source",
        _markdown(
            "Source", "alpha source", "Source", "alpha", "concept/target"
        ),
    )
    assert store.graph_neighbors(
        ["docs"], "docs/concept/source.md", depth=1
    ) == []

    store.write_page(
        "docs",
        "concept/target",
        _markdown("Target", "beta target", "Target", "beta"),
    )

    assert store.graph_neighbors(
        ["docs"], "docs/concept/source.md", depth=1
    ) == ["docs/concept/target.md"]


def test_target_delete_and_recreate_preserves_authored_source_link(store_factory):
    store = store_factory()
    store.write_page(
        "docs",
        "concept/target",
        _markdown("Target", "beta target", "Target", "beta"),
    )
    store.write_page(
        "docs",
        "concept/source",
        _markdown(
            "Source", "alpha source", "Source", "alpha", "concept/target"
        ),
    )

    store.delete_page("docs", "concept/target", 1)
    store.write_page(
        "docs",
        "concept/target",
        _markdown("Target", "beta target", "Target", "beta restored"),
    )

    assert store.graph_neighbors(
        ["docs"], "docs/concept/source.md", depth=1
    ) == ["docs/concept/target.md"]


def test_git_and_postgres_fixture_have_same_order_and_graph_neighbors(
    store_factory, tmp_path, monkeypatch
):
    from iwiki_mcp import indexer, retrieval
    from iwiki_mcp.engine.hier import rank_graph_pages

    cfg = _cfg()
    pages = {
        "concept/target.md": _markdown(
            "Target", "beta target", "Target", "beta details"
        ),
        "concept/seed.md": _markdown(
            "Seed",
            "alpha seed",
            "Match",
            "alpha exact_token exact_token",
            "concept/target",
        ),
    }

    git_base = tmp_path / "wiki"
    git_domain = git_base / "docs"
    for file, markdown in pages.items():
        path = git_domain / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
    monkeypatch.setattr(indexer, "embed_texts", _embed)
    monkeypatch.setattr(retrieval, "embed_texts", _embed)
    indexer.index_domain(cfg, str(git_base), "docs")

    postgres = store_factory()
    for file, markdown in pages.items():
        postgres.write_page("docs", file.removesuffix(".md"), markdown)

    query = "alpha exact_token"
    git_results = retrieval.search_read(
        cfg, str(git_base), ["docs"], query, 8, 0.0, "hybrid"
    )
    postgres_results = postgres.search(
        ["docs"], query, top_k=8, threshold=0.0, mode="hybrid"
    )

    def normalize(rows):
        return [
            (row["domain"], row["file"], row["heading"], row["chunk"])
            for row in rows
        ]

    assert normalize(postgres_results) == normalize(git_results)
    git_graph = rank_graph_pages(
        [("concept/seed.md", "graph", 0)], str(git_domain), depth=1, cap=10
    )
    git_neighbors = [
        f"docs/{row['file']}"
        for row in git_graph
        if row["file"] != "concept/seed.md"
    ]
    assert postgres.graph_neighbors(
        ["docs"], "docs/concept/seed.md", depth=1
    ) == git_neighbors
