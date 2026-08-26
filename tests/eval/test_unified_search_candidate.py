import pytest

from eval.unified_search.candidate import compose_unified_search
from iwiki_mcp import server


def _code(*, state="ready", fresh=True, revision="r1", results=None, **extra):
    value = {"state": state, "fresh": fresh, "revision": revision,
             "results": [] if results is None else results}
    value.update(extra)
    return value


def _context(*, revision="r1", fresh=True, wiki_pages=None, **extra):
    value = {"fresh": fresh, "revision": revision, "seeds": ["a"],
             "nodes": [{"entity_id": "a"}], "relations": [], "files": [],
             "wiki_pages": [] if wiki_pages is None else wiki_pages,
             "limits": {"depth": 1}, "truncated": False, "warnings": []}
    value.update(extra)
    return value


def test_candidate_uses_three_unique_ranked_seeds_and_separates_wiki_pages():
    calls = []
    result = compose_unified_search(
        wiki_call=lambda: {"results": [{"slug": "design"}]},
        code_call=lambda: _code(results=[
            {"entity_id": "a"}, {"entity_id": "a"}, {"entity_id": "b"},
            {"entity_id": "c"}, {"entity_id": "d"},
        ]),
        context_call=lambda seeds: calls.append(seeds) or _context(
            wiki_pages=[{"slug": "design"}],
        ),
    )
    assert calls == [["a", "b", "c"]]
    assert result["associations"] == [{"slug": "design"}]
    assert "wiki_pages" not in result["context"]
    assert set(result) == {"wiki", "code", "associations", "context", "degradation"}


def test_candidate_discards_context_from_another_revision():
    result = compose_unified_search(
        wiki_call=lambda: {"results": [{"slug": "design"}]},
        code_call=lambda: _code(results=[{"entity_id": "a"}]),
        context_call=lambda seeds: _context(revision="r2", wiki_pages=[{"slug": "x"}]),
    )
    assert result["wiki"]["results"]
    assert result["code"]["results"]
    assert result["context"]["nodes"] == []
    assert result["context"]["seeds"] == []
    assert result["associations"] == []
    assert result["degradation"]["context"]["reason"] == "revision_changed"


def test_candidate_preserves_sanitized_context_failure_metadata():
    result = compose_unified_search(
        wiki_call=lambda: {"results": [{"slug": "design"}]},
        code_call=lambda: _code(results=[{"entity_id": "a"}]),
        context_call=lambda seeds: {
            "fresh": False,
            "revision": "r1",
            "code": "busy",
            "error": {"code": "busy"},
            "hint": "retry later",
            "warnings": ["snapshot_busy"],
            "limits": {"depth": 1},
            "nodes": [{"entity_id": "a"}],
            "relations": [{"source": "a", "target": "b"}],
            "files": [{"file": "src/a.py"}],
            "wiki_pages": [{"slug": "must-not-leak"}],
        },
    )
    assert result["wiki"]["results"] == [{"slug": "design"}]
    assert result["code"]["results"] == [{"entity_id": "a"}]
    assert result["context"]["code"] == "busy"
    assert result["context"]["error"] == {"code": "busy"}
    assert result["context"]["hint"] == "retry later"
    assert result["context"]["warnings"] == ["snapshot_busy"]
    assert result["context"]["limits"] == {"depth": 1}
    assert result["context"]["seeds"] == []
    assert result["context"]["nodes"] == []
    assert result["context"]["relations"] == []
    assert result["context"]["files"] == []
    assert "wiki_pages" not in result["context"]
    assert result["associations"] == []
    assert result["degradation"]["context"] == {"degraded": True, "reason": "busy"}
    assert result["degradation"]["associations"] == {"degraded": True, "reason": "busy"}


def test_candidate_skips_context_for_zero_hits_and_empty_ids():
    calls = []
    result = compose_unified_search(
        wiki_call=lambda: {"results": []},
        code_call=lambda: _code(results=[{"entity_id": ""}, {"entity_id": None}]),
        context_call=lambda seeds: calls.append(seeds),
    )
    assert calls == []
    assert result["context"] == {}
    assert result["associations"] == []
    assert not result["degradation"]["context"]["degraded"]


@pytest.mark.parametrize("state", ["missing", "dirty", "busy", "stale", "failed"])
def test_candidate_does_not_run_context_for_nonfresh_code_states(state):
    calls = []
    result = compose_unified_search(
        wiki_call=lambda: {"results": [{"slug": "x"}]},
        code_call=lambda: _code(state=state, fresh=False, results=[{"entity_id": "a"}]),
        context_call=lambda seeds: calls.append(seeds),
    )
    assert calls == []
    assert result["context"] == {}
    assert result["associations"] == []
    assert result["degradation"]["context"] == {"degraded": True, "reason": "not_run"}
    assert result["degradation"]["associations"] == {"degraded": True, "reason": "not_run"}


