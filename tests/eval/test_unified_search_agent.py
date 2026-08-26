import json

import pytest

from eval.unified_search import agent
from eval.unified_search.agent import run_agent_case
from eval.unified_search.fixtures import (
    CodeResponse, ContextResponse, FIXED_CASES, UnifiedSearchCase, WikiResponse,
)


def _response(*, content=None, tool_calls=None):
    return {
        "id": "chatcmpl-fixture", "object": "chat.completion", "created": 1,
        "model": "fixture-model", "choices": [{"index": 0, "message": {
            "role": "assistant", "content": content, "tool_calls": tool_calls,
        }, "finish_reason": "tool_calls" if tool_calls else "stop"}],
    }


def _tool_call(name, arguments, call_id="call-1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


class ScriptedPost:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.payloads = []

    def __call__(self, path, *, json):
        self.payloads.append((path, json))
        return self.responses.pop(0)


def test_catalog_covers_fixed_unified_search_failure_shapes():
    case_ids = {case.id for case in FIXED_CASES}
    assert {
        "linked-meaning-code", "relevant-code-no-association", "wiki-only",
        "code-empty", "code-graph-missing", "code-graph-dirty",
        "code-graph-busy", "code-graph-stale", "wiki-links-stale",
        "context-truncated", "revision-mismatch", "wiki-embedding-failure",
        "wiki-rerank-failure", "code-reader-failure", "invalid-filters",
        "out-of-scope-domains", "sqlite-postgres-hosted-labels",
    } <= case_ids
    assert all(case.coordinated_meaning_code is not None for case in FIXED_CASES)


def test_catalog_is_deeply_immutable_and_adapters_thaw_fresh_values():
    case = next(case for case in FIXED_CASES if case.id == "linked-meaning-code")
    try:
        case.wiki.results[0]["fact_id"] = "mutated"
    except TypeError:
        pass
    else:
        raise AssertionError("fixture nested mapping must be immutable")

    first = case.wiki.as_dict()
    first["results"][0]["fact_id"] = "mutated"
    assert case.wiki.as_dict()["results"][0]["fact_id"] == "wiki-policy"

    constructed = UnifiedSearchCase(
        "constructed", "Synthetic task", "sqlite", WikiResponse(results=[]),
        CodeResponse(results=[]), ContextResponse(seeds=["seed"], warnings=["warning"]),
        ["fact"], "ready", False,
    )
    assert isinstance(constructed.context.seeds, tuple)
    assert isinstance(constructed.context.warnings, tuple)
    assert isinstance(constructed.expected_fact_ids, tuple)


def test_baseline_tool_loop_executes_complete_chat_shapes_and_scores_exactly():
    case = next(case for case in FIXED_CASES if case.id == "linked-meaning-code")
    post = ScriptedPost(
        _response(tool_calls=[_tool_call("wiki_search", {"query": case.task_prompt})]),
        _response(tool_calls=[_tool_call("wiki_code_search", {"query": case.task_prompt})]),
        _response(tool_calls=[_tool_call("wiki_code_context", {"seeds": ["entity-policy"]})]),
        _response(content='{"fact_ids":["wiki-policy","code-policy","association-policy"],"graph_state":"ready"}'),
    )

    result = run_agent_case(case, "baseline", "fixture-model", post, max_rounds=5)

    assert result.success
    assert result.status == "ok"
    assert result.client_visible_call_count == 4
    assert result.missing_fact_ids == ()
    assert result.extra_fact_ids == ()
    assert result.graph_state_correct
    assert [item["name"] for item in result.tool_trace] == [
        "wiki_search", "wiki_code_search", "wiki_code_context",
    ]
    assert post.payloads[0][0] == "/chat/completions"
    first = post.payloads[0][1]
    assert first["model"] == "fixture-model"
    assert first["temperature"] == 0
    assert first["messages"] == result.shared_messages
    assert first["messages"][0]["content"] == result.shared_messages[0]["content"]
    assert "scope=" in first["messages"][0]["content"]
    assert "rubric=" in first["messages"][0]["content"]
    assert {tool["function"]["name"] for tool in first["tools"]} == {
        "wiki_search", "wiki_code_search", "wiki_code_context",
    }


def test_candidate_shares_environment_and_prompt_but_uses_one_unified_call():
    case = next(case for case in FIXED_CASES if case.id == "linked-meaning-code")
    baseline_post = ScriptedPost(_response(content='{"fact_ids":[],"graph_state":"ready"}'))
    candidate_post = ScriptedPost(
        _response(tool_calls=[_tool_call("wiki_unified_search", {"query": case.task_prompt})]),
        _response(content='{"fact_ids":["wiki-policy","code-policy","association-policy"],"graph_state":"ready"}'),
    )

    baseline = run_agent_case(case, "baseline", "fixture-model", baseline_post)
    candidate = run_agent_case(case, "candidate", "fixture-model", candidate_post)

    assert baseline.environment_hash == candidate.environment_hash
    assert baseline.prompt_hash == candidate.prompt_hash
    assert baseline.tool_schema_hash != candidate.tool_schema_hash
    assert candidate.client_visible_call_count == 2
    assert candidate.tool_trace[0]["name"] == "wiki_unified_search"
    assert baseline.declared_tool_names == (
        "wiki_search", "wiki_code_search", "wiki_code_context",
    )
    assert candidate.declared_tool_names == ("wiki_unified_search",)
    assert candidate.expected_graph_state == "ready"
    baseline_payload = dict(baseline_post.payloads[0][1])
    candidate_payload = dict(candidate_post.payloads[0][1])
    baseline_payload.pop("tools")
    candidate_payload.pop("tools")
    assert baseline_payload == candidate_payload
    assert {tool["function"]["name"] for tool in candidate_post.payloads[0][1]["tools"]} == {"wiki_unified_search"}


def test_agent_blocks_cycles_and_redacts_transport_and_secret_sentinel():
    case = next(case for case in FIXED_CASES if case.id == "wiki-only")
    post = ScriptedPost(
        _response(tool_calls=[_tool_call("wiki_search", {"query": case.task_prompt})]),
        _response(tool_calls=[_tool_call("wiki_search", {"query": case.task_prompt})]),
    )
    cycle = run_agent_case(case, "baseline", "fixture-model", post)
    assert cycle.status == "blocked_cycle"
    assert not cycle.success

    def fail_post(*args, **kwargs):
        raise RuntimeError("SECRET_SENTINEL private-token")

    failed = run_agent_case(case, "candidate", "fixture-model", fail_post)
    assert failed.status == "failed_transport"
    assert failed.client_visible_call_count == 1
    assert "SECRET_SENTINEL" not in str(failed)


def test_agent_rejects_unknown_tools_malformed_arguments_and_invalid_final_json():
    case = next(case for case in FIXED_CASES if case.id == "wiki-only")
    unknown = run_agent_case(
        case, "candidate", "fixture-model",
        ScriptedPost(_response(tool_calls=[_tool_call("not_declared", {})])),
    )
    malformed = run_agent_case(
        case, "candidate", "fixture-model",
        ScriptedPost(_response(tool_calls=[{"id": "x", "type": "function", "function": {"name": "wiki_unified_search", "arguments": "{"}}])),
    )
    final = run_agent_case(
        case, "candidate", "fixture-model",
        ScriptedPost(_response(content="not-json")),
    )
    assert (unknown.status, malformed.status, final.status) == (
        "failed_unknown_tool", "failed_malformed_arguments", "failed_final_json",
    )


def test_agent_rejects_schema_invalid_arguments_and_duplicate_facts():
    case = next(case for case in FIXED_CASES if case.id == "wiki-only")
    missing = run_agent_case(
        case, "candidate", "fixture-model",
        ScriptedPost(_response(tool_calls=[_tool_call("wiki_unified_search", {})])),
    )
    wrong_type = run_agent_case(
        case, "baseline", "fixture-model",
        ScriptedPost(_response(tool_calls=[_tool_call("wiki_code_context", {"seeds": "not-an-array"})])),
    )
    extra = run_agent_case(
        case, "candidate", "fixture-model",
        ScriptedPost(_response(tool_calls=[_tool_call("wiki_unified_search", {"query": case.task_prompt, "extra": 1})])),
    )
    duplicate = run_agent_case(
        case, "candidate", "fixture-model",
        ScriptedPost(
            _response(content='{"fact_ids":["wiki-policy","wiki-policy"],"graph_state":"ready"}'),
        ),
    )
    assert (missing.status, wrong_type.status, extra.status) == (
        "failed_invalid_arguments", "failed_invalid_arguments", "failed_invalid_arguments",
    )
    assert not duplicate.success
    assert duplicate.extra_fact_ids == ("wiki-policy",)


def test_agent_sanitizes_max_round_and_incomplete_chat_completion_shapes():
    case = next(case for case in FIXED_CASES if case.id == "wiki-only")
    exhausted = run_agent_case(
        case, "candidate", "fixture-model",
        ScriptedPost(_response(tool_calls=[_tool_call("wiki_unified_search", {"query": case.task_prompt})])),
        max_rounds=1,
    )
    incomplete = run_agent_case(
        case, "candidate", "fixture-model",
        ScriptedPost({"choices": [{"message": {"role": "assistant", "content": '{"fact_ids":["unaccepted-input-detail"],"graph_state":"ready"}', "tool_calls": None}}]}),
    )
    assert exhausted.status == "failed_max_rounds"
    assert incomplete.status == "failed_response"
    assert "unaccepted-input-detail" not in str(incomplete)


def test_agent_bounds_tool_trace_before_executing_a_large_completion(monkeypatch):
    case = next(case for case in FIXED_CASES if case.id == "wiki-only")
    callback_count = 0
    schema = {
        "type": "function", "function": {"name": "private_tool", "parameters": {
            "type": "object", "properties": {"query": {"type": "string"}},
            "required": ["query"], "additionalProperties": False,
        }},
    }

    def callback(args):
        nonlocal callback_count
        callback_count += 1
        return {"results": []}

    monkeypatch.setattr(agent, "_adapters", lambda case, arm: ([schema], {"private_tool": callback}))
    calls = [_tool_call("private_tool", {"query": str(index)}, f"call-{index}") for index in range(4)]
    result = run_agent_case(case, "candidate", "fixture-model", ScriptedPost(_response(tool_calls=calls)), max_rounds=1)

    assert result.status == "failed_tool_limit"
    assert callback_count == 0
    assert len(result.tool_trace) <= 1


def test_agent_sanitizes_non_json_callback_output(monkeypatch):
    case = next(case for case in FIXED_CASES if case.id == "wiki-only")
    schema = {
        "type": "function", "function": {"name": "private_tool", "parameters": {
            "type": "object", "properties": {"query": {"type": "string"}},
            "required": ["query"], "additionalProperties": False,
        }},
    }
    monkeypatch.setattr(agent, "_adapters", lambda case, arm: ([schema], {"private_tool": lambda args: {"bad": object()}}))

    result = run_agent_case(
        case, "candidate", "fixture-model",
        ScriptedPost(_response(tool_calls=[_tool_call("private_tool", {"query": "safe"})])),
    )

    assert result.status == "failed_tool_output"
    assert "object at" not in str(result)


@pytest.mark.parametrize("envelope", [
    {"id": 1, "type": "function", "function": {"name": "private_tool", "arguments": "{}"}},
    {"id": "call", "type": "not-function", "function": {"name": "private_tool", "arguments": "{}"}},
    {"id": "call", "type": "function", "function": "not-a-mapping"},
    {"id": "call", "type": "function", "function": {"name": "private_tool", "arguments": 1}},
])
def test_agent_rejects_invalid_tool_envelopes_before_callback(monkeypatch, envelope):
    case = next(case for case in FIXED_CASES if case.id == "wiki-only")
    callback_count = 0
    schema = {"type": "function", "function": {"name": "private_tool", "parameters": {
        "type": "object", "properties": {}, "required": [], "additionalProperties": False,
    }}}

    def callback(args):
        nonlocal callback_count
        callback_count += 1
        return {}

    monkeypatch.setattr(agent, "_adapters", lambda case, arm: ([schema], {"private_tool": callback}))
    result = run_agent_case(case, "candidate", "fixture-model", ScriptedPost(_response(tool_calls=[envelope])))
    assert result.status == "failed_tool_envelope"
    assert callback_count == 0
    assert result.tool_trace == ()


@pytest.mark.parametrize("case", FIXED_CASES, ids=lambda case: case.id)
def test_every_catalog_case_runs_both_private_arms_without_mutation(case):
    expected = json.dumps({"fact_ids": list(case.expected_fact_ids), "graph_state": case.expected_graph_state})
    original_wiki = case.wiki.as_dict()
    original_code = case.code.as_dict()
    original_context = case.context.as_dict()
    baseline = run_agent_case(
        case, "baseline", "fixture-model",
        ScriptedPost(
            _response(tool_calls=[_tool_call("wiki_search", {"query": case.task_prompt})]),
            _response(tool_calls=[_tool_call("wiki_code_search", {"query": case.task_prompt})]),
            _response(tool_calls=[_tool_call("wiki_code_context", {"seeds": []})]),
            _response(content=expected),
        ),
    )
    candidate = run_agent_case(
        case, "candidate", "fixture-model",
        ScriptedPost(
            _response(tool_calls=[_tool_call("wiki_unified_search", {"query": case.task_prompt})]),
            _response(content=expected),
        ),
    )
    assert baseline.success and candidate.success
    assert baseline.expected_fact_ids == candidate.expected_fact_ids == case.expected_fact_ids
    assert baseline.expected_graph_state == candidate.expected_graph_state == case.expected_graph_state
    assert case.wiki.as_dict() == original_wiki
    assert case.code.as_dict() == original_code
    assert case.context.as_dict() == original_context


def test_task2_source_has_no_registry_boundary():
    server_module = "iwiki_mcp" + ".server"
    registry_name = "fast" + "mcp"
    for path in ("eval/unified_search/agent.py", "tests/eval/test_unified_search_agent.py"):
        text = open(path, encoding="utf-8").read().lower()
        assert server_module not in text
        assert registry_name not in text
