"""PostgreSQL specification projection contract."""
from __future__ import annotations

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from types import SimpleNamespace
import pytest

from iwiki_mcp.engine.config import Config
from iwiki_mcp.postgres.auth import AccessError, AuthContext
from iwiki_mcp.postgres.store import PostgresStore
from iwiki_mcp.specification_store import (
    DomainProjection,
    GitSpecificationStore,
    ResolutionAttempt,
)
from iwiki_mcp.specifications import PageSnapshot, assemble_projection


def _cfg():
    return Config(
        base_url="http://example.invalid/v1",
        api_key="test",
        embed_model="test-embedding",
        dimensions=3,
        chunk_size=512,
        chunk_overlap=64,
        summary_max=400,
        top_k=8,
        score_threshold=0.0,
        graph_depth=2,
        ignore=None,
        seed_top_k=2,
        bfs_top_k=10,
        seed_threshold=0.0,
    )


def _embed(_cfg_value, texts):
    return [[1.0, 0.0, 0.0] for _text in texts]


def _empty_projection(domain="docs"):
    return DomainProjection(
        domain=domain,
        markdown_revision="sha256:" + "0" * 64,
        scenarios=(),
        bindings=(),
        evidence=(),
        findings=(),
    )


def _resolution_attempt(domain="docs"):
    return ResolutionAttempt(
        binding_id="spec:binding:" + "0" * 64,
        domain=domain,
        scenario_id="stable-id",
        state="unresolved",
        targets=(),
        unresolved_reference="missing.Target",
        graph_revision="graph-1",
        graph_state_fingerprint="sha256:" + "1" * 64,
        specification_source_hash="2" * 64,
        checked_at="2026-08-29T12:00:00Z",
        reason=None,
    )


def _specification_markdown(scenario_id="stable-id", heading="Stable behavior"):
    return (
        "---\n"
        "type: specification\n"
        "title: Stable behavior\n"
        "description: Observable stable behavior.\n"
        "tags: [fixture]\n"
        "status: developing\n"
        "---\n"
        "# Stable behavior\n\n"
        f"## {heading}\n\n"
        "Scenario prose.\n\n"
        "```iwiki-gwt\n"
        f'id = "{scenario_id}"\n'
        'title = "Stable behavior"\n'
        "given = []\n"
        'when = { role = "command", name = "RunStable" }\n'
        'then = [{ role = "event", name = "StableRan" }]\n'
        "code = [\n"
        '  { relation = "implements", phase = "when", symbol = "app.run" },\n'
        '  { relation = "verifies", symbol = "tests.test_run" }\n'
        "]\n"
        "```\n"
    )


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("replace_specification_projection", (_empty_projection(),)),
        ("search_specifications", (("docs",), "scenario", 20)),
        ("specification_context", ("docs", "stable-id")),
        ("record_specification_resolution", (_resolution_attempt(),)),
        ("specification_status", ("docs",)),
    ],
)
def test_projection_methods_authorize_before_opening_sql(method, args):
    def forbidden_connection():
        raise AssertionError("SQL opened before authorization")

    store = PostgresStore(
        "postgresql://example.invalid/unused",
        "wiki-a",
        _cfg(),
        auth_context=AuthContext(
            iwiki_id="wiki-a",
            token_id="fixture",
            read_domains=(),
            write_domains=(),
            primary=None,
        ),
        connection_factory=forbidden_connection,
    )

    with pytest.raises(AccessError):
        getattr(store, method)(*args)


@pytest.mark.parametrize("mode", ["disabled", "optional", "strict"])
def test_specification_mode_is_validated_and_preserved_by_with_embedder(mode):
    store = PostgresStore(
        "postgresql://example.invalid/unused",
        "wiki-a",
        _cfg(),
        specification_mode=mode,
        connection_factory=lambda: None,
    )

    assert store.specification_mode == mode
    assert store.with_embedder(lambda _cfg, _texts: []).specification_mode == mode


def test_invalid_specification_mode_is_rejected_before_sql():
    with pytest.raises(ValueError, match="invalid specification mode"):
        PostgresStore(
            "postgresql://example.invalid/unused",
            "wiki-a",
            _cfg(),
            specification_mode="advisory",
            connection_factory=lambda: None,
        )


