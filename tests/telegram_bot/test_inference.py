import json
import logging
import traceback

import httpx
import pytest

import iwiki_mcp.telegram_bot.inference as inference_module
from iwiki_mcp.telegram_bot.inference import InferenceClient, InferenceError


def assert_sanitized_error(captured, marker):
    formatted = "".join(
        traceback.format_exception(captured.type, captured.value, captured.tb)
    )
    assert marker not in formatted
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.asyncio
async def test_probe_requires_configured_chat_model():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"id": "chat-model"}]})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = InferenceClient(
        "https://models.example/v1", "key", "chat-model", "audio-model", http
    )

    await client.probe()

    assert seen == {
        "method": "GET",
        "url": "https://models.example/v1/models",
    }
    await http.aclose()


@pytest.mark.asyncio
async def test_probe_rejects_missing_chat_model():
    def handler(request):
        return httpx.Response(200, json={"data": [{"id": "other-model"}]})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = InferenceClient(
        "https://models.example/v1", "key", "chat-model", "audio-model", http
    )

    with pytest.raises(InferenceError, match="configured_model_unavailable") as captured:
        await client.probe()

    assert captured.value.retryable is False
    await http.aclose()


@pytest.mark.asyncio
async def test_probe_transport_failure_has_no_private_exception_chain():
    marker = "probe-provider-url-key-marker"

    class FailingHttp:
        async def get(self, *args, **kwargs):
            raise httpx.ConnectError(marker)

    client = InferenceClient(
        "https://models.example/v1",
        "key",
        "chat-model",
        "audio-model",
        FailingHttp(),
    )

    with pytest.raises(InferenceError) as captured:
        await client.probe()

    assert captured.value.retryable is True
    assert_sanitized_error(captured, marker)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "retryable"),
    ((401, False), (403, False), (429, True), (500, True)),
)
async def test_probe_http_status_retryability(status, retryable):
    def handler(request):
        return httpx.Response(status, text="provider-response-marker")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = InferenceClient(
        "https://models.example/v1", "key", "chat-model", "audio-model", http
    )

    with pytest.raises(InferenceError) as captured:
        await client.probe()

    assert captured.value.retryable is retryable
    await http.aclose()


@pytest.mark.asyncio
async def test_answer_posts_only_question_and_selected_context():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["json"] = json.loads(request.content)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Answer"}}]}
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = InferenceClient(
        "https://models.example/v1", "key", "chat-model", "audio-model", http
    )

    assert await client.answer("Question", "Selected context") == "Answer"
    assert seen["url"] == "https://models.example/v1/chat/completions"
    assert seen["json"]["model"] == "chat-model"
    assert "Question" in seen["json"]["messages"][1]["content"]
    assert "Selected context" in seen["json"]["messages"][1]["content"]

    await http.aclose()


@pytest.mark.asyncio
async def test_answer_emits_content_free_usage_telemetry(caplog):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "private answer"}}],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 3,
                    "total_tokens": 15,
                },
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = InferenceClient(
        "https://models.example/v1", "key", "chat-model", "audio-model", http
    )

    with caplog.at_level(logging.INFO, logger="iwiki_mcp.telegram_bot.inference"):
        await client.answer("private question", "private context")

    record = caplog.records[-1]
    assert record.operation == "chat"
    assert record.outcome == "success"
    assert record.elapsed_ms >= 0
    assert record.usage == {
        "prompt_tokens": 12,
        "completion_tokens": 3,
        "total_tokens": 15,
    }
    assert "private" not in record.getMessage()
    await http.aclose()


@pytest.mark.asyncio
async def test_draft_markdown_uses_same_chat_contract():
    seen = {}

    def handler(request):
        seen["json"] = json.loads(request.content)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "# Draft"}}]}
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = InferenceClient(
        "https://models.example/v1", "key", "chat-model", "audio-model", http
    )

    assert await client.draft_markdown("Create page", "Target context") == "# Draft"
    assert "Markdown" in seen["json"]["messages"][0]["content"]

    await http.aclose()


