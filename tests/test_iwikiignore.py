from iwiki_mcp import ignore, project_files


def test_ensure_creates_with_secret_defaults(tmp_path):
    created = ignore.ensure_iwikiignore(str(tmp_path))
    assert created is True
    text = (tmp_path / ".iwikiignore").read_text()
    assert ".env" in text
    assert "*secret*" in text
    assert ".git/" in text
    assert "node_modules/" in text
    assert "__pycache__/" in text


def test_ensure_fills_whitespace_only_file(tmp_path):
    path = tmp_path / ".iwikiignore"
    path.write_text(" \n\t")

    created = ignore.ensure_iwikiignore(str(tmp_path))

    assert created is True
    assert "*.pem" in path.read_text()
    assert "build/" in path.read_text()


def test_ensure_is_idempotent(tmp_path):
    path = tmp_path / ".iwikiignore"
    original = b"custom\r\n# manual\r\n"
    path.write_bytes(original)
    created = ignore.ensure_iwikiignore(str(tmp_path))
    assert created is False
    assert path.read_bytes() == original


def test_ensure_seeds_from_gitignore(tmp_path):
    (tmp_path / ".gitignore").write_text("build/\n*.log\n")
    ignore.ensure_iwikiignore(str(tmp_path))
    text = (tmp_path / ".iwikiignore").read_text()
    assert "build/" in text
    assert "*.log" in text


def test_ensure_does_not_follow_symlink(tmp_path):
    outside = tmp_path / "manual-ignore"
    outside.write_bytes(b" \n\t")
    (tmp_path / ".iwikiignore").symlink_to(outside)

    assert ignore.ensure_iwikiignore(str(tmp_path)) is False
    assert outside.read_bytes() == b" \n\t"


def test_atomic_initialization_failure_keeps_empty_file_recoverable(
    tmp_path, monkeypatch
):
    path = tmp_path / ".iwikiignore"
    monkeypatch.setattr(
        project_files.os,
        "link",
        lambda *_args: (_ for _ in ()).throw(OSError("publication failed")),
    )

    assert ignore.ensure_iwikiignore(str(tmp_path)) is False
    assert not path.exists()
    assert list(tmp_path.glob(".*.tmp")) == []


def test_manual_replace_during_empty_initialization_is_preserved(
    tmp_path, monkeypatch
):
    path = tmp_path / ".iwikiignore"
    path.write_bytes(b" \n\t")
    manual = b"manual-entry/\n"
    original_lseek = project_files.os.lseek
    seeks = 0

    def replace_before_write(descriptor, offset, whence):
        nonlocal seeks
        if offset == 0 and whence == project_files.os.SEEK_SET:
            seeks += 1
        if seeks == 2:
            replacement = tmp_path / "replacement"
            replacement.write_bytes(manual)
            replacement.replace(path)
            seeks += 1
        return original_lseek(descriptor, offset, whence)

    monkeypatch.setattr(project_files.os, "lseek", replace_before_write)

    assert ignore.ensure_iwikiignore(str(tmp_path)) is False
    assert path.read_bytes() == manual


def test_partial_existing_write_restores_original_whitespace(tmp_path, monkeypatch):
    path = tmp_path / ".iwikiignore"
    original = b" \n\t"
    path.write_bytes(original)
    original_write = project_files.os.write
    calls = 0

    def fail_after_partial_write(descriptor, content):
        nonlocal calls
        calls += 1
        if calls == 1:
            original_write(descriptor, content[:8])
            raise OSError("partial write")
        return original_write(descriptor, content)

    monkeypatch.setattr(project_files.os, "write", fail_after_partial_write)

    assert ignore.ensure_iwikiignore(str(tmp_path)) is False
    assert path.read_bytes() == original


def test_cleanup_error_does_not_escape_initialization(tmp_path, monkeypatch):
    monkeypatch.setattr(
        project_files.os,
        "link",
        lambda *_args: (_ for _ in ()).throw(OSError("publication failed")),
    )
    monkeypatch.setattr(
        project_files.os,
        "unlink",
        lambda *_args: (_ for _ in ()).throw(PermissionError("cleanup denied")),
    )

    assert ignore.ensure_iwikiignore(str(tmp_path)) is False


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
