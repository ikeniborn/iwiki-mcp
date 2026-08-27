import hashlib
import inspect
import subprocess
import unicodedata
from dataclasses import asdict, fields
from typing import Literal, get_type_hints

import pytest

from iwiki_mcp.codegraph import models as models_module
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
    RelationRecord,
    SearchResult,
    SymbolRecord,
)
from iwiki_mcp.codegraph.languages.python import PythonAdapter


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
        publish_mode="sqlite",
        read_mode="sqlite",
        typescript_type_boost=False,
        max_snapshot_age_seconds=86400,
        max_batch_rows=1000,
        max_batch_bytes=1_000_000,
        publication_session_ttl_seconds=900,
        staging_retention_seconds=86400,
        staging_cleanup_limit=100,
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


def test_modes_and_snapshot_age_are_explicit():
    config = CodeGraphConfig.from_mapping(
        {
            "publish_mode": "mcp",
            "read_mode": "postgres",
            "max_snapshot_age_seconds": 0,
        }
    )

    assert config.publish_mode == "mcp"
    assert config.read_mode == "postgres"
    assert config.max_snapshot_age_seconds == 0


@pytest.mark.parametrize(
    "mapping",
    [
        {"publish_mode": "auto"},
        {"read_mode": "auto"},
        {"max_snapshot_age_seconds": -1},
        {"max_batch_rows": 0},
        {"max_batch_bytes": True},
        {"publication_session_ttl_seconds": 0},
        {"staging_retention_seconds": 0},
        {"staging_cleanup_limit": 0},
        {"dsn": "postgresql://secret"},
        {"mcp_token": "secret"},
        {"mcp_url": "https://example.invalid"},
        {"password": "secret"},
    ],
)
def test_code_graph_publication_config_rejects_invalid_or_secret_values(mapping):
    with pytest.raises(CodeGraphConfigError) as caught:
        CodeGraphConfig.from_mapping(mapping)

    assert "secret" not in str(caught.value)


def test_code_graph_config_has_no_secret_bearing_fields():
    field_names = {field.name for field in fields(CodeGraphConfig)}

    assert field_names.isdisjoint({"dsn", "mcp_token", "mcp_url", "password"})


