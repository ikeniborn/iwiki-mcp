from dataclasses import replace

import httpx
import pytest

import iwiki_mcp.server as server
from iwiki_mcp.engine import frontmatter as fm
from iwiki_mcp.engine import system1
from iwiki_mcp.engine.config import Config, ConfigError

from tests.test_server_write_frontmatter import _patch


def _config(monkeypatch, **changes):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://llm")
    monkeypatch.setenv("IWIKI_LLM_KEY", "llm-key")
    return replace(
        Config.load(),
        system1_base_url="http://system1/v1",
        system1_api_key="system1-key",
        **changes,
    )


def _decision(page_type, probability=0.9, status="ok"):
    rest = (1 - probability) / 5
    return system1.PageTypeDecision(
        status=status,
        page_type=page_type,
        probabilities={
            t: probability if t == page_type else rest for t in fm.CLASSIFIABLE_TYPES
        },
        latency_ms=1.0,
    )


def test_guidance_settings_default_off(monkeypatch):
    for name in ("IWIKI_SYSTEM1_GUIDANCE", "IWIKI_SYSTEM1_SEARCH_BOOST",
                 "IWIKI_SYSTEM1_MIN_CONFIDENCE", "IWIKI_SYSTEM1_SHADOW"):
        monkeypatch.delenv(name, raising=False)
    cfg = _config(monkeypatch)

    assert (cfg.system1_guidance, cfg.system1_search_boost,
            cfg.system1_min_confidence) == (False, 0.0, 0.5)


def test_guidance_settings_load_and_require_connection(monkeypatch):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://llm")
    monkeypatch.setenv("IWIKI_LLM_KEY", "llm-key")
    monkeypatch.delenv("IWIKI_SYSTEM1_SHADOW", raising=False)
    monkeypatch.setenv("IWIKI_SYSTEM1_GUIDANCE", "on")
    monkeypatch.setenv("IWIKI_SYSTEM1_SEARCH_BOOST", "0.002")
    monkeypatch.setenv("IWIKI_SYSTEM1_MIN_CONFIDENCE", "0.6")
    monkeypatch.delenv("IWIKI_SYSTEM1_BASE_URL", raising=False)
    monkeypatch.delenv("IWIKI_SYSTEM1_KEY", raising=False)
    with pytest.raises(ConfigError, match="IWIKI_SYSTEM1_BASE_URL and IWIKI_SYSTEM1_KEY"):
        Config.load()

    monkeypatch.setenv("IWIKI_SYSTEM1_BASE_URL", "http://system1/v1")
    monkeypatch.setenv("IWIKI_SYSTEM1_KEY", "system1-key")
    cfg = Config.load()
    assert (cfg.system1_guidance, cfg.system1_search_boost,
            cfg.system1_min_confidence) == (True, 0.002, 0.6)


@pytest.mark.parametrize(("name", "value"), [
    ("IWIKI_SYSTEM1_SEARCH_BOOST", "high"),
    ("IWIKI_SYSTEM1_SEARCH_BOOST", "1.5"),
    ("IWIKI_SYSTEM1_MIN_CONFIDENCE", "2"),
])
def test_guidance_settings_reject_invalid_numbers(monkeypatch, name, value):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://llm")
    monkeypatch.setenv("IWIKI_LLM_KEY", "llm-key")
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError):
        Config.load()


def test_warning_only_for_confident_non_weak_disagreement(monkeypatch):
    cfg = _config(monkeypatch, system1_guidance=True, system1_min_confidence=0.5)

    warning = system1.type_guidance_warning(cfg, _decision("reference", 0.82), "Concept")

    assert warning == (
        "System One suggests type 'reference' (p=0.82); explicit type 'concept' kept"
    )
    assert system1.type_guidance_warning(cfg, _decision("concept"), "concept") is None
    assert system1.type_guidance_warning(cfg, _decision("reference", 0.4), "concept") is None
    assert system1.type_guidance_warning(cfg, _decision("runbook"), "concept") is None
    assert system1.type_guidance_warning(cfg, _decision("guide"), "concept") is None
    assert system1.type_guidance_warning(cfg, _decision(None, status="unavailable"),
                                         "concept") is None
    assert system1.type_guidance_warning(cfg, None, "concept") is None
    assert system1.type_guidance_warning(cfg, _decision("reference"), None) is None
    assert system1.type_guidance_warning(cfg, _decision("reference"),
                                         "specification") is None
    off = replace(cfg, system1_guidance=False)
    assert system1.type_guidance_warning(off, _decision("reference"), "concept") is None