def test_candidate_sanitizes_independent_branch_exceptions():
    def fail():
        raise RuntimeError("secret DSN and private path")

    result = compose_unified_search(
        wiki_call=fail,
        code_call=lambda: _code(results=[{"entity_id": "a"}]),
        context_call=fail,
    )
    assert result["wiki"]["results"] == []
    assert result["code"]["results"]
    assert result["context"] == {}
    assert result["degradation"]["wiki"]["degraded"]
    assert result["degradation"]["wiki"]["reason"] == "failed"
    assert result["degradation"]["context"]["reason"] == "failed"
    assert "secret" not in str(result)


def test_candidate_marks_code_exception_failed_without_leaking_exception_text():
    def fail_code():
        raise RuntimeError("private database password")

    result = compose_unified_search(
        wiki_call=lambda: {"results": [{"slug": "survives"}]},
        code_call=fail_code,
        context_call=lambda seeds: pytest.fail("context must not run"),
    )
    assert result["wiki"]["results"] == [{"slug": "survives"}]
    assert result["degradation"]["code"] == {"degraded": True, "reason": "failed"}
    assert result["degradation"]["context"] == {"degraded": True, "reason": "not_run"}
    assert result["degradation"]["associations"] == {"degraded": True, "reason": "not_run"}
    assert "password" not in str(result)


def test_candidate_preserves_context_limits_warnings_and_truncation():
    result = compose_unified_search(
        wiki_call=lambda: {"results": []},
        code_call=lambda: _code(results=[{"entity_id": "a"}]),
        context_call=lambda seeds: _context(
            wiki_pages=[{"slug": "x"}], truncated=True,
            limits={"depth": 1, "nodes": 3}, warnings=["node_limit"],
        ),
    )
    assert result["context"]["truncated"] is True
    assert result["context"]["limits"] == {"depth": 1, "nodes": 3}
    assert result["context"]["warnings"] == ["node_limit"]
    assert result["associations"] == [{"slug": "x"}]


def test_candidate_preserves_context_when_wiki_links_are_stale():
    result = compose_unified_search(
        wiki_call=lambda: {"results": []},
        code_call=lambda: _code(results=[{"entity_id": "a"}]),
        context_call=lambda seeds: _context(
            wiki_pages=[{"slug": "must-not-leak"}], wiki_links_stale=True,
            relations=[{"source": "a", "target": "b"}],
            files=[{"file": "src/a.py"}],
        ),
    )
    assert result["context"]["nodes"]
    assert result["context"]["relations"] == [{"source": "a", "target": "b"}]
    assert result["context"]["files"] == [{"file": "src/a.py"}]
    assert result["associations"] == []
    assert result["degradation"]["associations"] == {
        "degraded": True, "reason": "wiki_links_stale"
    }


def test_candidate_does_not_mutate_context_or_call_registry_operations():
    context = _context(wiki_pages=[{"slug": "x"}])
    result = compose_unified_search(
        wiki_call=lambda: {"results": []},
        code_call=lambda: _code(results=[{"entity_id": "a"}]),
        context_call=lambda seeds: context,
    )
    assert context["wiki_pages"] == [{"slug": "x"}]
    assert result["context"] is not context


@pytest.mark.asyncio
async def test_candidate_use_does_not_register_fastmcp_tool():
    before = {tool.name for tool in await server.mcp.list_tools()}
    compose_unified_search(
        wiki_call=lambda: {"results": []},
        code_call=lambda: _code(results=[]),
        context_call=lambda seeds: pytest.fail("context must not run"),
    )
    after = {tool.name for tool in await server.mcp.list_tools()}
    assert "wiki_unified_search" not in before
    assert after == before


def test_candidate_does_not_call_existing_mutation_or_storage_operations(monkeypatch):
    mutation_names = (
        "wiki_write_page", "wiki_update_page", "wiki_insert_section",
        "wiki_delete_section", "wiki_move_section", "wiki_delete_page",
        "wiki_create_domain", "wiki_index", "wiki_code_index",
        "wiki_code_publish_begin", "wiki_code_publish_batch",
        "wiki_code_publish_finalize", "wiki_code_publish_abort", "wiki_sync",
    )
    calls = []

    def fail_if_called(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("mutation operation called")

    for name in mutation_names:
        monkeypatch.setattr(server, name, fail_if_called)

    compose_unified_search(
        wiki_call=lambda: {"results": [{"slug": "read-only"}]},
        code_call=lambda: _code(results=[{"entity_id": "a"}]),
        context_call=lambda seeds: _context(),
    )
    assert calls == []
