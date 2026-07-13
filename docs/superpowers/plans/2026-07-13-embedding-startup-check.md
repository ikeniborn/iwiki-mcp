---
review:
  plan_hash: c00291b9115841f2
  last_run: 2026-07-13
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-07-13-embedding-startup-check-intent.md
  spec: docs/superpowers/specs/2026-07-13-embedding-startup-check-design.md
result_check:
  verdict: OK
  plan_hash: c00291b9115841f2
  last_run: 2026-07-13
  reviewed: true
  docs_checked: true
---
# Embedding Endpoint Startup Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refuse to start the stdio MCP server when its configured embeddings endpoint is unusable, and emit a safe actionable stderr diagnostic before protocol startup.

**Architecture:** Keep environment validation in `engine/config.py`, add an isolated one-attempt probe in `engine/embed.py`, and orchestrate the hard gate in `server.main()` before `mcp.run()`. Preserve the runtime embedding client and prove the subprocess path with a deterministic loopback stub.

**Tech Stack:** Python 3.10+, FastMCP stdio, `httpx`, `pytest`, `pytest-asyncio`, standard-library `http.server`, `uv`, iwiki MCP tools.

**Intent:** `docs/superpowers/intents/2026-07-13-embedding-startup-check-intent.md`

**Spec:** `docs/superpowers/specs/2026-07-13-embedding-startup-check-design.md`

---

## File Map

- `src/iwiki_mcp/engine/config.py` and `tests/engine/test_config.py`: local embedding config validation.
- `src/iwiki_mcp/engine/embed.py` and `tests/engine/test_embed.py`: startup probe and protocol validation.
- `src/iwiki_mcp/server.py` and new `tests/test_server_startup.py`: startup order and stderr failure contract.
- `tests/test_mcp_smoke.py`: subprocess coverage through a loopback embedding stub.
- `README.md`, `docs/README.ru.md`, `pyproject.toml`, `src/iwiki_mcp/__init__.py`, `uv.lock`: user contract and synchronized patch version.
- iwiki pages `mcp-server`, `indexing`, `installation`: documented implemented behavior.

## Requirement Traceability

| Requirement | Tasks |
|---|---|
| R1 Startup Order | 3, 4 |
| R2 Configuration Validation | 1 |
| R3 Dedicated Endpoint Probe | 2 |
| R4 Probe Response Validation | 2 |
| R5 Startup Failure Diagnostic | 3 |
| R6 Existing Runtime Behavior | 2, 6 |
| R7 MCP Smoke Coverage | 4 |
| R8 Documentation and Version | 5 |

### Task 1: Validate Embedding Configuration

**Files:**
- Modify: `tests/engine/test_config.py`
- Modify: `src/iwiki_mcp/engine/config.py`

- [x] **Step 1: Write failing config tests**

Add a fixture setting URL/key, then exact cases for blank model and invalid dimensions:

```python
@pytest.fixture
def embedding_env(monkeypatch):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "test-secret")


def test_empty_embedding_model_is_config_error(monkeypatch, embedding_env):
    monkeypatch.setenv("IWIKI_EMBED_MODEL", "  ")
    with pytest.raises(ConfigError, match="IWIKI_EMBED_MODEL"):
        Config.load()


@pytest.mark.parametrize("value", ["abc", "0", "-1"])
def test_invalid_embedding_dimensions_are_config_error(
    monkeypatch, embedding_env, value
):
    monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", value)
    with pytest.raises(ConfigError, match="IWIKI_EMBED_DIMENSIONS"):
        Config.load()
```

- [x] **Step 2: Verify RED**

```bash
uv run pytest -q tests/engine/test_config.py
```

Expected: new cases fail because blank model is accepted and invalid dimensions leak `ValueError` or accept non-positive values.

- [x] **Step 3: Implement minimal parsing**

Before `Config(...)` in `Config.load()`:

