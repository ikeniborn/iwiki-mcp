from iwiki_mcp.engine.config import Config


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
