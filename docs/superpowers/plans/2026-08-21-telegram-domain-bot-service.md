---
review:
  plan_hash: 44fccd9a95080435
  last_run: 2026-08-21
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-08-21-telegram-domain-bot-service-intent.md
  spec: docs/superpowers/specs/2026-08-21-telegram-domain-bot-service-design.md
result_check:
  verdict: needs_work
  source: plan
  plan_hash: 44fccd9a95080435
  last_run: 2026-08-22
  reviewed: true
  docs_checked: true
---

# Telegram Domain Bot Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a separately deployed Telegram long-polling service that safely reads and writes the remote iwiki MCP service and uses OpenAI-compatible inference for text and voice requests.

**Architecture:** Add an `iwiki_mcp.telegram_bot` package with bounded configuration, access-policy, remote-MCP, inference, conversation, transport, and runner modules. A service-token scope and administrator-owned Telegram-ID allowlist gate every operation; short-lived conversation state enables selected-domain reads and preview-confirmed optimistic-concurrency writes.

**Tech Stack:** Python 3.10+, existing `anyio`, `httpx`, and `mcp` Streamable HTTP client; Telegram Bot API long polling; OpenAI-compatible `/chat/completions` and `/audio/transcriptions` endpoints; pytest and pytest-asyncio.

---

## File Structure

- Create: `src/iwiki_mcp/telegram_bot/models.py` — immutable request, page-target, preview, and result values.
- Create: `src/iwiki_mcp/telegram_bot/config.py` — environment-only service configuration and fail-closed validation.
- Create: `src/iwiki_mcp/telegram_bot/access.py` — Telegram-ID allowlist policy.
- Create: `src/iwiki_mcp/telegram_bot/iwiki.py` — typed remote MCP tool wrapper over Streamable HTTP.
- Create: `src/iwiki_mcp/telegram_bot/inference.py` — OpenAI-compatible text and transcription client.
- Create: `src/iwiki_mcp/telegram_bot/conversation.py` — transient selected-domain and confirmed-write workflows.
- Create: `src/iwiki_mcp/telegram_bot/transport.py` — Telegram long-polling update decoding and response rendering.
- Create: `src/iwiki_mcp/telegram_bot/main.py` — composition root and console-script entry point.
- Create: `tests/telegram_bot/` — focused unit and service-flow tests using fake remote, inference, and Telegram clients.
- Create: `docs/telegram-bot.md` — deployment configuration, trust model, and operator procedure.
- Modify: `pyproject.toml` — console script and patch version `0.7.171`.
- Modify: `src/iwiki_mcp/__init__.py`, `tests/test_package.py` — synchronized patch version `0.7.171`.
- Modify: `README.md`, `docs/README.ru.md`, `docs/architecture.md` — link and document the separate bot service.

## Task 1: Configuration, Values, and Access Policy

**Files:**
- Create: `src/iwiki_mcp/telegram_bot/__init__.py`
- Create: `src/iwiki_mcp/telegram_bot/models.py`
- Create: `src/iwiki_mcp/telegram_bot/config.py`
- Create: `src/iwiki_mcp/telegram_bot/access.py`
- Test: `tests/telegram_bot/test_config_access.py`

- [ ] **Step 1: Write failing configuration and allowlist tests.**

```python
import pytest

from iwiki_mcp.telegram_bot.access import AccessPolicy
from iwiki_mcp.telegram_bot.config import BotConfig, BotConfigError


def test_config_rejects_missing_inference_key(monkeypatch):
    monkeypatch.setenv("IWIKI_BOT_TELEGRAM_TOKEN", "telegram-token")
    monkeypatch.setenv("IWIKI_BOT_IWIKI_URL", "https://wiki.example/mcp")
    monkeypatch.setenv("IWIKI_BOT_IWIKI_TOKEN", "iwiki-token")
    monkeypatch.setenv("IWIKI_BOT_ALLOWED_TELEGRAM_IDS", "1001")
    monkeypatch.setenv("IWIKI_BOT_LLM_BASE_URL", "https://models.example/v1")
    monkeypatch.delenv("IWIKI_BOT_LLM_KEY", raising=False)

    with pytest.raises(BotConfigError, match="IWIKI_BOT_LLM_KEY"):
        BotConfig.load()


def test_allowlist_denies_unknown_id():
    policy = AccessPolicy(frozenset({1001}))

    assert policy.allows(1001) is True
    assert policy.allows(2002) is False
```

