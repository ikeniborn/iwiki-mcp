from iwiki_mcp.engine.config import Config


def test_write_seed_threshold_default_and_env(monkeypatch):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.delenv("IWIKI_WRITE_SEED_THRESHOLD", raising=False)
    assert Config.load().write_seed_threshold == 0.35
    monkeypatch.setenv("IWIKI_WRITE_SEED_THRESHOLD", "0.5")
    assert Config.load().write_seed_threshold == 0.5
