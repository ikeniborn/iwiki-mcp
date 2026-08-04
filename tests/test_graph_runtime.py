import multiprocessing
import os
import subprocess
from pathlib import Path
import sqlite3
import time
from hashlib import sha256

import pytest

import iwiki_mcp.graph as graph
from iwiki_mcp.engine import graph_store
from iwiki_mcp.graph import markdown_fingerprint
from iwiki_mcp.lock import base_lock


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    base = tmp_path / "base"
    base.mkdir(parents=True)
    _git(base, "init", "-q")
    _git(base, "config", "user.email", "test@example.com")
    _git(base, "config", "user.name", "Test User")
    (base / "alpha" / "concept").mkdir(parents=True)
    (base / "alpha" / "concept" / "a.md").write_text("# A\n", encoding="utf-8")
    (base / "alpha" / "index.jsonl").write_text("{}\n", encoding="utf-8")
    (base / "alpha" / "log.jsonl").write_text("{}\n", encoding="utf-8")
    _git(base, "add", "-A")
    _git(base, "commit", "-q", "-m", "seed")
    return base


def _commit(base: Path, message: str = "change") -> str:
    _git(base, "add", "-A")
    _git(base, "commit", "-q", "-m", message)
    return _git(base, "rev-parse", "HEAD")


def _stored_pages(base: Path, domain: str) -> list[tuple[str, str]]:
    with graph_store.GraphStore(base).read_snapshot() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT file, content_hash FROM pages "
                "WHERE domain = ? ORDER BY file",
                (domain,),
            )
        ]


def _stored_edges(base: Path, domain: str) -> list[str]:
    with graph_store.GraphStore(base).read_snapshot() as connection:
        return [
            row[0]
            for row in connection.execute(
                "SELECT edges.target_page_id FROM edges "
                "JOIN pages ON pages.page_id = edges.source_page_id "
                "WHERE pages.domain = ? ORDER BY edges.target_page_id",
                (domain,),
            )
        ]


def _hold_base_lock(base: str, ready, release) -> None:
    with base_lock(base):
        ready.set()
        release.wait(5)


def _hold_sqlite_writer(base: str, ready) -> None:
    store = graph_store.GraphStore(base)
    with store.transaction():
        ready.set()
        time.sleep(0.15)


def _crash_while_rebuilding(base: str, domain: str) -> None:
    graph_store.GraphStore(base)._mark_domain_rebuilding(domain)
    os._exit(0)


def test_markdown_fingerprint_is_deterministic_and_ignores_portable_stores(tmp_path):
    base = _repo(tmp_path)

    first = markdown_fingerprint(str(base), "alpha")
    (base / "alpha" / "index.jsonl").write_text('{"changed": true}\n')
    (base / "alpha" / "log.jsonl").write_text('{"changed": true}\n')
    second = markdown_fingerprint(str(base), "alpha")

    assert second.value == first.value
    assert second.indexed_commit == first.indexed_commit


def test_clean_git_fingerprint_does_not_read_markdown_bodies(tmp_path, monkeypatch):
    base = _repo(tmp_path)

    def fail_read(_path):
        raise AssertionError("clean Markdown body was read")

    monkeypatch.setattr(Path, "read_bytes", fail_read)

    result = markdown_fingerprint(str(base), "alpha")

    assert result.value


def test_dirty_untracked_deleted_and_renamed_markdown_change_fingerprint(tmp_path):
    base = _repo(tmp_path)
    page = base / "alpha" / "concept" / "a.md"
    original = markdown_fingerprint(str(base), "alpha").value

    page.write_text("# Changed\n", encoding="utf-8")
    dirty = markdown_fingerprint(str(base), "alpha").value
    _git(base, "restore", "alpha/concept/a.md")

    untracked_page = base / "alpha" / "concept" / "new.md"
    untracked_page.write_text("# New\n", encoding="utf-8")
    untracked = markdown_fingerprint(str(base), "alpha").value
    untracked_page.unlink()

    page.unlink()
    deleted = markdown_fingerprint(str(base), "alpha").value
    _git(base, "restore", "alpha/concept/a.md")

    _git(base, "mv", "alpha/concept/a.md", "alpha/concept/renamed.md")
    renamed = markdown_fingerprint(str(base), "alpha").value

    assert len({original, dirty, untracked, deleted, renamed}) == 5


