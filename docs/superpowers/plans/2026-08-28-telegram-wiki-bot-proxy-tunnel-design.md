---
review:
  plan_hash: 139aa534446af29c
  last_run: 2026-08-28
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-08-28-telegram-wiki-bot-proxy-tunnel-design-intent.md
  spec: docs/superpowers/specs/2026-08-28-telegram-wiki-bot-proxy-tunnel-design-design.md
---
# Telegram Wiki Bot HTTPS Proxy and Single-Container Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver one hardened application container whose Telegram bot uses an operator-managed literal HTTPS CONNECT proxy while hosted iwiki MCP, inference, and external PostgreSQL retain direct routes.

**Architecture:** Add a strict proxy configuration value and a small async adapter around `urllib3.ProxyManager`; keep Telegram URLs owned by `TelegramTransport` and run blocking proxy I/O in an AnyIO worker thread. Run hosted MCP, nginx, and the bot under foreground supervisord in one host-network container, with a local healthcheck, read-only root filesystem, and tmpfs-only runtime state.

**Tech Stack:** Python 3.10+, AnyIO, urllib3 2.x, HTTPX, MCP Streamable HTTP, PostgreSQL/psycopg, nginx, supervisord, Docker Compose, pytest/pytest-asyncio.

---

## Boundaries and success conditions

- `IWIKI_BOT_TELEGRAM_PROXY_URL` is required and accepts only literal lowercase `https://host:port` URLs with optional URL-encoded Basic credentials.
- Only Telegram Bot API and Telegram file requests use `urllib3.ProxyManager`; no standard proxy environment variable is configured or trusted.
- `InferenceClient`, the remote iwiki MCP client, and psycopg use direct routes. No direct Telegram fallback exists.
- The repository ships one Compose application service and no PostgreSQL, GOST, stunnel, or proxy-daemon service.
- Hosted MCP remains on `127.0.0.1:8765`; mounted nginx configuration exposes the operator-selected LAN address, preserves `Authorization`, accepts 16 MiB, disables access logging, and does not implement CONNECT.
- Telegram content stays in memory or `/tmp` tmpfs. The only bot liveness state is a timestamp in `/run` tmpfs.
- Existing user-bound, single-use confirmation and PostgreSQL compare-and-swap behavior remains unchanged.
- Every task commit updates the version quartet (`pyproject.toml`, `src/iwiki_mcp/__init__.py`, `tests/test_package.py`, `uv.lock`) to the version assigned below.

## Requirement coverage

| Spec requirement | Plan task | Verification evidence |
| --- | --- | --- |
| R1, R5 HTTPS proxy and URL contract | 1, 2 | parser matrix, manager-construction test, CONNECT integration fixture |
| R2 single application container | 4, 5 | rendered Compose JSON, image process inventory, supervisor restart test |
| R3 nginx ingress contract | 4, 5 | nginx configuration test, live authorization/body/stream checks |
| R4 external PostgreSQL only | 4, 5 | one-service assertion, same-host published-port and remote custom-port fixtures |
| R6 Telegram-only routing | 2, 3, 5 | request-origin assertions and direct-client constructor spies |
| R7 timeout/retry behavior | 2, 3 | no-POST-retry assertion, deterministic backoff/reset tests |
| R8 secret-safe failures | 1–5 | repr/error/log marker tests over unit and container streams |
| R9 transient content | 3, 5 | temp-file cleanup and restart/mount marker scans |
| R10 health and recovery | 3–5 | local health unit tests, child failure, stale heartbeat, proxy recovery |
| R11 delivery artifacts | 1–6 | file inventory, image build, Compose render, version quartet |
| R12 end-to-end acceptance | 5 | production-like blocked-direct-route scenario |

## File map

| Path | Responsibility |
| --- | --- |
| `src/iwiki_mcp/telegram_bot/config.py` | Secret-safe bot configuration and strict HTTPS proxy parsing. |
| `src/iwiki_mcp/telegram_bot/proxy.py` | urllib3 HTTPS proxy adapter; no Telegram business logic. |
| `src/iwiki_mcp/telegram_bot/transport.py` | Fixed Telegram origins, Bot API/file calls, polling, sanitized transport errors. |
| `src/iwiki_mcp/telegram_bot/runtime.py` | Deterministic backoff and timestamp-only heartbeat. |
| `src/iwiki_mcp/telegram_bot/main.py` | Direct dependency composition and startup retry loop. |
| `src/iwiki_mcp/telegram_bot/inference.py` | Direct HTTPX client with environment proxy trust disabled. |
| `src/iwiki_mcp/telegram_bot/iwiki.py` | Direct MCP HTTPX factory with environment proxy trust disabled. |
| `Dockerfile`, `.dockerignore`, `compose.yaml` | One reproducible application image/service and hardened runtime mounts. |
| `deploy/supervisord.conf` | Lifecycle for MCP, nginx, and bot child processes. |
| `deploy/nginx.conf.example` | LAN/Traefik reverse-proxy contract. |
| `deploy/healthcheck.py` | Local child/listener/heartbeat checks with stable error codes. |
| `tests/telegram_bot/` | Proxy configuration, transport, backoff, route isolation, and privacy tests. |
| `tests/deployment/` | Compose, image, nginx, supervisor, health, and production-like acceptance tests. |
| `README.md`, `docs/README.ru.md`, `docs/telegram-bot.md`, `docs/deployment.md` | Operator contract in English/Russian and deployment runbook. |

