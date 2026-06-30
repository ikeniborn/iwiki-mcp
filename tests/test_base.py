import os

import pytest
from iwiki_mcp import base


def _mkbase(tmp_path, *domains):
    b = tmp_path / "wiki"
    for d in domains:
        (b / d / ".iwiki").mkdir(parents=True)
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


def test_index_path_uses_jsonl_index():
    assert base.index_path("/wiki", "backend").endswith(
        os.path.join(".iwiki", "index.jsonl")
    )
