import hashlib
import subprocess

import pytest

from iwiki_mcp.codegraph.config import (
    CodeGraphConfig,
    CodeGraphConfigError,
    load_code_graph_config,
)
from iwiki_mcp.codegraph.location import (
    CodeGraphLocationError,
    CodeGraphLocationResolver,
)
from iwiki_mcp.codegraph.models import (
    FileRecord,
    LANGUAGE_PREFIXES,
    file_id,
    relation_id,
    symbol_id,
)


def test_code_graph_config_defaults_and_mapping_values():
    assert CodeGraphConfig.from_mapping({}) == CodeGraphConfig(
        enabled=True,
        languages=("python",),
        auto_rebuild="bounded",
        max_rebuild_seconds=10,
        max_file_bytes=1_000_000,
        max_total_files=20_000,
        include_tests=True,
        exclude=(),
    )
    assert CodeGraphConfig.from_mapping(
        {
            "enabled": False,
            "languages": ["python"],
            "auto_rebuild": "off",
            "max_rebuild_seconds": 1,
            "max_file_bytes": 2,
            "max_total_files": 3,
            "include_tests": False,
            "exclude": ["generated/**"],
        }
    ).exclude == ("generated/**",)


@pytest.mark.parametrize(
    "mapping",
    [
        {"languages": ["typescript"]},
        {"incremental": True},
        {"database": "custom.sqlite3"},
        {"project_id": "project"},
        {"enabled": 1},
        {"max_file_bytes": True},
        {"max_total_files": 0},
        {"exclude": ["../private"]},
    ],
)
def test_code_graph_config_rejects_unsupported_or_unsafe_values(mapping):
    with pytest.raises(CodeGraphConfigError):
        CodeGraphConfig.from_mapping(mapping)


def test_load_code_graph_config_reads_toml_and_exactly_four_environment_overrides(
    tmp_path, monkeypatch
):
    (tmp_path / ".iwiki.toml").write_text(
        """[code_graph]
enabled = false
languages = ["python"]
auto_rebuild = "off"
max_file_bytes = 10
max_total_files = 20
include_tests = false
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("IWIKI_CODE_GRAPH_ENABLED", "true")
    monkeypatch.setenv("IWIKI_CODE_GRAPH_MAX_FILE_BYTES", "30")
    monkeypatch.setenv("IWIKI_CODE_GRAPH_MAX_FILES", "40")
    monkeypatch.setenv("IWIKI_CODE_GRAPH_AUTO_REBUILD", "bounded")
    monkeypatch.setenv("IWIKI_CODE_GRAPH_INCLUDE_TESTS", "true")

    config = load_code_graph_config(str(tmp_path))

    assert config.enabled is True
    assert config.auto_rebuild == "bounded"
    assert config.max_file_bytes == 30
    assert config.max_total_files == 40
    assert config.include_tests is False


def test_load_code_graph_config_rejects_malformed_project_toml(tmp_path):
    (tmp_path / ".iwiki.toml").write_text("[code_graph\nenabled = true\n", encoding="utf-8")

    with pytest.raises(CodeGraphConfigError, match="invalid project configuration"):
        load_code_graph_config(str(tmp_path))


def test_load_code_graph_config_rejects_non_utf8_project_toml(tmp_path):
    (tmp_path / ".iwiki.toml").write_bytes(b"\xff")

    with pytest.raises(CodeGraphConfigError, match="invalid project configuration"):
        load_code_graph_config(str(tmp_path))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("IWIKI_CODE_GRAPH_ENABLED", "yes"),
        ("IWIKI_CODE_GRAPH_MAX_FILE_BYTES", "not-an-integer"),
        ("IWIKI_CODE_GRAPH_MAX_FILES", "not-an-integer"),
        ("IWIKI_CODE_GRAPH_AUTO_REBUILD", "always"),
    ],
)
def test_load_code_graph_config_rejects_invalid_environment_override(
    tmp_path, monkeypatch, name, value
):
    monkeypatch.setenv(name, value)

    with pytest.raises(CodeGraphConfigError):
        load_code_graph_config(str(tmp_path))


def test_location_resolver_uses_fixed_base_local_paths_and_git_exclusion(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)

    paths = CodeGraphLocationResolver(str(tmp_path), "project", str(tmp_path)).resolve()

    graph_dir = tmp_path / ".iwiki"
    assert paths.database == graph_dir / "code-project.sqlite3"
    assert paths.wal == graph_dir / "code-project.sqlite3-wal"
    assert paths.shm == graph_dir / "code-project.sqlite3-shm"
    assert paths.lock == graph_dir / "code-project.lock"
    assert paths.metadata == graph_dir / "code-project.metadata.json"
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", ".iwiki/code-project.sqlite3"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert ignored.returncode == 0


def test_location_resolver_rejects_unsafe_domain(tmp_path):
    with pytest.raises(CodeGraphLocationError):
        CodeGraphLocationResolver(str(tmp_path), "../unsafe", str(tmp_path)).resolve()


def test_stable_ids_use_python_prefix_and_nul_delimited_hashes():
    assert LANGUAGE_PREFIXES == {"python": "py"}
    digest = hashlib.sha256(b"file\x00domain\x00python\x00pkg/module.py").hexdigest()
    assert file_id("domain", "python", "pkg/module.py") == f"py:file:{digest}"
    assert symbol_id("python", "domain", "pkg.module", "A.method", "(x: int)").startswith(
        "py:symbol:"
    )
    assert relation_id("python", "source", "calls", "3:2", "target").startswith(
        "py:relation:"
    )
    with pytest.raises(ValueError):
        file_id("domain", "typescript", "pkg/module.ts")


def test_models_are_frozen():
    record = FileRecord("id", "repo", "a.py", "python", "hash", "v1", 1)
    with pytest.raises(AttributeError):
        record.path = "b.py"


def test_tree_sitter_packages_are_available():
    import tree_sitter
    import tree_sitter_language_pack

    assert tree_sitter is not None
    assert tree_sitter_language_pack is not None
