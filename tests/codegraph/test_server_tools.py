"""FastMCP Unit B code-graph tool integration."""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import replace
import sqlite3

import pytest

from iwiki_mcp import server
from iwiki_mcp.codegraph import runtime as runtime_module
from iwiki_mcp.codegraph.config import CodeGraphConfig
from iwiki_mcp.codegraph.models import CodeGraphError
from iwiki_mcp.codegraph.query import CodeGraphQueryError


class _FakeRuntime:
    def __init__(self, binding, calls):
        self.binding = binding
        self.calls = calls
        self.config = None

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

    def context(
        self,
        seeds,
        *,
        direction="both",
        depth=1,
        relations=None,
        include_source=False,
        include_wiki=True,
        max_nodes=50,
        max_files=20,
        max_source_bytes=200_000,
    ):
        self.calls.append(f"context:{self.binding.primary}")
        return {
            "domain": self.binding.primary,
            "seeds": seeds,
            "direction": direction,
            "depth": depth,
            "relations": relations,
            "include_source": include_source,
            "include_wiki": include_wiki,
            "max_nodes": max_nodes,
            "max_files": max_files,
            "max_source_bytes": max_source_bytes,
        }


def test_code_handlers_use_the_bound_primary(seed_binding, monkeypatch):
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
    context = server.wiki_code_context(
        ["py:symbol:" + "a" * 64],
        direction="out",
        depth=2,
        relations=["CALLS"],
        include_source=True,
        include_wiki=False,
        max_nodes=4,
        max_files=3,
        max_source_bytes=1024,
    )
    assert context == {
        "domain": "backend",
        "seeds": ["py:symbol:" + "a" * 64],
        "direction": "out",
        "depth": 2,
        "relations": ["CALLS"],
        "include_source": True,
        "include_wiki": False,
        "max_nodes": 4,
        "max_files": 3,
        "max_source_bytes": 1024,
    }
    assert calls == [
        "status:backend",
        "index:backend",
        "search:backend",
        "context:backend",
    ]


