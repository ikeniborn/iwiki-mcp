import math

import httpx
import pytest

from iwiki_mcp.engine.config import Config


def _cfg():
    return Config(
        base_url="https://litellm.test/v1",
        api_key="secret",
        embed_model="embed",
        dimensions=2,
        chunk_size=512,
        chunk_overlap=64,
        summary_max=400,
        top_k=8,
        score_threshold=0.2,
        graph_depth=2,
        ignore=None,
        rerank_model="rerank-model",
    )


def _candidates():
    return [
        {
            "domain": "docs",
            "file": "a.md",
            "heading": "Alpha",
            "chunk": 0,
            "score": 0.2,
            "hit": "semantic",
            "source": "global",
            "text": "alpha",
        },
        {
            "domain": "docs",
            "file": "b.md",
            "heading": "Beta",
            "chunk": 1,
            "score": 0.1,
            "hit": "lexical",
            "source": "seed",
            "text": "beta",
        },
    ]


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def _public(candidates):
    return [{key: value for key, value in item.items() if key != "text"}
            for item in candidates]


def test_rerank_calls_litellm_once_and_reorders_candidates(monkeypatch):
    from iwiki_mcp.engine.rerank import rerank_candidates

    calls = []

    def post(*args, **kwargs):
        calls.append((args, kwargs))
        return _Response({"results": [
            {"index": 0, "relevance_score": 0.3},
            {"index": 1, "relevance_score": 0.9},
        ]})

    monkeypatch.setattr(httpx, "post", post)

    ranked, metadata = rerank_candidates(_cfg(), "question", _candidates())

    assert calls == [(('https://litellm.test/v1/rerank',), {
        "json": {
            "model": "rerank-model",
            "query": "question",
            "documents": ["alpha", "beta"],
            "top_n": 2,
        },
        "headers": {"Authorization": "Bearer secret"},
        "timeout": 60.0,
    })]
    assert ranked == [
        {
            "domain": "docs", "file": "b.md", "heading": "Beta", "chunk": 1,
            "score": 0.9, "hit": "lexical", "source": "seed",
        },
        {
            "domain": "docs", "file": "a.md", "heading": "Alpha", "chunk": 0,
            "score": 0.3, "hit": "semantic", "source": "global",
        },
    ]
    assert metadata == {"applied": True}


@pytest.mark.parametrize("payload", [
    {},
    {"results": "invalid"},
    {"results": [{"index": True, "relevance_score": 0.8}]},
    {"results": [{"index": 2, "relevance_score": 0.8}]},
    {"results": [{"index": 0, "relevance_score": True}]},
    {"results": [{"index": 0, "relevance_score": math.nan}]},
    {"results": [
        {"index": 0, "relevance_score": 0.8},
        {"index": 0, "relevance_score": 0.7},
    ]},
    {"results": [
        {"index": 0, "relevance_score": 0.8},
        {"index": 0, "relevance_score": True},
    ]},
])
def test_invalid_provider_payload_falls_back_to_preliminary_order(monkeypatch, payload):
    from iwiki_mcp.engine.rerank import rerank_candidates

    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: _Response(payload))

    ranked, metadata = rerank_candidates(_cfg(), "question", _candidates())

    assert ranked == _public(_candidates())
    assert metadata == {"applied": False, "warning": "reranker unavailable"}


@pytest.mark.parametrize("error", [
    httpx.TimeoutException("secret timeout", request=httpx.Request("POST", "https://x")),
    httpx.ConnectError("secret connect", request=httpx.Request("POST", "https://x")),
])
def test_transport_error_falls_back_without_leaking_details(monkeypatch, error):
    from iwiki_mcp.engine.rerank import rerank_candidates

    def post(*args, **kwargs):
        raise error

    monkeypatch.setattr(httpx, "post", post)

    result = rerank_candidates(_cfg(), "provider body", _candidates())

    assert result == (
        _public(_candidates()),
        {"applied": False, "warning": "reranker unavailable"},
    )
    assert "secret" not in repr(result)
    assert "provider body" not in repr(result)
    assert "litellm" not in repr(result)
    assert "rerank-model" not in repr(result)


def test_http_500_falls_back_without_leaking_provider_body(monkeypatch):
    from iwiki_mcp.engine.rerank import rerank_candidates

    response = httpx.Response(
        500,
        text="secret provider body",
        request=httpx.Request("POST", "https://litellm.test/v1/rerank"),
    )
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: response)

    result = rerank_candidates(_cfg(), "question", _candidates())

    assert result == (
        _public(_candidates()),
        {"applied": False, "warning": "reranker unavailable"},
    )
    assert "secret" not in repr(result)
    assert "provider body" not in repr(result)


def test_json_value_error_falls_back_without_leaking_details(monkeypatch):
    from iwiki_mcp.engine.rerank import rerank_candidates

    class InvalidJsonResponse(_Response):
        def json(self):
            raise ValueError("secret invalid response")

    monkeypatch.setattr(
        httpx, "post", lambda *args, **kwargs: InvalidJsonResponse(None)
    )

    result = rerank_candidates(_cfg(), "question", _candidates())

    assert result == (
        _public(_candidates()),
        {"applied": False, "warning": "reranker unavailable"},
    )
    assert "secret" not in repr(result)


def test_partial_response_ranks_scored_then_keeps_unscored_relative_order(monkeypatch):
    from iwiki_mcp.engine.rerank import rerank_candidates

    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: _Response({
            "results": [{"index": 1, "relevance_score": 0.7}]
        }),
    )

    ranked, metadata = rerank_candidates(_cfg(), "question", _candidates())

    assert [item["file"] for item in ranked] == ["b.md", "a.md"]
    assert [item["score"] for item in ranked] == [0.7, 0.2]
    assert all("text" not in item for item in ranked)
    assert metadata == {"applied": True}


def test_empty_candidates_fail_soft_without_network(monkeypatch):
    from iwiki_mcp.engine.rerank import rerank_candidates

    def unexpected_post(*args, **kwargs):
        pytest.fail("empty candidates must not call the provider")

    monkeypatch.setattr(httpx, "post", unexpected_post)

    assert rerank_candidates(_cfg(), "question", []) == (
        [],
        {"applied": False, "warning": "reranker unavailable"},
    )