### Task 1: Add strict, secret-safe HTTPS proxy configuration

**Closes:** R1, R5, R8, R11.

**Version:** `0.7.196`.

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `src/iwiki_mcp/telegram_bot/config.py`
- Modify: `tests/test_package.py`
- Modify: `tests/telegram_bot/test_config_access.py`

- [ ] **Step 1: Write failing proxy parser and redaction tests**

Add a proxy value to `REQUIRED_ENV`, then add parameterized accepted/rejected cases and marker assertions:

```python
REQUIRED_ENV["IWIKI_BOT_TELEGRAM_PROXY_URL"] = (
    "https://proxy-user:proxy-password@proxy.example:8443"
)

@pytest.mark.parametrize(
    ("value", "origin", "authorization"),
    [
        ("https://proxy.example:8443", "https://proxy.example:8443", None),
        (
            "https://user%40team:p%3Ass@proxy.example:9443",
            "https://proxy.example:9443",
            "Basic dXNlckB0ZWFtOnA6c3M=",
        ),
    ],
)
def test_config_parses_literal_https_proxy(monkeypatch, value, origin, authorization):
    configure(monkeypatch)
    monkeypatch.setenv("IWIKI_BOT_TELEGRAM_PROXY_URL", value)

    proxy = BotConfig.load().telegram_proxy

    assert proxy.origin == origin
    assert proxy.authorization == authorization


@pytest.mark.parametrize(
    "value",
    [
        "http://proxy.example:8443",
        "HTTPS://proxy.example:8443",
        "socks5://proxy.example:1080",
        "https://proxy.example",
        "https://proxy.example:8443/path",
        "https://proxy.example:8443?query=1",
        "https://proxy.example:8443#fragment",
    ],
)
def test_config_rejects_non_contract_proxy_without_echo(monkeypatch, value):
    configure(monkeypatch)
    monkeypatch.setenv("IWIKI_BOT_TELEGRAM_PROXY_URL", value)

    with pytest.raises(BotConfigError) as captured:
        BotConfig.load()

    assert str(captured.value) == "invalid IWIKI_BOT_TELEGRAM_PROXY_URL"
    assert value not in str(captured.value)


def test_config_repr_hides_all_credentials(monkeypatch):
    configure(monkeypatch)
    config_repr = repr(BotConfig.load())

    for marker in ("telegram-token", "iwiki-token", "llm-key", "proxy-password"):
        assert marker not in config_repr
```

- [ ] **Step 2: Run the focused tests and confirm the missing field/parser failure**

Run:

```bash
uv run pytest -q tests/telegram_bot/test_config_access.py
```

Expected: failures mention missing `telegram_proxy` or the missing required environment name; no marker value appears in pytest exception text.

- [ ] **Step 3: Implement the minimal parser and redacted dataclasses**

Use only standard URL parsing and a stable configuration error:

```python
import base64
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit


@dataclass(frozen=True)
class TelegramProxyConfig:
    origin: str = field(repr=False)
    authorization: str | None = field(default=None, repr=False)


def _parse_telegram_proxy(value: str) -> TelegramProxyConfig:
    if not value.startswith("https://"):
        raise BotConfigError("invalid IWIKI_BOT_TELEGRAM_PROXY_URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise BotConfigError("invalid IWIKI_BOT_TELEGRAM_PROXY_URL") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or port is None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise BotConfigError("invalid IWIKI_BOT_TELEGRAM_PROXY_URL")
    host = parsed.hostname
    authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    authorization = None
    if parsed.username is not None:
        credentials = f"{unquote(parsed.username)}:{unquote(parsed.password or '')}"
        encoded = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
        authorization = f"Basic {encoded}"
    return TelegramProxyConfig(f"https://{authority}", authorization)
```

Declare `telegram_token`, `iwiki_token`, `llm_key`, and `telegram_proxy` with `field(repr=False)`. Load `IWIKI_BOT_TELEGRAM_PROXY_URL` as required, call `_parse_telegram_proxy`, and never retain the raw URL.

- [ ] **Step 4: Add urllib3 and synchronize version metadata**

Add `"urllib3>=2.5,<3"` to runtime dependencies. Set these exact literals, then regenerate the lock:

