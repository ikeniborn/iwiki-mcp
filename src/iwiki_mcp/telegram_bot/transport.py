"""Minimal Telegram Bot API long-polling transport."""

from collections.abc import Awaitable, Callable
import json
import logging
from pathlib import Path
import random
import time
from typing import Any

import anyio
import urllib3

from .access import AccessPolicy
from .models import BotReply, WritePreview
from .proxy import TelegramHttpClient
from .runtime import Backoff, Heartbeat


LOGGER = logging.getLogger(__name__)
_COMMANDS = (
    ("menu", "Open the action menu"),
    ("domains", "List the domains this bot can read"),
    ("create", "Draft a new page: /create <slug>: <request>"),
    ("update", "Draft one section: /update <slug>#<heading>: <request>"),
    ("help", "Show the command reference"),
)
_MENU_BUTTONS = (
    ("Domains", "menu:domains"),
    ("Create page", "menu:create"),
    ("Update section", "menu:update"),
    ("Help", "menu:help"),
)
_MENU_TEXT = "Choose an action:"
_HELP_TEXT = (
    "/menu opens this menu.\n"
    "/domains lists domains; a domain button selects one.\n"
    "Plain text asks a question about the selected domain.\n"
    "A voice message is transcribed and asked the same way.\n"
    "/create <slug>: <request> drafts a new page.\n"
    "/update <slug>#<heading>: <request> drafts one section."
)
_MENU_HINTS = {
    "create": "Use /create <slug>: <request>.",
    "update": "Use /update <slug>#<heading>: <request>.",
    "help": _HELP_TEXT,
}
# Telegram clears a chat action after about five seconds, so refresh it sooner.
_CHAT_ACTION_INTERVAL_SECONDS = 4.0
_STATUS_PREFIX = "⏳ "
# Only emoji from the Telegram reaction allowlist are accepted.
_REACTION_WORKING = "👀"
_REACTION_DONE = "👍"
_REACTION_FAILED = "🤨"


class TelegramError(RuntimeError):
    """A sanitized Telegram transport failure."""


class _ProgressSession:
    """One update's status message, edited in place as stages are reported."""

    def __init__(self, transport: "TelegramTransport", chat_id: int) -> None:
        self._transport = transport
        self._chat_id = chat_id
        self._message_id: int | None = None
        self._muted = False

    async def stage(self, text: str) -> None:
        rendered = f"{_STATUS_PREFIX}{text}…"
        if self._muted:
            return
        if self._message_id is None:
            self._message_id = await self._transport.send_status(
                self._chat_id, rendered
            )
            # Without a message to edit, further stages would each post a new
            # message, so report none of them.
            self._muted = self._message_id is None
            return
        await self._transport.edit_status(
            self._chat_id, self._message_id, rendered
        )

    async def finish(self, reply: BotReply | WritePreview) -> None:
        """Replace the status message with the answer, or send a new message."""
        if self._message_id is not None and await self._transport.edit_reply(
            self._chat_id, self._message_id, reply
        ):
            return
        await self._transport.send(self._chat_id, reply)