```python
embed_model = getenv("IWIKI_EMBED_MODEL", "text-embedding-3-small").strip()
if not embed_model:
    raise ConfigError("IWIKI_EMBED_MODEL must be a non-empty model name. Halting.")
raw_dimensions = getenv("IWIKI_EMBED_DIMENSIONS", "1536").strip()
try:
    dimensions = int(raw_dimensions)
except ValueError as exc:
    raise ConfigError(
        "IWIKI_EMBED_DIMENSIONS must be a positive integer. Halting."
    ) from exc
if dimensions <= 0:
    raise ConfigError(
        "IWIKI_EMBED_DIMENSIONS must be a positive integer. Halting."
    )
```

Pass these two locals into `Config`; do not alter unrelated tuning parsing.

- [x] **Step 4: Verify GREEN and commit**

```bash
uv run pytest -q tests/engine/test_config.py
git add src/iwiki_mcp/engine/config.py tests/engine/test_config.py
git commit -m "fix: validate embedding startup configuration"
```

Expected: config tests pass and no output contains `test-secret`.

### Task 2: Add the Dedicated Endpoint Probe

**Files:**
- Modify: `tests/engine/test_embed.py`
- Modify: `src/iwiki_mcp/engine/embed.py`

- [x] **Step 1: Write the successful request test**

Import `dataclasses` and `probe_embedding_endpoint`; record `httpx.post` arguments:

```python
def test_probe_posts_once_and_validates_vector(monkeypatch):
    calls = []

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"index": 0, "embedding": [0.1, 0.2]}]}

    monkeypatch.setattr(
        embed_mod.httpx,
        "post",
        lambda url, **kwargs: calls.append((url, kwargs)) or Resp(),
    )
    cfg = dataclasses.replace(_cfg(), dimensions=2)
    assert probe_embedding_endpoint(cfg) is None
    assert calls == [("http://x/embeddings", {
        "json": {"model": "m", "input": ["iwiki startup probe"], "dimensions": 2},
        "headers": {"Authorization": "Bearer k"}, "timeout": 10.0,
    })]
```

- [x] **Step 2: Write all failure tests**

Use parameterized fake JSON responses for `{}`, empty/two-row `data`, missing `embedding`, empty vector, boolean, `NaN`, and one-value dimension mismatch. Expected safe fragments respectively: `exactly one embedding result`, `missing embedding vector`, `non-empty numeric vector`, `finite numbers`, and `dimension mismatch`. Add separate one-call tests for malformed JSON, `httpx.TimeoutException`, `httpx.ConnectError`, and HTTP 401 whose response body contains `test-secret-response-body`; assert that body is absent from `EmbedError`.

```python
@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({}, "exactly one embedding result"),
        ({"data": []}, "exactly one embedding result"),
        ({"data": [{}, {}]}, "exactly one embedding result"),
        ({"data": [{}]}, "missing embedding vector"),
        ({"data": [{"embedding": []}]}, "non-empty numeric vector"),
        ({"data": [{"embedding": [True, 0.2]}]}, "finite numbers"),
        ({"data": [{"embedding": [float("nan"), 0.2]}]}, "finite numbers"),
        ({"data": [{"embedding": [0.1]}]}, "dimension mismatch"),
    ],
)
def test_probe_rejects_invalid_response(monkeypatch, payload, reason):
    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    monkeypatch.setattr(embed_mod.httpx, "post", lambda *a, **k: Resp())
    with pytest.raises(EmbedError, match=reason):
        probe_embedding_endpoint(dataclasses.replace(_cfg(), dimensions=2))


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (httpx.TimeoutException("slow"), "timed out after 10 seconds"),
        (httpx.ConnectError("down"), "connection failed"),
    ],
)
def test_probe_does_not_retry_transport_errors(monkeypatch, error, reason):
    calls = {"n": 0}

    def fail(*args, **kwargs):
        calls["n"] += 1
        raise error

    monkeypatch.setattr(embed_mod.httpx, "post", fail)
    with pytest.raises(EmbedError, match=reason):
        probe_embedding_endpoint(dataclasses.replace(_cfg(), dimensions=2))
    assert calls["n"] == 1


def test_probe_hides_http_response_body(monkeypatch):
    request = httpx.Request("POST", "http://x/embeddings")
    response = httpx.Response(
        401, request=request, text="test-secret-response-body"
    )
    monkeypatch.setattr(embed_mod.httpx, "post", lambda *a, **k: response)
    with pytest.raises(EmbedError) as exc:
        probe_embedding_endpoint(dataclasses.replace(_cfg(), dimensions=2))
    assert "HTTP 401 Unauthorized" in str(exc.value)
    assert "test-secret-response-body" not in str(exc.value)
```

