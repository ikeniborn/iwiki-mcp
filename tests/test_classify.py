import iwiki_mcp.engine.classify as classify
from iwiki_mcp.engine.config import Config


def _cfg():
    return Config(base_url="http://x", api_key="k", embed_model="e", chat_model="c",
                  dimensions=2, chunk_size=512, chunk_overlap=64, summary_max=400,
                  top_k=8, score_threshold=0.2, graph_depth=2, ignore=None)


def test_classify_parses_and_governs(monkeypatch):
    monkeypatch.setattr(classify, "_chat", lambda cfg, prompt: '{"type": "api", "tags": ["Config", "config"]}')
    out = classify.classify_page(_cfg(), "body", existing_tags=[])
    assert out["type"] == "api"
    assert out["tags"] == ["config"]          # normalized + deduped
    assert out["warning"] is None


def test_classify_offvocab_falls_back(monkeypatch):
    monkeypatch.setattr(classify, "_chat", lambda cfg, prompt: '{"type": "nonsense", "tags": []}')
    out = classify.classify_page(_cfg(), "body", existing_tags=[])
    assert out["type"] == "concept"


def test_classify_failure_is_best_effort(monkeypatch):
    def boom(cfg, prompt):
        raise RuntimeError("endpoint down")
    monkeypatch.setattr(classify, "_chat", boom)
    out = classify.classify_page(_cfg(), "body", existing_tags=[])
    assert out["type"] == "concept"
    assert out["tags"] == []
    assert "classification unavailable" in out["warning"]
