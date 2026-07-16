import pytest
from iwiki_mcp.engine.config import Config, ConfigError


@pytest.fixture(autouse=True)
def clear_search_env(monkeypatch):
    monkeypatch.delenv("IWIKI_SEARCH_MODE", raising=False)
    monkeypatch.delenv("IWIKI_RERANK_MODEL", raising=False)


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


@pytest.mark.parametrize("value", ["hybrid", "lexical", "semantic"])
def test_search_mode_normalizes_canonical_values(monkeypatch, embedding_env, value):
    monkeypatch.setenv("IWIKI_SEARCH_MODE", f"  {value.upper()}  ")

    assert Config.load().search_mode == value


def test_search_mode_defaults_to_hybrid(monkeypatch, embedding_env):
    monkeypatch.delenv("IWIKI_SEARCH_MODE", raising=False)

    assert Config.load().search_mode == "hybrid"


@pytest.mark.parametrize("value", ["vector", "bogus", ""])
def test_search_mode_rejects_invalid_values(monkeypatch, embedding_env, value):
    monkeypatch.setenv("IWIKI_SEARCH_MODE", value)

    with pytest.raises(ConfigError) as exc_info:
        Config.load()
    assert str(exc_info.value) == (
        "IWIKI_SEARCH_MODE must be one of: hybrid, lexical, semantic."
    )


def test_rerank_model_defaults_to_empty_string(monkeypatch, embedding_env):
    monkeypatch.delenv("IWIKI_RERANK_MODEL", raising=False)

    assert Config.load().rerank_model == ""


def test_rerank_model_is_trimmed(monkeypatch, embedding_env):
    monkeypatch.setenv("IWIKI_RERANK_MODEL", "  cohere-rerank-v3.5  ")

    assert Config.load().rerank_model == "cohere-rerank-v3.5"
