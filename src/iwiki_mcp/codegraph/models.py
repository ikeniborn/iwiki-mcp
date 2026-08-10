"""Stable code graph records and identifiers."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath


class CodeGraphError(RuntimeError):
    code = "code_graph_error"


LANGUAGE_PREFIXES = {"python": "py"}


def _prefix(language: str) -> str:
    try:
        return LANGUAGE_PREFIXES[language]
    except KeyError as exc:
        raise ValueError("unsupported code graph language") from exc


def _hashed(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def file_id(domain: str, language: str, path: str) -> str:
    normalized_path = PurePosixPath(path).as_posix()
    return f"{_prefix(language)}:file:{_hashed('file', domain, language, normalized_path)}"


def symbol_id(language: str, domain: str, module: str, qualified: str, signature: str) -> str:
    digest = _hashed("symbol", language, domain, module, qualified, signature)
    return f"{_prefix(language)}:symbol:{digest}"


def relation_id(
    language: str,
    source_identity: str,
    relation_type: str,
    source_location: str,
    target_identity_or_reference: str,
) -> str:
    digest = _hashed(
        "relation",
        source_identity,
        relation_type,
        source_location,
        target_identity_or_reference,
    )
    return f"{_prefix(language)}:relation:{digest}"


@dataclass(frozen=True)
class FileRecord:
    file_id: str
    repository_id: str
    path: str
    language: str
    content_hash: str
    parser_version: str
    size_bytes: int


@dataclass(frozen=True)
class SymbolRecord:
    symbol_id: str
    file_id: str
    kind: str
    qualified_name: str
    local_name: str
    start_line: int
    end_line: int
    start_byte: int | None
    end_byte: int | None
    signature: str | None
    visibility: str | None
    content_hash: str
    metadata_json: str


@dataclass(frozen=True)
class ReferenceRecord:
    source_symbol_id: str | None
    source_file_id: str
    relation_type: str
    target_reference: str | None
    source_line: int | None


@dataclass(frozen=True)
class RelationRecord:
    relation_id: str
    source_symbol_id: str | None
    source_file_id: str
    target_symbol_id: str | None
    target_reference: str | None
    relation_type: str
    source_line: int | None
    confidence: float
    resolution_state: str
    metadata_json: str


@dataclass(frozen=True)
class ParsedFile:
    file: FileRecord
    symbols: tuple[SymbolRecord, ...]
    references: tuple[ReferenceRecord, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ResolutionResult:
    relations: tuple[RelationRecord, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class SearchResult:
    symbol_id: str
    kind: str
    qualified_name: str
    local_name: str
    signature: str | None
    path: str
    start_line: int
    end_line: int
    start_byte: int | None
    end_byte: int | None
    match: str
