import iwiki_mcp.server as server
from iwiki_mcp import base, indexer


def _env(monkeypatch, base_dir):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setattr(
        indexer, "embed_texts",
        lambda cfg, texts: [[0.0] * cfg.dimensions for _ in texts])


def test_create_domain_makes_no_iwiki_dir(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(base, "resolve_binding", lambda project_dir=None: base.Binding(
        base=str(tmp_path), read=("d",), write="d", project_dir=str(tmp_path)))
    server.wiki_create_domain("d")
    assert (tmp_path / "d").is_dir()
    assert not (tmp_path / "d" / ".iwiki").exists()


def test_create_domain_bootstraps_empty_unbound_domain_with_exact_pathspec(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(base, "resolve_binding", lambda project_dir=None: base.Binding(
        base=str(tmp_path), read=("d",), write="d", project_dir=str(tmp_path)))
    monkeypatch.setattr(server.sync, "ensure_fresh", lambda *_: {"state": "clean"})
    calls = []
    monkeypatch.setattr(
        server.sync,
        "commit_and_push",
        lambda *args, **kwargs: calls.append((args, kwargs))
        or {"committed": False, "pushed": False},
    )

    out = server.wiki_create_domain("new-domain")

    assert out["created"] == "new-domain"
    assert list((tmp_path / "new-domain").iterdir()) == []
    assert calls[0][1]["pathspec"] == "new-domain"
