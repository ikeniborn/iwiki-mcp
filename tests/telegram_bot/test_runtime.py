from contextlib import asynccontextmanager
import asyncio
from dataclasses import replace
import json
import logging
import sys
import traceback

import anyio
import pytest
import urllib3
from mcp import McpError
from mcp.types import ErrorData

import iwiki_mcp.telegram_bot.main as main_module
from iwiki_mcp.telegram_bot.access import AccessPolicy
from iwiki_mcp.telegram_bot.config import BotConfig, TelegramProxyConfig
from iwiki_mcp.telegram_bot.inference import InferenceClient, InferenceError
from iwiki_mcp.telegram_bot.iwiki import RemoteIwikiClient, RemoteIwikiError
from iwiki_mcp.telegram_bot.proxy import ProxyResponse
from iwiki_mcp.telegram_bot.runtime import Backoff, Heartbeat
from iwiki_mcp.telegram_bot.transport import TelegramTransport


class SequencedHttp:
    def __init__(self, outcomes):
        self._outcomes = iter(outcomes)
        self.calls = []

    async def post_json(self, url, payload):
        self.calls.append((url, payload))
        outcome = next(self._outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def get_bytes(self, url):
        raise AssertionError("voice download is not expected")

    async def close(self):
        pass


class NullConversation:
    def expire_state(self):
        pass


class RecordingHeartbeat:
    def __init__(self):
        self.touches = 0

    def touch(self):
        self.touches += 1


def test_backoff_caps_and_resets():
    backoff = Backoff(initial=1, maximum=8, jitter_ratio=0.25)

    assert [backoff.next_delay(0.0) for _ in range(5)] == [
        0.75,
        1.5,
        3.0,
        6.0,
        6.0,
    ]
    backoff.reset()
    assert backoff.next_delay(1.0) == 1.25


def test_backoff_remains_capped_after_1025_failures():
    backoff = Backoff(initial=1.0, maximum=8.0, jitter_ratio=0.25)

    delays = [backoff.next_delay(0.5) for _ in range(1026)]

    assert delays[-2:] == [8.0, 8.0]


def test_heartbeat_writes_only_timestamp(tmp_path):
    path = tmp_path / "heartbeat"

    Heartbeat(path, clock=lambda: 123.5).touch()

    assert path.read_bytes() == b"123.500000\n"


@pytest.mark.asyncio
async def test_remote_cancel_scope_is_closed_before_shielded_cleanup():
    @asynccontextmanager
    async def remote_context():
        async with anyio.create_task_group() as task_group:
            class Remote:
                async def cancel_session(self):
                    task_group.cancel_scope.cancel()
                    await anyio.sleep(0)

            yield Remote()

    context = remote_context()
    remote = await context.__aenter__()
    with pytest.raises(asyncio.CancelledError):
        await remote.cancel_session()
    error = RemoteIwikiError(
        "remote_call_failed",
        retryable=True,
    )
    await main_module._close_dependencies(
        context,
        True,
        None,
        None,
        (RemoteIwikiError, error, error.__traceback__),
    )

    await anyio.sleep(0)


@pytest.mark.asyncio
async def test_internal_remote_cancel_scope_reconnects_after_scope_closes(
    monkeypatch,
):
    polling = anyio.Event()
    remotes = []

    class Proxy:
        async def close(self):
            pass

    class Inference:
        def __init__(self, *arguments, **options):
            pass

        async def probe(self):
            pass

        async def close(self):
            pass

    class Remote:
        async def list_domains(self):
            return ["team"]

    @asynccontextmanager
    async def remote_context(*arguments):
        remote = Remote()
        remotes.append(remote)
        if len(remotes) == 1:
            async with anyio.create_task_group() as task_group:
                async def cancel_during_poll():
                    await polling.wait()
                    task_group.cancel_scope.cancel()

                task_group.start_soon(cancel_during_poll)
                yield remote
        else:
            yield remote

    class Transport:
        calls = 0

        def __init__(self, *arguments, **options):
            pass

        async def publish_commands(self):
            pass

        async def poll_forever(self, **kwargs):
            self.__class__.calls += 1
            if self.calls == 1:
                polling.set()
                await anyio.sleep_forever()
            raise RuntimeError("stop after remote reconnect")

    async def sleep(delay):
        pass

    monkeypatch.setattr(main_module, "build_proxy_client", lambda config: Proxy())
    monkeypatch.setattr(main_module, "InferenceClient", Inference)
    monkeypatch.setattr(main_module, "open_remote_iwiki", remote_context)
    monkeypatch.setattr(main_module, "TelegramTransport", Transport)

    with pytest.raises(RuntimeError, match="stop after remote reconnect"):
        await main_module.run_bot(
            _config(),
            sleep=sleep,
            heartbeat=RecordingHeartbeat(),
        )

    assert len(remotes) == 2
    assert Transport.calls == 2


@pytest.mark.asyncio
async def test_run_bot_preserves_external_anyio_cancellation(monkeypatch):
    polling = anyio.Event()
    events = []

    class Proxy:
        async def close(self):
            events.append("proxy_close")

    class Inference:
        def __init__(self, *arguments, **options):
            pass

        async def probe(self):
            pass

        async def close(self):
            events.append("inference_close")

    class Remote:
        async def list_domains(self):
            return ["team"]

    @asynccontextmanager
    async def remote_context(*arguments):
        events.append("remote_enter")
        try:
            yield Remote()
        finally:
            events.append("remote_close")

    class Transport:
        def __init__(self, *arguments, **options):
            pass

        async def publish_commands(self):
            pass

        async def poll_forever(self, **kwargs):
            polling.set()
            await anyio.sleep_forever()

    monkeypatch.setattr(main_module, "build_proxy_client", lambda config: Proxy())
    monkeypatch.setattr(main_module, "InferenceClient", Inference)
    monkeypatch.setattr(main_module, "open_remote_iwiki", remote_context)
    monkeypatch.setattr(main_module, "TelegramTransport", Transport)

    async def invoke():
        with pytest.raises(asyncio.CancelledError):
            await main_module.run_bot(
                _config(), heartbeat=RecordingHeartbeat()
            )

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(invoke)
        await polling.wait()
        task_group.cancel_scope.cancel()

    assert events == [
        "remote_enter",
        "remote_close",
        "inference_close",
        "proxy_close",
    ]


@pytest.mark.asyncio
async def test_polling_retries_safely_without_advancing_failed_offset(caplog):
    markers = (
        "bot-token-marker",
        "https://proxy-user-marker:proxy-password-marker@proxy-marker:8443",
        "update-text-marker",
        "transcription-marker",
        "filename-marker.ogg",
        "provider-response-marker",
    )
    http = SequencedHttp(
        (
            urllib3.exceptions.ProtocolError(" ".join(markers)),
            urllib3.exceptions.ProtocolError("second " + " ".join(markers)),
            ProxyResponse(
                200, b'{"ok":true,"result":[{"update_id":16}]}'
            ),
            urllib3.exceptions.ProtocolError("after success " + " ".join(markers)),
            RuntimeError("stop polling"),
        )
    )
    transport = TelegramTransport(
        markers[0],
        AccessPolicy(frozenset({1001})),
        NullConversation(),
        http,
    )

    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    random_values = iter((0.0, 1.0, 0.0))
    heartbeat = RecordingHeartbeat()

    with caplog.at_level(logging.WARNING, logger="iwiki_mcp.telegram_bot.transport"):
        with pytest.raises(RuntimeError, match="stop polling"):
            await transport.poll_forever(
                sleep=sleep,
                random_value=lambda: next(random_values),
                heartbeat=heartbeat,
                clock=lambda: 10.0,
            )

    assert sleeps == pytest.approx([0.8, 2.4, 0.8])
    assert [call[1].get("offset") for call in http.calls] == [
        None,
        None,
        None,
        17,
        17,
    ]
    assert heartbeat.touches == 1
    assert len(caplog.records) == 3
    for record in caplog.records:
        assert record.operation == "poll"
        assert record.outcome == "retry"
        assert isinstance(record.delay_seconds, float)
        assert isinstance(record.elapsed_ms, int)
        rendered = record.getMessage() + repr(record.__dict__)
        assert all(marker not in rendered for marker in markers)


def _config() -> BotConfig:
    return BotConfig(
        "bot-token-marker",
        "https://wiki-marker.example/mcp",
        "iwiki-token-marker",
        frozenset({1001}),
        "https://provider-marker.example/v1",
        "provider-key-marker",
        "chat-model",
        "transcription-marker",
        300,
        TelegramProxyConfig(
            "https://proxy-marker.example:8443",
            "Basic proxy-user-password-marker",
        ),
    )


@pytest.mark.asyncio
async def test_startup_retries_close_each_attempt_before_sleep_and_log_no_secrets(
    monkeypatch, caplog
):
    events = []
    proxies = []
    inferences = []
    remotes = []

    class Proxy:
        def __init__(self, attempt):
            self.attempt = attempt
            self.closed = False

        async def close(self):
            events.append(f"proxy_{self.attempt}_close")
            self.closed = True

    def build_proxy(config):
        proxy = Proxy(len(proxies) + 1)
        proxies.append(proxy)
        events.append(f"proxy_{proxy.attempt}_create")
        return proxy

    class Inference:
        def __init__(self, *arguments, **options):
            self.attempt = len(inferences) + 1
            self.closed = False
            inferences.append(self)
            events.append(f"inference_{self.attempt}_create")

        async def probe(self):
            events.append(f"inference_{self.attempt}_probe")
            if self.attempt == 1:
                try:
                    raise ValueError("provider-response-marker")
                except ValueError as exc:
                    raise InferenceError(
                        "inference_failed", retryable=True
                    ) from exc

        async def close(self):
            events.append(f"inference_{self.attempt}_close")
            self.closed = True

    class Remote:
        def __init__(self, attempt):
            self.attempt = attempt
            self.closed = False

        async def list_domains(self):
            events.append(f"remote_{self.attempt}_probe")
            if self.attempt == 1:
                raise RemoteIwikiError("remote_call_failed", retryable=True)
            return ["team"]

    @asynccontextmanager
    async def remote_context(*arguments):
        remote = Remote(len(remotes) + 1)
        remotes.append(remote)
        events.append(f"remote_{remote.attempt}_enter")
        try:
            yield remote
        finally:
            events.append(f"remote_{remote.attempt}_close")
            remote.closed = True

    class StoppingTransport:
        def __init__(self, *arguments, **options):
            events.append("transport_create")

        async def publish_commands(self):
            pass

        async def poll_forever(self, **kwargs):
            events.append("poll")
            raise RuntimeError("stop after startup")

    sleeps = []

    async def sleep(delay):
        assert all(proxy.closed for proxy in proxies)
        assert all(inference.closed for inference in inferences)
        assert all(remote.closed for remote in remotes)
        sleeps.append(delay)
        events.append("sleep")

    monkeypatch.setattr(main_module, "build_proxy_client", build_proxy)
    monkeypatch.setattr(main_module, "InferenceClient", Inference)
    monkeypatch.setattr(main_module, "open_remote_iwiki", remote_context)
    monkeypatch.setattr(main_module, "TelegramTransport", StoppingTransport)

    random_values = iter((0.0, 1.0))
    with caplog.at_level(logging.WARNING, logger="iwiki_mcp.telegram_bot.main"):
        with pytest.raises(RuntimeError, match="stop after startup"):
            await main_module.run_bot(
                _config(),
                sleep=sleep,
                random_value=lambda: next(random_values),
                heartbeat=RecordingHeartbeat(),
                clock=lambda: 10.0,
            )

    assert sleeps == pytest.approx([0.8, 2.4])
    assert len(proxies) == len(inferences) == 3
    assert len(remotes) == 2
    assert all(proxy.closed for proxy in proxies)
    assert all(inference.closed for inference in inferences)
    assert all(remote.closed for remote in remotes)
    assert events.index("inference_1_close") < events.index("sleep")
    assert events.index("remote_1_close") < events.index("sleep", events.index("sleep") + 1)
    assert len(caplog.records) == 2
    markers = (
        "bot-token-marker",
        "iwiki-token-marker",
        "proxy-marker",
        "proxy-user-password-marker",
        "update-text-marker",
        "transcription-marker",
        "filename-marker",
        "provider-response-marker",
    )
    for record in caplog.records:
        assert record.operation == "startup"
        assert record.outcome == "retry"
        assert isinstance(record.delay_seconds, float)
        assert isinstance(record.elapsed_ms, int)
        rendered = record.getMessage() + repr(record.__dict__)
        assert all(marker not in rendered for marker in markers)


@pytest.mark.asyncio
async def test_post_start_remote_failure_reconnects_and_replays_only_failed_update(
    monkeypatch, caplog
):
    updates = (
        {
            "update_id": 20,
            "message": {
                "from": {"id": 1001},
                "chat": {"id": 9},
                "text": "/domains",
            },
        },
        {
            "update_id": 21,
            "message": {
                "from": {"id": 1001},
                "chat": {"id": 10},
                "text": "/domains",
            },
        },
    )
    offsets = []
    sent = []

    class TelegramHttp:
        async def post_json(self, url, payload):
            method = url.rsplit("/", 1)[-1]
            if method == "getUpdates":
                offsets.append(payload.get("offset"))
                if len(offsets) == 1:
                    result = list(updates)
                elif len(offsets) == 2:
                    result = [updates[1]]
                else:
                    raise RuntimeError("stop after recovered update")
                return ProxyResponse(
                    200,
                    json.dumps({"ok": True, "result": result}).encode(),
                )
            if method == "sendMessage":
                sent.append(payload)
                return ProxyResponse(200, b'{"ok":true,"result":{}}')
            if method in (
                "setMyCommands",
                "setChatMenuButton",
                "sendChatAction",
                "setMessageReaction",
                "editMessageText",
            ):
                return ProxyResponse(200, b'{"ok":true,"result":true}')
            raise AssertionError(f"unexpected Telegram method: {method}")

        async def get_bytes(self, url):
            raise AssertionError("voice download is not expected")

        async def close(self):
            pass

    class Inference:
        instances = 0

        def __init__(self, *arguments, **options):
            self.__class__.instances += 1

        async def probe(self):
            pass

        async def close(self):
            pass

    remotes = []

    class Remote:
        def __init__(self, attempt):
            self.attempt = attempt
            self.calls = 0

        async def list_domains(self):
            self.calls += 1
            if self.attempt == 1 and self.calls == 3:
                marker = "post-start-session-private-marker"

                async def terminated_call(name, arguments):
                    raise McpError(
                        ErrorData(
                            code=-32600,
                            message="Session terminated",
                            data={"detail": marker},
                        )
                    )

                return await RemoteIwikiClient(terminated_call).list_domains()
            return ["team"]

    @asynccontextmanager
    async def remote_context(*arguments):
        remote = Remote(len(remotes) + 1)
        remotes.append(remote)
        yield remote

    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    telegram_http = TelegramHttp()
    monkeypatch.setattr(
        main_module, "build_proxy_client", lambda config: telegram_http
    )
    monkeypatch.setattr(main_module, "InferenceClient", Inference)
    monkeypatch.setattr(main_module, "open_remote_iwiki", remote_context)

    with caplog.at_level(logging.WARNING, logger="iwiki_mcp.telegram_bot.main"):
        with pytest.raises(RuntimeError, match="stop after recovered update"):
            await main_module.run_bot(
                _config(),
                sleep=sleep,
                random_value=lambda: 0.0,
                heartbeat=RecordingHeartbeat(),
                clock=lambda: 10.0,
            )

    assert offsets == [None, 21, 22]
    assert [(payload["chat_id"], payload["text"]) for payload in sent] == [
        (9, "Available domains:"),
        (10, "Available domains:"),
    ]
    assert len(remotes) == 2
    assert Inference.instances == 1
    assert sleeps == pytest.approx([0.8])
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.operation == "remote_session"
    assert record.outcome == "retry"
    rendered = record.getMessage() + repr(record.__dict__)
    assert "post-start-session-private-marker" not in rendered
    assert "iwiki-token-marker" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_type", "code"),
    (
        (InferenceError, "inference_failed"),
        (RemoteIwikiError, "invalid_remote_response"),
    ),
)
async def test_non_retryable_startup_failure_does_not_sleep(
    monkeypatch, failure_type, code
):
    events = []
    failure = failure_type(code, retryable=False)

    class Proxy:
        async def close(self):
            events.append("proxy_close")

    class Inference:
        async def probe(self):
            events.append("inference_probe")
            if isinstance(failure, InferenceError):
                raise failure

        async def close(self):
            events.append("inference_close")

    class Remote:
        async def list_domains(self):
            events.append("remote_probe")
            raise failure

    @asynccontextmanager
    async def remote_context(*arguments):
        events.append("remote_enter")
        try:
            yield Remote()
        finally:
            events.append("remote_close")

    async def unexpected_sleep(delay):
        raise AssertionError("fatal startup errors must not sleep")

    monkeypatch.setattr(main_module, "build_proxy_client", lambda config: Proxy())
    monkeypatch.setattr(
        main_module, "InferenceClient", lambda *args, **options: Inference()
    )
    monkeypatch.setattr(main_module, "open_remote_iwiki", remote_context)

    with pytest.raises(failure_type) as captured:
        await main_module.run_bot(
            _config(),
            sleep=unexpected_sleep,
            heartbeat=RecordingHeartbeat(),
        )

    assert captured.value is failure
    assert events.count("inference_probe") == 1
    assert events.count("proxy_close") == 1
    assert events.count("inference_close") == 1
    if isinstance(failure, RemoteIwikiError):
        assert events.count("remote_probe") == 1
        assert events.count("remote_close") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url",
    (
        "provider-without-scheme",
        "https://provider.example:invalid-port-marker/v1",
    ),
    ids=("unsupported-protocol", "invalid-url"),
)
async def test_invalid_inference_url_is_not_retried(monkeypatch, base_url):
    events = []

    class Proxy:
        async def close(self):
            events.append("proxy_close")

    class RecordingInference(InferenceClient):
        async def close(self):
            events.append("inference_close")
            await super().close()

    async def unexpected_sleep(delay):
        raise AssertionError("invalid inference URLs must not be retried")

    monkeypatch.setattr(main_module, "build_proxy_client", lambda config: Proxy())
    monkeypatch.setattr(main_module, "InferenceClient", RecordingInference)

    config = replace(_config(), llm_base_url=base_url)
    with pytest.raises(InferenceError) as captured:
        await main_module.run_bot(
            config,
            sleep=unexpected_sleep,
            heartbeat=RecordingHeartbeat(),
        )

    assert captured.value.retryable is False
    assert events == ["inference_close", "proxy_close"]


