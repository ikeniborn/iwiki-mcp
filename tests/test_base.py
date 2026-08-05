import subprocess
from pathlib import Path

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
    assert bind.write_scope == ("backend",)


def test_manual_binding_fixture_keeps_scalar_write_compatibility():
    bind = base.Binding(
        base="/wiki",
        read=("backend",),
        write="backend",
        project_dir="/project",
    )

    assert bind.write_scope == ()
    assert base.writable_domains(bind) == ("backend",)
    assert base.write_scope_error(bind, "backend") is None
    assert "outside bound write scope" in base.write_scope_error(bind, "other")["error"]


def test_resolve_binding_deduplicates_explicit_write_scope(tmp_path, monkeypatch):
    b = _mkbase(tmp_path, "backend", "shared")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text(
        'read = ["backend", "shared"]\n'
        'write = "backend"\n'
        'write_scope = ["backend", "shared", "backend"]\n'
    )
    monkeypatch.setenv("IWIKI_BASE_DIR", b)

    bind = base.resolve_binding(str(proj))

    assert bind.write_scope == ("backend", "shared")


@pytest.mark.parametrize(
    ("write_scope", "message"),
    [
        ('["shared"]', "primary write domain"),
        ('["backend", "hidden"]', "read scope"),
        ('["backend", "missing"]', "not found"),
    ],
)
def test_resolve_binding_rejects_invalid_write_scope(
    tmp_path, monkeypatch, write_scope, message
):
    b = _mkbase(tmp_path, "backend", "shared", "hidden")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text(
        'read = ["backend", "shared"]\n'
        'write = "backend"\n'
        f"write_scope = {write_scope}\n"
    )
    monkeypatch.setenv("IWIKI_BASE_DIR", b)

    with pytest.raises(base.BaseError, match=message):
        base.resolve_binding(str(proj))


def test_resolve_binding_rejects_absolute_write_scope_domain(tmp_path, monkeypatch):
    b = _mkbase(tmp_path, "backend")
    outside = tmp_path / "outside"
    outside.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text(
        f'base = "{b}"\n'
        f'read = ["{outside}"]\n'
        f'write = "{outside}"\n'
        f'write_scope = ["{outside}"]\n'
    )
    monkeypatch.delenv("IWIKI_BASE_DIR", raising=False)

    with pytest.raises(base.BaseError, match="not found"):
        base.resolve_binding(str(proj))


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
    base.write_project_config(
        str(proj), read=["x"], write="x", write_scope=["x", "x"]
    )
    bind = base.resolve_binding(str(proj))
    assert bind.write == "x"
    assert bind.read == ("x",)
    assert bind.write_scope == ("x",)


def test_write_project_config_preserves_fields_on_partial_updates(tmp_path, monkeypatch):
    b = _mkbase(tmp_path, "a", "b")
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.delenv("IWIKI_BASE_DIR", raising=False)
    (proj / ".iwiki.toml").write_text(
        f'base = "{b}"\nread = ["a", "b"]\nwrite = "a"\n'
    )

    base.write_project_config(str(proj), write="b")
    bind = base.resolve_binding(str(proj))
    assert bind.base == b
    assert bind.read == ("a", "b")
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


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def test_ensure_graph_store_excluded_writes_root_pattern_to_git_exclude(tmp_path):
    repo = tmp_path / "wiki"
    repo.mkdir()
    _git("init", cwd=repo)

    assert base.ensure_graph_store_excluded(str(repo)) is True

    exclude_path = _git(
        "rev-parse", "--git-path", "info/exclude", cwd=repo
    ).stdout.strip()
    exclude_path = repo / exclude_path
    exclude_lines = exclude_path.read_text(encoding="utf-8").splitlines()
    assert exclude_lines[-1] == "/.iwiki/"
    assert exclude_lines.count("/.iwiki/") == 1
    assert not (repo / ".gitignore").exists()


def test_ensure_graph_store_excluded_is_idempotent(tmp_path):
    repo = tmp_path / "wiki"
    repo.mkdir()
    _git("init", cwd=repo)

    assert base.ensure_graph_store_excluded(str(repo)) is True
    assert base.ensure_graph_store_excluded(str(repo)) is True

    exclude_path = _git(
        "rev-parse", "--git-path", "info/exclude", cwd=repo
    ).stdout.strip()
    assert (repo / exclude_path).read_text(encoding="utf-8").splitlines().count(
        "/.iwiki/"
    ) == 1


def test_ensure_graph_store_excluded_fails_soft_outside_git(tmp_path):
    base_dir = tmp_path / "plain"
    base_dir.mkdir()

    assert base.ensure_graph_store_excluded(str(base_dir)) is False
    assert list(base_dir.iterdir()) == []


def test_ensure_graph_store_excluded_uses_linked_worktree_git_path(tmp_path):
    repo = tmp_path / "wiki"
    linked = tmp_path / "wiki-linked"
    repo.mkdir()
    _git("init", cwd=repo)
    (repo / "README.md").write_text("wiki\n", encoding="utf-8")
    _git("add", "README.md", cwd=repo)
    _git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "initial",
        cwd=repo,
    )
    _git("worktree", "add", "-b", "linked", str(linked), cwd=repo)

    assert base.ensure_graph_store_excluded(str(linked)) is True

    exclude_output = _git(
        "rev-parse", "--git-path", "info/exclude", cwd=linked
    ).stdout.strip()
    exclude_path = Path(exclude_output)
    if not exclude_path.is_absolute():
        exclude_path = linked / exclude_path
    assert "/.iwiki/" in exclude_path.read_text(encoding="utf-8").splitlines()
    assert not (linked / ".gitignore").exists()


def test_root_graph_exclude_does_not_match_domain_iwiki_directory(tmp_path):
    repo = tmp_path / "wiki"
    repo.mkdir()
    _git("init", cwd=repo)
    base.ensure_graph_store_excluded(str(repo))
    (repo / ".iwiki").mkdir()
    (repo / ".iwiki" / "graph.sqlite3").touch()
    (repo / "domain" / ".iwiki").mkdir(parents=True)
    (repo / "domain" / ".iwiki" / "index.jsonl").touch()

    root_match = subprocess.run(
        ["git", "check-ignore", ".iwiki/graph.sqlite3"], cwd=repo
    )
    nested_match = subprocess.run(
        ["git", "check-ignore", "domain/.iwiki/index.jsonl"], cwd=repo
    )

    assert root_match.returncode == 0
    assert nested_match.returncode == 1
