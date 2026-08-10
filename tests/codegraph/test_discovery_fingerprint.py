from __future__ import annotations

import inspect
import os
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from iwiki_mcp.codegraph.config import CodeGraphConfig
from iwiki_mcp.codegraph.discovery import (
    DiscoveryError,
    DiscoverySnapshot,
    DiscoveryWarning,
    SourceFile,
    discover_sources,
)
from iwiki_mcp.codegraph.fingerprint import (
    FingerprintSet,
    compose_fingerprints,
    config_fingerprint,
    git_commit,
    git_dirty_marker,
    normalized_config,
    parser_fingerprint,
    source_fingerprint,
)


def _write(path: Path, content: bytes = b"pass\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _config(**overrides) -> CodeGraphConfig:
    return CodeGraphConfig.from_mapping(overrides)


def test_discovery_records_are_frozen() -> None:
    warning = DiscoveryWarning("ignored", "src/a.py", "ignore_rule")
    source = SourceFile("src/a.py", b"pass\n", "hash", 5)
    snapshot = DiscoverySnapshot((source,), (warning,), False)

    with pytest.raises(FrozenInstanceError):
        warning.code = "changed"
    with pytest.raises(FrozenInstanceError):
        source.path = "changed.py"
    with pytest.raises(FrozenInstanceError):
        snapshot.truncated = True


def test_discovery_rejects_symlinks_secrets_and_outside_paths(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside.py"
    project.mkdir()
    outside.write_bytes(b"OUTSIDE_SECRET_BYTES\n")
    fixture = Path(__file__).parents[1] / "fixtures" / "codegraph" / "security_paths"
    _write(project / "safe.py", (fixture / "safe.py").read_bytes())
    _write(project / ".env", b"KEY=project-secret\n")
    (project / "linked.py").symlink_to(outside)
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    _write(outside_dir / "nested.py", b"OUTSIDE_DIR_SECRET\n")
    (project / "linked-dir").symlink_to(outside_dir, target_is_directory=True)

    snapshot = discover_sources(project, _config(), extensions=(".py",))

    assert [item.path for item in snapshot.files] == ["safe.py"]
    assert {(item.code, item.path) for item in snapshot.warnings} >= {
        ("secret_excluded", ".env"),
        ("symlink_excluded", "linked.py"),
        ("symlink_excluded", "linked-dir"),
    }
    rendered = repr(snapshot)
    assert str(tmp_path) not in rendered
    assert "OUTSIDE_SECRET_BYTES" not in rendered
    assert "OUTSIDE_DIR_SECRET" not in rendered


def test_discovery_rejects_directory_swap_before_recursion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    _write(project / "safe.py")
    _write(project / "race" / "inside.py")
    _write(outside / "credentials.py", b"OUTSIDE_RACE_SECRET\n")
    displaced = project / "displaced"
    real_scandir = os.scandir
    real_open = os.open
    swapped = False

    def swap_directory() -> None:
        nonlocal swapped
        if swapped:
            return
        (project / "race").rename(displaced)
        (project / "race").symlink_to(outside, target_is_directory=True)
        swapped = True

    def swapping_scandir(path):
        if not isinstance(path, int) and Path(path) == project / "race":
            swap_directory()
        return real_scandir(path)

    def swapping_open(path, flags, *args, **kwargs):
        if path == "race" and kwargs.get("dir_fd") is not None:
            swap_directory()
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", swapping_scandir)
    monkeypatch.setattr(os, "open", swapping_open)

    snapshot = discover_sources(project, _config(), extensions=(".py",))

    assert [item.path for item in snapshot.files] == ["safe.py"]
    assert "credentials.py" not in repr(snapshot)
    assert "OUTSIDE_RACE_SECRET" not in repr(snapshot)


def test_discovery_fails_closed_without_descriptor_relative_primitives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write(project / "safe.py")
    from iwiki_mcp.codegraph import discovery

    monkeypatch.setattr(discovery, "_OPEN_SUPPORTS_DIR_FD", False)
    monkeypatch.setattr(discovery, "_SCANDIR_SUPPORTS_FD", False)
    monkeypatch.setattr(discovery, "_STAT_SUPPORTS_DIR_FD", False)

    def forbidden(*args, **kwargs):
        raise AssertionError("unsupported backend must not scan or read")

    monkeypatch.setattr(os, "scandir", forbidden)
    monkeypatch.setattr(os, "open", forbidden)

    with pytest.raises(DiscoveryError) as caught:
        discover_sources(project, _config(), extensions=(".py",))

    assert str(caught.value) == "secure_traversal_unavailable"
    assert caught.value.__cause__ is None


def test_discovery_fails_closed_without_no_follow_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write(project / "safe.py")
    from iwiki_mcp.codegraph import discovery

    monkeypatch.setattr(
        discovery, "_STAT_SUPPORTS_FOLLOW_SYMLINKS", False
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("capability gate must precede scan and open")

    monkeypatch.setattr(os, "scandir", forbidden)
    monkeypatch.setattr(os, "open", forbidden)

    with pytest.raises(DiscoveryError) as caught:
        discover_sources(project, _config(), extensions=(".py",))

    assert str(caught.value) == "secure_traversal_unavailable"
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("missing_flag", ["O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK"])
def test_discovery_fails_closed_without_required_open_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_flag: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write(project / "safe.py")
    monkeypatch.delattr(os, missing_flag, raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("capability gate must precede scan and open")

    monkeypatch.setattr(os, "scandir", forbidden)
    monkeypatch.setattr(os, "open", forbidden)

    with pytest.raises(DiscoveryError) as caught:
        discover_sources(project, _config(), extensions=(".py",))

    assert str(caught.value) == "secure_traversal_unavailable"
    assert caught.value.__cause__ is None


def test_discovery_opens_candidate_files_nonblocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not hasattr(os, "O_NONBLOCK"):
        pytest.skip("platform has no nonblocking open flag")
    project = tmp_path / "project"
    project.mkdir()
    _write(project / "safe.py")
    real_open = os.open

    def guarded_open(path, flags, *args, **kwargs):
        if path == "safe.py" and kwargs.get("dir_fd") is not None:
            assert flags & os.O_NONBLOCK
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)

    snapshot = discover_sources(project, _config(), extensions=(".py",))

    assert [item.path for item in snapshot.files] == ["safe.py"]


def test_discovery_applies_ignore_sources_builtins_and_hard_secrets(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write(project / ".gitignore", b"git_ignored.py\n!.env\n")
    _write(project / ".iwikiignore", b"wiki_ignored.py\n!credentials.py\n")
    _write(project / "git_ignored.py")
    _write(project / "wiki_ignored.py")
    _write(project / "configured.py")
    _write(project / "keep.py")
    _write(project / ".env", b"SECRET=1\n")
    _write(project / "credentials.py", b"TOKEN = 'secret'\n")
    _write(project / "vendor" / "vendored.py")
    _write(project / "generated" / "machine.py")

    snapshot = discover_sources(
        project,
        _config(exclude=["configured.py", "!.env", "!credentials.py"]),
        extensions=(".py",),
    )

    assert [item.path for item in snapshot.files] == ["keep.py"]
    warning_pairs = {(item.code, item.path) for item in snapshot.warnings}
    assert ("secret_excluded", ".env") in warning_pairs
    assert ("secret_excluded", "credentials.py") in warning_pairs
    assert ("ignored", "configured.py") in warning_pairs
    assert ("ignored", "git_ignored.py") in warning_pairs
    assert ("ignored", "wiki_ignored.py") in warning_pairs


@pytest.mark.parametrize("ignore_name", [".gitignore", ".iwikiignore"])
def test_discovery_fails_closed_before_reading_oversized_ignore_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ignore_name: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write(project / ignore_name, b"ignored.py\n")
    _write(project / "safe.py")
    from iwiki_mcp.codegraph import discovery

    def forbidden(*args, **kwargs):
        raise AssertionError("oversized ignore file must not be read")

    monkeypatch.setattr(discovery, "_read_contained_file", forbidden)

    with pytest.raises(DiscoveryError) as caught:
        discover_sources(
            project,
            _config(max_file_bytes=2),
            extensions=(".py",),
        )

    assert str(caught.value) == "ignore_file_too_large"
    assert caught.value.__cause__ is None


def test_discovery_fails_closed_when_ignore_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write(project / ".gitignore", b"ignored.py\n")
    _write(project / "safe.py")
    from iwiki_mcp.codegraph import discovery
    real_read = discovery._read_contained_file

    def changed(directory_descriptor, name, expected_stat):
        if name == ".gitignore":
            raise discovery._CandidateRejected("file_changed", "identity_changed")
        return real_read(directory_descriptor, name, expected_stat)

    monkeypatch.setattr(discovery, "_read_contained_file", changed)

    with pytest.raises(DiscoveryError) as caught:
        discover_sources(project, _config(), extensions=(".py",))

    assert str(caught.value) == "ignore_file_unavailable"
    assert caught.value.__cause__ is None


def test_discovery_uses_supplied_normalized_extensions_and_no_python_rules(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write(project / "alpha.CUSTOM", b"alpha")
    _write(project / "test_named.CUSTOM", b"not a language-specific test rule")
    _write(project / "ignored.py", b"python-specific content is irrelevant")

    snapshot = discover_sources(project, _config(), extensions=("custom",))

    assert [item.path for item in snapshot.files] == [
        "alpha.CUSTOM",
        "test_named.CUSTOM",
    ]
    module_source = inspect.getsource(__import__(
        "iwiki_mcp.codegraph.discovery", fromlist=["discovery"]
    ))
    assert "languages.python" not in module_source
    assert "tree_sitter" not in module_source
    assert '".py"' not in module_source


def test_discovery_honors_include_tests_without_language_filename_rules(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write(project / "src" / "main.code")
    _write(project / "tests" / "case.code")
    _write(project / "test" / "case.code")

    excluded = discover_sources(
        project, _config(include_tests=False), extensions=(".code",)
    )
    included = discover_sources(
        project, _config(include_tests=True), extensions=(".code",)
    )

    assert [item.path for item in excluded.files] == ["src/main.code"]
    assert [item.path for item in included.files] == [
        "src/main.code",
        "test/case.code",
        "tests/case.code",
    ]


def test_discovery_checks_size_and_count_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write(project / "a.code", b"a")
    _write(project / "b.code", b"bb")
    _write(project / "oversized.code", b"too large")

    from iwiki_mcp.codegraph import discovery

    observed = []
    real_read = discovery._read_contained_file

    def recording_read(directory_descriptor, name, expected_stat):
        observed.append(name)
        return real_read(directory_descriptor, name, expected_stat)

    monkeypatch.setattr(discovery, "_read_contained_file", recording_read)

    snapshot = discover_sources(
        project,
        _config(max_file_bytes=2, max_total_files=1),
        extensions=(".code",),
    )

    assert [item.path for item in snapshot.files] == ["a.code"]
    assert observed == ["a.code"]
    assert snapshot.truncated is True
    assert [(item.code, item.path) for item in snapshot.warnings] == [
        ("file_limit_reached", "b.code"),
    ]

    size_snapshot = discover_sources(
        project,
        _config(max_file_bytes=2, max_total_files=10),
        extensions=(".code",),
    )
    assert [item.path for item in size_snapshot.files] == ["a.code", "b.code"]
    assert ("file_too_large", "oversized.code") in {
        (item.code, item.path) for item in size_snapshot.warnings
    }
    assert observed.count("oversized.code") == 0


def test_discovery_truncates_deep_directory_tree_without_recursion_error(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    current = project
    created = []
    for _ in range(1_100):
        current = current / "d"
        os.mkdir(current)
        created.append(current)
    try:
        snapshot = discover_sources(project, _config(), extensions=(".py",))
    finally:
        for directory in reversed(created):
            os.rmdir(directory)

    assert snapshot.truncated is True
    assert "directory_depth_limit" in {
        warning.code for warning in snapshot.warnings
    }


def test_discovery_bounds_scanned_directories_and_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from iwiki_mcp.codegraph import discovery

    directory_project = tmp_path / "directories"
    directory_project.mkdir()
    for index in range(6):
        (directory_project / f"d{index}").mkdir()
    monkeypatch.setattr(discovery, "_MAX_SCANNED_DIRECTORIES", 3, raising=False)

    directory_snapshot = discover_sources(
        directory_project, _config(), extensions=(".py",)
    )

    assert directory_snapshot.truncated is True
    assert "directory_limit_reached" in {
        warning.code for warning in directory_snapshot.warnings
    }

    entry_project = tmp_path / "entries"
    entry_project.mkdir()
    for index in range(6):
        _write(entry_project / f"item-{index}.unsupported")
    monkeypatch.setattr(discovery, "_MAX_SCANNED_ENTRIES", 3, raising=False)

    entry_snapshot = discover_sources(
        entry_project, _config(), extensions=(".py",)
    )

    assert entry_snapshot.truncated is True
    assert "entry_limit_reached" in {
        warning.code for warning in entry_snapshot.warnings
    }


def test_entry_bound_stops_scandir_before_unbounded_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    from iwiki_mcp.codegraph import discovery

    monkeypatch.setattr(discovery, "_MAX_SCANNED_ENTRIES", 3)

    class Entry:
        def __init__(self, name: str) -> None:
            self.name = name

    class BoundedIterator:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def __iter__(self):
            for index in range(4):
                yield Entry(f"item-{index}")
            raise AssertionError("entry bound consumed a fifth directory entry")

    monkeypatch.setattr(os, "scandir", lambda descriptor: BoundedIterator())

    snapshot = discover_sources(project, _config(), extensions=(".py",))

    assert snapshot.files == ()
    assert snapshot.truncated is True
    assert [(warning.code, warning.path) for warning in snapshot.warnings] == [
        ("entry_limit_reached", "."),
    ]


def test_discovery_output_and_warnings_are_sorted_posix_and_relocatable(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "relocated" / "second"
    for project in (first, second):
        project.mkdir(parents=True)
        _write(project / "z.code", b"z")
        _write(project / "nested" / "a.code", b"a")
        _write(project / "secret.key", b"private")
        _write(project / "skip.code", b"skip")

    config = _config(exclude=["skip.code"])
    left = discover_sources(first, config, extensions=("code",))
    right = discover_sources(second, config, extensions=(".code",))

    assert left == right
    assert [item.path for item in left.files] == ["nested/a.code", "z.code"]
    assert all("\\" not in item.path for item in left.files)
    assert list(left.warnings) == sorted(
        left.warnings, key=lambda item: (item.path, item.code, item.detail)
    )


def test_discovery_errors_are_sanitized(tmp_path: Path) -> None:
    missing = tmp_path / "private" / "missing-project"

    with pytest.raises(DiscoveryError) as caught:
        discover_sources(missing, _config(), extensions=(".py",))

    assert str(missing) not in str(caught.value)
    assert str(caught.value) == "project_root_unavailable"
    assert caught.value.__cause__ is None

    project = tmp_path / "project"
    project.mkdir()

    class UnsafeExtensions:
        def __iter__(self):
            raise TypeError(f"unsafe extension source at {missing}")

    with pytest.raises(DiscoveryError) as invalid:
        discover_sources(project, _config(), extensions=UnsafeExtensions())

    assert str(invalid.value) == "invalid_extensions"
    assert invalid.value.__cause__ is None


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_extension_iteration_errors_are_sanitized(
    tmp_path: Path, error_type: type[Exception]
) -> None:
    project = tmp_path / "project"
    project.mkdir()

    class UnsafeExtensions:
        def __iter__(self):
            raise error_type(f"secret iterator detail at {project}")

    with pytest.raises(DiscoveryError) as caught:
        discover_sources(project, _config(), extensions=UnsafeExtensions())

    assert str(caught.value) == "invalid_extensions"
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("helper", ["root", "child"])
def test_directory_open_helpers_close_fd_when_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    helper: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    from iwiki_mcp.codegraph import discovery

    descriptor = 731
    closed = []
    monkeypatch.setattr(os, "open", lambda *args, **kwargs: descriptor)
    monkeypatch.setattr(os, "fstat", lambda fd: (_ for _ in ()).throw(OSError("secret")))
    monkeypatch.setattr(os, "close", closed.append)

    with pytest.raises(discovery._CandidateRejected) as caught:
        if helper == "root":
            discovery._open_root_directory(project)
        else:
            discovery._open_directory(descriptor + 1, "child", project.stat())

    assert caught.value.code in {"project_root_unavailable", "directory_unavailable"}
    assert closed == [descriptor]


def test_source_fingerprint_has_exact_canonical_vector_and_ignores_order() -> None:
    files = (
        SourceFile("b.ts", b"raw-b", "b" * 64, 5),
        SourceFile("a.py", b"raw-a", "a" * 64, 5),
    )

    expected = "ded495f4ef468e627b82271e70f87caa64f6624b80d85a08f2d8508bb2a7ca7e"
    assert source_fingerprint(files) == expected
    assert source_fingerprint(reversed(files)) == expected
    assert source_fingerprint(
        SourceFile(item.path, b"different raw bytes", item.content_hash, 19)
        for item in files
    ) == expected


def test_config_parser_and_composed_fingerprints_are_deterministic() -> None:
    left_config = _config(languages=["python"], exclude=["z/**", "a/**"])
    files = (
        SourceFile("b.py", b"b", "b" * 64, 1),
        SourceFile("a.py", b"a", "a" * 64, 1),
    )

    assert set(normalized_config(left_config)) == {
        "auto_rebuild",
        "enabled",
        "exclude",
        "include_tests",
        "languages",
        "max_file_bytes",
        "max_rebuild_seconds",
        "max_total_files",
    }
    parser = parser_fingerprint(
        languages=reversed(left_config.languages),
        schema_version=1,
        parser_version="tree-sitter@1",
        grammar_version="tree-sitter-python@1",
        adapter_version="python-adapter@1",
        resolver_version="resolver@1",
    )
    first = compose_fingerprints(
        files,
        left_config,
        repository_id="example-domain",
        git_commit="1" * 40,
        dirty_marker="clean",
        schema_version=1,
        parser_version="tree-sitter@1",
        grammar_version="tree-sitter-python@1",
        adapter_version="python-adapter@1",
        resolver_version="resolver@1",
    )
    second = compose_fingerprints(
        reversed(files),
        left_config,
        repository_id="example-domain",
        git_commit="1" * 40,
        dirty_marker="clean",
        schema_version=1,
        parser_version="tree-sitter@1",
        grammar_version="tree-sitter-python@1",
        adapter_version="python-adapter@1",
        resolver_version="resolver@1",
    )

    assert isinstance(first, FingerprintSet)
    assert first == second
    assert first.parser == parser
    assert first.source == source_fingerprint(files)
    assert first.config == config_fingerprint(left_config)
    assert len(first.inputs) == 64
    rendered = repr(first)
    assert str(Path.cwd()) not in rendered
    assert "raw-a" not in rendered


@pytest.mark.parametrize(
    ("changed", "field"),
    [
        ({"git_commit": "2" * 40}, "inputs"),
        ({"dirty_marker": "dirty"}, "inputs"),
        ({"schema_version": 2}, "parser"),
        ({"parser_version": "parser@2"}, "parser"),
        ({"grammar_version": "grammar@2"}, "parser"),
        ({"adapter_version": "adapter@2"}, "parser"),
        ({"resolver_version": "resolver@2"}, "parser"),
    ],
)
def test_composed_fingerprint_changes_for_each_versioned_input(changed, field) -> None:
    files = (SourceFile("a.py", b"a", "a" * 64, 1),)
    config = _config()
    kwargs = {
        "repository_id": "domain",
        "git_commit": "1" * 40,
        "dirty_marker": "clean",
        "schema_version": 1,
        "parser_version": "parser@1",
        "grammar_version": "grammar@1",
        "adapter_version": "adapter@1",
        "resolver_version": "resolver@1",
    }
    baseline = compose_fingerprints(files, config, **kwargs)
    mutated = compose_fingerprints(files, config, **(kwargs | changed))

    assert getattr(mutated, field) != getattr(baseline, field)
    assert mutated.inputs != baseline.inputs


def test_config_fingerprint_preserves_ordered_exclude_semantics() -> None:
    exclude_then_include = _config(exclude=["*.py", "!keep.py"])
    include_then_exclude = _config(exclude=["!keep.py", "*.py"])

    assert normalized_config(exclude_then_include)["exclude"] == [
        "*.py",
        "!keep.py",
    ]
    assert config_fingerprint(exclude_then_include) != config_fingerprint(
        include_then_exclude
    )


@pytest.mark.parametrize(
    "path",
    [
        "/private/a.py",
        "../a.py",
        "dir\\a.py",
        "a//b.py",
        "a/./b.py",
        "./a.py",
    ],
)
def test_source_file_rejects_non_relative_posix_paths(path: str) -> None:
    with pytest.raises(ValueError, match="invalid source path"):
        SourceFile(path, b"source", "a" * 64, 6)


def test_git_helpers_report_commit_and_dirty_state_without_content(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.invalid"],
        cwd=project,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Tests"], cwd=project, check=True
    )
    _write(project / "safe.py")
    subprocess.run(["git", "add", "safe.py"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=project, check=True)

    commit = git_commit(project)
    assert commit is not None and len(commit) == 40
    assert git_dirty_marker(project) == "clean"

    _write(project / "private-token.txt", b"DO_NOT_RETURN_THIS")
    assert git_dirty_marker(project) == "dirty"
    assert "DO_NOT_RETURN_THIS" not in git_dirty_marker(project)


def test_git_helpers_return_stable_sanitized_results_on_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_path = tmp_path / "private-repository"
    private_path.mkdir()

    def fail(*args, **kwargs):
        raise OSError(f"credential at {private_path}/secret.key")

    monkeypatch.setattr(subprocess, "run", fail)

    assert git_commit(private_path) is None
    assert git_dirty_marker(private_path) == "unavailable"
    assert str(private_path) not in repr((git_commit(private_path), git_dirty_marker(private_path)))


def test_fixture_safe_source_is_available() -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "codegraph" / "security_paths"
    copied = fixture / "safe.py"

    assert copied.read_text(encoding="utf-8") == "def safe():\n    return True\n"
