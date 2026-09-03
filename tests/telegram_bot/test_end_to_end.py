from pathlib import Path
import subprocess

import anyio
import pytest

from iwiki_mcp.telegram_bot.access import AccessPolicy
from iwiki_mcp.telegram_bot.conversation import ConversationService
from iwiki_mcp.telegram_bot.transport import TelegramTransport


class FakeRemote:
    def __init__(self):
        self.calls = []
        self.writes = 0

    async def bind(self):
        await self.list_domains()

    def writable(self, domain):
        return True

    async def list_domains(self):
        self.calls.append(("list_domains",))
        return ["team"]

    async def search(self, domain, query):
        self.calls.append(("search", domain, query))
        return [{"slug": "guide/deploy"}]

    async def read_page(self, domain, slug, heading=None):
        self.calls.append(("read_page", domain, slug, heading))
        return {
            "markdown": "Team deployment guide",
            "body": "Deployment steps",
            "revision": 7,
            "section_hash": "section-hash",
        }

    async def write_page(self, domain, slug, markdown):
        self.calls.append(("write_page", domain, slug, markdown))
        self.writes += 1

    async def update_section(self, *arguments):
        self.calls.append(("update_section", *arguments))
        self.writes += 1


class FakeInference:
    def __init__(self):
        self.calls = []
        self.answers = 0

    async def answer(self, question, context):
        self.calls.append(("answer", question, context))
        self.answers += 1
        return "Grounded answer"

    async def transcribe(self, filename, audio):
        self.calls.append(("transcribe", filename, audio))
        return "Voice question"

    async def draft_markdown(self, request, context):
        self.calls.append(("draft_markdown", request, context))
        return "# Runbook\n\nDeployment steps"


class FakeTelegramHttp:
    async def post_json(self, url, payload):
        raise AssertionError("BotHarness must handle Telegram API calls")

    async def get_bytes(self, url):
        raise AssertionError("BotHarness must handle Telegram file downloads")

    async def close(self):
        pass


class BotHarness(TelegramTransport):
    def __init__(self, access, conversation):
        super().__init__("token", access, conversation, FakeTelegramHttp())
        self.sent = []

    async def _api(self, method, data):
        if method == "sendMessage":
            self.sent.append(dict(data))
            return {"message_id": len(self.sent)}
        if method == "editMessageText":
            edited = dict(data)
            index = edited.pop("message_id") - 1
            self.sent[index] = edited
            return {}
        return [] if method == "getUpdates" else {}

    async def _download_voice(self, file_id):
        return "voice.ogg", b"audio"

    async def text(self, telegram_id, value):
        await self.handle_update(
            {
                "message": {
                    "from": {"id": telegram_id},
                    "chat": {"id": telegram_id},
                    "text": value,
                }
            }
        )

    async def voice(self, telegram_id):
        await self.handle_update(
            {
                "message": {
                    "from": {"id": telegram_id},
                    "chat": {"id": telegram_id},
                    "voice": {"file_id": "voice-file"},
                }
            }
        )

    async def callback(self, telegram_id, data):
        await self.handle_update(
            {
                "callback_query": {
                    "id": "callback-id",
                    "from": {"id": telegram_id},
                    "message": {"chat": {"id": telegram_id}},
                    "data": data,
                }
            }
        )


@pytest.fixture
def bot(tmp_path, monkeypatch):
    async def convert(command, **kwargs):
        Path(command[-1]).write_bytes(
            b"RIFF\x24\x00\x00\x00WAVEconverted-audio"
        )

    monkeypatch.setattr(anyio, "run_process", convert)
    access = AccessPolicy(frozenset({1001}))
    remote = FakeRemote()
    inference = FakeInference()
    conversation = ConversationService(
        access,
        remote,
        inference,
        confirmation_ttl_seconds=300,
        temporary_directory=tmp_path,
    )
    transport = BotHarness(access, conversation)
    transport.remote = remote
    transport.inference = inference
    return transport


@pytest.mark.asyncio
async def test_authorized_text_voice_and_confirmed_write_path(bot):
    await bot.text(1001, "/domains")
    await bot.callback(1001, "domain:team")
    await bot.text(1001, "How do I deploy?")
    await bot.voice(1001)
    await bot.text(1001, "/create runbook: deployment steps")
    confirmation = bot.sent[-1]["reply_markup"]["inline_keyboard"][0][0]
    await bot.callback(1001, confirmation["callback_data"])

    assert bot.remote.writes == 1
    assert bot.inference.answers == 2
    assert {call[1] for call in bot.remote.calls if call[0] == "search"} == {
        "team"
    }


@pytest.mark.asyncio
async def test_text_update_is_processed_after_voice_conversion_failure(
    bot, monkeypatch
):
    async def fail_conversion(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(anyio, "run_process", fail_conversion)

    await bot.text(1001, "/domains")
    await bot.callback(1001, "domain:team")
    await bot.voice(1001)
    await bot.text(1001, "How do I deploy?")

    assert bot.sent[-2]["text"] == "Voice transcription is unavailable."
    assert bot.sent[-1]["text"] == "Grounded answer"
    assert bot.inference.answers == 1


@pytest.mark.asyncio
async def test_unauthorized_sender_has_no_outbound_calls(bot):
    await bot.text(2002, "/domains")

    assert bot.remote.calls == []
    assert bot.inference.calls == []
    assert bot.sent == [{"chat_id": 2002, "text": "Access denied."}]
