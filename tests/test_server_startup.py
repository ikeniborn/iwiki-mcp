from dataclasses import replace

import pytest

from iwiki_mcp import server
from iwiki_mcp.engine.config import Config, ConfigError
from iwiki_mcp.engine.embed import EmbedError


def _cfg() -> Config:
    return Config(
        base_url="https://example.test/v1",
        api_key="test-secret",
        embed_model="test-model",
        dimensions=2,
        chunk_size=512,
        chunk_overlap=64,
        summary_max=400,
        top_k=8,
        score_threshold=0.2,
        graph_depth=2,
        ignore=None,
    )


def test_main_loads_config_probes_and_runs_mcp_in_order(monkeypatch):
    cfg = _cfg()
    calls = []
    monkeypatch.setattr(server.sys, "argv", ["iwiki-mcp"])
    monkeypatch.setattr(server.Config, "load", lambda: calls.append("load") or cfg)
    monkeypatch.setattr(
        server,
        "probe_embedding_endpoint",
        lambda actual: calls.append(("probe", actual)),
    )
    monkeypatch.setattr(server.mcp, "run", lambda: calls.append("run"))

    server.main()

    assert calls == ["load", ("probe", cfg), "run"]


def test_main_applies_project_before_loading_config(monkeypatch, tmp_path):
    project = tmp_path / "project"
    calls = []
    monkeypatch.setenv("IWIKI_PROJECT_DIR", "original-project")
    monkeypatch.setattr(server.sys, "argv", ["iwiki-mcp", "--project", str(project)])

    def load():
        calls.append(("load", server.os.environ.get("IWIKI_PROJECT_DIR")))
        return _cfg()

    monkeypatch.setattr(server.Config, "load", load)
    monkeypatch.setattr(server, "probe_embedding_endpoint", lambda cfg: calls.append("probe"))
    monkeypatch.setattr(server.mcp, "run", lambda: calls.append("run"))

    server.main()

    assert calls == [("load", str(project.resolve())), "probe", "run"]


def test_main_blocks_mcp_and_reports_probe_failure(monkeypatch, capsys):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "https://example.test/v1/")
    monkeypatch.setenv("IWIKI_LLM_KEY", "test-secret")
    monkeypatch.setenv("IWIKI_EMBED_MODEL", "test-model")
    monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", "2")
    monkeypatch.setattr(server.sys, "argv", ["iwiki-mcp"])
    monkeypatch.setattr(server.Config, "load", _cfg)

    def fail_probe(cfg):
        raise EmbedError("embedding endpoint probe timed out")

    monkeypatch.setattr(server, "probe_embedding_endpoint", fail_probe)
    monkeypatch.setattr(server.mcp, "run", lambda: pytest.fail("mcp.run called"))

    with pytest.raises(SystemExit) as exc:
        server.main()

    captured = capsys.readouterr()
    assert exc.value.code == 1
    assert captured.out == ""
    assert "iwiki-mcp: startup failed" in captured.err
    assert "Embeddings endpoint: https://example.test/v1/embeddings" in captured.err
    assert "Model: test-model" in captured.err
    assert "Reason: embedding endpoint probe timed out" in captured.err
    assert "IWIKI_LLM_BASE_URL" in captured.err
    assert "IWIKI_LLM_KEY" in captured.err
    assert "IWIKI_EMBED_MODEL" in captured.err
    assert "IWIKI_EMBED_DIMENSIONS" in captured.err
    assert "test-secret" not in captured.err
    assert "Authorization" not in captured.err


def test_probe_failure_reports_exact_endpoint_used_by_loaded_config(monkeypatch, capsys):
    cfg = replace(_cfg(), base_url="https://example.test/v1//")
    endpoints = []
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "https://different.test/v1")
    monkeypatch.setattr(server.sys, "argv", ["iwiki-mcp"])
    monkeypatch.setattr(server.Config, "load", lambda: cfg)

    def fail_probe(actual):
        endpoints.append(f"{actual.base_url}/embeddings")
        raise EmbedError("controlled probe failure")

    monkeypatch.setattr(server, "probe_embedding_endpoint", fail_probe)
    monkeypatch.setattr(server.mcp, "run", lambda: pytest.fail("mcp.run called"))

    with pytest.raises(SystemExit):
        server.main()

    captured = capsys.readouterr()
    assert endpoints == ["https://example.test/v1///embeddings"]
    assert "Embeddings endpoint: https://example.test/v1///embeddings" in captured.err
    assert "https://different.test" not in captured.err


