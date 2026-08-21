"""Environment-only configuration for the Telegram bot service."""

import os
from dataclasses import dataclass


class BotConfigError(RuntimeError):
    """The service configuration is incomplete or invalid."""


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
        names = (
            "IWIKI_BOT_TELEGRAM_TOKEN",
            "IWIKI_BOT_IWIKI_URL",
            "IWIKI_BOT_IWIKI_TOKEN",
            "IWIKI_BOT_ALLOWED_TELEGRAM_IDS",
            "IWIKI_BOT_LLM_BASE_URL",
            "IWIKI_BOT_LLM_KEY",
            "IWIKI_BOT_LLM_MODEL",
            "IWIKI_BOT_TRANSCRIPTION_MODEL",
        )
        required = {name: os.environ.get(name, "").strip() for name in names}
        missing = [name for name in names if not required[name]]
        if missing:
            raise BotConfigError(f"missing configuration: {', '.join(missing)}")

        try:
            allowed = frozenset(
                int(value.strip())
                for value in required["IWIKI_BOT_ALLOWED_TELEGRAM_IDS"].split(",")
            )
        except ValueError as exc:
            raise BotConfigError("invalid IWIKI_BOT_ALLOWED_TELEGRAM_IDS") from exc

        ttl_name = "IWIKI_BOT_CONFIRMATION_TTL_SECONDS"
        try:
            ttl = int(os.environ.get(ttl_name, "300"))
        except ValueError as exc:
            raise BotConfigError(f"invalid {ttl_name}") from exc
        if not allowed:
            raise BotConfigError("invalid IWIKI_BOT_ALLOWED_TELEGRAM_IDS")
        if ttl <= 0:
            raise BotConfigError(f"invalid {ttl_name}")

        return cls(
            telegram_token=required["IWIKI_BOT_TELEGRAM_TOKEN"],
            iwiki_url=required["IWIKI_BOT_IWIKI_URL"],
            iwiki_token=required["IWIKI_BOT_IWIKI_TOKEN"],
            allowed_telegram_ids=allowed,
            llm_base_url=required["IWIKI_BOT_LLM_BASE_URL"].rstrip("/"),
            llm_key=required["IWIKI_BOT_LLM_KEY"],
            llm_model=required["IWIKI_BOT_LLM_MODEL"],
            transcription_model=required["IWIKI_BOT_TRANSCRIPTION_MODEL"],
            confirmation_ttl_seconds=ttl,
        )