def test_code_handlers_fail_soft_without_primary(
    seed_without_primary, monkeypatch
):
    monkeypatch.setattr(
        server.base, "resolve_binding", lambda: seed_without_primary
    )

    for result in (
        server.wiki_code_status(),
        server.wiki_code_index(),
        server.wiki_code_search("run"),
        server.wiki_code_context(["py:file:" + "a" * 64]),
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


def test_index_handler_accepts_every_known_language(seed_binding, monkeypatch):
    # Regression for C2: wiki_code_index's languages gate used to reject
    # every language except the literal "python" -- a fifth python-only
    # gate the plan's inventory (config.py, runtime.snapshot,
    # runtime.index, sqlite_adapter.begin) missed. It must now accept
    # every iwiki_mcp.codegraph.config.KNOWN_LANGUAGES member.
    calls = []
    monkeypatch.setattr(server.base, "resolve_binding", lambda: seed_binding)
    monkeypatch.setattr(
        server, "_code_runtime", lambda current: _FakeRuntime(current, calls)
    )

    assert server.wiki_code_index(languages=["typescript"])["languages"] == [
        "typescript"
    ]
    assert server.wiki_code_index(languages=["python", "typescript"])[
        "languages"
    ] == ["python", "typescript"]
    assert "error" not in server.wiki_code_index(languages=["typescript"])


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


def test_search_handler_accepts_explicit_typescript_filter(
    seed_binding, monkeypatch
):
    # Regression for C3 (item 1): the binding-free pre-validation gate used
    # to default configured_languages to ("python",), so an explicit
    # languages=["typescript"] filter raised "unsupported language" before
    # the request ever reached the project's real code_graph.languages
    # config -- even for a project that configures typescript. It must
    # reach the runtime instead.
    calls = []
    monkeypatch.setattr(server.base, "resolve_binding", lambda: seed_binding)
    monkeypatch.setattr(
        server, "_code_runtime", lambda current: _FakeRuntime(current, calls)
    )

    result = server.wiki_code_search("run", languages=["typescript"])

    assert "error" not in result
    assert result["languages"] == ["typescript"]
    assert calls == [f"search:{seed_binding.primary}"]


def test_search_handler_defers_languages_to_the_active_snapshot(monkeypatch):
    # Regression for the hosted read path: the PostgreSQL call site used
    # to validate the languages filter against the server's own project
    # directory, which for the HTTP transport is just wherever
    # server.toml lives -- silently dropping every non-python row of a
    # snapshot the client published as python + javascript. The reader
    # now calls back with the snapshot's declared languages instead.
    postgres_binding = server.base.PostgresBinding(
        host="localhost",
        port=5432,
        database="iwiki_test",
        user="tester",
        sslmode="disable",
        iwiki_id="iwiki-test",
        read=("project",),
        write=("project",),
        primary="project",
        project_dir="/tmp/does-not-matter",
        embed_model="test-embed",
        embed_dimensions=2,
        rerank_model="test-rerank",
        password="secret",
    )
    monkeypatch.setattr(
        server, "_resolved_binding", lambda: postgres_binding
    )

    def fail_config(project_dir):
        raise AssertionError("hosted reads must not read a project config")

    monkeypatch.setattr(
        server._codegraph_config, "load_code_graph_config", fail_config
    )
    captured = []

    class _FakeReader:
        """Answer as a snapshot published with python + javascript."""

        def search(self, build_request):
            captured.append(build_request(("javascript", "python")))
            return {"results": []}

    monkeypatch.setattr(server, "_postgres_code_reader", lambda bind: _FakeReader())

    result = server.wiki_code_search("run")

    assert "error" not in result
    assert len(captured) == 1
    assert set(captured[0].languages) == {"javascript", "python"}

    captured.clear()
    result = server.wiki_code_search("run", languages=["javascript"])

    assert "error" not in result
    assert captured[0].languages == ("javascript",)

    captured.clear()
    assert server.wiki_code_search("run", languages=["typescript"]) == {
        "error": "language not available in the active snapshot",
        "code": "unsupported_language",
        "hint": "the active snapshot declares: javascript, python",
    }
    assert captured == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"seeds": []},
        {"seeds": ["not-an-entity-id"]},
        {"seeds": ["py:file:" + "a" * 64], "direction": "sideways"},
        {"seeds": ["py:file:" + "a" * 64], "depth": 4},
        {"seeds": ["py:file:" + "a" * 64], "relations": ["DOCUMENTED_BY"]},
        {"seeds": ["py:file:" + "a" * 64], "include_source": 1},
        {"seeds": ["py:file:" + "a" * 64], "max_nodes": 51},
        {"seeds": ["py:file:" + "a" * 64], "max_files": 21},
        {"seeds": ["py:file:" + "a" * 64], "max_source_bytes": 200_001},
    ],
)
def test_context_validation_precedes_binding(monkeypatch, arguments):
    def fail_binding():
        raise AssertionError("binding must not be resolved")

    monkeypatch.setattr(server.base, "resolve_binding", fail_binding)

    assert server.wiki_code_context(**arguments) == {
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
        lambda: server.wiki_code_context(["py:module:" + "a" * 64]),
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
        lambda: server.wiki_code_context(["py:module:" + "a" * 64]),
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
    assert server.wiki_status()["primary"] == seed_binding.primary


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
            if statement.startswith("/* iwiki-rank:"):
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
async def test_fastmcp_registry_has_exact_code_tools():
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}

    code_tools = {
        name: tool for name, tool in tools.items() if name.startswith("wiki_code_")
    }
    assert set(code_tools) == {
        "wiki_code_status",
        "wiki_code_index",
        "wiki_code_search",
        "wiki_code_context",
        "wiki_code_publish_begin",
        "wiki_code_publish_batch",
        "wiki_code_publish_finalize",
        "wiki_code_publish_abort",
    }
    assert all(
        "domain" not in tool.inputSchema.get("properties", {})
        for tool in code_tools.values()
    )
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
    assert set(tools["wiki_code_publish_batch"].inputSchema["properties"]) == {
        "session_id", "kind", "ordinal", "rows", "payload_hash",
    }
    assert set(tools["wiki_code_publish_finalize"].inputSchema["properties"]) == {
        "session_id",
    }
    assert all(
        "iwiki_id" not in tool.inputSchema.get("properties", {})
        for tool in code_tools.values()
    )
    context_schema = tools["wiki_code_context"].inputSchema
    assert context_schema["required"] == ["seeds"]
    assert set(context_schema["properties"]) == {
        "seeds",
        "direction",
        "depth",
        "relations",
        "include_source",
        "include_wiki",
        "max_nodes",
        "max_files",
        "max_source_bytes",
    }
    assert "symbols" not in context_schema["properties"]
    assert context_schema["properties"]["direction"]["default"] == "both"
    assert context_schema["properties"]["include_source"]["default"] is False
    assert context_schema["properties"]["include_wiki"]["default"] is True


