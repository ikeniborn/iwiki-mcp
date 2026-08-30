import asyncio
from pathlib import Path
import subprocess

import anyio
import pytest

from iwiki_mcp.telegram_bot import conversation as conversation_module
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
async def test_voice_is_converted_to_wav_before_transcription(
    service, tmp_path, monkeypatch
):
    await service.select_domain(1001, "team")
    wav = b"RIFF\x24\x00\x00\x00WAVEconverted-audio"
    commands = []

    async def convert(command, **kwargs):
        commands.append((command, kwargs))
        Path(command[-1]).write_bytes(wav)

    monkeypatch.setattr(anyio, "run_process", convert)

    reply = await service.answer_voice(1001, "voice.ogg", b"opus-audio")

    assert reply.text == "Answer"
    assert service.inference.calls[0] == ("transcribe", "audio.wav", wav)
    assert commands[0][0][0] == "ffmpeg"
    size_limit_index = commands[0][0].index("-fs")
    assert commands[0][0][size_limit_index + 1] == str(
        conversation_module._TRANSCRIPTION_MAX_BYTES
    )
    assert commands[0][1] == {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }


@pytest.mark.asyncio
async def test_voice_rejects_converter_output_without_wav_signature(
    service, monkeypatch
):
    await service.select_domain(1001, "team")

    async def convert(command, **kwargs):
        Path(command[-1]).write_bytes(b"not-a-wav-file")

    monkeypatch.setattr(anyio, "run_process", convert)

    reply = await service.answer_voice(1001, "voice.ogg", b"opus-audio")

    assert reply.text == "Voice transcription is unavailable."
    assert service.inference.calls == []


@pytest.mark.asyncio
async def test_voice_rejects_wav_larger_than_framework_limit(
    service, monkeypatch
):
    await service.select_domain(1001, "team")
    monkeypatch.setattr(
        conversation_module, "_TRANSCRIPTION_MAX_BYTES", 16, raising=False
    )

    async def convert(command, **kwargs):
        Path(command[-1]).write_bytes(b"RIFF\x24\x00\x00\x00WAVEoversized")

    monkeypatch.setattr(anyio, "run_process", convert)

    reply = await service.answer_voice(1001, "voice.ogg", b"opus-audio")

    assert reply.text == "Voice transcription is unavailable."
    assert service.inference.calls == []


@pytest.mark.asyncio
async def test_voice_conversion_failure_is_sanitized_and_temp_files_are_removed(
    service, tmp_path, monkeypatch
):
    await service.select_domain(1001, "team")
    observed_paths = []

    async def convert(command, **kwargs):
        observed_paths.extend(Path(command[-1]).parent.iterdir())
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(anyio, "run_process", convert)

    reply = await service.answer_voice(1001, "voice.ogg", b"opus-audio")

    assert reply.text == "Voice transcription is unavailable."
    assert service.inference.calls == []
    assert observed_paths
    assert all(path.exists() is False for path in observed_paths)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_voice_cancellation_removes_temp_files(
    service, tmp_path, monkeypatch
):
    await service.select_domain(1001, "team")
    conversion_started = asyncio.Event()
    observed_paths = []

    async def convert(command, **kwargs):
        observed_paths.extend(Path(command[-1]).parent.iterdir())
        conversion_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(anyio, "run_process", convert)

    task = asyncio.create_task(
        service.answer_voice(1001, "voice.ogg", b"opus-audio")
    )
    await conversion_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert observed_paths
    assert all(path.exists() is False for path in observed_paths)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_voice_file_is_removed_after_transcription(
    service, tmp_path, monkeypatch
):
    await service.select_domain(1001, "team")
    wav = b"RIFF\x24\x00\x00\x00WAVEconverted-audio"

    async def convert(command, **kwargs):
        Path(command[-1]).write_bytes(wav)

    monkeypatch.setattr(anyio, "run_process", convert)

    reply = await service.answer_voice(1001, "voice.ogg", b"audio")

    assert reply.text == "Answer"
    assert ("transcribe", "audio.wav", wav) in service.inference.calls
    assert list(Path(tmp_path).iterdir()) == []


@pytest.mark.asyncio
async def test_voice_file_is_removed_when_transcription_fails(
    tmp_path, clock, monkeypatch
):
    temporary_directory = tmp_path / "transient"
    temporary_directory.mkdir()
    outside_directory = tmp_path / "outside"
    outside_directory.mkdir()
    observed_paths = []
    wav = b"RIFF\x24\x00\x00\x00WAVEconverted-audio"

    async def convert(command, **kwargs):
        Path(command[-1]).write_bytes(wav)

    monkeypatch.setattr(anyio, "run_process", convert)

    class FailingInference(FakeInference):
        async def transcribe(self, filename, audio):
            observed_paths.extend(temporary_directory.iterdir())
            assert filename == "audio.wav"
            assert audio == wav
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
