from dataclasses import FrozenInstanceError
import json

import pytest

from iwiki_mcp.codegraph.canonical import canonical_json_bytes, canonical_sha256
from iwiki_mcp.codegraph.discovery import SourceFile
from iwiki_mcp.codegraph.fingerprint import source_fingerprint
from iwiki_mcp.codegraph.publication import (
    ADAPTER_ERROR_CODES,
    PUBLICATION_ERROR_CODES,
    READINESS_ERROR_CODES,
    PublicationSession,
    SnapshotBatch,
    SnapshotHeader,
    graph_payload_revision,
    iter_snapshot_batches,
)
from iwiki_mcp.codegraph.reader import CodeGraphReader
from iwiki_mcp.codegraph.store import _snapshot_revision


def _snapshot_header(**overrides):
    values = {
        "protocol_version": 1,
        "schema_version": 2,
        "repository_id": "docs",
        "source_fingerprint": "a" * 64,
        "parser_fingerprint": "b" * 64,
        "normalizer_version": "casefold-token-v1",
        "unicode_data_version": "16.0.0",
        "languages": ("python",),
        "expected_counts": {
            "repositories": 1,
            "files": 0,
            "symbols": 0,
            "relations": 0,
        },
        "graph_payload_revision": "sha256:" + "c" * 64,
    }
    values.update(overrides)
    return SnapshotHeader(**values)