@pytest.mark.parametrize("mode", ["disabled", "optional", "strict"])
def test_postgres_binding_passes_effective_specification_mode_to_store(
    monkeypatch, mode,
):
    from iwiki_mcp import server

    captured = {}

    def capture_store(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return object()

    binding = SimpleNamespace(
        iwiki_id="wiki-a",
        read=("docs",),
        write=("docs",),
        primary="docs",
        specification_mode=mode,
        connection_dsn=lambda: "postgresql://example.invalid/unused",
    )
    monkeypatch.setattr(server._postgres_store, "PostgresStore", capture_store)
    monkeypatch.setattr(server.Config, "load", _cfg)
    monkeypatch.setattr(server, "_HOSTED_CONFIG", None)
    monkeypatch.setattr(server, "_HOSTED_POOL", None)
    monkeypatch.setattr(server, "_AUTH_CONTEXT", SimpleNamespace(get=lambda: None))

    server._postgres_store_for_binding(binding)

    assert captured["kwargs"]["specification_mode"] == mode


def test_disabled_projection_methods_authorize_then_open_no_sql():
    def forbidden_connection():
        raise AssertionError("disabled specifications opened SQL")

    store = PostgresStore(
        "postgresql://example.invalid/unused",
        "wiki-a",
        _cfg(),
        specification_mode="disabled",
        auth_context=AuthContext(
            iwiki_id="wiki-a",
            token_id="fixture",
            read_domains=("docs",),
            write_domains=("docs",),
            primary="docs",
        ),
        connection_factory=forbidden_connection,
    )

    assert store.replace_specification_projection(_empty_projection()) == {
        "state": "disabled"
    }
    assert store.search_specifications(("docs",), "stable", 20) == ()
    assert store.specification_context("docs", "stable-id") is None
    assert store.record_specification_resolution(_resolution_attempt()) is None
    assert store.specification_status("docs").state == "disabled"


@pytest.mark.parametrize("fault", ["_lock_specification_domain", "_specification_pages"])
def test_optional_refresh_read_fault_rolls_back_savepoint_and_commits_outer_mutation(
    monkeypatch, fault,
):
    events = []

    class Connection:
        depth = 0

        @contextmanager
        def transaction(self):
            self.depth += 1
            depth = self.depth
            events.append(("enter", depth))
            try:
                yield
            except Exception:
                events.append(("rollback", depth))
                raise
            else:
                events.append(("commit", depth))
            finally:
                self.depth -= 1

    store = PostgresStore(
        "postgresql://example.invalid/unused",
        "wiki-a",
        _cfg(),
        specification_mode="optional",
        connection_factory=lambda: None,
    )

    def fail_read(*_args):
        assert connection.depth == 2
        raise RuntimeError("private database detail")

    if fault == "_specification_pages":
        monkeypatch.setattr(store, "_lock_specification_domain", lambda *_args: 1)
    monkeypatch.setattr(store, fault, fail_read)
    connection = Connection()
    with connection.transaction():
        events.append("page mutation")
        warning = store._publish_specification_projection(
            connection, object(), _empty_projection()
        )

    assert warning == "specification projection is stale"
    assert "private database detail" not in warning
    assert events == [
        ("enter", 1),
        "page mutation",
        ("enter", 2),
        ("rollback", 2),
        ("commit", 1),
    ]


def test_index_domain_rebuilds_projection_from_its_sorted_page_snapshot(monkeypatch):
    events = []

    class Cursor:
        sql = ""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, _params):
            self.sql = sql

        def fetchall(self):
            if "p.revision" in self.sql:
                return [(11, "specification/a", _specification_markdown(), 4)]
            return [(11, "specification/a", _specification_markdown())]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return Cursor()

        @contextmanager
        def transaction(self):
            yield

    store = PostgresStore(
        "postgresql://example.invalid/unused",
        "wiki-a",
        _cfg(),
        specification_mode="optional",
        connection_factory=lambda: Connection(),
    )

    @contextmanager
    def connect():
        yield Connection()

    monkeypatch.setattr(store, "_connect", connect)
    monkeypatch.setattr(
        store,
        "_prepare_page",
        lambda domain, slug, markdown: (
            domain, slug, markdown, (), (SimpleNamespace(),), (),
        ),
    )
    monkeypatch.setattr(
        store,
        "_replace_derived",
        lambda _cursor, page_id, *_args: events.append(("ordinary", page_id)),
    )

    def assemble(domain, pages, previous_evidence=(), *, markdown_revision=None):
        events.append(("snapshot", domain, tuple(pages), previous_evidence))
        return _empty_projection(domain)

    monkeypatch.setattr("iwiki_mcp.specifications.assemble_projection", assemble)
    monkeypatch.setattr(
        store,
        "_publish_specification_projection",
        lambda _connection, _cursor, projection: events.append(
            ("projection", projection.domain)
        ),
    )

    result = store.index_domain("docs")

    assert result["indexed_chunks"] == 1
    assert events[0][0] == "snapshot"
    assert events[0][2] == (
        PageSnapshot("specification/a", _specification_markdown(), 4),
    )
    assert events[1:] == [("ordinary", 11), ("projection", "docs")]


