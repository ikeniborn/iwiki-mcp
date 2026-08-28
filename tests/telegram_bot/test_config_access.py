import traceback

import pytest

from iwiki_mcp.telegram_bot.access import AccessPolicy
from iwiki_mcp.telegram_bot.config import BotConfig, BotConfigError


REQUIRED_ENV = {
    "IWIKI_BOT_TELEGRAM_TOKEN": "telegram-token",
    "IWIKI_BOT_IWIKI_URL": "https://wiki.example/mcp",
    "IWIKI_BOT_IWIKI_TOKEN": "iwiki-token",
    "IWIKI_BOT_ALLOWED_TELEGRAM_IDS": "1001, 2002",
    "IWIKI_BOT_LLM_BASE_URL": "https://models.example/v1/",
    "IWIKI_BOT_LLM_KEY": "llm-key",
    "IWIKI_BOT_LLM_MODEL": "chat-model",
    "IWIKI_BOT_TRANSCRIPTION_MODEL": "audio-model",
    "IWIKI_BOT_TELEGRAM_PROXY_URL": (
        "https://proxy-user:proxy-password@proxy.example:8443"
    ),
}


def configure(monkeypatch):
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)


def test_config_rejects_missing_inference_key(monkeypatch):
    configure(monkeypatch)
    monkeypatch.delenv("IWIKI_BOT_LLM_KEY")

    with pytest.raises(BotConfigError, match="IWIKI_BOT_LLM_KEY"):
        BotConfig.load()


def test_config_loads_allowlist_and_defaults(monkeypatch):
    configure(monkeypatch)

    config = BotConfig.load()

    assert config.allowed_telegram_ids == frozenset({1001, 2002})
    assert config.llm_base_url == "https://models.example/v1"
    assert config.confirmation_ttl_seconds == 300


@pytest.mark.parametrize(
    ("value", "origin", "authorization"),
    (
        ("https://proxy.example:8443", "https://proxy.example:8443", None),
        ("https://proxy.example:8443/", "https://proxy.example:8443", None),
        (
            "https://[2001:db8::1]:8443",
            "https://[2001:db8::1]:8443",
            None,
        ),
        (
            "https://user%40team:p%3Ass@proxy.example:9443",
            "https://proxy.example:9443",
            "Basic dXNlckB0ZWFtOnA6c3M=",
        ),
    ),
)
def test_config_accepts_strict_https_proxy(
    monkeypatch, value, origin, authorization
):
    configure(monkeypatch)
    monkeypatch.setenv("IWIKI_BOT_TELEGRAM_PROXY_URL", value)

    config = BotConfig.load()

    assert config.telegram_proxy.origin == origin
    assert config.telegram_proxy.authorization == authorization


@pytest.mark.parametrize(
    "value",
    (
        "http://proxy.example:8443",
        "HTTPS://proxy.example:8443",
        "socks5://proxy.example:1080",
        "https://proxy.example",
        "https://proxy.example:not-a-port",
        "https://proxy.example:65536",
        "https://:8443",
        " https://proxy.example:8443",
        "https://[2001:db8::1:8443",
        "https://proxy.example:8443/path",
        "https://proxy.example:8443?mode=tunnel",
        "https://proxy.example:8443#credentials",
    ),
)
def test_config_rejects_invalid_telegram_proxy_without_echoing_value(
    monkeypatch, value
):
    configure(monkeypatch)
    monkeypatch.setenv("IWIKI_BOT_TELEGRAM_PROXY_URL", value)

    with pytest.raises(BotConfigError) as exc_info:
        BotConfig.load()

    assert str(exc_info.value) == "invalid IWIKI_BOT_TELEGRAM_PROXY_URL"
    assert value not in repr(exc_info.value)


def test_config_repr_redacts_credentials(monkeypatch):
    configure(monkeypatch)

    representation = repr(BotConfig.load())

    assert "telegram-token" not in representation
    assert "iwiki-token" not in representation
    assert "llm-key" not in representation
    assert "proxy-password" not in representation


def test_config_rejects_non_positive_confirmation_ttl(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setenv("IWIKI_BOT_CONFIRMATION_TTL_SECONDS", "0")

    with pytest.raises(BotConfigError, match="IWIKI_BOT_CONFIRMATION_TTL_SECONDS"):
        BotConfig.load()


@pytest.mark.parametrize(
    ("name", "marker", "error_name"),
    (
        (
            "IWIKI_BOT_ALLOWED_TELEGRAM_IDS",
            "allowlist-secret-marker",
            "IWIKI_BOT_ALLOWED_TELEGRAM_IDS",
        ),
        (
            "IWIKI_BOT_CONFIRMATION_TTL_SECONDS",
            "ttl-secret-marker",
            "IWIKI_BOT_CONFIRMATION_TTL_SECONDS",
        ),
    ),
)
def test_config_parse_errors_do_not_leak_values_in_tracebacks(
    monkeypatch, name, marker, error_name
):
    configure(monkeypatch)
    monkeypatch.setenv(name, marker)

    with pytest.raises(BotConfigError) as exc_info:
        BotConfig.load()

    formatted = "".join(
        traceback.format_exception(exc_info.type, exc_info.value, exc_info.tb)
    )
    assert str(exc_info.value) == f"invalid {error_name}"
    assert marker not in formatted


def test_config_rejects_non_positive_telegram_id(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setenv("IWIKI_BOT_ALLOWED_TELEGRAM_IDS", "-1")

    with pytest.raises(BotConfigError, match="IWIKI_BOT_ALLOWED_TELEGRAM_IDS"):
        BotConfig.load()


def test_allowlist_denies_unknown_id():
    policy = AccessPolicy(frozenset({1001}))

    assert policy.allows(1001) is True
    assert policy.allows(2002) is False
