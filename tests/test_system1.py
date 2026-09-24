from dataclasses import replace
import importlib

import httpx
import pytest

from iwiki_mcp.engine.config import Config
from iwiki_mcp.engine import frontmatter as fm


def _system1_module():
    try:
        return importlib.import_module("iwiki_mcp.engine.system1")
    except ModuleNotFoundError:
        pytest.fail("iwiki_mcp.engine.system1 is missing")


def _config(monkeypatch, **changes):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://llm")
    monkeypatch.setenv("IWIKI_LLM_KEY", "llm-key")
    return replace(Config.load(), **changes)


def test_disabled_shadow_makes_no_http_call(monkeypatch):
    system1 = _system1_module()
    cfg = _config(monkeypatch, system1_shadow=False)

    def unexpected_post(*args, **kwargs):
        raise AssertionError("disabled shadow must not call HTTP")

    monkeypatch.setattr(system1.httpx, "post", unexpected_post)

    assert system1.classify_page_type(cfg, "# Page") is None


def test_shadow_sends_native_choice_request_and_returns_probabilities(monkeypatch):
    system1 = _system1_module()
    cfg = _config(
        monkeypatch,
        system1_shadow=True,
        system1_base_url="http://system1",
        system1_api_key="system1-key",
    )
    captured = {}
    probabilities = {
        page_type: (0.75 if page_type == "guide" else 0.05)
        for page_type in fm.CLASSIFIABLE_TYPES
    }

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "model": "MultilingualSystem1",
                "answers": {
                    "page_type": {
                        "type": "choice",
                        "choice": "guide",
                        "probabilities": probabilities,
                    }
                },
            }

    def post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(system1.httpx, "post", post)

    decision = system1.classify_page_type(cfg, "# Page\n\n## Steps\nDo this.")

    assert decision is not None
    assert decision.status == "ok"
    assert decision.page_type == "guide"
    assert decision.probabilities == probabilities
    assert decision.model == "MultilingualSystem1"
    assert decision.latency_ms >= 0
    assert captured["url"] == "http://system1/v1/systemone"
    assert captured["headers"] == {"Authorization": "Bearer system1-key"}
    assert captured["json"]["state"] == {
        "document": "# Page\n\n## Steps\nDo this."
    }
    question = captured["json"]["questions"]["page_type"]
    assert question["type"] == "choice"
    assert tuple(question["criteria"]) == fm.CLASSIFIABLE_TYPES


def test_shadow_transport_failure_is_safe_and_payload_free(monkeypatch):
    system1 = _system1_module()
    cfg = _config(
        monkeypatch,
        system1_shadow=True,
        system1_base_url="http://system1",
        system1_api_key="system1-key",
    )
    body = "private page body"

    def fail(*args, **kwargs):
        raise httpx.ConnectError("connection failed")

    monkeypatch.setattr(system1.httpx, "post", fail)

    decision = system1.classify_page_type(cfg, body)

    assert decision.status == "unavailable"
    assert decision.page_type is None
    assert decision.probabilities == {}
    assert body not in repr(decision)


def test_shadow_unexpected_provider_failure_cannot_escape(monkeypatch):
    system1 = _system1_module()
    cfg = _config(
        monkeypatch,
        system1_shadow=True,
        system1_base_url="http://system1",
        system1_api_key="system1-key",
    )
    monkeypatch.setattr(
        system1.httpx,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("provider bug")),
    )

    decision = system1.classify_page_type(cfg, "private page body")

    assert decision.status == "invalid"


def test_shadow_rejects_incomplete_probability_vector(monkeypatch):
    system1 = _system1_module()
    cfg = _config(
        monkeypatch,
        system1_shadow=True,
        system1_base_url="http://system1",
        system1_api_key="system1-key",
    )

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "answers": {
                    "page_type": {
                        "type": "choice",
                        "choice": "guide",
                        "probabilities": {"guide": 1.0},
                    }
                }
            }

    monkeypatch.setattr(system1.httpx, "post", lambda *a, **k: Response())

    decision = system1.classify_page_type(cfg, "# Page")

    assert decision.status == "invalid"
    assert decision.page_type is None
    assert decision.probabilities == {}