class TelegramTransport:
    def __init__(
        self,
        token: str,
        access: AccessPolicy,
        conversation: Any,
        http: TelegramHttpClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    ) -> None:
        self._token = token
        self._access = access
        self._conversation = conversation
        self._http = http
        self._sleep = sleep
        self._api_base = f"https://api.telegram.org/bot{token}"
        self._file_base = f"https://api.telegram.org/file/bot{token}"
        self._poll_offset: int | None = None
        # Set while polling so long work keeps the liveness heartbeat fresh.
        self._heartbeat: Heartbeat | None = None

    async def _api(self, method: str, data: dict[str, object]) -> object:
        try:
            response = await self._http.post_json(
                f"{self._api_base}/{method}", data
            )
            if not 200 <= response.status < 300:
                raise TelegramError("telegram_request_failed")
            payload = json.loads(response.body)
        except TelegramError:
            raise
        except (
            urllib3.exceptions.HTTPError,
            OSError,
            UnicodeError,
            json.JSONDecodeError,
        ):
            raise TelegramError("telegram_request_failed") from None
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise TelegramError("telegram_request_failed")
        return payload.get("result")

    async def _download_voice(self, file_id: str) -> tuple[str, bytes]:
        metadata = await self._api("getFile", {"file_id": file_id})
        if not isinstance(metadata, dict) or not isinstance(
            metadata.get("file_path"), str
        ):
            raise TelegramError("telegram_file_failed")
        file_path = metadata["file_path"]
        try:
            response = await self._http.get_bytes(
                f"{self._file_base}/{file_path}"
            )
            if not 200 <= response.status < 300:
                raise TelegramError("telegram_file_failed")
        except TelegramError:
            raise
        except (urllib3.exceptions.HTTPError, OSError, UnicodeError):
            raise TelegramError("telegram_file_failed") from None
        return Path(file_path).name or "voice.ogg", response.body

    @staticmethod
    def _reply_markup(reply: BotReply | WritePreview) -> dict[str, object] | None:
        if isinstance(reply, WritePreview):
            return {
                "inline_keyboard": [
                    [
                        {
                            "text": "Confirm",
                            "callback_data": f"confirm:{reply.token}",
                        },
                        {"text": "Reject", "callback_data": f"reject:{reply.token}"},
                    ]
                ]
            }
        if reply.buttons:
            return {
                "inline_keyboard": [
                    [{"text": label, "callback_data": data}]
                    for label, data in reply.buttons
                ]
            }
        return None

    async def send(self, chat_id: int, reply: BotReply | WritePreview) -> None:
        payload: dict[str, object] = {"chat_id": chat_id, "text": reply.text}
        markup = self._reply_markup(reply)
        if markup is not None:
            payload["reply_markup"] = markup
        await self._api("sendMessage", payload)

    async def send_status(self, chat_id: int, text: str) -> int | None:
        """Post the first status line; feedback failures never fail the update."""
        try:
            result = await self._api(
                "sendMessage", {"chat_id": chat_id, "text": text}
            )
        except TelegramError:
            return None
        message_id = result.get("message_id") if isinstance(result, dict) else None
        return message_id if isinstance(message_id, int) else None

    async def edit_status(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            await self._api(
                "editMessageText",
                {"chat_id": chat_id, "message_id": message_id, "text": text},
            )
        except TelegramError:
            LOGGER.warning(
                "telegram status edit failed",
                extra={"operation": "edit_status", "outcome": "failure"},
            )

    async def edit_reply(
        self, chat_id: int, message_id: int, reply: BotReply | WritePreview
    ) -> bool:
        """Turn the status message into the answer; False asks for a new one."""
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": reply.text,
        }
        markup = self._reply_markup(reply)
        if markup is not None:
            payload["reply_markup"] = markup
        try:
            await self._api("editMessageText", payload)
        except TelegramError:
            return False
        return True

    async def _react(self, chat_id: int, message_id: int | None, emoji: str) -> None:
        if message_id is None:
            return
        try:
            await self._api(
                "setMessageReaction",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reaction": [{"type": "emoji", "emoji": emoji}],
                },
            )
        except TelegramError:
            LOGGER.warning(
                "telegram reaction failed",
                extra={"operation": "set_reaction", "outcome": "failure"},
            )

    async def _chat_action(self, chat_id: int) -> None:
        try:
            await self._api(
                "sendChatAction", {"chat_id": chat_id, "action": "typing"}
            )
        except TelegramError:
            LOGGER.warning(
                "telegram chat action failed",
                extra={"operation": "chat_action", "outcome": "failure"},
            )

    async def _keep_alive(self, chat_id: int) -> None:
        """Refresh the typing action and the liveness heartbeat while working."""
        while True:
            await self._sleep(_CHAT_ACTION_INTERVAL_SECONDS)
            await self._chat_action(chat_id)
            if self._heartbeat is not None:
                self._heartbeat.touch()

    async def _run_with_feedback(
        self,
        chat_id: int,
        message_id: int | None,
        work: Callable[[_ProgressSession], Awaitable[BotReply | WritePreview]],
    ) -> None:
        """Show the work in progress, then deliver its reply."""
        session = _ProgressSession(self, chat_id)
        await self._react(chat_id, message_id, _REACTION_WORKING)
        await self._chat_action(chat_id)
        reply: BotReply | WritePreview | None = None
        error: Exception | None = None
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(self._keep_alive, chat_id)
            try:
                reply = await work(session)
            except Exception as caught:
                error = caught
            tasks.cancel_scope.cancel()
        if error is not None:
            # The update is replayed after the session recovers, so leave the
            # working reaction in place instead of reporting a final outcome.
            raise error
        if reply is None:
            return
        failed = bool(getattr(reply, "failed", False))
        try:
            await session.finish(reply)
        finally:
            await self._react(
                chat_id,
                message_id,
                _REACTION_FAILED if failed else _REACTION_DONE,
            )

    async def publish_commands(self) -> None:
        """Register the '/' command list and point the menu button at it."""
        try:
            await self._api(
                "setMyCommands",
                {
                    "commands": [
                        {"command": name, "description": description}
                        for name, description in _COMMANDS
                    ]
                },
            )
            await self._api(
                "setChatMenuButton", {"menu_button": {"type": "commands"}}
            )
        except TelegramError:
            LOGGER.warning(
                "telegram command registration failed",
                extra={"operation": "set_commands", "outcome": "failure"},
            )

    async def handle_update(self, update: dict[str, object]) -> None:
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            await self._handle_callback(callback)
            return
        message = update.get("message")
        if isinstance(message, dict):
            await self._handle_message(message)

    @staticmethod
    def _identity(payload: dict[str, object]) -> tuple[int, int] | None:
        sender = payload.get("from")
        message = payload.get("message", payload)
        chat = message.get("chat") if isinstance(message, dict) else None
        if not isinstance(sender, dict) or not isinstance(chat, dict):
            return None
        telegram_id = sender.get("id")
        chat_id = chat.get("id")
        if not isinstance(telegram_id, int) or not isinstance(chat_id, int):
            return None
        return telegram_id, chat_id

    async def _handle_message(self, message: dict[str, object]) -> None:
        identity = self._identity(message)
        if identity is None:
            return
        telegram_id, chat_id = identity
        if not self._access.allows(telegram_id):
            await self.send(chat_id, BotReply("Access denied."))
            return
        raw_message_id = message.get("message_id")
        message_id = raw_message_id if isinstance(raw_message_id, int) else None

        voice = message.get("voice")
        if isinstance(voice, dict) and isinstance(voice.get("file_id"), str):
            file_id = voice["file_id"]
            await self._run_with_feedback(
                chat_id,
                message_id,
                lambda progress: self._voice_reply(telegram_id, file_id, progress),
            )
            return

        text = message.get("text")
        if not isinstance(text, str):
            return
        stripped = text.strip()
        immediate = self._immediate_reply(stripped)
        if immediate is not None:
            await self.send(chat_id, immediate)
            return
        await self._run_with_feedback(
            chat_id,
            message_id,
            lambda progress: self._dispatch_text(telegram_id, stripped, progress),
        )

    async def _voice_reply(
        self, telegram_id: int, file_id: str, progress: _ProgressSession
    ) -> BotReply | WritePreview:
        try:
            filename, audio = await self._download_voice(file_id)
        except TelegramError:
            return BotReply("Voice download failed.", failed=True)
        return await self._conversation.answer_voice(
            telegram_id, filename, audio, progress.stage
        )

    @staticmethod
    def _immediate_reply(text: str) -> BotReply | None:
        """Answer the static commands without touching iwiki or inference."""
        if text in ("/menu", "/start"):
            return BotReply(_MENU_TEXT, _MENU_BUTTONS)
        if text == "/help":
            return BotReply(_HELP_TEXT)
        return None

    async def _dispatch_text(
        self,
        telegram_id: int,
        text: str,
        progress: _ProgressSession | None = None,
    ) -> BotReply | WritePreview:
        immediate = self._immediate_reply(text)
        if immediate is not None:
            return immediate
        stage = progress.stage if progress is not None else None
        if text == "/domains":
            return await self._conversation.list_domains(telegram_id)
        if text.startswith("/create "):
            parsed = self._split_command(text.removeprefix("/create "))
            if parsed is None:
                return BotReply("Use /create <slug>: <request>.")
            slug, request = parsed
            return await self._conversation.propose_create(
                telegram_id, slug, request, stage
            )
        if text.startswith("/update "):
            parsed = self._split_command(text.removeprefix("/update "))
            if parsed is None or "#" not in parsed[0]:
                return BotReply("Use /update <slug>#<heading>: <request>.")
            target, request = parsed
            slug, heading = (part.strip() for part in target.split("#", 1))
            if not slug or not heading:
                return BotReply("Use /update <slug>#<heading>: <request>.")
            return await self._conversation.propose_update(
                telegram_id, slug, heading, request, stage
            )
        if text.startswith("/"):
            return BotReply("Unknown command. Send /menu.")
        if not text:
            return BotReply("Send a question.")
        return await self._conversation.answer_question(telegram_id, text, stage)

    @staticmethod
    def _split_command(value: str) -> tuple[str, str] | None:
        if ":" not in value:
            return None
        target, request = (part.strip() for part in value.split(":", 1))
        if not target or not request:
            return None
        return target, request

    async def _handle_callback(self, callback: dict[str, object]) -> None:
        identity = self._identity(callback)
        if identity is None:
            return
        telegram_id, chat_id = identity
        callback_id = callback.get("id")
        if isinstance(callback_id, str):
            await self._api("answerCallbackQuery", {"callback_query_id": callback_id})
        if not self._access.allows(telegram_id):
            await self.send(chat_id, BotReply("Access denied."))
            return
        data = callback.get("data")
        if not isinstance(data, str) or ":" not in data:
            await self.send(chat_id, BotReply("Invalid action."))
            return
        action, value = data.split(":", 1)
        if action == "menu" and value in _MENU_HINTS:
            await self.send(chat_id, BotReply(_MENU_HINTS[value]))
            return
        await self._run_with_feedback(
            chat_id,
            None,
            lambda progress: self._callback_reply(
                telegram_id, action, value, progress
            ),
        )

    async def _callback_reply(
        self,
        telegram_id: int,
        action: str,
        value: str,
        progress: _ProgressSession,
    ) -> BotReply | WritePreview:
        if action == "menu":
            if value != "domains":
                return BotReply("Invalid action.")
            return await self._conversation.list_domains(telegram_id)
        if action == "domain":
            return await self._conversation.select_domain(
                telegram_id, value, progress.stage
            )
        if action == "confirm":
            return await self._conversation.confirm_write(
                telegram_id, value, progress.stage
            )
        if action == "reject":
            return await self._conversation.reject_write(telegram_id, value)
        return BotReply("Invalid action.")

    async def _fetch_updates(self, offset: int | None) -> list[object]:
        self._conversation.expire_state()
        arguments: dict[str, object] = {"timeout": 30}
        if offset is not None:
            arguments["offset"] = offset
        updates = await self._api("getUpdates", arguments)
        if not isinstance(updates, list):
            raise TelegramError("telegram_request_failed")
        return updates

    async def _dispatch_updates(
        self,
        updates: list[object],
        offset: int | None,
        *,
        remember_offset: bool = False,
    ) -> int | None:
        next_offset = offset
        for update in updates:
            if not isinstance(update, dict):
                continue
            await self.handle_update(update)
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                next_offset = update_id + 1
                if remember_offset:
                    self._poll_offset = next_offset
        return next_offset

    async def poll_once(self, offset: int | None) -> int | None:
        updates = await self._fetch_updates(offset)
        return await self._dispatch_updates(updates, offset)

    async def poll_forever(
        self,
        *,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
        random_value: Callable[[], float] = random.random,
        heartbeat: Heartbeat,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        backoff = Backoff()
        self._heartbeat = heartbeat
        while True:
            started = clock()
            try:
                updates = await self._fetch_updates(self._poll_offset)
            except TelegramError:
                delay = backoff.next_delay(random_value())
                LOGGER.warning(
                    "telegram poll retry",
                    extra={
                        "operation": "poll",
                        "outcome": "retry",
                        "delay_seconds": float(delay),
                        "elapsed_ms": int((clock() - started) * 1000),
                    },
                )
                await sleep(delay)
                continue
            backoff.reset()
            heartbeat.touch()
            await self._dispatch_updates(
                updates,
                self._poll_offset,
                remember_offset=True,
            )
