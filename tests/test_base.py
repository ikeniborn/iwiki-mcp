import dataclasses
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
    (proj / ".iwiki.toml").write_text(
        'read = ["backend"]\nwrite = ["backend"]\nprimary = "backend"\n'
    )
    monkeypatch.setenv("IWIKI_BASE_DIR", b)
    bind = base.resolve_binding(str(proj))
    assert bind.base == b
    assert bind.read == ("backend",)
    assert bind.write == ("backend",)
    assert bind.primary == "backend"
    assert (proj / ".iwikiignore").is_file()


@pytest.mark.parametrize("storage_block", ["", '[storage]\ntype = "git"\n'])
def test_resolve_binding_uses_git_storage_by_default(
    tmp_path, monkeypatch, storage_block
):
    b = _mkbase(tmp_path, "backend")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text(
        'read = ["backend"]\nwrite = ["backend"]\nprimary = "backend"\n'
        + storage_block
    )
    monkeypatch.setenv("IWIKI_BASE_DIR", b)

    bind = base.resolve_binding(str(proj))

    assert bind.storage == "git"
    assert bind.base == b


@pytest.mark.parametrize(
    "contents",
    [
        b'password = "must-not-be-shown"\n[storage]\ntype = [',
        b'password = "must-not-be-shown"\n\xff',
    ],
)
def test_resolve_storage_binding_rejects_invalid_project_toml_safely(
    tmp_path, monkeypatch, contents
):
    b = _mkbase(tmp_path, "backend")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_bytes(contents)
    monkeypatch.setenv("IWIKI_BASE_DIR", b)

    with pytest.raises(base.BaseError) as caught:
        base.resolve_storage_binding(str(proj))

    assert "must-not-be-shown" not in str(caught.value)
    assert "project configuration" in str(caught.value)


@pytest.mark.parametrize("contents", [b"invalid = [", b"\xff"])
def test_legacy_project_config_loader_remains_fail_soft(tmp_path, contents):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_bytes(contents)

    assert base.load_project_config(str(proj)) == {}


def test_resolve_storage_binding_keeps_absent_project_config_git_default(
    tmp_path, monkeypatch
):
    b = _mkbase(tmp_path, "backend")
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("IWIKI_BASE_DIR", b)

    bind = base.resolve_storage_binding(str(proj))

    assert bind.storage == "git"
    assert bind.base == b


def _set_postgres_runtime(monkeypatch, *, password="database-secret"):
    monkeypatch.setenv("IWIKI_DB_PASSWORD", password)
    monkeypatch.setenv("IWIKI_EMBED_MODEL", "lemonade-embeddings-bge-m3-q8")
    monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", "1024")
    monkeypatch.setenv("IWIKI_RERANK_MODEL", "lemonade-reranker-bge-reranker-v2-m3")