class UnknownStartupFailure(BaseException):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    (
        asyncio.CancelledError("startup-cancellation-marker"),
        UnknownStartupFailure("unknown-startup-marker"),
    ),
    ids=("cancellation", "unknown-base-exception"),
)
async def test_run_bot_cleanup_preserves_startup_base_exception(
    monkeypatch, failure
):
    events = []

    class Proxy:
        async def close(self):
            events.append("proxy_close")
            raise RuntimeError("proxy-cleanup-marker")

    class Inference:
        async def probe(self):
            events.append("inference_probe")

        async def close(self):
            events.append("inference_close")
            raise RuntimeError("inference-cleanup-marker")

    class Remote:
        async def list_domains(self):
            events.append("remote_probe")
            raise failure

    @asynccontextmanager
    async def remote_context(*arguments):
        try:
            yield Remote()
        finally:
            events.append("remote_close")
            raise RuntimeError("remote-cleanup-marker")

    monkeypatch.setattr(main_module, "build_proxy_client", lambda config: Proxy())
    monkeypatch.setattr(
        main_module, "InferenceClient", lambda *args, **options: Inference()
    )
    monkeypatch.setattr(main_module, "open_remote_iwiki", remote_context)

    with pytest.raises(type(failure)) as captured:
        await main_module.run_bot(_config(), heartbeat=RecordingHeartbeat())

    assert captured.value is failure
    assert events == [
        "inference_probe",
        "remote_probe",
        "remote_close",
        "inference_close",
        "proxy_close",
    ]
    rendered = "".join(
        traceback.format_exception(
            type(captured.value), captured.value, captured.value.__traceback__
        )
    )
    assert "cleanup-marker" not in rendered


