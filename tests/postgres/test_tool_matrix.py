"""Complete MCP storage-mode matrix and early PostgreSQL dispatch contract."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace

import pytest

from iwiki_mcp import server
from iwiki_mcp.engine.config import Config
from iwiki_mcp.engine import frontmatter as fm
from iwiki_mcp.specification_store import ProjectionStatus
from iwiki_mcp.storage import PostgresBinding


TOOLS = {
    "wiki_status": "supported",
    "wiki_code_status": "supported",
    "wiki_code_index": "source_unavailable_without_checkout",
    "wiki_code_search": "supported",
    "wiki_code_context": "supported",
    "wiki_code_publish_begin": "supported",
    "wiki_code_publish_batch": "supported",
    "wiki_code_publish_finalize": "supported",
    "wiki_code_publish_abort": "supported",
    "wiki_code_refresh_links": "supported",
    "wiki_list_domains": "supported",
    "wiki_list_pages": "supported",
    "wiki_read_page": "supported",
    "wiki_search": "supported",
    "wiki_spec_search": "supported",
    "wiki_spec_context": "supported",
    "wiki_spec_resolve": "supported",
    "wiki_related": "supported",
    "wiki_write_page": "supported",
    "wiki_update_page": "supported",
    "wiki_delete_page": "supported",
    "wiki_insert_section": "supported",
    "wiki_delete_section": "supported",
    "wiki_move_section": "supported",
    "wiki_index": "supported",
    "wiki_create_domain": "unsupported",
    "wiki_list_domain_grants": "unsupported",
    "wiki_set_domain_grant": "unsupported",
    "wiki_revoke_domain_grant": "unsupported",
    "wiki_bind": "supported",
    "wiki_lint": "supported",
    "wiki_remediation_plan": "unsupported",
    "wiki_migrate_okf": "unsupported",
    "wiki_apply_okf": "unsupported",
    "wiki_export_okf": "unsupported",
    "wiki_sync": "unsupported",
}


@pytest.fixture
def postgres_binding():
    return PostgresBinding(
        host="db.invalid",
        port=5432,
        database="fixture",
        user="fixture",
        sslmode="require",
        iwiki_id="wiki-a",
        read=("docs",),
        write=("docs",),
        primary="docs",
        project_dir="/not-used",
        embed_model="fixture-model",
        embed_dimensions=3,
        rerank_model="",
        password="fixture-secret",
    )


class FakeStore:
    def __init__(self):
        self.update_calls = []

    def list_domains(self):
        return ["docs"]

    def list_pages(self, domain):
        assert domain == "docs"
        return ["concept/page"]

    def read_page(self, domain, slug):
        return {
            "domain": domain,
            "slug": slug,
            "markdown": "# Page\n\n## Body\ntext\n",
            "revision": 4,
        }

    def prepare_read_candidates(self, *_args, **_kwargs):
        return []

    def hydrate_candidates(self, candidates):
        return candidates

    def related(self, _domain, _section_id):
        return {"vector": [], "graph": []}

    def write_page(self, domain, slug, _markdown):
        return {"page": f"{domain}/{slug}.md", "revision": 1, "indexed_chunks": 1}

    def update_page(self, domain, slug, markdown, expected_revision):
        self.update_calls.append((domain, slug, markdown, expected_revision))
        return {
            "page": f"{domain}/{slug}.md",
            "revision": expected_revision + 1,
            "indexed_chunks": 1,
        }

    def delete_page(self, domain, slug, _expected_revision):
        return {"page": f"{domain}/{slug}.md", "deleted": True}

    def index_domain(self, domain):
        return {"domain": domain, "indexed_chunks": 1, "reused": 1, "embedded": 0}

    def lint_domain(self, domain, visible_domains):
        return {"domain": domain, "visible_domains": visible_domains, "broken": []}

    def specification_status(self, domain):
        return ProjectionStatus(domain=domain, state="absent")

    def _specification_projection(self, _domain):
        return None


@pytest.fixture
def postgres_server(monkeypatch, postgres_binding):
    fake = FakeStore()
    monkeypatch.setattr(server, "_LOCAL_POSTGRES_BINDING", None)
    monkeypatch.setattr(server.base, "resolve_binding", lambda: postgres_binding)
    monkeypatch.setattr(server, "_postgres_store_for_binding", lambda _binding: fake)
    monkeypatch.setattr(
        server.Config,
        "load",
        lambda: Config(
            base_url="http://example.invalid/v1",
            api_key="fixture",
            embed_model="fixture-model",
            dimensions=3,
            chunk_size=512,
            chunk_overlap=64,
            summary_max=400,
            top_k=8,
            score_threshold=0.0,
            graph_depth=2,
            ignore=None,
        ),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("PostgreSQL dispatch reached local storage")

    @contextmanager
    def forbidden_lock(*_args, **_kwargs):
        forbidden()
        yield

    monkeypatch.setattr(server.sync, "ensure_fresh", forbidden)
    monkeypatch.setattr(server.sync, "sync", forbidden)
    monkeypatch.setattr(server.base, "list_domains", forbidden)
    monkeypatch.setattr(server.base, "migrate_store_location", forbidden)
    monkeypatch.setattr(server, "_domain_path", forbidden)
    monkeypatch.setattr(server, "mutation_lock", forbidden_lock)
    return postgres_binding, fake


def test_registered_tools_match_complete_mode_matrix():
    registered = {tool.name for tool in server.mcp._tool_manager.list_tools()}

    assert len(TOOLS) == 36
    assert len(registered) == 36
    assert registered == set(TOOLS)


def test_every_tool_schema_root_stays_a_plain_object():
    # Client tool validation accepts only a plain object schema root; a
    # combinator there makes the client drop the tool from the session.
    combinators = {"anyOf", "oneOf", "allOf", "not", "$ref"}

    for tool in server.mcp._tool_manager.list_tools():
        assert tool.parameters.get("type") == "object", tool.name
        assert not combinators & set(tool.parameters), tool.name


def test_update_page_documents_its_operations_without_a_root_combinator():
    tool = server.mcp._tool_manager.get_tool("wiki_update_page")

    assert tool is not None
    assert tool.parameters["required"] == ["domain", "slug"]
    assert "code" in tool.parameters["properties"]
    assert "code-only" in tool.description
    assert "combined" in tool.description


def test_update_page_runtime_rejects_partial_and_empty_operations():
    def prepare(**overrides):
        request = {
            "heading": None, "new_body": None, "source": None,
            "description": None, "status": None, "new_heading": None,
            "expected_section_hash": None, "code": None,
        }
        request.update(overrides)
        heading = request.pop("heading")
        new_body = request.pop("new_body")
        return server._prepare_update_page_request(heading, new_body, **request)

    assert "error" in prepare(heading="Body")
    assert "error" in prepare(new_body="text")
    assert "error" in prepare()
    assert "error" in prepare(code={}, source="src.py")
    assert prepare(heading="Body", new_body="text")["mode"] == "section"
    assert prepare(code={})["mode"] == "code"
    assert prepare(
        heading="Body", new_body="text", code={}
    )["mode"] == "combined"


def test_bind_schema_exposes_optional_specification_mode_enum():
    tool = next(
        item for item in server.mcp._tool_manager.list_tools()
        if item.name == "wiki_bind"
    )

    schema = tool.parameters["properties"]["specification_mode"]

    assert {"type": "string", "enum": ["disabled", "optional", "strict"]} in schema[
        "anyOf"
    ]
    assert "specification_mode" not in tool.parameters.get("required", [])


def test_grant_tool_schemas_expose_content_authority_only():
    protected = {
        "wiki_list_domain_grants",
        "wiki_set_domain_grant",
        "wiki_revoke_domain_grant",
    }
    tools = {
        tool.name: tool
        for tool in server.mcp._tool_manager.list_tools()
        if tool.name in protected
    }

    assert set(tools) == protected
    for tool in tools.values():
        properties = tool.parameters["properties"]
        assert "iwiki_id" not in properties
        assert "can_manage_grants" not in properties
        assert all("management" not in name for name in properties)


@pytest.mark.parametrize(
    ("handler", "args", "kwargs"),
    [
        (server.wiki_create_domain, ("new-domain",), {}),
        (server.wiki_remediation_plan, (), {}),
        (server.wiki_migrate_okf, (), {}),
        (server.wiki_apply_okf, ("docs", "concept/page", "concept"), {}),
        (server.wiki_export_okf, (), {}),
        (server.wiki_sync, (), {}),
    ],
)
def test_postgres_unsupported_tools_fail_before_local_access(
    postgres_server, handler, args, kwargs
):
    result = handler(*args, **kwargs)

    assert result == {
        "error": "unsupported_storage",
        "storage": "postgres",
        "hint": "use this tool with Git storage",
    }


def test_grant_tools_reject_postgres_stdio(postgres_server):
    expected = {
        "error": "unsupported_transport",
        "storage": "postgres",
        "transport": "stdio",
        "hint": "use hosted Streamable HTTP with PostgreSQL storage",
    }

    assert server.wiki_list_domain_grants("docs") == expected
    assert server.wiki_set_domain_grant(
        "docs", "target", True, False
    ) == expected
    assert server.wiki_revoke_domain_grant("docs", "target") == expected


def test_grant_tools_reject_git_stdio(tmp_path, monkeypatch):
    binding = server.base.Binding(
        base=str(tmp_path),
        read=("docs",),
        write=("docs",),
        primary="docs",
        project_dir=str(tmp_path),
    )
    monkeypatch.setattr(server, "_LOCAL_POSTGRES_BINDING", None)
    monkeypatch.setattr(server.base, "resolve_binding", lambda: binding)
    expected = {
        "error": "unsupported_transport",
        "storage": "git",
        "transport": "stdio",
        "hint": "use hosted Streamable HTTP with PostgreSQL storage",
    }

    assert server.wiki_list_domain_grants("docs") == expected
    assert server.wiki_set_domain_grant(
        "docs", "target", True, False
    ) == expected
    assert server.wiki_revoke_domain_grant("docs", "target") == expected


def test_postgres_supported_handlers_use_store_without_local_access(postgres_server):
    status = server.wiki_status()
    assert status == {
        "storage": "postgres",
        "transport": "stdio",
        "read": ["docs"],
        "write": ["docs"],
        "primary": "docs",
        "project_dir": "/not-used",
        "domains": ["docs"],
        "specifications": {"domains": [{
            "domain": "docs",
            "mode": "optional",
            "source": "built_in_default",
            "projection_state": "absent",
            "scenarios": 0,
            "bindings": 0,
        }]},
    }
    assert "fixture-secret" not in repr(status)
    assert server.wiki_list_domains()["domains"] == ["docs"]
    assert server.wiki_list_pages("docs")["pages"] == [
        {"slug": "concept/page", "file": "concept/page.md"}
    ]
    assert server.wiki_read_page("docs", "concept/page")["revision"] == 4
    assert server.wiki_search("query") == {"results": []}
    assert server.wiki_related("docs", "concept/page.md#Body") == {
        "vector": [],
        "graph": [],
    }
    assert server.wiki_write_page(
        "docs", "concept/new", "# New\n\n## Body\ntext\n", type="concept"
    )["revision"] == 1
    assert server.wiki_update_page(
        "docs", "concept/page", "Body", "new text", expected_revision=4
    )["revision"] == 5
    assert server.wiki_delete_page(
        "docs", "concept/page", expected_revision=4
    )["deleted"] is True
    assert server.wiki_index("docs")["indexed_chunks"] == 1
    assert server.wiki_lint("docs")["reports"]["docs"]["broken"] == []


def test_postgres_read_page_with_heading_returns_section(postgres_server):
    out = server.wiki_read_page("docs", "concept/page", heading="Body")

    assert out["heading"] == "Body"
    assert out["body"] == "text"
    assert "section_hash" in out
    assert "markdown" not in out

    missing = server.wiki_read_page("docs", "concept/page", heading="Nope")
    assert "not found" in missing["error"]


def test_postgres_revision_is_required_but_git_schema_remains_optional(postgres_server):
    assert server.wiki_update_page(
        "docs", "concept/page", "Body", "new text"
    ) == {
        "error": "expected_revision_required",
        "hint": "read the page and retry with its revision",
    }
    assert server.wiki_delete_page("docs", "concept/page") == {
        "error": "expected_revision_required",
        "hint": "read the page and retry with its revision",
    }
    assert server.wiki_insert_section(
        "docs", "concept/page", "New", "body"
    ) == {
        "error": "expected_revision_required",
        "hint": "read the page and retry with its revision",
    }
    assert server.wiki_delete_section("docs", "concept/page", "Body") == {
        "error": "expected_revision_required",
        "hint": "read the page and retry with its revision",
    }
    assert server.wiki_move_section("docs", "concept/page", "Body") == {
        "error": "expected_revision_required",
        "hint": "read the page and retry with its revision",
    }

    tools = {tool.name: tool for tool in server.mcp._tool_manager.list_tools()}
    for name in (
        "wiki_update_page",
        "wiki_delete_page",
        "wiki_insert_section",
        "wiki_delete_section",
        "wiki_move_section",
    ):
        schema = tools[name].parameters
        assert "expected_revision" in schema["properties"]
        assert "expected_revision" not in schema.get("required", [])


@pytest.mark.parametrize(
    ("kwargs", "expected_error"),
    [
        ({}, "no update operation requested"),
        ({"heading": "Body"}, "heading and new_body must be provided together"),
        ({"code": {"modules": ["pkg.page"]}}, "unsupported code selector key"),
    ],
)
def test_postgres_invalid_update_request_precedes_revision_guard(
    postgres_server, kwargs, expected_error
):
    _binding, fake = postgres_server

    result = server.wiki_update_page("docs", "concept/page", **kwargs)

    assert result["error"] == expected_error
    assert fake.update_calls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"code": {"modules": ["pkg.page"]}},
    ],
)
def test_postgres_write_scope_precedes_invalid_update_request(
    postgres_server, monkeypatch, kwargs
):
    binding, fake = postgres_server
    restricted = replace(
        binding,
        read=("docs", "private"),
        write=("docs",),
    )
    monkeypatch.setattr(server.base, "resolve_binding", lambda: restricted)

    result = server.wiki_update_page("private", "concept/page", **kwargs)

    assert "outside bound write scope" in result["error"]
    assert fake.update_calls == []


def test_postgres_code_only_update_builds_one_candidate_and_omits_heading(
    postgres_server,
):
    _binding, fake = postgres_server

    result = server.wiki_update_page(
        "docs",
        "concept/page",
        code={"files": ["src/page.py"]},
        expected_revision=4,
    )

    assert result == {
        "page": "docs/concept/page.md",
        "revision": 5,
        "indexed_chunks": 1,
    }
    assert len(fake.update_calls) == 1
    domain, slug, candidate, revision = fake.update_calls[0]
    assert (domain, slug, revision) == ("docs", "concept/page", 4)
    meta, body = fm.split(candidate, strict_code=True)
    assert meta["code"] == {"files": ["src/page.py"]}
    assert body == "# Page\n\n## Body\ntext\n"


def test_postgres_update_page_combines_section_and_code_in_one_candidate(
    postgres_server,
):
    _binding, fake = postgres_server

    result = server.wiki_update_page(
        "docs",
        "concept/page",
        "Body",
        "changed",
        code={"source_globs": ["src/**"]},
        expected_revision=4,
    )

    assert result == {
        "page": "docs/concept/page.md",
        "revision": 5,
        "indexed_chunks": 1,
        "heading": "Body",
    }
    assert len(fake.update_calls) == 1
    _domain, _slug, candidate, _revision = fake.update_calls[0]
    meta, body = fm.split(candidate, strict_code=True)
    assert meta["code"] == {"source_globs": ["src/**"]}
    assert "## Body\n\nchanged" in body


def test_postgres_update_page_invalid_code_selector_never_calls_store(postgres_server):
    _binding, fake = postgres_server

    result = server.wiki_update_page(
        "docs",
        "concept/page",
        code={"modules": ["pkg.page"]},
        expected_revision=4,
    )

    assert result == {
        "error": "unsupported code selector key",
        "hint": "use only code.symbols, code.files, and code.source_globs",
    }
    assert fake.update_calls == []


def test_postgres_driver_errors_are_sanitized(postgres_server, monkeypatch):
    _binding, fake = postgres_server
    monkeypatch.setattr(
        fake,
        "list_domains",
        lambda: (_ for _ in ()).throw(
            server._postgres_store.psycopg.OperationalError(
                "password=fixture-secret host=db.invalid SQL SELECT private"
            )
        ),
    )

    result = server.wiki_status()

    assert result == {
        "error": "PostgreSQL operation failed",
        "hint": "retry or inspect sanitized server diagnostics",
    }
    assert "fixture-secret" not in repr(result)
    assert "SELECT" not in repr(result)


def test_postgres_bind_can_only_narrow_session_scope(
    postgres_server, monkeypatch
):
    binding, fake = postgres_server
    broader = replace(
        binding,
        read=("docs", "private"),
        write=("docs", "private"),
    )
    monkeypatch.setattr(server.base, "resolve_binding", lambda: broader)
    monkeypatch.setattr(fake, "list_domains", lambda: ["docs", "private"])
    token = server._SESSION_BINDING.set(None)
    previous_local = server._LOCAL_POSTGRES_BINDING
    server._LOCAL_POSTGRES_BINDING = None
    try:
        narrowed = server.wiki_bind(
            read=["docs"], write=["docs"], primary="docs"
        )
        expanded = server.wiki_bind(
            read=["docs", "private"], write=["docs"], primary="docs"
        )
    finally:
        server._SESSION_BINDING.reset(token)
        server._LOCAL_POSTGRES_BINDING = previous_local

    assert narrowed["read"] == ["docs"]
    assert narrowed["write"] == ["docs"]
    assert expanded["error"] == "read scope is protected"


def test_hosted_bind_persists_project_specification_mode(postgres_server):
    binding, _fake = postgres_server
    state = server._HostedBindingState(binding)
    token = server._SESSION_BINDING.set(state)
    try:
        result = server.wiki_bind(specification_mode="strict")
    finally:
        server._SESSION_BINDING.reset(token)

    assert "error" not in result
    assert state.selected_state().get().project_specification_mode == "strict"


def test_local_postgres_bind_rejects_project_specification_mode(postgres_server):
    token = server._SESSION_BINDING.set(None)
    previous_local = server._LOCAL_POSTGRES_BINDING
    server._LOCAL_POSTGRES_BINDING = None
    try:
        result = server.wiki_bind(specification_mode="strict")
    finally:
        server._SESSION_BINDING.reset(token)
        server._LOCAL_POSTGRES_BINDING = previous_local

    assert result["error"] == "specification mode requires a hosted session"


def test_hosted_bind_rejects_invalid_project_mode_without_mutating_session(
    postgres_server, monkeypatch,
):
    binding, fake = postgres_server
    state = server._HostedBindingState(binding)
    token = server._SESSION_BINDING.set(state)
    try:
        assert "error" not in server.wiki_bind(specification_mode="strict")
        monkeypatch.setattr(
            fake,
            "list_domains",
            lambda: (_ for _ in ()).throw(AssertionError("domain lookup reached")),
        )
        result = server.wiki_bind(specification_mode="required")
    finally:
        server._SESSION_BINDING.reset(token)

    assert result["error"] == "specification mode is invalid"
    assert state.selected_state().get().project_specification_mode == "strict"


def test_hosted_project_mode_is_isolated_between_sessions(postgres_server):
    binding, _fake = postgres_server
    first = server._HostedBindingState(binding)
    second = server._HostedBindingState(binding)

    token = server._SESSION_BINDING.set(first)
    try:
        assert "error" not in server.wiki_bind(specification_mode="strict")
    finally:
        server._SESSION_BINDING.reset(token)

    assert first.selected_state().get().project_specification_mode == "strict"
    assert second.selected_state().get().project_specification_mode is None


@pytest.mark.parametrize(
    "operation",
    [
        lambda: server.wiki_write_page(
            "docs", "concept/new", "# New\n\n## Body\ntext\n"
        ),
        lambda: server.wiki_update_page(
            "docs", "concept/page", "Body", "changed", expected_revision=4
        ),
        lambda: server.wiki_insert_section(
            "docs", "concept/page", "Added", "text", expected_revision=4
        ),
        lambda: server.wiki_delete_section(
            "docs", "concept/page", "Body", expected_revision=4
        ),
        lambda: server.wiki_move_section(
            "docs", "concept/page", "Body", before_heading="Body", expected_revision=4
        ),
        lambda: server.wiki_delete_page(
            "docs", "concept/page", expected_revision=4
        ),
        lambda: server.wiki_index("docs"),
    ],
)
def test_hosted_project_mode_scopes_postgres_mutation_store(
    operation,
    postgres_server, monkeypatch,
):
    binding, fake = postgres_server
    state = server._HostedBindingState(binding)
    token = server._SESSION_BINDING.set(state)
    try:
        assert "error" not in server.wiki_bind(specification_mode="strict")
        captured = []

        def store_for(scoped_binding):
            captured.append(scoped_binding)
            return fake

        monkeypatch.setattr(server, "_postgres_store_for_binding", store_for)
        operation()
    finally:
        server._SESSION_BINDING.reset(token)

    assert captured[-1].specification_mode == "strict"


def test_hosted_concurrent_bind_narrowing_is_atomic(
    postgres_server, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time

    binding, fake = postgres_server
    broader = replace(
        binding,
        read=("docs", "private"),
        write=("docs", "private"),
    )
    state = server._HostedBindingState(broader)
    gate = threading.Barrier(2)
    activity_lock = threading.Lock()
    active = 0
    maximum_active = 0

    def list_domains():
        nonlocal active, maximum_active
        with activity_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.02)
        with activity_lock:
            active -= 1
        return ["docs", "private"]

    monkeypatch.setattr(fake, "list_domains", list_domains)

    def narrow(domain):
        gate.wait()
        token = server._SESSION_BINDING.set(state)
        try:
            return server.wiki_bind(read=[domain], write=[])
        finally:
            server._SESSION_BINDING.reset(token)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(narrow, ("docs", "private")))

    assert maximum_active == 1
    assert sum("error" not in result for result in results) == 1
    assert len(state.get().read) == 1


@pytest.mark.postgres_integration
def test_real_postgres_handlers_preserve_revision_and_conflict(
    clean_postgres, runtime_principal, monkeypatch
):
    from psycopg.conninfo import conninfo_to_dict

    from iwiki_mcp.postgres.migrations import MigrationSettings, run_migrations

    cfg = Config(
        base_url="http://example.invalid/v1",
        api_key="fixture",
        embed_model="fixture-model",
        dimensions=3,
        chunk_size=512,
        chunk_overlap=64,
        summary_max=400,
        top_k=8,
        score_threshold=0.0,
        graph_depth=2,
        ignore=None,
    )
    run_migrations(
        MigrationSettings(
            dsn=clean_postgres,
            embed_model=cfg.embed_model,
            embed_dimensions=cfg.dimensions,
            statement_timeout_ms=30_000,
            lock_timeout_ms=5_000,
        )
    )
    values = conninfo_to_dict(clean_postgres)
    admin_store = server._postgres_store.PostgresStore(
        clean_postgres,
        "wiki-a",
        cfg,
    )
    admin_store.create_wiki("wiki-a")
    admin_store.create_domain("docs")
    role, password = runtime_principal(("docs",), ("docs",))
    binding = PostgresBinding(
        host=values["host"],
        port=int(values.get("port", 5432)),
        database=values["dbname"],
        user=role,
        sslmode=values.get("sslmode", "prefer"),
        iwiki_id="wiki-a",
        read=("docs",),
        write=("docs",),
        primary="docs",
        project_dir="/not-used",
        embed_model=cfg.embed_model,
        embed_dimensions=cfg.dimensions,
        rerank_model="",
        password=password,
    )
    monkeypatch.setattr(server.base, "resolve_binding", lambda: binding)
    monkeypatch.setattr(server.Config, "load", lambda: cfg)
    monkeypatch.setattr(
        server._postgres_store,
        "embed_texts",
        lambda _cfg, texts: [[1.0, 0.0, 0.0] for _text in texts],
    )
    store = server._postgres_store_for_binding(binding)
    store._embedder = lambda _cfg, texts: [
        [1.0, 0.0, 0.0] for _text in texts
    ]
    monkeypatch.setattr(server, "_postgres_store_for_binding", lambda _binding: store)
    session_token = server._SESSION_BINDING.set(None)
    try:
        created = server.wiki_write_page(
            "docs",
            "concept/page",
            "# Page\n\n## Body\nalpha text\n",
            type="concept",
        )
        read = server.wiki_read_page("docs", "concept/page")
        updated = server.wiki_update_page(
            "docs",
            "concept/page",
            "Body",
            "beta text",
            expected_revision=read["revision"],
        )
        conflict = server.wiki_update_page(
            "docs",
            "concept/page",
            "Body",
            "stale text",
            expected_revision=read["revision"],
        )
        indexed = server.wiki_index("docs")
        linted = server.wiki_lint("docs")
        status = server.wiki_status()
    finally:
        server._SESSION_BINDING.reset(session_token)

    assert created["revision"] == 1
    assert read["revision"] == 1
    assert updated["revision"] == 2
    assert conflict == {
        "error": "conflict",
        "current_revision": 2,
        "hint": "read the page and retry against the current revision",
    }
    assert indexed["indexed_chunks"] > 0
    assert linted["reports"]["docs"]["broken"] == []
    assert status["storage"] == "postgres"
    assert "fixture-secret" not in repr(status)