```python
# src/iwiki_mcp/__init__.py
__version__ = "0.7.196"

# tests/test_package.py
assert iwiki_mcp.__version__ == "0.7.196"
```

```toml
# pyproject.toml
version = "0.7.196"
```

Run:

```bash
uv lock
uv run pytest -q tests/telegram_bot/test_config_access.py tests/test_package.py
```

Expected: all focused tests pass and `uv.lock` contains the project at `0.7.196` plus a direct urllib3 dependency.

- [ ] **Step 5: Commit the configuration boundary**

```bash
git add pyproject.toml uv.lock src/iwiki_mcp/__init__.py src/iwiki_mcp/telegram_bot/config.py tests/test_package.py tests/telegram_bot/test_config_access.py
git commit -m "feat(telegram): validate HTTPS proxy configuration"
```

### Task 2: Route every Telegram request through urllib3 ProxyManager

**Closes:** R1, R6, R7, R8.

**Version:** `0.7.197`.

**Files:**
- Create: `src/iwiki_mcp/telegram_bot/proxy.py`
- Create: `tests/telegram_bot/test_proxy.py`
- Modify: `src/iwiki_mcp/telegram_bot/transport.py`
- Modify: `src/iwiki_mcp/telegram_bot/main.py`
- Modify: `tests/telegram_bot/test_transport.py`
- Modify: version quartet

- [ ] **Step 1: Write failing adapter tests for proxy construction and request semantics**

Use a recording manager injected into the adapter:

```python
from dataclasses import dataclass

import iwiki_mcp.telegram_bot.proxy as proxy_module


@dataclass
class FakeResponse:
    status: int
    data: bytes
    released: bool = False

    def release_conn(self):
        self.released = True


class RecordingManager:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return next(self.responses)


@pytest.mark.asyncio
async def test_proxy_adapter_disables_redirects_and_retries():
    manager = RecordingManager([FakeResponse(200, b'{"ok":true,"result":[]}')])
    client = TelegramProxyClient(manager)

    response = await client.post_json(
        "https://api.telegram.org/botTOKEN/getUpdates", {"timeout": 30}
    )

    assert response.status == 200
    method, url, kwargs = manager.calls[0]
    assert (method, url) == (
        "POST", "https://api.telegram.org/botTOKEN/getUpdates"
    )
    assert kwargs["redirect"] is False
    assert kwargs["retries"] is False
    assert kwargs["timeout"].connect_timeout == 10
    assert kwargs["timeout"].read_timeout == 40


def test_proxy_manager_uses_tls_origin_and_separate_credentials(monkeypatch):
    seen = {}

    def manager(origin, **kwargs):
        seen.update(origin=origin, kwargs=kwargs)
        return object()

    monkeypatch.setattr(proxy_module.urllib3, "ProxyManager", manager)
    build_proxy_client(TelegramProxyConfig("https://proxy.example:8443", "Basic abc"))

    assert seen["origin"] == "https://proxy.example:8443"
    assert seen["kwargs"]["proxy_headers"] == {"Proxy-Authorization": "Basic abc"}
    assert seen["kwargs"]["cert_reqs"] == "CERT_REQUIRED"
```

- [ ] **Step 2: Run the adapter test and confirm the module is absent**

```bash
uv run pytest -q tests/telegram_bot/test_proxy.py
```

Expected: collection fails because `iwiki_mcp.telegram_bot.proxy` does not exist.

- [ ] **Step 3: Implement the small blocking adapter behind an async interface**

Create `proxy.py` with these complete public boundaries:

```python
from dataclasses import dataclass
import json
from typing import Protocol

import anyio
import urllib3

from .config import TelegramProxyConfig


@dataclass(frozen=True)
class ProxyResponse:
    status: int
    body: bytes


class TelegramHttpClient(Protocol):
    async def post_json(self, url: str, payload: dict[str, object]) -> ProxyResponse: ...
    async def get_bytes(self, url: str) -> ProxyResponse: ...
    async def close(self) -> None: ...


class TelegramProxyClient:
    def __init__(self, manager: urllib3.ProxyManager) -> None:
        self._manager = manager
        self._timeout = urllib3.Timeout(connect=10, read=40)

    def _request(self, method: str, url: str, body: bytes | None = None) -> ProxyResponse:
        headers = {"Content-Type": "application/json"} if body is not None else None
        response = self._manager.request(
            method,
            url,
            body=body,
            headers=headers,
            timeout=self._timeout,
            retries=False,
            redirect=False,
        )
        try:
            return ProxyResponse(response.status, bytes(response.data))
        finally:
            response.release_conn()

    async def post_json(self, url: str, payload: dict[str, object]) -> ProxyResponse:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return await anyio.to_thread.run_sync(self._request, "POST", url, body)

    async def get_bytes(self, url: str) -> ProxyResponse:
        return await anyio.to_thread.run_sync(self._request, "GET", url)

    async def close(self) -> None:
        await anyio.to_thread.run_sync(self._manager.clear)


def build_proxy_client(config: TelegramProxyConfig) -> TelegramProxyClient:
    proxy_headers = (
        {"Proxy-Authorization": config.authorization}
        if config.authorization is not None
        else None
    )
    manager = urllib3.ProxyManager(
        config.origin,
        proxy_headers=proxy_headers,
        cert_reqs="CERT_REQUIRED",
        retries=False,
    )
    return TelegramProxyClient(manager)
```