For malformed JSON, add:

```python
def test_probe_rejects_malformed_json(monkeypatch):
    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            raise ValueError("bad")

    monkeypatch.setattr(embed_mod.httpx, "post", lambda *a, **k: Resp())
    with pytest.raises(EmbedError, match="malformed JSON"):
        probe_embedding_endpoint(dataclasses.replace(_cfg(), dimensions=2))
```

- [x] **Step 3: Verify RED**

```bash
uv run pytest -q tests/engine/test_embed.py
```

Expected: probe tests fail because the function is absent; existing runtime retry tests pass.

- [x] **Step 4: Implement the probe**

Add `math`, `numbers`, `_PROBE_TIMEOUT = 10.0`, `_PROBE_INPUT = "iwiki startup probe"`, then:

```python
def probe_embedding_endpoint(cfg: Config) -> None:
    url = f"{cfg.base_url}/embeddings"
    payload = {
        "model": cfg.embed_model,
        "input": [_PROBE_INPUT],
        "dimensions": cfg.dimensions,
    }
    try:
        response = httpx.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            timeout=_PROBE_TIMEOUT,
        )
        response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise EmbedError("embedding probe timed out after 10 seconds") from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        reason = exc.response.reason_phrase
        raise EmbedError(f"embedding probe returned HTTP {status} {reason}") from exc
    except httpx.TransportError as exc:
        raise EmbedError(f"embedding probe connection failed: {exc}") from exc
    try:
        body = response.json()
    except (TypeError, ValueError) as exc:
        raise EmbedError("embedding probe returned malformed JSON") from exc
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list) or len(data) != 1:
        raise EmbedError("embedding probe expected exactly one embedding result")
    row = data[0]
    if not isinstance(row, dict) or "embedding" not in row:
        raise EmbedError("embedding probe response is missing embedding vector")
    vector = row["embedding"]
    if not isinstance(vector, list) or not vector:
        raise EmbedError("embedding probe requires a non-empty numeric vector")
    if any(isinstance(v, bool) or not isinstance(v, numbers.Real)
           or not math.isfinite(v) for v in vector):
        raise EmbedError("embedding probe vector must contain finite numbers")
    if len(vector) != cfg.dimensions:
        raise EmbedError(
            f"embedding probe dimension mismatch: expected {cfg.dimensions}, "
            f"got {len(vector)}"
        )
```

Do not call retry helpers or change `embed_texts()`.

- [x] **Step 5: Verify GREEN and commit**

```bash
uv run pytest -q tests/engine/test_embed.py
git add src/iwiki_mcp/engine/embed.py tests/engine/test_embed.py
git commit -m "feat: probe embeddings endpoint before startup"
```

Expected: new probe cases and existing three-attempt runtime cases pass.

### Task 3: Gate FastMCP Startup

**Files:**
- Create: `tests/test_server_startup.py`
- Modify: `src/iwiki_mcp/server.py`

- [x] **Step 1: Write startup-order and expected-failure tests**

Patch `sys.argv`, `Config.load`, `probe_embedding_endpoint`, and `mcp.run`. Assert success calls `load`, `probe`, `run` in order. For `ConfigError` and `EmbedError`, assert `SystemExit(1)`, no `mcp.run`, empty stdout, and stderr containing header, full normalized `/embeddings` URL, model, reason, and all four env names but not fake key `test-secret`.

