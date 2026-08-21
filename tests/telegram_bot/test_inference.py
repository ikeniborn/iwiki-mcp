import json

import httpx
import pytest

from iwiki_mcp.telegram_bot.inference import InferenceClient, InferenceError


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

    with pytest.raises(InferenceError, match="configured_model_unavailable"):
        await client.probe()

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
