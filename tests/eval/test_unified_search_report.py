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
    assert "exactly 20" in help_text.lower()


def test_report_summarizes_sampling_quality_failures_calls_and_registry():
    from eval.unified_search.report import render_markdown

    rendered = render_markdown({
        "decision": "do_not_implement", "model": "test", "sampling": {
            "required_pairs": 20, "max_attempts": 30, "complete": False,
            "attempt_cap_exhausted": True,
        }, "attempt_counts": {"total_attempts": 3, "included_pairs": 2, "excluded_pairs": 1,
                                "per_case": {"a": {"total_attempts": 3, "included_pairs": 2, "excluded_pairs": 1,
                                                   "exclusion_reasons": {"failed_transport": 1}}},
                                "exclusion_reasons": {"failed_transport": 1}},
        "quality": {"aggregate_lower_bound": -0.1, "scenario_lower_bounds": {"a": -0.2},
                    "non_inferiority_margin": 0.15, "bootstrap_samples": 50_000, "bootstrap_seed": 20260826},
        "preflight": {"available": True, "status": "supported"},
        "protocol": {"expected_case_ids": ["a"], "required_pairs": 20, "max_attempts": 30},
        "gates": {"preflight": True, "retained_attempts": True},
        "aggregates": {"per_case": {"a": {"candidate_success_rate": 0.75, "baseline_success_rate": 0.5}}},
        "workflow_failure_counts": {"candidate": {"failed_response": 1}},
        "tool_calls": {"included_pair_candidate_mean": 2.0, "included_pair_baseline_mean": 4.0,
                       "included_pair_mean_difference": -2.0, "excluded_attempt_calls": {"candidate_total": 1}},
        "public_registry_contains_tool": False,
    })

    for heading in ("## Sampling", "## Quality", "## Failure counts", "## Secondary tool calls", "## Registry state"):
        assert heading in rendered

    for value in ("Total attempts: 3", "Included pairs: 2", "Excluded pairs: 1",
                  "Non-inferiority margin: 0.15", "Bootstrap samples: 50000",
                  "Bootstrap seed: 20260826", "Included-pair candidate mean: 2.0",
                  "Excluded-attempt calls", "a: candidate 0.75; baseline 0.5; bound -0.2",
                  "Preflight: supported (available=True)", "Protocol required pairs: 20",
                  "Decision gates", "a: total 3; included 2; excluded 1; reasons {\"failed_transport\": 1}"):
        assert value in rendered


def test_cli_capability_preflight_blocks_without_starting_sampling(tmp_path, monkeypatch):
    from eval.unified_search import __main__ as cli

    class Response:
        def json(self):
            return {"choices": [{"message": {"tool_calls": []}}]}

    class Client:
        def __init__(self, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def post(self, *_args, **_kwargs): return Response()

    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "key")
    monkeypatch.delenv("IWIKI_CHAT_MODEL", raising=False)
    monkeypatch.setattr(cli.httpx, "Client", Client)
    monkeypatch.setattr(cli, "_public_registry_contains_tool", lambda: False)
    called = []
    monkeypatch.setattr(cli, "build_evidence", lambda *args, **kwargs: called.append(kwargs) or {"decision": "blocked"})

    assert cli.main(["--output-dir", str(tmp_path), "--model", "explicit-model"]) == 2
    assert called[-1]["tool_calling_available"] is False
    assert called[-1]["post_factory"] is None
    assert called[-1]["preflight"]["status"] == "failed_response"


def test_cli_capability_preflight_accepts_actual_tool_call(tmp_path, monkeypatch):
    from eval.unified_search import __main__ as cli

    class Response:
        def json(self):
            return {"choices": [{"message": {"tool_calls": [{"id": "call-1", "type": "function",
                "function": {"name": "preflight", "arguments": "{}"}}]}}]}

    class Client:
        def __init__(self, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def post(self, *_args, **_kwargs): return Response()

    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "key")
    monkeypatch.delenv("IWIKI_CHAT_MODEL", raising=False)
    monkeypatch.setattr(cli.httpx, "Client", Client)
    monkeypatch.setattr(cli, "_public_registry_contains_tool", lambda: False)
    called = []
    monkeypatch.setattr(cli, "build_evidence", lambda *args, **kwargs: called.append(kwargs) or {"decision": "do_not_implement"})

    assert cli.main(["--output-dir", str(tmp_path), "--model", "explicit-model"]) == 1
    assert called[-1]["tool_calling_available"] is True
    assert called[-1]["post_factory"] is not None
    assert called[-1]["preflight"]["status"] == "supported"


def test_tool_calling_preflight_does_not_cap_reasoning_budget():
    from eval.unified_search import __main__ as cli

    class Response:
        def json(self):
            return {"choices": [{"message": {"tool_calls": [{"id": "call-1", "type": "function",
                "function": {"name": "preflight", "arguments": "{}"}}]}}]}

    class Client:
        def __init__(self):
            self.payload = None

        def post(self, *_args, **kwargs):
            self.payload = kwargs["json"]
            return Response()

    client = Client()
    assert cli._tool_calling_preflight(client, "model") == {"available": True, "status": "supported"}
    assert "max_tokens" not in client.payload