@pytest.mark.asyncio
async def test_transcription_posts_ogg_multipart_contract():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = request.content
        seen["content_type"] = request.headers["content-type"]
        return httpx.Response(200, json={"text": "spoken question"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = InferenceClient(
        "https://models.example/v1", "key", "chat-model", "audio-model", http
    )

    assert await client.transcribe("voice.ogg", b"audio-bytes") == "spoken question"
    assert seen["url"] == "https://models.example/v1/audio/transcriptions"
    assert seen["content_type"].startswith("multipart/form-data;")
    assert b'audio-model' in seen["body"]
    assert b'filename="voice.ogg"' in seen["body"]
    assert b"audio/ogg" in seen["body"]
    assert b"audio-bytes" in seen["body"]

    await http.aclose()


@pytest.mark.asyncio
async def test_http_failure_is_sanitized():
    def handler(request):
        return httpx.Response(500, text="provider secret detail")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = InferenceClient(
        "https://models.example/v1", "key", "chat-model", "audio-model", http
    )

    with pytest.raises(InferenceError) as captured:
        await client.answer("Question", "Context")

    assert str(captured.value) == "inference_failed"
    assert "secret" not in str(captured.value)
    await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ("post", "json", "schema"))
async def test_completion_failures_have_no_private_exception_chain(failure_stage):
    marker = f"{failure_stage}-provider-response-marker"

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            if failure_stage == "json":
                raise ValueError(marker)
            if failure_stage == "schema":
                class InvalidPayload(dict):
                    def __getitem__(self, key):
                        raise KeyError(marker)

                return InvalidPayload()
            return {}

    class FailingHttp:
        async def post(self, *args, **kwargs):
            if failure_stage == "post":
                raise httpx.ConnectError(marker)
            return Response()

    client = InferenceClient(
        "https://models.example/v1",
        "key",
        "chat-model",
        "audio-model",
        FailingHttp(),
    )

    with pytest.raises(InferenceError) as captured:
        await client.answer("Question", "Context")

    if failure_stage in {"json", "schema"}:
        assert captured.value.retryable is False
    assert_sanitized_error(captured, marker)


@pytest.mark.asyncio
async def test_empty_completion_is_rejected():
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = InferenceClient(
        "https://models.example/v1", "key", "chat-model", "audio-model", http
    )

    with pytest.raises(InferenceError, match="invalid_inference_response"):
        await client.answer("Question", "Context")

    await http.aclose()


@pytest.mark.asyncio
async def test_close_closes_internally_owned_http_client():
    client = InferenceClient(
        "https://models.example/v1", "key", "chat-model", "audio-model"
    )

    await client.close()

    assert client._http.is_closed is True


def test_internal_http_client_ignores_environment_proxies(monkeypatch):
    seen = {}

    class RecordingAsyncClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setenv("HTTP_PROXY", "http://environment-proxy-marker:8000")
    monkeypatch.setenv("HTTPS_PROXY", "http://environment-proxy-marker:8001")
    monkeypatch.setenv("ALL_PROXY", "socks5://environment-proxy-marker:1080")
    monkeypatch.setenv("NO_PROXY", "models.example")
    monkeypatch.setattr(inference_module.httpx, "AsyncClient", RecordingAsyncClient)

    InferenceClient(
        "https://models.example/v1", "key", "chat-model", "audio-model"
    )

    assert seen == {"timeout": 60, "trust_env": False}


def test_injected_http_client_is_used_without_constructing_another(monkeypatch):
    injected = object()

    def unexpected_client(**kwargs):
        raise AssertionError("an injected client must be used")

    monkeypatch.setattr(inference_module.httpx, "AsyncClient", unexpected_client)

    client = InferenceClient(
        "https://models.example/v1",
        "key",
        "chat-model",
        "audio-model",
        injected,
    )

    assert client._http is injected