@pytest.mark.parametrize(
    "mapping",
    [
        {"languages": ["ruby"]},
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


def test_schema_v2_identity_normalization_and_typed_records():
    assert models_module.NORMALIZER_VERSION == "casefold-token-v1"
    assert models_module.UNICODE_DATA_VERSION == unicodedata.unidata_version
    assert not hasattr(models_module, "LANGUAGE_PREFIXES")
    assert PythonAdapter.prefix == "py"
    expected_record_types = {
        FileRecord: {
            "file_id": str,
            "repository_id": str,
            "path": str,
            "path_casefold": str | None,
            "file_local_name": str,
            "file_name_tokens_casefold": str,
            "language": str,
            "content_hash": str,
            "parser_version": str,
            "size_bytes": int,
            "start_line": int,
            "end_line": int,
            "start_byte": int,
            "end_byte": int,
            "module_key": str,
            "module_id": str | None,
            "module_qualified_name": str | None,
            "module_local_name": str | None,
            "module_name_tokens_casefold": str | None,
        },
        SymbolRecord: {
            "symbol_id": str,
            "file_id": str,
            "kind": Literal[
                "class", "function", "async_function", "method",
                "interface", "type_alias", "enum",
            ],
            "qualified_name": str,
            "local_name": str,
            "name_tokens_casefold": str,
            "start_line": int,
            "end_line": int,
            "start_byte": int,
            "end_byte": int,
            "signature": str | None,
            "signature_casefold": str | None,
            "visibility": str | None,
            "content_hash": str,
            "metadata_json": str,
        },
        RelationRecord: {
            "relation_id": str,
            "source_file_id": str,
            "source_module_id": str | None,
            "source_symbol_id": str | None,
            "target_module_id": str | None,
            "target_symbol_id": str | None,
            "target_reference": str | None,
            "relation_type": Literal["DECLARES", "IMPORTS", "CALLS", "INHERITS"],
            "source_start_line": int,
            "source_end_line": int,
            "source_start_byte": int,
            "source_end_byte": int,
            "binding_name": str | None,
            "binding_kind": Literal["implicit_binding", "explicit_alias"] | None,
            "binding_name_tokens_casefold": str | None,
            "confidence": float,
            "resolution_state": Literal[
                "resolved", "partially_resolved", "unresolved", "ambiguous"
            ],
            "metadata_json": str,
        },
        SearchResult: {
            "entity_id": str,
            "entity_type": Literal["file", "module", "symbol"],
            "file_id": str | None,
            "module_id": str | None,
            "symbol_id": str | None,
            "kind": Literal[
                "file", "module", "class", "function", "async_function", "method",
                "interface", "type_alias", "enum",
            ],
            "qualified_name": str,
            "local_name": str,
            "signature": str | None,
            "path": str,
            "start_line": int,
            "end_line": int,
            "start_byte": int,
            "end_byte": int,
            "match": Literal[
                "qualified_exact",
                "local_exact",
                "alias_exact",
                "canonical_prefix",
                "alias_prefix",
                "canonical_lexical",
                "alias_lexical",
                "signature",
                "path",
            ],
            "matched_alias": str | None,
            "alias_ambiguous": bool,
            "alias_target_count": int,
        },
    }
    for record_type, expected_types in expected_record_types.items():
        assert tuple(field.name for field in fields(record_type)) == tuple(expected_types)
        assert get_type_hints(record_type) == expected_types
        parameters = inspect.signature(record_type).parameters
        assert all(
            parameters[name].default is inspect.Parameter.empty
            for name in expected_types
        )
    with pytest.raises(TypeError):
        FileRecord(path="a.py")
    with pytest.raises(TypeError):
        SymbolRecord()

    assert models_module.token_key("Straße_Name", "NAME Straße") == (
        "\x1fname\x1fstrasse\x1f"
    )
    assert models_module.token_key("e\N{COMBINING ACUTE ACCENT}") != (
        models_module.token_key("\N{LATIN SMALL LETTER E WITH ACUTE}")
    )
    assert models_module.compact_casefold(None) is None
    assert models_module.compact_casefold("ascii/path.py") is None
    assert models_module.compact_casefold("Straße.py") == "strasse.py"
    assert models_module.module_key("pkg/./module.py") == "pkg/module.py"
    for unsafe_path in (
        "",
        "/pkg/module.py",
        "../module.py",
        "pkg\\module.py",
        "a/\0b",
        "\ud800",
    ):
        with pytest.raises(ValueError):
            models_module.module_key(unsafe_path)

    file_digest = hashlib.sha256(
        b"file\x00domain\x00python\x00pkg/module.py"
    ).hexdigest()
    expected_file_id = f"py:file:{file_digest}"
    assert models_module.file_id(
        "python", "py", "domain", "pkg/module.py"
    ) == expected_file_id

    module_digest = hashlib.sha256(
        b"module\x00python\x00domain\x00pkg/module.py\x00pkg.module"
    ).hexdigest()
    expected_module_id = f"py:module:{module_digest}"
    assert models_module.module_id(
        "python", "py", "domain", "pkg/module.py", "pkg.module"
    ) == expected_module_id

    symbol_digest = hashlib.sha256(
        b"symbol\x00python\x00domain\x00pkg/module.py\x00pkg.module.run\x00function|run()"
    ).hexdigest()
    expected_symbol_id = f"py:symbol:{symbol_digest}"
    assert models_module.symbol_id(
        "python",
        "py",
        "domain",
        "pkg/module.py",
        "pkg.module.run",
        "function|run()",
    ) == expected_symbol_id

    relation_parts = (
        "relation",
        "python",
        "domain",
        expected_symbol_id,
        "CALLS",
        "3",
        "3",
        "10",
        "18",
        expected_symbol_id,
        "",
        "",
        "",
    )
    relation_digest = hashlib.sha256("\0".join(relation_parts).encode()).hexdigest()
    expected_relation_id = f"py:relation:{relation_digest}"
    assert models_module.relation_id(
        "python",
        "py",
        "domain",
        expected_symbol_id,
        "CALLS",
        3,
        3,
        10,
        18,
        expected_symbol_id,
        None,
        None,
        None,
    ) == expected_relation_id

    file_record = FileRecord(
        file_id=expected_file_id,
        repository_id="domain",
        path="pkg/module.py",
        path_casefold=None,
        file_local_name="module.py",
        file_name_tokens_casefold="\x1fmodule\x1fpy\x1f",
        language="python",
        content_hash="file-hash",
        parser_version="tree-sitter-python-v1",
        size_bytes=120,
        start_line=1,
        end_line=8,
        start_byte=0,
        end_byte=120,
        module_key="pkg/module.py",
        module_id=expected_module_id,
        module_qualified_name="pkg.module",
        module_local_name="module",
        module_name_tokens_casefold="\x1fmodule\x1fpkg\x1f",
    )
    symbol_record = SymbolRecord(
        symbol_id=expected_symbol_id,
        file_id=expected_file_id,
        kind="function",
        qualified_name="pkg.module.run",
        local_name="run",
        name_tokens_casefold="\x1fmodule\x1fpkg\x1frun\x1f",
        start_line=3,
        end_line=4,
        start_byte=10,
        end_byte=35,
        signature="function|run()",
        signature_casefold=None,
        visibility="public",
        content_hash="symbol-hash",
        metadata_json="{}",
    )
    relation_record = RelationRecord(
        relation_id=expected_relation_id,
        source_file_id=expected_file_id,
        source_module_id=None,
        source_symbol_id=expected_symbol_id,
        target_module_id=None,
        target_symbol_id=expected_symbol_id,
        target_reference=None,
        relation_type="CALLS",
        source_start_line=3,
        source_end_line=3,
        source_start_byte=10,
        source_end_byte=18,
        binding_name=None,
        binding_kind=None,
        binding_name_tokens_casefold=None,
        confidence=1.0,
        resolution_state="resolved",
        metadata_json="{}",
    )
    search_result = SearchResult(
        entity_id=expected_symbol_id,
        entity_type="symbol",
        file_id=expected_file_id,
        module_id=expected_module_id,
        symbol_id=expected_symbol_id,
        kind="function",
        qualified_name="pkg.module.run",
        local_name="run",
        signature="function|run()",
        path="pkg/module.py",
        start_line=3,
        end_line=4,
        start_byte=10,
        end_byte=35,
        match="qualified_exact",
        matched_alias=None,
        alias_ambiguous=False,
        alias_target_count=0,
    )

    assert file_record.module_id == expected_module_id
    assert (file_record.start_line, file_record.end_line) == (1, 8)
    assert (file_record.start_byte, file_record.end_byte) == (0, 120)
    assert symbol_record.symbol_id == search_result.entity_id
    assert relation_record.target_symbol_id == expected_symbol_id
    assert (relation_record.source_start_line, relation_record.source_end_line) == (3, 3)
    assert (relation_record.source_start_byte, relation_record.source_end_byte) == (10, 18)
    with pytest.raises(AttributeError):
        file_record.path = "other.py"


def test_schema_v2_records_do_not_synthesize_persisted_fields():
    record = RelationRecord(
        relation_id="py:relation:id",
        source_file_id="py:file:source",
        source_module_id="py:module:source",
        source_symbol_id=None,
        target_module_id="py:module:target",
        target_symbol_id=None,
        target_reference=None,
        relation_type="IMPORTS",
        source_start_line=1,
        source_end_line=1,
        source_start_byte=0,
        source_end_byte=8,
        binding_name="alias",
        binding_kind="explicit_alias",
        binding_name_tokens_casefold=None,
        confidence=1.0,
        resolution_state="resolved",
        metadata_json="{}",
    )

    assert record.binding_name_tokens_casefold is None
    assert asdict(record)["binding_name_tokens_casefold"] is None


def test_models_are_frozen():
    record = FileRecord(
        file_id="id",
        repository_id="repo",
        path="a.py",
        path_casefold=None,
        file_local_name="a.py",
        file_name_tokens_casefold="\x1fa\x1fpy\x1f",
        language="python",
        content_hash="hash",
        parser_version="v1",
        size_bytes=1,
        start_line=1,
        end_line=1,
        start_byte=0,
        end_byte=1,
        module_key="a.py",
        module_id=None,
        module_qualified_name=None,
        module_local_name=None,
        module_name_tokens_casefold=None,
    )
    with pytest.raises(AttributeError):
        record.path = "b.py"


def test_tree_sitter_packages_are_available():
    import tree_sitter
    import tree_sitter_bash
    import tree_sitter_language_pack

    assert tree_sitter is not None
    assert tree_sitter_bash is not None
    assert tree_sitter_language_pack is not None


def test_languages_accepts_typescript():
    config = CodeGraphConfig.from_mapping({"languages": ["python", "typescript"]})
    assert config.languages == ("python", "typescript")


def test_languages_rejects_unknown_language():
    with pytest.raises(CodeGraphConfigError, match="languages"):
        CodeGraphConfig.from_mapping({"languages": ["python", "ruby"]})


def test_languages_accepts_javascript():
    config = CodeGraphConfig.from_mapping({"languages": ["python", "javascript"]})
    assert config.languages == ("python", "javascript")


def test_languages_accepts_bash():
    config = CodeGraphConfig.from_mapping({"languages": ["python", "bash"]})
    assert config.languages == ("python", "bash")


def test_languages_rejects_unknown_language_message_lists_javascript():
    with pytest.raises(CodeGraphConfigError) as excinfo:
        CodeGraphConfig.from_mapping({"languages": ["ruby"]})
    assert str(excinfo.value) == (
        "code_graph.languages supports only python, typescript, javascript, bash"
    )


def test_typescript_type_boost_defaults_false():
    config = CodeGraphConfig.from_mapping({})
    assert config.typescript_type_boost is False


def test_typescript_type_boost_rejects_non_bool():
    with pytest.raises(CodeGraphConfigError, match="typescript_type_boost"):
        CodeGraphConfig.from_mapping({"typescript_type_boost": "yes"})