def test_tool_calling_preflight_requires_complete_tool_envelope():
    from eval.unified_search import __main__ as cli

    class Response:
        def __init__(self, call): self.call = call
        def json(self): return {"choices": [{"message": {"tool_calls": [self.call]}}]}

    class Client:
        def __init__(self, call): self.call = call
        def post(self, *_args, **_kwargs): return Response(self.call)

    malformed = {"function": {"name": "preflight"}}
    assert cli._tool_calling_preflight(Client(malformed), "model") == {"available": False, "status": "failed_response"}
    valid = {"id": "call-1", "type": "function", "function": {"name": "preflight", "arguments": "{}"}}
    assert cli._tool_calling_preflight(Client(valid), "model") == {"available": True, "status": "supported"}
    nonempty = {"id": "call-1", "type": "function", "function": {"name": "preflight", "arguments": "{\"extra\": 1}"}}
    assert cli._tool_calling_preflight(Client(nonempty), "model") == {"available": False, "status": "failed_response"}


@pytest.mark.parametrize("call", [
    {"id": "x", "type": "other", "function": {"name": "preflight", "arguments": "{}"}},
    {"id": "x", "type": "function", "function": {"name": "other", "arguments": "{}"}},
    {"id": "x", "type": "function", "function": {"name": "preflight"}},
    {"id": "x", "type": "function", "function": {"name": "preflight", "arguments": "[]"}},
    {"id": "x", "type": "function", "function": {"name": "preflight", "arguments": "bad"}},
])
def test_tool_calling_preflight_rejects_invalid_envelopes(call):
    from eval.unified_search import __main__ as cli

    class Response:
        def json(self): return {"choices": [{"message": {"tool_calls": [call]}}]}
    class Client:
        def post(self, *_args, **_kwargs): return Response()

    assert cli._tool_calling_preflight(Client(), "model") == {"available": False, "status": "failed_response"}


def test_http_500_is_safe_in_preflight_and_raised_by_sampling_post(tmp_path, monkeypatch):
    import httpx
    from eval.unified_search import __main__ as cli

    failed = httpx.Response(500, request=httpx.Request("POST", "https://example.invalid/v1/chat/completions"))
    class FailedResponse:
        def raise_for_status(self): failed.raise_for_status()
        def json(self): raise AssertionError("JSON must not be read after HTTP 500")
    class ValidResponse:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "preflight", "arguments": "{}"}}]}}]}
    class Client:
        def __init__(self, **_kwargs): self.calls = 0
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def post(self, *_args, **_kwargs):
            self.calls += 1
            return ValidResponse() if self.calls == 1 else FailedResponse()

    assert cli._tool_calling_preflight(type("Broken", (), {"post": lambda *_args, **_kwargs: FailedResponse()})(), "model") == {"available": False, "status": "failed_transport"}
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "key")
    monkeypatch.setenv("IWIKI_CHAT_MODEL", "model")
    monkeypatch.setattr(cli.httpx, "Client", Client)
    monkeypatch.setattr(cli, "_public_registry_contains_tool", lambda: False)
    captured = []
    monkeypatch.setattr(cli, "build_evidence", lambda *args, **kwargs: captured.append(kwargs) or {"decision": "do_not_implement"})
    assert cli.main(["--output-dir", str(tmp_path)]) == 1
    with pytest.raises(httpx.HTTPStatusError):
        captured[-1]["post_factory"](None, None, None)("/chat/completions", json={})


def test_malformed_2xx_sampling_json_is_failed_response(tmp_path, monkeypatch):
    from eval.unified_search import __main__ as cli
    from eval.unified_search.agent import run_agent_case
    from eval.unified_search.fixtures import FIXED_CASES

    class ValidResponse:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "preflight", "arguments": "{}"}}]}}]}
    class MalformedResponse:
        def raise_for_status(self): pass
        def json(self): raise ValueError("malformed")
    class Client:
        def __init__(self, **_kwargs): self.calls = 0
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def post(self, *_args, **_kwargs):
            self.calls += 1
            return ValidResponse() if self.calls == 1 else MalformedResponse()

    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "key")
    monkeypatch.setenv("IWIKI_CHAT_MODEL", "model")
    monkeypatch.setattr(cli.httpx, "Client", Client)
    monkeypatch.setattr(cli, "_public_registry_contains_tool", lambda: False)
    captured = []
    monkeypatch.setattr(cli, "build_evidence", lambda *args, **kwargs: captured.append(kwargs) or {"decision": "do_not_implement"})
    assert cli.main(["--output-dir", str(tmp_path)]) == 1
    post = captured[-1]["post_factory"](None, None, None)
    assert post("/chat/completions", json={}) == {}
    run = run_agent_case(FIXED_CASES[0], "candidate", "model", post)
    assert run.status == "failed_response"
    assert not run.tool_trace and run.client_visible_call_count == 1