```python
import pytest

from iwiki_mcp import server
from iwiki_mcp.engine.config import Config, ConfigError
from iwiki_mcp.engine.embed import EmbedError


def _cfg():
    return Config(
        base_url="https://example.test/v1",
        api_key="test-secret",
        embed_model="test-model",
        dimensions=2,
        chunk_size=512,
        chunk_overlap=64,
        summary_max=400,
        top_k=8,
        score_threshold=0.2,
        graph_depth=2,
        ignore=None,
    )


def test_main_probes_before_running_mcp(monkeypatch):
    cfg = _cfg()
    calls = []
    monkeypatch.setattr(server.sys, "argv", ["iwiki-mcp"])
    monkeypatch.setattr(server.Config, "load", lambda: calls.append("load") or cfg)
    monkeypatch.setattr(server, "probe_embedding_endpoint",
                        lambda actual: calls.append(("probe", actual)))
    monkeypatch.setattr(server.mcp, "run", lambda: calls.append("run"))
    server.main()
    assert calls == ["load", ("probe", cfg), "run"]


def test_main_blocks_mcp_and_reports_probe_failure(monkeypatch, capsys):
    cfg = _cfg()
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "https://example.test/v1/")
    monkeypatch.setenv("IWIKI_LLM_KEY", "test-secret")
    monkeypatch.setenv("IWIKI_EMBED_MODEL", "test-model")
    monkeypatch.setattr(server.sys, "argv", ["iwiki-mcp"])
    monkeypatch.setattr(server.Config, "load", lambda: cfg)
    monkeypatch.setattr(
        server,
        "probe_embedding_endpoint",
        lambda actual: (_ for _ in ()).throw(
            EmbedError("embedding probe timed out after 10 seconds")
        ),
    )
    run_calls = []
    monkeypatch.setattr(server.mcp, "run", lambda: run_calls.append(True))
    with pytest.raises(SystemExit) as exc:
        server.main()
    captured = capsys.readouterr()
    assert exc.value.code == 1
    assert run_calls == []
    assert captured.out == ""
    assert "iwiki-mcp: startup failed" in captured.err
    assert "https://example.test/v1/embeddings" in captured.err
    assert "Model: test-model" in captured.err
    assert "timed out after 10 seconds" in captured.err
    assert "IWIKI_EMBED_DIMENSIONS" in captured.err
    assert "test-secret" not in captured.err


def test_main_reports_missing_config_without_running_mcp(monkeypatch, capsys):
    monkeypatch.delenv("IWIKI_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("IWIKI_LLM_KEY", raising=False)
    monkeypatch.setattr(server.sys, "argv", ["iwiki-mcp"])
    monkeypatch.setattr(server.mcp, "run", lambda: pytest.fail("mcp.run called"))
    with pytest.raises(SystemExit) as exc:
        server.main()
    captured = capsys.readouterr()
    assert exc.value.code == 1
    assert captured.out == ""
    assert "Embeddings endpoint: <not set>" in captured.err
    assert "IWIKI_LLM_BASE_URL" in captured.err
```

- [x] **Step 2: Write help and unexpected-error tests**

```python
def test_help_exits_without_loading_config(monkeypatch, capsys):
    monkeypatch.setattr(server.sys, "argv", ["iwiki-mcp", "--help"])
    monkeypatch.setattr(server.Config, "load",
                        lambda: pytest.fail("Config.load ran for --help"))
    with pytest.raises(SystemExit) as exc:
        server.main()
    assert exc.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_unexpected_probe_error_propagates(monkeypatch):
    monkeypatch.setattr(server.sys, "argv", ["iwiki-mcp"])
    monkeypatch.setattr(server.Config, "load", _cfg)
    monkeypatch.setattr(server, "probe_embedding_endpoint",
                        lambda cfg: (_ for _ in ()).throw(RuntimeError("bug")))
    with pytest.raises(RuntimeError, match="bug"):
        server.main()
```

- [x] **Step 3: Verify RED**

```bash
uv run pytest -q tests/test_server_startup.py
```

Expected: tests fail because startup does not probe or format failures.

- [x] **Step 4: Implement orchestration and diagnostic**

Import `sys` and `probe_embedding_endpoint`. Add helpers that read only allowed env fields:

