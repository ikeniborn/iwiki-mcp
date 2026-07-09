from iwiki_mcp import ignore


def test_ensure_creates_with_secret_defaults(tmp_path):
    created = ignore.ensure_iwikiignore(str(tmp_path))
    assert created is True
    text = (tmp_path / ".iwikiignore").read_text()
    assert ".env" in text
    assert "*secret*" in text


def test_ensure_is_idempotent(tmp_path):
    (tmp_path / ".iwikiignore").write_text("custom\n")
    created = ignore.ensure_iwikiignore(str(tmp_path))
    assert created is False
    assert (tmp_path / ".iwikiignore").read_text() == "custom\n"


def test_ensure_seeds_from_gitignore(tmp_path):
    (tmp_path / ".gitignore").write_text("build/\n*.log\n")
    ignore.ensure_iwikiignore(str(tmp_path))
    text = (tmp_path / ".iwikiignore").read_text()
    assert "build/" in text
    assert "*.log" in text


def test_is_ignored_matches_inside_project(tmp_path):
    (tmp_path / ".iwikiignore").write_text(".env\nsecrets/**\n")
    spec = ignore.load_project_ignore(str(tmp_path))
    assert ignore.is_ignored(spec, str(tmp_path / ".env"), str(tmp_path)) is True
    assert ignore.is_ignored(spec, str(tmp_path / "secrets" / "k.txt"),
                             str(tmp_path)) is True
    assert ignore.is_ignored(spec, str(tmp_path / "src" / "main.py"),
                             str(tmp_path)) is False


def test_is_ignored_outside_project_matches_basename(tmp_path):
    (tmp_path / ".iwikiignore").write_text("*.key\n")
    spec = ignore.load_project_ignore(str(tmp_path))
    outside = tmp_path.parent / "elsewhere" / "id.key"
    assert ignore.is_ignored(spec, str(outside), str(tmp_path)) is True


def test_load_returns_none_when_absent(tmp_path):
    assert ignore.load_project_ignore(str(tmp_path)) is None
