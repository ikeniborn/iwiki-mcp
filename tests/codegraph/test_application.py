from pathlib import Path

import pytest

from iwiki_mcp.codegraph import application
from iwiki_mcp.storage import GitBinding, PostgresBinding


def _postgres_binding(project: Path) -> PostgresBinding:
    return PostgresBinding(
        host="127.0.0.1",
        port=5432,
        database="synthetic_test",
        user="fixture",
        password="fixture-password",
        sslmode="disable",
        iwiki_id="wiki-a",
        read=("docs",),
        write=("docs",),
        primary="docs",
        project_dir=str(project),
        embed_model="fixture-model",
        embed_dimensions=3,
        rerank_model="",
    )


def test_git_source_context_keeps_the_wiki_cache_and_selector(tmp_path):
    project = tmp_path / "project"
    wiki = tmp_path / "wiki"
    project.mkdir()
    wiki.mkdir()
    binding = GitBinding(
        base=str(wiki),
        read=("docs",),
        write=("docs",),
        primary="docs",
        project_dir=str(project),
    )

    source = application.source_context(binding)

    assert source.base == str(wiki)
    assert source.project_dir == str(project)
    assert source.primary == "docs"
    assert source.wiki_base == str(wiki)


def test_postgres_source_context_uses_project_cache_and_local_exclude(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    calls = []
    monkeypatch.setattr(
        application.wiki_base,
        "ensure_graph_store_excluded",
        lambda value: calls.append(value) or True,
    )

    source = application.source_context(_postgres_binding(project))

    assert source.base == str(project)
    assert source.wiki_base is None
    assert calls == [str(project)]


def test_postgres_source_context_fails_before_cache_when_exclusion_fails(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        application.wiki_base,
        "ensure_graph_store_excluded",
        lambda _value: False,
    )

    with pytest.raises(application.CodeGraphApplicationError) as failure:
        application.source_context(_postgres_binding(project))

    assert failure.value.code == "invalid_config"
    assert not (project / ".iwiki").exists()
