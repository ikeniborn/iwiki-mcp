import pytest
from iwiki_mcp import base


def _mkbase(tmp_path, *domains):
    b = tmp_path / "wiki"
    for d in domains:
        (b / d).mkdir(parents=True)
        (b / d / "page.md").write_text("# P\n## Overview\nx\n")
    b.mkdir(exist_ok=True)
    return str(b)


def test_resolve_from_env(tmp_path, monkeypatch):
    b = _mkbase(tmp_path, "backend", "shared")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text('read = ["backend"]\nwrite = "backend"\n')
    monkeypatch.setenv("IWIKI_BASE_DIR", b)
    bind = base.resolve_binding(str(proj))
    assert bind.base == b
    assert bind.read == ("backend",)
    assert bind.write == "backend"


def test_missing_base_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("IWIKI_BASE_DIR", raising=False)
    proj = tmp_path / "proj"
    proj.mkdir()
    with pytest.raises(base.BaseError):
        base.resolve_binding(str(proj))


def test_empty_read_defaults_to_all_domains(tmp_path, monkeypatch):
    b = _mkbase(tmp_path, "a", "b")
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("IWIKI_BASE_DIR", b)
    bind = base.resolve_binding(str(proj))
    assert set(base.resolve_scope(bind, "project", None)) == {"a", "b"}


def test_scope_all_vs_explicit(tmp_path, monkeypatch):
    b = _mkbase(tmp_path, "a", "b", "c")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text('read = ["a"]\nwrite = "a"\n')
    monkeypatch.setenv("IWIKI_BASE_DIR", b)
    bind = base.resolve_binding(str(proj))
    assert base.resolve_scope(bind, "project", None) == ["a"]
    assert set(base.resolve_scope(bind, "all", None)) == {"a", "b", "c"}
    assert base.resolve_scope(bind, "project", ["b", "c"]) == ["b", "c"]


def test_write_project_config_roundtrip(tmp_path, monkeypatch):
    b = _mkbase(tmp_path, "x")
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("IWIKI_BASE_DIR", b)
    base.write_project_config(str(proj), read=["x"], write="x")
    bind = base.resolve_binding(str(proj))
    assert bind.write == "x"
    assert bind.read == ("x",)


def test_write_project_config_preserves_fields_on_partial_updates(tmp_path, monkeypatch):
    b = _mkbase(tmp_path, "a", "b")
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.delenv("IWIKI_BASE_DIR", raising=False)
    (proj / ".iwiki.toml").write_text(
        f'base = "{b}"\nread = ["a"]\nwrite = "a"\n'
    )

    base.write_project_config(str(proj), write="b")
    bind = base.resolve_binding(str(proj))
    assert bind.base == b
    assert bind.read == ("a",)
    assert bind.write == "b"

    base.write_project_config(str(proj), read=["b"])
    bind = base.resolve_binding(str(proj))
    assert bind.base == b
    assert bind.read == ("b",)
    assert bind.write == "b"


def test_write_project_config_preserves_unknown_lines_and_comments(
    tmp_path, monkeypatch
):
    b = _mkbase(tmp_path, "a", "b")
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.delenv("IWIKI_BASE_DIR", raising=False)
    (proj / ".iwiki.toml").write_text(
        f'# keep me\nbase = "{b}"\ncustom = "value"\nread = ["a"]\nwrite = "a"\n'
    )

    base.write_project_config(str(proj), read=["b"], write="b")

    text = (proj / ".iwiki.toml").read_text()
    assert "# keep me" in text
    assert 'custom = "value"' in text
    assert f'base = "{b}"' in text
    assert 'read = ["b"]' in text
    assert 'write = "b"' in text


def test_write_project_config_removes_multiline_core_assignment(
    tmp_path, monkeypatch
):
    b = _mkbase(tmp_path, "new")
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.delenv("IWIKI_BASE_DIR", raising=False)
    (proj / ".iwiki.toml").write_text(
        f'base = "{b}"\n'
        "# keep multiline\n"
        "custom = \"value\"\n"
        "read = [\n"
        '  "old",\n'
        "]\n"
        'write = "old"\n'
    )

    base.write_project_config(str(proj), read=["new"], write="new")

    text = (proj / ".iwiki.toml").read_text()
    bind = base.resolve_binding(str(proj))
    assert bind.read == ("new",)
    assert bind.write == "new"
    assert "# keep multiline" in text
    assert 'custom = "value"' in text
    assert '"old"' not in text
    assert "\n]\n" not in text


def test_index_path_uses_jsonl_index():
    assert base.index_path("/wiki", "backend").endswith(
        "index.jsonl"
    )


def test_current_project_domain_uses_project_dir_basename(tmp_path):
    proj = tmp_path / "my-project"
    proj.mkdir()

    assert base.current_project_domain(str(proj)) == "my-project"


def test_merge_read_scope_sets_read_when_existing_empty():
    merged, error = base.merge_read_scope((), ("backend", "shared"), "backend")

    assert error is None
    assert merged == ("backend", "shared")


def test_merge_read_scope_appends_current_domain_only():
    merged, error = base.merge_read_scope(("foreign",), ("backend",), "backend")

    assert error is None
    assert merged == ("foreign", "backend")


def test_merge_read_scope_preserves_existing_when_current_already_present():
    merged, error = base.merge_read_scope(
        ("foreign", "backend"),
        ("backend",),
        "backend",
    )

    assert error is None
    assert merged == ("foreign", "backend")


def test_merge_read_scope_rejects_new_non_current_domain():
    merged, error = base.merge_read_scope(("foreign",), ("shared",), "backend")

    assert merged == ("foreign",)
    assert error == "read scope is protected"
