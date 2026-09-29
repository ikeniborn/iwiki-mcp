import logging

import pytest

import iwiki_mcp.telegram_bot.main as main_module
from iwiki_mcp.telegram_bot.config import BotConfig, BotConfigError


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), ("TRUE", True), (" true ", True), ("false", False), ("False", False)],
)
def test_bot_enabled_accepts_exact_booleans(value, expected):
    assert main_module.bot_enabled({"IWIKI_BOT_ENABLED": value}) is expected


def test_bot_enabled_defaults_to_true():
    assert main_module.bot_enabled({}) is True


@pytest.mark.parametrize("value", ["", "0", "1", "yes", "no", "off"])
def test_bot_enabled_rejects_other_values(value):
    with pytest.raises(BotConfigError, match="invalid IWIKI_BOT_ENABLED"):
        main_module.bot_enabled({"IWIKI_BOT_ENABLED": value})


def test_main_disabled_idles_without_loading_config(monkeypatch, caplog):
    monkeypatch.setenv("IWIKI_BOT_ENABLED", "false")
    monkeypatch.setattr("sys.argv", ["iwiki-telegram-bot"])
    monkeypatch.setattr(
        BotConfig, "load", classmethod(lambda cls: pytest.fail("config must not load"))
    )
    monkeypatch.setattr(
        main_module.anyio, "run", lambda *args, **kwargs: pytest.fail("bot must not run")
    )
    waited = []
    monkeypatch.setattr(main_module, "_wait_forever", lambda: waited.append(True))

    with caplog.at_level(logging.INFO, logger=main_module.LOGGER.name):
        main_module.main()

    assert waited == [True]
    assert "telegram bot disabled" in caplog.text
