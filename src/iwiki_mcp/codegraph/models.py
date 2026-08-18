"""Stable code graph records, normalization, and portable identifiers."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal
import unicodedata


class CodeGraphError(RuntimeError):
    code = "code_graph_error"


NORMALIZER_VERSION = "casefold-token-v1"
UNICODE_DATA_VERSION = unicodedata.unidata_version
_TOKENS = re.compile(r"[^\W_]+", re.UNICODE)


def _hashed(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _identity(prefix: str, kind: str, *parts: str) -> str:
    if not isinstance(prefix, str) or not prefix or "\0" in prefix:
        raise ValueError("invalid language prefix")
    return f"{prefix}:{kind}:{_hashed(kind, *parts)}"


def token_key(*values: str) -> str:
    """Return sorted, distinct casefolded tokens with addressable boundaries."""
    tokens = sorted({
        token
        for value in values
        for token in _TOKENS.findall(value.casefold())
    })
    return "\x1f" + "\x1f".join(tokens) + "\x1f"


def compact_casefold(value: str | None) -> str | None:
    """Persist a casefold scalar only when ASCII ``lower`` is insufficient."""
    if value is None or value.isascii():
        return None
    return value.casefold()


def _validated_relative_posix(path: str) -> PurePosixPath:
    """Return one valid UTF-8 project-relative POSIX path."""
    if not isinstance(path, str) or not path or "\0" in path or "\\" in path:
        raise ValueError("invalid code graph path")
    try:
        path.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("invalid code graph path") from exc
    posix_path = PurePosixPath(path)
    windows_path = PureWindowsPath(path)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or ".." in posix_path.parts
    ):
        raise ValueError("invalid code graph path")
    return posix_path


def module_key(path: str) -> str:
    """Validate and normalize a project-relative POSIX occurrence path."""
    posix_path = _validated_relative_posix(path)
    normalized = "/".join(part for part in posix_path.parts if part != ".")
    if not normalized:
        raise ValueError("invalid code graph path")
    return normalized


def file_id(language: str, language_prefix: str, domain: str, path: str) -> str:
    normalized_path = module_key(path)
    return _identity(language_prefix, "file", domain, language, normalized_path)


def module_id(
    language: str,
    language_prefix: str,
    domain: str,
    key: str,
    qualified_name: str,
) -> str:
    return _identity(
        language_prefix,
        "module",
        language,
        domain,
        module_key(key),
        qualified_name,
    )


def symbol_id(
    language: str,
    language_prefix: str,
    domain: str,
    key: str,
    qualified_name: str,
    normalized_signature: str,
) -> str:
    return _identity(
        language_prefix,
        "symbol",
        language,
        domain,
        module_key(key),
        qualified_name,
        normalized_signature,
    )


def relation_id(
    language: str,
    language_prefix: str,
    domain: str,
    source_entity_id: str,
    relation_type: str,
    source_start_line: int,
    source_end_line: int,
    source_start_byte: int,
    source_end_byte: int,
    target_entity_id: str | None,
    target_reference: str | None,
    binding_kind: str | None,
    binding_name: str | None,
) -> str:
    ranges = (
        source_start_line,
        source_end_line,
        source_start_byte,
        source_end_byte,
    )
    if any(type(value) is not int or value < 0 for value in ranges):
        raise ValueError("invalid relation source range")
    return _identity(
        language_prefix,
        "relation",
        language,
        domain,
        source_entity_id,
        relation_type,
        *(str(value) for value in ranges),
        target_entity_id or "",
        target_reference or "",
        binding_kind or "",
        binding_name or "",
    )


@dataclass(frozen=True)
class FileRecord:
    file_id: str
    repository_id: str
    path: str
    path_casefold: str | None
    file_local_name: str
    file_name_tokens_casefold: str
    language: str
    content_hash: str
    parser_version: str
    size_bytes: int
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    module_key: str
    module_id: str | None
    module_qualified_name: str | None
    module_local_name: str | None
    module_name_tokens_casefold: str | None


@dataclass(frozen=True)
class SymbolRecord:
    symbol_id: str
    file_id: str
    kind: Literal[
        "class", "function", "async_function", "method",
        "interface", "type_alias", "enum",
    ]
    qualified_name: str
    local_name: str
    name_tokens_casefold: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    signature: str | None
    signature_casefold: str | None
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
    source_byte: int | None = None
    source_module_id: str | None = None
    source_end_line: int | None = None
    source_end_byte: int | None = None
    binding_name: str | None = None
    binding_kind: Literal["implicit_binding", "explicit_alias"] | None = None
    binding_name_tokens_casefold: str | None = None
    target_kind_hint: Literal["module", "member"] | None = None
    resolution_hint: Literal["unresolved"] | None = None
    resolution_scope: Literal["file", "project"] | None = None


@dataclass(frozen=True)
class RelationRecord:
    relation_id: str
    source_file_id: str
    source_module_id: str | None
    source_symbol_id: str | None
    target_module_id: str | None
    target_symbol_id: str | None
    target_reference: str | None
    relation_type: Literal["DECLARES", "IMPORTS", "CALLS", "INHERITS"]
    source_start_line: int
    source_end_line: int
    source_start_byte: int
    source_end_byte: int
    binding_name: str | None
    binding_kind: Literal["implicit_binding", "explicit_alias"] | None
    binding_name_tokens_casefold: str | None
    confidence: float
    resolution_state: Literal[
        "resolved", "partially_resolved", "unresolved", "ambiguous"
    ]
    metadata_json: str

    @property
    def source_line(self) -> int:
        """Compatibility alias for schema-v1 query and resolver callers."""
        return self.source_start_line

    @property
    def source_byte(self) -> int:
        """Compatibility alias for schema-v1 query and resolver callers."""
        return self.source_start_byte


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
    entity_id: str
    entity_type: Literal["file", "module", "symbol"]
    file_id: str | None
    module_id: str | None
    symbol_id: str | None
    kind: Literal[
        "file", "module", "class", "function", "async_function", "method",
        "interface", "type_alias", "enum",
    ]
    qualified_name: str
    local_name: str
    signature: str | None
    path: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    match: Literal[
        "qualified_exact",
        "local_exact",
        "alias_exact",
        "canonical_prefix",
        "alias_prefix",
        "canonical_lexical",
        "alias_lexical",
        "signature",
        "path",
    ]
    matched_alias: str | None
    alias_ambiguous: bool
    alias_target_count: int


@dataclass(frozen=True)
class ContextNode:
    entity_id: str
    entity_type: Literal["file", "module", "symbol"]
    file_id: str | None
    module_id: str | None
    symbol_id: str | None
    kind: Literal[
        "file", "module", "class", "function", "async_function", "method",
        "interface", "type_alias", "enum",
    ]
    qualified_name: str
    local_name: str
    signature: str | None
    path: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int


@dataclass(frozen=True)
class ContextRelation:
    relation_id: str
    source_entity_id: str
    source_file_id: str
    source_module_id: str | None
    source_symbol_id: str | None
    target_entity_id: str | None
    target_module_id: str | None
    target_symbol_id: str | None
    target_reference: str | None
    relation_type: Literal["DECLARES", "IMPORTS", "CALLS", "INHERITS"]
    source_start_line: int
    source_end_line: int
    source_start_byte: int
    source_end_byte: int
    binding_name: str | None
    binding_kind: Literal["implicit_binding", "explicit_alias"] | None
    binding_name_tokens_casefold: str | None
    confidence: float
    resolution_state: Literal[
        "resolved", "partially_resolved", "unresolved", "ambiguous"
    ]
    metadata_json: str