def test_wiki_search_regression_keeps_existing_function():
    assert server.wiki_search.__name__ == "wiki_search"


def test_adapter_factories_include_typescript():
    factories = server._code_graph_adapter_factories("domain")

    assert "typescript" in factories
    assert factories["typescript"].extensions == (".ts", ".tsx")
    adapter = factories["typescript"].create(("a.ts",))
    assert adapter.language == "typescript"


def test_javascript_factory_is_registered():
    factories = server._code_graph_adapter_factories("domain")
    factory = factories["javascript"]
    assert factory.extensions == (".js", ".jsx", ".mjs", ".cjs")
    assert factory.adapter_version == "javascript-adapter-v1"
    adapter = factory.create(())
    assert adapter.language == "javascript"
    assert adapter.parser_version.startswith("tree-sitter-typescript:")


@pytest.mark.parametrize(
    ("handler", "args"),
    [
        (lambda: server.wiki_code_publish_begin({}), ()),
        (lambda: server.wiki_code_publish_batch("s", "files", 0, [], "h"), ()),
        (lambda: server.wiki_code_publish_finalize("s"), ()),
        (lambda: server.wiki_code_publish_abort("s"), ()),
    ],
)
def test_publication_tools_reject_git_sqlite_runtime(
    seed_binding, monkeypatch, handler, args
):
    monkeypatch.setattr(server.base, "resolve_binding", lambda: seed_binding)

    assert handler(*args)["error"] == "unsupported_storage"


def test_publish_mode_selects_exactly_one_publisher(seed_binding, monkeypatch):
    selected = []

    class _Publisher:
        def __init__(self, mode):
            selected.append(mode)

    monkeypatch.setattr(server.base, "resolve_binding", lambda: seed_binding)
    monkeypatch.setattr(
        server,
        "_code_publisher_factories",
        {
            "sqlite": lambda binding, config: _Publisher("sqlite"),
            "postgres": lambda binding, config: _Publisher("postgres"),
            "mcp": lambda binding, config: _Publisher("mcp"),
        },
    )

    for mode in ("sqlite", "postgres", "mcp"):
        server._code_publisher(seed_binding, mode)

    assert selected == ["sqlite", "postgres", "mcp"]


class _RecordingPublisher:
    def __init__(self):
        self.calls = []

    def begin(self, header):
        self.calls.append(("begin", header.repository_id))
        return _publication_session()

    def publish_batch(self, session, batch):
        self.calls.append(("batch", batch.kind, batch.ordinal))
        return {"accepted": True}

    def finalize(self, session):
        self.calls.append(("finalize", session.session_id))
        return {"state": "ready", "snapshot_revision": "sha256:remote"}

    def abort(self, session):
        self.calls.append(("abort", session.session_id))
        return {"state": "aborted"}


def _publication_session():
    from iwiki_mcp.codegraph.publication import PublicationSession

    return PublicationSession(
        session_id="remote-session",
        lease_expires_at="2026-08-16T10:00:00+00:00",
        base_snapshot_revision=None,
        base_markdown_token=0,
    )


