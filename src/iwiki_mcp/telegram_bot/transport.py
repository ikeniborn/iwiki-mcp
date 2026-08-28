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


class TelegramError(RuntimeError):
    """A sanitized Telegram transport failure."""


class TelegramTransport:
    def __init__(
        self,
        token: str,
        access: AccessPolicy,
        conversation: Any,
        http: TelegramHttpClient,
    ) -> None:
        self._token = token
        self._access = access
        self._conversation = conversation
        self._http = http
        self._api_base = f"https://api.telegram.org/bot{token}"
        self._file_base = f"https://api.telegram.org/file/bot{token}"

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

    async def _send(self, chat_id: int, reply: BotReply | WritePreview) -> None:
        payload: dict[str, object] = {"chat_id": chat_id, "text": reply.text}
        if isinstance(reply, WritePreview):
            payload["reply_markup"] = {
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
        elif reply.buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [
                        {
                            "text": button.split(":", 1)[-1],
                            "callback_data": button,
                        }
                    ]
                    for button in reply.buttons
                ]
            }
        await self._api("sendMessage", payload)

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
            await self._send(chat_id, BotReply("Access denied."))
            return

        voice = message.get("voice")
        if isinstance(voice, dict) and isinstance(voice.get("file_id"), str):
            try:
                filename, audio = await self._download_voice(voice["file_id"])
                reply = await self._conversation.answer_voice(
                    telegram_id, filename, audio
                )
            except TelegramError:
                reply = BotReply("Voice download failed.")
            await self._send(chat_id, reply)
            return

        text = message.get("text")
        if not isinstance(text, str):
            return
        reply = await self._dispatch_text(telegram_id, text.strip())
        await self._send(chat_id, reply)

    async def _dispatch_text(
        self, telegram_id: int, text: str
    ) -> BotReply | WritePreview:
        if text == "/domains":
            return await self._conversation.list_domains(telegram_id)
        if text.startswith("/create "):
            parsed = self._split_command(text.removeprefix("/create "))
            if parsed is None:
                return BotReply("Use /create <slug>: <request>.")
            slug, request = parsed
            return await self._conversation.propose_create(
                telegram_id, slug, request
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
                telegram_id, slug, heading, request
            )
        if text.startswith("/"):
            return BotReply("Unknown command.")
        if not text:
            return BotReply("Send a question.")
        return await self._conversation.answer_question(telegram_id, text)

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
            await self._send(chat_id, BotReply("Access denied."))
            return
        data = callback.get("data")
        if not isinstance(data, str) or ":" not in data:
            await self._send(chat_id, BotReply("Invalid action."))
            return
        action, value = data.split(":", 1)
        if action == "domain":
            reply = await self._conversation.select_domain(telegram_id, value)
        elif action == "confirm":
            reply = await self._conversation.confirm_write(telegram_id, value)
        elif action == "reject":
            reply = await self._conversation.reject_write(telegram_id, value)
        else:
            reply = BotReply("Invalid action.")
        await self._send(chat_id, reply)

    async def poll_once(self, offset: int | None) -> int | None:
        self._conversation.expire_state()
        arguments: dict[str, object] = {"timeout": 30}
        if offset is not None:
            arguments["offset"] = offset
        updates = await self._api("getUpdates", arguments)
        if not isinstance(updates, list):
            raise TelegramError("telegram_request_failed")
        next_offset = offset
        for update in updates:
            if not isinstance(update, dict):
                continue
            await self.handle_update(update)
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                next_offset = update_id + 1
        return next_offset

    async def poll_forever(
        self,
        *,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
        random_value: Callable[[], float] = random.random,
        heartbeat: Heartbeat,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        offset = None
        backoff = Backoff()
        while True:
            started = clock()
            try:
                next_offset = await self.poll_once(offset)
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
            offset = next_offset
            backoff.reset()
            heartbeat.touch()
