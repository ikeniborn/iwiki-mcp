"""Shared immutable publication protocol and canonical row batches."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterator, Literal, Mapping, Protocol, Sequence

from .canonical import (
    canonical_bytes_sha256,
    canonical_json_bytes,
    canonical_sha256,
)
from .reader import CodeGraphReader as _CodeGraphReader


CodeGraphReader = _CodeGraphReader


RowKind = Literal["repositories", "files", "symbols", "relations"]
PublishMode = Literal["sqlite", "postgres", "mcp"]
PublicationErrorCode = Literal[
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
    "commit_uncertain",
]
AdapterErrorCode = Literal[
    "invalid_config",
    "remote_mcp_failed",
    "source_unavailable",
]
ReadinessErrorCode = Literal["missing_snapshot", "stale_snapshot"]

PUBLICATION_ERROR_CODES: tuple[PublicationErrorCode, ...] = (
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
    "commit_uncertain",
)
ADAPTER_ERROR_CODES: tuple[AdapterErrorCode, ...] = (
    "invalid_config",
    "remote_mcp_failed",
    "source_unavailable",
)
READINESS_ERROR_CODES: tuple[ReadinessErrorCode, ...] = (
    "missing_snapshot",
    "stale_snapshot",
)

_ROW_KINDS: tuple[RowKind, ...] = (
    "repositories",
    "files",
    "symbols",
    "relations",
)
_IDENTITY_FIELDS: Mapping[RowKind, str] = {
    "repositories": "repository_id",
    "files": "file_id",
    "symbols": "symbol_id",
    "relations": "relation_id",
}
_WIRE_FIELDS: Mapping[RowKind, tuple[str, ...]] = {
    "repositories": (
        "repository_id",
        "git_commit",
        "source_fingerprint",
        "config_fingerprint",
        "parser_fingerprint",
        "normalizer_version",
        "unicode_data_version",
        "state",
        "indexed_at",
    ),
    "files": (
        "file_id",
        "repository_id",
        "path",
        "path_casefold",
        "file_local_name",
        "file_name_tokens_casefold",
        "language",
        "content_hash",
        "parser_version",
        "size_bytes",
        "start_line",
        "end_line",
        "start_byte",
        "end_byte",
        "module_key",
        "module_id",
        "module_qualified_name",
        "module_local_name",
        "module_name_tokens_casefold",
    ),
    "symbols": (
        "symbol_id",
        "file_id",
        "kind",
        "qualified_name",
        "local_name",
        "name_tokens_casefold",
        "start_line",
        "end_line",
        "start_byte",
        "end_byte",
        "signature",
        "signature_casefold",
        "visibility",
        "content_hash",
        "metadata_json",
    ),
    "relations": (
        "relation_id",
        "source_file_id",
        "source_module_id",
        "source_symbol_id",
        "target_module_id",
        "target_symbol_id",
        "target_reference",
        "relation_type",
        "source_start_line",
        "source_end_line",
        "source_start_byte",
        "source_end_byte",
        "binding_name",
        "binding_kind",
        "binding_name_tokens_casefold",
        "confidence",
        "resolution_state",
        "metadata_json",
    ),
}


@dataclass(frozen=True)
class SnapshotHeader:
    protocol_version: int
    schema_version: int
    repository_id: str
    source_fingerprint: str
    parser_fingerprint: str
    normalizer_version: str
    unicode_data_version: str
    languages: tuple[str, ...]
    expected_counts: Mapping[str, int]
    graph_payload_revision: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "languages", tuple(self.languages))
        try:
            counts = dict(self.expected_counts)
        except (TypeError, ValueError) as exc:
            raise ValueError("expected_counts must contain the four row kinds") from exc
        if set(counts) != set(_ROW_KINDS):
            raise ValueError("expected_counts must contain the four row kinds")
        if any(type(counts[kind]) is not int or counts[kind] < 0 for kind in _ROW_KINDS):
            raise ValueError("expected_counts must contain non-negative integers")
        normalized = {kind: counts[kind] for kind in _ROW_KINDS}
        object.__setattr__(
            self,
            "expected_counts",
            MappingProxyType(normalized),
        )


@dataclass(frozen=True)
class SnapshotBatch:
    kind: RowKind
    ordinal: int
    row_count: int
    byte_count: int
    payload_hash: str
    payload: bytes


@dataclass(frozen=True)
class PublicationSession:
    session_id: str
    lease_expires_at: str
    base_snapshot_revision: str | None
    base_markdown_token: str | int


class SnapshotPublisher(Protocol):
    def begin(self, header: SnapshotHeader) -> PublicationSession: ...

    def publish_batch(
        self, session: PublicationSession, batch: SnapshotBatch
    ) -> dict[str, object]: ...

    def finalize(self, session: PublicationSession) -> dict[str, object]: ...

    def abort(self, session: PublicationSession) -> dict[str, object]: ...


def _portable_row(kind: RowKind, row: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(row, Mapping):
        raise ValueError("snapshot rows must be mappings")
    portable = {field: row[field] for field in _WIRE_FIELDS[kind] if field in row}
    identity = portable.get(_IDENTITY_FIELDS[kind])
    if not isinstance(identity, str) or not identity:
        raise ValueError(f"{kind} row identity is required")
    return portable


def _projected_rows(
    kind: RowKind, rows: Sequence[Mapping[str, object]]
) -> tuple[Mapping[str, object], ...]:
    identity_field = _IDENTITY_FIELDS[kind]
    projected = [_portable_row(kind, row) for row in rows]
    projected.sort(key=lambda row: str(row[identity_field]))
    return tuple(projected)


def _canonical_rows(
    rows: Mapping[RowKind, Sequence[Mapping[str, object]]],
) -> dict[RowKind, tuple[Mapping[str, object], ...]]:
    if not isinstance(rows, Mapping) or set(rows) != set(_ROW_KINDS):
        raise ValueError("snapshot rows must contain the four row kinds")
    return {kind: _projected_rows(kind, rows[kind]) for kind in _ROW_KINDS}


def graph_payload_revision(
    rows: Mapping[RowKind, Sequence[Mapping[str, object]]],
) -> str:
    """Hash the complete safe graph payload with stable table row ordering."""
    return canonical_sha256(_canonical_rows(rows), prefix=True)


def _batch(
    kind: RowKind, ordinal: int, rows: Sequence[bytes]
) -> SnapshotBatch:
    payload = b"[" + b",".join(rows) + b"]"
    return SnapshotBatch(
        kind=kind,
        ordinal=ordinal,
        row_count=len(rows),
        byte_count=len(payload),
        payload_hash=canonical_bytes_sha256(payload, prefix=True),
        payload=payload,
    )


def iter_snapshot_batches(
    rows: Mapping[RowKind, Sequence[Mapping[str, object]]],
    *,
    max_rows: int,
    max_bytes: int,
) -> Iterator[SnapshotBatch]:
    """Yield stable identity-sorted batches bounded by row and byte limits."""
    if type(max_rows) is not int or max_rows <= 0:
        raise ValueError("max_rows must be a positive integer")
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if not isinstance(rows, Mapping) or set(rows) != set(_ROW_KINDS):
        raise ValueError("snapshot rows must contain the four row kinds")

    for kind in _ROW_KINDS:
        projected = _projected_rows(kind, rows[kind])
        pending: list[bytes] = []
        pending_size = 2
        ordinal = 0
        for row in projected:
            serialized = canonical_json_bytes(row)
            comma_size = 1 if pending else 0
            candidate_size = pending_size + comma_size + len(serialized)
            if pending and (
                len(pending) >= max_rows or candidate_size > max_bytes
            ):
                yield _batch(kind, ordinal, pending)
                ordinal += 1
                pending = []
                pending_size = 2
                candidate_size = pending_size + len(serialized)
            if candidate_size > max_bytes:
                raise ValueError(f"{kind} row exceeds max_bytes")
            pending.append(serialized)
            pending_size = candidate_size
        if pending:
            yield _batch(kind, ordinal, pending)