Catch `urllib3.exceptions.HTTPError`, `OSError`, Unicode/JSON errors only in `TelegramTransport`; translate them with `raise TelegramError("telegram_request_failed") from None` so low-level URLs cannot escape.

- [ ] **Step 4: Make TelegramTransport accept only the injected proxy adapter**

Change the constructor to require `http: TelegramHttpClient`; remove `httpx`, `_owns_http`, and implicit client creation. `_api` uses `post_json`; `_download_voice` uses `get_bytes`. Reject non-2xx status, malformed JSON, and non-Telegram redirects using the same stable errors.

Extend transport tests with one recording adapter and assert the exact URL sequence:

```python
assert recording.urls == [
    "https://api.telegram.org/bottelegram-token/getUpdates",
    "https://api.telegram.org/bottelegram-token/sendMessage",
    "https://api.telegram.org/bottelegram-token/getFile",
    "https://api.telegram.org/file/bottelegram-token/voice/file_1.ogg",
]
```

Create the adapter in `run_bot` from `config.telegram_proxy`, pass it to `TelegramTransport`, and close it in `finally`. There is no branch that constructs another Telegram client.

- [ ] **Step 5: Prove POST ambiguity is not retried and run transport tests**

Add a manager that raises after recording one `sendMessage` request; assert one call and sanitized `telegram_request_failed` with no URL, token, proxy host, or response marker.

```bash
uv run pytest -q tests/telegram_bot/test_proxy.py tests/telegram_bot/test_transport.py
```

Expected: all tests pass; every manager request has `retries=False` and `redirect=False`.

- [ ] **Step 6: Set version quartet to `0.7.197`, run `uv lock`, and commit**

```bash
uv lock
uv run pytest -q tests/test_package.py tests/telegram_bot/test_proxy.py tests/telegram_bot/test_transport.py
git add pyproject.toml uv.lock src/iwiki_mcp/__init__.py src/iwiki_mcp/telegram_bot/proxy.py src/iwiki_mcp/telegram_bot/transport.py src/iwiki_mcp/telegram_bot/main.py tests/test_package.py tests/telegram_bot/test_proxy.py tests/telegram_bot/test_transport.py
git commit -m "feat(telegram): route Bot API through HTTPS proxy"
```

### Task 3: Add bounded recovery, direct-route isolation, and transient heartbeat

**Closes:** R6, R7, R8, R9, R10.

**Version:** `0.7.198`.

**Files:**
- Create: `src/iwiki_mcp/telegram_bot/runtime.py`
- Create: `tests/telegram_bot/test_runtime.py`
- Modify: `src/iwiki_mcp/telegram_bot/transport.py`
- Modify: `src/iwiki_mcp/telegram_bot/main.py`
- Modify: `src/iwiki_mcp/telegram_bot/inference.py`
- Modify: `src/iwiki_mcp/telegram_bot/iwiki.py`
- Modify: `tests/telegram_bot/test_transport.py`
- Modify: `tests/telegram_bot/test_inference.py`
- Modify: `tests/telegram_bot/test_iwiki_client.py`
- Modify: `tests/telegram_bot/test_conversation_read.py`
- Modify: version quartet

- [ ] **Step 1: Write deterministic backoff and heartbeat tests**

```python
def test_backoff_caps_and_resets():
    backoff = Backoff(initial=1, maximum=8, jitter_ratio=0.25)
    assert [backoff.next_delay(0.0) for _ in range(5)] == [0.75, 1.5, 3.0, 6.0, 6.0]
    backoff.reset()
    assert backoff.next_delay(1.0) == 1.25


def test_heartbeat_writes_only_timestamp(tmp_path):
    path = tmp_path / "heartbeat"
    Heartbeat(path, clock=lambda: 123.5).touch()
    assert path.read_text(encoding="ascii") == "123.500000\n"
```

Add an async polling test with injected sleep/random/heartbeat. Two sanitized failures must produce delays, one success must reset delay and touch once, and the update offset must remain unchanged across failed polls.

- [ ] **Step 2: Run the new tests and confirm missing runtime behavior**

```bash
uv run pytest -q tests/telegram_bot/test_runtime.py tests/telegram_bot/test_transport.py
```

Expected: missing `runtime` module or absent retry parameters causes failure.