- [ ] **Step 2: Run the focused tests and confirm they fail because the package is absent.**

```bash
uv run pytest -q tests/telegram_bot/test_config_access.py
```

Expected: collection fails with `ModuleNotFoundError: No module named 'iwiki_mcp.telegram_bot'`.

- [ ] **Step 3: Implement immutable configuration and policy.**

```python
@dataclass(frozen=True)
class BotConfig:
    telegram_token: str
    iwiki_url: str
    iwiki_token: str
    allowed_telegram_ids: frozenset[int]
    llm_base_url: str
    llm_key: str
    llm_model: str
    transcription_model: str
    confirmation_ttl_seconds: int

    @classmethod
    def load(cls) -> "BotConfig":
        required = {name: os.environ.get(name, "").strip() for name in (
            "IWIKI_BOT_TELEGRAM_TOKEN", "IWIKI_BOT_IWIKI_URL", "IWIKI_BOT_IWIKI_TOKEN",
            "IWIKI_BOT_ALLOWED_TELEGRAM_IDS", "IWIKI_BOT_LLM_BASE_URL", "IWIKI_BOT_LLM_KEY",
            "IWIKI_BOT_LLM_MODEL", "IWIKI_BOT_TRANSCRIPTION_MODEL",
        )}
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise BotConfigError(f"missing configuration: {', '.join(missing)}")
        try:
            allowed = frozenset(int(value) for value in required["IWIKI_BOT_ALLOWED_TELEGRAM_IDS"].split(","))
            ttl = int(os.environ.get("IWIKI_BOT_CONFIRMATION_TTL_SECONDS", "300"))
        except ValueError as exc:
            raise BotConfigError("invalid Telegram IDs or confirmation TTL") from exc
        if not allowed or ttl <= 0:
            raise BotConfigError("Telegram IDs and confirmation TTL must be positive")
        return cls(required["IWIKI_BOT_TELEGRAM_TOKEN"], required["IWIKI_BOT_IWIKI_URL"], required["IWIKI_BOT_IWIKI_TOKEN"], allowed, required["IWIKI_BOT_LLM_BASE_URL"].rstrip("/"), required["IWIKI_BOT_LLM_KEY"], required["IWIKI_BOT_LLM_MODEL"], required["IWIKI_BOT_TRANSCRIPTION_MODEL"], ttl)


@dataclass(frozen=True)
class AccessPolicy:
    allowed_telegram_ids: frozenset[int]

    def allows(self, telegram_id: int) -> bool:
        return telegram_id in self.allowed_telegram_ids
```

Define `BotConfigError` as a `RuntimeError`; use message text naming only the missing or invalid configuration key. Keep models in `models.py` provider-free: `PageTarget(domain, slug, heading, revision, section_hash)`, `PendingWrite(token, telegram_id, action, payload, expires_at)`, `WritePreview(token, text, buttons)`, and `BotReply(text, buttons=())`.

- [ ] **Step 4: Run focused tests and static import check.**

```bash
uv run pytest -q tests/telegram_bot/test_config_access.py
uv run python -c "from iwiki_mcp.telegram_bot.config import BotConfig; print(BotConfig.__name__)"
```

Expected: both commands pass.

- [ ] **Step 5: Commit the configuration boundary.**

```bash
git add src/iwiki_mcp/telegram_bot tests/telegram_bot/test_config_access.py
git commit -m "feat(bot): add configuration and access policy"
```

## Task 2: Remote iwiki MCP Client

**Files:**
- Create: `src/iwiki_mcp/telegram_bot/iwiki.py`
- Test: `tests/telegram_bot/test_iwiki_client.py`

- [ ] **Step 1: Write failing tests against a fake tool caller.**