def test_startup_failure_redacts_key_from_all_diagnostic_values(monkeypatch, capsys):
    key = "same-secret"
    cfg = replace(
        _cfg(),
        base_url="https://same-secret@example.test/v1?token=same-secret",
        api_key=key,
        embed_model="model-same-secret",
    )
    monkeypatch.setenv("IWIKI_LLM_KEY", key)
    monkeypatch.setattr(server.sys, "argv", ["iwiki-mcp"])
    monkeypatch.setattr(server.Config, "load", lambda: cfg)

    def fail_probe(actual):
        raise EmbedError("controlled same-secret failure")

    monkeypatch.setattr(server, "probe_embedding_endpoint", fail_probe)
    monkeypatch.setattr(server.mcp, "run", lambda: pytest.fail("mcp.run called"))

    with pytest.raises(SystemExit):
        server.main()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert key not in captured.err
    assert "Embeddings endpoint: https://<redacted>@example.test/" in captured.err
    assert "Model: model-<redacted>" in captured.err
    assert "Reason: controlled <redacted> failure" in captured.err


def test_main_blocks_mcp_and_reports_config_failure(monkeypatch, capsys):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "https://example.test/v1/")
    monkeypatch.setenv("IWIKI_EMBED_MODEL", "")
    monkeypatch.setattr(server.sys, "argv", ["iwiki-mcp"])
    monkeypatch.setattr(
        server.Config,
        "load",
        lambda: (_ for _ in ()).throw(ConfigError("IWIKI_EMBED_MODEL must not be blank.")),
    )
    monkeypatch.setattr(
        server,
        "probe_embedding_endpoint",
        lambda cfg: pytest.fail("probe called"),
    )
    monkeypatch.setattr(server.mcp, "run", lambda: pytest.fail("mcp.run called"))

    with pytest.raises(SystemExit) as exc:
        server.main()

    captured = capsys.readouterr()
    assert exc.value.code == 1
    assert captured.out == ""
    assert "Embeddings endpoint: https://example.test/v1/embeddings" in captured.err
    assert "Model: <not set>" in captured.err
    assert "Reason: IWIKI_EMBED_MODEL must not be blank." in captured.err


def test_main_reports_missing_key_without_exposing_config(monkeypatch, capsys):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "https://example.test/v1/")
    monkeypatch.delenv("IWIKI_LLM_KEY", raising=False)
    monkeypatch.setenv("IWIKI_EMBED_MODEL", "test-model")
    monkeypatch.setattr(server.sys, "argv", ["iwiki-mcp"])
    monkeypatch.setattr(server.mcp, "run", lambda: pytest.fail("mcp.run called"))

    with pytest.raises(SystemExit) as exc:
        server.main()

    captured = capsys.readouterr()
    assert exc.value.code == 1
    assert captured.out == ""
    assert "Embeddings endpoint: https://example.test/v1/embeddings" in captured.err
    assert "Model: test-model" in captured.err
    assert "IWIKI_LLM_BASE_URL and IWIKI_LLM_KEY must be set" in captured.err
    assert "Config(" not in captured.err


def test_main_reports_unset_endpoint(monkeypatch, capsys):
    monkeypatch.delenv("IWIKI_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("IWIKI_LLM_KEY", raising=False)
    monkeypatch.delenv("IWIKI_EMBED_MODEL", raising=False)
    monkeypatch.setattr(server.sys, "argv", ["iwiki-mcp"])
    monkeypatch.setattr(server.mcp, "run", lambda: pytest.fail("mcp.run called"))

    with pytest.raises(SystemExit):
        server.main()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Embeddings endpoint: <not set>" in captured.err
    assert "Model: text-embedding-3-small" in captured.err


def test_help_exits_offline_without_loading_or_probing(monkeypatch, capsys):
    monkeypatch.setattr(server.sys, "argv", ["iwiki-mcp", "--help"])
    monkeypatch.setattr(
        server.Config,
        "load",
        lambda: pytest.fail("Config.load called for --help"),
    )
    monkeypatch.setattr(
        server,
        "probe_embedding_endpoint",
        lambda cfg: pytest.fail("probe called for --help"),
    )
    monkeypatch.setattr(server.mcp, "run", lambda: pytest.fail("mcp.run called"))

    with pytest.raises(SystemExit) as exc:
        server.main()

    captured = capsys.readouterr()
    assert exc.value.code == 0
    assert "usage:" in captured.out
    assert captured.err == ""


def test_unexpected_probe_error_propagates(monkeypatch):
    monkeypatch.setattr(server.sys, "argv", ["iwiki-mcp"])
    monkeypatch.setattr(server.Config, "load", _cfg)

    def fail_probe(cfg):
        raise RuntimeError("unexpected bug")

    monkeypatch.setattr(server, "probe_embedding_endpoint", fail_probe)
    monkeypatch.setattr(server.mcp, "run", lambda: pytest.fail("mcp.run called"))

    with pytest.raises(RuntimeError, match="unexpected bug"):
        server.main()