def test_reserved_markdown_artifacts_do_not_change_fingerprint(tmp_path):
    base = _repo(tmp_path)
    original = markdown_fingerprint(str(base), "alpha").value

    (base / "alpha" / "index.md").write_text("generated", encoding="utf-8")
    (base / "alpha" / "log.md").write_text("generated", encoding="utf-8")

    assert markdown_fingerprint(str(base), "alpha").value == original


def test_ignored_markdown_changes_fingerprint_and_rebuilds_snapshot(tmp_path):
    base = _repo(tmp_path)
    (base / ".gitignore").write_text("alpha/local.md\n", encoding="utf-8")
    _commit(base, "ignore local page")
    ignored = base / "alpha" / "local.md"
    ignored.write_text("# Local one\n", encoding="utf-8")

    first_fingerprint = markdown_fingerprint(str(base), "alpha").value
    first = graph.scoped_graph(str(base), ["alpha"])
    assert first is not None
    first_hash = dict(_stored_pages(base, "alpha"))["local.md"]

    changed_content = "# Local two\n"
    ignored.write_text(changed_content, encoding="utf-8")
    second_fingerprint = markdown_fingerprint(str(base), "alpha").value
    second = graph.scoped_graph(str(base), ["alpha"])
    assert second is not None
    second_hash = dict(_stored_pages(base, "alpha"))["local.md"]

    assert second_fingerprint != first_fingerprint
    assert second_hash == sha256(changed_content.encode()).hexdigest()
    assert second_hash != first_hash


def test_scoped_graph_lazily_builds_missing_store_and_rebuilds_external_change(
    tmp_path,
):
    base = _repo(tmp_path)

    first = graph.scoped_graph(str(base), ["alpha"])
    assert first is not None
    assert [file for file, _hash in _stored_pages(base, "alpha")] == [
        "concept/a.md"
    ]

    page = base / "alpha" / "concept" / "a.md"
    page.write_text("# A\n\n[New](concept/new.md)\n", encoding="utf-8")
    (page.parent / "new.md").write_text("# New\n", encoding="utf-8")

    second = graph.scoped_graph(str(base), ["alpha"])

    assert second is not None
    assert [file for file, _hash in _stored_pages(base, "alpha")] == [
        "concept/a.md",
        "concept/new.md",
    ]
    assert _stored_edges(base, "alpha") == ["alpha/concept/new"]


def test_scoped_provider_never_loads_full_domain_snapshot(tmp_path, monkeypatch):
    base = _repo(tmp_path)

    def fail_load(*_args, **_kwargs):
        raise AssertionError("full domain snapshot was loaded")

    monkeypatch.setattr(graph_store.GraphStore, "load_ready_domain", fail_load)

    provider = graph.scoped_graph(str(base), ["alpha"])

    assert provider is not None
    assert provider.neighbors("alpha/concept/a") == ()


def test_scoped_provider_uses_outgoing_and_incoming_edges_with_scope_filter(tmp_path):
    base = _repo(tmp_path)
    alpha = base / "alpha" / "concept"
    (alpha / "a.md").write_text(
        "# A\n\n[B](iwiki://beta/b)\n",
        encoding="utf-8",
    )
    (alpha / "c.md").write_text(
        "# C\n\n[A](concept/a.md)\n",
        encoding="utf-8",
    )
    (base / "beta").mkdir()
    (base / "beta" / "b.md").write_text("# B\n", encoding="utf-8")
    _commit(base, "linked graph")

    local = graph.scoped_graph(str(base), ["alpha"])
    cross = graph.scoped_graph(str(base), ["alpha", "beta"])

    assert local is not None
    assert cross is not None
    assert local.neighbors("alpha/concept/a") == ("alpha/concept/c",)
    assert cross.neighbors("alpha/concept/a") == (
        "alpha/concept/c",
        "beta/b",
    )
    assert cross.neighbors("beta/b") == ("alpha/concept/a",)
    assert local.neighbors("beta/b") == ()