def test_resolve_binding_builds_immutable_local_postgres_binding(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text(
        'read = ["docs", "shared"]\n'
        'write = ["docs"]\n'
        'primary = "docs"\n'
        '[storage]\n'
        'type = "postgres"\n'
        'host = "db.example.net"\n'
        'port = 5432\n'
        'database = "iwiki"\n'
        'user = "iwiki_local"\n'
        'sslmode = "verify-full"\n'
        'iwiki_id = "personal"\n'
    )
    monkeypatch.delenv("IWIKI_BASE_DIR", raising=False)
    _set_postgres_runtime(monkeypatch)

    bind = base.resolve_binding(str(proj))

    assert bind.storage == "postgres"
    assert bind.host == "db.example.net"
    assert bind.port == 5432
    assert bind.database == "iwiki"
    assert bind.user == "iwiki_local"
    assert bind.sslmode == "verify-full"
    assert bind.iwiki_id == "personal"
    assert bind.read == ("docs", "shared")
    assert bind.write == ("docs",)
    assert bind.primary == "docs"
    assert bind.embed_model == "lemonade-embeddings-bge-m3-q8"
    assert bind.embed_dimensions == 1024
    assert bind.rerank_model == "lemonade-reranker-bge-reranker-v2-m3"
    with pytest.raises(dataclasses.FrozenInstanceError):
        bind.iwiki_id = "other"


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (
            'read = ["docs"]\nwrite = ["docs"]\nprimary = "docs"\n'
            '[storage]\ntype = "sqlite"\n',
            "unsupported storage type",
        ),
        (
            'read = ["docs"]\nwrite = ["docs"]\nprimary = "docs"\n'
            '[storage]\ntype = "postgres"\nport = 5432\n'
            'database = "iwiki"\nuser = "user"\nsslmode = "require"\n'
            'iwiki_id = "personal"\n',
            "host",
        ),
        (
            'read = ["docs"]\nwrite = ["docs"]\nprimary = "docs"\n'
            '[storage]\ntype = "postgres"\nhost = "db"\nport = 5432\n'
            'database = "iwiki"\nuser = "user"\nsslmode = "require"\n',
            "iwiki_id",
        ),
        (
            '[storage]\ntype = "postgres"\nhost = "db"\nport = 5432\n'
            'database = "iwiki"\nuser = "user"\nsslmode = "require"\n'
            'iwiki_id = "personal"\n',
            "read",
        ),
        (
            'read = ["docs"]\nwrite = ["private"]\nprimary = "private"\n'
            '[storage]\ntype = "postgres"\nhost = "db"\nport = 5432\n'
            'database = "iwiki"\nuser = "user"\nsslmode = "require"\n'
            'iwiki_id = "personal"\n',
            "write scope",
        ),
        (
            'read = [""]\nwrite = ["docs"]\nprimary = "docs"\n'
            '[storage]\ntype = "postgres"\nhost = "db"\nport = 5432\n'
            'database = "iwiki"\nuser = "user"\nsslmode = "require"\n'
            'iwiki_id = "personal"\n',
            "read",
        ),
        (
            'read = ["docs"]\nwrite = [""]\nprimary = "docs"\n'
            '[storage]\ntype = "postgres"\nhost = "db"\nport = 5432\n'
            'database = "iwiki"\nuser = "user"\nsslmode = "require"\n'
            'iwiki_id = "personal"\n',
            "write",
        ),
    ],
)
def test_resolve_binding_rejects_invalid_postgres_configuration(
    tmp_path, monkeypatch, config, message
):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text(config)
    monkeypatch.delenv("IWIKI_BASE_DIR", raising=False)
    _set_postgres_runtime(monkeypatch)

    with pytest.raises(base.BaseError, match=message):
        base.resolve_binding(str(proj))


def test_postgres_binding_diagnostic_and_repr_redact_password(tmp_path, monkeypatch):
    secret = "swordfish-database-secret"
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text(
        'read = ["docs"]\nwrite = ["docs"]\nprimary = "docs"\n'
        '[storage]\ntype = "postgres"\nhost = "db"\nport = 5432\n'
        'database = "iwiki"\nuser = "user"\nsslmode = "require"\n'
        'iwiki_id = "personal"\n'
    )
    _set_postgres_runtime(monkeypatch, password=secret)

    bind = base.resolve_binding(str(proj))

    assert secret not in repr(bind)
    assert "IWIKI_DB_PASSWORD" not in repr(bind)


def test_local_postgres_rejects_credentials_and_models_in_project_toml(
    tmp_path, monkeypatch
):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text(
        'read = ["docs"]\nwrite = ["docs"]\nprimary = "docs"\n'
        '[storage]\ntype = "postgres"\nhost = "db"\nport = 5432\n'
        'database = "iwiki"\nuser = "user"\nsslmode = "require"\n'
        'iwiki_id = "personal"\npassword = "must-not-be-used"\n'
        'embed_model = "must-not-be-used"\n'
    )
    _set_postgres_runtime(monkeypatch)

    with pytest.raises(base.BaseError, match="runtime environment"):
        base.resolve_binding(str(proj))


