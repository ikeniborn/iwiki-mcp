---
chain:
  intent: docs/superpowers/intents/2026-09-03-telegram-bot-agentic-retrieval-intent.md
  spec: docs/superpowers/specs/2026-09-03-telegram-bot-agentic-retrieval-design.md
review:
  plan_hash: 13c99d68fbbcb727
  last_run: 2026-09-03
  phases:
    structure:
      status: passed
    coverage:
      status: passed
    dependencies:
      status: passed
    verifiability:
      status: passed
    consistency:
      status: passed
  findings:
    - id: F-001
      phase: coverage
      severity: INFO
      section: "Task 7: docs, version bump, full verification"
      section_hash: null
      fragment: "Bump the version"
      text: "Version bump and README steps trace to repository policy (CLAUDE.md), not to a spec requirement"
      fix: "None needed; policy-mandated steps are allowed extras"
      verdict: accepted
      verdict_at: 2026-09-03
result_check:
  verdict: OK
  plan_hash: 13c99d68fbbcb727
  last_run: 2026-09-04
---

# Telegram Bot Agentic Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Telegram bot's single-pass RAG pipeline with an LLM-driven native tool-calling loop (`search_wiki` / `read_section`), with startup capability detection and the current pipeline as the fallback.

**Architecture:** One new module `agent.py` (`AgentLoop`) drives OpenAI tool calling against the existing `RemoteIwikiClient`, `InferenceClient`, and `ContextBudget`. `inference.py` gains `complete_with_tools` and a probe-time `tools_supported` flag; `conversation.py` branches on that flag for questions and `/create` drafts. `/update`, writes, and every privacy/trust invariant stay untouched.

**Tech Stack:** Python 3.11+, httpx, anyio, pytest (`asyncio_mode=auto`), flake8 (max-line-length 100). No new dependencies, no new environment variables.

**Spec:** docs/superpowers/specs/2026-09-03-telegram-bot-agentic-retrieval-design.md

## Global Constraints

- No new environment variables; the tool-call limit is `_MAX_TOOL_CALLS = 6` in `agent.py`.
- The agentic loop gets read-only tools only; the model never picks a domain; `wiki_search` is never called with `intent="write"`.
- Never log or persist prompts, tool results, transcripts, or answers; logs carry only stable fields (operation, outcome, elapsed, usage, prompt_chars, iteration count).
- The fallback path (`_collect_sections` → `select_context` → `answer()`) stays byte-for-byte unchanged; existing tests must keep passing unmodified (except the `FakeInference` fixtures gaining a `tools_supported` attribute).
- Tests never hit the network: `httpx.MockTransport` for inference, fake remote objects for iwiki.
- Lint: `uv run flake8 src tests` clean; suite: `uv run pytest -q tests/telegram_bot` green.
- Version bump in `pyproject.toml`: `0.7.245` → `0.7.246` (patch), in the final task.
- All code comments and commit messages in English; Conventional Commits with the harness co-author line.

---

### Task 1: `complete_with_tools` on InferenceClient

**Files:**
- Modify: `src/iwiki_mcp/telegram_bot/inference.py`
- Test: `tests/telegram_bot/test_inference.py`

**Interfaces:**
- Consumes: existing `InferenceClient._post_json`, `_record_telemetry`, `_observe_usage`.
- Produces: `ToolCall` frozen dataclass (`id: str`, `name: str`, `arguments: str` — raw JSON string), `ToolResponse` frozen dataclass (`content: str | None`, `tool_calls: tuple[ToolCall, ...]`), and `async InferenceClient.complete_with_tools(messages: list[dict], tools: list[dict], tool_choice: str = "auto") -> ToolResponse`. Task 3 relies on these exact names.

- [ ] **Step 1: Write the failing tests**

Append to `tests/telegram_bot/test_inference.py`:

```python
@pytest.mark.asyncio
async def test_complete_with_tools_returns_tool_calls():
    seen = {}

    def handler(request):
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {
                "content": None,
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "search_wiki",
                        "arguments": "{\"query\": \"deploy\"}",
                    },
                }],
            }}],
            "usage": {"prompt_tokens": 50},
        })

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = InferenceClient(
        "https://models.example/v1", "key", "chat-model", "audio-model", http
    )
    messages = [{"role": "user", "content": "q"}]
    tools = [{"type": "function", "function": {"name": "search_wiki"}}]

    response = await client.complete_with_tools(messages, tools)

    assert response.content is None
    assert response.tool_calls[0].name == "search_wiki"
    assert response.tool_calls[0].arguments == "{\"query\": \"deploy\"}"
    assert seen["payload"]["tools"] == tools
    assert seen["payload"]["tool_choice"] == "auto"
    assert seen["payload"]["temperature"] == 0
    await http.aclose()


@pytest.mark.asyncio
async def test_complete_with_tools_returns_final_content():
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "Answer."}}],
            "usage": {"prompt_tokens": 30},
        })

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = InferenceClient(
        "https://models.example/v1", "key", "chat-model", "audio-model", http
    )

    response = await client.complete_with_tools(
        [{"role": "user", "content": "q"}], [], tool_choice="none"
    )

    assert response.content == "Answer."
    assert response.tool_calls == ()
    await http.aclose()


@pytest.mark.asyncio
async def test_complete_with_tools_rejects_empty_message():
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "", "tool_calls": []}}],
        })

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = InferenceClient(
        "https://models.example/v1", "key", "chat-model", "audio-model", http
    )

    with pytest.raises(InferenceError, match="invalid_inference_response"):
        await client.complete_with_tools(
            [{"role": "user", "content": "q"}], []
        )
    await http.aclose()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest -q tests/telegram_bot/test_inference.py -k complete_with_tools`
Expected: FAIL with `AttributeError: ... has no attribute 'complete_with_tools'`

- [ ] **Step 3: Implement**

In `src/iwiki_mcp/telegram_bot/inference.py`, add after the imports block:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation the model requested."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ToolResponse:
    """One chat completion: either tool calls to run or the final content."""

    content: str | None
    tool_calls: tuple[ToolCall, ...]
```

Add to `InferenceClient` (near `_complete`):

```python
    async def complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        tool_choice: str = "auto",
    ) -> ToolResponse:
        started = time.monotonic()
        prompt_chars = sum(
            len(str(message.get("content") or "")) for message in messages
        )
        try:
            payload = await self._post_json(
                "/chat/completions",
                json={
                    "model": self._chat_model,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": tool_choice,
                    "temperature": 0,
                    "max_tokens": self._max_output_tokens,
                },
            )
            response = self._parse_tool_response(payload)
        except InferenceError as error:
            self._record_telemetry("chat", "failure", started, {}, prompt_chars)
            if str(error) == "context_overflow":
                self._escalate_budget()
            raise
        self._record_telemetry("chat", "success", started, payload, prompt_chars)
        self._observe_usage(payload, prompt_chars)
        return response

    @staticmethod
    def _parse_tool_response(payload: dict[str, object]) -> ToolResponse:
        try:
            message = payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise InferenceError("invalid_inference_response") from None
        if not isinstance(message, dict):
            raise InferenceError("invalid_inference_response")
        raw_calls = message.get("tool_calls")
        calls: list[ToolCall] = []
        if isinstance(raw_calls, list):
            for raw in raw_calls:
                function = raw.get("function") if isinstance(raw, dict) else None
                if not isinstance(function, dict):
                    continue
                name = function.get("name")
                arguments = function.get("arguments")
                if isinstance(name, str) and isinstance(arguments, str):
                    calls.append(ToolCall(
                        id=str(raw.get("id", "")),
                        name=name,
                        arguments=arguments,
                    ))
        content = message.get("content")
        content = content if isinstance(content, str) and content.strip() else None
        if content is None and not calls:
            raise InferenceError("invalid_inference_response")
        return ToolResponse(content=content, tool_calls=tuple(calls))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q tests/telegram_bot/test_inference.py`
Expected: PASS (all, including pre-existing)

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/telegram_bot/inference.py tests/telegram_bot/test_inference.py
git commit -m "feat(telegram): add tool-calling completion to the inference client"
```

---

### Task 2: probe-time tool-calling capability detection

**Files:**
- Modify: `src/iwiki_mcp/telegram_bot/inference.py`
- Test: `tests/telegram_bot/test_inference.py`