```python
def _print_startup_failure(reason: str) -> None:
    base_url = os.environ.get("IWIKI_LLM_BASE_URL", "").strip().rstrip("/")
    endpoint = f"{base_url}/embeddings" if base_url else "<not set>"
    model = os.environ.get(
        "IWIKI_EMBED_MODEL", "text-embedding-3-small"
    ).strip() or "<not set>"
    print("iwiki-mcp: startup failed", file=sys.stderr)
    print(f"Embeddings endpoint: {endpoint}", file=sys.stderr)
    print(f"Model: {model}", file=sys.stderr)
    print(f"Reason: {reason}", file=sys.stderr)
    print(
        "Hint: verify IWIKI_LLM_BASE_URL, IWIKI_LLM_KEY, "
        "IWIKI_EMBED_MODEL, and IWIKI_EMBED_DIMENSIONS",
        file=sys.stderr,
    )
```

After `--project` handling:

```python
    try:
        cfg = Config.load()
        probe_embedding_endpoint(cfg)
    except (ConfigError, EmbedError) as exc:
        _print_startup_failure(str(exc))
        raise SystemExit(1) from None
    mcp.run()
```

- [x] **Step 5: Verify unit GREEN and expected smoke RED, then commit**

```bash
uv run pytest -q tests/test_server_startup.py
uv run pytest -q tests/test_mcp_smoke.py
git add src/iwiki_mcp/server.py tests/test_server_startup.py
git commit -m "feat: block MCP startup when embeddings fail"
```

Expected: startup unit tests pass; old smoke test fails because Task 4 has not supplied an endpoint.

### Task 4: Make MCP Smoke Deterministic

**Files:**
- Modify: `tests/test_mcp_smoke.py`

- [x] **Step 1: Add a loopback stub**

Use `ThreadingHTTPServer`, a daemon thread, and a context manager. Handler `do_POST` records `(path, decoded_json)` and returns `{"data":[{"index":0,"embedding":[0.1,0.2]}]}`; override `log_message` to stay silent. The context manager yields `http://127.0.0.1:<port>/v1` and state, then calls `shutdown`, `join(timeout=5)`, and `server_close`.

```python
@contextmanager
def embedding_stub():
    state = {"requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(size))
            state["requests"].append((self.path, payload))
            body = json.dumps({
                "data": [{"index": 0, "embedding": [0.1, 0.2]}]
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return None

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = httpd.server_address
        yield f"http://{host}:{port}/v1", state
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
```

- [x] **Step 2: Route subprocess env and assert one request**

Set explicit values before `StdioServerParameters`:

```python
env["IWIKI_LLM_BASE_URL"] = url
env["IWIKI_LLM_KEY"] = "smoke-test-key"
env["IWIKI_EMBED_MODEL"] = "smoke-test-model"
env["IWIKI_EMBED_DIMENSIONS"] = "2"
```

After the MCP session closes:

```python
assert state["requests"] == [(
    "/v1/embeddings",
    {"model": "smoke-test-model",
     "input": ["iwiki startup probe"], "dimensions": 2},
)]
```

- [x] **Step 3: Verify GREEN and commit**

```bash
uv run pytest -q tests/test_mcp_smoke.py
git add tests/test_mcp_smoke.py
git commit -m "test: cover embedding-gated MCP startup"
```

Expected: MCP initializes, lists tools, calls `wiki_status`, and exactly one probe is recorded without external network access.

### Task 5: Update Docs, Version, and iwiki

