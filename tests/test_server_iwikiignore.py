import os

from iwiki_mcp import server

# Reuse the established seed pattern.
from tests.test_server_write import _seed


def test_write_page_rejects_ignored_source(tmp_path, monkeypatch):
    b, proj = _seed(tmp_path, monkeypatch)
    open(os.path.join(proj, ".iwikiignore"), "w").write(".env\n")
    secret = os.path.join(proj, ".env")
    open(secret, "w").write("TOKEN=1")

    md = "# Auth\n## Overview\no\n## Flow\nx\n"
    out = server.wiki_write_page("backend", "auth", md, source=secret)

    assert "error" in out
    assert "iwikiignore" in out["error"]
    assert not os.path.exists(os.path.join(b, "backend", "auth.md"))


def test_write_page_rejects_path_anchored_ignored_source(tmp_path, monkeypatch):
    # A PATH-ANCHORED pattern must still match when source is an absolute path
    # inside the project. The ignore gate runs on the raw source before it is
    # normalized to project-relative; if normalization ran first, is_ignored
    # would abspath the relative path against the process CWD (the repo root,
    # not the tmp project) and the anchored pattern would silently miss.
    b, proj = _seed(tmp_path, monkeypatch)
    open(os.path.join(proj, ".iwikiignore"), "w").write("config/secret.py\n")
    os.makedirs(os.path.join(proj, "config"))
    secret = os.path.join(proj, "config", "secret.py")
    open(secret, "w").write("TOKEN=1")

    md = "# Auth\n## Overview\no\n## Flow\nx\n"
    out = server.wiki_write_page("backend", "auth", md, source=secret)

    assert "error" in out
    assert "iwikiignore" in out["error"]
    assert not os.path.exists(os.path.join(b, "backend", "auth.md"))


def test_write_page_allows_non_ignored_source(tmp_path, monkeypatch):
    b, proj = _seed(tmp_path, monkeypatch)
    open(os.path.join(proj, ".iwikiignore"), "w").write(".env\n")
    src = os.path.join(proj, "src.py")
    open(src, "w").write("x = 1")

    md = "# Auth\n## Overview\no\n## Flow\nx\n"
    out = server.wiki_write_page("backend", "auth", md, source=src)

    assert out.get("page") == "backend/concept/auth.md"
    assert os.path.isfile(os.path.join(b, "backend", "concept", "auth.md"))


def test_create_domain_creates_iwikiignore(tmp_path, monkeypatch):
    b, proj = _seed(tmp_path, monkeypatch, with_domain=False)
    server.wiki_create_domain("backend")
    assert os.path.isfile(os.path.join(proj, ".iwikiignore"))


def test_bind_creates_iwikiignore(tmp_path, monkeypatch):
    b, proj = _seed(tmp_path, monkeypatch)
    os.makedirs(os.path.join(b, "proj"))
    server.wiki_bind(read=["proj"], write="proj")
    assert os.path.isfile(os.path.join(proj, ".iwikiignore"))
