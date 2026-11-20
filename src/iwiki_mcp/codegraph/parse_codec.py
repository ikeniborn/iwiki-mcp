"""Closed JSON codec for local, intermediate language-adapter results."""
from __future__ import annotations

from dataclasses import fields
from functools import lru_cache
from typing import Literal, get_args, get_origin, get_type_hints, TYPE_CHECKING

from .discovery import SourceFile
from .languages import bash, javascript, python, typescript
from .models import FileRecord, ParsedFile, ReferenceRecord, SymbolRecord, file_id

if TYPE_CHECKING:
    from .indexer import AdapterBinding


def parse_identity(
    binding: AdapterBinding, source: SourceFile, *, checkout: str, domain: str,
    normalization_versions: tuple[str, str],
) -> dict | None:
    """Return the exact parse inputs for an explicitly supported adapter."""
    adapter = binding.adapter
    if type(adapter) not in (
        python.PythonAdapter, typescript.TypeScriptAdapter,
        javascript.JavaScriptAdapter, bash.BashAdapter,
    ):
        return None
    return {
        "format": 1,
        "checkout": checkout,
        "domain": domain,
        "language": adapter.language,
        "path": source.path,
        "content_hash": source.content_hash,
        "size_bytes": source.size_bytes,
        "parser_version": binding.parser_version,
        "grammar_version": binding.grammar_version,
        "adapter_version": binding.adapter_version,
        "normalizer_version": normalization_versions[0],
        "unicode_data_version": normalization_versions[1],
        "context": adapter.parse_context(source.path),
    }


def _variants(language):
    return {
        "python": {"python": python._PythonParsedFile, "base": ParsedFile},
        "typescript": {"typescript": typescript._TypeScriptParsedFile},
        "javascript": {"javascript": javascript._JavaScriptParsedFile},
        "bash": {"base": ParsedFile},
    }[language]


@lru_cache(maxsize=None)
def _field_names(cls):
    return tuple(field.name for field in fields(cls))


def _encode_record(cls, record):
    return {name: getattr(record, name) for name in _field_names(cls)}


def encode_parsed(parsed: ParsedFile, identity: dict) -> dict:
    for variant, cls in _variants(identity["language"]).items():
        if type(parsed) is cls:
            break
    else:
        raise ValueError("unsupported parsed variant")
    result = {
        "variant": variant,
        "file": _encode_record(FileRecord, parsed.file),
        "symbols": [_encode_record(SymbolRecord, item) for item in parsed.symbols],
        "references": [_encode_record(ReferenceRecord, item) for item in parsed.references],
        "warnings": list(parsed.warnings),
    }
    if variant == "python":
        result["evidence"] = [list(row) for row in parsed._adapter_evidence]
    return result


@lru_cache(maxsize=None)
def _schema(cls):
    # Only local, statically selected record classes supply annotations.
    schema = {}
    for name, annotation in get_type_hints(cls).items():
        alternatives = get_args(annotation) or (annotation,)
        types = []
        literals = []
        for item in alternatives:
            if get_origin(item) is Literal:
                literals.extend(get_args(item))
            elif isinstance(item, str):
                literals.append(item)
            elif item in (str, int, type(None)):
                types.append(item)
            else:
                raise TypeError("unsupported parse record schema")
        schema[name] = (tuple(types), tuple(literals))
    return schema


def _record(cls, data):
    schema = _schema(cls)
    if type(data) is not dict or data.keys() != schema.keys():
        raise ValueError("invalid parse record fields")
    for name, (types, literals) in schema.items():
        value = data[name]
        if type(value) not in types and not (type(value) is str and value in literals):
            raise ValueError("invalid parse record scalar")
    return cls(**data)


def decode_parsed(payload: object, identity: dict) -> ParsedFile:
    if type(payload) is not dict:
        raise ValueError("invalid parse payload")
    variant = payload.get("variant")
    variants = _variants(identity["language"])
    if type(variant) is not str or variant not in variants:
        raise ValueError("invalid parse variant")
    keys = {"variant", "file", "symbols", "references", "warnings"}
    if variant == "python":
        keys.add("evidence")
    if payload.keys() != keys or any(
        type(payload[key]) is not list for key in keys - {"file", "variant"}
    ):
        raise ValueError("invalid parse payload fields")

    file = _record(FileRecord, payload["file"])
    prefix = {"python": "py", "bash": "sh", "typescript": "ts", "javascript": "js"}
    if (
        file.repository_id != identity["domain"]
        or file.language != identity["language"]
        or any(getattr(file, key) != identity[key] for key in
               ("path", "content_hash", "size_bytes", "parser_version"))
        or file.module_key != file.path
        or file.file_id != file_id(
            file.language, prefix[file.language], file.repository_id, file.path
        )
        or (file.language == "python"
            and file.module_qualified_name != identity["context"]["module_name"])
    ):
        raise ValueError("invalid parse file identity")
    symbols = tuple(_record(SymbolRecord, row) for row in payload["symbols"])
    references = tuple(_record(ReferenceRecord, row) for row in payload["references"])
    if (any(item.file_id != file.file_id for item in symbols)
            or any(item.source_file_id != file.file_id for item in references)):
        raise ValueError("invalid parse member identity")
    warnings = tuple(payload["warnings"])
    if any(type(item) is not str for item in warnings):
        raise ValueError("invalid parse warning")
    if file.language == "python" and variant == "base":
        if symbols or references or "parse_error" not in warnings:
            raise ValueError("invalid Python parser-error variant")
    if variant == "python":
        evidence = payload["evidence"]
        if any(type(row) is not list or len(row) != 5
               or any(type(item) is not str for item in row) for row in evidence):
            raise ValueError("invalid Python evidence")
        return variants[variant](file, symbols, references, warnings, tuple(map(tuple, evidence)))
    return variants[variant](file, symbols, references, warnings)