```python
import pytest

from iwiki_mcp.telegram_bot.iwiki import RemoteIwikiClient, RemoteIwikiError


@pytest.mark.asyncio
async def test_search_forces_selected_domain_only():
    calls = []

    async def call_tool(name, arguments):
        calls.append((name, arguments))
        return {"results": [{"slug": "guide/a", "heading": "Answer"}]}

    client = RemoteIwikiClient(call_tool)
    await client.search("team", "how to deploy")

    assert calls == [("wiki_search", {"domains": ["team"], "query": "how to deploy", "k": 5})]


@pytest.mark.asyncio
async def test_update_requires_fresh_revision_and_section_hash():
    async def call_tool(name, arguments):
        return {"error": "section_conflict"}

    client = RemoteIwikiClient(call_tool)

    with pytest.raises(RemoteIwikiError, match="section_conflict"):
        await client.update_section("team", "guide/a", "Steps", "new body", 7, "abc")
```

- [ ] **Step 2: Run the client tests and confirm the import fails.**

```bash
uv run pytest -q tests/telegram_bot/test_iwiki_client.py
```

Expected: collection fails because `iwiki.py` does not exist.

- [ ] **Step 3: Implement the typed remote wrapper and Streamable HTTP adapter.**

```python
class RemoteIwikiClient:
    def __init__(self, call_tool: Callable[[str, dict[str, object]], Awaitable[dict[str, object]]]):
        self._call_tool = call_tool

    async def search(self, domain: str, query: str) -> list[dict[str, object]]:
        result = await self._call_tool("wiki_search", {"domains": [domain], "query": query, "k": 5})
        return self._require_results(result)

    async def update_section(self, domain, slug, heading, new_body, revision, section_hash):
        result = await self._call_tool("wiki_update_page", {
            "domain": domain, "slug": slug, "heading": heading, "new_body": new_body,
            "expected_revision": revision, "expected_section_hash": section_hash,
        })
        self._raise_on_error(result)
        return result
```

Add an async connection factory using `mcp.client.streamable_http.streamablehttp_client`, `ClientSession`, `initialize()`, and an `Authorization: Bearer <service token>` header. Expose only `list_domains`, `search`, `read_page(domain, slug, heading=None)`, `write_page`, and `update_section`; normalize all remote errors into `RemoteIwikiError` without including tokens or raw remote payloads.

- [ ] **Step 4: Run focused client tests.**

```bash
uv run pytest -q tests/telegram_bot/test_iwiki_client.py
```

Expected: PASS; asserts that selected-domain scope and compare-and-swap values are forwarded exactly.

- [ ] **Step 5: Commit the remote boundary.**

```bash
git add src/iwiki_mcp/telegram_bot/iwiki.py tests/telegram_bot/test_iwiki_client.py
git commit -m "feat(bot): add scoped remote iwiki client"
```

## Task 3: OpenAI-Compatible Inference Client

**Files:**
- Create: `src/iwiki_mcp/telegram_bot/inference.py`
- Test: `tests/telegram_bot/test_inference.py`

- [ ] **Step 1: Write failing HTTP-contract tests with `httpx.MockTransport`.**

```python
import json
import httpx
import pytest

from iwiki_mcp.telegram_bot.inference import InferenceClient


@pytest.mark.asyncio
async def test_answer_posts_only_question_and_selected_context():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Answer"}}]})

    client = InferenceClient("https://models.example/v1", "key", "chat-model", "audio-model", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await client.answer("Question", "Selected context") == "Answer"
    assert seen["url"] == "https://models.example/v1/chat/completions"
    assert "Selected context" in str(seen["json"])
```

- [ ] **Step 2: Run the inference tests and confirm the import fails.**

```bash
uv run pytest -q tests/telegram_bot/test_inference.py
```

Expected: collection fails because `inference.py` does not exist.

- [ ] **Step 3: Implement answer and transcription calls.**

```python
class InferenceClient:
    async def answer(self, question: str, context: str) -> str:
        response = await self._http.post(
            f"{self._base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={"model": self._chat_model, "messages": [
                {"role": "system", "content": "Answer only from supplied wiki context."},
                {"role": "user", "content": f"Question:\n{question}\n\nWiki context:\n{context}"},
            ], "temperature": 0},
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    async def transcribe(self, filename: str, audio: bytes) -> str:
        response = await self._http.post(
            f"{self._base_url}/audio/transcriptions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            data={"model": self._transcription_model},
            files={"file": (filename, audio, "audio/ogg")},
        )
        response.raise_for_status()
        return response.json()["text"]

    async def draft_markdown(self, request: str, context: str) -> str:
        return await self._complete("Produce Markdown only for the requested wiki change.", request, context)
```

