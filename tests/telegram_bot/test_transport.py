import pytest

from iwiki_mcp.telegram_bot.access import AccessPolicy
from iwiki_mcp.telegram_bot.config import BotConfig
from iwiki_mcp.telegram_bot.main import main
from iwiki_mcp.telegram_bot.models import BotReply, WritePreview
from iwiki_mcp.telegram_bot.transport import TelegramTransport


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


def test_help_does_not_load_configuration(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["iwiki-telegram-bot", "--help"])

    def fail_load():
        raise AssertionError("configuration must not load for --help")

    monkeypatch.setattr(BotConfig, "load", fail_load)

    with pytest.raises(SystemExit) as captured:
        main()

    assert captured.value.code == 0
    assert "Telegram client for remote iwiki" in capsys.readouterr().out