**Files:**
- Modify: `README.md`
- Modify: `docs/README.ru.md`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`
- Update iwiki pages: `mcp-server`, `indexing`, `installation`

- [x] **Step 1: Document the startup contract**

Near existing session-start text in both READMEs, state: one minimal request before MCP stdio; 10-second timeout; no retries; config/endpoint/response failure blocks startup with stderr diagnostic; `--help` stays offline. Keep API-key storage guidance unchanged.

English text:

```markdown
Before starting the MCP stdio protocol, `iwiki-mcp` sends one minimal request to
the configured embeddings endpoint. The probe has a 10-second timeout and no
retries. Missing configuration, an unavailable endpoint, or an invalid embedding
response prevents startup and produces an actionable diagnostic on stderr;
`--help` remains available without endpoint access.
```

Russian text:

```markdown
Перед запуском MCP-протокола по stdio `iwiki-mcp` отправляет один минимальный
запрос к настроенному endpoint эмбеддингов. Probe имеет timeout 10 секунд и не
повторяется. Отсутствующая конфигурация, недоступный endpoint или некорректный
embedding-ответ блокируют запуск и выводят понятную диагностику в stderr;
`--help` остаётся доступным без endpoint.
```

- [x] **Step 2: Bump and lock version**

Change both project and module versions from `0.6.9` to `0.6.10`, then:

```bash
uv lock
```

Expected: root package entries in `pyproject.toml` and `uv.lock` are `0.6.10`; dependencies do not otherwise change.

- [x] **Step 3: Verify repository docs and version**

```bash
uv run pytest -q tests/test_package.py tests/test_server_startup.py tests/test_mcp_smoke.py
uv run iwiki-mcp --help
git diff --check
```

Expected: tests pass, help exits `0` without endpoint access, and diff is clean.

- [x] **Step 4: Update iwiki through MCP tools**

Call `wiki_update_page` for:

- `mcp-server` heading `FastMCP wiring`: ordered `Config.load()` → `probe_embedding_endpoint()` → `mcp.run()`, stderr-only exit; source `src/iwiki_mcp/server.py`.
- `indexing` heading `Embeddings client`: 10-second no-retry startup probe versus three-attempt runtime client; source `src/iwiki_mcp/engine/embed.py`.
- `installation` heading `Required environment`: startup refusal, diagnostic fields, and recovery; source `README.md`.

Run `wiki_lint(domain="iwiki-mcp")`. Expected: no new broken/stale/missing-source findings; record the pre-existing `architecture.md` orphan, long-lead, and tag-drift advisories without editing them.

- [x] **Step 5: Commit repository docs/version**

```bash
git add README.md docs/README.ru.md pyproject.toml src/iwiki_mcp/__init__.py uv.lock
git commit -m "docs: explain embedding startup validation"
```

iwiki MCP writes auto-commit the external wiki base and are not staged here.

### Task 6: Full Verification and Result Gate

**Files:**
- Verify all changed files
- Update through chain tools: this plan, `docs/TODO.md`, final result report

- [x] **Step 1: Run focused and full tests**

```bash
uv run pytest -q tests/engine/test_config.py tests/engine/test_embed.py tests/test_server_startup.py tests/test_mcp_smoke.py
uv run pytest -q
```

Expected: zero failures; focused evidence covers config rejection, one-attempt probe, blocked failure startup, and successful MCP initialization.

- [x] **Step 2: Run lint, CLI, version, and diff checks**

```bash
uv run flake8 src tests
uv run iwiki-mcp --help
uv run python -c "from importlib.metadata import version; import iwiki_mcp; assert version('iwiki-mcp') == iwiki_mcp.__version__ == '0.6.10'"
git diff --check origin/master...HEAD
git status --short
```

Expected: all commands exit `0`; status contains only intended chain bookkeeping if uncommitted.

- [x] **Step 3: Verify observable intent outcomes**

Use passing `test_mcp_smoke.py` as evidence that a usable endpoint permits normal startup. Use passing server failure tests as evidence that invalid/unavailable endpoints block `mcp.run()`, exit `1`, keep stdout empty, and emit safe stderr. Re-run the existing three runtime retry tests as evidence that normal embedding behavior did not degrade.

- [x] **Step 4: Run result reconciliation**

Run `$check-chain result docs/superpowers/plans/2026-07-13-embedding-startup-check.md --since=origin/master`.

Expected: Tasks 1–6 are `DONE`; R1–R8 and both Desired Outcomes have evidence; no bug or stale documentation remains; final HTML report is generated; TODO row closes with `Result: OK`.

- [x] **Step 5: Commit final chain bookkeeping**

```bash
git add docs/TODO.md docs/superpowers/plans/2026-07-13-embedding-startup-check.md docs/superpowers/reports/embedding-startup-check-results.html
git commit -m "docs: close embedding startup check chain"
```

Expected: commit contains only plan state, task log, and final result report.
