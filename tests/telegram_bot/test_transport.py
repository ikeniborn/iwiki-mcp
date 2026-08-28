import pytest
from contextlib import asynccontextmanager
import json
import traceback

import anyio
import urllib3

import iwiki_mcp.telegram_bot.main as main_module
from iwiki_mcp.telegram_bot.access import AccessPolicy
from iwiki_mcp.telegram_bot.config import BotConfig, TelegramProxyConfig
from iwiki_mcp.telegram_bot.inference import InferenceError
from iwiki_mcp.telegram_bot.main import main
from iwiki_mcp.telegram_bot.models import BotReply, WritePreview
from iwiki_mcp.telegram_bot.proxy import ProxyResponse, TelegramProxyClient
from iwiki_mcp.telegram_bot.transport import TelegramError, TelegramTransport


class RecordingHttp:
    def __init__(self, post_results=(), get_results=()):
        self.post_results = list(post_results)
        self.get_results = list(get_results)
        self.calls = []

    async def post_json(self, url, payload):
        self.calls.append(("POST", url, payload))
        result = self.post_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def get_bytes(self, url):
        self.calls.append(("GET", url))
        result = self.get_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def close(self):
        self.calls.append(("CLOSE",))


class FakeConversation:
    def __init__(self):
        self.calls = []

    def expire_state(self):
        self.calls.append(("expire_state",))

    async def list_domains(self, telegram_id):
        self.calls.append(("list_domains", telegram_id))
        return BotReply("Available domains:", ("domain:team",))

    async def select_domain(self, telegram_id, domain):
        self.calls.append(("select_domain", telegram_id, domain))
        return BotReply(f"Selected domain: {domain}")

    async def answer_question(self, telegram_id, text):
        self.calls.append(("answer_question", telegram_id, text))
        return BotReply("Answer")

    async def answer_voice(self, telegram_id, filename, audio):
        self.calls.append(("answer_voice", telegram_id, filename, audio))
        return BotReply("Voice answer")

    async def propose_create(self, telegram_id, slug, request):
        self.calls.append(("propose_create", telegram_id, slug, request))
        return WritePreview("nonce", "# Draft")

    async def propose_update(self, telegram_id, slug, heading, request):
        self.calls.append(
            ("propose_update", telegram_id, slug, heading, request)
        )
        return WritePreview("nonce", "Updated section")

    async def confirm_write(self, telegram_id, token):
        self.calls.append(("confirm_write", telegram_id, token))
        return BotReply("Page change saved.")

    async def reject_write(self, telegram_id, token):
        self.calls.append(("reject_write", telegram_id, token))
        return BotReply("Change rejected.")


class FakeTransport(TelegramTransport):
    def __init__(self, conversation):
        super().__init__(
            "telegram-token",
            AccessPolicy(frozenset({1001})),
            conversation,
            RecordingHttp(),
        )
        self.api_calls = []

    async def _api(self, method, data):
        self.api_calls.append((method, data))
        return [] if method == "getUpdates" else {}

    async def _download_voice(self, file_id):
        self.api_calls.append(("downloadVoice", {"file_id": file_id}))
        return "voice.ogg", b"audio"


@pytest.fixture
def transport():
    conversation = FakeConversation()
    value = FakeTransport(conversation)
    value.conversation = conversation
    return value


@pytest.mark.asyncio
async def test_unknown_sender_never_reaches_conversation_service(transport):
    await transport.handle_update(
        {"message": {"from": {"id": 2002}, "chat": {"id": 9}, "text": "/domains"}}
    )

    assert transport.conversation.calls == []
    assert transport.api_calls == [
        ("sendMessage", {"chat_id": 9, "text": "Access denied."})
    ]


@pytest.mark.asyncio
async def test_domains_command_renders_selection_button(transport):
    await transport.handle_update(
        {"message": {"from": {"id": 1001}, "chat": {"id": 9}, "text": "/domains"}}
    )

    assert transport.conversation.calls == [("list_domains", 1001)]
    method, payload = transport.api_calls[-1]
    assert method == "sendMessage"
    assert payload["reply_markup"]["inline_keyboard"] == [
        [{"text": "team", "callback_data": "domain:team"}]
    ]


