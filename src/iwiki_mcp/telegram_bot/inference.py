"""OpenAI-compatible text and audio inference client."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
import time
from typing import Any

import anyio
import httpx

from .context import ContextBudget


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


LOGGER = logging.getLogger(__name__)
# Keep in sync with config.BotConfig.max_output_tokens.
_DEFAULT_MAX_OUTPUT_TOKENS = 1024
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
# Keep in sync with config.BotConfig.inference_timeout_seconds.
_DEFAULT_TIMEOUT_SECONDS = 180.0
_CONNECT_TIMEOUT_SECONDS = 10.0
# One retry covers the two transient failures this bot actually sees: a read
# timeout on a long completion and a keep-alive connection the provider closed
# between requests.
_DEFAULT_MAX_ATTEMPTS = 2
_RETRY_BASE_DELAY_SECONDS = 0.5
_USAGE_FIELDS = frozenset({
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "input_tokens",
    "output_tokens",
    "audio_seconds",
    "duration",
    "seconds",
})


class InferenceError(RuntimeError):
    """A sanitized inference failure."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        status: int | None = None,
        path: str | None = None,
        provider_code: str | None = None,
        provider_message: str | None = None,
    ) -> None:
        super().__init__(code)
        self.retryable = retryable
        self.status = status
        self.path = path
        self.provider_code = provider_code
        self.provider_message = provider_message


def _provider_error_fields(
    response: httpx.Response,
) -> tuple[str | None, str | None]:
    """Read the provider error code and message from a JSON error body."""
    try:
        payload = response.json()
    except ValueError:
        return None, None
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return None, None
    code = next(
        (
            error[key]
            for key in ("code", "type")
            if isinstance(error.get(key), str) and error[key]
        ),
        None,
    )
    message = error.get("message")
    return code, message if isinstance(message, str) else None


_CONTEXT_OVERFLOW_CODES = frozenset({
    "context_length_exceeded",
    "string_above_max_length",
})
# Providers word the same refusal differently: OpenAI says the request exceeds
# the context, vLLM and llama.cpp say the maximum context length is N tokens and
# ask for a shorter prompt.
_CONTEXT_OVERFLOW_PHRASES = (
    "maximum context length",
    "context window",
    "reduce the length",
    "too many tokens",
    "input is too long",
    "prompt is too long",
)


def _is_context_overflow(code: str | None, message: str | None) -> bool:
    if code in _CONTEXT_OVERFLOW_CODES:
        return True
    lowered = (message or "").lower()
    if "context" in lowered and "exceed" in lowered:
        return True
    return any(phrase in lowered for phrase in _CONTEXT_OVERFLOW_PHRASES)


def _retryable_http_error(error: httpx.HTTPError) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        return status == 429 or 500 <= status < 600
    return isinstance(
        error,
        (
            httpx.NetworkError,
            httpx.TimeoutException,
            httpx.RemoteProtocolError,
        ),
    )


class InferenceClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        chat_model: str,
        transcription_model: str,
        http: httpx.AsyncClient | None = None,
        max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
        budget: ContextBudget | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._chat_model = chat_model
        self._transcription_model = transcription_model
        self._max_output_tokens = max_output_tokens
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._budget = budget
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=_CONNECT_TIMEOUT_SECONDS,
                read=timeout_seconds,
                write=timeout_seconds,
                pool=_CONNECT_TIMEOUT_SECONDS,
            ),
            trust_env=False,
        )
        self.tools_supported = False

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def probe(self) -> None:
        retryable = None
        try:
            response = await self._http.get(
                f"{self._base_url}/models",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as error:
            retryable = _retryable_http_error(error)
        except (httpx.InvalidURL, ValueError):
            retryable = False
        if retryable is not None:
            raise InferenceError(
                "inference_failed", retryable=retryable
            ) from None
        data = payload.get("data") if isinstance(payload, dict) else None
        available = (
            {item.get("id") for item in data if isinstance(item, dict)}
            if isinstance(data, list)
            else set()
        )
        for role, model in (
            ("chat", self._chat_model),
            ("transcription", self._transcription_model),
        ):
            if model not in available:
                LOGGER.warning("configured model unavailable role=%s", role)
                raise InferenceError("configured_model_unavailable")
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

    async def answer(self, question: str, context: str) -> str:
        return await self._complete(
            "Answer only from the supplied wiki context.", question, context
        )

    async def draft_markdown(self, request: str, context: str) -> str:
        return await self._complete(
            "Produce Markdown only for the requested wiki change.", request, context
        )

    async def _complete(self, instruction: str, request: str, context: str) -> str:
        started = time.monotonic()
        invalid_response = False
        prompt_chars = len(instruction) + len(request) + len(context)
        try:
            payload = await self._post_json(
                "/chat/completions",
                json={
                    "model": self._chat_model,
                    "messages": [
                        {"role": "system", "content": instruction},
                        {
                            "role": "user",
                            "content": f"Request:\n{request}\n\nWiki context:\n{context}",
                        },
                    ],
                    "temperature": 0,
                    "max_tokens": self._max_output_tokens,
                },
            )
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise InferenceError("invalid_inference_response")
        except (KeyError, IndexError, TypeError):
            self._record_telemetry("chat", "failure", started, {}, prompt_chars)
            invalid_response = True
        except InferenceError as error:
            self._record_telemetry("chat", "failure", started, {}, prompt_chars)
            if str(error) == "context_overflow":
                self._escalate_budget()
            raise
        if invalid_response:
            raise InferenceError("invalid_inference_response") from None
        self._record_telemetry("chat", "success", started, payload, prompt_chars)
        self._observe_usage(payload, prompt_chars)
        return content

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

    def _observe_usage(
        self, payload: dict[str, object], prompt_chars: int
    ) -> None:
        """Calibrate the context budget from the reported prompt usage."""
        if self._budget is None:
            return
        usage = payload.get("usage")
        tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        if isinstance(tokens, int) and not isinstance(tokens, bool):
            self._budget.observe(prompt_chars, tokens)

    def _escalate_budget(self) -> None:
        """Assume a denser prompt after the provider refused this one."""
        if self._budget is not None:
            self._budget.escalate()

    async def transcribe(self, filename: str, audio: bytes) -> str:
        started = time.monotonic()
        try:
            payload = await self._post_json(
                "/audio/transcriptions",
                data={"model": self._transcription_model},
                files={"file": (filename, audio, "audio/wav")},
            )
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                raise InferenceError("invalid_inference_response")
        except InferenceError:
            self._record_telemetry("transcription", "failure", started, {})
            raise
        self._record_telemetry("transcription", "success", started, payload)
        return text

    @staticmethod
    def _record_telemetry(
        operation: str,
        outcome: str,
        started: float,
        payload: dict[str, object],
        prompt_chars: int | None = None,
    ) -> None:
        raw_usage = payload.get("usage")
        usage = {}
        if isinstance(raw_usage, dict):
            usage = {
                key: value
                for key, value in raw_usage.items()
                if key in _USAGE_FIELDS
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            }
        LOGGER.info(
            "inference operation completed",
            extra={
                "operation": operation,
                "outcome": outcome,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "usage": usage,
                "prompt_chars": prompt_chars,
            },
        )

    @staticmethod
    def _request_failure(
        error: httpx.HTTPError | None, path: str, started: float
    ) -> InferenceError:
        """Classify and record one failed inference request."""
        status = None
        provider_code = None
        message = None
        if isinstance(error, httpx.HTTPStatusError):
            status = error.response.status_code
            provider_code, message = _provider_error_fields(error.response)
        if _is_context_overflow(provider_code, message):
            code, retryable = "context_overflow", False
        else:
            code = "inference_failed"
            retryable = error is not None and _retryable_http_error(error)
        LOGGER.warning(
            "inference request failed status=%s path=%s code=%s retryable=%s "
            "elapsed_ms=%s",
            status,
            path,
            provider_code or code,
            retryable,
            int((time.monotonic() - started) * 1000),
        )
        return InferenceError(
            code,
            retryable=retryable,
            status=status,
            path=path,
            provider_code=provider_code,
            provider_message=message,
        )

    async def _post_json(self, path: str, **kwargs: Any) -> dict[str, object]:
        """Post one inference request, retrying only transient failures."""
        attempt = 0
        while True:
            started = time.monotonic()
            failure = None
            try:
                response = await self._http.post(
                    f"{self._base_url}{path}",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    **kwargs,
                )
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPError as error:
                failure = self._request_failure(error, path, started)
            except (httpx.InvalidURL, ValueError):
                failure = self._request_failure(None, path, started)
            if failure is None:
                break
            attempt += 1
            if not failure.retryable or attempt >= self._max_attempts:
                raise failure from None
            LOGGER.warning(
                "inference request retry path=%s attempt=%s status=%s",
                path,
                attempt,
                failure.status,
            )
            await self._sleep(_RETRY_BASE_DELAY_SECONDS * attempt)
        if not isinstance(payload, dict):
            raise InferenceError("invalid_inference_response")
        return payload
