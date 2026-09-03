import json

import pytest

from iwiki_mcp.telegram_bot.agent import AgentLoop, _MAX_TOOL_CALLS
from iwiki_mcp.telegram_bot.context import ContextBudget
from iwiki_mcp.telegram_bot.inference import ToolCall, ToolResponse


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
