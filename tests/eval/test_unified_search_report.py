from __future__ import annotations

import json
import math

import pytest


def test_report_is_deterministic_atomic_and_sanitized(tmp_path):
    from eval.unified_search.report import write_reports

    evidence = {"decision": "blocked", "blocker": "Bearer SECRET_SENTINEL https://x.invalid/a /private/path",
                "exception": "SECRET_SENTINEL /private/error",
                "raw_parity": [{"case_id": "b", "passed": True}], "run_results": []}
    first = write_reports(evidence, tmp_path)
    first_json = first["json"].read_bytes()
    second = write_reports(evidence, tmp_path)
    assert first_json == second["json"].read_bytes()
    text = first["markdown"].read_text()
    assert "SECRET_SENTINEL" not in text
    assert "x.invalid" not in text
    assert "/private/path" not in text
    assert json.loads(first_json)["exception"] == "[redacted]"
    assert json.loads(first_json)["decision"] == "blocked"
    assert not list(tmp_path.glob(".*.tmp"))


def test_sanitizer_redacts_auth_dsn_and_non_json_values(tmp_path):
    from eval.unified_search.report import write_reports
    class SecretError(Exception):
        def __str__(self): return "SECRET_SENTINEL /private/error"
    class Unsupported:
        def __repr__(self): return "SECRET_SENTINEL /private/object"
    paths = write_reports({"Bearer": "SECRET_SENTINEL", "auth": "SECRET_SENTINEL",
                           "dsn": "host=private.example dbname=secret user=admin",
                           "scheme": "postgres:dbname=secret", "error": SecretError(),
                           "unsupported": Unsupported()}, tmp_path)
    rendered = paths["json"].read_text()
    assert "SECRET_SENTINEL" not in rendered
    assert "private.example" not in rendered
    assert "dbname=secret" not in rendered
    assert "[redacted-exception]" in rendered
    assert "[unsupported:Unsupported]" in rendered


def test_report_sanitizes_non_finite_floats_for_json_and_markdown(tmp_path):
    from eval.unified_search.report import write_reports
    paths = write_reports({"nan": math.nan, "positive": math.inf, "negative": -math.inf}, tmp_path)
    assert "NaN" not in paths["json"].read_text()
    assert "Infinity" not in paths["markdown"].read_text()
    assert paths["json"].read_text().count("[non-finite-float]") == 3


def test_atomic_write_cleans_temp_and_preserves_target_on_replace_failure(tmp_path, monkeypatch):
    from eval.unified_search import report
    target = tmp_path / "wiki-unified-search-evaluation.json"
    target.write_text("old")
    monkeypatch.setattr(report.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("replace failed")))
    with pytest.raises(OSError):
        report._atomic_write(target, "new")
    assert target.read_text() == "old"
    assert not list(tmp_path.glob(".iwiki-unified-*.tmp"))


def test_cli_missing_config_writes_blocked_report_and_maps_exit(tmp_path, monkeypatch):
    from eval.unified_search import __main__ as cli

    monkeypatch.delenv("IWIKI_CHAT_MODEL", raising=False)
    monkeypatch.delenv("IWIKI_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("IWIKI_LLM_KEY", raising=False)
    assert cli.main(["--output-dir", str(tmp_path)]) == 2
    evidence = json.loads((tmp_path / "wiki-unified-search-evaluation.json").read_text())
    assert evidence["decision"] == "blocked"


def test_cli_argument_validation_and_exit_mapping(tmp_path, monkeypatch):
    from eval.unified_search import __main__ as cli
    assert cli.main([]) == 2
    assert cli.main(["--output-dir", str(tmp_path), "--runs", "2"]) == 2
    monkeypatch.setenv("IWIKI_CHAT_MODEL", "model")
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "key")
    calls = []
    monkeypatch.setattr(cli, "build_evidence", lambda *args, **kwargs: calls.append(kwargs) or {"decision": "do_not_implement", "public_registry_contains_tool": False})
    assert cli.main(["--output-dir", str(tmp_path)]) == 1
    assert calls[-1]["public_registry_contains_tool"] is False
    monkeypatch.setattr(cli, "build_evidence", lambda *args, **kwargs: {"decision": "implement", "public_registry_contains_tool": False})
    assert cli.main(["--output-dir", str(tmp_path)]) == 0


def test_cli_registry_probe_flows_false_and_true_without_mutation(tmp_path, monkeypatch):
    from eval.unified_search import __main__ as cli
    monkeypatch.delenv("IWIKI_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("IWIKI_LLM_KEY", raising=False)
    monkeypatch.delenv("IWIKI_CHAT_MODEL", raising=False)
    observed = []
    monkeypatch.setattr(cli, "build_evidence", lambda *args, **kwargs: observed.append(kwargs["public_registry_contains_tool"]) or {"decision": "blocked"})
    monkeypatch.setattr(cli, "_public_registry_contains_tool", lambda: False)
    assert cli.main(["--output-dir", str(tmp_path)]) == 2
    monkeypatch.setattr(cli, "_public_registry_contains_tool", lambda: True)
    assert cli.main(["--output-dir", str(tmp_path)]) == 2
    assert observed == [False, True]


def test_configured_cli_uses_one_shared_client_context(tmp_path, monkeypatch):
    from eval.unified_search import __main__ as cli
    class Client:
        entered = exited = 0
        def __init__(self, **kwargs): self.kwargs = kwargs
        def __enter__(self): type(self).entered += 1; return self
        def __exit__(self, *args): type(self).exited += 1
        def post(self, *args, **kwargs): raise AssertionError("no network")
    monkeypatch.setenv("IWIKI_CHAT_MODEL", "model")
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "key")
    monkeypatch.setattr(cli.httpx, "Client", Client)
    monkeypatch.setattr(cli, "_public_registry_contains_tool", lambda: False)
    monkeypatch.setattr(cli, "build_evidence", lambda *args, **kwargs: {"decision": "implement"})
    assert cli.main(["--output-dir", str(tmp_path)]) == 0
    assert (Client.entered, Client.exited) == (1, 1)


def test_cli_help_lists_only_evaluation_arguments(capsys):
    from eval.unified_search import __main__ as cli
    assert cli.main(["--help"]) == 0
    help_text = capsys.readouterr().out
    assert {"--output-dir", "--runs", "--model"} <= set(help_text.split())
