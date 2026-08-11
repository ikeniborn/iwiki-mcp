"""Full-build indexer and fail-soft runtime lifecycle tests."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import json
import logging
from pathlib import Path
import threading
import time

import pytest
from filelock import Timeout

from iwiki_mcp import base as wiki_base
from iwiki_mcp.codegraph import models as codegraph_models
from iwiki_mcp.codegraph.discovery import DiscoveryError
from iwiki_mcp.codegraph.fingerprint import parser_fingerprint
from iwiki_mcp.codegraph.indexer import (
    AdapterFactory,
    CodeGraphParseError,
    CodeGraphStaleError,
    CodeGraphStoreFailure,
    CodeGraphUnsafePathError,
)
from iwiki_mcp.codegraph import location as codegraph_location
from iwiki_mcp.codegraph.location import CodeGraphLocationResolver
from iwiki_mcp.codegraph.languages.python import PythonAdapter
from iwiki_mcp.codegraph.runtime import CodeGraphRuntime
from iwiki_mcp.codegraph.schema import SCHEMA_VERSION, CodeGraphStoreError
from iwiki_mcp.codegraph.store import (
    CodeGraphStore,
    code_graph_read_lock,
    code_graph_write_lock,
)
from iwiki_mcp.base import Binding


def test_adapter_factory_binds_fresh_isolated_instances():
    created = []

    def create(source_paths):
        adapter = PythonAdapter(
            "domain",
            source_paths,
            parser_version="parser:test",
        )
        created.append(adapter)
        return adapter

    factory = AdapterFactory(
        create=create,
        extensions=(".py",),
        parser_version="parser:test",
        grammar_version="grammar:test",
        adapter_version="adapter:test",
    )

    first = factory.bind(("pkg/__init__.py",))
    first_mapping = dict(first.adapter._module_names)
    second = factory.bind(("other.py",))

    assert first.adapter is created[0]
    assert second.adapter is created[1]
    assert first.adapter is not second.adapter
    assert first.adapter._module_names == first_mapping
    assert first.adapter.repository_id == "domain"
    assert second.adapter.repository_id == "domain"
    assert not hasattr(first, "factory")


def test_runtime_defers_fresh_adapter_binding_until_each_build(seed_binding):
    calls = []

    def create(source_paths):
        adapter = PythonAdapter(
            "project",
            source_paths,
            parser_version="parser:test",
        )
        calls.append((source_paths, adapter))
        return adapter

    runtime = CodeGraphRuntime(
        seed_binding,
        adapter_factories={
            "python": AdapterFactory(
                create=create,
                extensions=(".py",),
                parser_version="parser:test",
                grammar_version="grammar:test",
                adapter_version="adapter:test",
            )
        },
    )

    assert calls == []
    assert runtime.index(force=True)["state"] == "ready"
    assert runtime.index(force=True)["state"] == "ready"
    runtime.join_workers(timeout=5)

    assert [item[0] for item in calls] == [
        ("src/pkg/__init__.py", "src/pkg/service.py"),
        ("src/pkg/__init__.py", "src/pkg/service.py"),
    ]
    assert calls[0][1] is not calls[1][1]


def test_publication_primitive_orders_two_canonical_verifications():
    from iwiki_mcp.codegraph.store import run_publication_protocol

    events = []

    run_publication_protocol(
        replace=lambda: events.append("replace"),
        metadata_rebuilding=lambda: events.append("metadata_rebuilding"),
        verify_1=lambda: events.append("verify_1"),
        metadata_ready_pending=lambda: events.append("metadata_ready_pending"),
        verify_2=lambda: events.append("verify_2"),
        timing_refresh=lambda: events.append("timing_refresh"),
    )

    assert events == [
        "replace",
        "metadata_rebuilding",
        "verify_1",
        "metadata_ready_pending",
        "verify_2",
        "timing_refresh",
    ]


def test_indexer_builds_noops_and_preserves_previous_revision_on_failure(
    seed_runtime,
):
    first = seed_runtime.index(force=False)
    second = seed_runtime.index(force=False)

    assert first["state"] == "ready"
    assert first["no_op"] is False
    assert second["no_op"] is True
    assert second["revision"] == first["revision"]

    seed_runtime.fail_before_publish = True
    failed = seed_runtime.index(force=True)

    assert failed == {
        "error": "code graph rebuild failed",
        "code": "rebuild_failed",
        "hint": "inspect wiki_code_status and retry",
    }
    assert seed_runtime.status()["revision"] == first["revision"]
    assert seed_runtime.status()["state"] == "failed"


def test_failed_writer_leaves_rebuilding_for_later_status_recovery(
    seed_runtime, monkeypatch
):
    indexer = seed_runtime.runtime._indexer
    real_publish = indexer.store.publish_metadata
    published_states = []

    def observed_publish(metadata_path, staging, **kwargs):
        payload = json.loads(Path(staging).read_text(encoding="utf-8"))
        published_states.append(payload["state"])
        return real_publish(metadata_path, staging, **kwargs)

    def fail_parse(_discovered, _config):
        raise CodeGraphParseError("parse fixture secret")

    monkeypatch.setattr(indexer.store, "publish_metadata", observed_publish)
    monkeypatch.setattr(indexer, "_parse", fail_parse)

    failed = seed_runtime.index(force=True)
    stale = json.loads(seed_runtime.paths.metadata.read_text(encoding="utf-8"))

    assert failed["code"] == "parse_failed"
    assert published_states == ["rebuilding"]
    assert stale["state"] == "rebuilding"

    recovered = seed_runtime.status()

    assert recovered["state"] == "failed"
    assert recovered["fresh"] is False
    assert "parse fixture secret" not in str(failed)
    assert "parse fixture secret" not in str(recovered)


def test_disabled_runtime_creates_or_reads_no_database_and_build_never_embeds(
    seed_runtime, monkeypatch
):
    def fail_embed(*_args, **_kwargs):
        raise AssertionError("code graph called the Wiki embedding path")

    monkeypatch.setattr("iwiki_mcp.indexer.embed_texts", fail_embed)
    database_reads = []
    real_open = CodeGraphStore.open_existing

    def observed_open(store):
        database_reads.append(store.path.name)
        return real_open(store)

    monkeypatch.setattr(CodeGraphStore, "open_existing", observed_open)
    disabled = seed_runtime.with_config(enabled=False)

    assert disabled.status()["code"] == "not_configured"
    disabled_guard = disabled.query_guard()
    assert disabled_guard["code"] == "not_configured"
    assert disabled_guard["hint"] == (
        "configure a primary domain and enable code_graph"
    )
    assert disabled_guard["results"] == []
    assert disabled.database_accesses == []
    assert database_reads == []
    assert not disabled.paths.database.exists()

    assert seed_runtime.index(force=True)["state"] == "ready"
    assert seed_runtime.embedding_requests == []


def test_runtime_status_defers_git_exclusion_until_build(
    seed_binding, monkeypatch, production_runtime_factory
):
    calls = []

    def record_exclusion(base):
        calls.append(base)
        return True

    monkeypatch.setattr(
        wiki_base, "ensure_graph_store_excluded", record_exclusion
    )

    runtime = production_runtime_factory(seed_binding)
    status = runtime.status()

    assert status["state"] == "missing"
    assert calls == []

    built = runtime.index(force=True)
    runtime.join_workers()

    assert built["state"] == "ready"
    assert calls == [seed_binding.base]


def test_cache_parent_symlink_fails_closed_without_outside_writes(
    seed_binding, tmp_path, production_runtime_factory
):
    outside = tmp_path / "outside-cache"
    outside.mkdir()
    cache = Path(seed_binding.base) / ".iwiki"
    cache.symlink_to(outside, target_is_directory=True)

    runtime = production_runtime_factory(seed_binding)
    status = runtime.status()
    built = runtime.index(force=True)

    assert status["code"] == "unsafe_path"
    assert built["code"] == "unsafe_path"
    assert list(outside.iterdir()) == []


def test_base_symlink_fails_closed_without_outside_writes(
    seed_binding, tmp_path, production_runtime_factory
):
    outside = tmp_path / "outside-base"
    outside.mkdir()
    linked_base = tmp_path / "linked-base"
    linked_base.symlink_to(outside, target_is_directory=True)
    binding = replace(seed_binding, base=str(linked_base))

    runtime = production_runtime_factory(binding)
    status = runtime.status()
    built = runtime.index(force=True)

    assert status["code"] == "unsafe_path"
    assert built["code"] == "unsafe_path"
    assert list(outside.iterdir()) == []


def test_status_reads_metadata_and_schema_without_initializing_parser(
    ready_runtime, monkeypatch
):
    def fail_parser(_self):
        raise AssertionError("status initialized a parser")

    monkeypatch.setattr(PythonAdapter, "_get_parser", fail_parser)

    status = ready_runtime.status()

    assert status["state"] == "ready"
    assert status["fresh"] is True


def test_two_status_readers_coexist_under_shared_lock(
    ready_runtime, monkeypatch
):
    first = ready_runtime.with_config()
    second = ready_runtime.with_config()
    first_reading = threading.Event()
    release_first = threading.Event()
    real_read = first.runtime._read_status

    def held_read(*args, **kwargs):
        first_reading.set()
        assert release_first.wait(timeout=5)
        return real_read(*args, **kwargs)

    monkeypatch.setattr(first.runtime, "_read_status", held_read)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(first.status)
        assert first_reading.wait(timeout=5)
        second_future = executor.submit(second.status)
        second_status = second_future.result(timeout=2)
        release_first.set()
        first_status = first_future.result(timeout=5)

    assert first_status["state"] == "ready"
    assert second_status["state"] == "ready"


def test_writer_lock_before_rebuilding_metadata_blocks_status_reader(
    ready_runtime, monkeypatch
):
    observer = ready_runtime.with_config()
    writer_holds_lock = threading.Event()
    release_writer = threading.Event()
    indexer = ready_runtime.runtime._indexer
    real_publish_record = indexer._publish_metadata_record

    def held_initial_publish(metadata, *, deadline):
        if metadata["state"] == "rebuilding":
            writer_holds_lock.set()
            assert release_writer.wait(timeout=5)
        return real_publish_record(metadata, deadline=deadline)

    monkeypatch.setattr(indexer, "_publish_metadata_record", held_initial_publish)
    with ThreadPoolExecutor(max_workers=1) as executor:
        build_future = executor.submit(ready_runtime.index, force=True)
        assert writer_holds_lock.wait(timeout=5)
        observed = observer.status()
        release_writer.set()
        rebuilt = build_future.result(timeout=5)

    assert observed["state"] == "rebuilding"
    assert observed["fresh"] is False
    assert rebuilt["state"] == "ready"


def test_nonready_state_guard_returns_stable_empty_contract(seed_runtime):
    hints = set()
    for state in ("missing", "dirty", "failed"):
        runtime = seed_runtime.with_state(state, auto_rebuild="off")
        out = runtime.query_guard()

        assert out["state"] == state
        assert out["fresh"] is False
        assert out["results"] == []
        assert out["hint"]
        hints.add(out["hint"])

    ready = seed_runtime.with_state("ready", auto_rebuild="off").query_guard()
    assert ready["fresh"] is True
    assert ready["results"] == []
    assert hints == {"run wiki_code_index"}


def test_dirty_bounded_auto_rebuild_attempts_once_and_becomes_fresh(seed_runtime):
    bounded = seed_runtime.with_state(
        "dirty", auto_rebuild="bounded", max_rebuild_seconds=2
    )

    out = bounded.query_guard()

    assert out["fresh"] is True
    assert out["state"] == "ready"
    assert bounded.build_attempts == 1


def test_missing_primary_is_not_configured_and_touches_no_database(
    seed_without_primary,
):
    runtime = CodeGraphRuntime(seed_without_primary)

    assert runtime.status()["code"] == "not_configured"


def test_fake_runtime_records_only_enabled_code_activity(
    seed_binding, fake_runtime_factory
):
    runtime = fake_runtime_factory(seed_binding)

    runtime.status()
    runtime.index(force=True)
    runtime.query_guard()

    assert runtime.embedding_requests == []
    assert runtime.database_accesses == ["status", "index", "query"]
    assert runtime.build_attempts == 1
    disabled = runtime.with_config(enabled=False)
    assert disabled.status()["code"] == "not_configured"
    assert disabled.database_accesses == []


def test_build_reports_observability_without_source_text(seed_runtime):
    out = seed_runtime.index(force=True)

    assert out["state"] == "ready"
    assert out["counts"]["languages"] == {"python": 2}
    assert out["counts"]["files"] == 2
    assert out["counts"]["symbols"] == 2
    assert "relations" in out["counts"]
    assert "resolution_ratios" in out
    assert set(out["phase_timings_ms"]) == {
        "discovery",
        "fingerprint",
        "parsing",
        "resolution",
        "persistence",
        "validation",
        "publication",
    }
    assert out["parser_errors"] == 0
    assert "return str(value)" not in str(out)
    assert str(seed_runtime.project_dir) not in str(out)
    assert seed_runtime.status()["phase_timings_ms"] == out["phase_timings_ms"]


def test_changed_source_triggers_full_rebuild(seed_runtime):
    first = seed_runtime.index(force=False)
    seed_runtime.project_file("src/pkg/service.py").write_text(
        "class Service:\n    def changed(self):\n        return None\n",
        encoding="utf-8",
    )

    rebuilt = seed_runtime.index(force=False)

    assert rebuilt["no_op"] is False
    assert rebuilt["revision"] != first["revision"]


def test_query_guard_materializes_changed_source_as_dirty_without_rows(
    ready_runtime,
):
    initial = ready_runtime.status()
    ready_runtime.project_file("src/pkg/service.py").write_text(
        "class Service:\n    def changed(self):\n        return None\n",
        encoding="utf-8",
    )

    assert ready_runtime.status()["state"] == "ready"
    guarded = ready_runtime.query_guard()

    assert guarded["state"] == "dirty"
    assert guarded["code"] == "stale"
    assert guarded["fresh"] is False
    assert guarded["results"] == []
    assert ready_runtime.status()["state"] == "dirty"
    assert ready_runtime.status()["revision"] == initial["revision"]


def test_status_and_guard_show_rebuilding_during_current_process_build(
    ready_runtime, monkeypatch
):
    entered = threading.Event()
    release = threading.Event()
    real_parse = ready_runtime.runtime._indexer._parse

    def held_parse(discovered, config):
        entered.set()
        assert release.wait(timeout=5)
        return real_parse(discovered, config)

    monkeypatch.setattr(ready_runtime.runtime._indexer, "_parse", held_parse)
    with ThreadPoolExecutor(max_workers=1) as executor:
        build = executor.submit(ready_runtime.index, force=True)
        assert entered.wait(timeout=5)
        status = ready_runtime.status()
        guarded = ready_runtime.query_guard()
        release.set()
        rebuilt = build.result(timeout=5)

    assert status["state"] == "rebuilding"
    assert status["fresh"] is False
    assert guarded["state"] == "rebuilding"
    assert guarded["fresh"] is False
    assert guarded["results"] == []
    assert rebuilt["state"] == "ready"


def test_second_runtime_observes_shared_writer_as_rebuilding(
    ready_runtime, monkeypatch
):
    other_runtime = ready_runtime.with_config()
    entered = threading.Event()
    release = threading.Event()
    real_parse = ready_runtime.runtime._indexer._parse

    def held_parse(discovered, config):
        entered.set()
        assert release.wait(timeout=5)
        return real_parse(discovered, config)

    monkeypatch.setattr(ready_runtime.runtime._indexer, "_parse", held_parse)
    with ThreadPoolExecutor(max_workers=1) as executor:
        build = executor.submit(ready_runtime.index, force=True)
        assert entered.wait(timeout=5)
        status = other_runtime.status()
        guarded = other_runtime.query_guard()
        release.set()
        rebuilt = build.result(timeout=5)

    assert status["state"] == "rebuilding"
    assert status["fresh"] is False
    assert guarded["state"] == "rebuilding"
    assert guarded["fresh"] is False
    assert guarded["results"] == []
    assert rebuilt["state"] == "ready"


def test_writer_waits_for_status_shared_snapshot(
    ready_runtime, monkeypatch
):
    observer = ready_runtime.with_config()
    canonical_read = threading.Event()
    writer_entered = threading.Event()
    release_status = threading.Event()
    release_writer = threading.Event()
    real_status_read = observer.runtime._read_status
    real_parse = ready_runtime.runtime._indexer._parse

    def paused_status_read(*args, **kwargs):
        result = real_status_read(*args, **kwargs)
        canonical_read.set()
        assert release_status.wait(timeout=5)
        return result

    def held_parse(discovered, config):
        writer_entered.set()
        assert release_writer.wait(timeout=5)
        return real_parse(discovered, config)

    monkeypatch.setattr(observer.runtime, "_read_status", paused_status_read)
    monkeypatch.setattr(ready_runtime.runtime._indexer, "_parse", held_parse)
    with ThreadPoolExecutor(max_workers=2) as executor:
        status_future = executor.submit(observer.status)
        assert canonical_read.wait(timeout=5)
        build_future = executor.submit(ready_runtime.index, force=True)
        assert not writer_entered.wait(timeout=0.2)
        release_status.set()
        observed = status_future.result(timeout=5)
        assert writer_entered.wait(timeout=5)
        release_writer.set()
        rebuilt = build_future.result(timeout=5)

    assert observed["state"] == "ready"
    assert observed["fresh"] is True
    assert rebuilt["state"] == "ready"


@pytest.mark.parametrize("stale_state", ["rebuilding", "recovering"])
def test_crash_stale_metadata_recovers_without_writer(
    ready_runtime, stale_state
):
    metadata = json.loads(
        ready_runtime.paths.metadata.read_text(encoding="utf-8")
    )
    metadata.update({
        "state": stale_state,
        "generation": 41,
        "warnings": ["metrics_incomplete"],
    })
    metadata.pop("duration_ms", None)
    metadata.pop("phase_timings_ms", None)
    ready_runtime.paths.metadata.write_text(
        json.dumps(metadata, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    recovered = ready_runtime.status()
    persisted = json.loads(
        ready_runtime.paths.metadata.read_text(encoding="utf-8")
    )

    assert recovered["state"] == "ready"
    assert recovered["fresh"] is True
    assert persisted["state"] == "ready"
    assert persisted["generation"] == 41
    assert persisted["revision"] == recovered["revision"]
    assert "metadata_reconstructed" in recovered["warnings"]
    assert "metrics_incomplete" in recovered["warnings"]
    assert "duration_ms" not in recovered
    assert "phase_timings_ms" not in recovered


def test_crash_stale_metadata_over_dirty_sql_recovers_as_failed(seed_runtime):
    runtime = seed_runtime.with_state("dirty", auto_rebuild="off")
    metadata = json.loads(runtime.paths.metadata.read_text(encoding="utf-8"))
    metadata.update({
        "state": "rebuilding",
        "generation": 42,
        "warnings": ["metrics_incomplete"],
    })
    runtime.paths.metadata.write_text(
        json.dumps(metadata, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    recovered = runtime.status()
    persisted = json.loads(
        runtime.paths.metadata.read_text(encoding="utf-8")
    )

    assert recovered["state"] == "failed"
    assert recovered["fresh"] is False
    assert persisted["state"] == "failed"
    assert persisted["generation"] == 42
    assert persisted["state"] != "recovering"


def test_old_writer_recovery_cannot_overwrite_new_writer_generation(
    ready_runtime, monkeypatch
):
    old_writer = ready_runtime
    new_writer = ready_runtime.with_config()
    observer = ready_runtime.with_config()
    old_indexer = old_writer.runtime._indexer

    def fail_old_parse(_discovered, _config):
        raise CodeGraphParseError("old writer failed")

    monkeypatch.setattr(old_indexer, "_parse", fail_old_parse)
    assert old_writer.index(force=True)["code"] == "parse_failed"
    old_metadata = json.loads(
        ready_runtime.paths.metadata.read_text(encoding="utf-8")
    )
    assert old_metadata["state"] == "rebuilding"

    recovery_waiting = threading.Event()
    release_recovery = threading.Event()
    new_writer_entered = threading.Event()
    release_new_writer = threading.Event()
    real_recover = observer.runtime._recover_stale_metadata
    real_new_parse = new_writer.runtime._indexer._parse

    def paused_recovery(expected):
        recovery_waiting.set()
        assert release_recovery.wait(timeout=5)
        return real_recover(expected)

    def held_new_parse(discovered, config):
        new_writer_entered.set()
        assert release_new_writer.wait(timeout=5)
        return real_new_parse(discovered, config)

    monkeypatch.setattr(observer.runtime, "_recover_stale_metadata", paused_recovery)
    monkeypatch.setattr(new_writer.runtime._indexer, "_parse", held_new_parse)
    with ThreadPoolExecutor(max_workers=2) as executor:
        recovery_future = executor.submit(observer.status)
        assert recovery_waiting.wait(timeout=5)
        build_future = executor.submit(new_writer.index, force=True)
        assert new_writer_entered.wait(timeout=5)
        release_recovery.set()
        observed = recovery_future.result(timeout=5)
        release_new_writer.set()
        rebuilt = build_future.result(timeout=5)

    persisted = json.loads(
        ready_runtime.paths.metadata.read_text(encoding="utf-8")
    )
    assert observed["state"] == "rebuilding"
    assert rebuilt["state"] == "ready"
    assert persisted["state"] == "ready"
    assert persisted["generation"] > old_metadata["generation"]


def test_build_uses_exact_five_persistent_code_graph_paths(seed_runtime):
    assert seed_runtime.index(force=True)["state"] == "ready"
    paths = seed_runtime.paths
    expected = {
        paths.database,
        paths.wal,
        paths.shm,
        paths.lock,
        paths.metadata,
    }

    assert set(vars(paths)) == {"database", "wal", "shm", "lock", "metadata"}
    assert {path.name for path in expected} == {
        "code-project.sqlite3",
        "code-project.sqlite3-wal",
        "code-project.sqlite3-shm",
        "code-project.lock",
        "code-project.metadata.json",
    }
    artifacts = {
        path for path in paths.database.parent.glob("code-project*")
        if ".staging-" not in path.name
    }
    assert artifacts <= expected
    assert paths.database in artifacts
    assert paths.metadata in artifacts


def test_guard_rereads_authoritative_revision_after_matching_probe(
    ready_runtime, monkeypatch
):
    old_revision = ready_runtime.status()["revision"]
    publisher = ready_runtime.with_config()
    published = {}

    def publish_then_report_match(**_kwargs):
        ready_runtime.project_file("src/pkg/service.py").write_text(
            "def concurrently_published():\n    return None\n",
            encoding="utf-8",
        )
        published.update(publisher.index(force=True))
        return False

    monkeypatch.setattr(
        ready_runtime.runtime._indexer,
        "mark_dirty_if_stale",
        publish_then_report_match,
    )

    guarded = ready_runtime.query_guard()

    assert published["revision"] != old_revision
    assert guarded["revision"] == published["revision"]
    assert guarded["fresh"] is True


def test_guard_does_not_overlay_stale_on_concurrently_published_ready(
    ready_runtime, monkeypatch
):
    old_revision = ready_runtime.status()["revision"]
    publisher = ready_runtime.with_config()
    published = {}

    def publish_then_report_dirty(**_kwargs):
        ready_runtime.project_file("src/pkg/service.py").write_text(
            "def published_after_dirty_probe():\n    return None\n",
            encoding="utf-8",
        )
        published.update(publisher.index(force=True))
        return True

    monkeypatch.setattr(
        ready_runtime.runtime._indexer,
        "mark_dirty_if_stale",
        publish_then_report_dirty,
    )

    guarded = ready_runtime.query_guard()

    assert published["revision"] != old_revision
    assert guarded["revision"] == published["revision"]
    assert guarded["state"] == "ready"
    assert guarded["fresh"] is True
    assert "code" not in guarded


def test_index_lock_timeout_returns_stable_busy_contract(seed_runtime):
    runtime = seed_runtime.with_config(max_rebuild_seconds=1)

    with runtime.hold_publication_lock():
        out = runtime.index(force=True)

    assert out == {
        "error": "code graph is busy",
        "code": "busy",
        "hint": "retry wiki_code_index",
    }


def test_unsupported_index_language_returns_invalid_config(seed_runtime):
    assert seed_runtime.index(languages=["typescript"]) == {
        "error": "code graph configuration is invalid",
        "code": "invalid_config",
        "hint": "inspect code_graph project configuration",
    }


def test_bounded_rebuild_checks_one_shared_deadline_before_publication(
    seed_runtime, monkeypatch
):
    runtime = seed_runtime.with_state(
        "dirty", auto_rebuild="bounded", max_rebuild_seconds=1
    )
    real_parse = runtime.runtime._indexer._parse

    def slow_parse(discovered, config):
        time.sleep(1.05)
        return real_parse(discovered, config)

    monkeypatch.setattr(runtime.runtime._indexer, "_parse", slow_parse)

    out = runtime.query_guard()
    runtime.runtime.join_workers(timeout=3)
    settled = runtime.status()

    assert out["state"] == "rebuilding"
    assert out["fresh"] is False
    assert out["results"] == []
    assert settled["state"] == "dirty"
    assert runtime.build_attempts == 1
    assert not list(runtime.paths.database.parent.glob(
        f"{runtime.paths.database.name}.staging-*"
    ))


def test_bounded_rebuild_caps_larger_request_budget_at_configured_limit(
    seed_runtime, monkeypatch
):
    runtime = seed_runtime.with_state(
        "dirty", auto_rebuild="bounded", max_rebuild_seconds=1
    )
    real_parse = runtime.runtime._indexer._parse

    def slow_parse(discovered, config):
        time.sleep(1.1)
        return real_parse(discovered, config)

    monkeypatch.setattr(runtime.runtime._indexer, "_parse", slow_parse)

    out = runtime.runtime.query_guard(remaining_seconds=2)
    runtime.runtime.join_workers(timeout=3)
    settled = runtime.status()

    assert out["state"] == "rebuilding"
    assert out["fresh"] is False
    assert out["results"] == []
    assert settled["state"] == "dirty"
    assert runtime.build_attempts == 1


def test_explicit_index_uses_one_deadline_and_discards_staging(
    seed_runtime, monkeypatch
):
    runtime = seed_runtime.with_config(max_rebuild_seconds=1)
    real_parse = runtime.runtime._indexer._parse

    def slow_parse(discovered, config):
        time.sleep(1.05)
        return real_parse(discovered, config)

    monkeypatch.setattr(runtime.runtime._indexer, "_parse", slow_parse)

    assert runtime.index(force=True) == {
        "error": "code graph is busy",
        "code": "busy",
        "hint": "retry wiki_code_index",
    }
    runtime.runtime.join_workers(timeout=3)
    assert not list(runtime.paths.database.parent.glob(
        f"{runtime.paths.database.name}.staging-*"
    ))


def test_timeout_does_not_publish_metadata_after_deadline_and_restores_dirty(
    seed_runtime, monkeypatch
):
    runtime = seed_runtime.with_state(
        "dirty", auto_rebuild="off", max_rebuild_seconds=1
    )
    indexer = runtime.runtime._indexer
    real_parse = indexer._parse
    real_publish = indexer.store.publish_metadata
    published_states = []

    def slow_parse(discovered, config):
        time.sleep(1.05)
        return real_parse(discovered, config)

    def observed_publish(metadata_path, staging, **kwargs):
        payload = json.loads(Path(staging).read_text(encoding="utf-8"))
        published_states.append(payload["state"])
        return real_publish(metadata_path, staging, **kwargs)

    monkeypatch.setattr(indexer, "_parse", slow_parse)
    monkeypatch.setattr(indexer.store, "publish_metadata", observed_publish)

    with pytest.raises(Timeout):
        indexer.build(
            force=True,
            deadline=time.monotonic() + 1,
            restore_prior_on_abort=True,
        )
    stale = json.loads(runtime.paths.metadata.read_text(encoding="utf-8"))

    assert published_states == ["rebuilding"]
    assert stale["state"] == "rebuilding"
    assert stale["prior_state"] == "dirty"
    assert stale["previous_revision"] == stale["revision"]

    recovered = runtime.status()
    repaired = json.loads(runtime.paths.metadata.read_text(encoding="utf-8"))

    assert recovered["state"] == "dirty"
    assert recovered["fresh"] is False
    assert published_states == ["rebuilding"]
    assert repaired["state"] == "dirty"
    assert repaired["generation"] == stale["generation"]


def test_slow_metadata_publication_respects_absolute_deadline(
    seed_runtime, monkeypatch
):
    runtime = seed_runtime.with_config(max_rebuild_seconds=1)
    first = runtime.index(force=True)
    runtime.project_file("src/pkg/service.py").write_text(
        "def changed_during_metadata_publish():\n    return None\n",
        encoding="utf-8",
    )
    real_publish = runtime.runtime._indexer.store.publish_metadata
    store = runtime.runtime._indexer.store
    real_replace = store.replace_staging
    database_replaced = False

    def observed_replace(*args, **kwargs):
        nonlocal database_replaced
        result = real_replace(*args, **kwargs)
        database_replaced = True
        return result

    def slow_publish(*args, **kwargs):
        if database_replaced:
            time.sleep(1.05)
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(store, "replace_staging", observed_replace)
    monkeypatch.setattr(
        store, "publish_metadata", slow_publish
    )

    out = runtime.index(force=True)
    during = runtime.status()
    runtime.runtime.join_workers(timeout=3)
    status = runtime.status()

    assert out["code"] == "busy"
    assert during["state"] == "rebuilding"
    assert during["fresh"] is False
    assert status["state"] == "ready"
    assert status["fresh"] is True
    assert status["revision"] != first["revision"]
    assert "duration_ms" in status
    assert "phase_timings_ms" in status


def test_expired_final_ready_metadata_is_not_published(
    seed_runtime, monkeypatch
):
    runtime = seed_runtime.with_config(max_rebuild_seconds=1)
    real_publish = runtime.runtime._indexer.store.publish_metadata

    def slow_final_publish(metadata_path, staging, **kwargs):
        payload = json.loads(Path(staging).read_text(encoding="utf-8"))
        if payload["state"] == "ready":
            time.sleep(1.05)
        return real_publish(metadata_path, staging, **kwargs)

    monkeypatch.setattr(
        runtime.runtime._indexer.store,
        "publish_metadata",
        slow_final_publish,
    )

    out = runtime.index(force=True)
    persisted = json.loads(runtime.paths.metadata.read_text(encoding="utf-8"))
    during = runtime.status()
    runtime.runtime.join_workers(timeout=3)
    status = runtime.status()

    assert out["code"] == "busy"
    assert persisted["state"] == "rebuilding"
    assert "duration_ms" not in persisted
    assert "phase_timings_ms" not in persisted
    assert during["state"] == "rebuilding"
    assert not list(runtime.paths.metadata.parent.glob(
        f"{runtime.paths.metadata.name}.staging-*"
    ))
    assert status["state"] == "ready"
    assert status["fresh"] is True


def test_publication_timing_includes_metadata_and_final_verification(
    seed_runtime, monkeypatch
):
    runtime = seed_runtime.with_config(max_rebuild_seconds=2)
    indexer = runtime.runtime._indexer
    real_publish = indexer.store.publish_metadata
    real_verify = indexer._verify_published

    def slow_publish(*args, **kwargs):
        time.sleep(0.06)
        return real_publish(*args, **kwargs)

    def slow_verify(revision):
        time.sleep(0.06)
        return real_verify(revision)

    monkeypatch.setattr(indexer.store, "publish_metadata", slow_publish)
    monkeypatch.setattr(indexer, "_verify_published", slow_verify)

    out = runtime.index(force=True)
    persisted = json.loads(runtime.paths.metadata.read_text(encoding="utf-8"))
    status = runtime.status()

    assert out["state"] == "ready"
    assert out["duration_ms"] >= 200
    assert out["phase_timings_ms"]["publication"] >= 200
    assert persisted["duration_ms"] == out["duration_ms"]
    assert persisted["phase_timings_ms"] == out["phase_timings_ms"]
    assert status["duration_ms"] == out["duration_ms"]


def test_lifecycle_order_ends_with_final_verify_then_diagnostics_refresh(
    seed_runtime, monkeypatch
):
    assert hasattr(CodeGraphStore, "prepare_metadata")
    assert hasattr(CodeGraphStore, "refresh_metadata_diagnostics")
    events = []
    stage_metadata = []
    store = seed_runtime.runtime._indexer.store
    indexer = seed_runtime.runtime._indexer
    real_replace_database = store.replace_staging
    real_publish = store.publish_metadata
    real_refresh = store.refresh_metadata_diagnostics
    real_verify = indexer._verify_published
    after_replace = False

    def observed_replace_database(*args, **kwargs):
        nonlocal after_replace
        events.append("replace_database")
        result = real_replace_database(*args, **kwargs)
        after_replace = True
        return result

    def observed_publish(metadata_path, staging, **kwargs):
        payload = json.loads(Path(staging).read_text(encoding="utf-8"))
        if after_replace:
            events.append(f"publish_metadata:{payload['state']}")
            stage_metadata.append(payload)
        return real_publish(metadata_path, staging, **kwargs)

    def observed_verify(revision):
        events.append("verify_database")
        return real_verify(revision)

    def observed_refresh(*args, **kwargs):
        events.append("refresh_metadata_diagnostics")
        return real_refresh(*args, **kwargs)

    monkeypatch.setattr(store, "replace_staging", observed_replace_database)
    monkeypatch.setattr(store, "publish_metadata", observed_publish)
    monkeypatch.setattr(store, "refresh_metadata_diagnostics", observed_refresh)
    monkeypatch.setattr(indexer, "_verify_published", observed_verify)

    assert seed_runtime.index(force=True)["state"] == "ready"
    assert events == [
        "replace_database",
        "publish_metadata:rebuilding",
        "verify_database",
        "publish_metadata:ready",
        "verify_database",
        "refresh_metadata_diagnostics",
    ]
    assert stage_metadata[0]["revision"] == stage_metadata[1]["revision"]
    assert stage_metadata[0]["generation"] == stage_metadata[1]["generation"]
    assert "metrics_incomplete" in stage_metadata[0]["warnings"]
    assert "duration_ms" not in stage_metadata[0]
    assert "phase_timings_ms" not in stage_metadata[0]


def test_failed_diagnostics_refresh_never_returns_ready_before_recovery(
    seed_runtime, monkeypatch
):
    secret = "diagnostics refresh fixture secret"
    store = seed_runtime.runtime._indexer.store

    def fail_refresh(*_args, **_kwargs):
        raise CodeGraphStoreError(secret)

    monkeypatch.setattr(store, "refresh_metadata_diagnostics", fail_refresh)

    rebuilt = seed_runtime.index(force=True)
    pending = json.loads(
        seed_runtime.paths.metadata.read_text(encoding="utf-8")
    )
    status = seed_runtime.status()

    assert rebuilt["code"] == "store_failed"
    assert rebuilt["fresh"] is False
    assert pending["state"] == "ready"
    assert pending["publication_phase"] == "pending_final_verify"
    assert "duration_ms" not in pending
    assert "phase_timings_ms" not in pending
    assert status["state"] == "failed"
    assert status["fresh"] is False
    assert status["revision"] == pending["revision"]
    assert secret not in str(rebuilt)
    assert secret not in str(status)


def test_build_persists_unresolved_inputs_before_resolution_and_selector_seam(
    seed_runtime, monkeypatch
):
    events = []
    unresolved_states = []
    indexer = seed_runtime.runtime._indexer
    real_insert = CodeGraphStore.insert_snapshot
    real_prepare = CodeGraphStore.prepare_staging

    def observed_insert(store, snapshot):
        events.append("persist")
        unresolved_states.extend(
            row["resolution_state"] for row in snapshot["relations"]
        )
        return real_insert(store, snapshot)

    def observed_replace_relations(store, relations):
        events.append("cross_file_resolution")
        return real_replace_relations(store, relations)

    def observed_finalize(store, **kwargs):
        events.append("finalize")
        return real_finalize(store, **kwargs)

    def observed_prepare(store, staging, **kwargs):
        events.append("validate")
        return real_prepare(store, staging, **kwargs)

    class SelectorSeam:
        def resolve(self, **_kwargs):
            events.append("wiki_selector_resolution")
            return ()

    real_replace_relations = CodeGraphStore.replace_relations
    real_finalize = CodeGraphStore.finalize_snapshot
    monkeypatch.setattr(CodeGraphStore, "insert_snapshot", observed_insert)
    monkeypatch.setattr(
        CodeGraphStore, "replace_relations", observed_replace_relations
    )
    monkeypatch.setattr(CodeGraphStore, "finalize_snapshot", observed_finalize)
    monkeypatch.setattr(CodeGraphStore, "prepare_staging", observed_prepare)
    indexer.wiki_selector_resolver = SelectorSeam()

    result = seed_runtime.index(force=True)

    assert result["state"] == "ready"
    assert unresolved_states
    assert set(unresolved_states) == {"unresolved"}
    assert events == [
        "persist",
        "cross_file_resolution",
        "wiki_selector_resolution",
        "finalize",
        "validate",
    ]


def test_codegraph_core_has_no_python_adapter_dependency():
    codegraph = Path(__file__).parents[2] / "src/iwiki_mcp/codegraph"
    core_sources = [
        path.read_text(encoding="utf-8")
        for path in codegraph.glob("*.py")
    ]

    assert all(".languages.python" not in source for source in core_sources)
    assert all("PythonAdapter" not in source for source in core_sources)


def test_status_reports_enabled_and_persisted_duration_without_revision_noise(
    seed_runtime,
):
    first = seed_runtime.index(force=True)
    second = seed_runtime.index(force=True)
    metadata = json.loads(
        seed_runtime.paths.metadata.read_text(encoding="utf-8")
    )
    status = seed_runtime.status()

    assert status["enabled"] is True
    assert status["duration_ms"] == metadata["duration_ms"]
    assert type(status["duration_ms"]) is int
    assert status["duration_ms"] >= 0
    assert first["revision"] == second["revision"]


def test_toolchain_drift_reports_persisted_snapshot_versions_as_dirty(
    ready_runtime,
):
    persisted = ready_runtime.status()
    changed = ready_runtime.with_toolchain_versions(
        parser="parser-next",
        adapter="adapter-next",
        resolver="resolver-next",
    )

    status = changed.status()
    guarded = changed.query_guard()

    assert status["state"] == "dirty"
    assert status["fresh"] is False
    assert status["parser_version"] == persisted["parser_version"]
    assert status["adapter_version"] == persisted["adapter_version"]
    assert status["resolver_version"] == persisted["resolver_version"]
    assert guarded["state"] == "dirty"
    assert guarded["fresh"] is False
    assert guarded["results"] == []


def test_python_adapter_v2_rebuilds_unchanged_v1_cache(seed_runtime):
    current = seed_runtime
    factory = current.runtime._indexer.adapter_factories["python"]
    old = seed_runtime.with_toolchain_versions(
        parser=factory.parser_version,
        grammar=factory.grammar_version,
        adapter="python-adapter-v1",
        resolver=current.runtime._indexer.resolver_version,
    )
    first = old.index(force=True)

    dirty = current.status()
    rebuilt = current.index(force=False)
    no_op = current.index(force=False)
    expected_fingerprint = parser_fingerprint(
        languages=current.runtime.config.languages,
        schema_version=SCHEMA_VERSION,
        parser_version=current.runtime._indexer._parser_version(
            current.runtime.config
        ),
        grammar_version=current.runtime._indexer._grammar_version(
            current.runtime.config
        ),
        adapter_version="python:python-adapter-v2",
        resolver_version=current.runtime._indexer.resolver_version,
        normalizer_version=codegraph_models.NORMALIZER_VERSION,
        unicode_data_version=codegraph_models.UNICODE_DATA_VERSION,
    )
    metadata = json.loads(
        current.paths.metadata.read_text(encoding="utf-8")
    )
    with closing(current.runtime._store.open_existing()) as connection:
        repository_fingerprint = connection.execute(
            "SELECT parser_fingerprint FROM repositories"
        ).fetchone()[0]

    assert dirty["state"] == "dirty"
    assert dirty["fresh"] is False
    assert rebuilt["state"] == "ready"
    assert rebuilt["revision"] != first["revision"]
    assert rebuilt["adapter_version"] == "python:python-adapter-v2"
    assert rebuilt["fingerprints"]["parser"] == expected_fingerprint
    assert repository_fingerprint == expected_fingerprint
    assert metadata["adapter_version"] == "python:python-adapter-v2"
    assert metadata["fingerprints"]["parser"] == expected_fingerprint
    assert no_op["no_op"] is True
    assert no_op["revision"] == rebuilt["revision"]
    assert no_op["adapter_version"] == "python:python-adapter-v2"


def test_grammar_only_drift_reports_persisted_snapshot_as_dirty(
    seed_runtime,
):
    original = seed_runtime.with_toolchain_versions(
        parser="parser-v1",
        grammar="grammar-v1",
        adapter="adapter-v1",
        resolver="resolver-v1",
    )
    built = original.index(force=True)
    changed = seed_runtime.with_toolchain_versions(
        parser="parser-v1",
        grammar="grammar-v2",
        adapter="adapter-v1",
        resolver="resolver-v1",
    )

    status = changed.status()
    guarded = changed.query_guard()

    assert status["state"] == "dirty"
    assert status["fresh"] is False
    assert status["grammar_version"] == built["grammar_version"]
    assert status["grammar_version"] == "python:grammar-v1"
    assert guarded["state"] == "dirty"
    assert guarded["fresh"] is False
    assert guarded["grammar_version"] == "python:grammar-v1"
    assert guarded["results"] == []


@pytest.mark.parametrize("damage", ("missing", "corrupt"))
def test_reconstructed_metadata_uses_sql_toolchain_fingerprint(
    seed_runtime, damage
):
    old = seed_runtime.with_toolchain_versions(
        parser="parser-v1",
        grammar="grammar-v1",
        adapter="adapter-v1",
        resolver="resolver-v1",
    )
    old.index(force=True)
    if damage == "missing":
        old.paths.metadata.unlink()
    else:
        old.paths.metadata.write_text("{corrupt", encoding="utf-8")

    matching = seed_runtime.with_toolchain_versions(
        parser="parser-v1",
        grammar="grammar-v1",
        adapter="adapter-v1",
        resolver="resolver-v1",
    ).status()
    changed = seed_runtime.with_toolchain_versions(
        parser="parser-v2",
        grammar="grammar-v2",
        adapter="adapter-v2",
        resolver="resolver-v2",
    )
    status = changed.status()
    guarded = changed.query_guard()

    assert matching["state"] == "ready"
    assert "metadata_reconstructed" in matching["warnings"]
    for key in (
        "parser_version",
        "grammar_version",
        "adapter_version",
        "resolver_version",
        "normalizer_version",
        "unicode_data_version",
    ):
        assert matching[key] is None
        assert status[key] is None
    assert status["state"] == "dirty"
    assert status["fresh"] is False
    assert "metadata_reconstructed" in status["warnings"]
    assert guarded["state"] == "dirty"
    assert guarded["fresh"] is False
    assert guarded["results"] == []


@pytest.mark.parametrize(
    "version_name",
    ("NORMALIZER_VERSION", "UNICODE_DATA_VERSION"),
)
def test_status_marks_current_model_version_drift_dirty_before_query(
    seed_runtime,
    monkeypatch: pytest.MonkeyPatch,
    version_name: str,
):
    built = seed_runtime.index(force=True)
    current_version = getattr(codegraph_models, version_name)
    assert built[version_name.casefold()] == current_version

    monkeypatch.setattr(
        codegraph_models,
        version_name,
        current_version + "-drift",
    )

    status = seed_runtime.status()

    assert status["state"] == "dirty"
    assert status["fresh"] is False
    assert status["hint"] == "run wiki_code_index"
    assert status[version_name.casefold()] == current_version


def test_build_captures_one_model_version_pair_for_all_persistence(
    seed_runtime,
    monkeypatch: pytest.MonkeyPatch,
):
    runtime = seed_runtime.runtime
    assert runtime.config is not None
    assert runtime._indexer is not None
    indexer = runtime._indexer
    normalizer_version = codegraph_models.NORMALIZER_VERSION
    unicode_data_version = codegraph_models.UNICODE_DATA_VERSION
    expected_parser_fingerprint = parser_fingerprint(
        languages=runtime.config.languages,
        schema_version=SCHEMA_VERSION,
        parser_version=indexer._parser_version(runtime.config),
        grammar_version=indexer._grammar_version(runtime.config),
        adapter_version=indexer._adapter_version(runtime.config),
        resolver_version=indexer.resolver_version,
        normalizer_version=normalizer_version,
        unicode_data_version=unicode_data_version,
    )
    real_fingerprints = indexer._fingerprints

    def drift_after_capture(discovered, config, **kwargs):
        monkeypatch.setattr(
            codegraph_models,
            "NORMALIZER_VERSION",
            normalizer_version + "-drift",
        )
        monkeypatch.setattr(
            codegraph_models,
            "UNICODE_DATA_VERSION",
            unicode_data_version + "-drift",
        )
        return real_fingerprints(discovered, config, **kwargs)

    monkeypatch.setattr(indexer, "_fingerprints", drift_after_capture)

    built = seed_runtime.index(force=True)
    connection = runtime._store.open_existing()
    try:
        row = connection.execute(
            "SELECT parser_fingerprint, normalizer_version, "
            "unicode_data_version FROM repositories"
        ).fetchone()
    finally:
        connection.close()
    metadata = json.loads(
        seed_runtime.paths.metadata.read_text(encoding="utf-8")
    )

    assert row == (
        expected_parser_fingerprint,
        normalizer_version,
        unicode_data_version,
    )
    assert built["fingerprints"]["parser"] == expected_parser_fingerprint
    assert built["normalizer_version"] == normalizer_version
    assert built["unicode_data_version"] == unicode_data_version
    assert metadata["fingerprints"]["parser"] == expected_parser_fingerprint
    assert metadata["normalizer_version"] == normalizer_version
    assert metadata["unicode_data_version"] == unicode_data_version


def test_process_worker_registry_caps_four_domains_at_one(
    seed_binding, tmp_path, monkeypatch, production_runtime_factory
):
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def blocked_exclusion(_base):
        nonlocal calls
        with calls_lock:
            calls += 1
            entered.set()
        assert release.wait(timeout=5)
        return True

    monkeypatch.setattr(
        wiki_base, "ensure_graph_store_excluded", blocked_exclusion
    )
    runtimes = []
    for index in range(4):
        base = tmp_path / f"worker-base-{index}"
        base.mkdir()
        binding = replace(
            seed_binding,
            base=str(base),
            primary=f"project-{index}",
        )
        runtimes.append(production_runtime_factory(binding))

    executor = ThreadPoolExecutor(max_workers=4)
    futures = [executor.submit(runtime.index, force=True) for runtime in runtimes]
    try:
        assert entered.wait(timeout=1)
        time.sleep(0.1)
        finished = [future for future in futures if future.done()]
        assert len(finished) == 3
        assert all(future.result()["code"] == "busy" for future in finished)
        assert calls == 1
        assert sum(
            thread.name == "iwiki-code-graph-build"
            for thread in threading.enumerate()
        ) == 1
    finally:
        release.set()
        results = [future.result(timeout=5) for future in futures]
        executor.shutdown(wait=True)
        for runtime in runtimes:
            runtime.join_workers(timeout=5)

    assert sum(result.get("code") == "busy" for result in results) == 3
    assert runtimes[0].active_workers == 0


def test_secure_descriptor_path_falls_back_to_dev_fd(monkeypatch):
    monkeypatch.setattr(
        codegraph_location.Path,
        "is_dir",
        lambda path: str(path).startswith("/dev/fd/"),
    )

    assert codegraph_location._descriptor_path(17) == Path("/dev/fd/17")


def test_missing_replace_dir_fd_support_fails_closed(monkeypatch):
    monkeypatch.setattr(
        codegraph_location,
        "_replace_supports_dir_fd",
        lambda: False,
        raising=False,
    )

    with pytest.raises(codegraph_location.CodeGraphLocationError):
        codegraph_location._directory_flags()


def test_noop_response_does_not_reuse_full_build_phase_timings(seed_runtime):
    built = seed_runtime.index(force=True)
    no_op = seed_runtime.index(force=False)

    assert built["phase_timings_ms"]
    assert no_op["no_op"] is True
    assert set(no_op["phase_timings_ms"]) <= {"no_op"}


def test_slow_adapter_returns_by_deadline_and_cancels_before_publication(
    ready_runtime, monkeypatch
):
    runtime = ready_runtime.with_config(max_rebuild_seconds=1)
    previous = runtime.status()["revision"]
    runtime.project_file("src/pkg/service.py").write_text(
        "def changed_while_parser_blocks():\n    return None\n",
        encoding="utf-8",
    )
    factory = runtime.runtime._indexer.adapter_factories["python"]
    real_create = factory.create
    store = runtime.runtime._indexer.store
    real_replace = store.replace_staging
    entered = threading.Event()
    release = threading.Event()
    caller_returned = threading.Event()
    publications = []

    def observed_replace(*args, **kwargs):
        publications.append("database")
        return real_replace(*args, **kwargs)

    def safety_release():
        caller_returned.wait(timeout=2)
        release.set()

    def create_blocked_adapter(source_paths):
        adapter = real_create(source_paths)
        real_parse = adapter.parse_file

        def parse(*args, **kwargs):
            entered.set()
            assert release.wait(timeout=3)
            return real_parse(*args, **kwargs)

        adapter.parse_file = parse
        return adapter

    monkeypatch.setitem(
        runtime.runtime._indexer.adapter_factories,
        "python",
        replace(factory, create=create_blocked_adapter),
    )
    monkeypatch.setattr(store, "replace_staging", observed_replace)
    releaser = threading.Thread(target=safety_release, daemon=True)
    releaser.start()

    started = time.monotonic()
    out = runtime.index(force=True)
    elapsed = time.monotonic() - started
    caller_returned.set()
    release.set()
    releaser.join(timeout=3)
    runtime.runtime.join_workers(timeout=3)

    assert entered.is_set()
    assert out["code"] == "busy"
    assert elapsed < 1.5
    assert publications == []
    assert runtime.status()["revision"] == previous
    assert runtime.runtime.active_workers == 0
    assert not list(runtime.paths.database.parent.glob(
        f"{runtime.paths.database.name}.staging-*"
    ))


def test_slow_git_setup_is_bounded_and_cannot_publish_after_cancel(
    ready_runtime, monkeypatch
):
    runtime = ready_runtime.with_config(max_rebuild_seconds=1)
    previous = runtime.status()["revision"]
    entered = threading.Event()
    release = threading.Event()
    caller_returned = threading.Event()
    publications = []
    store = runtime.runtime._indexer.store
    real_replace = store.replace_staging

    def blocked_exclusion(_base):
        entered.set()
        assert release.wait(timeout=3)
        return True

    def observed_replace(*args, **kwargs):
        publications.append("database")
        return real_replace(*args, **kwargs)

    def safety_release():
        caller_returned.wait(timeout=2)
        release.set()

    monkeypatch.setattr(
        wiki_base, "ensure_graph_store_excluded", blocked_exclusion
    )
    monkeypatch.setattr(store, "replace_staging", observed_replace)
    releaser = threading.Thread(target=safety_release, daemon=True)
    releaser.start()

    started = time.monotonic()
    out = runtime.index(force=True)
    elapsed = time.monotonic() - started
    caller_returned.set()
    release.set()
    releaser.join(timeout=3)
    runtime.runtime.join_workers(timeout=3)

    assert entered.is_set()
    assert out["code"] == "busy"
    assert elapsed < 1.5
    assert publications == []
    assert runtime.status()["revision"] == previous
    assert runtime.runtime.active_workers == 0


def test_timeout_after_atomic_entry_finishes_non_ready_then_ready(
    ready_runtime, monkeypatch
):
    runtime = ready_runtime.with_config(max_rebuild_seconds=1)
    previous = runtime.status()["revision"]
    runtime.project_file("src/pkg/service.py").write_text(
        "def changed_during_publication():\n    return None\n",
        encoding="utf-8",
    )
    store = runtime.runtime._indexer.store
    real_replace = store.replace_staging
    entered = threading.Event()
    release = threading.Event()

    def blocked_replace(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=3)
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(store, "replace_staging", blocked_replace)

    started = time.monotonic()
    out = runtime.index(force=True)
    elapsed = time.monotonic() - started
    during = runtime.status()
    release.set()
    runtime.runtime.join_workers(timeout=3)
    after = runtime.status()

    assert entered.is_set()
    assert out["code"] == "busy"
    assert elapsed < 1.5
    assert during["state"] == "rebuilding"
    assert during["fresh"] is False
    assert after["state"] == "ready"
    assert after["fresh"] is True
    assert after["revision"] != previous
    assert runtime.runtime.active_workers == 0


@pytest.mark.parametrize(
    ("failure_type", "expected_code"),
    (
        (CodeGraphParseError, "parse_failed"),
        (CodeGraphStoreFailure, "store_failed"),
        (CodeGraphStaleError, "stale"),
        (CodeGraphUnsafePathError, "unsafe_path"),
    ),
)
def test_runtime_maps_typed_build_failures_to_stable_sanitized_codes(
    seed_runtime, monkeypatch, failure_type, expected_code
):
    secret = "typed_failure_fixture_secret"

    def fail_build(**_kwargs):
        raise failure_type(secret)

    monkeypatch.setattr(seed_runtime.runtime._indexer, "build", fail_build)

    result = seed_runtime.index(force=True)

    assert result["code"] == expected_code
    assert secret not in str(result)


def test_runtime_maps_real_parse_store_and_unsafe_path_failures(
    seed_runtime, monkeypatch
):
    factory = seed_runtime.runtime._indexer.adapter_factories["python"]
    real_create = factory.create

    def fail_parse(*_args, **_kwargs):
        raise RuntimeError("parse fixture secret")

    def create_failing_adapter(source_paths):
        adapter = real_create(source_paths)
        adapter.parse_file = fail_parse
        return adapter

    monkeypatch.setitem(
        seed_runtime.runtime._indexer.adapter_factories,
        "python",
        replace(factory, create=create_failing_adapter),
    )
    assert seed_runtime.index(force=True)["code"] == "parse_failed"
    monkeypatch.undo()

    def fail_store(*_args, **_kwargs):
        raise CodeGraphStoreError("store fixture secret")

    monkeypatch.setattr(CodeGraphStore, "insert_snapshot", fail_store)
    assert seed_runtime.index(force=True)["code"] == "store_failed"
    monkeypatch.undo()

    def fail_discovery(*_args, **_kwargs):
        raise DiscoveryError("unsafe fixture secret")

    monkeypatch.setattr(
        "iwiki_mcp.codegraph.indexer.discover_sources", fail_discovery
    )
    assert seed_runtime.index(force=True)["code"] == "unsafe_path"


def test_runtime_initialization_and_status_fail_soft(
    seed_binding, monkeypatch, production_runtime_factory
):
    secret = "runtime_fixture_secret"

    def fail_location(_self):
        raise RuntimeError(secret)

    monkeypatch.setattr(CodeGraphLocationResolver, "resolve", fail_location)
    unavailable = production_runtime_factory(seed_binding).status()

    assert unavailable["code"] == "rebuild_failed"
    assert secret not in str(unavailable)

    monkeypatch.undo()
    runtime = production_runtime_factory(seed_binding)
    assert runtime.index(force=True)["state"] == "ready"

    def fail_status():
        raise RuntimeError(secret)

    monkeypatch.setattr(runtime._store, "open_existing", fail_status)
    failed = runtime.status()
    assert failed["state"] == "failed"
    assert failed["code"] == "store_failed"
    assert secret not in str(failed)


def test_corrupt_lock_backend_returns_sanitized_store_failure(seed_runtime):
    assert seed_runtime.index(force=True)["state"] == "ready"
    wiki_before = seed_runtime.wiki_hashes()
    secret = "corrupt lock fixture secret"
    seed_runtime.paths.lock.write_bytes(secret.encode("utf-8"))

    status = seed_runtime.status()

    assert status["code"] == "store_failed"
    assert status["state"] == "failed"
    assert status["fresh"] is False
    assert secret not in str(status)
    assert str(seed_runtime.binding.project_dir) not in str(status)
    assert seed_runtime.wiki_hashes() == wiki_before


@pytest.mark.parametrize("mode", ["read", "write"])
@pytest.mark.parametrize("failure_stage", ["construct", "acquire", "close"])
def test_lock_backend_errors_are_sanitized(
    tmp_path, monkeypatch, mode, failure_stage
):
    secret = "lock backend fixture secret"

    class BackendContext:
        def __enter__(self):
            if failure_stage == "acquire":
                raise OSError(secret)

        def __exit__(self, *_args):
            return None

    class FailingLock:
        def __init__(self, *_args, **_kwargs):
            if failure_stage == "construct":
                raise OSError(secret)

        def read_lock(self, **_kwargs):
            return BackendContext()

        def write_lock(self, **_kwargs):
            return BackendContext()

        def acquire_read(self, *_args, **_kwargs):
            if failure_stage == "acquire":
                raise OSError(secret)

        def acquire_write(self, *_args, **_kwargs):
            if failure_stage == "acquire":
                raise OSError(secret)

        def close(self):
            if failure_stage == "close":
                raise OSError(secret)

    monkeypatch.setattr(
        "iwiki_mcp.codegraph.store.ReadWriteLock", FailingLock
    )
    context = (
        code_graph_read_lock(tmp_path / "graph.lock")
        if mode == "read"
        else code_graph_write_lock(tmp_path / "graph.lock", timeout=1)
    )

    with pytest.raises(CodeGraphStoreError) as caught:
        with context:
            pass

    assert secret not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


def test_unsafe_location_maps_to_stable_runtime_failure(seed_binding):
    unsafe = Binding(
        base=seed_binding.base,
        read=seed_binding.read,
        write=seed_binding.write,
        primary="../unsafe",
        project_dir=seed_binding.project_dir,
    )

    status = CodeGraphRuntime(unsafe).status()

    assert status["code"] == "unsafe_path"
    assert status["enabled"] is True


def test_corrupt_cache_is_quarantined_before_rebuild_without_touching_wiki(
    seed_runtime,
):
    assert seed_runtime.index(force=True)["state"] == "ready"
    wiki_before = seed_runtime.wiki_hashes()
    seed_runtime.paths.database.write_bytes(b"not sqlite")

    rebuilt = seed_runtime.index(force=True)

    assert rebuilt["state"] == "ready"
    assert seed_runtime.wiki_hashes() == wiki_before
    assert list(seed_runtime.paths.database.parent.glob(
        f"{seed_runtime.paths.database.name}.corrupt-*"
    ))


def test_build_logs_are_sanitized_and_cache_is_git_ignored(
    seed_runtime, caplog, monkeypatch
):
    secret = "fixture-secret-token"
    environment_secret = "environment-secret-value"
    monkeypatch.setenv("FIXTURE_PRIVATE_VALUE", environment_secret)
    seed_runtime.fail_with_message(
        f"{secret} {seed_runtime.project_dir} {environment_secret}"
    )

    with caplog.at_level(logging.INFO):
        out = seed_runtime.index(force=True)

    assert out["code"] == "rebuild_failed"
    assert secret not in caplog.text
    assert str(seed_runtime.project_dir) not in caplog.text
    assert environment_secret not in caplog.text
    assert "fixture" not in caplog.text
    assert seed_runtime.git_status() == ""


def test_status_and_noop_sanitize_tampered_metadata(seed_runtime):
    assert seed_runtime.index(force=True)["state"] == "ready"
    secret = "fixture_secret_token"
    metadata = json.loads(
        seed_runtime.paths.metadata.read_text(encoding="utf-8")
    )
    metadata["warnings"] = [secret]
    metadata["phase_timings_ms"] = {"discovery": secret}
    metadata["excluded_files"] = secret
    metadata["parser_errors"] = secret
    metadata["resolution_ratios"] = {secret: 1.0}
    seed_runtime.paths.metadata.write_text(
        json.dumps(metadata),
        encoding="utf-8",
    )

    status = seed_runtime.status()
    no_op = seed_runtime.index(force=False)

    assert no_op["no_op"] is True
    assert secret not in str(status)
    assert secret not in str(no_op)


def test_metadata_failure_after_database_replace_keeps_sql_revision_authoritative(
    seed_runtime, monkeypatch
):
    first = seed_runtime.index(force=True)
    seed_runtime.project_file("src/pkg/service.py").write_text(
        "def changed():\n    return None\n",
        encoding="utf-8",
    )
    store = seed_runtime.runtime._indexer.store
    real_publish = store.publish_metadata
    real_replace = store.replace_staging
    database_replaced = False
    failure_injected = False

    def observed_replace(*args, **kwargs):
        nonlocal database_replaced
        result = real_replace(*args, **kwargs)
        database_replaced = True
        return result

    def fail_metadata(*args, **kwargs):
        nonlocal failure_injected
        if database_replaced and not failure_injected:
            failure_injected = True
            raise CodeGraphStoreError("metadata fixture secret")
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(store, "replace_staging", observed_replace)
    monkeypatch.setattr(
        store,
        "publish_metadata",
        fail_metadata,
    )
    rebuilt = seed_runtime.index(force=True)
    status = seed_runtime.status()
    guarded = seed_runtime.query_guard()

    assert rebuilt["code"] == "store_failed"
    assert status["revision"] != first["revision"]
    assert status["state"] == "failed"
    assert status["fresh"] is False
    assert guarded["revision"] == status["revision"]
    assert guarded["state"] == "failed"
    assert guarded["fresh"] is False
    assert guarded["results"] == []
    assert "metrics_incomplete" in status["warnings"]
    assert "metadata fixture secret" not in str(rebuilt)
    assert "metadata fixture secret" not in str(status)


def test_final_canonical_verify_failure_transitions_new_revision_to_failed(
    seed_runtime, monkeypatch
):
    first = seed_runtime.index(force=True)
    seed_runtime.project_file("src/pkg/service.py").write_text(
        "def changed_before_final_verify():\n    return None\n",
        encoding="utf-8",
    )
    indexer = seed_runtime.runtime._indexer
    real_verify = indexer._verify_published
    verify_calls = 0

    def fail_second_verify(revision):
        nonlocal verify_calls
        verify_calls += 1
        if verify_calls == 2:
            raise CodeGraphStoreError("final verify fixture secret")
        return real_verify(revision)

    monkeypatch.setattr(indexer, "_verify_published", fail_second_verify)

    rebuilt = seed_runtime.index(force=True)
    pending = json.loads(
        seed_runtime.paths.metadata.read_text(encoding="utf-8")
    )
    status = seed_runtime.status()
    persisted = json.loads(
        seed_runtime.paths.metadata.read_text(encoding="utf-8")
    )

    assert rebuilt["code"] == "store_failed"
    assert verify_calls == 2
    assert pending["state"] == "ready"
    assert pending["publication_phase"] == "pending_final_verify"
    assert "duration_ms" not in pending
    assert "phase_timings_ms" not in pending
    assert status["state"] == "failed"
    assert status["fresh"] is False
    assert status["revision"] != first["revision"]
    assert persisted["state"] == "failed"
    assert persisted["revision"] == status["revision"]
    assert "final verify fixture secret" not in str(rebuilt)
    assert "final verify fixture secret" not in str(status)
