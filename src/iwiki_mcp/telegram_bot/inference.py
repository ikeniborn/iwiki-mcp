"""OpenAI-compatible text and audio inference client."""

import logging
import time
from typing import Any

import httpx


LOGGER = logging.getLogger(__name__)
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

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.retryable = retryable


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
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._chat_model = chat_model
        self._transcription_model = transcription_model
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
        if not isinstance(data, list) or self._chat_model not in {
            item.get("id") for item in data if isinstance(item, dict)
        }:
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
                files={"file": (filename, audio, "audio/ogg")},
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

    async def _post_json(self, path: str, **kwargs: Any) -> dict[str, object]:
        retryable = None
        try:
            response = await self._http.post(
                f"{self._base_url}{path}",
                headers={"Authorization": f"Bearer {self._api_key}"},
                **kwargs,
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
        if not isinstance(payload, dict):
            raise InferenceError("invalid_inference_response")
        return payload