@pytest.mark.parametrize(
    "field",
    [
        'password = "must-not-be-used"',
        'llm_key = "must-not-be-used"',
        'embed_model = "must-not-be-used"',
        "embed_dimensions = 12",
        'rerank_model = "must-not-be-used"',
        "unexpected = true",
    ],
)
def test_local_postgres_rejects_non_project_top_level_keys(
    tmp_path, monkeypatch, field
):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text(
        field
        + '\nread = ["docs"]\nwrite = ["docs"]\nprimary = "docs"\n'
        '[storage]\ntype = "postgres"\nhost = "db"\nport = 5432\n'
        'database = "iwiki"\nuser = "user"\nsslmode = "require"\n'
        'iwiki_id = "personal"\n'
    )
    _set_postgres_runtime(monkeypatch)

    with pytest.raises(base.BaseError, match="not allowed") as caught:
        base.resolve_storage_binding(str(proj))

    assert "must-not-be-used" not in str(caught.value)


def test_explicit_git_keeps_unknown_top_level_fields(tmp_path, monkeypatch):
    b = _mkbase(tmp_path, "docs")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text(
        'unexpected = true\nread = ["docs"]\nwrite = ["docs"]\nprimary = "docs"\n'
        '[storage]\ntype = "git"\n'
    )
    monkeypatch.setenv("IWIKI_BASE_DIR", b)

    bind = base.resolve_storage_binding(str(proj))

    assert bind.storage == "git"
    assert bind.read == ("docs",)


@pytest.mark.parametrize(
    ("scope", "message"),
    [
        ('read = [1]\nwrite = ["docs"]\nprimary = "docs"\n', "read elements"),
        ('read = ["docs"]\nwrite = [1]\nprimary = "1"\n', "write elements"),
        ('read = ["docs"]\nwrite = ["docs"]\nprimary = 1\n', "primary must"),
    ],
)
def test_local_postgres_rejects_non_string_scope_values(
    tmp_path, monkeypatch, scope, message
):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text(
        scope
        + '[storage]\ntype = "postgres"\nhost = "db"\nport = 5432\n'
        'database = "iwiki"\nuser = "user"\nsslmode = "require"\n'
        'iwiki_id = "personal"\n'
    )
    _set_postgres_runtime(monkeypatch)

    with pytest.raises(base.BaseError, match=message):
        base.resolve_binding(str(proj))


def test_git_binding_preserves_legacy_numeric_scope_coercion(tmp_path, monkeypatch):
    b = _mkbase(tmp_path, "1")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text(
        'read = [1]\nwrite = ["1"]\nprimary = 1\n[storage]\ntype = "git"\n'
    )
    monkeypatch.setenv("IWIKI_BASE_DIR", b)

    bind = base.resolve_binding(str(proj))

    assert bind.read == ("1",)
    assert bind.write == ("1",)
    assert bind.primary == "1"


def test_resolve_list_write_with_primary_domain(tmp_path, monkeypatch):
    b = _mkbase(tmp_path, "backend", "shared")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text(
        'read = ["backend", "shared"]\n'
        'write = ["backend", "shared"]\n'
        'primary = "backend"\n'
    )
    monkeypatch.setenv("IWIKI_BASE_DIR", b)

    bind = base.resolve_binding(str(proj))

    assert bind.write == ("backend", "shared")
    assert bind.primary == "backend"
    assert base.writable_domains(bind) == ("backend", "shared")


def test_resolve_rejects_scalar_write_configuration(tmp_path, monkeypatch):
    b = _mkbase(tmp_path, "backend")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text('write = "backend"\n')
    monkeypatch.setenv("IWIKI_BASE_DIR", b)

    with pytest.raises(base.BaseError, match="write must be an array"):
        base.resolve_binding(str(proj))


def test_manual_binding_fixture_exposes_write_domains():
    bind = base.Binding(
        base="/wiki",
        read=("backend",),
        write=("backend",),
        primary="backend",
        project_dir="/project",
    )

    assert base.writable_domains(bind) == ("backend",)
    assert base.write_scope_error(bind, "backend") is None
    assert "outside bound write scope" in base.write_scope_error(bind, "other")["error"]
    assert repr(bind).startswith("Binding(")


