import pytest
from types import SimpleNamespace

from iwiki_mcp import admin
from iwiki_mcp.engine.config import Config, ConfigError


def test_chat_model_default_empty(monkeypatch):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.delenv("IWIKI_CHAT_MODEL", raising=False)
    assert Config.load().chat_model == ""


def test_chat_model_override(monkeypatch):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setenv("IWIKI_CHAT_MODEL", "my-model")
    assert Config.load().chat_model == "my-model"


def test_system1_shadow_defaults_disabled(monkeypatch):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.delenv("IWIKI_SYSTEM1_SHADOW", raising=False)
    monkeypatch.delenv("IWIKI_SYSTEM1_BASE_URL", raising=False)
    monkeypatch.delenv("IWIKI_SYSTEM1_KEY", raising=False)

    config = Config.load()

    assert config.system1_shadow is False
    assert config.system1_base_url == ""
    assert config.system1_api_key == ""


def test_system1_shadow_loads_separate_endpoint_and_key(monkeypatch):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x")
    monkeypatch.setenv("IWIKI_LLM_KEY", "llm-key")
    monkeypatch.setenv("IWIKI_SYSTEM1_SHADOW", "true")
    monkeypatch.setenv("IWIKI_SYSTEM1_BASE_URL", "http://system1/v1/")
    monkeypatch.setenv("IWIKI_SYSTEM1_KEY", "system1-key")

    config = Config.load()

    assert config.system1_shadow is True
    assert config.system1_base_url == "http://system1/v1"
    assert config.system1_api_key == "system1-key"


@pytest.mark.parametrize("missing", ["IWIKI_SYSTEM1_BASE_URL", "IWIKI_SYSTEM1_KEY"])
def test_system1_shadow_requires_separate_connection(monkeypatch, missing):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x")
    monkeypatch.setenv("IWIKI_LLM_KEY", "llm-key")
    monkeypatch.setenv("IWIKI_SYSTEM1_SHADOW", "true")
    monkeypatch.setenv("IWIKI_SYSTEM1_BASE_URL", "http://system1/v1")
    monkeypatch.setenv("IWIKI_SYSTEM1_KEY", "system1-key")
    monkeypatch.delenv(missing)

    with pytest.raises(ConfigError, match="IWIKI_SYSTEM1_BASE_URL and IWIKI_SYSTEM1_KEY"):
        Config.load()


def test_system1_shadow_requires_v1_api_root(monkeypatch):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x")
    monkeypatch.setenv("IWIKI_LLM_KEY", "llm-key")
    monkeypatch.setenv("IWIKI_SYSTEM1_SHADOW", "true")
    monkeypatch.setenv("IWIKI_SYSTEM1_BASE_URL", "http://system1")
    monkeypatch.setenv("IWIKI_SYSTEM1_KEY", "system1-key")

    with pytest.raises(ConfigError, match="IWIKI_SYSTEM1_BASE_URL must end in /v1"):
        Config.load()


def test_hosted_engine_config_carries_system1_shadow_connection():
    server_config = SimpleNamespace(
        models=SimpleNamespace(
            embed_model="embed",
            embed_dimensions=3,
            rerank_model="",
        )
    )
    environ = {
        "IWIKI_LLM_BASE_URL": "http://llm",
        "IWIKI_LLM_KEY": "llm-key",
        "IWIKI_SYSTEM1_SHADOW": "true",
        "IWIKI_SYSTEM1_BASE_URL": "http://system1/v1/",
        "IWIKI_SYSTEM1_KEY": "system1-key",
    }

    config = admin._engine_config(server_config, environ)

    assert config.system1_shadow is True
    assert config.system1_base_url == "http://system1/v1"
    assert config.system1_api_key == "system1-key"


def test_hosted_engine_config_requires_v1_api_root():
    server_config = SimpleNamespace(
        models=SimpleNamespace(
            embed_model="embed",
            embed_dimensions=3,
            rerank_model="",
        )
    )
    environ = {
        "IWIKI_LLM_BASE_URL": "http://llm",
        "IWIKI_LLM_KEY": "llm-key",
        "IWIKI_SYSTEM1_SHADOW": "true",
        "IWIKI_SYSTEM1_BASE_URL": "http://system1",
        "IWIKI_SYSTEM1_KEY": "system1-key",
    }

    with pytest.raises(admin.ConfigError, match="IWIKI_SYSTEM1_BASE_URL must end in /v1"):
        admin._engine_config(server_config, environ)