@pytest.mark.asyncio
async def test_domain_callback_selects_domain(transport):
    await transport.handle_update(
        {
            "callback_query": {
                "id": "callback-1",
                "from": {"id": 1001},
                "message": {"chat": {"id": 9}},
                "data": "domain:team",
            }
        }
    )

    assert ("select_domain", 1001, "team") in transport.conversation.calls


@pytest.mark.asyncio
async def test_confirm_callback_calls_confirm_write(transport):
    await transport.handle_update(
        {
            "callback_query": {
                "id": "callback-1",
                "from": {"id": 1001},
                "message": {"chat": {"id": 9}},
                "data": "confirm:nonce",
            }
        }
    )

    assert ("confirm_write", 1001, "nonce") in transport.conversation.calls


@pytest.mark.asyncio
async def test_create_command_renders_token_bound_confirmation(transport):
    await transport.handle_update(
        {
            "message": {
                "from": {"id": 1001},
                "chat": {"id": 9},
                "text": "/create runbook: deployment steps",
            }
        }
    )

    assert ("propose_create", 1001, "runbook", "deployment steps") in (
        transport.conversation.calls
    )
    payload = transport.api_calls[-1][1]
    assert payload["reply_markup"]["inline_keyboard"] == [
        [
            {"text": "Confirm", "callback_data": "confirm:nonce"},
            {"text": "Reject", "callback_data": "reject:nonce"},
        ]
    ]


@pytest.mark.asyncio
async def test_voice_message_is_downloaded_and_dispatched(transport):
    await transport.handle_update(
        {
            "message": {
                "from": {"id": 1001},
                "chat": {"id": 9},
                "voice": {"file_id": "voice-file"},
            }
        }
    )

    assert ("answer_voice", 1001, "voice.ogg", b"audio") in (
        transport.conversation.calls
    )


@pytest.mark.asyncio
async def test_poll_once_advances_offset(transport):
    async def updates(method, data):
        assert method == "getUpdates"
        return [{"update_id": 41}, {"update_id": 43}]

    transport._api = updates

    assert await transport.poll_once(None) == 44
    assert transport.conversation.calls == [("expire_state",)]


@pytest.mark.asyncio
async def test_all_telegram_request_classes_use_injected_adapter_and_fixed_origins():
    token = "fixed-token"
    callback_update = {
        "update_id": 40,
        "callback_query": {
            "id": "callback-1",
            "from": {"id": 1001},
            "message": {"chat": {"id": 9}},
            "data": "domain:team",
        },
    }
    voice_update = {
        "update_id": 41,
        "message": {
            "from": {"id": 1001},
            "chat": {"id": 9},
            "voice": {"file_id": "voice-file"},
        },
    }
    http = RecordingHttp(
        post_results=(
            ProxyResponse(
                200,
                json.dumps(
                    {"ok": True, "result": [callback_update, voice_update]}
                ).encode(),
            ),
            ProxyResponse(200, b'{"ok":true,"result":{}}'),
            ProxyResponse(200, b'{"ok":true,"result":{}}'),
            ProxyResponse(
                200,
                json.dumps(
                    {"ok": True, "result": {"file_path": "voice/file.ogg"}}
                ).encode(),
            ),
            ProxyResponse(200, b'{"ok":true,"result":{}}'),
        ),
        get_results=(ProxyResponse(200, b"audio"),),
    )
    conversation = FakeConversation()
    transport = TelegramTransport(
        token,
        AccessPolicy(frozenset({1001})),
        conversation,
        http,
    )

    assert await transport.poll_once(None) == 42

    assert http.calls == [
        (
            "POST",
            f"https://api.telegram.org/bot{token}/getUpdates",
            {"timeout": 30},
        ),
        (
            "POST",
            f"https://api.telegram.org/bot{token}/answerCallbackQuery",
            {"callback_query_id": "callback-1"},
        ),
        (
            "POST",
            f"https://api.telegram.org/bot{token}/sendMessage",
            {"chat_id": 9, "text": "Selected domain: team"},
        ),
        (
            "POST",
            f"https://api.telegram.org/bot{token}/getFile",
            {"file_id": "voice-file"},
        ),
        (
            "GET",
            f"https://api.telegram.org/file/bot{token}/voice/file.ogg",
        ),
        (
            "POST",
            f"https://api.telegram.org/bot{token}/sendMessage",
            {"chat_id": 9, "text": "Voice answer"},
        ),
    ]
    assert all("api.telegram.org" in call[1] for call in http.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    (
        ProxyResponse(503, b"upstream response marker"),
        ProxyResponse(200, b"not-json response marker"),
        ProxyResponse(200, b'{"ok":false,"description":"response marker"}'),
    ),
)
async def test_api_rejects_non_2xx_malformed_and_non_ok_responses(response):
    http = RecordingHttp(post_results=(response,))
    transport = TelegramTransport(
        "telegram-token",
        AccessPolicy(frozenset({1001})),
        FakeConversation(),
        http,
    )

    with pytest.raises(TelegramError, match="^telegram_request_failed$"):
        await transport.poll_once(None)


