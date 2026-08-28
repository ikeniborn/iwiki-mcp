"""Proxy-only HTTP adapter for Telegram Bot API traffic."""

import json
from dataclasses import dataclass
from typing import Protocol

import anyio
import urllib3

from .config import TelegramProxyConfig


@dataclass(frozen=True)
class ProxyResponse:
    status: int
    body: bytes


class TelegramHttpClient(Protocol):
    async def post_json(
        self, url: str, payload: dict[str, object]
    ) -> ProxyResponse: ...

    async def get_bytes(self, url: str) -> ProxyResponse: ...

    async def close(self) -> None: ...


class TelegramProxyClient:
    def __init__(self, manager: urllib3.ProxyManager) -> None:
        self._manager = manager
        self._timeout = urllib3.Timeout(connect=10, read=40)

    def _request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> ProxyResponse:
        response = self._manager.request(
            method,
            url,
            body=body,
            headers=headers,
            preload_content=True,
            redirect=False,
            retries=False,
            timeout=self._timeout,
        )
        try:
            return ProxyResponse(status=response.status, body=bytes(response.data))
        finally:
            response.release_conn()

    async def post_json(
        self, url: str, payload: dict[str, object]
    ) -> ProxyResponse:
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return await anyio.to_thread.run_sync(
            self._request,
            "POST",
            url,
            body,
            {"Content-Type": "application/json"},
        )

    async def get_bytes(self, url: str) -> ProxyResponse:
        return await anyio.to_thread.run_sync(self._request, "GET", url)

    async def close(self) -> None:
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(self._manager.clear)


def build_proxy_client(config: TelegramProxyConfig) -> TelegramProxyClient:
    arguments: dict[str, object] = {
        "cert_reqs": "CERT_REQUIRED",
        "retries": False,
    }
    if config.authorization is not None:
        arguments["proxy_headers"] = {
            "Proxy-Authorization": config.authorization,
        }
    manager = urllib3.ProxyManager(config.origin, **arguments)
    return TelegramProxyClient(manager)
