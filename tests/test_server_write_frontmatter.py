import iwiki_mcp.server as server
import iwiki_mcp.indexer as indexer
from iwiki_mcp.engine import frontmatter as fm


def _bind(tmp_path):
    (tmp_path / "d").mkdir(parents=True)
    return server.base.Binding(base=str(tmp_path), read=("d",), write="d",
                               project_dir=str(tmp_path))


def _patch(monkeypatch, tmp_path):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setattr(server.base, "resolve_binding", lambda: _bind(tmp_path))
    monkeypatch.setattr(server.sync, "ensure_fresh", lambda b: {"state": "clean"})
    monkeypatch.setattr(
        server.sync, "commit_and_push",
        lambda *a, **k: {"committed": True, "pushed": False}
    )
    monkeypatch.setattr(
        indexer, "embed_texts",
        lambda cfg, texts: [[0.1, 0.2] for _ in texts]
    )


def test_write_with_explicit_type_and_tags(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    body = "# Base binding\n\n## Overview\nHow binding works.\n\n## Detail\nwords here\n"
    res = server.wiki_write_page("d", "base", body, source=None, type="api", tags=["Binding"])
    assert "error" not in res
    meta, rest = fm.split((tmp_path / "d" / "api" / "base.md").read_text(encoding="utf-8"))
    assert meta["type"] == "api"
    assert meta["title"] == "Base binding"
    assert meta["description"].startswith("How binding works")
    assert meta["tags"] == ["binding"]          # normalized
    assert rest.startswith("# Base binding")


def test_write_without_type_and_no_chat_model_defaults_concept(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)   # IWIKI_CHAT_MODEL unset -> default path
    body = "# T\n\n## Overview\nsumm\n\n## B\nwords\n"
    res = server.wiki_write_page("d", "p", body, source=None)
    meta, _ = fm.split((tmp_path / "d" / "concept" / "p.md").read_text(encoding="utf-8"))
    assert meta["type"] == "concept"
    assert "warning" in res


def test_write_without_type_uses_server_classifier_when_configured(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    monkeypatch.setenv("IWIKI_CHAT_MODEL", "chat-x")
    from iwiki_mcp import okf
    monkeypatch.setattr(
        okf.classify, "classify_page",
        lambda cfg, body, existing_tags: {
            "type": "guide", "tags": ["x"], "warning": None
        }
    )
    body = "# T\n\n## Overview\nsumm\n\n## B\nwords\n"
    server.wiki_write_page("d", "q", body, source=None)
    meta, _ = fm.split((tmp_path / "d" / "guide" / "q.md").read_text(encoding="utf-8"))
    assert meta["type"] == "guide"
    assert meta["tags"] == ["x"]


def test_explicit_tags_win_over_classifier(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    monkeypatch.setenv("IWIKI_CHAT_MODEL", "chat-x")
    from iwiki_mcp import okf
    monkeypatch.setattr(
        okf.classify, "classify_page",
        lambda cfg, body, existing_tags: {
            "type": "guide", "tags": ["classifier-tag"], "warning": None
        }
    )
    body = "# T\n\n## Overview\nsumm\n\n## B\nwords\n"
    server.wiki_write_page("d", "p", body, source=None, tags=["Explicit"])
    meta, _ = fm.split((tmp_path / "d" / "guide" / "p.md").read_text(encoding="utf-8"))
    assert meta["tags"] == ["explicit"]        # normalized explicit, not classifier's
    assert meta["type"] == "guide"             # type still from classifier


def test_write_with_description_and_status(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    body = "# Alice\n\n## Role\nBilling engineer work.\n"
    res = server.wiki_write_page("d", "alice", body, source=None, type="person",
                                 description="Alice covers AR ledger.", status="stable")
    assert "error" not in res
    meta, _ = fm.split((tmp_path / "d" / "person" / "alice.md").read_text(encoding="utf-8"))
    assert meta["type"] == "person"                       # open type kept
    assert meta["description"] == "Alice covers AR ledger."
    assert meta["status"] == "stable"


def test_write_missing_status_defaults_stub(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    body = "# Alice\n\n## Role\nwork.\n"
    server.wiki_write_page("d", "alice", body, source=None, type="person",
                           description="d")
    meta, _ = fm.split((tmp_path / "d" / "person" / "alice.md").read_text(encoding="utf-8"))
    assert meta["status"] == "stub"


def test_write_missing_description_warns(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    body = "# Alice\n\n## Role\nwork.\n"          # no Overview, no description param
    res = server.wiki_write_page("d", "alice", body, source=None, type="person")
    assert "warning" in res and "description" in res["warning"]