**Interfaces:**
- Consumes: `InferenceClient.probe()` (extended in place).
- Produces: `InferenceClient.tools_supported: bool` (False until a successful probe with tools; also set False by runtime demotion in Task 6), module helper `_tools_refusal(status, code, message) -> bool`. Tasks 5–6 read `tools_supported`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/telegram_bot/test_inference.py`:

```python
def _models_ok():
    return httpx.Response(
        200, json={"data": [{"id": "chat-model"}, {"id": "audio-model"}]}
    )


@pytest.mark.asyncio
async def test_probe_detects_tool_calling_support():
    def handler(request):
        if request.url.path.endswith("/models"):
            return _models_ok()
        payload = json.loads(request.content)
        assert payload["tools"]
        assert payload["max_tokens"] == 1
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
        })

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = InferenceClient(
        "https://models.example/v1", "key", "chat-model", "audio-model", http
    )

    await client.probe()

    assert client.tools_supported is True
    await http.aclose()


@pytest.mark.asyncio
async def test_probe_demotes_when_provider_refuses_tools():
    def handler(request):
        if request.url.path.endswith("/models"):
            return _models_ok()
        return httpx.Response(400, json={"error": {
            "message": "unknown parameter: tools",
            "type": "invalid_request_error",
        }})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = InferenceClient(
        "https://models.example/v1", "key", "chat-model", "audio-model", http
    )

    await client.probe()

    assert client.tools_supported is False
    await http.aclose()


@pytest.mark.asyncio
async def test_probe_transient_tool_check_failure_keeps_startup_semantics():
    def handler(request):
        if request.url.path.endswith("/models"):
            return _models_ok()
        return httpx.Response(503)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = InferenceClient(
        "https://models.example/v1", "key", "chat-model", "audio-model", http
    )

    with pytest.raises(InferenceError) as captured:
        await client.probe()

    assert captured.value.retryable is True
    await http.aclose()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest -q tests/telegram_bot/test_inference.py -k probe`
Expected: the three new tests FAIL (`tools_supported` missing / probe sends no tool check)

- [ ] **Step 3: Implement**

In `inference.py`, add a module constant near the top:

```python
# Any client-side rejection of the probe request is a tools refusal: the only
# unusual thing about the probe is the tools parameter. At runtime (task 6)
# the same helper additionally requires tool wording, because a live 400 can
# have other causes.
_TOOLS_REFUSAL_STATUSES = frozenset({400, 404, 422, 501})
_TOOLS_REFUSAL_WORDS = ("tool", "function")


def _tools_refusal(
    status: int | None, code: str | None, message: str | None
) -> bool:
    if status not in _TOOLS_REFUSAL_STATUSES:
        return False
    lowered = f"{code or ''} {message or ''}".lower()
    return any(word in lowered for word in _TOOLS_REFUSAL_WORDS)


_PROBE_TOOL = [{
    "type": "function",
    "function": {
        "name": "noop",
        "description": "capability probe",
        "parameters": {"type": "object", "properties": {}},
    },
}]
```

In `InferenceClient.__init__`, add `self.tools_supported = False`.

At the end of `probe()` (after the model checks), add:

```python
        try:
            await self._post_json(
                "/chat/completions",
                json={
                    "model": self._chat_model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "tools": _PROBE_TOOL,
                    "max_tokens": 1,
                },
            )
        except InferenceError as error:
            probe_refusal = error.status in _TOOLS_REFUSAL_STATUSES
            if not probe_refusal:
                raise
            LOGGER.warning(
                "tool calling unavailable status=%s code=%s",
                error.status,
                error.provider_code,
            )
            self.tools_supported = False
            return
        self.tools_supported = True
```

Also extend `InferenceError.__init__` with a `provider_message: str | None = None`
keyword stored as `self.provider_message`, and make `_request_failure` pass the
`message` it already extracts:

```python
        return InferenceError(
            code,
            retryable=retryable,
            status=status,
            path=path,
            provider_code=provider_code,
            provider_message=message,
        )
