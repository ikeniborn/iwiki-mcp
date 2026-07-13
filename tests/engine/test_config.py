import pytest
from iwiki_mcp.engine.config import Config, ConfigError


@pytest.fixture
def embedding_env(monkeypatch):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "test-secret")
    monkeypatch.setenv("IWIKI_EMBED_MODEL", "text-embedding-test")
    monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", "3")


def test_missing_config_names_env_vars(monkeypatch):
    monkeypatch.delenv("IWIKI_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("IWIKI_LLM_KEY", raising=False)
    with pytest.raises(ConfigError) as ei:
        Config.load()
    msg = str(ei.value)
    assert "IWIKI_LLM_BASE_URL" in msg
    assert "IWIKI_LLM_KEY" in msg
    assert "environment variable" in msg.lower()


def test_summary_max_default_and_override(monkeypatch):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "https://x/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.delenv("IWIKI_SUMMARY_MAX_CHARS", raising=False)
    assert Config.load().summary_max == 400
    monkeypatch.setenv("IWIKI_SUMMARY_MAX_CHARS", "250")
    assert Config.load().summary_max == 250


def test_blank_embed_model_names_env_var(monkeypatch, embedding_env):
    monkeypatch.setenv("IWIKI_EMBED_MODEL", "   ")

    with pytest.raises(ConfigError, match="IWIKI_EMBED_MODEL"):
        Config.load()


@pytest.mark.parametrize("value", ["abc", "0", "-1"])
def test_invalid_embed_dimensions_names_env_var(monkeypatch, embedding_env, value):
    monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", value)

    with pytest.raises(ConfigError, match="IWIKI_EMBED_DIMENSIONS"):
        Config.load()
