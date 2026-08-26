"""Small, private OpenAI-compatible chat-completions tool-loop harness."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

from eval.unified_search.candidate import compose_unified_search
from eval.unified_search.fixtures import UnifiedSearchCase


Post = Callable[..., dict[str, Any]]
_SECRET_SENTINEL = "SECRET_SENTINEL"
_SYSTEM_PROMPT = (
    "Use supplied read-only tools. scope={scope}; rubric={rubric}. "
    "Final answer must be strict JSON exactly "
    '{{"fact_ids":[...],"graph_state":"ready"}}; include no prose.'
)
_RUBRIC = "exact ordered fact_ids and exact graph_state; duplicate fact_ids fail"


@dataclass(frozen=True)
class AgentRun:
    arm: str
    case_id: str
    model: str
    environment_hash: str
    prompt_hash: str
    tool_schema_hash: str
    tool_trace: tuple[dict[str, str], ...]
    parsed_answer: dict[str, Any] | None
    expected_fact_ids: tuple[str, ...]
    expected_graph_state: str
    missing_fact_ids: tuple[str, ...]
    extra_fact_ids: tuple[str, ...]
    graph_state_correct: bool
    success: bool
    status: str
    client_visible_call_count: int
    shared_messages: list[dict[str, Any]]
    declared_tool_names: tuple[str, ...]


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _schema(name: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": "Synthetic read-only fixture adapter", "parameters": {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}}}


def _adapters(case: UnifiedSearchCase, arm: str) -> tuple[list[dict[str, Any]], dict[str, Callable[[dict[str, Any]], dict[str, Any]]]]:
    wiki = lambda _args: case.wiki.as_dict()
    code = lambda _args: case.code.as_dict()
    context = lambda _args: case.context.as_dict()
    if arm == "baseline":
        return ([_schema("wiki_search", {"query": {"type": "string"}}), _schema("wiki_code_search", {"query": {"type": "string"}}), _schema("wiki_code_context", {"seeds": {"type": "array", "items": {"type": "string"}}})], {"wiki_search": wiki, "wiki_code_search": code, "wiki_code_context": context})
    if arm == "candidate":
        return ([_schema("wiki_unified_search", {"query": {"type": "string"}})], {"wiki_unified_search": lambda _args: compose_unified_search(wiki_call=lambda: wiki({}), code_call=lambda: code({}), context_call=lambda seeds: context({"seeds": seeds}))})
    raise ValueError("invalid arm")


def _valid_value(value: Any, schema: dict[str, Any]) -> bool:
    kind = schema.get("type")
    if kind == "string":
        return isinstance(value, str)
    if kind == "array":
        return isinstance(value, list) and all(_valid_value(item, schema.get("items", {})) for item in value)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "object":
        return isinstance(value, dict)
    return False


def _valid_arguments(args: dict[str, Any], schema: dict[str, Any]) -> bool:
    parameters = schema["function"]["parameters"]
    properties = parameters["properties"]
    if any(name not in args for name in parameters.get("required", ())):
        return False
    if parameters.get("additionalProperties") is False and any(name not in properties for name in args):
        return False
    return all(_valid_value(value, properties[name]) for name, value in args.items())


def _tool_envelope(call: Any) -> tuple[str, str, str] | None:
    if not isinstance(call, Mapping) or call.get("type") != "function":
        return None
    call_id = call.get("id")
    function = call.get("function")
    if not isinstance(call_id, str) or not call_id or not isinstance(function, Mapping):
        return None
    name = function.get("name")
    arguments = function.get("arguments")
    if not isinstance(name, str) or not name or not isinstance(arguments, str):
        return None
    return call_id, name, arguments


def _valid_completion(response: dict[str, Any]) -> bool:
    if not isinstance(response.get("id"), str) or response.get("object") != "chat.completion":
        return False
    if not isinstance(response.get("created"), int) or not isinstance(response.get("model"), str):
        return False
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return False
    choice = choices[0]
    message = choice.get("message")
    return (
        isinstance(choice.get("index"), int)
        and isinstance(choice.get("finish_reason"), str)
        and isinstance(message, dict)
        and message.get("role") == "assistant"
        and (message.get("content") is None or isinstance(message.get("content"), str))
        and (message.get("tool_calls") is None or isinstance(message.get("tool_calls"), list))
    )


def _run_result(case: UnifiedSearchCase, arm: str, model: str, environment_hash: str, prompt_hash: str, schema_hash: str, trace: list[dict[str, str]], parsed: dict[str, Any] | None, status: str, calls: int, messages: list[dict[str, Any]], tool_names: tuple[str, ...]) -> AgentRun:
    fact_ids = parsed.get("fact_ids", []) if parsed else []
    actual = tuple(fact_ids) if isinstance(fact_ids, list) and all(isinstance(item, str) for item in fact_ids) else ()
    expected = case.expected_fact_ids
    missing = tuple(item for index, item in enumerate(expected) if index >= len(actual) or actual[index] != item)
    extra = tuple(item for index, item in enumerate(actual) if index >= len(expected) or expected[index] != item)
    graph_ok = bool(parsed and parsed.get("graph_state") == case.expected_graph_state)
    success = status == "ok" and not missing and not extra and graph_ok
    return AgentRun(arm, case.id, model, environment_hash, prompt_hash, schema_hash, tuple(trace), parsed, expected, case.expected_graph_state, missing, extra, graph_ok, success, status, calls, messages[:2], tool_names)


def run_agent_case(case: UnifiedSearchCase, arm: str, model: str, post: Post, max_rounds: int = 4) -> AgentRun:
    """Run one fixed synthetic case; expected failure is represented, never raised."""
    schemas, callbacks = _adapters(case, arm)
    system_prompt = _SYSTEM_PROMPT.format(scope=case.scope_label, rubric=_RUBRIC)
    shared_messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": case.task_prompt}]
    environment_hash = _stable_hash({"case": case.id, "model": model, "max_rounds": max_rounds, "system": system_prompt, "task": case.task_prompt, "scope": case.scope_label, "rubric": _RUBRIC, "backend": case.backend_label, "expected": case.expected_fact_ids, "graph": case.expected_graph_state, "coordinated": case.coordinated_meaning_code, "wiki": case.wiki.as_dict(), "code": case.code.as_dict(), "context": case.context.as_dict()})
    prompt_hash = _stable_hash(shared_messages)
    schema_hash = _stable_hash(schemas)
    tool_names = tuple(schema["function"]["name"] for schema in schemas)
    messages = list(shared_messages)
    trace: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    tool_call_limit = max_rounds * len(tool_names)
    calls = 0
    for _round in range(max_rounds):
        payload = {"model": model, "messages": list(messages), "tools": schemas, "temperature": 0}
        calls += 1
        try:
            response = post("/chat/completions", json=payload)
        except Exception:
            return _run_result(case, arm, model, environment_hash, prompt_hash, schema_hash, trace, None, "failed_transport", calls, shared_messages, tool_names)
        if not isinstance(response, dict) or _SECRET_SENTINEL in str(response) or not _valid_completion(response):
            return _run_result(case, arm, model, environment_hash, prompt_hash, schema_hash, trace, None, "failed_response", calls, shared_messages, tool_names)
        try:
            message = response["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            return _run_result(case, arm, model, environment_hash, prompt_hash, schema_hash, trace, None, "failed_response", calls, shared_messages, tool_names)
        if not isinstance(message, dict):
            return _run_result(case, arm, model, environment_hash, prompt_hash, schema_hash, trace, None, "failed_response", calls, shared_messages, tool_names)
        tool_calls = message.get("tool_calls")
        if tool_calls:
            if not isinstance(tool_calls, list):
                return _run_result(case, arm, model, environment_hash, prompt_hash, schema_hash, trace, None, "failed_response", calls, shared_messages, tool_names)
            if len(tool_calls) > tool_call_limit - len(trace):
                return _run_result(case, arm, model, environment_hash, prompt_hash, schema_hash, trace, None, "failed_tool_limit", calls, shared_messages, tool_names)
            messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": tool_calls})
            for call in tool_calls:
                envelope = _tool_envelope(call)
                if envelope is None:
                    return _run_result(case, arm, model, environment_hash, prompt_hash, schema_hash, trace, None, "failed_tool_envelope", calls, shared_messages, tool_names)
                call_id, name, raw_args = envelope
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError:
                    return _run_result(case, arm, model, environment_hash, prompt_hash, schema_hash, trace, None, "failed_malformed_arguments", calls, shared_messages, tool_names)
                if name not in callbacks:
                    return _run_result(case, arm, model, environment_hash, prompt_hash, schema_hash, trace, None, "failed_unknown_tool", calls, shared_messages, tool_names)
                if not isinstance(args, dict):
                    return _run_result(case, arm, model, environment_hash, prompt_hash, schema_hash, trace, None, "failed_malformed_arguments", calls, shared_messages, tool_names)
                if not _valid_arguments(args, next(schema for schema in schemas if schema["function"]["name"] == name)):
                    return _run_result(case, arm, model, environment_hash, prompt_hash, schema_hash, trace, None, "failed_invalid_arguments", calls, shared_messages, tool_names)
                signature = (name, _stable_hash(args))
                if signature in seen:
                    return _run_result(case, arm, model, environment_hash, prompt_hash, schema_hash, trace, None, "blocked_cycle", calls, shared_messages, tool_names)
                seen.add(signature)
                try:
                    output = callbacks[name](args)
                except Exception:
                    return _run_result(case, arm, model, environment_hash, prompt_hash, schema_hash, trace, None, "failed_tool", calls, shared_messages, tool_names)
                if not isinstance(output, dict) or _SECRET_SENTINEL in str(output):
                    return _run_result(case, arm, model, environment_hash, prompt_hash, schema_hash, trace, None, "failed_tool", calls, shared_messages, tool_names)
                try:
                    serialized_output = json.dumps(output, sort_keys=True)
                except (TypeError, ValueError):
                    return _run_result(case, arm, model, environment_hash, prompt_hash, schema_hash, trace, None, "failed_tool_output", calls, shared_messages, tool_names)
                trace.append({"name": name, "call_id": str(call_id)})
                messages.append({"role": "tool", "tool_call_id": call_id, "content": serialized_output})
            continue
        content = message.get("content")
        if not isinstance(content, str) or _SECRET_SENTINEL in content:
            return _run_result(case, arm, model, environment_hash, prompt_hash, schema_hash, trace, None, "failed_final_json", calls, shared_messages, tool_names)
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return _run_result(case, arm, model, environment_hash, prompt_hash, schema_hash, trace, None, "failed_final_json", calls, shared_messages, tool_names)
        if not isinstance(parsed, dict) or set(parsed) != {"fact_ids", "graph_state"} or not isinstance(parsed["fact_ids"], list) or not all(isinstance(item, str) for item in parsed["fact_ids"]) or not isinstance(parsed["graph_state"], str):
            return _run_result(case, arm, model, environment_hash, prompt_hash, schema_hash, trace, None, "failed_final_json", calls, shared_messages, tool_names)
        return _run_result(case, arm, model, environment_hash, prompt_hash, schema_hash, trace, parsed, "ok", calls, shared_messages, tool_names)
    return _run_result(case, arm, model, environment_hash, prompt_hash, schema_hash, trace, None, "failed_max_rounds", calls, shared_messages, tool_names)
