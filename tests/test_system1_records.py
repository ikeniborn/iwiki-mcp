import json
import os
import stat

import pytest

import iwiki_mcp.server as server
from iwiki_mcp.engine import frontmatter as fm
from iwiki_mcp.engine import system1
from iwiki_mcp.engine.config import Config, ConfigError

from tests.test_server_write_frontmatter import _patch
from tests.test_system1_guidance import _decision

BODY = (
    "# Keys\n\n## Overview\nLookup keys.\n\n"
    "## Table\n\n| Key | Default |\n|---|---|\n| A | 1 |\n"
)


def _enable(monkeypatch, log_path=None, assign=True):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setenv("IWIKI_SYSTEM1_BASE_URL", "http://system1/v1")
    monkeypatch.setenv("IWIKI_SYSTEM1_KEY", "system1-key")
    monkeypatch.setenv("IWIKI_SYSTEM1_GUIDANCE", "true")
    if assign:
        monkeypatch.setenv("IWIKI_SYSTEM1_ASSIGN_TYPE", "true")
    if log_path is not None:
        monkeypatch.setenv("IWIKI_SYSTEM1_DECISION_LOG", str(log_path))


def _records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_decision_log_must_be_absolute(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("IWIKI_SYSTEM1_DECISION_LOG", "relative/log.jsonl")
    with pytest.raises(ConfigError, match="absolute"):
        Config.load()


def test_assign_type_alone_requires_connection(monkeypatch):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setenv("IWIKI_SYSTEM1_ASSIGN_TYPE", "true")
    monkeypatch.delenv("IWIKI_SYSTEM1_BASE_URL", raising=False)
    monkeypatch.delenv("IWIKI_SYSTEM1_KEY", raising=False)
    with pytest.raises(ConfigError, match="IWIKI_SYSTEM1_BASE_URL"):
        Config.load()


def test_postgres_assigns_confident_type_and_logs_without_body(tmp_path, monkeypatch):
    log = tmp_path / "state" / "decisions.jsonl"
    _enable(monkeypatch, log)
    monkeypatch.setattr(system1, "_decide", lambda cfg, body: _decision("reference", 0.9))

    identity, rendered, warning = server._prepare_postgres_page(
        Config.load(), "d", "keys", BODY, source=None, type=None, tags=["Keys"],
        description=None, status=None)

    assert identity == "reference/keys"
    assert fm.split(rendered)[0]["type"] == "reference"
    assert "type 'reference' assigned by System One (p=0.90)" in warning
    (record,) = _records(log)
    assert record["type_source"] == "system1"
    assert record["final_type"] == "reference"
    assert record["identity"] == "reference/keys"
    assert record["requested_type"] is None and record["warned"] is False
    assert "Lookup keys" not in log.read_text()
    assert stat.S_IMODE(os.stat(log).st_mode) == 0o600


@pytest.mark.parametrize("decision", [
    _decision("runbook", 0.95), _decision("reference", 0.3),
    _decision(None, status="unavailable"),
])
def test_unusable_decision_keeps_default_type(tmp_path, monkeypatch, decision):
    log = tmp_path / "decisions.jsonl"
    _enable(monkeypatch, log)
    monkeypatch.delenv("IWIKI_CHAT_MODEL", raising=False)
    monkeypatch.setattr(system1, "_decide", lambda cfg, body: decision)

    identity, _rendered, warning = server._prepare_postgres_page(
        Config.load(), "d", "keys", BODY, source=None, type=None, tags=None,
        description=None, status=None)

    assert identity == "concept/keys"
    assert "defaulted to concept" in warning
    assert _records(log)[0]["type_source"] == "default"


def test_explicit_type_is_never_assigned_and_warning_is_logged(tmp_path, monkeypatch):
    log = tmp_path / "decisions.jsonl"
    _enable(monkeypatch, log)
    monkeypatch.setattr(system1, "_decide", lambda cfg, body: _decision("reference", 0.9))

    identity, _rendered, _warning = server._prepare_postgres_page(
        Config.load(), "d", "keys", BODY, source=None, type="concept", tags=None,
        description=None, status=None)

    assert identity == "concept/keys"
    (record,) = _records(log)
    assert (record["type_source"], record["requested_type"], record["predicted"],
            record["warned"]) == ("explicit", "concept", "reference", True)


def test_git_write_assigns_type_and_logs(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    log = tmp_path / "state" / "decisions.jsonl"
    _enable(monkeypatch, log)
    monkeypatch.setattr(system1, "_decide", lambda cfg, body: _decision("architecture", 0.8))

    result = server.wiki_write_page("d", "layout", BODY, source=None)

    assert "error" not in result
    assert "assigned by System One" in result["warning"]
    assert (tmp_path / "d" / "architecture" / "layout.md").exists()
    (record,) = _records(log)
    assert (record["backend"], record["type_source"], record["final_type"]) == (
        "git", "system1", "architecture")


def test_log_failure_never_breaks_the_write(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    _enable(monkeypatch, blocker / "decisions.jsonl")
    monkeypatch.setattr(system1, "_decide", lambda cfg, body: _decision("reference", 0.9))

    identity, _rendered, _warning = server._prepare_postgres_page(
        Config.load(), "d", "keys", BODY, source=None, type=None, tags=None,
        description=None, status=None)

    assert identity == "reference/keys"


def test_no_log_without_path(tmp_path, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.delenv("IWIKI_SYSTEM1_DECISION_LOG", raising=False)
    monkeypatch.setattr(system1, "_decide", lambda cfg, body: _decision("reference", 0.9))
    monkeypatch.chdir(tmp_path)

    server._prepare_postgres_page(Config.load(), "d", "keys", BODY, source=None,
                                  type=None, tags=None, description=None, status=None)

    assert list(tmp_path.iterdir()) == []