@pytest.mark.asyncio
async def test_file_download_rejects_non_2xx_response():
    http = RecordingHttp(
        post_results=(
            ProxyResponse(
                200,
                b'{"ok":true,"result":{"file_path":"voice/file.ogg"}}',
            ),
        ),
        get_results=(ProxyResponse(302, b"redirect marker"),),
    )
    transport = TelegramTransport(
        "telegram-token",
        AccessPolicy(frozenset({1001})),
        FakeConversation(),
        http,
    )

    with pytest.raises(TelegramError, match="^telegram_file_failed$"):
        await transport._download_voice("voice-file")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", (urllib3.exceptions.HTTPError, OSError, UnicodeError))
async def test_api_low_level_failures_are_sanitized_without_traceback_leaks(failure):
    marker = "token-proxy-response-secret-marker"
    http = RecordingHttp(post_results=(failure(marker),))
    transport = TelegramTransport(
        marker,
        AccessPolicy(frozenset({1001})),
        FakeConversation(),
        http,
    )

    with pytest.raises(TelegramError) as exc_info:
        await transport.poll_once(None)

    formatted = "".join(
        traceback.format_exception(exc_info.type, exc_info.value, exc_info.tb)
    )
    assert str(exc_info.value) == "telegram_request_failed"
    assert marker not in str(exc_info.value)
    assert marker not in repr(exc_info.value)
    assert marker not in formatted


@pytest.mark.asyncio
async def test_file_low_level_failure_is_sanitized_without_traceback_leaks():
    marker = "file-token-proxy-response-secret-marker"
    http = RecordingHttp(
        post_results=(
            ProxyResponse(
                200,
                b'{"ok":true,"result":{"file_path":"voice/file.ogg"}}',
            ),
        ),
        get_results=(urllib3.exceptions.ProtocolError(marker),),
    )
    transport = TelegramTransport(
        marker,
        AccessPolicy(frozenset({1001})),
        FakeConversation(),
        http,
    )

    with pytest.raises(TelegramError) as exc_info:
        await transport._download_voice("voice-file")

    formatted = "".join(
        traceback.format_exception(exc_info.type, exc_info.value, exc_info.tb)
    )
    assert str(exc_info.value) == "telegram_file_failed"
    assert marker not in str(exc_info.value)
    assert marker not in repr(exc_info.value)
    assert marker not in formatted


@pytest.mark.asyncio
async def test_ambiguous_send_message_failure_is_not_retried():
    http = RecordingHttp(
        post_results=(urllib3.exceptions.ProtocolError("ambiguous send"),)
    )
    transport = TelegramTransport(
        "telegram-token",
        AccessPolicy(frozenset({1001})),
        FakeConversation(),
        http,
    )

    with pytest.raises(TelegramError, match="^telegram_request_failed$"):
        await transport._send(9, BotReply("One send only"))

    assert len(http.calls) == 1
    assert http.calls[0][1].endswith("/sendMessage")