def test_resolution_write_uses_projection_domain_lock(monkeypatch):
    events = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return Cursor()

    store = PostgresStore(
        "postgresql://example.invalid/unused",
        "wiki-a",
        _cfg(),
        specification_mode="optional",
        connection_factory=lambda: Connection(),
    )

    @contextmanager
    def connect():
        yield Connection()

    monkeypatch.setattr(store, "_connect", connect)
    monkeypatch.setattr(
        store,
        "_specification_domain",
        lambda *_args: pytest.fail("unlocked domain lookup used"),
    )
    monkeypatch.setattr(
        store,
        "_lock_specification_domain",
        lambda _cursor, domain: events.append(("lock", domain)) or 7,
    )
    monkeypatch.setattr(
        store,
        "_record_specification_resolution",
        lambda _cursor, domain_id, _attempt: events.append(("write", domain_id)),
    )

    store.record_specification_resolution(_resolution_attempt())

    assert events == [("lock", "docs"), ("write", 7)]


def test_changed_domain_snapshot_requests_full_transaction_retry(monkeypatch):
    from iwiki_mcp.postgres import store as store_module

    store = PostgresStore(
        "postgresql://example.invalid/unused",
        "wiki-a",
        _cfg(),
        specification_mode="optional",
        connection_factory=lambda: None,
    )
    projection = _empty_projection()

    changed = getattr(store_module, "_SpecificationSnapshotChanged")
    monkeypatch.setattr(
        store,
        "_replace_specification_projection",
        lambda *_args: (_ for _ in ()).throw(changed()),
    )

    class Connection:
        @contextmanager
        def transaction(self):
            yield

    with pytest.raises(changed):
        store._publish_specification_projection(
            Connection(), object(), projection
        )


def test_changed_domain_snapshot_reprepares_before_retry():
    from iwiki_mcp.postgres.store import _SpecificationSnapshotChanged

    store = PostgresStore(
        "postgresql://example.invalid/unused",
        "wiki-a",
        _cfg(),
        specification_mode="strict",
        connection_factory=lambda: None,
    )
    prepared = []
    mutations = []

    def prepare():
        prepared.append(len(prepared) + 1)
        return prepared[-1]

    def mutate(projection):
        mutations.append(projection)
        if len(mutations) == 1:
            raise _SpecificationSnapshotChanged
        return projection

    assert store._run_specification_transaction(prepare, mutate) == 2
    assert prepared == [1, 2]
    assert mutations == [1, 2]