Factor the shared `/chat/completions` request into `_complete(system_instruction, request, context)` so `answer` and `draft_markdown` parse the same response shape. Raise `InferenceError` for malformed, empty, transport, timeout, or non-2xx responses. Do not log prompts, answers, audio bytes, or API keys. Close an internally owned `httpx.AsyncClient` during service shutdown.

- [ ] **Step 4: Run focused inference tests.**

```bash
uv run pytest -q tests/telegram_bot/test_inference.py
```

Expected: PASS; tests cover text response parsing, transcription parsing, and sanitized failures.

- [ ] **Step 5: Commit the inference boundary.**

```bash
git add src/iwiki_mcp/telegram_bot/inference.py tests/telegram_bot/test_inference.py
git commit -m "feat(bot): add openai compatible inference client"
```

## Task 4: Read, Voice, and Transient Conversation Flows

**Files:**
- Create: `src/iwiki_mcp/telegram_bot/conversation.py`
- Test: `tests/telegram_bot/test_conversation_read.py`

- [ ] **Step 1: Write failing flow tests with fakes.**

```python
@pytest.mark.asyncio
async def test_question_uses_only_selected_domain_context(service):
    await service.select_domain(1001, "team")

    reply = await service.answer_question(1001, "How do I deploy?")

    assert reply.text == "Answer"
    assert service.remote.search_calls == [("team", "How do I deploy?")]
    assert service.inference.contexts == ["team deployment section"]


@pytest.mark.asyncio
async def test_voice_bytes_are_removed_after_transcription(service, tmp_path):
    reply = await service.answer_voice(1001, "voice.ogg", b"audio")

    assert reply.text == "Answer"
    assert list(tmp_path.iterdir()) == []
```

- [ ] **Step 2: Run the read-flow tests and confirm they fail.**

```bash
uv run pytest -q tests/telegram_bot/test_conversation_read.py
```

Expected: FAIL because `ConversationService` is not implemented.

- [ ] **Step 3: Implement read and voice orchestration.**

```python
class ConversationService:
    async def select_domain(self, telegram_id: int, domain: str) -> BotReply:
        self._require_access(telegram_id)
        if domain not in await self._remote.list_domains():
            raise ConversationError("domain is not available")
        self._selected_domains[telegram_id] = domain
        return BotReply(f"Selected domain: {domain}")

    async def answer_question(self, telegram_id: int, question: str) -> BotReply:
        domain = self._selected_domain(telegram_id)
        results = await self._remote.search(domain, question)
        pages = [await self._remote.read_page(domain, result["slug"]) for result in results]
        context = "\n\n".join(str(page["markdown"]) for page in pages)
        return BotReply(await self._inference.answer(question, context))
```

Use a temporary file only inside an async context manager for voice input; delete it in `finally`. Store selected domains in memory keyed by allowed Telegram ID. On unknown sender, absent selection, empty retrieval, transcription failure, or inference failure return a sanitized `BotReply` and clear transient state where applicable.

- [ ] **Step 4: Run focused flow tests.**

```bash
uv run pytest -q tests/telegram_bot/test_conversation_read.py
```

Expected: PASS; proves no cross-domain retrieval and no retained voice file.

- [ ] **Step 5: Commit read and voice flows.**

```bash
git add src/iwiki_mcp/telegram_bot/conversation.py tests/telegram_bot/test_conversation_read.py
git commit -m "feat(bot): add read and voice conversations"
```

## Task 5: Preview-Confirmed Page Writes

**Files:**
- Modify: `src/iwiki_mcp/telegram_bot/conversation.py`
- Test: `tests/telegram_bot/test_conversation_write.py`

- [ ] **Step 1: Write failing confirmation and conflict tests.**

