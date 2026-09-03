import json

import pytest

from iwiki_mcp.telegram_bot.agent import AgentLoop, _MAX_TOOL_CALLS
from iwiki_mcp.telegram_bot.context import ContextBudget
from iwiki_mcp.telegram_bot.inference import InferenceError, ToolCall, ToolResponse
from iwiki_mcp.telegram_bot.iwiki import RemoteIwikiError


class FakeRemote:
    def __init__(self):
        self.calls = []

    async def search(self, domain, query):
        self.calls.append(("search", domain, query))
        return [{"slug": "guide/deploy", "heading": "Rollout"}]

    async def read_page(self, domain, slug, heading=None):
        self.calls.append(("read_page", domain, slug, heading))
        return {"body": "Lead paragraph.\n\nRollout uses blue-green."}


class ScriptedInference:
    """Returns queued ToolResponse objects and records every request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.tools_supported = True

    async def complete_with_tools(self, messages, tools, tool_choice="auto"):
        self.requests.append((
            [dict(message) for message in messages], tool_choice
        ))
        return self.responses.pop(0)


def _call(name, arguments, call_id="c1"):
    return ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))


@pytest.mark.asyncio
async def test_loop_searches_reads_and_answers():
    inference = ScriptedInference([
        ToolResponse(None, (_call("search_wiki", {"query": "rollout"}),)),
        ToolResponse(None, (_call(
            "read_section", {"slug": "guide/deploy", "heading": "Rollout"},
        ),)),
        ToolResponse("Blue-green rollout (guide/deploy#Rollout).", ()),
    ])
    remote = FakeRemote()
    loop = AgentLoop(remote, inference, ContextBudget())

    answer = await loop.run("team", "How do we roll out?")

    assert answer == "Blue-green rollout (guide/deploy#Rollout)."
    assert ("search", "team", "rollout") in remote.calls
    assert ("read_page", "team", "guide/deploy", "Rollout") in remote.calls
    # Tool results reached the transcript of the final completion.
    final_messages = inference.requests[-1][0]
    assert any(
        message["role"] == "tool" and "blue-green" in message["content"]
        for message in final_messages
    )


@pytest.mark.asyncio
async def test_loop_forces_answer_at_tool_call_limit():
    burst = [
        ToolResponse(None, (_call("search_wiki", {"query": f"q{i}"}, f"c{i}"),))
        for i in range(_MAX_TOOL_CALLS)
    ]
    inference = ScriptedInference(burst + [ToolResponse("Done.", ())])
    loop = AgentLoop(FakeRemote(), inference, ContextBudget())

    answer = await loop.run("team", "question")

    assert answer == "Done."
    assert inference.requests[-1][1] == "none"


@pytest.mark.asyncio
async def test_progress_reports_iterations():
    inference = ScriptedInference([
        ToolResponse(None, (_call("search_wiki", {"query": "x"}),)),
        ToolResponse("Answer.", ()),
    ])
    stages = []

    async def progress(text):
        stages.append(text)

    loop = AgentLoop(FakeRemote(), inference, ContextBudget())
    await loop.run("team", "q", progress)

    assert any(stage.startswith("Searching wiki (1/") for stage in stages)
    assert "Generating answer" in stages


class StubbornInference(ScriptedInference):
    """Ignores tool_choice and always returns tool_calls."""

    async def complete_with_tools(self, messages, tools, tool_choice="auto"):
        self.requests.append((
            [dict(message) for message in messages], tool_choice
        ))
        return ToolResponse(None, (_call("search_wiki", {"query": "x"}),))


@pytest.mark.asyncio
async def test_forced_loop_terminates_when_provider_ignores_tool_choice():
    burst = [None] * (_MAX_TOOL_CALLS + 3)  # responses come from override
    loop = AgentLoop(FakeRemote(), StubbornInference(burst), ContextBudget())
    with pytest.raises(InferenceError, match="invalid_inference_response"):
        await loop.run("team", "q")


@pytest.mark.asyncio
async def test_batched_tool_calls_never_exceed_limit():
    calls = tuple(
        _call("search_wiki", {"query": f"q{i}"}, f"c{i}") for i in range(4)
    )
    inference = ScriptedInference([
        ToolResponse(None, calls),
        ToolResponse(None, calls),
        ToolResponse("Done.", ()),
    ])
    remote = FakeRemote()
    loop = AgentLoop(remote, inference, ContextBudget())
    await loop.run("team", "q")
    searches = [c for c in remote.calls if c[0] == "search"]
    assert len(searches) <= _MAX_TOOL_CALLS


class OverflowingInference(ScriptedInference):
    """Raises context_overflow once, then serves the queue."""

    def __init__(self, responses, overflow_at):
        super().__init__(responses)
        self.overflow_at = overflow_at
        self.count = 0

    async def complete_with_tools(self, messages, tools, tool_choice="auto"):
        self.count += 1
        if self.count == self.overflow_at:
            self.requests.append((
                [dict(message) for message in messages], tool_choice
            ))
            raise InferenceError("context_overflow")
        return await super().complete_with_tools(
            messages, tools, tool_choice
        )


@pytest.mark.asyncio
async def test_duplicate_read_returns_marker():
    read = _call(
        "read_section", {"slug": "guide/deploy", "heading": "Rollout"}
    )
    inference = ScriptedInference([
        ToolResponse(None, (read,)),
        ToolResponse(None, (read,)),
        ToolResponse("Answer.", ()),
    ])
    remote = FakeRemote()
    loop = AgentLoop(remote, inference, ContextBudget())

    await loop.run("team", "q")

    reads = [call for call in remote.calls if call[0] == "read_page"]
    assert len(reads) == 1
    final_messages = inference.requests[-1][0]
    assert any(
        message["role"] == "tool" and message["content"] == "already provided"
        for message in final_messages
    )


@pytest.mark.asyncio
async def test_invalid_arguments_become_error_results():
    inference = ScriptedInference([
        ToolResponse(None, (ToolCall("c1", "search_wiki", "not json"),)),
        ToolResponse(None, (_call("search_wiki", {"query": "   "}),)),
        ToolResponse(None, (_call("no_such_tool", {}),)),
        ToolResponse("Answer.", ()),
    ])
    loop = AgentLoop(FakeRemote(), inference, ContextBudget())

    answer = await loop.run("team", "q")

    assert answer == "Answer."
    final_messages = inference.requests[-1][0]
    errors = [
        message["content"] for message in final_messages
        if message["role"] == "tool"
    ]
    assert all(text.startswith("error:") for text in errors)
    assert len(errors) == 3


@pytest.mark.asyncio
async def test_overflow_drops_oldest_results_and_retries_once():
    inference = OverflowingInference([
        ToolResponse(None, (_call("search_wiki", {"query": "a"}, "c1"),)),
        ToolResponse(None, (_call(
            "read_section", {"slug": "guide/deploy", "heading": "Rollout"},
            "c2",
        ),)),
        ToolResponse("Answer.", ()),
    ], overflow_at=3)
    loop = AgentLoop(FakeRemote(), inference, ContextBudget())

    answer = await loop.run("team", "q")

    assert answer == "Answer."
    final_messages = inference.requests[-1][0]
    tool_contents = [
        message["content"] for message in final_messages
        if message["role"] == "tool"
    ]
    assert "[dropped]" in tool_contents


@pytest.mark.asyncio
async def test_second_overflow_raises():
    class AlwaysOverflow(ScriptedInference):
        async def complete_with_tools(self, messages, tools, tool_choice="auto"):
            raise InferenceError("context_overflow")

    loop = AgentLoop(FakeRemote(), AlwaysOverflow([]), ContextBudget())

    with pytest.raises(InferenceError, match="context_overflow"):
        await loop.run("team", "q")


@pytest.mark.asyncio
async def test_nonretryable_wiki_error_becomes_tool_result():
    class BrokenRemote(FakeRemote):
        async def search(self, domain, query):
            raise RemoteIwikiError("remote_call_failed")

    inference = ScriptedInference([
        ToolResponse(None, (_call("search_wiki", {"query": "x"}),)),
        ToolResponse("Partial answer.", ()),
    ])
    loop = AgentLoop(BrokenRemote(), inference, ContextBudget())

    answer = await loop.run("team", "q")

    assert answer == "Partial answer."


@pytest.mark.asyncio
async def test_retryable_wiki_error_propagates():
    class DownRemote(FakeRemote):
        async def search(self, domain, query):
            raise RemoteIwikiError("remote_call_failed", retryable=True)

    inference = ScriptedInference([
        ToolResponse(None, (_call("search_wiki", {"query": "x"}),)),
    ])
    loop = AgentLoop(DownRemote(), inference, ContextBudget())

    with pytest.raises(RemoteIwikiError):
        await loop.run("team", "q")


@pytest.mark.asyncio
async def test_part_read_returns_untrimmed_chunk():
    class LongRemote(FakeRemote):
        async def read_page(self, domain, slug, heading=None):
            self.calls.append(("read_page", domain, slug, heading))
            return {"body": "x" * 5000}

    inference = ScriptedInference([
        ToolResponse(None, (_call(
            "read_section",
            {"slug": "guide/deploy", "heading": "Rollout", "part": 2},
        ),)),
        ToolResponse("Answer.", ()),
    ])
    loop = AgentLoop(LongRemote(), inference, ContextBudget())

    await loop.run("team", "q")

    final_messages = inference.requests[-1][0]
    chunk = next(
        message["content"] for message in final_messages
        if message["role"] == "tool"
    )
    assert chunk == "x" * 1000  # 5000 - _PART_CHARS offset for part 2
