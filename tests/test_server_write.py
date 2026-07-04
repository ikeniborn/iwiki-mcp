import os

from iwiki_mcp import base, indexer, server


def _seed(tmp_path, monkeypatch, with_domain=True):
    b = tmp_path / "wiki"
    b.mkdir()
    if with_domain:
        (b / "backend" / ".iwiki").mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text('read = ["backend"]\nwrite = "backend"\n')
    monkeypatch.setenv("IWIKI_BASE_DIR", str(b))
    monkeypatch.setenv("IWIKI_PROJECT_DIR", str(proj))
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", "2")
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, t: [[1.0, 0.0] for _ in t])
    return str(b), str(proj)


def test_write_page_indexes_and_logs(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch)
    md = "# Auth\n## Overview\nsummary\n## Flow\nlogin then token\n"
    out = server.wiki_write_page("backend", "auth", md)
    assert out["page"] == "backend/auth.md"
    assert out["indexed_chunks"] >= 1
    assert os.path.isfile(os.path.join(b, "backend", "auth.md"))
    assert os.path.isfile(os.path.join(b, "backend", ".iwiki", "log.jsonl"))


def test_write_rejects_deep_heading(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_write_page("backend", "bad", "# T\n### Too Deep\nx\n")
    assert "error" in out


def test_write_refuses_overwrite_without_force(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    md = "# Auth\n## Overview\no\n## Flow\nx\n"
    server.wiki_write_page("backend", "auth", md)
    out = server.wiki_write_page("backend", "auth", md)
    assert "error" in out and "exists" in out["error"]


def test_create_domain(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch, with_domain=False)
    out = server.wiki_create_domain("backend")
    assert out["created"] == "backend"
    assert os.path.isdir(os.path.join(b, "backend"))


def test_bind_writes_config_for_current_project_domain(tmp_path, monkeypatch):
    b, proj = _seed(tmp_path, monkeypatch)
    os.makedirs(os.path.join(b, "proj", ".iwiki"))

    out = server.wiki_bind(read=["backend", "proj"], write="proj")

    assert out["read"] == ["backend", "proj"]
    text = open(os.path.join(proj, ".iwiki.toml")).read()
    assert 'read = ["backend", "proj"]' in text
    assert 'write = "proj"' in text


def test_bind_rejects_missing_domain_without_writing(tmp_path, monkeypatch):
    _b, proj = _seed(tmp_path, monkeypatch)
    config_path = os.path.join(proj, ".iwiki.toml")

    out = server.wiki_bind(write="missing")

    text = open(config_path).read()
    assert "error" in out
    assert "missing" in out["error"]
    assert 'write = "backend"' in text
    assert "missing" not in text


def test_bind_preserves_existing_read_when_adding_current_project(tmp_path, monkeypatch):
    b, proj = _seed(tmp_path, monkeypatch)
    os.makedirs(os.path.join(b, "proj", ".iwiki"))

    out = server.wiki_bind(read=["proj"], write="proj")

    assert out["read"] == ["backend", "proj"]
    text = open(os.path.join(proj, ".iwiki.toml")).read()
    assert 'read = ["backend", "proj"]' in text
    assert 'write = "proj"' in text


def test_bind_does_not_remove_existing_read_when_current_already_present(
    tmp_path, monkeypatch
):
    b, proj = _seed(tmp_path, monkeypatch)
    os.makedirs(os.path.join(b, "proj", ".iwiki"))
    config_path = os.path.join(proj, ".iwiki.toml")
    open(config_path, "w").write('read = ["backend", "proj"]\nwrite = "proj"\n')

    out = server.wiki_bind(read=["proj"], write="proj")

    assert out["read"] == ["backend", "proj"]
    assert 'read = ["backend", "proj"]' in open(config_path).read()


def test_bind_rejects_new_non_current_read_without_writing(tmp_path, monkeypatch):
    b, proj = _seed(tmp_path, monkeypatch)
    os.makedirs(os.path.join(b, "shared", ".iwiki"))
    config_path = os.path.join(proj, ".iwiki.toml")

    out = server.wiki_bind(read=["shared"], write="proj")

    text = open(config_path).read()
    assert out["error"] == "read scope is protected"
    assert 'read = ["backend"]' in text
    assert 'shared' not in text


def test_bind_rejects_non_current_write_without_writing(tmp_path, monkeypatch):
    b, proj = _seed(tmp_path, monkeypatch)
    os.makedirs(os.path.join(b, "shared", ".iwiki"))
    config_path = os.path.join(proj, ".iwiki.toml")

    out = server.wiki_bind(write="shared")

    text = open(config_path).read()
    assert out["error"] == "write domain must match current project domain"
    assert 'write = "backend"' in text
    assert 'write = "shared"' not in text


def test_write_page_removes_new_file_when_indexing_fails(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        indexer,
        "index_domain",
        lambda cfg, base, domain: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    out = server.wiki_write_page(
        "backend",
        "auth",
        "# Auth\n## Overview\nsummary\n## Flow\nlogin then token\n",
    )

    log_path = base.log_path(b, "backend")
    log_text = (
        open(log_path, encoding="utf-8").read() if os.path.exists(log_path) else ""
    )
    assert "error" in out
    assert not os.path.exists(os.path.join(b, "backend", "auth.md"))
    assert "auth.md" not in log_text


def test_write_page_does_not_leave_index_record_when_logging_fails(
    tmp_path, monkeypatch
):
    b, _ = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        indexer,
        "append_log",
        lambda base, domain, op, source, page, src_hash: (_ for _ in ()).throw(
            RuntimeError("log failed")
        ),
    )

    out = server.wiki_write_page(
        "backend",
        "auth",
        "# Auth\n## Overview\nsummary\n## Flow\nlogin then token\n",
    )

    index_path = base.index_path(b, "backend")
    index_text = (
        open(index_path, encoding="utf-8").read()
        if os.path.exists(index_path)
        else ""
    )
    assert "error" in out
    assert not os.path.exists(os.path.join(b, "backend", "auth.md"))
    assert "auth.md" not in index_text


def test_index_commits_and_reports_push(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch)
    server.wiki_write_page("backend", "auth", "# A\n## Overview\no\n## Flow\nx\n")
    out = server.wiki_index("backend")
    assert out["domain"] == "backend"
    assert "committed" in out and "pushed" in out
