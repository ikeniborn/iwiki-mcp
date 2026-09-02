"""OpenAI-compatible text and audio inference client."""

import logging
import time
from typing import Any

import httpx


LOGGER = logging.getLogger(__name__)
# Keep in sync with config.BotConfig.max_output_tokens.
_DEFAULT_MAX_OUTPUT_TOKENS = 1024
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
    ) -> None:
        super().__init__(code)
        self.retryable = retryable
        self.status = status
        self.path = path
        self.provider_code = provider_code


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


def _is_context_overflow(code: str | None, message: str | None) -> bool:
    if code == "context_length_exceeded":
        return True
    lowered = (message or "").lower()
    return "context" in lowered and "exceed" in lowered


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
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._chat_model = chat_model
        self._transcription_model = transcription_model
        self._max_output_tokens = max_output_tokens
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=60, trust_env=False)

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
            self._record_telemetry("chat", "failure", started, {})
            invalid_response = True
        except InferenceError:
            self._record_telemetry("chat", "failure", started, {})
            raise
        if invalid_response:
            raise InferenceError("invalid_inference_response") from None
        self._record_telemetry("chat", "success", started, payload)
        return content

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
            "inference request failed status=%s path=%s code=%s elapsed_ms=%s",
            status,
            path,
            provider_code or code,
            int((time.monotonic() - started) * 1000),
        )
        return InferenceError(
            code,
            retryable=retryable,
            status=status,
            path=path,
            provider_code=provider_code,
        )

    async def _post_json(self, path: str, **kwargs: Any) -> dict[str, object]:
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
        if failure is not None:
            raise failure from None
        if not isinstance(payload, dict):
            raise InferenceError("invalid_inference_response")
        return payload
