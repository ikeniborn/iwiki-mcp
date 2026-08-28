import pytest
import urllib3

from iwiki_mcp.telegram_bot.config import TelegramProxyConfig
from iwiki_mcp.telegram_bot.proxy import (
    ProxyResponse,
    TelegramProxyClient,
    build_proxy_client,
)


class RecordingResponse:
    def __init__(self, status=200, data=b"response", data_error=None):
        self.status = status
        self._data = data
        self._data_error = data_error
        self.release_count = 0

    @property
    def data(self):
        if self._data_error is not None:
            raise self._data_error
        return self._data

    def release_conn(self):
        self.release_count += 1


class RecordingManager:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.clear_count = 0

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)

    def clear(self):
        self.clear_count += 1


@pytest.mark.asyncio
async def test_post_json_uses_compact_utf8_json_and_fixed_request_controls():
    response = RecordingResponse(status=201, data=b"created")
    manager = RecordingManager([response])
    client = TelegramProxyClient(manager)

    result = await client.post_json(
        "https://api.telegram.org/bottoken/sendMessage",
        {"chat_id": 9, "text": "hello мир"},
    )

    assert result == ProxyResponse(201, b"created")
    assert manager.requests == [
        (
            "POST",
            "https://api.telegram.org/bottoken/sendMessage",
            {
                "body": '{"chat_id":9,"text":"hello мир"}'.encode("utf-8"),
                "headers": {"Content-Type": "application/json"},
                "preload_content": True,
                "redirect": False,
                "retries": False,
                "timeout": manager.requests[0][2]["timeout"],
            },
        )
    ]
    timeout = manager.requests[0][2]["timeout"]
    assert isinstance(timeout, urllib3.Timeout)
    assert timeout.connect_timeout == 10
    assert timeout.read_timeout == 40
    assert response.release_count == 1


@pytest.mark.asyncio
async def test_get_bytes_uses_fixed_request_controls_and_releases_connection():
    response = RecordingResponse(status=200, data=bytearray(b"voice"))
    manager = RecordingManager([response])
    client = TelegramProxyClient(manager)

    result = await client.get_bytes(
        "https://api.telegram.org/file/bottoken/voice/file.ogg"
    )

    assert result == ProxyResponse(200, b"voice")
    method, url, arguments = manager.requests[0]
    assert method == "GET"
    assert url == "https://api.telegram.org/file/bottoken/voice/file.ogg"
    assert arguments["redirect"] is False
    assert arguments["retries"] is False
    assert arguments["preload_content"] is True
    assert arguments["timeout"].connect_timeout == 10
    assert arguments["timeout"].read_timeout == 40
    assert response.release_count == 1


@pytest.mark.asyncio
async def test_response_connection_is_released_when_body_copy_fails():
    response = RecordingResponse(data_error=RuntimeError("copy failed"))
    manager = RecordingManager([response])
    client = TelegramProxyClient(manager)

    with pytest.raises(RuntimeError, match="copy failed"):
        await client.get_bytes("https://api.telegram.org/file/bottoken/file")

    assert response.release_count == 1


@pytest.mark.asyncio
async def test_close_clears_manager():
    manager = RecordingManager([])
    client = TelegramProxyClient(manager)

    await client.close()

    assert manager.clear_count == 1


@pytest.mark.parametrize(
    ("authorization", "expected_proxy_headers"),
    (
        (None, None),
        ("Basic cHJveHk6c2VjcmV0", {"Proxy-Authorization": "Basic cHJveHk6c2VjcmV0"}),
    ),
)
def test_builder_uses_literal_tls_proxy_origin_and_separate_authorization(
    monkeypatch, authorization, expected_proxy_headers
):
    calls = []
    manager = RecordingManager([])

    def record_proxy_manager(origin, **kwargs):
        calls.append((origin, kwargs))
        return manager

    monkeypatch.setattr(urllib3, "ProxyManager", record_proxy_manager)
    config = TelegramProxyConfig(
        origin="https://proxy.example:8443",
        authorization=authorization,
    )

    client = build_proxy_client(config)

    assert isinstance(client, TelegramProxyClient)
    assert calls[0][0] == "https://proxy.example:8443"
    assert "@" not in calls[0][0]
    assert calls[0][1]["cert_reqs"] == "CERT_REQUIRED"
    assert calls[0][1]["retries"] is False
    assert calls[0][1].get("proxy_headers") == expected_proxy_headers
    assert "ca_certs" not in calls[0][1]
    assert "cert_file" not in calls[0][1]
