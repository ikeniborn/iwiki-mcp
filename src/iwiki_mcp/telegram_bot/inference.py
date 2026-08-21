"""OpenAI-compatible text and audio inference client."""

from typing import Any

import httpx


class InferenceError(RuntimeError):
    """A sanitized inference failure."""


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
        self._http = http or httpx.AsyncClient(timeout=60)

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def probe(self) -> None:
        try:
            response = await self._http.get(
                f"{self._base_url}/models",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise InferenceError("inference_failed") from exc
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
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise InferenceError("invalid_inference_response") from exc
        if not isinstance(content, str) or not content.strip():
            raise InferenceError("invalid_inference_response")
        return content

    async def transcribe(self, filename: str, audio: bytes) -> str:
        payload = await self._post_json(
            "/audio/transcriptions",
            data={"model": self._transcription_model},
            files={"file": (filename, audio, "audio/ogg")},
        )
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise InferenceError("invalid_inference_response")
        return text

    async def _post_json(self, path: str, **kwargs: Any) -> dict[str, object]:
        try:
            response = await self._http.post(
                f"{self._base_url}{path}",
                headers={"Authorization": f"Bearer {self._api_key}"},
                **kwargs,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise InferenceError("inference_failed") from exc
        if not isinstance(payload, dict):
            raise InferenceError("invalid_inference_response")
        return payload