def test_help_does_not_load_configuration(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["iwiki-telegram-bot", "--help"])

    def fail_load():
        raise AssertionError("configuration must not load for --help")

    monkeypatch.setattr(BotConfig, "load", fail_load)

    with pytest.raises(SystemExit) as captured:
        main()

    assert captured.value.code == 0
    assert "Telegram client for remote iwiki" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_runner_stops_before_remote_when_inference_probe_fails(monkeypatch):
    events = []

    class TelegramHttp:
        async def close(self):
            events.append("telegram_close")

    class FailingInference:
        def __init__(self, *arguments):
            pass

        async def probe(self):
            events.append("probe")
            raise InferenceError("configured_model_unavailable")

        async def close(self):
            events.append("close")

    def unexpected_remote(*arguments):
        raise AssertionError("remote connection must not open after failed probe")

    monkeypatch.setattr(main_module, "InferenceClient", FailingInference)
    monkeypatch.setattr(main_module, "open_remote_iwiki", unexpected_remote)
    monkeypatch.setattr(
        main_module,
        "build_proxy_client",
        lambda config: TelegramHttp(),
    )
    config = BotConfig(
        "telegram-token",
        "https://wiki.example/mcp",
        "iwiki-token",
        frozenset({1001}),
        "https://models.example/v1",
        "llm-key",
        "chat-model",
        "audio-model",
        300,
        TelegramProxyConfig("https://proxy.example:8443"),
    )

    with pytest.raises(InferenceError, match="configured_model_unavailable"):
        await main_module.run_bot(config)

    assert events == ["probe", "close", "telegram_close"]


@pytest.mark.asyncio
async def test_runner_probes_remote_scope_before_polling(monkeypatch):
    events = []

    class ReadyInference:
        def __init__(self, *arguments):
            pass

        async def probe(self):
            events.append("inference_probe")

        async def close(self):
            events.append("inference_close")

    class EmptyRemote:
        async def list_domains(self):
            events.append("remote_probe")
            raise RuntimeError("no_remote_domains")

    @asynccontextmanager
    async def remote_context(*arguments):
        yield EmptyRemote()

    monkeypatch.setattr(main_module, "InferenceClient", ReadyInference)
    monkeypatch.setattr(main_module, "open_remote_iwiki", remote_context)
    config = BotConfig(
        "telegram-token",
        "https://wiki.example/mcp",
        "iwiki-token",
        frozenset({1001}),
        "https://models.example/v1",
        "llm-key",
        "chat-model",
        "audio-model",
        300,
        TelegramProxyConfig("https://proxy.example:8443"),
    )

    with pytest.raises(RuntimeError, match="no_remote_domains"):
        await main_module.run_bot(config)

    assert events == ["inference_probe", "remote_probe", "inference_close"]


@pytest.mark.asyncio
async def test_runner_cancellation_completes_inference_and_proxy_cleanup(monkeypatch):
    events = []
    polling = anyio.Event()

    class CancellableInference:
        def __init__(self, *arguments):
            pass

        async def probe(self):
            pass

        async def close(self):
            events.append("inference_close_started")
            await anyio.sleep(0)
            events.append("inference_close_finished")

    class ReadyRemote:
        async def list_domains(self):
            return ["team"]

    class BlockingTransport:
        def __init__(self, *arguments):
            pass

        async def poll_forever(self, **kwargs):
            polling.set()
            await anyio.sleep_forever()

    class RecordingManager:
        def __init__(self):
            self.clear_count = 0

        def clear(self):
            self.clear_count += 1

    @asynccontextmanager
    async def remote_context(*arguments):
        yield ReadyRemote()

    manager = RecordingManager()
    proxy = TelegramProxyClient(manager)
    monkeypatch.setattr(main_module, "InferenceClient", CancellableInference)
    monkeypatch.setattr(main_module, "TelegramTransport", BlockingTransport)
    monkeypatch.setattr(main_module, "open_remote_iwiki", remote_context)
    monkeypatch.setattr(main_module, "build_proxy_client", lambda config: proxy)
    config = BotConfig(
        "telegram-token",
        "https://wiki.example/mcp",
        "iwiki-token",
        frozenset({1001}),
        "https://models.example/v1",
        "llm-key",
        "chat-model",
        "audio-model",
        300,
        TelegramProxyConfig("https://proxy.example:8443"),
    )

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(main_module.run_bot, config)
        await polling.wait()
        tasks.cancel_scope.cancel()

    assert events == ["inference_close_started", "inference_close_finished"]
    assert manager.clear_count == 1