- [ ] **Step 3: Implement Backoff and Heartbeat without content state**

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class Backoff:
    initial: float = 1.0
    maximum: float = 30.0
    jitter_ratio: float = 0.2
    _attempt: int = 0

    def next_delay(self, random_value: float) -> float:
        base = min(self.maximum, self.initial * (2 ** self._attempt))
        self._attempt += 1
        return base * (1 - self.jitter_ratio + 2 * self.jitter_ratio * random_value)

    def reset(self) -> None:
        self._attempt = 0


class Heartbeat:
    def __init__(self, path: Path, clock: Callable[[], float]) -> None:
        self._path = path
        self._clock = clock

    def touch(self) -> None:
        self._path.write_text(f"{self._clock():.6f}\n", encoding="ascii")
```

Production constructs `Heartbeat(Path("/run/iwiki-telegram-bot.heartbeat"), time.monotonic)`.

- [ ] **Step 4: Add sanitized polling and startup retry loops**

`poll_forever` catches only `TelegramError`, records `operation="poll"`, `outcome="retry"`, numeric delay/elapsed fields, sleeps with capped jitter, and resets/touches after a successful long poll. It never logs the exception object.

`run_bot` retries `InferenceError` and `RemoteIwikiError` around dependency startup with a separate `Backoff`. Configuration errors remain outside the loop. Close every created inference, remote MCP context, and proxy adapter before sleeping.

Tests capture log records and assert unique markers for token, proxy URL, username, password, update text, transcription, filename, and provider response do not appear in `record.getMessage()` or `record.__dict__`.

- [ ] **Step 5: Disable environment-proxy inheritance for non-Telegram clients**

Use this constructor in `InferenceClient`:

```python
self._http = http or httpx.AsyncClient(timeout=60, trust_env=False)
```

Add the MCP factory and pass it to `streamablehttp_client`:

```python
def _direct_httpx_client(headers=None, timeout=None, auth=None):
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout,
        auth=auth,
        follow_redirects=True,
        trust_env=False,
    )
```

Constructor spies must observe `trust_env=False` for inference and remote iwiki. Preserve psycopg configuration unchanged and assert no standard proxy environment variable is read by these clients.

- [ ] **Step 6: Extend voice cleanup coverage to failure paths**

Keep the existing `NamedTemporaryFile` context manager. Add a transcription failure test that records the temporary path and asserts it no longer exists after `InferenceError`; assert no audio/content marker is written outside the supplied tmp directory.

```bash
uv run pytest -q tests/telegram_bot
```

Expected: all Telegram bot tests pass, including confirmation replay and compare-and-swap tests.

- [ ] **Step 7: Set version quartet to `0.7.198`, run `uv lock`, and commit**

```bash
uv lock
git add pyproject.toml uv.lock src/iwiki_mcp/__init__.py src/iwiki_mcp/telegram_bot tests/test_package.py tests/telegram_bot
git commit -m "feat(telegram): add safe proxy recovery"
```

### Task 4: Package MCP, nginx, and bot in one hardened container

**Closes:** R2, R3, R4, R9, R10, R11.

**Version:** `0.7.199`.

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `compose.yaml`
- Create: `deploy/supervisord.conf`
- Create: `deploy/nginx.conf.example`
- Create: `deploy/healthcheck.py`
- Create: `tests/deployment/test_healthcheck.py`
- Create: `tests/deployment/fixtures/render.env`
- Modify: version quartet

- [ ] **Step 1: Write failing local healthcheck tests**

Load `deploy/healthcheck.py` with `importlib.util`. Inject process, HTTP, file, and clock functions. Cover exactly: all healthy; each required child absent/not RUNNING; MCP unavailable; nginx unavailable; missing/malformed/stale heartbeat. Assert failure output is one stable code such as `child_not_running`, never injected environment or content markers.

```bash
uv run pytest -q tests/deployment/test_healthcheck.py
```

Expected: collection fails because the deployment healthcheck is absent.

- [ ] **Step 2: Implement the local-only healthcheck**

`deploy/healthcheck.py` must expose testable functions and this exact decision order:

```python
REQUIRED_CHILDREN = frozenset({"iwiki-mcp", "nginx", "telegram-bot"})


