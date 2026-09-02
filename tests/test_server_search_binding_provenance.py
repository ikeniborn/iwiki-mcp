"""`wiki_search` answers name the tier that chose the scope they searched."""
from __future__ import annotations

import pytest

from iwiki_mcp import base, indexer, retrieval, server


def _seed(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    (wiki / "backend").mkdir(parents=True)
    (wiki / "backend" / "auth.md").write_text(
        "---\ndescription: auth token guide\n---\n"
        "# Auth\n## Overview\no\n## Token\nrefresh_token rotates\n"
    )
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".iwiki.toml").write_text(
        'read = ["backend"]\nwrite = ["backend"]\nprimary = "backend"\n'
    )
    monkeypatch.setenv("IWIKI_BASE_DIR", str(wiki))
    monkeypatch.setenv("IWIKI_PROJECT_DIR", str(project))
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", "2")
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, t: [[1.0, 0.0] for _ in t])
    monkeypatch.setattr(retrieval, "embed_texts", lambda cfg, t: [[1.0, 0.0] for _ in t])
    from iwiki_mcp.engine.config import Config

    indexer.index_domain(Config.load(), str(wiki), "backend")
    # The session state below only supplies provenance: resolution stays on the
    # seeded Git binding so no PostgreSQL connection is attempted.
    monkeypatch.setattr(server, "_resolved_binding", base.resolve_binding)


@pytest.fixture
def hosted_session(tmp_path, monkeypatch):
    """Install one hosted binding state so answers carry provenance."""
    _seed(tmp_path, monkeypatch)
    tokens = []

    def install(source):
        binding = base.PostgresBinding(
            host="127.0.0.1",
            port=5432,
            database="iwiki_test",
            user="iwiki",
            sslmode="prefer",
            iwiki_id="wiki-a",
            read=("backend",),
            write=("backend",),
            primary="backend",
            project_dir=str(tmp_path),
            embed_model="fixture-model",
            embed_dimensions=2,
            rerank_model="",
            password="secret",
        )
        selected = server._HostedSelectedState(binding, source=source)
        state = server._HostedBindingState(selected, selected.get())
        state.bind_session("session-a")
        tokens.append(server._SESSION_BINDING.set(state))
        return state

    try:
        yield install
    finally:
        for token in reversed(tokens):
            server._SESSION_BINDING.reset(token)


def test_search_without_domains_names_a_defaulted_scope(hosted_session):
    """Omitting `domains` hands the search set to the binding.

    A lapsed selection therefore searches every domain the token may read
    instead of the project's, so the fallback is named in `warnings`.
    """
    hosted_session("token_default")

    answer = server.wiki_search("token", threshold=0.0)

    assert answer["results"]
    assert answer["binding_source"] == "token_default"
    assert "binding_defaulted" in answer["warnings"]


def test_search_with_explicit_domains_is_never_defaulted(hosted_session):
    """A caller that named its domains chose the scope itself."""
    hosted_session("token_default")

    answer = server.wiki_search("token", domains=["backend"], threshold=0.0)

    assert answer["results"]
    assert answer["binding_source"] == "token_default"
    assert "warnings" not in answer


def test_search_under_a_selected_binding_carries_no_warning(hosted_session):
    hosted_session("session")

    answer = server.wiki_search("token", threshold=0.0)

    assert answer["binding_source"] == "session"
    assert "warnings" not in answer


def test_write_intent_is_defaulted_even_with_explicit_domains(hosted_session):
    """`intent="write"` prefers `binding.primary` over any named domain."""
    hosted_session("token_default")

    answer = server.wiki_search(
        "token", intent="write", heading="Token", domains=["backend"]
    )

    assert answer["target"]["exists"] is True
    assert answer["binding_source"] == "token_default"
    assert "binding_defaulted" in answer["warnings"]


def test_empty_scope_answer_reports_the_same_provenance(hosted_session):
    """The short-circuit answer carries provenance like a full search.

    An empty `domains` list is still a scope the caller named, so the answer
    reports the tier without claiming the binding chose it.
    """
    hosted_session("token_default")

    answer = server.wiki_search("token", domains=[], threshold=0.0)

    assert answer["results"] == []
    assert answer["hint"] == "no domains in scope"
    assert answer["binding_source"] == "token_default"
    assert "warnings" not in answer


def test_reranked_answer_keeps_the_provenance(hosted_session, monkeypatch):
    """The rerank branch rebuilds the response and must not drop provenance."""
    hosted_session("token_default")
    monkeypatch.setenv("IWIKI_RERANK_MODEL", "model")
    monkeypatch.setattr(
        server.rerank,
        "rerank_candidates",
        lambda cfg, query, hydrated, top_n: (hydrated, {"applied": True}),
    )

    answer = server.wiki_search("token", threshold=0.0)

    assert answer["rerank"] == {"applied": True}
    assert answer["binding_source"] == "token_default"
    assert "binding_defaulted" in answer["warnings"]


def test_local_search_carries_no_provenance(tmp_path, monkeypatch):
    """Stdio and local PostgreSQL have no session tier to report."""
    _seed(tmp_path, monkeypatch)

    answer = server.wiki_search("token", threshold=0.0)

    assert answer["results"]
    assert "binding_source" not in answer
    assert "warnings" not in answer