def _exported_snapshot():
    from iwiki_mcp.codegraph.publication import SnapshotHeader

    rows = {
        "repositories": [{"repository_id": "project", "state": "ready"}],
        "files": [{"file_id": "py:file:0", "repository_id": "project"}],
        "symbols": [],
        "relations": [],
    }
    header = SnapshotHeader(
        protocol_version=1,
        schema_version=2,
        repository_id="project",
        source_fingerprint="source",
        parser_fingerprint="parser",
        normalizer_version="normalizer-1",
        unicode_data_version="15.1",
        languages=("python",),
        expected_counts={kind: len(value) for kind, value in rows.items()},
        graph_payload_revision="sha256:payload",
    )
    return header, rows


def test_index_publishes_through_the_selected_remote_publisher(
    seed_binding, monkeypatch
):
    from iwiki_mcp.codegraph.config import CodeGraphConfig

    publisher = _RecordingPublisher()

    class _Runtime:
        config = CodeGraphConfig(publish_mode="postgres")

        def index(self, *, force=False, languages=None):
            return {"state": "ready", "revision": "sha256:local"}

        def export_snapshot(self):
            return _exported_snapshot()

    monkeypatch.setattr(server.base, "resolve_binding", lambda: seed_binding)
    monkeypatch.setattr(server, "_code_runtime", lambda _binding: _Runtime())
    monkeypatch.setitem(
        server._code_publisher_factories,
        "postgres",
        lambda binding, config: publisher,
    )

    result = server.wiki_code_index()

    assert result["state"] == "ready"
    assert result["publication"] == {
        "state": "ready",
        "snapshot_revision": "sha256:remote",
    }
    assert publisher.calls[0] == ("begin", "project")
    assert ("finalize", "remote-session") == publisher.calls[-1]
    assert any(call[0] == "batch" for call in publisher.calls)


def test_index_aborts_the_remote_session_on_a_rejected_batch(
    seed_binding, monkeypatch
):
    from iwiki_mcp.codegraph.config import CodeGraphConfig

    publisher = _RecordingPublisher()
    publisher.publish_batch = lambda session, batch: {
        "error": "batch_conflict",
        "hint": "resend the identical batch or begin a new session",
    }

    class _Runtime:
        config = CodeGraphConfig(publish_mode="mcp")

        def index(self, *, force=False, languages=None):
            return {"state": "ready"}

        def export_snapshot(self):
            return _exported_snapshot()

    monkeypatch.setattr(server.base, "resolve_binding", lambda: seed_binding)
    monkeypatch.setattr(server, "_code_runtime", lambda _binding: _Runtime())
    monkeypatch.setitem(
        server._code_publisher_factories,
        "mcp",
        lambda binding, config: publisher,
    )

    result = server.wiki_code_index()

    assert result["publication"]["error"] == "batch_conflict"
    assert publisher.calls[-1][0] == "abort"


def test_sqlite_publish_mode_stays_on_the_local_atomic_path(
    seed_binding, monkeypatch
):
    from iwiki_mcp.codegraph.config import CodeGraphConfig

    class _Runtime:
        config = CodeGraphConfig()

        def index(self, *, force=False, languages=None):
            return {"state": "ready", "revision": "sha256:local"}

        def export_snapshot(self):
            raise AssertionError("SQLite mode must not export a snapshot")

    monkeypatch.setattr(server.base, "resolve_binding", lambda: seed_binding)
    monkeypatch.setattr(server, "_code_runtime", lambda _binding: _Runtime())

    assert server.wiki_code_index() == {
        "state": "ready", "revision": "sha256:local"
    }


def test_export_snapshot_reproduces_the_ready_payload_revision(ready_runtime):
    from iwiki_mcp.codegraph.publication import graph_payload_revision

    exported = ready_runtime.runtime.export_snapshot()

    assert not isinstance(exported, dict)
    header, rows = exported
    assert header.repository_id == ready_runtime.binding.primary
    assert header.schema_version == 2
    assert header.languages == ("python",)
    assert header.expected_counts == {
        kind: len(value) for kind, value in rows.items()
    }
    assert header.graph_payload_revision == graph_payload_revision(rows)
    assert header.graph_payload_revision.startswith("sha256:")


