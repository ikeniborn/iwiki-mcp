from pathlib import Path

import pytest

from iwiki_mcp.telegram_bot.access import AccessPolicy
from iwiki_mcp.telegram_bot.conversation import ConversationService
from iwiki_mcp.telegram_bot.inference import InferenceError


class FakeRemote:
    def __init__(self):
        self.calls = []
        self.domains = ["team", "public"]

    async def list_domains(self):
        self.calls.append(("list_domains",))
        return self.domains

    async def search(self, domain, query):
        self.calls.append(("search", domain, query))
        return [{"slug": "guide/deploy"}]

    async def read_page(self, domain, slug, heading=None):
        self.calls.append(("read_page", domain, slug, heading))
        return {"markdown": f"{domain} deployment section"}


class FakeInference:
    def __init__(self):
        self.calls = []

    async def answer(self, question, context):
        self.calls.append(("answer", question, context))
        return "Answer"

    async def transcribe(self, filename, audio):
        self.calls.append(("transcribe", filename, audio))
        return "Spoken question"


@pytest.fixture
def clock():
    value = [100.0]

    def now():
        return value[0]

    now.value = value
    return now


@pytest.fixture
def service(tmp_path, clock):
    remote = FakeRemote()
    inference = FakeInference()
    value = ConversationService(
        AccessPolicy(frozenset({1001})),
        remote,
        inference,
        confirmation_ttl_seconds=300,
        temporary_directory=tmp_path,
        clock=clock,
    )
    value.remote = remote
    value.inference = inference
    return value


@pytest.mark.asyncio
async def test_unknown_sender_never_reaches_remote_or_inference(service):
    reply = await service.list_domains(2002)

    assert reply.text == "Access denied."
    assert service.remote.calls == []
    assert service.inference.calls == []


@pytest.mark.asyncio
async def test_domain_must_be_visible_before_selection(service):
    reply = await service.select_domain(1001, "hidden")

    assert reply.text == "Domain is not available."
    assert service.remote.calls == [("list_domains",)]


@pytest.mark.asyncio
async def test_question_uses_only_selected_domain_context(service):
    await service.select_domain(1001, "team")

    reply = await service.answer_question(1001, "How do I deploy?")

    assert reply.text == "Answer"
    assert ("search", "team", "How do I deploy?") in service.remote.calls
    assert service.inference.calls == [
        ("answer", "How do I deploy?", "team deployment section")
    ]


@pytest.mark.asyncio
async def test_question_requires_selected_domain_before_outbound_calls(service):
    reply = await service.answer_question(1001, "Question")

    assert reply.text == "Select a domain first."
    assert service.remote.calls == []
    assert service.inference.calls == []


@pytest.mark.asyncio
async def test_voice_file_is_removed_after_transcription(service, tmp_path):
    await service.select_domain(1001, "team")

    reply = await service.answer_voice(1001, "voice.ogg", b"audio")

    assert reply.text == "Answer"
    assert ("transcribe", "voice.ogg", b"audio") in service.inference.calls
    assert list(Path(tmp_path).iterdir()) == []


@pytest.mark.asyncio
async def test_voice_file_is_removed_when_transcription_fails(tmp_path, clock):
    temporary_directory = tmp_path / "transient"
    temporary_directory.mkdir()
    outside_directory = tmp_path / "outside"
    outside_directory.mkdir()
    observed_paths = []

    class FailingInference(FakeInference):
        async def transcribe(self, filename, audio):
            observed_paths.extend(temporary_directory.iterdir())
            assert filename == "filename-marker.ogg"
            assert audio == b"audio-content-marker"
            raise InferenceError("inference_failed")

    remote = FakeRemote()
    inference = FailingInference()
    service = ConversationService(
        AccessPolicy(frozenset({1001})),
        remote,
        inference,
        confirmation_ttl_seconds=300,
        temporary_directory=temporary_directory,
        clock=clock,
    )
    await service.select_domain(1001, "team")

    reply = await service.answer_voice(
        1001, "filename-marker.ogg", b"audio-content-marker"
    )

    assert reply.text == "Voice transcription is unavailable."
    assert len(observed_paths) == 1
    assert observed_paths[0].exists() is False
    assert list(temporary_directory.iterdir()) == []
    assert list(outside_directory.iterdir()) == []


@pytest.mark.asyncio
async def test_selected_domain_expires_from_memory(service, clock):
    await service.select_domain(1001, "team")
    clock.value[0] = 401.0

    service.expire_state()
    reply = await service.answer_question(1001, "Question")

    assert reply.text == "Select a domain first."
