"""Environment isolation coverage for code-graph runtime composition."""
from __future__ import annotations

from types import SimpleNamespace

from iwiki_mcp.codegraph.indexer import AdapterFactory
from iwiki_mcp.codegraph.runtime import CodeGraphRuntime


def _write_config(project) -> None:
    project.joinpath(".iwiki.toml").write_text(
        "[code_graph]\n"
        "enabled = true\n"
        'languages = ["python"]\n',
        encoding="utf-8",
    )


def _runtime(project, *, environ=None) -> CodeGraphRuntime:
    source = SimpleNamespace(
        base=str(project),
        project_dir=str(project),
        primary="docs",
    )
    factory = AdapterFactory(
        create=lambda _paths: None,
        extensions=(".py",),
        parser_version="parser:test",
        grammar_version="grammar:test",
        adapter_version="adapter:test",
    )
    return CodeGraphRuntime(
        source,
        adapter_factories={"python": factory},
        environ=environ,
    )


def test_runtime_defaults_to_process_environment(tmp_path, monkeypatch):
    _write_config(tmp_path)
    monkeypatch.setenv("IWIKI_CODE_GRAPH_ENABLED", "false")

    runtime = _runtime(tmp_path)

    assert runtime.config is not None
    assert runtime.config.enabled is False
    assert runtime.status()["code"] == "not_configured"


def test_runtime_explicit_empty_environment_ignores_hostile_process_values(
    tmp_path, monkeypatch
):
    _write_config(tmp_path)
    monkeypatch.setenv("IWIKI_CODE_GRAPH_ENABLED", "false")
    monkeypatch.setenv("IWIKI_CODE_GRAPH_MAX_FILES", "process-sentinel")

    runtime = _runtime(tmp_path, environ={})

    assert runtime.config is not None
    assert runtime.config.enabled is True
    assert runtime.status()["state"] == "missing"


def test_runtime_explicit_environment_overrides_opposite_process_value(
    tmp_path, monkeypatch
):
    _write_config(tmp_path)
    monkeypatch.setenv("IWIKI_CODE_GRAPH_ENABLED", "true")

    runtime = _runtime(
        tmp_path,
        environ={"IWIKI_CODE_GRAPH_ENABLED": "false"},
    )

    assert runtime.config is not None
    assert runtime.config.enabled is False
    assert runtime.status()["code"] == "not_configured"
