"""Environment isolation coverage for code-graph configuration."""
from __future__ import annotations

from iwiki_mcp.codegraph.config import load_code_graph_config


def _write_config(project) -> None:
    project.joinpath(".iwiki.toml").write_text(
        "[code_graph]\n"
        "enabled = true\n"
        "max_file_bytes = 123\n",
        encoding="utf-8",
    )


def test_config_defaults_to_process_environment(tmp_path, monkeypatch):
    _write_config(tmp_path)
    monkeypatch.setenv("IWIKI_CODE_GRAPH_ENABLED", "false")

    config = load_code_graph_config(str(tmp_path))

    assert config.enabled is False


def test_explicit_empty_environment_ignores_hostile_process_values(
    tmp_path, monkeypatch
):
    _write_config(tmp_path)
    monkeypatch.setenv("IWIKI_CODE_GRAPH_ENABLED", "false")
    monkeypatch.setenv("IWIKI_CODE_GRAPH_MAX_FILE_BYTES", "process-sentinel")

    config = load_code_graph_config(str(tmp_path), environ={})

    assert config.enabled is True
    assert config.max_file_bytes == 123


def test_explicit_environment_overrides_opposite_process_value(
    tmp_path, monkeypatch
):
    _write_config(tmp_path)
    monkeypatch.setenv("IWIKI_CODE_GRAPH_ENABLED", "true")

    config = load_code_graph_config(
        str(tmp_path),
        environ={
            "IWIKI_CODE_GRAPH_ENABLED": "false",
            "IWIKI_CODE_GRAPH_MAX_FILE_BYTES": "456",
            "IWIKI_CODE_GRAPH_MAX_FILES": "789",
            "IWIKI_CODE_GRAPH_AUTO_REBUILD": "off",
        },
    )

    assert config.enabled is False
    assert config.max_file_bytes == 456
    assert config.max_total_files == 789
    assert config.auto_rebuild == "off"