def test_canonical_json_bytes_preserve_existing_encoding_contract():
    left = {"z": "Straße", "nested": {"z": 1, "a": []}}
    right = {"nested": {"a": [], "z": 1}, "z": "Straße"}

    expected = b'{"nested":{"a":[],"z":1},"z":"Stra\\u00dfe"}'
    assert canonical_json_bytes(left) == expected
    assert canonical_json_bytes(right) == expected
    assert canonical_sha256(left, prefix=False) == (
        "90303f8ae36027756f468e55f85d45d6d25313a6999bda6f3606f51a5c2d2b65"
    )
    assert canonical_sha256(left, prefix=True) == (
        "sha256:90303f8ae36027756f468e55f85d45d6d25313a6999bda6f3606f51a5c2d2b65"
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_paths_reject_non_finite_numbers(value):
    with pytest.raises(ValueError, match="JSON"):
        canonical_json_bytes({"value": value})
    with pytest.raises(ValueError, match="JSON"):
        canonical_sha256({"value": value}, prefix=True)

    rows = {
        "repositories": (),
        "files": ({"file_id": "py:file:a", "size_bytes": value},),
        "symbols": (),
        "relations": (),
    }
    with pytest.raises(ValueError, match="JSON"):
        tuple(iter_snapshot_batches(rows, max_rows=10, max_bytes=4096))


def test_existing_fingerprint_and_snapshot_revision_vectors_do_not_change():
    files = (
        SourceFile("b.ts", b"raw-b", "b" * 64, 5),
        SourceFile("a.py", b"raw-a", "a" * 64, 5),
    )
    repository = {
        "repository_id": "docs",
        "git_commit": None,
        "source_fingerprint": "a" * 64,
        "config_fingerprint": "b" * 64,
        "parser_fingerprint": "c" * 64,
        "normalizer_version": "casefold-token-v1",
        "unicode_data_version": "16.0.0",
    }

    assert source_fingerprint(files) == (
        "ded495f4ef468e627b82271e70f87caa64f6624b80d85a08f2d8508bb2a7ca7e"
    )
    assert _snapshot_revision(repository, (), (), (), ()) == (
        "sha256:060c5e7c7aee771ca94aef3d0e4b6f2f6e98ddf7ec3abce685290dd75b2afedf"
    )


def test_canonical_batches_are_stable_and_row_native():
    rows = {
        "repositories": (
            {
                "repository_id": "docs",
                "root_path": "/private/project",
                "state": "rebuilding",
            },
        ),
        "files": (
            {"file_id": "py:file:b", "path": "b.py"},
            {"file_id": "py:file:a", "path": "a.py"},
        ),
        "symbols": (),
        "relations": (),
    }

    first = tuple(iter_snapshot_batches(rows, max_rows=1, max_bytes=4096))
    second = tuple(iter_snapshot_batches(rows, max_rows=1, max_bytes=4096))

    assert first == second
    assert [batch.kind for batch in first] == [
        "repositories",
        "files",
        "files",
    ]
    assert [batch.ordinal for batch in first] == [0, 0, 1]
    assert [batch.row_count for batch in first] == [1, 1, 1]
    assert all(batch.byte_count == len(batch.payload) for batch in first)
    assert all(batch.payload_hash.startswith("sha256:") for batch in first)
    assert first[1].payload == b'[{"file_id":"py:file:a","path":"a.py"}]'
    assert first[1].payload_hash == (
        "sha256:b3a596d92a704659e31b8652ceb4f2b7ee0d45a0993e2521830794afed26861f"
    )
    assert b"root_path" not in b"".join(batch.payload for batch in first)


@pytest.mark.parametrize(
    "forbidden_field",
    [
        "source",
        "source_text",
        "password",
        "token",
        "dsn",
        "wiki_code_links",
        "future_unknown_field",
    ],
)
def test_wire_rows_project_out_forbidden_and_unknown_fields(forbidden_field):
    rows = {
        "repositories": (),
        "files": (
            {
                "file_id": "py:file:a",
                "path": "a.py",
                forbidden_field: "must-not-cross-wire",
            },
        ),
        "symbols": (),
        "relations": (),
    }

    batch = tuple(iter_snapshot_batches(rows, max_rows=10, max_bytes=4096))[0]

    assert json.loads(batch.payload) == [
        {"file_id": "py:file:a", "path": "a.py"}
    ]
    assert b"must-not-cross-wire" not in batch.payload


@pytest.mark.parametrize(
    ("kind", "identity_field", "identity"),
    [
        ("repositories", "repository_id", "docs"),
        ("files", "file_id", "py:file:a"),
        ("symbols", "symbol_id", "py:symbol:a"),
        ("relations", "relation_id", "py:relation:a"),
    ],
)
def test_every_wire_kind_projects_only_explicit_fields(
    kind, identity_field, identity
):
    rows = {
        "repositories": (),
        "files": (),
        "symbols": (),
        "relations": (),
    }
    rows[kind] = (
        {
            identity_field: identity,
            "future_unknown_field": "must-not-cross-wire",
        },
    )

    batch = tuple(iter_snapshot_batches(rows, max_rows=10, max_bytes=4096))[0]

    assert json.loads(batch.payload) == [{identity_field: identity}]


def test_batch_payload_hash_matches_independent_canonical_recomputation():
    rows = {
        "repositories": (),
        "files": (
            {"path": "b.py", "file_id": "py:file:b"},
            {"path": "a.py", "file_id": "py:file:a"},
        ),
        "symbols": (),
        "relations": (),
    }

    batch = tuple(iter_snapshot_batches(rows, max_rows=10, max_bytes=4096))[0]
    target_rows = json.loads(batch.payload)

    assert batch.payload_hash == canonical_sha256(target_rows, prefix=True)
    assert batch.payload == canonical_json_bytes(target_rows)


def test_graph_payload_revision_is_stable_across_order_and_wire_batches():
    rows = {
        "repositories": (
            {"state": "rebuilding", "repository_id": "docs"},
        ),
        "files": (
            {"path": "b.py", "file_id": "py:file:b"},
            {"path": "a.py", "file_id": "py:file:a"},
        ),
        "symbols": (
            {"symbol_id": "py:symbol:b"},
            {"symbol_id": "py:symbol:a"},
        ),
        "relations": (
            {"relation_id": "py:relation:b"},
            {"relation_id": "py:relation:a"},
        ),
    }
    reordered = {
        "relations": tuple(reversed(rows["relations"])),
        "symbols": tuple(reversed(rows["symbols"])),
        "files": tuple(reversed(rows["files"])),
        "repositories": tuple(reversed(rows["repositories"])),
    }

    revision = graph_payload_revision(rows)
    batches = tuple(iter_snapshot_batches(rows, max_rows=1, max_bytes=4096))
    wire_rows = {
        kind: tuple(
            row
            for batch in batches
            if batch.kind == kind
            for row in json.loads(batch.payload)
        )
        for kind in ("repositories", "files", "symbols", "relations")
    }

    assert revision == (
        "sha256:84397914da91de902f4c21d9d9ee3a102f9ab05f4aea8ac3df4f424733c29244"
    )
    assert graph_payload_revision(reordered) == revision
    assert graph_payload_revision(wire_rows) == revision


def test_graph_payload_revision_is_not_link_inclusive_snapshot_revision():
    graph_rows = {
        "repositories": ({"repository_id": "docs", "state": "rebuilding"},),
        "files": (),
        "symbols": (),
        "relations": (),
    }
    repository = {
        "repository_id": "docs",
        "git_commit": None,
        "source_fingerprint": "a" * 64,
        "config_fingerprint": "b" * 64,
        "parser_fingerprint": "c" * 64,
        "normalizer_version": "casefold-token-v1",
        "unicode_data_version": "16.0.0",
    }

    assert graph_payload_revision(graph_rows) != _snapshot_revision(
        repository, (), (), (), ()
    )


def test_batch_limits_are_enforced_without_empty_batches():
    rows = {
        "repositories": (),
        "files": (
            {"file_id": "py:file:a", "path": "a.py"},
            {"file_id": "py:file:b", "path": "b.py"},
        ),
        "symbols": (),
        "relations": (),
    }
    one_row_bytes = len(canonical_json_bytes([rows["files"][0]]))

    batches = tuple(
        iter_snapshot_batches(rows, max_rows=10, max_bytes=one_row_bytes)
    )

    assert [batch.row_count for batch in batches] == [1, 1]
    assert [batch.ordinal for batch in batches] == [0, 1]
    with pytest.raises(ValueError, match="max_bytes"):
        tuple(
            iter_snapshot_batches(
                rows,
                max_rows=10,
                max_bytes=one_row_bytes - 1,
            )
        )


def test_batching_serializes_each_projected_row_once(monkeypatch):
    from iwiki_mcp.codegraph import publication

    rows = {
        "repositories": (),
        "files": (
            {"file_id": "py:file:c", "path": "c.py"},
            {"file_id": "py:file:a", "path": "a.py"},
            {"file_id": "py:file:b", "path": "b.py"},
        ),
        "symbols": (),
        "relations": (),
    }
    serialized = []
    serialize = publication.canonical_json_bytes

    def counted(value):
        serialized.append(value)
        return serialize(value)

    monkeypatch.setattr(publication, "canonical_json_bytes", counted)

    batches = tuple(
        publication.iter_snapshot_batches(rows, max_rows=2, max_bytes=4096)
    )

    assert [batch.row_count for batch in batches] == [2, 1]
    assert len(serialized) == 3
    assert all(isinstance(value, dict) for value in serialized)


def test_batching_does_not_read_later_kinds_before_current_batch():
    class UnreadableRows:
        def __iter__(self):
            raise AssertionError("later row kind was read eagerly")

    rows = {
        "repositories": ({"repository_id": "docs"},),
        "files": UnreadableRows(),
        "symbols": (),
        "relations": (),
    }

    batches = iter_snapshot_batches(rows, max_rows=1, max_bytes=4096)

    assert next(batches).kind == "repositories"


@pytest.mark.parametrize(
    "rows",
    [
        {"repositories": (), "files": (), "symbols": (), "relations": (), "links": ()},
        {"repositories": ({"state": "ready"},), "files": (), "symbols": (), "relations": ()},
    ],
)
def test_batches_reject_unknown_kinds_and_missing_row_identities(rows):
    with pytest.raises(ValueError):
        tuple(iter_snapshot_batches(rows, max_rows=1, max_bytes=4096))


def test_publication_contract_values_are_closed_and_immutable():
    assert PUBLICATION_ERROR_CODES == (
        "unauthorized",
        "scope_mismatch",
        "unsupported_storage",
        "busy",
        "session_expired",
        "invalid_batch",
        "batch_conflict",
        "snapshot_incomplete",
        "revision_mismatch",
        "snapshot_conflict",
        "markdown_unavailable",
    )
    assert ADAPTER_ERROR_CODES == (
        "invalid_config",
        "remote_mcp_failed",
        "source_unavailable",
    )
    assert READINESS_ERROR_CODES == ("missing_snapshot", "stale_snapshot")

    header = SnapshotHeader(
        protocol_version=1,
        schema_version=2,
        repository_id="docs",
        source_fingerprint="a" * 64,
        parser_fingerprint="b" * 64,
        normalizer_version="casefold-token-v1",
        unicode_data_version="16.0.0",
        languages=("python",),
        expected_counts={
            "repositories": 1,
            "files": 0,
            "symbols": 0,
            "relations": 0,
        },
        graph_payload_revision="sha256:" + "c" * 64,
    )
    batch = SnapshotBatch(
        kind="files",
        ordinal=0,
        row_count=0,
        byte_count=2,
        payload_hash="sha256:" + "d" * 64,
        payload=b"[]",
    )
    session = PublicationSession(
        session_id="opaque",
        lease_expires_at="2026-08-14T00:00:00Z",
        base_snapshot_revision=None,
        base_markdown_token=0,
    )

    assert header.repository_id == "docs"
    assert batch.payload == b"[]"
    assert session.base_markdown_token == 0
    assert CodeGraphReader.__name__ == "CodeGraphReader"
    with pytest.raises(FrozenInstanceError):
        session.session_id = "changed"


def test_snapshot_header_defensively_freezes_languages_and_counts():
    languages = ["python"]
    counts = {
        "repositories": 1,
        "files": 0,
        "symbols": 0,
        "relations": 0,
    }

    header = _snapshot_header(languages=languages, expected_counts=counts)
    languages.append("typescript")
    counts["files"] = 99

    assert header.languages == ("python",)
    assert dict(header.expected_counts) == {
        "repositories": 1,
        "files": 0,
        "symbols": 0,
        "relations": 0,
    }
    with pytest.raises(TypeError):
        header.expected_counts["files"] = 2


@pytest.mark.parametrize(
    "counts",
    [
        {"repositories": 1, "files": 0, "symbols": 0},
        {
            "repositories": 1,
            "files": 0,
            "symbols": 0,
            "relations": 0,
            "wiki_code_links": 0,
        },
        {
            "repositories": 1,
            "files": -1,
            "symbols": 0,
            "relations": 0,
        },
        {
            "repositories": 1,
            "files": False,
            "symbols": 0,
            "relations": 0,
        },
    ],
)
def test_snapshot_header_rejects_invalid_expected_counts(counts):
    with pytest.raises(ValueError, match="expected_counts"):
        _snapshot_header(expected_counts=counts)
