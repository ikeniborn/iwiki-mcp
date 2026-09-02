"""Short-lived conversation workflows for the Telegram bot."""

from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable
from typing import Any

import anyio

from .access import AccessPolicy
from .context import ContextBudget
from .inference import InferenceError
from .iwiki import RemoteIwikiError
from .models import BotReply, PageTarget, PendingWrite, WritePreview


_TRANSCRIPTION_MAX_BYTES = 50 * 1024 * 1024
# Keep in sync with config.BotConfig.context_budget_chars.
_DEFAULT_CONTEXT_BUDGET_CHARS = 48000
_CONTEXT_SEPARATOR = "\n\n"

# A stage callback reports what the bot is doing while an update is processed.
ProgressCallback = Callable[[str], Awaitable[None]]
_STAGE_TRANSCRIBING = "Transcribing voice"
_STAGE_SEARCHING = "Searching wiki"
_STAGE_ANSWERING = "Generating answer"
_STAGE_READING = "Reading section"
_STAGE_DRAFTING = "Drafting Markdown"
_STAGE_SAVING = "Saving page"


async def _report(progress: ProgressCallback | None, stage: str) -> None:
    if progress is not None:
        await progress(stage)


class ConversationService:
    def __init__(
        self,
        access: AccessPolicy,
        remote: Any,
        inference: Any,
        *,
        confirmation_ttl_seconds: int,
        context_budget_chars: int = _DEFAULT_CONTEXT_BUDGET_CHARS,
        budget: ContextBudget | None = None,
        temporary_directory: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._access = access
        self._remote = remote
        self._inference = inference
        self._confirmation_ttl_seconds = confirmation_ttl_seconds
        # `context_budget_chars` is the operator's hard ceiling; the budget
        # actually spent is derived from the model window on every question.
        self._budget = budget or ContextBudget(ceiling_chars=context_budget_chars)
        self._temporary_directory = temporary_directory
        self._clock = clock
        # A selected domain is sticky for the process lifetime: it is a session
        # preference, not a confirmation secret, so it never expires.
        self._selected_domains: dict[int, str] = {}
        self._pending_writes: dict[str, PendingWrite] = {}
        self._pending_questions: dict[int, tuple[str, float]] = {}

    def _allowed(self, telegram_id: int) -> bool:
        return self._access.allows(telegram_id)

    def replace_remote(self, remote: Any) -> None:
        self._remote = remote

    @staticmethod
    def _remote_unavailable(error: RemoteIwikiError) -> BotReply:
        if error.retryable:
            raise error
        return BotReply("Wiki service is unavailable.", failed=True)

    @staticmethod
    def _inference_unavailable(error: InferenceError) -> BotReply:
        if str(error) == "context_overflow":
            return BotReply(
                "Question context is too large. Ask a narrower question.",
                failed=True,
            )
        if error.retryable:
            return BotReply(
                "Inference service is busy or too slow. Send the question again.",
                failed=True,
            )
        return BotReply("Inference service is unavailable.", failed=True)

    def expire_state(self) -> None:
        now = self._clock()
        self._pending_writes = {
            token: pending
            for token, pending in self._pending_writes.items()
            if pending.expires_at > now
        }
        self._pending_questions = {
            telegram_id: pending
            for telegram_id, pending in self._pending_questions.items()
            if pending[1] > now
        }

    def _selected_domain(self, telegram_id: int) -> str | None:
        self.expire_state()
        return self._selected_domains.get(telegram_id)

    async def _domain_buttons(self) -> tuple[tuple[str, str], ...]:
        domains = await self._remote.list_domains()
        return tuple((item, f"domain:{item}") for item in domains)

    async def list_domains(self, telegram_id: int) -> BotReply:
        if not self._allowed(telegram_id):
            return BotReply("Access denied.")
        try:
            buttons = await self._domain_buttons()
        except RemoteIwikiError as error:
            return self._remote_unavailable(error)
        return BotReply("Available domains:", buttons)

    async def _ask_for_domain(self, text: str) -> BotReply:
        """Offer the domain buttons instead of refusing work outright."""
        try:
            buttons = await self._domain_buttons()
        except RemoteIwikiError as error:
            return self._remote_unavailable(error)
        return BotReply(text, buttons)

    async def _defer_question(self, telegram_id: int, question: str) -> BotReply:
        self._pending_questions[telegram_id] = (
            question,
            self._clock() + self._confirmation_ttl_seconds,
        )
        return await self._ask_for_domain(
            "Select a domain and I will answer this question:\n"
            f"{question}"
        )

    async def select_domain(
        self,
        telegram_id: int,
        domain: str,
        progress: ProgressCallback | None = None,
    ) -> BotReply:
        if not self._allowed(telegram_id):
            return BotReply("Access denied.")
        try:
            domains = await self._remote.list_domains()
        except RemoteIwikiError as error:
            return self._remote_unavailable(error)
        if domain not in domains:
            return BotReply("Domain is not available.")
        self._selected_domains[telegram_id] = domain
        self.expire_state()
        pending = self._pending_questions.pop(telegram_id, None)
        if pending is None:
            return BotReply(f"Selected domain: {domain}")
        answer = await self._answer_selected(domain, pending[0], progress)
        return BotReply(
            f"Selected domain: {domain}\n\n{answer.text}",
            failed=answer.failed,
        )

    async def answer_question(
        self,
        telegram_id: int,
        question: str,
        progress: ProgressCallback | None = None,
    ) -> BotReply:
        if not self._allowed(telegram_id):
            return BotReply("Access denied.")
        domain = self._selected_domain(telegram_id)
        if domain is None:
            return await self._defer_question(telegram_id, question)
        return await self._answer_selected(domain, question, progress)

    async def _answer_selected(
        self,
        domain: str,
        question: str,
        progress: ProgressCallback | None = None,
    ) -> BotReply:
        try:
            await _report(progress, _STAGE_SEARCHING)
            sections = await self._collect_sections(domain, question)
            budget = self._budget.chars(len(question))
            context = self._assemble(sections, budget)
            if not context:
                return BotReply("No relevant wiki content found.")
            await _report(progress, _STAGE_ANSWERING)
            answer = await self._answer_within_budget(
                question, context, sections, budget
            )
        except KeyError:
            return BotReply("Wiki service is unavailable.", failed=True)
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

    @staticmethod
    def _unique_hits(
        results: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        """Keep the highest-ranked hit for each page section."""
        seen: set[tuple[str, str]] = set()
        unique: list[dict[str, object]] = []
        for result in results:
            heading = result.get("heading")
            key = (
                str(result.get("slug", "")),
                heading.strip() if isinstance(heading, str) else "",
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(result)
        return unique

    async def _collect_sections(self, domain: str, query: str) -> list[str]:
        """Read the section each deduplicated hit names, in result order."""
        results = await self._remote.search(domain, query)
        sections: list[str] = []
        for result in self._unique_hits(results):
            section = await self._read_section(domain, result)
            if section is not None:
                sections.append(section)
        return sections

    @staticmethod
    def _assemble(sections: list[str], budget: int) -> str:
        """Append sections in order and stop before the budget is exceeded."""
        chosen: list[str] = []
        used = 0
        for section in sections:
            if not chosen and len(section) > budget:
                return section[:budget]
            separator = len(_CONTEXT_SEPARATOR) if chosen else 0
            if used + separator + len(section) > budget:
                break
            used += separator + len(section)
            chosen.append(section)
        return _CONTEXT_SEPARATOR.join(chosen)

    async def _answer_within_budget(
        self,
        question: str,
        context: str,
        sections: list[str],
        budget: int,
    ) -> str:
        """Answer, retrying once with a halved budget on a context overflow."""
        try:
            return await self._inference.answer(question, context)
        except InferenceError as error:
            if str(error) != "context_overflow":
                raise
        # The provider refused the assembled prompt. Retry once with half the
        # budget, reusing the sections already read: no further wiki call.
        halved = max(budget // 2, 1)
        return await self._inference.answer(
            question, self._assemble(sections, halved)
        )

    async def _retrieve_context(self, domain: str, query: str) -> str:
        """Assemble retrieved sections in result order, within the budget."""
        sections = await self._collect_sections(domain, query)
        return self._assemble(sections, self._budget.chars(len(query)))

    async def answer_voice(
        self,
        telegram_id: int,
        filename: str,
        audio: bytes,
        progress: ProgressCallback | None = None,
    ) -> BotReply:
        if not self._allowed(telegram_id):
            return BotReply("Access denied.")
        question = await self._transcribe(audio, progress)
        if isinstance(question, BotReply):
            return question
        domain = self._selected_domain(telegram_id)
        if domain is None:
            return await self._defer_question(telegram_id, question)
        return await self._answer_selected(domain, question, progress)

    async def _transcribe(
        self, audio: bytes, progress: ProgressCallback | None = None
    ) -> str | BotReply:
        """Convert one voice message to text, or report why it failed."""
        try:
            await _report(progress, _STAGE_TRANSCRIBING)
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
                    return BotReply(
                        "Voice transcription is unavailable.", failed=True
                    )
                transient_audio = wav_path.read_bytes()
                if (
                    not transient_audio.startswith(b"RIFF")
                    or transient_audio[8:12] != b"WAVE"
                ):
                    return BotReply(
                        "Voice transcription is unavailable.", failed=True
                    )
                question = await self._inference.transcribe(
                    "audio.wav", transient_audio
                )
        except (InferenceError, OSError, subprocess.CalledProcessError):
            return BotReply("Voice transcription is unavailable.", failed=True)
        return question

    async def propose_create(
        self,
        telegram_id: int,
        slug: str,
        request: str,
        progress: ProgressCallback | None = None,
    ) -> WritePreview | BotReply:
        domain = await self._write_domain(telegram_id)
        if isinstance(domain, BotReply):
            return domain
        try:
            await _report(progress, _STAGE_SEARCHING)
            context = await self._draft_context(domain, request)
            await _report(progress, _STAGE_DRAFTING)
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
        self,
        telegram_id: int,
        slug: str,
        heading: str,
        request: str,
        progress: ProgressCallback | None = None,
    ) -> WritePreview | BotReply:
        domain = await self._write_domain(telegram_id)
        if isinstance(domain, BotReply):
            return domain
        if not slug or not heading:
            return BotReply("Page and section are required.")
        try:
            await _report(progress, _STAGE_READING)
            page = await self._remote.read_page(domain, slug, heading)
            context = page.get("body", page.get("markdown"))
            if not isinstance(context, str):
                return BotReply("Page section is unavailable.", failed=True)
            await _report(progress, _STAGE_DRAFTING)
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

    async def _write_domain(self, telegram_id: int) -> str | BotReply:
        if not self._allowed(telegram_id):
            return BotReply("Access denied.")
        domain = self._selected_domain(telegram_id)
        if domain is None:
            return await self._ask_for_domain(
                "Select a domain, then send the command again."
            )
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

    async def confirm_write(
        self,
        telegram_id: int,
        token: str,
        progress: ProgressCallback | None = None,
    ) -> BotReply:
        if not self._allowed(telegram_id):
            return BotReply("Access denied.")
        pending, error = self._consume_pending(telegram_id, token)
        if pending is None:
            return BotReply(error or "Confirmation is invalid.")
        try:
            await _report(progress, _STAGE_SAVING)
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
                    return BotReply("Page section is unavailable.", failed=True)
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
            return BotReply("Wiki service is unavailable.", failed=True)
        return BotReply("Page change saved.")
