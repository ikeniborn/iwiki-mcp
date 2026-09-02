"""Environment-only configuration for the Telegram bot service."""

import base64
import ipaddress
import os
from dataclasses import dataclass, field
from urllib.parse import unquote_to_bytes, urlsplit


_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})


class BotConfigError(RuntimeError):
    """The service configuration is incomplete or invalid."""


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip() or str(default)
    try:
        value = int(raw)
    except ValueError:
        raise BotConfigError(f"invalid {name}") from None
    if value <= 0:
        raise BotConfigError(f"invalid {name}")
    return value


def _log_level(name: str, default: str) -> str:
    value = (os.environ.get(name, "").strip() or default).upper()
    if value not in _LOG_LEVELS:
        raise BotConfigError(f"invalid {name}")
    return value


@dataclass(frozen=True)
class TelegramProxyConfig:
    origin: str = field(repr=False)
    authorization: str | None = field(default=None, repr=False)


def _numeric_proxy_label(label: str) -> bool:
    return label.isdecimal() or (
        len(label) > 2
        and label[:2].lower() == "0x"
        and all(character in "0123456789abcdefABCDEF" for character in label[2:])
    )


def _valid_proxy_hostname(hostname: str) -> bool:
    if "%" in hostname:
        return False
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        if not hostname.isascii() or len(hostname) > 253:
            return False
        labels = hostname.split(".")
        if all(_numeric_proxy_label(label) for label in labels):
            return False
        return all(
            0 < len(label) <= 63
            and not label.startswith("-")
            and not label.endswith("-")
            and all(character.isalnum() or character == "-" for character in label)
            for label in labels
        )
    return True


def _decode_proxy_userinfo(value: str) -> str:
    hexadecimal = frozenset("0123456789abcdefABCDEF")
    for index, character in enumerate(value):
        if character == "%" and (
            index + 2 >= len(value)
            or value[index + 1] not in hexadecimal
            or value[index + 2] not in hexadecimal
        ):
            raise BotConfigError("invalid IWIKI_BOT_TELEGRAM_PROXY_URL") from None
    try:
        return unquote_to_bytes(value).decode("utf-8", errors="strict")
    except UnicodeError:
        raise BotConfigError("invalid IWIKI_BOT_TELEGRAM_PROXY_URL") from None


def _parse_telegram_proxy(value: str) -> TelegramProxyConfig:
    error = "invalid IWIKI_BOT_TELEGRAM_PROXY_URL"
    if not value.startswith("https://"):
        raise BotConfigError(error)
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    ):
        raise BotConfigError(error) from None
    if value.count("@") > 1:
        raise BotConfigError(error) from None

    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except (UnicodeError, ValueError):
        raise BotConfigError(error) from None

    if (
        parsed.scheme != "https"
        or hostname is None
        or not _valid_proxy_hostname(hostname)
        or port is None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise BotConfigError(error)

    authorization = None
    if "@" in parsed.netloc:
        encoded_userinfo = parsed.netloc.split("@", 1)[0]
        if ":" not in encoded_userinfo:
            raise BotConfigError(error) from None
        encoded_username, encoded_password = encoded_userinfo.split(":", 1)
        if not encoded_username:
            raise BotConfigError(error) from None
        username = _decode_proxy_userinfo(encoded_username)
        password = _decode_proxy_userinfo(encoded_password)
        if ":" in username:
            raise BotConfigError(error) from None
        credentials = f"{username}:{password}".encode("utf-8")
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
    context_budget_chars: int = 48000
    context_window_tokens: int = 32768
    max_output_tokens: int = 1024
    inference_timeout_seconds: int = 180
    log_level: str = "INFO"

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
            context_budget_chars=_positive_int(
                "IWIKI_BOT_CONTEXT_BUDGET_CHARS", 48000
            ),
            context_window_tokens=_positive_int(
                "IWIKI_BOT_CONTEXT_WINDOW_TOKENS", 32768
            ),
            max_output_tokens=_positive_int("IWIKI_BOT_MAX_OUTPUT_TOKENS", 1024),
            inference_timeout_seconds=_positive_int(
                "IWIKI_BOT_INFERENCE_TIMEOUT_SECONDS", 180
            ),
            log_level=_log_level("IWIKI_BOT_LOG_LEVEL", "INFO"),
        )
