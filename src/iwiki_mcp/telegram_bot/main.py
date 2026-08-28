"""Composition root for the separately deployed Telegram bot process."""

import argparse
from collections.abc import Awaitable, Callable
import logging
from pathlib import Path
import random
import sys
import time

import anyio

from .access import AccessPolicy
from .config import BotConfig
from .conversation import ConversationService
from .inference import InferenceClient, InferenceError
from .iwiki import RemoteIwikiError, open_remote_iwiki
from .proxy import build_proxy_client
from .runtime import Backoff, Heartbeat
from .transport import TelegramTransport


LOGGER = logging.getLogger(__name__)
_NON_RETRYABLE_INFERENCE_ERRORS = frozenset({"configured_model_unavailable"})
_NON_RETRYABLE_REMOTE_ERRORS = frozenset(
    {"no_remote_domains", "unauthorized", "forbidden"}
)


def _retryable_startup_error(error: Exception) -> bool:
    if isinstance(error, InferenceError):
        return str(error) not in _NON_RETRYABLE_INFERENCE_ERRORS
    return str(error) not in _NON_RETRYABLE_REMOTE_ERRORS


async def _close_dependencies(
    remote_context,
    remote_entered: bool,
    inference,
    telegram_http,
    exc_info,
) -> None:
    with anyio.CancelScope(shield=True):
        try:
            if remote_entered:
                await remote_context.__aexit__(*exc_info)
        finally:
            try:
                if inference is not None:
                    await inference.close()
            finally:
                if telegram_http is not None:
                    await telegram_http.close()


async def run_bot(
    config: BotConfig,
    *,
    sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    random_value: Callable[[], float] = random.random,
    heartbeat: Heartbeat | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    access = AccessPolicy(config.allowed_telegram_ids)
    heartbeat = heartbeat or Heartbeat(
        Path("/run/iwiki-telegram-bot.heartbeat"), time.monotonic
    )
    backoff = Backoff()
    while True:
        started = clock()
        telegram_http = None
        inference = None
        remote_context = None
        remote_entered = False
        startup_ready = False
        retry_delay = None
        startup_exc_info = (None, None, None)
        try:
            telegram_http = build_proxy_client(config.telegram_proxy)
            inference = InferenceClient(
                config.llm_base_url,
                config.llm_key,
                config.llm_model,
                config.transcription_model,
            )
            await inference.probe()
            remote_context = open_remote_iwiki(
                config.iwiki_url, config.iwiki_token
            )
            remote = await remote_context.__aenter__()
            remote_entered = True
            await remote.list_domains()
            startup_ready = True
        except (InferenceError, RemoteIwikiError) as error:
            startup_exc_info = sys.exc_info()
            if not _retryable_startup_error(error):
                raise
            retry_delay = backoff.next_delay(random_value())
            LOGGER.warning(
                "telegram bot startup retry",
                extra={
                    "operation": "startup",
                    "outcome": "retry",
                    "delay_seconds": float(retry_delay),
                    "elapsed_ms": int((clock() - started) * 1000),
                },
            )
        finally:
            if not startup_ready:
                await _close_dependencies(
                    remote_context,
                    remote_entered,
                    inference,
                    telegram_http,
                    startup_exc_info,
                )

        if retry_delay is not None:
            await sleep(retry_delay)
            continue

        backoff.reset()
        try:
            conversation = ConversationService(
                access,
                remote,
                inference,
                confirmation_ttl_seconds=config.confirmation_ttl_seconds,
            )
            transport = TelegramTransport(
                config.telegram_token,
                access,
                conversation,
                telegram_http,
            )
            await transport.poll_forever(heartbeat=heartbeat)
        finally:
            await _close_dependencies(
                remote_context,
                remote_entered,
                inference,
                telegram_http,
                sys.exc_info(),
            )
        return


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Telegram client for remote iwiki"
    )
    parser.parse_args()
    config = BotConfig.load()
    anyio.run(run_bot, config)
