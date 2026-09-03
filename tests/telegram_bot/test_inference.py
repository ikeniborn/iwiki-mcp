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
async def test_probe_requires_both_configured_models():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200, json={"data": [{"id": "chat-model"}, {"id": "audio-model"}]}
            )
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
        })

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = InferenceClient(
        "https://models.example/v1", "key", "chat-model", "audio-model", http
    )

    await client.probe()

    assert seen["method"] == "POST"
    assert "chat/completions" in seen["url"]
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
async def test_probe_unsupported_protocol_is_not_retryable():
    client = InferenceClient(
        "provider-without-scheme",
        "key",
        "chat-model",
        "audio-model",
    )

    with pytest.raises(InferenceError) as captured:
        await client.probe()

    assert captured.value.retryable is False
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("probe", "post"))
async def test_invalid_url_is_sanitized_and_not_retryable(operation):
    marker = "invalid-port-marker"
    client = InferenceClient(
        f"https://provider.example:{marker}/v1",
        "key",
        "chat-model",
        "audio-model",
    )

    try:
        with pytest.raises(InferenceError) as captured:
            if operation == "probe":
                await client.probe()
            else:
                await client.answer("Question", "Context")
    finally:
        await client.close()

    assert str(captured.value) == "inference_failed"
    assert captured.value.retryable is False
    assert_sanitized_error(captured, marker)


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
async def test_transcription_posts_wav_multipart_contract():
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

    wav = b"RIFF\x24\x00\x00\x00WAVEaudio-bytes"

    assert await client.transcribe("audio.wav", wav) == "spoken question"
    assert seen["url"] == "https://models.example/v1/audio/transcriptions"
    assert seen["content_type"].startswith("multipart/form-data;")
    assert b'audio-model' in seen["body"]
    assert b'filename="audio.wav"' in seen["body"]
    assert b"audio/wav" in seen["body"]
    assert wav in seen["body"]

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

    assert seen["trust_env"] is False
    assert seen["timeout"] == httpx.Timeout(
        connect=10.0, read=180.0, write=180.0, pool=10.0
    )


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


@pytest.mark.asyncio
async def test_probe_requires_configured_transcription_model(caplog):
    def handler(request):
        return httpx.Response(200, json={"data": [{"id": "chat-model"}]})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = InferenceClient(
        "https://models.example/v1", "key", "chat-model", "audio-model", http
    )

    with caplog.at_level(logging.WARNING, logger="iwiki_mcp.telegram_bot.inference"):
        with pytest.raises(InferenceError) as captured:
            await client.probe()

    assert str(captured.value) == "configured_model_unavailable"
    assert "transcription" in caplog.records[-1].getMessage()
    await http.aclose()


@pytest.mark.asyncio
async def test_completion_bounds_the_output_budget():
    seen = {}

    def handler(request):
        seen["json"] = json.loads(request.content)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Answer"}}]}
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = InferenceClient(
        "https://models.example/v1",
        "key",
        "chat-model",
        "audio-model",
        http,
        max_output_tokens=256,
    )

    await client.answer("Question", "Context")

    assert seen["json"]["max_tokens"] == 256
    await http.aclose()


