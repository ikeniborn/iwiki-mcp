"""Typed, bounded code graph context contracts."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import json
import logging
import os

import pytest

from iwiki_mcp.codegraph import context as context_module
from iwiki_mcp.codegraph.context import (
    CodeGraphContextError,
    ContextRequest,
    validate_context_request,
)


def test_context_request_defaults_are_frozen():
    request = validate_context_request(["py:file:" + "a" * 64])

    assert request == ContextRequest(seeds=("py:file:" + "a" * 64,))
    with pytest.raises(FrozenInstanceError):
        request.depth = 2


@pytest.mark.parametrize(
    ("seeds", "changes"),
    [
        ([], {}),
        (["pkg.Service.run"], {}),
        (["py:alias:" + "a" * 64], {}),
        (["py:file:not-a-digest"], {}),
        (["py:file:" + "a" * 64] * 51, {}),
        (["py:file:" + "a" * 64], {"direction": "sideways"}),
        (["py:file:" + "a" * 64], {"depth": -1}),
        (["py:file:" + "a" * 64], {"depth": 4}),
        (["py:file:" + "a" * 64], {"relations": []}),
        (["py:file:" + "a" * 64], {"relations": ["LINKS"]}),
        (["py:file:" + "a" * 64], {"include_source": 1}),
        (["py:file:" + "a" * 64], {"include_wiki": "yes"}),
        (["py:file:" + "a" * 64], {"max_nodes": 0}),
        (["py:file:" + "a" * 64], {"max_nodes": 51}),
        (["py:file:" + "a" * 64], {"max_files": 21}),
        (["py:file:" + "a" * 64], {"max_source_bytes": 200_001}),
    ],
)
def test_context_validation_rejects_unbounded_or_untyped_input(seeds, changes):
    with pytest.raises(CodeGraphContextError):
        validate_context_request(seeds, **changes)


def test_context_validation_precedes_runtime_binding_and_io(seed_runtime, monkeypatch):
    def forbidden_guard():
        raise AssertionError("query guard must not run")

    monkeypatch.setattr(seed_runtime.runtime, "query_guard", forbidden_guard)

    response = seed_runtime.runtime.context(["pkg.Service.run"])

    assert response["code"] == "invalid_config"
    assert seed_runtime.database_accesses == []


@pytest.mark.parametrize(
    "seed",
    [
        "ts:file:" + "a" * 64,
        "python:file:" + "a" * 64,
        "PY:file:" + "a" * 64,
        "py:alias:" + "a" * 64,
        "py:file:" + "A" * 64,
        "py:file:" + "a" * 63 + " ",
        "py:file:" + "a" * 63 + "\t",
        "py:file:" + "a" * 63 + "\0",
        "py:file:" + "a" * 63 + "\x1f",
        "py:file:" + "a" * 63 + "\ud800",
    ],
)
def test_noncanonical_seed_is_rejected_before_query_guard(
    seed_runtime, monkeypatch, seed
):
    def forbidden_guard():
        raise AssertionError("query guard must not run")

    monkeypatch.setattr(seed_runtime.runtime, "query_guard", forbidden_guard)

    response = seed_runtime.runtime.context([seed])

    assert response["code"] == "invalid_config"
    assert seed_runtime.database_accesses == []


def test_context_accepts_file_module_and_symbol_seeds(ready_context):
    response = ready_context.context([
        ready_context.service_file_id,
        ready_context.service_module_id,
        ready_context.run_symbol_id,
    ])

    assert {node["entity_type"] for node in response["nodes"]} == {
        "file", "module", "symbol",
    }, response
    assert response["limits"] == {
        "depth": 1,
        "max_nodes": 50,
        "max_files": 20,
        "max_source_bytes": 200_000,
    }
    assert response["truncated"] is False
    assert response["wiki_pages"] == []


def test_file_seed_activates_module_at_depth_zero(ready_context):
    response = ready_context.context([ready_context.service_file_id], depth=0)

    assert [node["entity_type"] for node in response["nodes"]] == [
        "file", "module",
    ]
    assert response["relations"] == []


def test_module_direction_and_relation_filters_are_applied(ready_context):
    incoming = ready_context.context(
        [ready_context.worker_module_id],
        direction="in",
        relations=["IMPORTS"],
    )
    outgoing = ready_context.context(
        [ready_context.worker_module_id],
        direction="out",
        relations=["IMPORTS"],
    )

    assert [row["relation_type"] for row in incoming["relations"]] == ["IMPORTS"]
    assert incoming["relations"][0]["binding_kind"] == "explicit_alias"
    assert incoming["relations"][0]["binding_name"] == "worker"
    assert ready_context.service_module_id in {
        row["entity_id"] for row in incoming["nodes"]
    }
    assert outgoing["relations"] == []


def test_symbol_context_preserves_order_ranges_bindings_and_unresolved_evidence(
    ready_context,
):
    response = ready_context.context(
        [ready_context.run_symbol_id, ready_context.service_class_id],
        direction="out",
        relations=["INHERITS", "CALLS"],
    )

    assert [row["relation_type"] for row in response["relations"]] == [
        "CALLS", "INHERITS",
    ]
    unresolved = response["relations"][1]
    assert unresolved == {
        "relation_id": unresolved["relation_id"],
        "source_entity_id": ready_context.service_class_id,
        "source_file_id": ready_context.service_file_id,
        "source_module_id": None,
        "source_symbol_id": ready_context.service_class_id,
        "target_entity_id": None,
        "target_module_id": None,
        "target_symbol_id": None,
        "target_reference": "external.Base",
        "relation_type": "INHERITS",
        "source_start_line": unresolved["source_start_line"],
        "source_end_line": unresolved["source_end_line"],
        "source_start_byte": unresolved["source_start_byte"],
        "source_end_byte": unresolved["source_end_byte"],
        "binding_name": None,
        "binding_kind": None,
        "binding_name_tokens_casefold": None,
        "confidence": 0.0,
        "resolution_state": "unresolved",
        "metadata_json": "{}",
    }


def test_file_only_occurrence_expands_file_scoped_relations(ready_context):
    response = ready_context.context(
        [ready_context.loose_file_id], direction="out", relations=["IMPORTS"]
    )

    assert response["relations"][0]["source_entity_id"] == ready_context.loose_file_id
    assert response["relations"][0]["binding_kind"] == "implicit_binding"
    assert ready_context.service_module_id in {
        node["entity_id"] for node in response["nodes"]
    }


def test_depth_budget_is_breadth_first_and_frontiers_are_deterministic(ready_context):
    response = ready_context.context(
        [ready_context.service_module_id], direction="out", depth=2
    )
    repeated = ready_context.context(
        [ready_context.service_module_id], direction="out", depth=2
    )

    assert response["nodes"] == repeated["nodes"]
    assert response["relations"] == repeated["relations"]
    first_frontier = response["relations"][:2]
    assert [row["relation_type"] for row in first_frontier] == [
        "DECLARES", "IMPORTS",
    ]
    assert [
        (
            row["relation_type"],
            row["source_entity_id"],
            row["target_entity_id"] or row["target_reference"],
            row["source_start_byte"],
            row["relation_id"],
        )
        for row in first_frontier
    ] == sorted(
        (
            row["relation_type"],
            row["source_entity_id"],
            row["target_entity_id"] or row["target_reference"],
            row["source_start_byte"],
            row["relation_id"],
        )
        for row in first_frontier
    )


@pytest.mark.parametrize(
    ("changes", "warning"),
    [
        ({"max_nodes": 1}, "max_nodes_exhausted"),
        ({"max_files": 1}, "max_files_exhausted"),
    ],
)
def test_node_and_file_budgets_report_truncation(ready_context, changes, warning):
    response = ready_context.context(
        [ready_context.service_module_id, ready_context.worker_module_id],
        **changes,
    )

    assert response["truncated"] is True
    assert warning in response["warnings"]


def test_source_requires_explicit_true_and_current_hash(ready_context):
    omitted = ready_context.context([ready_context.service_file_id])
    assert all("source" not in row for row in omitted["files"])

    included = ready_context.context(
        [ready_context.service_file_id], include_source=True
    )
    service = next(
        row for row in included["files"]
        if row["file_id"] == ready_context.service_file_id
    )
    assert service["source"].startswith("import pkg.worker")

    ready_context.change_source_after_index()
    stale = ready_context.context(
        [ready_context.service_file_id], include_source=True
    )
    assert stale["fresh"] is False
    assert "results" not in stale
    assert all("source" not in row for row in stale["files"])


def test_replaced_project_root_symlink_never_reads_or_leaks_external_source(
    ready_context, monkeypatch, caplog
):
    external_source, sentinel = (
        ready_context.replace_project_root_with_external_symlink()
    )
    attempted_paths = []
    real_open = context_module.os.open
    captured_root = ready_context.runtime._context_root
    caplog.set_level(logging.DEBUG)
    with ready_context.runtime._store.read_lease() as connection:
        indexed_hash = connection.execute(
            "SELECT content_hash FROM files WHERE file_id = ?",
            (ready_context.service_file_id,),
        ).fetchone()[0]
    assert hashlib.sha256(external_source.read_bytes()).hexdigest() == indexed_hash

    public_response = ready_context.runtime.context(
        [ready_context.service_file_id], include_source=True, depth=0
    )
    assert public_response["fresh"] is False
    assert all("source" not in row for row in public_response["files"])

    external_path = str(external_source.parent.parent.parent)
    metadata_text = ready_context.paths.metadata.read_text(encoding="utf-8")
    for text in (
        json.dumps(public_response, sort_keys=True),
        caplog.text,
        metadata_text,
    ):
        assert sentinel not in text
        assert external_path not in text

    def observed_open(path, flags, *args, **kwargs):
        if (
            path == captured_root.path
            or isinstance(path, str) and not os.path.isabs(path)
        ):
            attempted_paths.append(os.fspath(path))
        return real_open(path, flags, *args, **kwargs)

    request = context_module.validate_context_request(
        [ready_context.service_file_id], include_source=True, depth=0
    )
    engine = context_module.CodeGraphContext(
        ready_context.binding.primary or "",
        captured_root,
        ready_context.runtime.config.max_file_bytes,
    )
    with ready_context.runtime._store.read_lease() as connection:
        monkeypatch.setattr(context_module.os, "open", observed_open)
        context_response = engine.context(connection, request)
        monkeypatch.setattr(context_module.os, "open", real_open)
    assert all("source" not in row for row in context_response["files"])
    assert "source_unavailable" in context_response["warnings"]
    assert external_path not in attempted_paths


def test_source_budget_is_aggregate_and_never_returns_partial_source(ready_context):
    response = ready_context.context(
        [ready_context.service_file_id, ready_context.worker_file_id],
        include_source=True,
        max_source_bytes=1,
    )

    service = next(
        row for row in response["files"]
        if row["file_id"] == ready_context.service_file_id
    )
    assert "source" not in service
    assert response["truncated"] is True
    assert "max_source_bytes_exhausted" in response["warnings"]


def test_source_read_budget_uses_actual_bytes_for_grown_file(
    tmp_path, monkeypatch
):
    root = tmp_path / "root"
    root.mkdir()
    source_path = root / "file.py"
    indexed = b"VALUE = 1\n"
    source_path.write_bytes(indexed)
    project_root = context_module.capture_project_root(root)
    source_path.write_bytes(b"X" * 500_000)
    read_sizes = []
    real_read = context_module.os.read

    def observed_read(descriptor, size):
        content = real_read(descriptor, size)
        read_sizes.append(len(content))
        return content

    monkeypatch.setattr(context_module.os, "read", observed_read)

    result = context_module._read_source(
        project_root,
        "file.py",
        hashlib.sha256(indexed).hexdigest(),
        1_000_000,
        read_cap=0,
    )

    assert result == (None, "max_source_bytes_exhausted", 0)
    assert read_sizes == []


def test_source_read_never_requests_beyond_cap_during_concurrent_growth(
    tmp_path, monkeypatch
):
    root = tmp_path / "root"
    root.mkdir()
    source_path = root / "file.py"
    indexed = b"12345"
    source_path.write_bytes(indexed)
    project_root = context_module.capture_project_root(root)
    read_requests = []
    real_read = context_module.os.read

    def grow_after_read(descriptor, size):
        read_requests.append(size)
        content = real_read(descriptor, size)
        source_path.write_bytes(indexed + b"GROWN")
        return content

    monkeypatch.setattr(context_module.os, "read", grow_after_read)

    result = context_module._read_source(
        project_root,
        "file.py",
        hashlib.sha256(indexed).hexdigest(),
        1_000_000,
        read_cap=5,
    )

    assert result == (None, "source_unavailable", 5)
    assert read_requests
    assert all(size <= 5 for size in read_requests)
    assert result[2] <= 5


def test_source_read_budget_tracks_remaining_bytes_across_files(
    ready_context, monkeypatch
):
    service_bytes = ready_context.project_file(
        "src/pkg/service.py"
    ).read_bytes()
    worker_bytes = ready_context.project_file(
        "src/pkg/worker.py"
    ).read_bytes()
    budget = len(service_bytes) + len(worker_bytes) - 1
    read_sizes = []
    real_read = context_module.os.read

    def observed_read(descriptor, size):
        content = real_read(descriptor, size)
        read_sizes.append(len(content))
        return content

    request = context_module.validate_context_request(
        [ready_context.service_file_id, ready_context.worker_file_id],
        include_source=True,
        depth=0,
        max_source_bytes=budget,
    )
    engine = context_module.CodeGraphContext(
        ready_context.binding.primary or "",
        ready_context.runtime._context_root,
        ready_context.runtime.config.max_file_bytes,
    )
    with ready_context.runtime._store.read_lease() as connection:
        monkeypatch.setattr(context_module.os, "read", observed_read)
        response = engine.context(connection, request)
        monkeypatch.setattr(context_module.os, "read", real_read)

    by_id = {row["file_id"]: row for row in response["files"]}
    assert by_id[ready_context.service_file_id]["source"] == (
        service_bytes.decode("utf-8")
    )
    assert "source" not in by_id[ready_context.worker_file_id]
    assert response["truncated"] is True
    assert "max_source_bytes_exhausted" in response["warnings"]
    assert read_sizes
    assert sum(read_sizes) <= budget


def test_source_open_is_nonblocking_against_regular_file_swap(
    ready_context, monkeypatch
):
    opened_flags = []
    real_open = context_module.os.open

    def observed_open(path, flags, *args, **kwargs):
        opened_flags.append(flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(context_module.os, "open", observed_open)

    source_path = ready_context.project_file("src/pkg/service.py")
    project_root = context_module.capture_project_root(
        ready_context.project_dir
    )
    source, warning, _bytes_consumed = context_module._read_source(
        project_root,
        "src/pkg/service.py",
        hashlib.sha256(source_path.read_bytes()).hexdigest(),
        1_000_000,
        read_cap=1_000_000,
    )

    assert warning is None
    assert source is not None
    assert opened_flags
    relevant = [
        flags for flags in opened_flags
        if flags & (context_module.os.O_NOFOLLOW | context_module.os.O_DIRECTORY)
    ]
    assert relevant
    assert all(flags & context_module.os.O_NONBLOCK for flags in relevant)


def test_source_closes_descendant_fd_when_identity_check_fails(
    tmp_path, monkeypatch
):
    root = tmp_path / "root"
    child = root / "sub"
    child.mkdir(parents=True)
    source_path = child / "file.py"
    source_path.write_text("VALUE = 1\n", encoding="utf-8")
    project_root = context_module.capture_project_root(root)
    opened = []
    child_descriptor = None
    raised = False
    real_open = context_module.os.open
    real_fstat = context_module.os.fstat

    def observed_open(path, flags, *args, **kwargs):
        nonlocal child_descriptor
        descriptor = real_open(path, flags, *args, **kwargs)
        opened.append(descriptor)
        if path == "sub":
            child_descriptor = descriptor
        return descriptor

    def failing_fstat(descriptor):
        nonlocal raised
        if descriptor == child_descriptor and not raised:
            raised = True
            raise OSError("injected descendant identity failure")
        return real_fstat(descriptor)

    monkeypatch.setattr(context_module.os, "open", observed_open)
    monkeypatch.setattr(context_module.os, "fstat", failing_fstat)

    source, warning, _bytes_consumed = context_module._read_source(
        project_root,
        "sub/file.py",
        hashlib.sha256(source_path.read_bytes()).hexdigest(),
        1_000_000,
        read_cap=1_000_000,
    )

    assert (source, warning) == (None, "source_unavailable")
    assert raised is True
    for descriptor in opened:
        with pytest.raises(OSError):
            real_fstat(descriptor)


def test_source_rechecks_named_directory_chain_after_component_rename(
    tmp_path, monkeypatch, caplog
):
    root = tmp_path / "root"
    child = root / "sub"
    child.mkdir(parents=True)
    source_path = child / "file.py"
    sentinel = "DETACHED_DIRECTORY_SOURCE_SENTINEL"
    source_path.write_text(sentinel, encoding="utf-8")
    expected_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    project_root = context_module.capture_project_root(root)
    outside = tmp_path / "outside-sub"
    real_open = context_module.os.open
    renamed = False

    def rename_after_open(path, flags, *args, **kwargs):
        nonlocal renamed
        descriptor = real_open(path, flags, *args, **kwargs)
        if path == "sub" and not renamed:
            renamed = True
            child.rename(outside)
            child.mkdir()
            child.joinpath("file.py").write_text(
                "REPLACEMENT_DIRECTORY_SENTINEL", encoding="utf-8"
            )
        return descriptor

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(context_module.os, "open", rename_after_open)

    source, warning, _bytes_consumed = context_module._read_source(
        project_root,
        "sub/file.py",
        expected_hash,
        1_000_000,
        read_cap=1_000_000,
    )

    assert renamed is True
    assert (source, warning) == (None, "source_unavailable")
    assert _bytes_consumed == 0
    assert outside.joinpath("file.py").read_text(encoding="utf-8") == sentinel
    assert sentinel not in caplog.text
    assert str(outside) not in caplog.text


def test_source_rechecks_name_after_open_during_final_directory_rewalk(
    tmp_path, monkeypatch, caplog
):
    root = tmp_path / "root"
    child = root / "sub"
    child.mkdir(parents=True)
    source_path = child / "file.py"
    sentinel = "DETACHED_POST_REWALK_SENTINEL"
    source_path.write_text(sentinel, encoding="utf-8")
    expected_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    project_root = context_module.capture_project_root(root)
    outside = tmp_path / "outside-sub"
    real_open = context_module.os.open
    component_opens = 0

    def rename_after_final_rewalk_open(path, flags, *args, **kwargs):
        nonlocal component_opens
        descriptor = real_open(path, flags, *args, **kwargs)
        if path == "sub":
            component_opens += 1
            if component_opens == 4:
                child.rename(outside)
                child.mkdir()
                child.joinpath("file.py").write_text(
                    "REPLACEMENT_DIRECTORY_SENTINEL", encoding="utf-8"
                )
        return descriptor

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(
        context_module.os, "open", rename_after_final_rewalk_open
    )

    source, warning, _bytes_consumed = context_module._read_source(
        project_root,
        "sub/file.py",
        expected_hash,
        1_000_000,
        read_cap=1_000_000,
    )

    assert component_opens == 4
    assert (source, warning) == (None, "source_unavailable")
    assert sentinel not in caplog.text
    assert str(outside) not in caplog.text


@pytest.mark.parametrize("unsafe_kind", ["outside", "secret", "symlink"])
def test_outside_secret_and_symlink_source_is_omitted(ready_context, unsafe_kind):
    ready_context.make_source_unsafe(unsafe_kind)

    response = ready_context.context(
        [ready_context.service_file_id], include_source=True
    )

    assert response["fresh"] is False
    assert all("source" not in row for row in response["files"])
    assert {"source_unavailable", "source_changed"} & set(response["warnings"])
