import httpx
import pytest
from iwiki_mcp.engine import embed as embed_mod
from iwiki_mcp.engine.embed import embed_texts, probe_embedding_endpoint, EmbedError
from iwiki_mcp.engine.config import Config


def _cfg(dimensions=0):
    return Config(base_url="http://x", api_key="k", embed_model="m", dimensions=dimensions,
                  chunk_size=512, chunk_overlap=64, summary_max=400, top_k=8,
                  score_threshold=0.2, graph_depth=2, ignore=None)


def _probe_cfg():
    return _cfg(dimensions=2)


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return {"data": self._data}


def test_retries_transient_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom")
        return _Resp([{"index": 0, "embedding": [0.1, 0.2]}])

    monkeypatch.setattr(embed_mod.httpx, "post", fake_post)
    monkeypatch.setattr(embed_mod.time, "sleep", lambda s: None)
    assert embed_texts(_cfg(), ["hello"]) == [[0.1, 0.2]]
    assert calls["n"] == 3


def test_gives_up_after_max_attempts(monkeypatch):
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        raise httpx.ConnectError("down")

    monkeypatch.setattr(embed_mod.httpx, "post", fake_post)
    monkeypatch.setattr(embed_mod.time, "sleep", lambda s: None)
    with pytest.raises(EmbedError):
        embed_texts(_cfg(), ["hello"])
    assert calls["n"] == 3


def test_4xx_not_retried(monkeypatch):
    calls = {"n": 0}
    req = httpx.Request("POST", "http://x/embeddings")

    def fake_post(*a, **k):
        calls["n"] += 1
        resp = httpx.Response(400, request=req)
        raise httpx.HTTPStatusError("bad", request=req, response=resp)

    monkeypatch.setattr(embed_mod.httpx, "post", fake_post)
    monkeypatch.setattr(embed_mod.time, "sleep", lambda s: None)
    with pytest.raises(EmbedError):
        embed_texts(_cfg(), ["hello"])
    assert calls["n"] == 1


def test_probe_sends_exact_request_once(monkeypatch):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return _Resp([{"index": 0, "embedding": [0.1, 2]}])

    monkeypatch.setattr(embed_mod.httpx, "post", fake_post)

    assert probe_embedding_endpoint(_probe_cfg()) is None
    assert calls == [
        (("http://x/embeddings",), {
            "json": {
                "model": "m",
                "input": ["iwiki startup probe"],
                "dimensions": 2,
            },
            "headers": {"Authorization": "Bearer k"},
            "timeout": 10.0,
        })
    ]


@pytest.mark.parametrize(
    ("failure", "match"),
    [
        (httpx.ReadTimeout("slow"), "timed out"),
        (httpx.ConnectError("down"), "transport error"),
    ],
)
def test_probe_wraps_transport_failures_without_retry(monkeypatch, failure, match):
    calls = {"n": 0}

    def fake_post(*args, **kwargs):
        calls["n"] += 1
        raise failure

    monkeypatch.setattr(embed_mod.httpx, "post", fake_post)

    with pytest.raises(EmbedError, match=match):
        probe_embedding_endpoint(_probe_cfg())
    assert calls["n"] == 1


def test_probe_reports_http_status_and_reason_without_response_body(monkeypatch):
    calls = {"n": 0}
    request = httpx.Request("POST", "http://x/embeddings")
    response = httpx.Response(
        401,
        request=request,
        content=b'response-body-secret',
    )

    def fake_post(*args, **kwargs):
        calls["n"] += 1
        return response

    monkeypatch.setattr(embed_mod.httpx, "post", fake_post)

    with pytest.raises(EmbedError) as exc_info:
        probe_embedding_endpoint(_probe_cfg())
    message = str(exc_info.value)
    assert "401" in message
    assert "Unauthorized" in message
    assert "response-body-secret" not in message
    assert calls["n"] == 1


class _MalformedJsonResp:
    def raise_for_status(self):
        pass

    def json(self):
        raise ValueError("response-body-secret")


def test_probe_rejects_malformed_json_without_leaking_details(monkeypatch):
    calls = {"n": 0}

    def fake_post(*args, **kwargs):
        calls["n"] += 1
        return _MalformedJsonResp()

    monkeypatch.setattr(embed_mod.httpx, "post", fake_post)

    with pytest.raises(EmbedError, match="malformed JSON") as exc_info:
        probe_embedding_endpoint(_probe_cfg())
    assert "response-body-secret" not in str(exc_info.value)
    assert calls["n"] == 1


@pytest.mark.parametrize("data", [[], [{"embedding": [0.1, 0.2]}] * 2])
def test_probe_requires_exactly_one_data_row(monkeypatch, data):
    calls = {"n": 0}

    def fake_post(*args, **kwargs):
        calls["n"] += 1
        return _Resp(data)

    monkeypatch.setattr(embed_mod.httpx, "post", fake_post)

    with pytest.raises(EmbedError, match="exactly one data row"):
        probe_embedding_endpoint(_probe_cfg())
    assert calls["n"] == 1


def test_probe_requires_embedding_vector(monkeypatch):
    calls = {"n": 0}

    def fake_post(*args, **kwargs):
        calls["n"] += 1
        return _Resp([{"index": 0}])

    monkeypatch.setattr(embed_mod.httpx, "post", fake_post)

    with pytest.raises(EmbedError, match="missing embedding vector"):
        probe_embedding_endpoint(_probe_cfg())
    assert calls["n"] == 1


@pytest.mark.parametrize(
    "embedding",
    [[], [0.1, True], [0.1, "0.2"], [0.1, float("nan")], [0.1, float("inf")]],
)
def test_probe_rejects_empty_or_invalid_embedding_vector(monkeypatch, embedding):
    calls = {"n": 0}

    def fake_post(*args, **kwargs):
        calls["n"] += 1
        return _Resp([{"index": 0, "embedding": embedding}])

    monkeypatch.setattr(embed_mod.httpx, "post", fake_post)

    with pytest.raises(EmbedError, match="invalid embedding vector"):
        probe_embedding_endpoint(_probe_cfg())
    assert calls["n"] == 1


def test_probe_rejects_dimension_mismatch(monkeypatch):
    calls = {"n": 0}

    def fake_post(*args, **kwargs):
        calls["n"] += 1
        return _Resp([{"index": 0, "embedding": [0.1]}])

    monkeypatch.setattr(embed_mod.httpx, "post", fake_post)

    with pytest.raises(EmbedError, match="dimension mismatch"):
        probe_embedding_endpoint(_probe_cfg())
    assert calls["n"] == 1