```

Note: the probe branch uses the status alone (any listed 4xx refusing the probe is a
tools refusal); `_tools_refusal` with wording is for the runtime path in Task 6, which
calls `_tools_refusal(error.status, error.provider_code, error.provider_message)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q tests/telegram_bot/test_inference.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/telegram_bot/inference.py tests/telegram_bot/test_inference.py
git commit -m "feat(telegram): detect tool-calling support at inference probe"
```

---

### Task 3: AgentLoop core — search → read → answer

**Files:**
- Create: `src/iwiki_mcp/telegram_bot/agent.py`
- Test: `tests/telegram_bot/test_agent.py` (new)

**Interfaces:**
- Consumes: `ToolResponse` / `ToolCall` (Task 1); `remote.search(domain, query)`, `remote.read_page(domain, slug, heading)`; `ContextBudget.chars(fixed_chars)`; `select_context` and `Section` from `context.py`.
- Produces: `class AgentLoop` with `__init__(self, remote, inference, budget)` and `async run(self, domain: str, question: str, progress=None, *, drafting: bool = False) -> str`. Module constants `_MAX_TOOL_CALLS = 6`, `_PART_CHARS = 4000`. Task 5 calls `AgentLoop(...).run(...)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/telegram_bot/test_agent.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest -q tests/telegram_bot/test_agent.py`
Expected: FAIL with `ModuleNotFoundError: iwiki_mcp.telegram_bot.agent`

- [ ] **Step 3: Implement `agent.py`**

Create `src/iwiki_mcp/telegram_bot/agent.py`:

```python
"""LLM-driven tool-use retrieval loop over the remote wiki."""

import json

from .context import ContextBudget, Section, select_context
from .iwiki import RemoteIwikiError

_MAX_TOOL_CALLS = 6
_PART_CHARS = 4000

_ANSWER_PROMPT = (
    "You answer questions using only the '{domain}' wiki domain.\n"
    "Rules: answer only from tool results; attribute statements as"
    " page#heading; if searching finds nothing relevant, say so - never"
    " invent.\n"
    "Strategy: start with search_wiki; reformulate with narrower or"
    " different terms when hits are weak; read only the sections you need;"
    " request a part continuation only when the trimmed section visibly cut"
    " something essential.\n"
    "You have at most {limit} tool calls; answer as soon as you have"
    " enough. Answer in the user's language."
)

_DRAFT_PROMPT = (
    "You draft wiki Markdown using only the '{domain}' wiki domain for"
    " context.\n"
    "Use search_wiki and read_section to gather related content first.\n"
    "You have at most {limit} tool calls.\n"
    "Your final message must be only the Markdown page body for the"
    " requested change - no commentary."
)

_FORCE_ANSWER = "Answer now from the context above."

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_wiki",
            "description": (
                "Search the wiki domain. Returns matching sections as"
                " slug and heading."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_section",
            "description": (
                "Read one wiki section. part=0 (default) returns a view"
                " trimmed to the question; part=1..N returns the full"
                " section in sequential chunks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "heading": {"type": "string"},
                    "part": {"type": "integer", "minimum": 0},
                },
                "required": ["slug", "heading"],
            },
        },
    },
]


def _transcript_chars(messages: list[dict]) -> int:
    return sum(len(str(message.get("content") or "")) for message in messages)