def test_guidance_alone_enables_classification(monkeypatch):
    cfg = _config(monkeypatch, system1_shadow=False, system1_guidance=False)
    monkeypatch.setattr(system1.httpx, "post", lambda *a, **k: pytest.fail("no call"))
    assert system1.classify_page_type(cfg, "# Page") is None

    guided = replace(cfg, system1_guidance=True)
    monkeypatch.setattr(system1.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(
        httpx.ConnectError("offline")))
    assert system1.classify_page_type(guided, "# Page").status == "unavailable"


def _hits(*files):
    return [{"domain": "d", "file": f, "heading": "H", "chunk": 0} for f in files]


def test_boost_promotes_matching_type_without_changing_membership(monkeypatch):
    cfg = _config(monkeypatch, system1_search_boost=0.002)
    monkeypatch.setattr(system1, "_decide", lambda c, q: _decision("reference", 0.9))
    ordered = _hits("concept/a.md", "architecture/b.md", "reference/c.md", "top.md")

    boosted = system1.boost_by_query_type(cfg, "which flags exist", ordered)

    assert [h["file"] for h in boosted] == [
        "reference/c.md", "concept/a.md", "architecture/b.md", "top.md"]
    assert sorted(map(id, boosted)) == sorted(map(id, ordered))


@pytest.mark.parametrize("decision", [
    _decision("runbook", 0.95),
    _decision("reference", 0.3),
    _decision(None, status="invalid"),
])
def test_boost_is_noop_for_unusable_decisions(monkeypatch, decision):
    cfg = _config(monkeypatch, system1_search_boost=0.002)
    monkeypatch.setattr(system1, "_decide", lambda c, q: decision)
    ordered = _hits("concept/a.md", "reference/c.md")

    assert system1.boost_by_query_type(cfg, "q", ordered) == ordered


def test_boost_disabled_makes_no_request(monkeypatch):
    cfg = _config(monkeypatch, system1_search_boost=0.0)
    monkeypatch.setattr(system1, "_decide", lambda c, q: pytest.fail("no call"))
    ordered = _hits("concept/a.md", "reference/c.md")

    assert system1.boost_by_query_type(cfg, "q", ordered) is ordered


def test_git_write_returns_guidance_warning_and_keeps_type(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    monkeypatch.setenv("IWIKI_SYSTEM1_GUIDANCE", "true")
    monkeypatch.setenv("IWIKI_SYSTEM1_BASE_URL", "http://system1/v1")
    monkeypatch.setenv("IWIKI_SYSTEM1_KEY", "system1-key")
    monkeypatch.setattr(system1, "_decide", lambda cfg, body: _decision("reference", 0.8))
    body = "# Base binding\n\n## Overview\nHow binding works.\n\n## Detail\nwords here\n"

    result = server.wiki_write_page("d", "base", body, source=None, type="concept")

    assert "error" not in result
    assert "System One suggests type 'reference' (p=0.80)" in result["warning"]
    meta, _ = fm.split(
        (tmp_path / "d" / "concept" / "base.md").read_text(encoding="utf-8"))
    assert meta["type"] == "concept"


def test_postgres_preparation_returns_guidance_warning(monkeypatch):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setenv("IWIKI_SYSTEM1_GUIDANCE", "true")
    monkeypatch.setenv("IWIKI_SYSTEM1_BASE_URL", "http://system1/v1")
    monkeypatch.setenv("IWIKI_SYSTEM1_KEY", "system1-key")
    monkeypatch.setattr(system1, "_decide", lambda cfg, body: _decision("architecture"))
    markdown = "# Page\n\n## Overview\nHow it works.\n\n## Detail\nMore.\n"

    identity, rendered, warning = server._prepare_postgres_page(
        Config.load(), "d", "page", markdown, source=None, type="reference",
        tags=None, description=None, status=None)

    assert identity == "reference/page"
    assert fm.split(rendered)[0]["type"] == "reference"
    assert "System One suggests type 'architecture'" in warning


def test_wiki_search_applies_boost_before_slicing_to_k(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    monkeypatch.setenv("IWIKI_SYSTEM1_SEARCH_BOOST", "0.002")
    monkeypatch.setenv("IWIKI_SYSTEM1_BASE_URL", "http://system1/v1")
    monkeypatch.setenv("IWIKI_SYSTEM1_KEY", "system1-key")
    monkeypatch.delenv("IWIKI_RERANK_MODEL", raising=False)
    pool = _hits("concept/a.md", "architecture/b.md", "reference/c.md")
    monkeypatch.setattr(server.retrieval, "prepare_read_candidates",
                        lambda *a, **k: [dict(h) for h in pool])
    queries = []

    def decide(cfg, query):
        queries.append(query)
        return _decision("reference", 0.9)

    monkeypatch.setattr(system1, "_decide", decide)

    result = server.wiki_search("which flags exist", domains=["d"], k=2)

    assert queries == ["which flags exist"]
    assert [h["file"] for h in result["results"]] == ["reference/c.md", "concept/a.md"]