def test_scoped_provider_rejects_domain_marked_dirty_after_creation(tmp_path):
    base = _repo(tmp_path)
    provider = graph.scoped_graph(str(base), ["alpha"])
    assert provider is not None
    graph_store.GraphStore(base).mark_domain_dirty("alpha")

    with pytest.raises(graph.GraphRuntimeError, match="^graph scope is unavailable$"):
        provider.neighbors("alpha/concept/a")


def test_ready_scoped_graph_does_not_acquire_base_lock(tmp_path, monkeypatch):
    base = _repo(tmp_path)
    assert graph.scoped_graph(str(base), ["alpha"]) is not None

    def fail_lock(*_args, **_kwargs):
        raise AssertionError("ready read acquired the Git lock")

    monkeypatch.setattr(graph, "base_lock", fail_lock)

    assert graph.scoped_graph(str(base), ["alpha"]) is not None


def test_lazy_rebuild_serializes_with_base_lock_in_another_process(tmp_path):
    base = _repo(tmp_path)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_base_lock,
        args=(str(base), ready, release),
    )
    holder.start()
    assert ready.wait(5)

    assert graph.scoped_graph(str(base), ["alpha"], timeout=0.05) is None

    release.set()
    holder.join(5)
    assert holder.exitcode == 0
    assert graph.scoped_graph(str(base), ["alpha"]) is not None


def test_lazy_rebuild_waits_for_sqlite_writer_in_another_process(tmp_path):
    base = _repo(tmp_path)
    assert graph.scoped_graph(str(base), ["alpha"]) is not None
    (base / "alpha" / "concept" / "a.md").write_text("# Changed\n")
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    holder = context.Process(target=_hold_sqlite_writer, args=(str(base), ready))
    holder.start()
    assert ready.wait(5)

    refreshed = graph.scoped_graph(str(base), ["alpha"])

    holder.join(5)
    assert holder.exitcode == 0
    assert refreshed is not None


def test_lazy_rebuild_acquires_base_lock_exactly_once(tmp_path, monkeypatch):
    base = _repo(tmp_path)
    acquisitions = 0
    real_lock = graph.base_lock

    def counted_lock(*args, **kwargs):
        nonlocal acquisitions
        acquisitions += 1
        return real_lock(*args, **kwargs)

    monkeypatch.setattr(graph, "base_lock", counted_lock)

    assert graph.scoped_graph(str(base), ["alpha"]) is not None
    assert acquisitions == 1


def test_rebuild_closes_schema_validation_connection(tmp_path, monkeypatch):
    base = _repo(tmp_path)
    store = graph_store.GraphStore(base)
    real_connect = store.connect
    validation = None
    calls = 0

    class ConnectionSpy:
        def __init__(self, connection):
            self.connection = connection
            self.closed = False

        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, *args):
            return self.connection.__exit__(*args)

        def close(self):
            self.closed = True
            self.connection.close()

    def observed_connect():
        nonlocal calls, validation
        calls += 1
        connection = real_connect()
        if calls == 1:
            validation = ConnectionSpy(connection)
            return validation
        return connection

    monkeypatch.setattr(store, "connect", observed_connect)

    graph._rebuild_locked(str(base), ("alpha",), store)

    assert validation is not None
    assert validation.closed is True


def test_freshness_mismatch_commits_dirty_before_rebuilding(tmp_path, monkeypatch):
    base = _repo(tmp_path)
    assert graph.scoped_graph(str(base), ["alpha"]) is not None
    (base / "alpha" / "concept" / "a.md").write_text("# Changed\n")
    transitions = []
    original_dirty = graph_store.GraphStore.mark_domain_dirty
    original_rebuilding = graph_store.GraphStore._mark_domain_rebuilding

    def record_dirty(store, domain):
        original_dirty(store, domain)
        transitions.append((domain, "dirty"))

    def record_rebuilding(store, domain):
        original_rebuilding(store, domain)
        transitions.append((domain, "rebuilding"))

    monkeypatch.setattr(graph_store.GraphStore, "mark_domain_dirty", record_dirty)
    monkeypatch.setattr(
        graph_store.GraphStore, "_mark_domain_rebuilding", record_rebuilding
    )

    assert graph.scoped_graph(str(base), ["alpha"]) is not None
    assert transitions == [("alpha", "dirty"), ("alpha", "rebuilding")]