```python
@pytest.mark.asyncio
async def test_write_requires_confirmation(service):
    preview = await service.propose_create(1001, "Runbook", "Add a deploy runbook")

    assert preview.buttons == ("confirm", "reject")
    assert service.remote.write_calls == []


@pytest.mark.asyncio
async def test_confirmed_update_reports_conflict_without_retry(service):
    token = (await service.propose_update(1001, "guide/deploy", "Steps", "replace step two")).token
    service.remote.update_error = "section_conflict"

    reply = await service.confirm_write(1001, token)

    assert "changed" in reply.text
    assert service.remote.update_calls == 1
```

- [ ] **Step 2: Run the write-flow tests and confirm they fail.**

```bash
uv run pytest -q tests/telegram_bot/test_conversation_write.py
```

Expected: FAIL because proposal and confirmation methods are absent.

- [ ] **Step 3: Implement pending-write state and mutation rules.**

```python
async def confirm_write(self, telegram_id: int, token: str) -> BotReply:
    pending = self._consume_pending(telegram_id, token)
    if pending.action == "create":
        await self._remote.write_page(**pending.payload)
    else:
        target = await self._remote.read_page(
            pending.payload["target"].domain,
            pending.payload["target"].slug,
            pending.payload["target"].heading,
        )
        await self._remote.update_section(
            pending.payload["target"].domain, pending.payload["target"].slug,
            pending.payload["target"].heading, pending.payload["new_body"],
            int(target["revision"]), str(target["section_hash"]),
        )
    return BotReply("Page change saved.")
```

Generate a cryptographically random pending token, store only the pending action in memory, bind it to the Telegram ID, and expire it after configured TTL. `propose_create` and `propose_update` obtain Markdown through `InferenceClient.draft_markdown`, then render a `WritePreview`; neither calls a remote mutation. Reject or expiry consumes and destroys the pending action. `propose_update` requires a single chosen page and `##` heading; it never infers an ambiguous target. `confirm_write` performs at most one mutation and never retries a conflict.

- [ ] **Step 4: Run focused write tests.**

```bash
uv run pytest -q tests/telegram_bot/test_conversation_write.py
```

Expected: PASS; proves preview-before-write, reject/expiry cleanup, fresh CAS values, and no conflict retry.

- [ ] **Step 5: Commit confirmed write flow.**

```bash
git add src/iwiki_mcp/telegram_bot/conversation.py tests/telegram_bot/test_conversation_write.py
git commit -m "feat(bot): add confirmed wiki writes"
```

## Task 6: Telegram Long Polling and Service Runner

**Files:**
- Create: `src/iwiki_mcp/telegram_bot/transport.py`
- Create: `src/iwiki_mcp/telegram_bot/main.py`
- Modify: `pyproject.toml`
- Test: `tests/telegram_bot/test_transport.py`

- [ ] **Step 1: Write failing transport tests with a fake Telegram HTTP client.**

```python
@pytest.mark.asyncio
async def test_unknown_sender_never_reaches_conversation_service(transport):
    await transport.handle_update({"message": {"from": {"id": 2002}, "chat": {"id": 9}, "text": "/domains"}})

    assert transport.conversation.calls == []
    assert transport.sent_texts == [(9, "Access denied.")]


@pytest.mark.asyncio
async def test_confirm_callback_calls_confirm_write(transport):
    await transport.handle_update({"callback_query": {"from": {"id": 1001}, "message": {"chat": {"id": 9}}, "data": "confirm:nonce"}})

    assert transport.conversation.confirmations == [(1001, "nonce")]
```

- [ ] **Step 2: Run the transport tests and confirm they fail.**

```bash
uv run pytest -q tests/telegram_bot/test_transport.py
```

Expected: collection fails because the transport module is absent.

- [ ] **Step 3: Implement minimal Telegram API transport and runner.**

```python
async def poll_forever(self) -> None:
    offset = None
    while True:
        updates = await self._api("getUpdates", {"timeout": 30, "offset": offset})
        for update in updates:
            await self.handle_update(update)
            offset = int(update["update_id"]) + 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram client for remote iwiki")
    parser.parse_args()
    config = BotConfig.load()
    anyio.run(build_service(config).poll_forever)
```

