"""FastMCP Unit B code-graph tool integration."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import sqlite3

import pytest

from iwiki_mcp import server
from iwiki_mcp.codegraph import runtime as runtime_module
from iwiki_mcp.codegraph.models import CodeGraphError
from iwiki_mcp.codegraph.query import CodeGraphQueryError


class _FakeRuntime:
    def __init__(self, binding, calls):
        self.binding = binding
        self.calls = calls

    def status(self):
        self.calls.append(f"status:{self.binding.primary}")
        return {"domain": self.binding.primary}

    def index(self, *, force=False, languages=None):
        self.calls.append(f"index:{self.binding.primary}")
        return {
            "domain": self.binding.primary,
            "force": force,
            "languages": languages,
        }

    def search(self, query, *, kinds=None, path=None, languages=None, limit=20):
        self.calls.append(f"search:{self.binding.primary}")
        return {
            "domain": self.binding.primary,
            "query": query,
            "kinds": kinds,
            "path": path,
            "languages": languages,
            "limit": limit,
        }


def test_unit_b_handlers_use_the_bound_primary(seed_binding, monkeypatch):
    binding = replace(seed_binding, primary="backend")
    calls = []
    monkeypatch.setattr(server.base, "resolve_binding", lambda: binding)
    monkeypatch.setattr(
        server,
        "_code_runtime",
        lambda current: _FakeRuntime(current, calls),
    )

    assert server.wiki_code_status()["domain"] == "backend"
    assert server.wiki_code_index(force=True, languages=["python"])["domain"] == "backend"
    result = server.wiki_code_search(
        "run", kinds=["method"], path="src/", languages=["python"], limit=4
    )
    assert result == {
        "domain": "backend",
        "query": "run",
        "kinds": ["method"],
        "path": "src/",
        "languages": ["python"],
        "limit": 4,
    }
    assert calls == ["status:backend", "index:backend", "search:backend"]


def test_unit_b_handlers_fail_soft_without_primary(seed_without_primary, monkeypatch):
    monkeypatch.setattr(
        server.base, "resolve_binding", lambda: seed_without_primary
    )

    for result in (
        server.wiki_code_status(),
        server.wiki_code_index(),
        server.wiki_code_search("run"),
    ):
        assert result == {
            "error": "code graph is not configured",
            "code": "not_configured",
            "hint": "configure a primary domain and enable code_graph",
        }


def test_index_handler_validates_languages_before_binding(monkeypatch):
    def fail_binding():
        raise AssertionError("binding must not be resolved")

    monkeypatch.setattr(server.base, "resolve_binding", fail_binding)

    assert server.wiki_code_index(languages=["go"]) == {
        "error": "code graph configuration is invalid",
        "code": "invalid_config",
        "hint": "inspect code_graph project configuration",
    }


@pytest.mark.parametrize(
    "query",
    [
        "nul\0query",
        "lone-surrogate-\ud800",
        "é" * 2049,
        " ".join(f"t{i}" for i in range(65)),
    ],
)
def test_search_validation_precedes_binding_for_all_text_bounds(
    monkeypatch, query
):
    def fail_binding():
        raise AssertionError("binding must not be resolved")

    monkeypatch.setattr(server.base, "resolve_binding", fail_binding)

    assert server.wiki_code_search(query) == {
        "error": "code graph configuration is invalid",
        "code": "invalid_config",
        "hint": "inspect code_graph project configuration",
    }


@pytest.mark.parametrize(
    "call",
    [
        lambda: server.wiki_code_status(),
        lambda: server.wiki_code_index(force=True, languages=["python"]),
        lambda: server.wiki_code_search(
            "private-binding-query",
            kinds=["method"],
            path="src/private/",
            languages=["python"],
        ),
    ],
)
def test_code_handlers_sanitize_binding_errors(monkeypatch, caplog, call):
    def fail_binding():
        raise server.base.BaseError(
            "secret-binding-token /absolute/private/path"
        )

    monkeypatch.setattr(server.base, "resolve_binding", fail_binding)
    caplog.clear()

    result = call()

    assert result == {
        "error": "code graph is not configured",
        "code": "not_configured",
        "hint": "configure a primary domain and enable code_graph",
    }
    assert "secret-binding-token" not in repr(result)
    assert "secret-binding-token" not in caplog.text
    assert "/absolute/private/path" not in caplog.text
    assert "private-binding-query" not in caplog.text


def test_safe_maps_code_graph_errors_without_leaking_exception_text(
    seed_binding, monkeypatch, caplog
):
    class SecretStoreFailure(CodeGraphError):
        code = "store_failed"

    class FailingRuntime:
        def status(self):
            raise SecretStoreFailure("secret /absolute/path SQL SELECT credentials")

    monkeypatch.setattr(server.base, "resolve_binding", lambda: seed_binding)
    monkeypatch.setattr(server, "_code_runtime", lambda _binding: FailingRuntime())

    assert server.wiki_code_status() == {
        "error": "code graph store failed",
        "code": "store_failed",
        "hint": "inspect wiki_code_status and retry",
        "fresh": False,
    }
    assert "code_graph_handler" not in caplog.text


def test_search_handler_maps_invalid_config_without_leaking_text(
    seed_binding, monkeypatch, caplog
):
    class InvalidRuntime:
        def search(self, *_args, **_kwargs):
            raise CodeGraphQueryError("secret query and /absolute/path")

    monkeypatch.setattr(server.base, "resolve_binding", lambda: seed_binding)
    monkeypatch.setattr(server, "_code_runtime", lambda _binding: InvalidRuntime())

    assert server.wiki_code_search("run") == {
        "error": "code graph configuration is invalid",
        "code": "invalid_config",
        "hint": "inspect code_graph project configuration",
    }
    assert "code_graph_handler" not in caplog.text


@pytest.mark.parametrize(
    "call",
    [
        lambda: server.wiki_code_status(),
        lambda: server.wiki_code_index(force=True, languages=["python"]),
        lambda: server.wiki_code_search(
            "private-handler-query",
            kinds=["method"],
            path="src/private/",
            languages=["python"],
        ),
    ],
)
def test_code_handlers_sanitize_and_log_unexpected_runtime_factory_failure(
    seed_binding, monkeypatch, caplog, call
):
    def fail_runtime(_binding):
        raise RuntimeError(
            "secret-handler-token /absolute/private/path SELECT source SQL"
        )

    monkeypatch.setattr(server.base, "resolve_binding", lambda: seed_binding)
    monkeypatch.setattr(server, "_code_runtime", fail_runtime)
    caplog.clear()

    with caplog.at_level("ERROR", logger=server.__name__):
        result = call()

    assert result == {
        "error": "code graph rebuild failed",
        "code": "rebuild_failed",
        "hint": "inspect wiki_code_status and retry",
    }
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == server.__name__
        and record.getMessage().startswith("code_graph_handler ")
    ]
    assert len(messages) == 1
    assert messages[0].startswith(
        "code_graph_handler code=rebuild_failed count=1 duration_ms="
    )
    assert messages[0].removeprefix(
        "code_graph_handler code=rebuild_failed count=1 duration_ms="
    ).isdigit()
    assert "secret-handler-token" not in caplog.text
    assert "private-handler-query" not in caplog.text
    assert "/absolute/private/path" not in caplog.text
    assert "SELECT source SQL" not in caplog.text


def test_general_wiki_safe_keeps_existing_unexpected_error_contract():
    @server._safe
    def ordinary_wiki_tool():
        raise RuntimeError("ordinary wiki failure")

    assert ordinary_wiki_tool() == {
        "error": "ordinary wiki failure",
        "hint": "unexpected error; see server logs",
    }


def test_search_handler_logs_unexpected_failure_without_sensitive_values(
    seed_binding, production_runtime_factory, monkeypatch, caplog
):
    runtime = production_runtime_factory(seed_binding)
    assert runtime.index(force=True)["state"] == "ready"

    class FailingQuery:
        def search(self, *_args, **_kwargs):
            raise RuntimeError("secret-handler-query /private/path SELECT source")

    monkeypatch.setattr(server.base, "resolve_binding", lambda: seed_binding)
    monkeypatch.setattr(server, "_code_runtime", lambda _binding: runtime)
    monkeypatch.setattr(
        runtime_module,
        "CodeGraphQuery",
        lambda _domain: FailingQuery(),
    )
    caplog.clear()

    with caplog.at_level("ERROR", logger=runtime_module.__name__):
        result = server.wiki_code_search("private-handler-query")

    runtime.join_workers(timeout=5)
    assert result["code"] == "rebuild_failed"
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == runtime_module.__name__
        and record.getMessage().startswith("code_graph_query ")
    ]
    assert len(messages) == 1
    assert messages[0].startswith(
        "code_graph_query code=rebuild_failed count=1 duration_ms="
    )
    assert "secret-handler-query" not in caplog.text
    assert "private-handler-query" not in caplog.text
    assert "/private/path" not in caplog.text
    assert "SELECT source" not in caplog.text


def test_search_handler_maps_lazy_cursor_failure_without_leaking_text(
    seed_binding, production_runtime_factory, monkeypatch, caplog
):
    runtime = production_runtime_factory(seed_binding)
    assert runtime.index(force=True)["state"] == "ready"
    original_read_lease = runtime._store.read_lease

    class LazyFailureCursor:
        def __iter__(self):
            return self

        def __next__(self):
            raise sqlite3.DatabaseError(
                "secret lazy cursor /private/path SELECT source"
            )

    class ConnectionProxy:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, statement, parameters=()):
            if statement.lstrip().startswith("WITH entities"):
                return LazyFailureCursor()
            return self.connection.execute(statement, parameters)

    @contextmanager
    def failing_read_lease():
        with original_read_lease() as connection:
            yield ConnectionProxy(connection)

    monkeypatch.setattr(runtime._store, "read_lease", failing_read_lease)
    monkeypatch.setattr(server.base, "resolve_binding", lambda: seed_binding)
    monkeypatch.setattr(server, "_code_runtime", lambda _binding: runtime)
    caplog.clear()

    result = server.wiki_code_search("run", kinds=["method"])

    runtime.join_workers(timeout=5)
    assert result == {
        "error": "code graph store failed",
        "code": "store_failed",
        "hint": "inspect wiki_code_status and retry",
        "fresh": False,
        "results": [],
    }
    assert "secret lazy cursor" not in repr(result)
    assert "secret lazy cursor" not in caplog.text
    assert "/private/path" not in caplog.text
    assert "SELECT source" not in caplog.text


@pytest.mark.asyncio
async def test_fastmcp_registry_contains_only_unit_b_code_tools():
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}

    assert {"wiki_code_status", "wiki_code_index", "wiki_code_search"} <= set(tools)
    assert "wiki_code_context" not in tools
    assert set(tools["wiki_code_status"].inputSchema.get("properties", {})) == set()
    assert set(tools["wiki_code_index"].inputSchema["properties"]) == {
        "force", "languages",
    }
    assert set(tools["wiki_code_search"].inputSchema["properties"]) == {
        "query", "kinds", "path", "languages", "limit",
    }
    search_schema = tools["wiki_code_search"].inputSchema
    assert search_schema["required"] == ["query"]
    assert search_schema["properties"]["languages"]["default"] is None
    assert search_schema["properties"]["limit"]["default"] == 20
    assert "domain" not in search_schema["properties"]


def test_wiki_search_regression_keeps_existing_function():
    assert server.wiki_search.__name__ == "wiki_search"