class AgentLoop:
    def __init__(self, remote, inference, budget: ContextBudget) -> None:
        self._remote = remote
        self._inference = inference
        self._budget = budget

    async def run(
        self,
        domain: str,
        question: str,
        progress=None,
        *,
        drafting: bool = False,
    ) -> str:
        prompt = (_DRAFT_PROMPT if drafting else _ANSWER_PROMPT).format(
            domain=domain, limit=_MAX_TOOL_CALLS
        )
        messages: list[dict] = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": question},
        ]
        limit_chars = self._budget.chars(len(prompt) + len(question))
        calls_used = 0
        seen: set[tuple[str, str, int]] = set()
        forced = False
        while True:
            over_budget = _transcript_chars(messages) >= limit_chars
            if not forced and (calls_used >= _MAX_TOOL_CALLS or over_budget):
                messages.append({"role": "user", "content": _FORCE_ANSWER})
                forced = True
            if forced and progress is not None:
                await progress("Generating answer")
            response = await self._complete(
                messages, "none" if forced else "auto"
            )
            if response.content is not None and not response.tool_calls:
                return response.content
            if forced:
                # A forced completion that still asks for tools is a
                # provider defect; treat any content as the answer.
                if response.content is not None:
                    return response.content
                messages.append({"role": "user", "content": _FORCE_ANSWER})
                continue
            for call in response.tool_calls:
                calls_used += 1
                result = await self._execute(
                    domain, question, call, seen,
                    limit_chars - _transcript_chars(messages),
                )
                messages.append({
                    "role": "assistant",
                    "tool_calls": [{
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": call.arguments,
                        },
                    }],
                    "content": None,
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result,
                })
                if progress is not None:
                    stage = (
                        "Searching wiki"
                        if call.name == "search_wiki"
                        else "Reading section"
                    )
                    await progress(
                        f"{stage} ({calls_used}/{_MAX_TOOL_CALLS})"
                    )

    async def _complete(self, messages, tool_choice):
        return await self._inference.complete_with_tools(
            messages, _TOOLS, tool_choice
        )

    async def _execute(
        self,
        domain: str,
        question: str,
        call,
        seen: set[tuple[str, str, int]],
        remaining_chars: int,
    ) -> str:
        try:
            arguments = json.loads(call.arguments)
        except ValueError:
            return "error: tool arguments are not valid JSON"
        if not isinstance(arguments, dict):
            return "error: tool arguments are not an object"
        if call.name == "search_wiki":
            return await self._search(domain, arguments)
        if call.name == "read_section":
            return await self._read(
                domain, question, arguments, seen, remaining_chars
            )
        return f"error: unknown tool {call.name!r}"

    async def _search(self, domain: str, arguments: dict) -> str:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return "error: query must be a non-empty string"
        try:
            results = await self._remote.search(domain, query)
        except RemoteIwikiError as error:
            if error.retryable:
                raise
            return "error: wiki is unavailable"
        if not results:
            return "no results"
        lines = []
        for result in results:
            heading = result.get("heading")
            suffix = f"#{heading}" if isinstance(heading, str) and heading else ""
            lines.append(f"{result['slug']}{suffix}")
        return "\n".join(lines)

    async def _read(
        self,
        domain: str,
        question: str,
        arguments: dict,
        seen: set[tuple[str, str, int]],
        remaining_chars: int,
    ) -> str:
        slug = arguments.get("slug")
        heading = arguments.get("heading")
        part = arguments.get("part", 0)
        if not isinstance(slug, str) or not slug.strip():
            return "error: slug must be a non-empty string"
        if not isinstance(heading, str) or not heading.strip():
            return "error: heading must be a non-empty string"
        if not isinstance(part, int) or isinstance(part, bool) or part < 0:
            return "error: part must be a non-negative integer"
        key = (slug, heading, part)
        if key in seen:
            return "already provided"
        try:
            page = await self._remote.read_page(domain, slug, heading)
        except RemoteIwikiError as error:
            if error.retryable:
                raise
            return "error: wiki is unavailable"
        body = page.get("body", page.get("markdown"))
        if not isinstance(body, str) or not body:
            return "error: section is unavailable"
        seen.add(key)
        if part > 0:
            start = (part - 1) * _PART_CHARS
            chunk = body[start:start + _PART_CHARS]
            if not chunk:
                return "error: no such part"
            return chunk
        share = max(500, remaining_chars // 2)
        selection = select_context(
            [Section(slug=slug, heading=heading, body=body)], share, question
        )
        return selection.text or body[:share]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q tests/telegram_bot/test_agent.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/telegram_bot/agent.py tests/telegram_bot/test_agent.py
git commit -m "feat(telegram): add agentic tool-use retrieval loop"
```

---

### Task 4: loop rails — dedup, invalid args, overflow, wiki errors, parts

**Files:**
- Modify: `src/iwiki_mcp/telegram_bot/agent.py` (only if a test exposes a gap)
- Test: `tests/telegram_bot/test_agent.py`

**Interfaces:**
- Consumes: everything Task 3 produced.
- Produces: overflow recovery — on `InferenceError("context_overflow")` the loop replaces the oldest half of `role: "tool"` message contents with `"[dropped]"` and retries the completion once; a second overflow re-raises. This behavior is added in this task.

- [ ] **Step 1: Write the failing tests**

Append to `tests/telegram_bot/test_agent.py`:

```python
from iwiki_mcp.telegram_bot.inference import InferenceError
from iwiki_mcp.telegram_bot.iwiki import RemoteIwikiError


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
```

- [ ] **Step 2: Run tests to verify current gaps**

Run: `uv run pytest -q tests/telegram_bot/test_agent.py`
Expected: the two overflow tests FAIL (no overflow handling yet); the rest may already pass from Task 3.

- [ ] **Step 3: Implement overflow recovery**

In `AgentLoop._complete`, replace the body with:

```python
    async def _complete(self, messages, tool_choice):
        try:
            return await self._inference.complete_with_tools(
                messages, _TOOLS, tool_choice
            )
        except InferenceError as error:
            if str(error) != "context_overflow":
                raise
        # Drop the oldest half of the tool results and retry exactly once,
        # with no further wiki call.
        tool_indexes = [
            index for index, message in enumerate(messages)
            if message.get("role") == "tool"
            and message.get("content") != "[dropped]"
        ]
        if not tool_indexes:
            raise InferenceError("context_overflow")
        for index in tool_indexes[:max(1, len(tool_indexes) // 2)]:
            messages[index]["content"] = "[dropped]"
        return await self._inference.complete_with_tools(
            messages, _TOOLS, tool_choice
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q tests/telegram_bot/test_agent.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/telegram_bot/agent.py tests/telegram_bot/test_agent.py
git commit -m "feat(telegram): bound the agent loop with overflow and error rails"
```

---

### Task 5: conversation branching — questions and /create drafts

**Files:**
- Modify: `src/iwiki_mcp/telegram_bot/conversation.py`
- Test: `tests/telegram_bot/test_conversation_read.py`, `tests/telegram_bot/test_conversation_write.py`

**Interfaces:**
- Consumes: `AgentLoop` (Task 3), `inference.tools_supported` (Task 2).
- Produces: `_answer_selected` and `propose_create` use the loop when `tools_supported` is truthy, the existing path otherwise. `_agentic()` helper: `getattr(self._inference, "tools_supported", False)`. No public signature changes.

- [ ] **Step 1: Write the failing tests**

Append to `tests/telegram_bot/test_conversation_read.py` (FakeInference there has no `tools_supported`, so every existing test keeps the fallback path):

```python
from iwiki_mcp.telegram_bot.inference import ToolResponse


class AgenticFakeInference(FakeInference):
    def __init__(self):
        super().__init__()
        self.tools_supported = True

    async def complete_with_tools(self, messages, tools, tool_choice="auto"):
        self.calls.append(("complete_with_tools", tool_choice))
        return ToolResponse("Agentic answer.", ())


@pytest.mark.asyncio
async def test_agentic_inference_answers_through_the_loop(tmp_path, clock):
    remote = FakeRemote()
    inference = AgenticFakeInference()
    service = ConversationService(
        AccessPolicy(frozenset({1001})),
        remote,
        inference,
        confirmation_ttl_seconds=300,
        temporary_directory=tmp_path,
        clock=clock,
    )
    await service.select_domain(1001, "team")

    reply = await service.answer_question(1001, "How do we deploy?")

    assert reply.text == "Agentic answer."
    assert ("complete_with_tools", "auto") in inference.calls
    assert not any(call[0] == "answer" for call in inference.calls)


@pytest.mark.asyncio
async def test_fallback_inference_keeps_single_pass(service):
    await service.select_domain(1001, "team")

    reply = await service.answer_question(1001, "How do we deploy?")

    assert reply.text.startswith("Answer")
    assert any(call[0] == "answer" for call in service.inference.calls)
```

Append to `tests/telegram_bot/test_conversation_write.py` (reusing its local fakes;
adapt fixture names to the ones that file defines):

```python
from iwiki_mcp.telegram_bot.inference import ToolResponse


@pytest.mark.asyncio
async def test_agentic_create_drafts_through_the_loop(service):
    inference = service.inference
    inference.tools_supported = True

    async def complete_with_tools(messages, tools, tool_choice="auto"):
        inference.calls.append(("complete_with_tools", tool_choice))
        return ToolResponse("# Page\n\n## Overview\n\nDrafted.", ())

    inference.complete_with_tools = complete_with_tools
    await service.select_domain(1001, "team")

    preview = await service.propose_create(1001, "guide/new", "write a page")

    assert preview.text == "# Page\n\n## Overview\n\nDrafted."
    assert ("complete_with_tools", "auto") in inference.calls
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest -q tests/telegram_bot/test_conversation_read.py tests/telegram_bot/test_conversation_write.py`
Expected: the new tests FAIL (loop never invoked); existing tests PASS.

- [ ] **Step 3: Implement**

In `conversation.py`, import the loop:

```python
from .agent import AgentLoop
```

Add to `ConversationService`:

```python
    def _agentic(self) -> bool:
        return bool(getattr(self._inference, "tools_supported", False))

    def _agent_loop(self) -> AgentLoop:
        return AgentLoop(self._remote, self._inference, self._budget)
```

In `_answer_selected`, replace the body of the `try` block with a branch (keep the
except clauses exactly as they are):

```python
        try:
            if self._agentic():
                await _report(progress, _STAGE_SEARCHING)
                answer = await self._agent_loop().run(
                    domain, question, progress
                )
                return BotReply(answer)
            await _report(progress, _STAGE_SEARCHING)
            sections = await self._collect_sections(domain, question)
            ...  # existing single-pass body unchanged
```

In `propose_create`, branch the same way around context+draft:

```python
        try:
            await _report(progress, _STAGE_SEARCHING)
            if self._agentic():
                markdown = await self._agent_loop().run(
                    domain, request, progress, drafting=True
                )
            else:
                context = await self._draft_context(domain, request)
                await _report(progress, _STAGE_DRAFTING)
                markdown = await self._inference.draft_markdown(
                    request, context
                )
```

`AgentLoop.run` raises the same `RemoteIwikiError` / `InferenceError` types, so the
existing `except` clauses and user messages apply without change. `propose_update`
is not touched.

- [ ] **Step 4: Run the focused suite**

Run: `uv run pytest -q tests/telegram_bot`
Expected: PASS — including every pre-existing conversation/end-to-end test (they use
fakes without `tools_supported`, i.e. the fallback path).

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/telegram_bot/conversation.py tests/telegram_bot/test_conversation_read.py tests/telegram_bot/test_conversation_write.py
git commit -m "feat(telegram): route questions and create drafts through the agent loop"
```

---

### Task 6: runtime demotion to the single-pass pipeline

**Files:**
- Modify: `src/iwiki_mcp/telegram_bot/inference.py`, `src/iwiki_mcp/telegram_bot/conversation.py`
- Test: `tests/telegram_bot/test_conversation_read.py`

**Interfaces:**
- Consumes: `_tools_refusal` (Task 2), `_agentic()` branch (Task 5).
- Produces: `complete_with_tools` raises `InferenceError("tools_unsupported")` after setting `self.tools_supported = False` when a live tool request is refused with tool wording; `_answer_selected` and `propose_create` catch exactly that code and rerun the same request through the fallback path.

- [ ] **Step 1: Write the failing test**

Append to `tests/telegram_bot/test_conversation_read.py`:

```python
from iwiki_mcp.telegram_bot.inference import InferenceError


class DemotingFakeInference(FakeInference):
    """Refuses the first tool completion the way a live provider would."""

    def __init__(self):
        super().__init__()
        self.tools_supported = True

    async def complete_with_tools(self, messages, tools, tool_choice="auto"):
        self.tools_supported = False
        raise InferenceError("tools_unsupported")


@pytest.mark.asyncio
async def test_runtime_demotion_falls_back_within_the_same_request(
    tmp_path, clock
):
    remote = FakeRemote()
    inference = DemotingFakeInference()
    service = ConversationService(
        AccessPolicy(frozenset({1001})),
        remote,
        inference,
        confirmation_ttl_seconds=300,
        temporary_directory=tmp_path,
        clock=clock,
    )
    await service.select_domain(1001, "team")

    reply = await service.answer_question(1001, "How do we deploy?")

    # The user gets a normal answer from the fallback path, no error.
    assert reply.failed is False
    assert any(call[0] == "answer" for call in inference.calls)
    # The next question skips the loop entirely.
    inference.calls.clear()
    await service.answer_question(1001, "Second question?")
    assert not any(
        call[0] == "complete_with_tools" for call in inference.calls
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest -q tests/telegram_bot/test_conversation_read.py -k demotion`
Expected: FAIL (`tools_unsupported` surfaces as an inference failure reply)

- [ ] **Step 3: Implement**

In `inference.py`, inside `complete_with_tools`, extend the `except InferenceError`
clause before re-raising:

```python
        except InferenceError as error:
            self._record_telemetry("chat", "failure", started, {}, prompt_chars)
            if str(error) == "context_overflow":
                self._escalate_budget()
                raise
            if _tools_refusal(
                error.status, error.provider_code, error.provider_message
            ):
                LOGGER.warning(
                    "agent demoted status=%s code=%s",
                    error.status,
                    error.provider_code,
                )
                self.tools_supported = False
                raise InferenceError("tools_unsupported") from None
            raise
```

In `conversation.py` `_answer_selected`, wrap the agentic branch:

```python
            if self._agentic():
                await _report(progress, _STAGE_SEARCHING)
                try:
                    answer = await self._agent_loop().run(
                        domain, question, progress
                    )
                    return BotReply(answer)
                except InferenceError as error:
                    if str(error) != "tools_unsupported":
                        raise
                    # Demoted mid-request: serve this question single-pass.
```

(after the except, fall through to the existing single-pass body). Apply the same
pattern in `propose_create` around the drafting branch, falling through to the
context + `draft_markdown` path.

- [ ] **Step 4: Run the focused suite**

Run: `uv run pytest -q tests/telegram_bot`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/telegram_bot/inference.py src/iwiki_mcp/telegram_bot/conversation.py tests/telegram_bot/test_conversation_read.py
git commit -m "feat(telegram): demote to single-pass when a live provider refuses tools"
```

---

### Task 7: docs, version bump, full verification

**Files:**
- Modify: `docs/telegram-bot.md`, `README.md`, `docs/README.ru.md` (only the parts describing the Q&A pipeline), `pyproject.toml`

**Interfaces:**
- Consumes: the implemented behavior of Tasks 1–6.
- Produces: documentation matching the shipped behavior; version `0.7.246`.

- [ ] **Step 1: Update `docs/telegram-bot.md`**

In the Configuration section (after the context-budget paragraphs), add a subsection:

```markdown
## Agentic retrieval

When the inference provider supports OpenAI tool calling (verified by a one-token
probe at startup), questions and `/create` drafts run as an agentic loop: the model
calls `search_wiki` and `read_section` tools against the selected domain, reformulates
searches, re-reads oversized sections in parts, and writes the final answer itself,
attributing statements as `page#heading`. The loop is bounded: at most 6 tool calls,
the same derived context budget, and a forced final answer when either runs out. A
provider that refuses tool calling — at the probe or on a live request — permanently
demotes the process to the single-pass pipeline described above, with no user-visible
error. The tools are read-only; the model never selects a domain and never mutates
the wiki. The `Context: n of m sections used in full.` line is not emitted on the
agentic path.
```

- [ ] **Step 2: Update `README.md` and `docs/README.ru.md`**

Find the Telegram-bot feature bullet(s); extend them with one sentence each (English
in `README.md`, Russian in `docs/README.ru.md`) stating that with a tool-calling
provider the bot answers through an agentic search/read loop with automatic fallback
to single-pass retrieval. Keep both files equivalent.

- [ ] **Step 3: Bump the version**

In `pyproject.toml`: `version = "0.7.245"` → `version = "0.7.246"`.

- [ ] **Step 4: Full verification**

Run: `uv run flake8 src tests`
Expected: no output.
Run: `uv run pytest -q tests/telegram_bot`
Expected: PASS.
Run: `uv run pytest -q`
Expected: PASS aside from documented environment skips (PostgreSQL/deployment suites
skip without their DSNs — that is the documented environment limit, not a regression).

- [ ] **Step 5: Commit**

```bash
git add docs/telegram-bot.md README.md docs/README.ru.md pyproject.toml
git commit -m "docs(telegram): document agentic retrieval and bump version to 0.7.246"
```

---

## Acceptance evidence (run after all tasks, before /check-chain result)

- Fallback outcome: `test_probe_demotes_when_provider_refuses_tools`,
  `test_fallback_inference_keeps_single_pass`, and
  `test_runtime_demotion_falls_back_within_the_same_request` are the executable
  demonstration of the no-tool-calling outcome.
- Multi-iteration outcome: `test_loop_searches_reads_and_answers` plus the progress
  assertions demonstrate the observable loop; live-domain benchmark (5 questions,
  spec §10 rubric) is a manual step recorded on the task page.
- Latency bound: `test_progress_reports_iterations` shows a simple question completes
  in 2 iterations (1 search + answer).