Use the Telegram Bot API through `httpx.AsyncClient`; accept only `/domains`, domain-selection callbacks, text questions after a domain is selected, voice messages, create/update proposal commands, and confirm/reject callbacks. Add `iwiki-telegram-bot = "iwiki_mcp.telegram_bot.main:main"` to `[project.scripts]`. The transport does not grant access or persist updates.

- [ ] **Step 4: Run transport tests and console-script help.**

```bash
uv run pytest -q tests/telegram_bot/test_transport.py
uv run iwiki-telegram-bot --help
```

Expected: PASS; help prints usage without contacting Telegram, iwiki, or inference.

- [ ] **Step 5: Commit transport and runner.**

```bash
git add src/iwiki_mcp/telegram_bot pyproject.toml tests/telegram_bot/test_transport.py
git commit -m "feat(bot): add telegram long polling runner"
```

## Task 7: Deployment Documentation, Integration Evidence, and Version Sync

**Files:**
- Create: `docs/telegram-bot.md`
- Modify: `README.md`
- Modify: `docs/README.ru.md`
- Modify: `docs/architecture.md`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `tests/test_package.py`
- Test: `tests/telegram_bot/test_end_to_end.py`

- [ ] **Step 1: Write a fake end-to-end scenario.**

```python
@pytest.mark.asyncio
async def test_authorized_text_voice_and_confirmed_write_path(bot):
    await bot.handle_text(1001, "/domains")
    await bot.handle_callback(1001, "domain:team")
    await bot.handle_text(1001, "How do I deploy?")
    await bot.handle_voice(1001, "voice.ogg", b"audio")
    await bot.handle_text(1001, "/create Runbook: deployment steps")
    await bot.handle_callback(1001, "confirm:nonce")

    assert bot.remote.writes == 1
    assert bot.inference.answers == 2


@pytest.mark.asyncio
async def test_unauthorized_sender_has_no_outbound_calls(bot):
    await bot.handle_text(2002, "/domains")

    assert bot.remote.calls == []
    assert bot.inference.calls == []
```

- [ ] **Step 2: Run the end-to-end test and confirm it fails before documentation changes.**

```bash
uv run pytest -q tests/telegram_bot/test_end_to_end.py
```

Expected: FAIL until the composed service paths are complete.

- [ ] **Step 3: Add operator documentation and synchronize release version.**

```toml
[project.scripts]
iwiki-mcp = "iwiki_mcp.server:main"
iwiki-telegram-bot = "iwiki_mcp.telegram_bot.main:main"
```

Document every required `IWIKI_BOT_*` configuration key, long-polling deployment command, service-token least privilege, admin-owned allowlist, OpenAI-compatible endpoints, no-content-retention behavior, confirmation flow, and failure behavior. Link the guide from English and Russian README files and add the bot boundary to `docs/architecture.md`. Bump `pyproject.toml`, `src/iwiki_mcp/__init__.py`, and `tests/test_package.py` together from `0.7.170` to `0.7.171`.

- [ ] **Step 4: Run end-to-end, package, full-suite, and documentation checks.**

```bash
uv run pytest -q tests/telegram_bot/test_end_to_end.py
uv run pytest -q tests/test_package.py
uv run iwiki-telegram-bot --help
uv run pytest -q
git diff --check
```

Expected: all pytest commands pass; console help makes no network call; diff check is empty.

- [ ] **Step 5: Commit documentation and release metadata.**

```bash
git add docs/telegram-bot.md README.md docs/README.ru.md docs/architecture.md pyproject.toml src/iwiki_mcp/__init__.py tests/test_package.py tests/telegram_bot/test_end_to_end.py
git commit -m "docs(bot): add deployment guide and release metadata"
```

## Plan-Level Verification

- [ ] Confirm every item in `## Acceptance (from intent)` maps to Tasks 2–7.
- [ ] Confirm every hard constraint maps to Tasks 1–7 and is tested in Tasks 2–7.
- [ ] Run `uv run pytest -q` only after all task-level checks pass.
- [ ] Run `$check-chain result docs/superpowers/plans/2026-08-21-telegram-domain-bot-service.md` only after implementation and outcome evidence exist.