def test_freshness_mismatch_does_not_dirty_or_rebuild_unchanged_domain(
    tmp_path, monkeypatch
):
    base = _repo(tmp_path)
    (base / "beta").mkdir()
    (base / "beta" / "b.md").write_text("# B\n")
    _commit(base, "beta")
    assert graph.scoped_graph(str(base), ["alpha", "beta"]) is not None
    (base / "alpha" / "concept" / "a.md").write_text("# Changed\n")
    transitions = []
    original_dirty = graph_store.GraphStore.mark_domain_dirty
    original_rebuilding = graph_store.GraphStore._mark_domain_rebuilding

    def record_dirty(store, domain):
        original_dirty(store, domain)
        transitions.append((domain, "dirty"))

    def record_rebuilding(store, domain):
        original_rebuilding(store, domain)
        transitions.append((domain, "rebuilding"))

    monkeypatch.setattr(graph_store.GraphStore, "mark_domain_dirty", record_dirty)
    monkeypatch.setattr(
        graph_store.GraphStore, "_mark_domain_rebuilding", record_rebuilding
    )

    assert graph.scoped_graph(str(base), ["alpha", "beta"]) is not None
    assert transitions == [("alpha", "dirty"), ("alpha", "rebuilding")]


def test_scoped_graph_falls_back_for_missing_or_non_git_base(tmp_path):
    missing = tmp_path / "missing"
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "alpha").mkdir()
    (plain / "alpha" / "a.md").write_text("# A\n", encoding="utf-8")

    assert graph.scoped_graph(str(missing), ["alpha"]) is None
    assert graph.scoped_graph(str(plain), ["alpha"]) is None


def test_scoped_graph_replaces_corrupt_or_incompatible_derived_store(tmp_path):
    for kind in ("corrupt", "future"):
        base = _repo(tmp_path / kind)
        graph_dir = base / ".iwiki"
        graph_dir.mkdir()
        database = graph_dir / "graph.sqlite3"
        if kind == "corrupt":
            database.write_bytes(b"not sqlite")
        else:
            connection = sqlite3.connect(database)
            try:
                connection.execute("PRAGMA user_version = 999")
                connection.commit()
            finally:
                connection.close()

        scoped = graph.scoped_graph(str(base), ["alpha"])

        assert scoped is not None
        assert _stored_pages(base, "alpha")[0][0] == "concept/a.md"


def test_future_schema_replacement_is_atomic_and_never_unlinks_sidecars(
    tmp_path, monkeypatch
):
    base = _repo(tmp_path)
    graph_dir = base / ".iwiki"
    graph_dir.mkdir()
    database = graph_dir / "graph.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("CREATE TABLE future_data(value TEXT)")
        connection.execute("PRAGMA user_version = 999")
        connection.commit()
    finally:
        connection.close()
    real_replace = os.replace
    real_unlink = Path.unlink
    replacements = []

    def observed_replace(source, destination):
        assert Path(destination) == database
        assert database.exists()
        replacements.append((Path(source), Path(destination)))
        return real_replace(source, destination)

    def fail_unlink(path, *args, **kwargs):
        if Path(path).name.startswith("graph.sqlite3"):
            raise AssertionError(f"manually unlinked SQLite file: {path}")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "replace", observed_replace)
    monkeypatch.setattr(Path, "unlink", fail_unlink)

    scoped = graph.scoped_graph(str(base), ["alpha"])

    assert scoped is not None
    assert len(replacements) == 1
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        connection.close()