@pytest.mark.postgres_integration
def test_projection_round_trip_search_context_status_and_restart(store_factory):
    store = store_factory()
    markdown = _specification_markdown()
    created = store.write_page("docs", "specification/stable", markdown)
    projection = assemble_projection(
        "docs",
        [PageSnapshot("specification/stable", markdown, created["revision"])],
    )

    assert store.replace_specification_projection(projection) == {
        "state": "ready",
        "scenarios": 1,
        "bindings": 2,
    }
    assert [item.scenario_id for item in store.search_specifications(
        ("docs",), "stable", 20
    )] == ["stable-id"]
    context = store.specification_context("docs", "stable-id")
    assert context is not None
    assert context.scenario == projection.scenarios[0]
    assert context.bindings == projection.bindings
    assert context.evidence == ()
    assert store.specification_status("docs").scenario_count == 1

    binding = context.bindings[0]
    attempt = ResolutionAttempt(
        binding_id=binding.binding_id,
        domain="docs",
        scenario_id="stable-id",
        state="unresolved",
        targets=(),
        unresolved_reference=binding.selector,
        graph_revision="graph-1",
        graph_state_fingerprint="sha256:" + "1" * 64,
        specification_source_hash=context.scenario.source_hash,
        checked_at="2026-08-29T12:00:00Z",
        reason=None,
    )
    store.record_specification_resolution(attempt)

    restarted = store_factory()
    restarted_context = restarted.specification_context("docs", "stable-id")
    assert restarted_context is not None
    assert restarted_context.evidence == (attempt,)


@pytest.mark.postgres_integration
def test_git_and_postgres_specification_records_have_golden_parity(
    store_factory, tmp_path,
):
    postgres = store_factory()
    markdown = _specification_markdown()
    created = postgres.write_page("docs", "specification/stable", markdown)
    projection = assemble_projection(
        "docs",
        [PageSnapshot("specification/stable", markdown, created["revision"])],
    )
    postgres.replace_specification_projection(projection)

    git_base = tmp_path / "wiki"
    (git_base / "docs").mkdir(parents=True)
    git = GitSpecificationStore(str(git_base))
    git.replace_projection(projection)
    binding = projection.bindings[0]
    attempt = ResolutionAttempt(
        binding_id=binding.binding_id,
        domain="docs",
        scenario_id="stable-id",
        state="unresolved",
        targets=(),
        unresolved_reference=binding.selector,
        graph_revision="graph-1",
        graph_state_fingerprint="sha256:" + "1" * 64,
        specification_source_hash=projection.scenarios[0].source_hash,
        checked_at="2026-08-29T14:00:00+02:00",
        reason=None,
    )
    postgres.record_specification_resolution(attempt)
    git.record_resolution(attempt)

    def normalize(context):
        value = asdict(context)
        value["scenario"].pop("page_revision")
        value.pop("projection_revision")
        return value

    assert normalize(postgres.specification_context("docs", "stable-id")) == (
        normalize(git.context("docs", "stable-id"))
    )
    assert attempt.checked_at == "2026-08-29T12:00:00Z"


@pytest.mark.postgres_integration
@pytest.mark.parametrize("fault", ["_lock_specification_domain", "_specification_pages"])
def test_strict_projection_failure_rolls_back_page_and_optional_preserves_rows(
    store_factory, monkeypatch, fault,
):
    store = store_factory()
    store.specification_mode = "strict"
    original = _specification_markdown()
    store.write_page("docs", "specification/stable", original)
    before = store.specification_context("docs", "stable-id")
    assert before is not None

    def fail_projection(*_args, **_kwargs):
        raise RuntimeError("private database detail")

    if fault == "_lock_specification_domain":
        monkeypatch.setattr(store, fault, fail_projection)
    else:
        original_lock = store._lock_specification_domain
        original_pages = store._specification_pages
        armed = False

        def arm_page_read(cursor, domain):
            nonlocal armed
            domain_id = original_lock(cursor, domain)
            armed = True
            return domain_id

        def fail_locked_page_read(cursor, domain_id):
            nonlocal armed
            if armed:
                armed = False
                return fail_projection(cursor, domain_id)
            return original_pages(cursor, domain_id)

        monkeypatch.setattr(store, "_lock_specification_domain", arm_page_read)
        monkeypatch.setattr(store, fault, fail_locked_page_read)
    changed = original.replace("Scenario prose.", "Changed prose.")
    with pytest.raises(ValueError, match="specification projection update failed"):
        store.update_page("docs", "specification/stable", changed, 1)
    assert store.read_page("docs", "specification/stable")["revision"] == 1

    store.specification_mode = "optional"
    result = store.update_page("docs", "specification/stable", changed, 1)
    assert result["revision"] == 2
    assert result["warning"] == "specification projection is stale"
    after = store.specification_context("docs", "stable-id")
    assert after == before