def test_resolve_binding_deduplicates_write_domains(tmp_path, monkeypatch):
    b = _mkbase(tmp_path, "backend", "shared")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text(
        'read = ["backend", "shared"]\n'
        'write = ["backend", "shared", "backend"]\n'
        'primary = "backend"\n'
    )
    monkeypatch.setenv("IWIKI_BASE_DIR", b)

    bind = base.resolve_binding(str(proj))

    assert bind.write == ("backend", "shared")


@pytest.mark.parametrize(
    ("write", "primary", "message"),
    [
        ('["shared"]', '"backend"', "primary domain"),
        ('["backend", "hidden"]', '"backend"', "read scope"),
        ('["backend", "missing"]', '"backend"', "not found"),
    ],
)
def test_resolve_binding_rejects_invalid_write(
    tmp_path, monkeypatch, write, primary, message
):
    b = _mkbase(tmp_path, "backend", "shared", "hidden")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text(
        'read = ["backend", "shared"]\n'
        f"write = {write}\n"
        f"primary = {primary}\n"
    )
    monkeypatch.setenv("IWIKI_BASE_DIR", b)

    with pytest.raises(base.BaseError, match=message):
        base.resolve_binding(str(proj))


def test_resolve_binding_rejects_absolute_write_domain(tmp_path, monkeypatch):
    b = _mkbase(tmp_path, "backend")
    outside = tmp_path / "outside"
    outside.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text(
        f'base = "{b}"\n'
        f'read = ["{outside}"]\n'
        f'write = ["{outside}"]\nprimary = "{outside}"\n'
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
    (proj / ".iwiki.toml").write_text('read = ["a"]\nwrite = ["a"]\nprimary = "a"\n')
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
        str(proj), read=["x"], write=["x", "x"], primary="x"
    )
    bind = base.resolve_binding(str(proj))
    assert bind.write == ("x",)
    assert bind.read == ("x",)
    assert bind.primary == "x"


def test_write_project_config_preserves_fields_on_partial_updates(tmp_path, monkeypatch):
    b = _mkbase(tmp_path, "a", "b")
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.delenv("IWIKI_BASE_DIR", raising=False)
    (proj / ".iwiki.toml").write_text(
        f'base = "{b}"\nread = ["a", "b"]\nwrite = ["a"]\nprimary = "a"\n'
    )

    base.write_project_config(str(proj), write=["b"], primary="b")
    bind = base.resolve_binding(str(proj))
    assert bind.base == b
    assert bind.read == ("a", "b")
    assert bind.write == ("b",)

    base.write_project_config(str(proj), read=["b"])
    bind = base.resolve_binding(str(proj))
    assert bind.base == b
    assert bind.read == ("b",)
    assert bind.write == ("b",)


def test_write_project_config_preserves_unknown_lines_and_comments(
    tmp_path, monkeypatch
):
    b = _mkbase(tmp_path, "a", "b")
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.delenv("IWIKI_BASE_DIR", raising=False)
    (proj / ".iwiki.toml").write_text(
        f'# keep me\nbase = "{b}"\ncustom = "value"\nread = ["a"]\nwrite = ["a"]\nprimary = "a"\n'
    )

    base.write_project_config(str(proj), read=["b"], write=["b"], primary="b")

    text = (proj / ".iwiki.toml").read_text()
    assert "# keep me" in text
    assert 'custom = "value"' in text
    assert f'base = "{b}"' in text
    assert 'read = ["b"]' in text
    assert 'write = ["b"]' in text


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
        'write = ["old"]\nprimary = "old"\n'
    )

    base.write_project_config(str(proj), read=["new"], write=["new"], primary="new")

    text = (proj / ".iwiki.toml").read_text()
    bind = base.resolve_binding(str(proj))
    assert bind.read == ("new",)
    assert bind.write == ("new",)
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
