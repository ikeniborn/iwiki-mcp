"""LLM-driven tool-use retrieval loop over the remote wiki."""

import json

from .context import ContextBudget, Section, select_context
from .inference import InferenceError
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
        forced_retries = 0
        while True:
            over_budget = _transcript_chars(messages) >= limit_chars
            if not forced and (calls_used >= _MAX_TOOL_CALLS or over_budget):
                messages.append({"role": "user", "content": _FORCE_ANSWER})
                forced = True
            if progress is not None and (forced or calls_used):
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
                if forced_retries >= 1:
                    raise InferenceError("invalid_inference_response")
                forced_retries += 1
                messages.append({"role": "user", "content": _FORCE_ANSWER})
                continue
            for call in response.tool_calls:
                if calls_used >= _MAX_TOOL_CALLS:
                    break
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