def test_export_snapshot_header_languages_reflects_stored_files_not_a_literal(
    ready_runtime,
):
    with closing(sqlite3.connect(ready_runtime.paths.database)) as connection:
        columns = [
            row[1] for row in connection.execute("PRAGMA table_info(files)")
        ]
        source = dict(
            zip(
                columns,
                connection.execute(
                    "SELECT * FROM files ORDER BY file_id LIMIT 1"
                ).fetchone(),
            )
        )
        source.update(
            file_id="typescript-fixture-file",
            path="src/pkg/fixture.ts",
            path_casefold="src/pkg/fixture.ts",
            file_local_name="fixture.ts",
            file_name_tokens_casefold="fixture ts",
            language="typescript",
            module_id=None,
            module_qualified_name=None,
            module_local_name=None,
            module_name_tokens_casefold=None,
        )
        placeholders = ", ".join("?" for _ in columns)
        connection.execute(
            f"INSERT INTO files ({', '.join(columns)}) VALUES ({placeholders})",
            tuple(source[column] for column in columns),
        )
        connection.commit()

    header, _rows = ready_runtime.runtime.export_snapshot()

    assert header.languages == ("python", "typescript")


class _FakeHostedStore:
    """Records begin() calls; never touches a real database."""

    def __init__(self, session):
        self.domain = "docs"
        self._session = session
        self.begin_calls = []

    def begin(self, header):
        self.begin_calls.append(header)
        return self._session


def test_begin_from_mapping_reports_hosted_batch_limits():
    from iwiki_mcp.codegraph.publication import PublicationSession
    from iwiki_mcp.postgres.config import HostedCodeGraphConfig
    from iwiki_mcp.server import _HostedPublication

    session = PublicationSession(
        session_id="s1",
        lease_expires_at="2026-08-19T00:00:00Z",
        base_snapshot_revision=None,
        base_markdown_token=0,
    )
    store = _FakeHostedStore(session)
    settings = HostedCodeGraphConfig(max_batch_rows=1000, max_batch_bytes=1_000_000)
    publication = _HostedPublication(store, settings)
    header = {
        "protocol_version": 1,
        "schema_version": 2,
        "repository_id": "docs",
        "source_fingerprint": "source",
        "parser_fingerprint": "parser",
        "normalizer_version": "normalizer-1",
        "unicode_data_version": "15.1",
        "languages": ["python"],
        "expected_counts": {
            "repositories": 1, "files": 0, "symbols": 0, "relations": 0
        },
        "graph_payload_revision": "sha256:" + "a" * 64,
    }

    result = publication.begin_from_mapping(header)

    assert result["max_batch_rows"] == 1000
    assert result["max_batch_bytes"] == 1_000_000
    assert result["session_id"] == "s1"


def _session(max_batch_rows=None, max_batch_bytes=None):
    from iwiki_mcp.codegraph.publication import PublicationSession

    return PublicationSession(
        session_id="s",
        lease_expires_at="2026-08-19T00:00:00Z",
        base_snapshot_revision=None,
        base_markdown_token=0,
        max_batch_rows=max_batch_rows,
        max_batch_bytes=max_batch_bytes,
    )


@pytest.mark.parametrize(
    "reported_rows,reported_bytes,expected_rows,expected_bytes",
    [
        (1000, 1_000_000, 1000, 1_000_000),   # valid server value used
        (None, None, 5000, 5_000_000),        # absent -> config fallback
        (0, 1_000_000, 5000, 1_000_000),      # zero rejected -> config fallback
        (-1, 1_000_000, 5000, 1_000_000),     # negative rejected -> config fallback
        (5001, 1_000_000, 5000, 1_000_000),   # over hard ceiling -> config fallback
        (1000, 0, 1000, 5_000_000),           # zero bytes rejected -> config fallback
        (1000, 5_000_001, 1000, 5_000_000),   # over hard ceiling -> config fallback
        (True, 1_000_000, 5000, 1_000_000),   # bool-is-int trap rejected
    ],
)
def test_effective_batch_bounds_validates_server_value(
    reported_rows, reported_bytes, expected_rows, expected_bytes
):
    from iwiki_mcp.server import _effective_batch_bounds

    session = _session(reported_rows, reported_bytes)
    config = CodeGraphConfig(max_batch_rows=5000, max_batch_bytes=5_000_000)

    rows_limit, bytes_limit = _effective_batch_bounds(session, config)

    assert rows_limit == expected_rows
    assert bytes_limit == expected_bytes