@pytest.mark.postgres_integration
def test_ordinary_and_disabled_mutations_do_not_replace_projection(
    store_factory, monkeypatch,
):
    store = store_factory()
    calls = []

    def observe(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("specification projection reached")

    monkeypatch.setattr(store, "_replace_specification_projection", observe)
    ordinary = (
        "---\ntype: concept\ntitle: Ordinary\ndescription: ordinary page\n"
        "tags: [fixture]\nstatus: stable\n---\n# Ordinary\n\n## Details\nBody.\n"
    )
    store.write_page("docs", "concept/ordinary", ordinary)
    store.update_page(
        "docs", "concept/ordinary", ordinary.replace("Body.", "Changed."), 1
    )
    store.delete_page("docs", "concept/ordinary", 2)
    store.specification_mode = "disabled"
    store.write_page(
        "docs", "specification/disabled", _specification_markdown("disabled-id")
    )

    assert calls == []


@pytest.mark.postgres_integration
def test_domain_duplicates_are_coherent_and_strict_rejection_preserves_projection(
    store_factory,
):
    store = store_factory()
    store.specification_mode = "strict"
    store.write_page(
        "docs", "specification/first", _specification_markdown("shared-id")
    )
    before = store.specification_context("docs", "shared-id")
    assert before is not None

    with pytest.raises(ValueError, match="invalid specification page"):
        store.write_page(
            "docs", "specification/second", _specification_markdown("shared-id")
        )

    assert store.read_page("docs", "specification/second") is None
    assert store.specification_context("docs", "shared-id") == before

    store.specification_mode = "optional"
    result = store.write_page(
        "docs", "specification/second", _specification_markdown("shared-id")
    )
    assert result["revision"] == 1
    assert store.specification_context("docs", "shared-id") is None
    assert store.specification_status("docs").scenario_count == 0


@pytest.mark.postgres_integration
def test_move_and_delete_preserve_unaffected_resolution_evidence(store_factory):
    store = store_factory()
    store.specification_mode = "strict"
    first = _specification_markdown("first-id", "Original heading")
    second = _specification_markdown("second-id", "Second heading")
    store.write_page("docs", "specification/first", first)
    store.write_page("docs", "specification/second", second)
    context = store.specification_context("docs", "first-id")
    assert context is not None
    binding = context.bindings[0]
    attempt = ResolutionAttempt(
        binding_id=binding.binding_id,
        domain="docs",
        scenario_id="first-id",
        state="unresolved",
        targets=(),
        unresolved_reference=binding.selector,
        graph_revision="graph-1",
        graph_state_fingerprint="sha256:" + "1" * 64,
        specification_source_hash=context.scenario.source_hash,
        checked_at="2026-08-29T12:00:00Z",
        reason=None,
    )
    store.record_specification_resolution(attempt)

    moved = first.replace("## Original heading", "## Moved heading")
    store.update_page("docs", "specification/first", moved, 1)
    after_move = store.specification_context("docs", "first-id")
    assert after_move is not None
    assert after_move.scenario.heading == "Moved heading"
    assert after_move.evidence == (attempt,)

    store.delete_page("docs", "specification/second", 1)
    after_delete = store.specification_context("docs", "first-id")
    assert after_delete is not None
    assert after_delete.evidence == (attempt,)
    assert store.specification_context("docs", "second-id") is None


@pytest.mark.postgres_integration
def test_concurrent_distinct_specification_pages_both_commit_coherent_projection(
    store_factory,
):
    store = store_factory()
    store.specification_mode = "strict"

    def write(item):
        slug, scenario_id = item
        return store.write_page(
            "docs", slug, _specification_markdown(scenario_id)
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(write, (
            ("specification/first", "first-id"),
            ("specification/second", "second-id"),
        )))

    assert [result.get("error") for result in results] == [None, None]
    assert store.specification_context("docs", "first-id") is not None
    assert store.specification_context("docs", "second-id") is not None
    assert store.specification_status("docs").scenario_count == 2


@pytest.mark.postgres_integration
@pytest.mark.parametrize("keep_survivor", [False, True])
def test_optional_delete_refresh_failure_keeps_detached_stale_rows_then_rebuilds(
    store_factory, monkeypatch, keep_survivor
):
    import psycopg

    store = store_factory()
    store.specification_mode = "optional"
    victim = _specification_markdown("victim-id", "Victim behavior")
    store.write_page("docs", "specification/victim", victim)
    if keep_survivor:
        survivor = _specification_markdown("survivor-id", "Survivor behavior")
        store.write_page("docs", "specification/survivor", survivor)

    with psycopg.connect(store._dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT markdown_revision, projection_revision, scenario_count, "
                "binding_count, updated_at FROM iwiki.specification_projection_state"
            )
            metadata_before = cursor.fetchone()

    original_lock = store._lock_specification_domain

    def fail_refresh(*_args, **_kwargs):
        raise RuntimeError("private database detail")

    monkeypatch.setattr(store, "_lock_specification_domain", fail_refresh)
    result = store.delete_page("docs", "specification/victim", 1)

    assert result == {
        "page": "docs/specification/victim.md",
        "deleted": True,
        "warning": "specification projection is stale",
    }
    restarted = store_factory()
    assert [item.scenario_id for item in restarted.search_specifications(
        ("docs",), "victim", 20
    )] == ["victim-id"]
    stale = restarted.specification_context("docs", "victim-id")
    assert stale is not None
    assert stale.projection_state == "stale"
    assert stale.scenario.page_slug == "specification/victim"
    assert restarted.specification_status("docs").state == "stale"
    with psycopg.connect(store._dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT page_id, page_slug FROM iwiki.specification_scenarios "
                "WHERE scenario_id = 'victim-id'"
            )
            assert cursor.fetchone() == (None, "specification/victim")
            cursor.execute(
                "SELECT markdown_revision, projection_revision, scenario_count, "
                "binding_count, updated_at FROM iwiki.specification_projection_state"
            )
            assert cursor.fetchone() == metadata_before

    monkeypatch.setattr(store, "_lock_specification_domain", original_lock)
    rebuilt = restarted.index_domain("docs")

    assert rebuilt["specifications"] == {
        "mode": "optional",
        "state": "ready",
        "scenarios": int(keep_survivor),
        "bindings": 2 * int(keep_survivor),
    }
    assert restarted.search_specifications(("docs",), "victim", 20) == ()
    assert store.specification_context("docs", "victim-id") is None
    ready = store.specification_status("docs")
    assert ready.state == "ready"
    assert ready.scenario_count == int(keep_survivor)


@pytest.mark.postgres_integration
def test_public_index_rebuilds_upgraded_specification_without_metadata(
    clean_postgres,
):
    import psycopg

    from iwiki_mcp.postgres.migrations import MIGRATIONS, MigrationSettings, run_migrations

    settings = MigrationSettings(
        dsn=clean_postgres,
        embed_model="test-embedding",
        embed_dimensions=3,
        statement_timeout_ms=30_000,
        lock_timeout_ms=5_000,
    )
    run_migrations(settings, migrations=MIGRATIONS[:6])
    with psycopg.connect(clean_postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO iwiki.iwikis VALUES ('wiki-a', 'wiki-a')")
            cursor.execute(
                "INSERT INTO iwiki.domains (iwiki_id, slug) "
                "VALUES ('wiki-a', 'docs') RETURNING domain_id"
            )
            domain_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO iwiki.pages (iwiki_id, domain_id, slug, markdown) "
                "VALUES ('wiki-a', %s, 'specification/upgraded', %s)",
                (domain_id, _specification_markdown("upgraded-id")),
            )
    run_migrations(settings)
    store = PostgresStore(
        clean_postgres,
        "wiki-a",
        _cfg(),
        embedder=_embed,
        specification_mode="optional",
    )

    assert store.specification_status("docs").state == "stale"

    rebuilt = store.index_domain("docs")

    assert rebuilt["specifications"] == {
        "mode": "optional",
        "state": "ready",
        "scenarios": 1,
        "bindings": 2,
    }
    assert store.specification_status("docs").state == "ready"
    assert store.specification_context("docs", "upgraded-id") is not None


@pytest.mark.postgres_integration
@pytest.mark.parametrize(
    ("mode", "projection_state"),
    [("optional", "stale"), ("strict", "failed")],
)
def test_public_index_projection_failure_keeps_ordinary_index_and_sanitizes_warning(
    store_factory, monkeypatch, mode, projection_state,
):
    import psycopg

    store = store_factory()
    store.specification_mode = mode
    store.write_page(
        "docs", "specification/stable", _specification_markdown()
    )

    def fail_lock(*_args):
        raise RuntimeError("private database detail")

    monkeypatch.setattr(store, "_lock_specification_domain", fail_lock)

    rebuilt = store.index_domain("docs")

    assert rebuilt["warning"] == "specification projection is stale"
    assert "private database detail" not in str(rebuilt)
    assert rebuilt["specifications"]["state"] == projection_state
    with psycopg.connect(store._dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM iwiki.chunks c JOIN iwiki.domains d "
                "ON d.iwiki_id = c.iwiki_id AND d.domain_id = c.domain_id "
                "WHERE d.iwiki_id = %s AND d.slug = 'docs'",
                (store.iwiki_id,),
            )
            assert cursor.fetchone()[0] > 0


@pytest.mark.postgres_integration
@pytest.mark.parametrize("first", ["refresh", "resolution"])
def test_source_change_concurrency_never_persists_obsolete_resolution(
    store_factory, monkeypatch, first,
):
    import psycopg
    from threading import Event

    store = store_factory()
    store.specification_mode = "strict"
    markdown = _specification_markdown()
    created = store.write_page("docs", "specification/stable", markdown)
    context = store.specification_context("docs", "stable-id")
    binding = context.bindings[0]
    attempt = ResolutionAttempt(
        binding_id=binding.binding_id,
        domain="docs",
        scenario_id="stable-id",
        state="unresolved",
        targets=(),
        unresolved_reference=binding.selector,
        graph_revision="graph-2",
        graph_state_fingerprint="sha256:" + "2" * 64,
        specification_source_hash=context.scenario.source_hash,
        checked_at="2026-08-29T13:00:00Z",
        reason=None,
    )
    changed_markdown = markdown.replace("RunStable", "RunChanged")
    with psycopg.connect(store._dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE iwiki.pages SET markdown = %s, revision = revision + 1 "
                "WHERE iwiki_id = %s AND slug = %s",
                (changed_markdown, store.iwiki_id, "specification/stable"),
            )
    projection = assemble_projection(
        "docs",
        [PageSnapshot(
            "specification/stable", changed_markdown, created["revision"] + 1
        )],
    )
    locked = Event()
    release = Event()
    if first == "refresh":
        original = store._specification_projection_from_cursor

        def pause_first(cursor, domain, domain_id=None):
            if domain_id is not None and not locked.is_set():
                locked.set()
                assert release.wait(5)
            return original(cursor, domain, domain_id)

        monkeypatch.setattr(
            store, "_specification_projection_from_cursor", pause_first
        )

        def first_call(executor):
            return executor.submit(store.replace_specification_projection, projection)

        def second_call(executor):
            return executor.submit(store.record_specification_resolution, attempt)
    else:
        original = store._record_specification_resolution

        def pause_first(cursor, domain_id, value):
            if not locked.is_set():
                locked.set()
                assert release.wait(5)
            return original(cursor, domain_id, value)

        monkeypatch.setattr(store, "_record_specification_resolution", pause_first)

        def first_call(executor):
            return executor.submit(store.record_specification_resolution, attempt)

        def second_call(executor):
            return executor.submit(store.replace_specification_projection, projection)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_result = first_call(executor)
        assert locked.wait(5)
        second_result = second_call(executor)
        release.set()
        first_result.result(timeout=5)
        if first == "refresh":
            with pytest.raises(
                ValueError, match="resolution attempt does not match projection"
            ):
                second_result.result(timeout=5)
        else:
            second_result.result(timeout=5)

    after = store.specification_context("docs", "stable-id")
    assert after.scenario.source_hash == projection.scenarios[0].source_hash
    assert after.scenario.source_hash != attempt.specification_source_hash
    assert after.evidence == ()
