"""Composition root for the separately deployed Telegram bot process."""

import argparse

import anyio

from .access import AccessPolicy
from .config import BotConfig
from .conversation import ConversationService
from .inference import InferenceClient
from .iwiki import open_remote_iwiki
from .transport import TelegramTransport


async def run_bot(config: BotConfig) -> None:
    access = AccessPolicy(config.allowed_telegram_ids)
    inference = InferenceClient(
        config.llm_base_url,
        config.llm_key,
        config.llm_model,
        config.transcription_model,
    )
    try:
        await inference.probe()
        async with open_remote_iwiki(config.iwiki_url, config.iwiki_token) as remote:
            await remote.list_domains()
            conversation = ConversationService(
                access,
                remote,
                inference,
                confirmation_ttl_seconds=config.confirmation_ttl_seconds,
            )
            transport = TelegramTransport(
                config.telegram_token, access, conversation
            )
            try:
                await transport.poll_forever()
            finally:
                await transport.close()
    finally:
        await inference.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Telegram client for remote iwiki"
    )
    parser.parse_args()
    config = BotConfig.load()
    anyio.run(run_bot, config)
