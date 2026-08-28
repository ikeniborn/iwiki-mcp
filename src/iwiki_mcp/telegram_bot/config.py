"""Environment-only configuration for the Telegram bot service."""

import base64
import os
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit


class BotConfigError(RuntimeError):
    """The service configuration is incomplete or invalid."""


@dataclass(frozen=True)
class TelegramProxyConfig:
    origin: str = field(repr=False)
    authorization: str | None = field(default=None, repr=False)


def _parse_telegram_proxy(value: str) -> TelegramProxyConfig:
    error = "invalid IWIKI_BOT_TELEGRAM_PROXY_URL"
    if not value.startswith("https://"):
        raise BotConfigError(error)

    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except (UnicodeError, ValueError):
        raise BotConfigError(error) from None

    if (
        parsed.scheme != "https"
        or hostname is None
        or port is None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise BotConfigError(error)

    authorization = None
    if parsed.username is not None:
        try:
            username = unquote(parsed.username)
            password = unquote(parsed.password or "")
            credentials = f"{username}:{password}".encode("utf-8")
        except UnicodeError:
            raise BotConfigError(error) from None
        authorization = "Basic " + base64.b64encode(credentials).decode("ascii")

    origin_host = f"[{hostname}]" if ":" in hostname else hostname
    return TelegramProxyConfig(
        origin=f"https://{origin_host}:{port}",
        authorization=authorization,
    )


@dataclass(frozen=True)
class BotConfig:
    telegram_token: str = field(repr=False)
    iwiki_url: str
    iwiki_token: str = field(repr=False)
    allowed_telegram_ids: frozenset[int]
    llm_base_url: str
    llm_key: str = field(repr=False)
    llm_model: str
    transcription_model: str
    confirmation_ttl_seconds: int
    telegram_proxy: TelegramProxyConfig = field(repr=False)

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
            "IWIKI_BOT_TELEGRAM_PROXY_URL",
        )
        required = {
            name: os.environ.get(name, "")
            if name == "IWIKI_BOT_TELEGRAM_PROXY_URL"
            else os.environ.get(name, "").strip()
            for name in names
        }
        missing = [name for name in names if not required[name]]
        if missing:
            raise BotConfigError(f"missing configuration: {', '.join(missing)}")

        telegram_proxy = _parse_telegram_proxy(
            required["IWIKI_BOT_TELEGRAM_PROXY_URL"]
        )

        try:
            allowed = frozenset(
                int(value.strip())
                for value in required["IWIKI_BOT_ALLOWED_TELEGRAM_IDS"].split(",")
            )
        except ValueError:
            raise BotConfigError("invalid IWIKI_BOT_ALLOWED_TELEGRAM_IDS") from None

        ttl_name = "IWIKI_BOT_CONFIRMATION_TTL_SECONDS"
        try:
            ttl = int(os.environ.get(ttl_name, "300"))
        except ValueError:
            raise BotConfigError(f"invalid {ttl_name}") from None
        if not allowed or any(telegram_id <= 0 for telegram_id in allowed):
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
            telegram_proxy=telegram_proxy,
        )
