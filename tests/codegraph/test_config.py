"""Environment isolation coverage for code-graph configuration."""
from __future__ import annotations

import pytest

from iwiki_mcp.codegraph.config import CodeGraphConfigError, load_code_graph_config
from iwiki_mcp.codegraph.runtime import _invalid_config


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


def _write_bad_config(project, body: str) -> None:
    project.joinpath(".iwiki.toml").write_text(
        f"[code_graph]\n{body}\n", encoding="utf-8"
    )


def load_config_error(tmp_path, extra_key: str | None = None, **overrides) -> dict:
    """Load a broken config and map the raised error the way the runtime does.

    Mirrors `CodeGraphRuntime.__init__`'s `except CodeGraphConfigError` ->
    `_unavailable` -> `_invalid_config` path without needing a full runtime.
    `extra_key`, when given, names an unrecognized `[code_graph]` key.
    """
    lines = [f"{key} = {value!r}" for key, value in overrides.items()]
    if extra_key is not None:
        lines.append(f"{extra_key} = true")
    _write_bad_config(tmp_path, "\n".join(lines))
    with pytest.raises(CodeGraphConfigError) as failure:
        load_code_graph_config(str(tmp_path))
    return _invalid_config(failure.value.field)


def test_unknown_config_key_names_the_field(tmp_path):
    error = load_config_error(tmp_path, extra_key="reed_mode")

    assert error["field"] == "reed_mode"
    assert "reed_mode" not in error["error"]


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"enabled": "not-a-bool"}, "enabled"),
        ({"max_file_bytes": -1}, "max_file_bytes"),
        ({"auto_rebuild": "sometimes"}, "auto_rebuild"),
        ({"publish_mode": "ftp"}, "publish_mode"),
        ({"languages": []}, "languages"),
        ({"exclude": ["../outside"]}, "exclude"),
    ],
)
def test_invalid_config_value_names_its_own_field(tmp_path, overrides, field):
    error = load_config_error(tmp_path, **overrides)

    assert error["field"] == field
    assert error["code"] == "invalid_config"
