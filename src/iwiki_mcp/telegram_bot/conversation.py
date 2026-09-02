"""Short-lived conversation workflows for the Telegram bot."""

from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from collections.abc import Callable
from typing import Any

import anyio

from .access import AccessPolicy
from .inference import InferenceError
from .iwiki import RemoteIwikiError
from .models import BotReply, PageTarget, PendingWrite, WritePreview


_TRANSCRIPTION_MAX_BYTES = 50 * 1024 * 1024
# Keep in sync with config.BotConfig.context_budget_chars.
_DEFAULT_CONTEXT_BUDGET_CHARS = 48000
_CONTEXT_SEPARATOR = "\n\n"


class ConversationService:
    def __init__(
        self,
        access: AccessPolicy,
        remote: Any,
        inference: Any,
        *,
        confirmation_ttl_seconds: int,
        context_budget_chars: int = _DEFAULT_CONTEXT_BUDGET_CHARS,
        temporary_directory: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._access = access
        self._remote = remote
        self._inference = inference
        self._confirmation_ttl_seconds = confirmation_ttl_seconds
        self._context_budget_chars = context_budget_chars
        self._temporary_directory = temporary_directory
        self._clock = clock
        self._selected_domains: dict[int, tuple[str, float]] = {}
        self._pending_writes: dict[str, PendingWrite] = {}

    def _allowed(self, telegram_id: int) -> bool:
        return self._access.allows(telegram_id)

    def replace_remote(self, remote: Any) -> None:
        self._remote = remote

    @staticmethod
    def _remote_unavailable(error: RemoteIwikiError) -> BotReply:
        if error.retryable:
            raise error
        return BotReply("Wiki service is unavailable.")

    @staticmethod
    def _inference_unavailable(error: InferenceError) -> BotReply:
        if str(error) == "context_overflow":
            return BotReply(
                "Question context is too large. Ask a narrower question."
            )
        return BotReply("Inference service is unavailable.")

    def expire_state(self) -> None:
        now = self._clock()
        self._selected_domains = {
            telegram_id: selected
            for telegram_id, selected in self._selected_domains.items()
            if selected[1] > now
        }
        self._pending_writes = {
            token: pending
            for token, pending in self._pending_writes.items()
            if pending.expires_at > now
        }

    def _selected_domain(self, telegram_id: int) -> str | None:
        self.expire_state()
        selected = self._selected_domains.get(telegram_id)
        return selected[0] if selected is not None else None

    async def list_domains(self, telegram_id: int) -> BotReply:
        if not self._allowed(telegram_id):
            return BotReply("Access denied.")
        try:
            domains = await self._remote.list_domains()
        except RemoteIwikiError as error:
            return self._remote_unavailable(error)
        return BotReply(
            "Available domains:",
            tuple((item, f"domain:{item}") for item in domains),
        )

    async def select_domain(self, telegram_id: int, domain: str) -> BotReply:
        if not self._allowed(telegram_id):
            return BotReply("Access denied.")
        try:
            domains = await self._remote.list_domains()
        except RemoteIwikiError as error:
            return self._remote_unavailable(error)
        if domain not in domains:
            return BotReply("Domain is not available.")
        self._selected_domains[telegram_id] = (
            domain,
            self._clock() + self._confirmation_ttl_seconds,
        )
        return BotReply(f"Selected domain: {domain}")

    async def answer_question(self, telegram_id: int, question: str) -> BotReply:
        if not self._allowed(telegram_id):
            return BotReply("Access denied.")
        domain = self._selected_domain(telegram_id)
        if domain is None:
            return BotReply("Select a domain first.")
        return await self._answer_selected(domain, question)

    async def _answer_selected(self, domain: str, question: str) -> BotReply:
        try:
            context = await self._retrieve_context(domain, question)
            if not context:
                return BotReply("No relevant wiki content found.")
            answer = await self._inference.answer(question, context)
        except KeyError:
            return BotReply("Wiki service is unavailable.")
        except RemoteIwikiError as error:
            return self._remote_unavailable(error)
        except InferenceError as error:
            return self._inference_unavailable(error)
        return BotReply(answer)

    async def _read_section(
        self, domain: str, result: dict[str, object]
    ) -> str | None:
        """Read the section a search hit names, or the whole page without one."""
        slug = str(result["slug"])
        heading = result.get("heading")
        if isinstance(heading, str) and heading.strip():
            page = await self._remote.read_page(domain, slug, heading)
            body = page.get("body", page.get("markdown"))
        else:
            page = await self._remote.read_page(domain, slug)
            body = page.get("markdown")
        return body if isinstance(body, str) and body else None

    async def _retrieve_context(self, domain: str, query: str) -> str:
        """Assemble retrieved sections in result order, within the budget."""
        results = await self._remote.search(domain, query)
        budget = self._context_budget_chars
        sections: list[str] = []
        used = 0
        for result in results:
            section = await self._read_section(domain, result)
            if section is None:
                continue
            if not sections and len(section) > budget:
                return section[:budget]
            separator = len(_CONTEXT_SEPARATOR) if sections else 0
            if used + separator + len(section) > budget:
                break
            used += separator + len(section)
            sections.append(section)
        return _CONTEXT_SEPARATOR.join(sections)

    async def answer_voice(
        self, telegram_id: int, filename: str, audio: bytes
    ) -> BotReply:
        if not self._allowed(telegram_id):
            return BotReply("Access denied.")
        domain = self._selected_domain(telegram_id)
        if domain is None:
            return BotReply("Select a domain first.")

        try:
            with tempfile.TemporaryDirectory(
                dir=self._temporary_directory
            ) as temporary:
                temporary_path = Path(temporary)
                source_path = temporary_path / "input.ogg"
                wav_path = temporary_path / "audio.wav"
                source_path.write_bytes(audio)
                await anyio.run_process(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        str(source_path),
                        "-vn",
                        "-acodec",
                        "pcm_s16le",
                        "-ar",
                        "16000",
                        "-ac",
                        "1",
                        "-fs",
                        str(_TRANSCRIPTION_MAX_BYTES),
                        str(wav_path),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if wav_path.stat().st_size > _TRANSCRIPTION_MAX_BYTES:
                    return BotReply("Voice transcription is unavailable.")
                transient_audio = wav_path.read_bytes()
                if (
                    not transient_audio.startswith(b"RIFF")
                    or transient_audio[8:12] != b"WAVE"
                ):
                    return BotReply("Voice transcription is unavailable.")
                question = await self._inference.transcribe(
                    "audio.wav", transient_audio
                )
        except (InferenceError, OSError, subprocess.CalledProcessError):
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
        except RemoteIwikiError as error:
            return self._remote_unavailable(error)
        except InferenceError as error:
            return self._inference_unavailable(error)
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
        except RemoteIwikiError as error:
            return self._remote_unavailable(error)
        except InferenceError as error:
            return self._inference_unavailable(error)
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
        domain = self._selected_domain(telegram_id)
        if domain is None:
            return BotReply("Select a domain first.")
        return domain

    async def _draft_context(self, domain: str, request: str) -> str:
        return await self._retrieve_context(domain, request)

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
            if exc.retryable:
                raise
            if str(exc) in {"conflict", "section_conflict"}:
                return BotReply("Page changed; request a new preview.")
            return BotReply("Wiki service is unavailable.")
        return BotReply("Page change saved.")