def check_heartbeat(path, maximum_age, clock):
    try:
        observed = float(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return False
    age = clock() - observed
    return 0 <= age <= maximum_age


def main():
    if not children_running(REQUIRED_CHILDREN):
        return fail("child_not_running")
    if not local_http_ok("127.0.0.1", 8765, "/mcp"):
        return fail("mcp_unavailable")
    if not local_http_ok(
        os.environ["IWIKI_INGRESS_HEALTH_HOST"],
        int(os.environ.get("IWIKI_INGRESS_HEALTH_PORT", "8766")),
        "/mcp",
    ):
        return fail("nginx_unavailable")
    if not check_heartbeat(
        Path("/run/iwiki-telegram-bot.heartbeat"),
        float(os.environ.get("IWIKI_BOT_HEARTBEAT_MAX_AGE_SECONDS", "120")),
        time.monotonic,
    ):
        return fail("telegram_heartbeat_stale")
    return 0
```

`children_running` calls `supervisorctl -c /etc/supervisor/supervisord.conf status` without printing captured output. `local_http_ok` accepts the expected unauthenticated `401` or `405` response and never sends credentials.

- [ ] **Step 3: Add the supervisor and nginx contracts**

`deploy/supervisord.conf` runs in foreground, writes no supervisor logfile, and defines exactly these programs:

```ini
[supervisord]
nodaemon=true
logfile=/dev/null
pidfile=/run/supervisord.pid

[unix_http_server]
file=/run/supervisor.sock
chmod=0600

[supervisorctl]
serverurl=unix:///run/supervisor.sock

[rpcinterface:supervisor]
supervisor.rpcinterface_factory=supervisor.rpcinterface:make_main_rpcinterface

[program:iwiki-mcp]
command=/app/.venv/bin/iwiki-mcp serve --transport streamable-http
autorestart=unexpected
startsecs=5
startretries=1000000
stopasgroup=true
killasgroup=true
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0

[program:nginx]
command=/usr/sbin/nginx -g "daemon off;"
autorestart=unexpected
startsecs=2
startretries=1000000
stopasgroup=true
killasgroup=true
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0

[program:telegram-bot]
command=/app/.venv/bin/iwiki-telegram-bot
autorestart=unexpected
startsecs=5
startretries=1000000
stopasgroup=true
killasgroup=true
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0
```

`deploy/nginx.conf.example` omits the nginx `user` directive because the whole container already runs as UID/GID `10001`. It sets `error_log /dev/stderr warn`, `access_log off`, `client_max_body_size 16m`, host-specific `listen 192.168.68.123:8766`, and:

```nginx
location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_http_version 1.1;
    proxy_set_header Authorization $http_authorization;
    proxy_set_header Host $host;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_read_timeout 300s;
}
```

Put nginx pid and all client/proxy temp paths under `/run` or `/tmp`; define no outbound proxy directives.

- [ ] **Step 4: Add one runtime image and one Compose service**

Use these exact stages and runtime packages; `uv sync --frozen` makes Python resolution come only from `uv.lock`:

```dockerfile
FROM ghcr.io/astral-sh/uv:0.8.14 AS uv
FROM python:3.12.11-slim-bookworm

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates nginx supervisor \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 iwiki \
    && useradd --uid 10001 --gid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin iwiki

WORKDIR /app
COPY --from=uv /uv /uvx /usr/local/bin/
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY deploy ./deploy
RUN uv sync --frozen --no-dev --no-editable \
    && chown -R 10001:10001 /app

COPY deploy/supervisord.conf /etc/supervisor/supervisord.conf