def test_publish_from_mapping_reports_row_limit_on_rejection():
    from iwiki_mcp.postgres.config import HostedCodeGraphConfig
    from iwiki_mcp.server import _HostedPublication

    store = _FakeHostedStore(_session())
    settings = HostedCodeGraphConfig(max_batch_rows=10, max_batch_bytes=1_000_000)
    publication = _HostedPublication(store, settings)
    rows = [{"file_id": f"f{i}"} for i in range(11)]

    result = publication.publish_from_mapping("s1", "files", 0, rows, "sha256:x")

    assert result == {
        "error": "invalid_batch",
        "hint": "send batches that match the declared header",
        "limit": 10,
        "received": 11,
    }


def test_publish_from_mapping_reports_byte_limit_on_rejection():
    from iwiki_mcp.codegraph.publication import canonical_batch
    from iwiki_mcp.postgres.config import HostedCodeGraphConfig
    from iwiki_mcp.server import _HostedPublication

    store = _FakeHostedStore(_session())
    settings = HostedCodeGraphConfig(max_batch_rows=1000, max_batch_bytes=10)
    publication = _HostedPublication(store, settings)
    rows = [{"file_id": "f0", "note": "x" * 50}]
    # Use the real payload hash so the hash check passes and the byte-limit
    # branch (not the hash-mismatch branch) is what rejects this batch.
    payload_hash = canonical_batch("files", 0, rows).payload_hash

    result = publication.publish_from_mapping("s1", "files", 0, rows, payload_hash)

    assert result["error"] == "invalid_batch"
    assert result["limit"] == 10
    assert result["received"] > 10


def test_publish_from_mapping_hash_mismatch_carries_no_size_diagnostics():
    # Scope guard: the limit/received diagnostic fields introduced for the
    # row- and byte-limit rejections above must stay confined to those two
    # size checks. A hash-mismatch rejection is not a size violation and
    # must return the bare _CODE_INVALID_BATCH dict with no extra keys.
    from iwiki_mcp.postgres.config import HostedCodeGraphConfig
    from iwiki_mcp.server import _HostedPublication

    store = _FakeHostedStore(_session())
    settings = HostedCodeGraphConfig(max_batch_rows=1000, max_batch_bytes=1_000_000)
    publication = _HostedPublication(store, settings)

    result = publication.publish_from_mapping(
        "s1", "files", 0, [{"file_id": "f0"}], "sha256:" + "0" * 64
    )

    assert result == {
        "error": "invalid_batch",
        "hint": "send batches that match the declared header",
    }


def test_publish_local_snapshot_uses_session_limits_over_config(monkeypatch):
    from iwiki_mcp import server as server_module

    captured = {}
    real_iter = server_module._codegraph_publication.iter_snapshot_batches

    def spying_iter(rows, *, max_rows, max_bytes):
        captured["max_rows"] = max_rows
        captured["max_bytes"] = max_bytes
        return real_iter(rows, max_rows=max_rows, max_bytes=max_bytes)

    monkeypatch.setattr(
        server_module._codegraph_publication, "iter_snapshot_batches", spying_iter
    )

    class _StubPublisher:
        def begin(self, header):
            return _session(max_batch_rows=1000, max_batch_bytes=1_000_000)

        def publish_batch(self, session, batch):
            return {"accepted": True}

        def finalize(self, session):
            return {"state": "ready"}

        def abort(self, session):
            return {"state": "aborted"}

    class _StubRuntime:
        def export_snapshot(self):
            return _exported_snapshot()

    monkeypatch.setattr(
        server_module, "_code_publisher", lambda *a, **k: _StubPublisher()
    )
    config = CodeGraphConfig(max_batch_rows=5000, max_batch_bytes=5_000_000)

    server_module._publish_local_snapshot(_StubRuntime(), object(), config)

    assert captured["max_rows"] == 1000
    assert captured["max_bytes"] == 1_000_000