@pytest.mark.asyncio
async def test_context_overflow_behind_a_gateway_status_is_not_retryable(caplog):
    attempts = []

    def handler(request):
        attempts.append(str(request.url))
        return httpx.Response(
            502,
            json={
                "error": {
                    "code": "context_length_exceeded",
                    "message": "request (41839 tokens) exceeds private-context",
                }
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = InferenceClient(
        "https://models.example/v1", "key", "chat-model", "audio-model", http
    )

    with caplog.at_level(logging.WARNING, logger="iwiki_mcp.telegram_bot.inference"):
        with pytest.raises(InferenceError) as captured:
            await client.answer("private question", "private context")

    assert str(captured.value) == "context_overflow"
    assert captured.value.retryable is False
    assert captured.value.status == 502
    assert captured.value.path == "/chat/completions"
    assert captured.value.provider_code == "context_length_exceeded"
    assert len(attempts) == 1
    message = caplog.records[0].getMessage()
    assert "502" in message
    assert "/chat/completions" in message
    assert "context_length_exceeded" in message
    assert "private" not in message
    await http.aclose()


@pytest.mark.asyncio
async def test_overflow_named_only_by_the_provider_message_is_not_retryable():
    def handler(request):
        return httpx.Response(
            500,
            json={
                "error": {
                    "message": "This model's maximum context length is exceeded"
                }
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = InferenceClient(
        "https://models.example/v1", "key", "chat-model", "audio-model", http
    )

    with pytest.raises(InferenceError) as captured:
        await client.answer("Question", "Context")

    assert str(captured.value) == "context_overflow"
    assert captured.value.retryable is False
    await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    (
        "This model's maximum context length is 32768 tokens. However, you "
        "requested 40100 tokens. Please reduce the length of the messages.",
        "the prompt is too long for the context window",
    ),
)
async def test_provider_wording_without_exceed_is_still_context_overflow(message):
    def handler(request):
        return httpx.Response(400, json={"error": {"message": message}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = InferenceClient(
        "https://models.example/v1", "key", "chat-model", "audio-model", http
    )

    with pytest.raises(InferenceError) as captured:
        await client.answer("Question", "Context")

    assert str(captured.value) == "context_overflow"
    assert captured.value.retryable is False
    await http.aclose()


@pytest.mark.asyncio
async def test_transient_failure_is_retried_once_before_the_answer():
    attempts = []
    delays = []

    def handler(request):
        attempts.append(str(request.url))
        if len(attempts) == 1:
            raise httpx.RemoteProtocolError("server disconnected")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Answer"}}]}
        )

    async def sleep(delay):
        delays.append(delay)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = InferenceClient(
        "https://models.example/v1",
        "key",
        "chat-model",
        "audio-model",
        http,
        sleep=sleep,
    )

    assert await client.answer("Question", "Context") == "Answer"
    assert len(attempts) == 2
    assert delays == [0.5]
    await http.aclose()


@pytest.mark.asyncio
async def test_transcription_retry_stops_at_the_attempt_budget():
    attempts = []

    def handler(request):
        attempts.append(str(request.url))
        return httpx.Response(503, text="provider unavailable")

    async def sleep(delay):
        pass

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = InferenceClient(
        "https://models.example/v1",
        "key",
        "chat-model",
        "audio-model",
        http,
        sleep=sleep,
    )

    with pytest.raises(InferenceError) as captured:
        await client.transcribe("audio.wav", b"RIFF")

    assert str(captured.value) == "inference_failed"
    assert len(attempts) == 2
    await http.aclose()


@pytest.mark.asyncio
async def test_permanent_failure_is_not_retried():
    attempts = []

    def handler(request):
        attempts.append(str(request.url))
        return httpx.Response(401, text="unauthorized")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = InferenceClient(
        "https://models.example/v1", "key", "chat-model", "audio-model", http
    )

    with pytest.raises(InferenceError):
        await client.answer("Question", "Context")

    assert len(attempts) == 1
    await http.aclose()


@pytest.mark.asyncio
async def test_ordinary_server_failure_stays_retryable_and_is_recorded(caplog):
    def handler(request):
        return httpx.Response(500, text="provider secret detail")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = InferenceClient(
        "https://models.example/v1", "key", "chat-model", "audio-model", http
    )

    with caplog.at_level(logging.WARNING, logger="iwiki_mcp.telegram_bot.inference"):
        with pytest.raises(InferenceError) as captured:
            await client.answer("Question", "Context")

    assert str(captured.value) == "inference_failed"
    assert captured.value.retryable is True
    assert captured.value.status == 500
    assert captured.value.provider_code is None
    message = caplog.records[0].getMessage()
    assert "500" in message
    assert "secret" not in message
    await http.aclose()


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