USER 10001:10001
HEALTHCHECK NONE
ENTRYPOINT ["/usr/bin/supervisord", "-c", "/etc/supervisor/supervisord.conf"]
```

The final image contains only the application plus nginx, supervisor, and CA certificates. It does not install curl, PostgreSQL server/client tools, GOST, or stunnel.

`compose.yaml` contains exactly one service named `iwiki` with:

```yaml
services:
  iwiki:
    build: .
    network_mode: host
    restart: unless-stopped
    read_only: true
    user: "10001:10001"
    tmpfs:
      - /run:uid=10001,gid=10001,mode=0750
      - /tmp:uid=10001,gid=10001,mode=1770
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    env_file:
      - ${IWIKI_RUNTIME_ENV_FILE:-/opt/iwiki-mcp/runtime.env}
    environment:
      IWIKI_SERVER_CONFIG: /etc/iwiki/server.toml
    volumes:
      - ${IWIKI_SERVER_CONFIG_FILE:-/opt/iwiki-mcp/server.toml}:/etc/iwiki/server.toml:ro
      - ${IWIKI_NGINX_CONFIG_FILE:-/opt/iwiki-mcp/nginx.conf}:/etc/nginx/nginx.conf:ro
    healthcheck:
      test: ["CMD", "/app/.venv/bin/python", "/app/deploy/healthcheck.py"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 45s
    stop_grace_period: 60s
```

Do not add `ports`, `depends_on`, PostgreSQL, GOST, stunnel, or a second service. `.dockerignore` excludes Git state, caches, tests, local env/config files, docs build artifacts, and secrets while retaining runtime source and `deploy/`.

Create `tests/deployment/fixtures/render.env` with non-secret fixture paths for `IWIKI_RUNTIME_ENV_FILE`, `IWIKI_SERVER_CONFIG_FILE`, and `IWIKI_NGINX_CONFIG_FILE`; add corresponding fixture files under the same directory so `docker compose config` never reads `/opt` during tests.

- [ ] **Step 5: Run static deployment checks**

```bash
uv run pytest -q tests/deployment/test_healthcheck.py
docker compose --env-file tests/deployment/fixtures/render.env config --format json
docker build -t iwiki-mcp:proxy-test .
```

Expected: health tests pass; rendered JSON has one `iwiki` service with host networking and no published port; image builds as unprivileged UID `10001` and contains no PostgreSQL server, GOST, or stunnel executable.

- [ ] **Step 6: Set version quartet to `0.7.199`, run `uv lock`, and commit**

```bash
uv lock
git add Dockerfile .dockerignore compose.yaml deploy pyproject.toml uv.lock src/iwiki_mcp/__init__.py tests/test_package.py tests/deployment/test_healthcheck.py
git commit -m "feat(deploy): add single supervised container"
```

### Task 5: Prove container, ingress, proxy, PostgreSQL, and privacy behavior

**Closes:** R1–R12 acceptance requirements.

**Version:** `0.7.200`.

**Files:**
- Create: `tests/deployment/conftest.py`
- Create: `tests/deployment/test_compose_contract.py`
- Create: `tests/deployment/test_nginx_contract.py`
- Create: `tests/deployment/test_single_container_acceptance.py`
- Create: `tests/telegram_bot/test_https_proxy_integration.py`
- Modify: version quartet

- [ ] **Step 1: Add a real TLS-in-TLS proxy integration fixture**

The fixture starts a local TLS listener with a test-only CA, records the first request line, requires `CONNECT api.telegram.org:443`, then terminates the inner TLS stream as `api.telegram.org` and serves deterministic Bot API/file responses. Inject a test-created `urllib3.ProxyManager` into `TelegramProxyClient`; production CA construction remains unchanged.

The test runs `poll_once`, handles one text reply and one voice update, and asserts recorded tunneled paths include:

```python
assert observed == [
    ("POST", "/botTOKEN/getUpdates"),
    ("POST", "/botTOKEN/sendMessage"),
    ("POST", "/botTOKEN/getFile"),
    ("GET", "/file/botTOKEN/voice/file_1.ogg"),
    ("POST", "/botTOKEN/sendMessage"),
]
assert connect_targets == ["api.telegram.org:443"]
```

Block resolver/connect calls for direct `api.telegram.org`; the test must still pass through the proxy. After proxy shutdown, the next poll raises only `telegram_request_failed` and the direct-connect spy remains empty.

- [ ] **Step 2: Test the rendered Compose and image inventory**

Render Compose as JSON in `test_compose_contract.py`. Assert one service, `network_mode == "host"`, `restart == "unless-stopped"`, read-only root, `/run` and `/tmp` tmpfs, two read-only config mounts, no standard proxy environment keys, and no service/image/command containing `postgres`, `gost`, or `stunnel`.

Inspect the built image as UID `10001` and assert the executable inventory contains `iwiki-mcp`, `iwiki-telegram-bot`, `nginx`, and `supervisord`, while `postgres`, `gost`, and `stunnel` are absent.

- [ ] **Step 3: Exercise nginx contract in the running image**

Start the image with a test nginx listener on loopback and a deterministic MCP upstream. Prove:

```python
assert forwarded_authorization == "Bearer ingress-marker"
assert response_for_16_mib.status_code != 413
assert response_over_16_mib.status_code == 413
assert first_stream_chunk_arrived_before_upstream_completion is True
assert access_log_marker not in container_logs
```

Also prove missing/invalid authorization is forwarded and rejected by the MCP middleware, not accepted by nginx.

- [ ] **Step 4: Exercise supervisor, health, and restart behavior**

Start the full test image with fake external proxy/inference/PostgreSQL dependencies. Wait for all three children and healthy status. TERM the container and assert all child process groups exit within 60 seconds. In separate runs, stop each child and assert supervisor restarts it; stale the heartbeat and assert unhealthy without an extra Telegram request; restore the proxy and assert polling resumes without restarting the container.

- [ ] **Step 5: Exercise external PostgreSQL paths and existing write safety**

Use the existing disposable PostgreSQL fixtures outside the application Compose project. Run once through a host-published loopback custom port and once through the fixture's configured remote/custom endpoint. Assert hosted MCP can read and perform one confirmed write, replay cannot write twice, stale revision/section hash reports conflict, invalid runtime principal and incompatible schema fail startup, and rendered application Compose still has no database service.

- [ ] **Step 6: Scan complete container output and mounts for markers**

Inject unique markers for all credentials, proxy origin, update text, reply, filename, audio, transcription, confirmation preview, and provider failure. Force proxy, inference, MCP, nginx, and voice-processing failures. Assert none appears in stdout, stderr, `docker logs`, `/run`, `/tmp` after request cleanup, mounted configuration, or image history. Restart the container and assert no Telegram/confirmation state survives.

- [ ] **Step 7: Run the acceptance layers**

```bash
uv run pytest -q tests/telegram_bot/test_https_proxy_integration.py tests/deployment/test_compose_contract.py tests/deployment/test_nginx_contract.py
uv run pytest -q -m postgres_integration tests/deployment/test_single_container_acceptance.py
```

Expected: every layer passes; a missing Docker daemon or disposable external PostgreSQL fixture is reported as an explicit environment skip, never as a product pass.

- [ ] **Step 8: Set version quartet to `0.7.200`, run `uv lock`, and commit**

```bash
uv lock
git add pyproject.toml uv.lock src/iwiki_mcp/__init__.py tests/test_package.py tests/telegram_bot/test_https_proxy_integration.py tests/deployment
git commit -m "test(deploy): verify isolated Telegram proxy path"
```

### Task 6: Publish operator documentation and run final reconciliation

**Closes:** R4, R5, R8–R12 and all documentation acceptance.

**Version:** `0.7.201`.

**Files:**
- Modify: `README.md`
- Modify: `docs/README.ru.md`
- Modify: `docs/telegram-bot.md`
- Create: `docs/deployment.md`
- Modify: version quartet

- [ ] **Step 1: Replace separate-bot deployment guidance with the supported container path**

Document these exact operator inputs without real values:

```text
/opt/iwiki-mcp/server.toml       hosted MCP and external PostgreSQL endpoint
/opt/iwiki-mcp/nginx.conf        LAN/Traefik listener and loopback upstream
/opt/iwiki-mcp/runtime.env       owner-only runtime secrets and bot settings
```

State that same-host PostgreSQL must publish a host port such as `127.0.0.1:55432`; remote PostgreSQL supplies its own host/custom port and should use `sslmode = "verify-full"`. State that the project neither creates PostgreSQL nor runs schema migrations.

- [ ] **Step 2: Document the proxy and route boundary**

Add accepted proxy examples and explicit rejections:

```text
IWIKI_BOT_TELEGRAM_PROXY_URL=https://proxy.example:8443
IWIKI_BOT_TELEGRAM_PROXY_URL=https://user:password@proxy.example:9443
```

Explain TLS to proxy, CONNECT to `api.telegram.org:443`, and inner Telegram TLS. State that all Telegram API/file traffic uses this proxy, there is no direct fallback, and inference/iwiki/PostgreSQL remain direct because standard proxy environment variables are absent and HTTPX clients use `trust_env=False`.

- [ ] **Step 3: Document migration from `iwiki-mcp-proxy-1`**

Provide an ordered, reversible runbook: copy and validate configs; build image; render Compose; stop only the old nginx proxy after the combined container is healthy on a non-conflicting validation listener; switch Traefik/LAN target; verify Authorization and streaming; then remove the old container. Rollback stops the combined container and restarts the old nginx proxy without modifying PostgreSQL.

- [ ] **Step 4: Document health, privacy, and failure semantics**

List the three supervised children, local health components, heartbeat window, `unless-stopped`, and 60-second graceful stop. State that proxy failure makes Telegram unhealthy but does not reroute traffic; `/run` and `/tmp` are tmpfs; Telegram content and confirmations do not persist; logs contain stable operational fields only.

- [ ] **Step 5: Run documentation and repository verification**

Set version quartet to `0.7.201`, regenerate the lock, then run:

```bash
uv lock
uv run pytest -q tests/telegram_bot tests/deployment tests/test_package.py
uv run pytest -q
uv run iwiki-mcp --help
uv run iwiki-telegram-bot --help
docker compose --env-file tests/deployment/fixtures/render.env config --quiet
docker build -t iwiki-mcp:proxy-final .
git diff --check
```

Expected: full suite and both console-script smoke tests pass; Compose renders one service; image builds; no whitespace errors remain. Record Docker/PostgreSQL skips separately and execute them in the production-like acceptance environment before release.

- [ ] **Step 6: Update iwiki architecture documentation and lint**

Update the bound `iwiki-mcp` domain with the delivered single-container boundary, Telegram-only HTTPS proxy route, external PostgreSQL ownership, nginx ingress, and verified commands. Run `wiki_lint`; task/history long-lead advisories remain non-blocking, while broken, stale, or missing-source findings must be resolved before result reconciliation.

- [ ] **Step 7: Commit documentation and final version**

```bash
git add README.md docs/README.ru.md docs/telegram-bot.md docs/deployment.md pyproject.toml uv.lock src/iwiki_mcp/__init__.py tests/test_package.py
git commit -m "docs(deploy): document Telegram proxy container"
```

After this commit, run `$check-chain result docs/superpowers/plans/2026-08-28-telegram-wiki-bot-proxy-tunnel-design.md` before branch finishing or PR creation.
