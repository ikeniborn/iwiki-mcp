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


def test_config_rejects_non_positive_confirmation_ttl(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setenv("IWIKI_BOT_CONFIRMATION_TTL_SECONDS", "0")

    with pytest.raises(BotConfigError, match="IWIKI_BOT_CONFIRMATION_TTL_SECONDS"):
        BotConfig.load()


def test_allowlist_denies_unknown_id():
    policy = AccessPolicy(frozenset({1001}))

    assert policy.allows(1001) is True
    assert policy.allows(2002) is False
