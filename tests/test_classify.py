import iwiki_mcp.engine.classify as classify
from iwiki_mcp.engine.config import Config


def _cfg():
    return Config(base_url="http://x", api_key="k", embed_model="e", chat_model="c",
                  dimensions=2, chunk_size=512, chunk_overlap=64, summary_max=400,
                  top_k=8, score_threshold=0.2, graph_depth=2, ignore=None)


def test_classify_parses_and_governs(monkeypatch):
    monkeypatch.setattr(
        classify, "_chat",
        lambda cfg, prompt: '{"type": "api", "tags": ["Config", "config"]}'
    )
    out = classify.classify_page(_cfg(), "body", existing_tags=[])
    assert out["type"] == "api"
    assert out["tags"] == ["config"]          # normalized + deduped
    assert out["warning"] is None


def test_classify_keeps_type_open(monkeypatch):
    # type is now an open vocabulary: an off-list classifier value is kept
    # (normalized), not clamped — advisory unknown_type flags it downstream.
    monkeypatch.setattr(classify, "_chat", lambda cfg, prompt: '{"type": "Person", "tags": []}')
    out = classify.classify_page(_cfg(), "body", existing_tags=[])
    assert out["type"] == "person"


def test_classify_prompt_and_output_never_select_specification(monkeypatch):
    prompts = []

    def answer(cfg, prompt):
        prompts.append(prompt)
        return '{"type": " Specification ", "tags": ["GWT"]}'

    monkeypatch.setattr(classify, "_chat", answer)
    out = classify.classify_page(
        _cfg(),
        "Given an iwiki-gwt fence, When classified, Then stay ordinary.",
        existing_tags=[],
    )

    assert "specification" not in prompts[0].split("type MUST", 1)[1].split("Pick by", 1)[0]
    assert out["type"] == "concept"
    assert out["tags"] == ["gwt"]
    assert out["warning"] is None


def test_classify_failure_is_best_effort(monkeypatch):
    def boom(cfg, prompt):
        raise RuntimeError("endpoint down")
    monkeypatch.setattr(classify, "_chat", boom)
    out = classify.classify_page(_cfg(), "body", existing_tags=[])
    assert out["type"] == "concept"
    assert out["tags"] == []
    assert "classification unavailable" in out["warning"]
