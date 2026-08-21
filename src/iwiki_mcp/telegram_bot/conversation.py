"""Short-lived conversation workflows for the Telegram bot."""

from pathlib import Path
import secrets
import tempfile
import time
from collections.abc import Callable
from typing import Any

from .access import AccessPolicy
from .inference import InferenceError
from .iwiki import RemoteIwikiError
from .models import BotReply, PageTarget, PendingWrite, WritePreview


class ConversationService:
    def __init__(
        self,
        access: AccessPolicy,
        remote: Any,
        inference: Any,
        *,
        confirmation_ttl_seconds: int,
        temporary_directory: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._access = access
        self._remote = remote
        self._inference = inference
        self._confirmation_ttl_seconds = confirmation_ttl_seconds
        self._temporary_directory = temporary_directory
        self._clock = clock
        self._selected_domains: dict[int, str] = {}
        self._pending_writes: dict[str, PendingWrite] = {}

    def _allowed(self, telegram_id: int) -> bool:
        return self._access.allows(telegram_id)

    async def list_domains(self, telegram_id: int) -> BotReply:
        if not self._allowed(telegram_id):
            return BotReply("Access denied.")
        try:
            domains = await self._remote.list_domains()
        except RemoteIwikiError:
            return BotReply("Wiki service is unavailable.")
        return BotReply("Available domains:", tuple(f"domain:{item}" for item in domains))

    async def select_domain(self, telegram_id: int, domain: str) -> BotReply:
        if not self._allowed(telegram_id):
            return BotReply("Access denied.")
        try:
            domains = await self._remote.list_domains()
        except RemoteIwikiError:
            return BotReply("Wiki service is unavailable.")
        if domain not in domains:
            return BotReply("Domain is not available.")
        self._selected_domains[telegram_id] = domain
        return BotReply(f"Selected domain: {domain}")

    async def answer_question(self, telegram_id: int, question: str) -> BotReply:
        if not self._allowed(telegram_id):
            return BotReply("Access denied.")
        domain = self._selected_domains.get(telegram_id)
        if domain is None:
            return BotReply("Select a domain first.")
        return await self._answer_selected(domain, question)

    async def _answer_selected(self, domain: str, question: str) -> BotReply:
        try:
            results = await self._remote.search(domain, question)
            if not results:
                return BotReply("No relevant wiki content found.")
            pages = [
                await self._remote.read_page(domain, str(result["slug"]))
                for result in results
            ]
            context = "\n\n".join(
                str(page["markdown"])
                for page in pages
                if isinstance(page.get("markdown"), str)
            )
            if not context:
                return BotReply("No relevant wiki content found.")
            answer = await self._inference.answer(question, context)
        except (KeyError, RemoteIwikiError):
            return BotReply("Wiki service is unavailable.")
        except InferenceError:
            return BotReply("Inference service is unavailable.")
        return BotReply(answer)

    async def answer_voice(
        self, telegram_id: int, filename: str, audio: bytes
    ) -> BotReply:
        if not self._allowed(telegram_id):
            return BotReply("Access denied.")
        domain = self._selected_domains.get(telegram_id)
        if domain is None:
            return BotReply("Select a domain first.")

        safe_name = Path(filename).name or "voice.ogg"
        suffix = Path(safe_name).suffix or ".ogg"
        try:
            with tempfile.NamedTemporaryFile(
                suffix=suffix, dir=self._temporary_directory
            ) as temporary:
                temporary.write(audio)
                temporary.flush()
                transient_audio = Path(temporary.name).read_bytes()
                question = await self._inference.transcribe(safe_name, transient_audio)
        except (InferenceError, OSError):
            return BotReply("Voice transcription is unavailable.")
        return await self._answer_selected(domain, question)

    async def propose_create(
        self, telegram_id: int, slug: str, request: str
    ) -> WritePreview | BotReply:
        domain = self._write_domain(telegram_id)
        if isinstance(domain, BotReply):
            return domain
        try:
            context = await self._draft_context(domain, request)
            markdown = await self._inference.draft_markdown(request, context)
        except RemoteIwikiError:
            return BotReply("Wiki service is unavailable.")
        except InferenceError:
            return BotReply("Inference service is unavailable.")
        return self._store_preview(
            telegram_id,
            "create",
            {"domain": domain, "slug": slug, "markdown": markdown},
            markdown,
        )

    async def propose_update(
        self, telegram_id: int, slug: str, heading: str, request: str
    ) -> WritePreview | BotReply:
        domain = self._write_domain(telegram_id)
        if isinstance(domain, BotReply):
            return domain
        if not slug or not heading:
            return BotReply("Page and section are required.")
        try:
            page = await self._remote.read_page(domain, slug, heading)
            context = page.get("body", page.get("markdown"))
            if not isinstance(context, str):
                return BotReply("Page section is unavailable.")
            markdown = await self._inference.draft_markdown(request, context)
        except RemoteIwikiError:
            return BotReply("Wiki service is unavailable.")
        except InferenceError:
            return BotReply("Inference service is unavailable.")
        target = PageTarget(domain=domain, slug=slug, heading=heading)
        return self._store_preview(
            telegram_id,
            "update",
            {"target": target, "new_body": markdown},
            markdown,
        )

    def _write_domain(self, telegram_id: int) -> str | BotReply:
        if not self._allowed(telegram_id):
            return BotReply("Access denied.")
        domain = self._selected_domains.get(telegram_id)
        if domain is None:
            return BotReply("Select a domain first.")
        return domain

    async def _draft_context(self, domain: str, request: str) -> str:
        results = await self._remote.search(domain, request)
        pages = [
            await self._remote.read_page(domain, str(result["slug"]))
            for result in results
        ]
        return "\n\n".join(
            str(page["markdown"])
            for page in pages
            if isinstance(page.get("markdown"), str)
        )

    def _store_preview(
        self,
        telegram_id: int,
        action: str,
        payload: dict[str, object],
        text: str,
    ) -> WritePreview:
        token = secrets.token_urlsafe(18)
        self._pending_writes[token] = PendingWrite(
            token=token,
            telegram_id=telegram_id,
            action=action,
            payload=payload,
            expires_at=self._clock() + self._confirmation_ttl_seconds,
        )
        return WritePreview(token=token, text=text)

    def _consume_pending(
        self, telegram_id: int, token: str
    ) -> tuple[PendingWrite | None, str | None]:
        pending = self._pending_writes.get(token)
        if pending is None or pending.telegram_id != telegram_id:
            return None, "Confirmation is invalid."
        self._pending_writes.pop(token, None)
        if pending.expires_at < self._clock():
            return None, "Confirmation expired."
        return pending, None

    async def reject_write(self, telegram_id: int, token: str) -> BotReply:
        if not self._allowed(telegram_id):
            return BotReply("Access denied.")
        pending, error = self._consume_pending(telegram_id, token)
        if pending is None:
            return BotReply(error or "Confirmation is invalid.")
        return BotReply("Change rejected.")

    async def confirm_write(self, telegram_id: int, token: str) -> BotReply:
        if not self._allowed(telegram_id):
            return BotReply("Access denied.")
        pending, error = self._consume_pending(telegram_id, token)
        if pending is None:
            return BotReply(error or "Confirmation is invalid.")
        try:
            if pending.action == "create":
                await self._remote.write_page(
                    str(pending.payload["domain"]),
                    str(pending.payload["slug"]),
                    str(pending.payload["markdown"]),
                )
            elif pending.action == "update":
                target = pending.payload["target"]
                if not isinstance(target, PageTarget) or target.heading is None:
                    return BotReply("Confirmation is invalid.")
                page = await self._remote.read_page(
                    target.domain, target.slug, target.heading
                )
                revision = page.get("revision")
                section_hash = page.get("section_hash")
                if not isinstance(revision, int) or not isinstance(section_hash, str):
                    return BotReply("Page section is unavailable.")
                await self._remote.update_section(
                    target.domain,
                    target.slug,
                    target.heading,
                    str(pending.payload["new_body"]),
                    revision,
                    section_hash,
                )
            else:
                return BotReply("Confirmation is invalid.")
        except RemoteIwikiError as exc:
            if str(exc) in {"conflict", "section_conflict"}:
                return BotReply("Page changed; request a new preview.")
            return BotReply("Wiki service is unavailable.")
        return BotReply("Page change saved.")
