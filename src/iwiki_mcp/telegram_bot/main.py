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


class DependencyCleanupError(RuntimeError):
    """A sanitized dependency cleanup failure."""


def _retryable_startup_error(error: Exception) -> bool:
    return bool(error.retryable)


async def _close_dependencies(
    remote_context,
    remote_entered: bool,
    inference,
    telegram_http,
    exc_info,
) -> None:
    failures = []
    with anyio.CancelScope(shield=True):
        if remote_entered:
            try:
                await remote_context.__aexit__(*exc_info)
            except BaseException as error:
                failures.append(error)
        if inference is not None:
            try:
                await inference.close()
            except BaseException as error:
                failures.append(error)
        if telegram_http is not None:
            try:
                await telegram_http.close()
            except BaseException as error:
                failures.append(error)
    if not failures or exc_info[0] is not None:
        return
    cancelled_class = anyio.get_cancelled_exc_class()
    for failure in failures:
        if isinstance(failure, (cancelled_class, KeyboardInterrupt, SystemExit)):
            raise failure
    raise DependencyCleanupError("dependency_cleanup_failed") from None


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
        except BaseException:
            startup_exc_info = sys.exc_info()
            raise
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