@pytest.mark.asyncio
async def test_cleanup_attempts_all_resources_and_surfaces_sanitized_failure():
    events = []

    class RemoteContext:
        async def __aexit__(self, *exc_info):
            events.append("remote_close")
            raise RuntimeError("remote-cleanup-marker")

    class Inference:
        async def close(self):
            events.append("inference_close")
            raise RuntimeError("inference-cleanup-marker")

    class Proxy:
        async def close(self):
            events.append("proxy_close")
            raise RuntimeError("proxy-cleanup-marker")

    with pytest.raises(RuntimeError) as captured:
        await main_module._close_dependencies(
            RemoteContext(),
            True,
            Inference(),
            Proxy(),
            (None, None, None),
        )

    assert str(captured.value) == "dependency_cleanup_failed"
    assert events == ["remote_close", "inference_close", "proxy_close"]
    rendered = "".join(
        traceback.format_exception(
            type(captured.value), captured.value, captured.value.__traceback__
        )
    )
    assert "cleanup-marker" not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_replace_cancellation():
    events = []

    class RemoteContext:
        async def __aexit__(self, *exc_info):
            events.append("remote_close")
            raise RuntimeError("remote-cleanup-marker")

    class Inference:
        async def close(self):
            events.append("inference_close")
            raise RuntimeError("inference-cleanup-marker")

    class Proxy:
        async def close(self):
            events.append("proxy_close")
            raise RuntimeError("proxy-cleanup-marker")

    cancellation = asyncio.CancelledError("original-cancellation")

    with pytest.raises(asyncio.CancelledError) as captured:
        try:
            raise cancellation
        except asyncio.CancelledError:
            await main_module._close_dependencies(
                RemoteContext(),
                True,
                Inference(),
                Proxy(),
                sys.exc_info(),
            )
            raise

    assert captured.value is cancellation
    assert events == ["remote_close", "inference_close", "proxy_close"]