@pytest.mark.parametrize("holder", ["reader", "writer"])
def test_active_wal_connection_prevents_schema_replacement_and_preserves_file(
    tmp_path, holder
):
    base = _repo(tmp_path)
    graph_dir = base / ".iwiki"
    graph_dir.mkdir()
    database = graph_dir / "graph.sqlite3"
    owner = sqlite3.connect(database)
    owner.execute("PRAGMA journal_mode = WAL")
    owner.execute("CREATE TABLE future_data(value TEXT)")
    owner.execute("INSERT INTO future_data VALUES ('old generation')")
    owner.execute("PRAGMA user_version = 999")
    owner.commit()
    active = sqlite3.connect(database)
    if holder == "reader":
        active.execute("BEGIN")
        active.execute("SELECT * FROM future_data").fetchall()
        owner.execute("INSERT INTO future_data VALUES ('wal frame')")
        owner.commit()
    else:
        active.execute("BEGIN IMMEDIATE")
        active.execute("INSERT INTO future_data VALUES ('uncommitted')")
    inode = database.stat().st_ino

    try:
        assert graph.scoped_graph(str(base), ["alpha"], timeout=0.1) is None
        assert database.stat().st_ino == inode
        check = sqlite3.connect(database)
        try:
            assert check.execute("PRAGMA user_version").fetchone()[0] == 999
        finally:
            check.close()
    finally:
        active.rollback()
        active.close()
        owner.close()


def test_scoped_graph_recovers_process_crash_left_rebuilding_state(tmp_path):
    base = _repo(tmp_path)
    assert graph.scoped_graph(str(base), ["alpha"]) is not None
    store = graph_store.GraphStore(base)
    context = multiprocessing.get_context("spawn")
    worker = context.Process(
        target=_crash_while_rebuilding,
        args=(str(base), "alpha"),
    )
    worker.start()
    worker.join(5)
    assert worker.exitcode == 0

    recovered = graph.scoped_graph(str(base), ["alpha"])

    assert recovered is not None
    with store.read_snapshot() as connection:
        state = connection.execute(
            "SELECT state FROM domains WHERE domain = 'alpha'"
        ).fetchone()[0]
    assert state == "ready"


def test_revision_change_reports_domains_without_absolute_paths(tmp_path):
    base = _repo(tmp_path)
    old = _git(base, "rev-parse", "HEAD")
    (base / "alpha" / "concept" / "a.md").write_text("# Changed\n")
    (base / "beta").mkdir()
    (base / "beta" / "b.md").write_text("# B\n")
    new = _commit(base)

    change = graph.changed_markdown_domains(str(base), old, new)

    assert change.domains == ("alpha", "beta")
    assert str(base) not in repr(change)


def test_revision_change_with_unavailable_old_commit_rebuilds_all_domains(tmp_path):
    base = _repo(tmp_path)
    (base / "beta").mkdir()
    (base / "beta" / "b.md").write_text("# B\n")
    new = _commit(base)

    change = graph.changed_markdown_domains(str(base), "0" * 40, new)

    assert change.domains == ("alpha", "beta")
    assert change.complete is False


def test_revision_change_includes_both_domains_for_cross_domain_rename(tmp_path):
    base = _repo(tmp_path)
    old = _git(base, "rev-parse", "HEAD")
    (base / "beta" / "concept").mkdir(parents=True)
    _git(
        base,
        "mv",
        "alpha/concept/a.md",
        "beta/concept/a.md",
    )
    new = _commit(base, "move domain")

    change = graph.changed_markdown_domains(str(base), old, new)

    assert change.domains == ("alpha", "beta")


def test_pull_refresh_commits_dirty_before_rebuilding_changed_domain(
    tmp_path, monkeypatch
):
    base = _repo(tmp_path)
    assert graph.scoped_graph(str(base), ["alpha"]) is not None
    old = _git(base, "rev-parse", "HEAD")
    (base / "alpha" / "concept" / "a.md").write_text("# Changed\n")
    new = _commit(base, "remote change")
    transitions = []
    original_dirty = graph_store.GraphStore.mark_domain_dirty
    original_rebuilding = graph_store.GraphStore._mark_domain_rebuilding

    def record_dirty(store, domain):
        original_dirty(store, domain)
        transitions.append((domain, "dirty"))

    def record_rebuilding(store, domain):
        original_rebuilding(store, domain)
        transitions.append((domain, "rebuilding"))

    monkeypatch.setattr(graph_store.GraphStore, "mark_domain_dirty", record_dirty)
    monkeypatch.setattr(
        graph_store.GraphStore, "_mark_domain_rebuilding", record_rebuilding
    )

    graph.refresh_revision_change(str(base), old, new, lock_held=True)

    assert transitions == [("alpha", "dirty"), ("alpha", "rebuilding")]
