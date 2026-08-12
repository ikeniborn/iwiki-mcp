"""Process-level evidence for code-graph recovery and atomic publication."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time

from filelock import Timeout
import pytest

from iwiki_mcp.codegraph.indexer import BuildControl
from iwiki_mcp.codegraph.store import CodeGraphStore


def _git(directory: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.mark.skipif(os.name != "posix", reason="fork process evidence")
def test_competing_writer_is_busy_and_reader_sees_complete_revision(
    runtime_pair,
):
    first, second = runtime_pair
    old = first.index(force=True)["revision"]
    with first.runtime._store.read_lease() as connection:
        old_names = tuple(
            row[0] for row in connection.execute(
                "SELECT qualified_name FROM symbols ORDER BY symbol_id"
            )
        )
    first.project_file("src/pkg/service.py").write_text(
        "def changed_by_process_writer():\n    return None\n",
        encoding="utf-8",
    )

    context = multiprocessing.get_context("fork")
    entered = first.project_dir / ".writer-entered"
    release_replace = first.project_dir / ".writer-release-replace"
    verify_pending = first.project_dir / ".writer-verify-pending"
    release_verify = first.project_dir / ".writer-release-verify"
    stop_reader = first.project_dir / ".reader-stop"
    result_reader, result_writer = context.Pipe(duplex=False)
    observation_reader, observation_writer = context.Pipe(duplex=False)

    def reader():
        observations = []
        deadline = time.monotonic() + 15
        while not stop_reader.exists() and time.monotonic() < deadline:
            try:
                with closing(sqlite3.connect(
                    f"file:{second.paths.database.as_posix()}?mode=ro",
                    uri=True,
                    isolation_level=None,
                )) as connection:
                    connection.execute("BEGIN")
                    repository = connection.execute(
                        "SELECT state, revision FROM repositories "
                        "WHERE repository_id = ?",
                        (second.binding.primary,),
                    ).fetchone()
                    names = tuple(
                        row[0] for row in connection.execute(
                            "SELECT qualified_name FROM symbols "
                            "ORDER BY symbol_id"
                        )
                    )
                    connection.execute("ROLLBACK")
                observations.append((repository, names))
            except Exception as exc:
                observations.append(("error", type(exc).__name__))
            time.sleep(0.005)
        observation_writer.send(observations)
        observation_writer.close()

    def writer():
        indexer = first.runtime._indexer
        store = indexer.store
        replace = store.replace_staging
        verify = indexer._verify_published
        verify_calls = 0

        def paused_replace(*args, **kwargs):
            entered.write_text("entered", encoding="utf-8")
            deadline = time.monotonic() + 8
            while not release_replace.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not release_replace.exists():
                raise AssertionError("writer publication was not released")
            return replace(*args, **kwargs)

        def paused_verify(revision):
            nonlocal verify_calls
            verify_calls += 1
            if verify_calls == 2:
                verify_pending.write_text("pending", encoding="utf-8")
                deadline = time.monotonic() + 8
                while (
                    not release_verify.exists()
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                if not release_verify.exists():
                    raise AssertionError("final verification was not released")
            return verify(revision)

        store.replace_staging = paused_replace
        indexer._verify_published = paused_verify
        call_result = first.runtime.index(force=True)
        first.runtime.join_workers(timeout=10)
        result_writer.send({
            "call": call_result,
            "status": first.runtime.status(),
        })
        result_writer.close()

    reader_process = context.Process(target=reader)
    writer_process = context.Process(target=writer)
    reader_process.start()
    writer_process.start()
    try:
        deadline = time.monotonic() + 5
        while not entered.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert entered.exists()
        started = time.monotonic()
        competing = second.index(force=True)
        elapsed = time.monotonic() - started
        release_replace.write_text("release", encoding="utf-8")
        deadline = time.monotonic() + 5
        while not verify_pending.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert verify_pending.exists()
        during = [second.status() for _attempt in range(3)]
        guarded = [second.query_guard() for _attempt in range(3)]
    finally:
        release_replace.write_text("release", encoding="utf-8")
        release_verify.write_text("release", encoding="utf-8")
        writer_process.join(timeout=10)
        stop_reader.write_text("stop", encoding="utf-8")
        reader_process.join(timeout=10)
        for process in (writer_process, reader_process):
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert writer_process.exitcode == 0
    assert reader_process.exitcode == 0
    assert result_reader.poll(2)
    writer_result = result_reader.recv()
    result_reader.close()
    assert observation_reader.poll(2)
    observations = observation_reader.recv()
    observation_reader.close()
    after = second.status()
    assert competing["code"] == "busy"
    assert elapsed < 3
    assert {item["state"] for item in during} == {"rebuilding"}
    assert {item["revision"] for item in during} == {
        writer_result["status"]["revision"]
    }
    assert all(item["fresh"] is False for item in during)
    assert all(item["fresh"] is False for item in guarded)
    assert all(item["results"] == [] for item in guarded)
    assert writer_result["call"]["code"] == "busy"
    rebuilt = writer_result["status"]
    assert rebuilt["state"] == "ready"
    assert rebuilt["revision"] != old
    assert after["state"] == "ready"
    assert after["revision"] == rebuilt["revision"]
    with closing(sqlite3.connect(second.paths.database)) as connection:
        row = connection.execute(
            "SELECT state, revision FROM repositories WHERE repository_id = ?",
            (second.binding.primary,),
        ).fetchone()
        assert row == ("ready", rebuilt["revision"])
        new_names = tuple(
            row[0] for row in connection.execute(
                "SELECT qualified_name FROM symbols ORDER BY symbol_id"
            )
        )
    allowed = {
        (old, old_names),
        (rebuilt["revision"], new_names),
    }
    assert observations
    successful = set()
    for state_revision, names in observations:
        assert state_revision != "error"
        assert state_revision is not None
        assert state_revision[0] == "ready"
        observation = (state_revision[1], names)
        assert observation in allowed
        successful.add(observation)
    assert (old, old_names) in successful
    assert (rebuilt["revision"], new_names) in successful


@pytest.mark.parametrize(
    "fault",
    (
        "replace",
        "metadata_rebuilding",
        "verify_1",
        "ready_pending",
        "verify_2",
        "timing_refresh",
    ),
)
def test_publication_faults_never_expose_unverified_snapshot(
    seed_runtime, fault
):
    old = seed_runtime.index(force=True)["revision"]
    wiki_before = seed_runtime.wiki_hashes()
    seed_runtime.project_file("src/pkg/service.py").write_text(
        f"def changed_at_{fault}():\n    return None\n",
        encoding="utf-8",
    )
    seed_runtime.inject_publication_fault(fault)

    failed = seed_runtime.index(force=True)
    status = seed_runtime.status()
    guarded = seed_runtime.query_guard()

    assert "error" in failed
    assert status["state"] != "ready"
    assert status["fresh"] is False
    assert guarded["fresh"] is False
    assert guarded["results"] == []
    assert seed_runtime.wiki_hashes() == wiki_before
    if fault == "replace":
        assert status["revision"] == old
    else:
        assert status["revision"] != old


@pytest.mark.parametrize("kind", ("corrupt", "schema_v1"))
def test_unusable_database_is_deterministically_quarantined_and_rebuilt(
    seed_runtime, kind
):
    seed_runtime.index(force=True)
    wiki_before = seed_runtime.wiki_hashes()
    for path in (
        seed_runtime.paths.database,
        seed_runtime.paths.wal,
        seed_runtime.paths.shm,
    ):
        path.unlink(missing_ok=True)
    if kind == "corrupt":
        seed_runtime.paths.database.write_bytes(b"task-12-corrupt-cache")
    else:
        with closing(sqlite3.connect(seed_runtime.paths.database)) as connection:
            connection.execute("PRAGMA user_version = 1")
            connection.execute("CREATE TABLE legacy_cache (value TEXT)")
            connection.commit()
    digest = hashlib.sha256(
        seed_runtime.paths.database.read_bytes()
    ).hexdigest()[:16]
    quarantine = seed_runtime.paths.database.with_name(
        f"{seed_runtime.paths.database.name}.corrupt-{digest}"
    )

    rebuilt = seed_runtime.index(force=True)

    assert rebuilt["state"] == "ready"
    assert rebuilt["schema_version"] == 2
    assert quarantine.is_file()
    assert seed_runtime.wiki_hashes() == wiki_before


def test_sql_revision_wins_and_stale_metadata_recovery_keeps_generation(
    seed_runtime,
):
    first = seed_runtime.index(force=True)
    seed_runtime.project_file("src/pkg/service.py").write_text(
        "def metadata_skew_revision():\n    return None\n",
        encoding="utf-8",
    )
    second = seed_runtime.index(force=True)
    metadata = json.loads(
        seed_runtime.paths.metadata.read_text(encoding="utf-8")
    )
    metadata.update({
        "state": "rebuilding",
        "fresh": False,
        "generation": 41,
        "revision": first["revision"],
        "previous_revision": first["revision"],
        "prior_state": "ready",
        "publication_phase": "provisional",
        "recovery_policy": "failed",
    })
    seed_runtime.paths.metadata.write_text(
        json.dumps(metadata, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    status = seed_runtime.status()
    persisted = json.loads(
        seed_runtime.paths.metadata.read_text(encoding="utf-8")
    )

    assert second["revision"] != first["revision"]
    assert status["revision"] == second["revision"]
    assert status["state"] == "failed"
    assert status["fresh"] is False
    assert persisted["revision"] == second["revision"]
    assert persisted["generation"] == 41


def test_added_changed_deleted_files_and_branch_switch_force_full_rebuild(
    ready_runtime,
):
    runtime = ready_runtime
    project = runtime.project_dir
    original_branch = _git(project, "branch", "--show-current")
    wiki_before = runtime.wiki_hashes()
    revisions = [runtime.status()["revision"]]

    added = project / "src/pkg/added.py"
    added.write_text("def added():\n    return 1\n", encoding="utf-8")
    assert "?? src/pkg/added.py" in _git(project, "status", "--porcelain=v1")
    assert runtime.query_guard()["fresh"] is False
    revisions.append(runtime.index(force=False)["revision"])
    _git(project, "add", "src/pkg/added.py")
    _git(project, "commit", "-q", "-m", "add source")

    runtime.project_file("src/pkg/service.py").write_text(
        "def changed():\n    return 2\n", encoding="utf-8"
    )
    assert "M src/pkg/service.py" in _git(
        project, "status", "--porcelain=v1"
    )
    assert runtime.query_guard()["fresh"] is False
    revisions.append(runtime.index(force=False)["revision"])
    _git(project, "add", "src/pkg/service.py")
    _git(project, "commit", "-q", "-m", "change source")

    added.unlink()
    assert "D src/pkg/added.py" in _git(
        project, "status", "--porcelain=v1"
    )
    assert runtime.query_guard()["fresh"] is False
    revisions.append(runtime.index(force=False)["revision"])
    _git(project, "add", "-u", "src/pkg/added.py")
    _git(project, "commit", "-q", "-m", "delete source")
    assert runtime.query_guard()["fresh"] is False
    original_revision = runtime.index(force=False)["revision"]
    revisions.append(original_revision)

    _git(project, "checkout", "-q", "-b", "alternate-source")
    runtime.project_file("src/pkg/service.py").write_text(
        "def alternate_branch():\n    return 3\n", encoding="utf-8"
    )
    _git(project, "add", "src/pkg/service.py")
    _git(project, "commit", "-q", "-m", "alternate source")
    assert runtime.query_guard()["fresh"] is False
    alternate_revision = runtime.index(force=False)["revision"]
    revisions.append(alternate_revision)

    _git(project, "checkout", "-q", original_branch)
    assert runtime.query_guard()["fresh"] is False
    switched_revision = runtime.index(force=False)["revision"]

    assert len(set(revisions)) == len(revisions)
    assert switched_revision == original_revision
    assert switched_revision != alternate_revision
    assert runtime.status()["state"] == "ready"
    assert runtime.wiki_hashes() == wiki_before


def test_cancellation_before_publication_preserves_canonical_revision(
    ready_runtime,
):
    runtime = ready_runtime
    old = runtime.status()["revision"]
    runtime.project_file("src/pkg/service.py").write_text(
        "def cancelled_before_publication():\n    return None\n",
        encoding="utf-8",
    )
    control = BuildControl()
    control.cancel()

    with pytest.raises(Timeout):
        runtime.runtime._indexer.build(
            force=True,
            deadline=time.monotonic() + 5,
            control=control,
        )

    assert runtime.status()["revision"] == old
    assert not list(runtime.paths.database.parent.glob(
        f"{runtime.paths.database.name}.staging-*"
    ))


def test_cancellation_after_atomic_entry_finishes_ordered_protocol(
    ready_runtime,
):
    runtime = ready_runtime
    old = runtime.status()["revision"]
    runtime.project_file("src/pkg/service.py").write_text(
        "def cancelled_after_entry():\n    return None\n",
        encoding="utf-8",
    )
    control = BuildControl()
    store = runtime.runtime._indexer.store
    replace = store.replace_staging
    release = threading.Event()

    def paused_replace(*args, **kwargs):
        assert control.publication_entered.is_set()
        assert release.wait(timeout=5)
        return replace(*args, **kwargs)

    store.replace_staging = paused_replace
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            runtime.runtime._indexer.build,
            force=True,
            deadline=time.monotonic() + 5,
            control=control,
        )
        assert control.publication_entered.wait(timeout=5)
        control.cancel()
        release.set()
        rebuilt = future.result(timeout=8)

    assert rebuilt["state"] == "ready"
    assert rebuilt["revision"] != old
    assert runtime.status()["revision"] == rebuilt["revision"]
    assert runtime.status()["fresh"] is True


def test_staging_cleanup_removes_only_callers_registered_artifacts(
    seed_runtime,
):
    first = CodeGraphStore(
        seed_runtime.paths.database,
        cache_base=seed_runtime.binding.base,
    )
    second = CodeGraphStore(
        seed_runtime.paths.database,
        cache_base=seed_runtime.binding.base,
    )
    first_staging = first.create_staging_path()
    second_staging = second.create_staging_path()

    first.discard_staging(first_staging)

    assert not first_staging.exists()
    assert second_staging.is_file()
    second.discard_staging(second_staging)
    assert not second_staging.exists()
