"""Language-neutral, conservative resolution of normalized references."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .models import (
    FileRecord,
    ParsedFile,
    ReferenceRecord,
    RelationRecord,
    SymbolRecord,
    relation_id,
)


@dataclass(frozen=True)
class SymbolIndex:
    """Project module occurrences and symbols indexed by normalized names."""

    by_qualified: Mapping[str, tuple[SymbolRecord, ...]]
    by_module_local: Mapping[tuple[str, str], tuple[SymbolRecord, ...]]
    modules_by_qualified: Mapping[str, tuple[FileRecord, ...]]
    module_backed_file_ids: frozenset[str]
    languages_by_file_id: Mapping[str, str] = field(default_factory=dict)
    _adapter_evidence: tuple[tuple[str, str, str, str, str], ...] = field(
        default=(), repr=False, compare=False
    )

    @classmethod
    def from_parsed_files(cls, parsed_files: Iterable[ParsedFile]) -> "SymbolIndex":
        parsed = tuple(parsed_files)
        modules: dict[str, list[FileRecord]] = {}
        for item in parsed:
            if item.file.module_id and item.file.module_qualified_name:
                modules.setdefault(item.file.module_qualified_name, []).append(item.file)
        symbols = cls.from_symbols(
            symbol for item in parsed for symbol in item.symbols
        )
        return cls(
            by_qualified=symbols.by_qualified,
            by_module_local=symbols.by_module_local,
            modules_by_qualified={
                name: tuple(sorted(
                    {item.module_id: item for item in values}.values(),
                    key=lambda item: item.module_id or "",
                ))
                for name, values in sorted(modules.items())
            },
            module_backed_file_ids=frozenset(
                item.file.file_id for item in parsed if item.file.module_id
            ),
            languages_by_file_id={
                item.file.file_id: item.file.language for item in parsed
            },
            _adapter_evidence=tuple(sorted({
                evidence
                for item in parsed
                for evidence in getattr(item, "_adapter_evidence", ())
                if _valid_adapter_evidence(evidence)
            })),
        )

    @classmethod
    def from_symbols(cls, symbols: Iterable[SymbolRecord]) -> "SymbolIndex":
        symbols = tuple(symbols)
        qualified: dict[str, list[SymbolRecord]] = {}
        module_local: dict[tuple[str, str], list[SymbolRecord]] = {}
        for symbol in symbols:
            qualified.setdefault(symbol.qualified_name, []).append(symbol)
            try:
                metadata = json.loads(symbol.metadata_json)
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            module = metadata.get("module", "") if isinstance(metadata, dict) else ""
            if not isinstance(module, str):
                module = ""
            expected = module + "." + symbol.local_name if module else symbol.local_name
            if symbol.qualified_name == expected:
                module_local.setdefault((module, symbol.local_name), []).append(symbol)
        return cls(
            by_qualified={key: tuple(sorted(
                {item.symbol_id: item for item in value}.values(),
                key=lambda item: item.symbol_id))
                          for key, value in sorted(qualified.items())},
            by_module_local={key: tuple(sorted(
                {item.symbol_id: item for item in value}.values(),
                key=lambda item: item.symbol_id))
                             for key, value in sorted(module_local.items())},
            modules_by_qualified={},
            module_backed_file_ids=frozenset(
                symbol.file_id
                for symbol in symbols
                if _symbol_module(symbol)
            ),
        )


LANGUAGE_FAMILIES = {
    "python": frozenset({"python"}),
    "typescript": frozenset({"typescript", "javascript"}),
    "javascript": frozenset({"javascript", "typescript"}),
    "bash": frozenset({"bash"}),
}


def family_languages(language: str) -> frozenset[str] | None:
    """Languages whose declarations may satisfy ``language``'s references.

    ``None`` means "apply no filter": an unknown language keeps the
    pre-scoping behaviour, so nothing regresses for callers this map does
    not describe.
    """
    return LANGUAGE_FAMILIES.get(language)


def _valid_adapter_evidence(value: object) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) == 5
        and all(
            isinstance(item, str)
            and item
            and "\0" not in item
            and len(item) <= 512
            for item in value
        )
    )


def _symbol_module(symbol: SymbolRecord) -> str:
    try:
        metadata = json.loads(symbol.metadata_json)
    except (TypeError, json.JSONDecodeError):
        return ""
    module = metadata.get("module", "") if isinstance(metadata, dict) else ""
    return module if isinstance(module, str) else ""


def _relation_sort_key(item: RelationRecord) -> tuple[object, ...]:
    return (
        item.source_symbol_id or item.source_module_id or item.source_file_id,
        item.relation_type,
        item.source_start_line,
        item.source_end_line,
        item.source_start_byte,
        item.source_end_byte,
        item.target_module_id or "",
        item.target_symbol_id or "",
        item.target_reference or "",
        item.binding_kind or "",
        item.binding_name or "",
        item.relation_id,
    )


def sort_relations(relations: Iterable[RelationRecord]) -> tuple[RelationRecord, ...]:
    return tuple(sorted(set(relations), key=_relation_sort_key))


def declaration_relations(
    language: str,
    language_prefix: str,
    domain: str,
    parsed: ParsedFile,
) -> tuple[RelationRecord, ...]:
    """Create one typed ownership edge for each extracted declaration."""
    relations = []
    symbols = tuple(parsed.symbols)
    symbols_by_id = {item.symbol_id: item for item in symbols}
    for target in symbols:
        try:
            metadata = json.loads(target.metadata_json)
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        explicit_owner_id = (
            metadata.get("owner_symbol_id")
            if isinstance(metadata, dict) else None
        )
        owner = (
            symbols_by_id.get(explicit_owner_id)
            if isinstance(explicit_owner_id, str) else None
        )
        if owner is None or owner.symbol_id == target.symbol_id:
            enclosing = tuple(
                candidate for candidate in symbols
                if candidate.symbol_id != target.symbol_id
                and candidate.start_byte <= target.start_byte
                and candidate.end_byte >= target.end_byte
            )
            owner = min(
                enclosing,
                key=lambda item: (
                    item.end_byte - item.start_byte,
                    -item.start_byte,
                    item.symbol_id,
                ),
                default=None,
            )
        source_symbol_id = owner.symbol_id if owner else None
        source_module_id = (
            parsed.file.module_id if owner is None else None
        )
        source_identity = (
            source_symbol_id or source_module_id or parsed.file.file_id
        )
        stable_relation_id = relation_id(
            language,
            language_prefix,
            domain,
            source_identity,
            "DECLARES",
            target.start_line,
            target.end_line,
            target.start_byte,
            target.end_byte,
            target.symbol_id,
            None,
            None,
            None,
        )
        relations.append(RelationRecord(
            relation_id=stable_relation_id,
            source_file_id=parsed.file.file_id,
            source_module_id=source_module_id,
            source_symbol_id=source_symbol_id,
            target_module_id=None,
            target_symbol_id=target.symbol_id,
            target_reference=None,
            relation_type="DECLARES",
            source_start_line=target.start_line,
            source_end_line=target.end_line,
            source_start_byte=target.start_byte,
            source_end_byte=target.end_byte,
            binding_name=None,
            binding_kind=None,
            binding_name_tokens_casefold=None,
            confidence=1.0,
            resolution_state="resolved",
            metadata_json="{}",
        ))
    return sort_relations(relations)


def _symbol_candidates(
    reference: ReferenceRecord,
    index: SymbolIndex,
    language: str,
) -> tuple[SymbolRecord, ...]:
    candidates = index.by_qualified.get(reference.target_reference or "", ())
    candidates = tuple(
        item for item in candidates
        if (reference.resolution_scope == "project"
            and item.file_id in index.module_backed_file_ids)
        or (reference.resolution_scope in {"file", "project"}
            and item.file_id == reference.source_file_id)
    )
    if reference.relation_type == "INHERITS":
        candidates = tuple(item for item in candidates if item.kind == "class")
    allowed = family_languages(language)
    if allowed is not None:
        candidates = tuple(
            item for item in candidates
            if index.languages_by_file_id.get(item.file_id, "") in allowed
            or item.file_id not in index.languages_by_file_id
        )
    return candidates


def _module_prefix_candidates(
    target: str,
    index: SymbolIndex,
    language: str,
) -> tuple[FileRecord, ...]:
    allowed = family_languages(language)
    names = tuple(
        name for name, files in index.modules_by_qualified.items()
        if (target == name or target.startswith(name + "."))
        and (allowed is None or any(item.language in allowed for item in files))
    )
    if not names:
        return ()
    longest = max(names, key=lambda name: (len(name), name))
    return tuple(
        item for item in index.modules_by_qualified[longest]
        if allowed is None or item.language in allowed
    )


def resolve_references(
    language: str,
    language_prefix: str,
    domain: str,
    references: Iterable[ReferenceRecord],
    index: SymbolIndex,
) -> tuple[RelationRecord, ...]:
    """Resolve only candidates already present in ``index``; never infer runtime values."""
    relations: list[RelationRecord] = []
    for reference in references:
        target = reference.target_reference
        if not target:
            continue
        force_unresolved = reference.resolution_hint == "unresolved"
        symbol_candidates = (
            () if force_unresolved else _symbol_candidates(reference, index, language)
        )
        allowed = family_languages(language)
        exact_modules = tuple(
            item for item in index.modules_by_qualified.get(target, ())
            if allowed is None or item.language in allowed
        ) if reference.relation_type == "IMPORTS" and not force_unresolved else ()
        module_only = reference.target_kind_hint == "module"
        if module_only:
            symbol_candidates = ()
        partial_modules = (
            () if (force_unresolved
                   or reference.resolution_scope != "project"
                   or symbol_candidates or exact_modules)
            else _module_prefix_candidates(target, index, language)
        )
        typed_targets: tuple[tuple[FileRecord | None, SymbolRecord | None], ...]
        if exact_modules and module_only:
            typed_targets = tuple((item, None) for item in exact_modules)
            state = "resolved" if len(typed_targets) == 1 else "ambiguous"
        elif symbol_candidates:
            typed_targets = tuple((None, item) for item in symbol_candidates)
            state = "resolved" if len(typed_targets) == 1 else "ambiguous"
        elif exact_modules:
            typed_targets = tuple((item, None) for item in exact_modules)
            state = "resolved" if len(typed_targets) == 1 else "ambiguous"
        elif partial_modules:
            typed_targets = tuple((item, None) for item in partial_modules)
            state = "partially_resolved"
        else:
            typed_targets = ((None, None),)
            state = "unresolved"
        for module_candidate, symbol_candidate in typed_targets:
            source_identity = (
                reference.source_symbol_id
                or reference.source_module_id
                or reference.source_file_id
            )
            source_line = reference.source_line or 1
            source_byte = reference.source_byte or 0
            source_end_line = reference.source_end_line or source_line
            source_end_byte = (
                reference.source_end_byte
                if reference.source_end_byte is not None else source_byte
            )
            target_entity_id = (
                symbol_candidate.symbol_id if symbol_candidate
                else module_candidate.module_id if module_candidate else None
            )
            retained_reference = target if state in {
                "partially_resolved", "unresolved"
            } else None
            relations.append(RelationRecord(
                relation_id=relation_id(
                    language,
                    language_prefix,
                    domain,
                    source_identity,
                    reference.relation_type,
                    source_line,
                    source_end_line,
                    source_byte,
                    source_end_byte,
                    target_entity_id,
                    retained_reference,
                    reference.binding_kind,
                    reference.binding_name,
                ),
                source_file_id=reference.source_file_id,
                source_module_id=reference.source_module_id,
                source_symbol_id=reference.source_symbol_id,
                target_module_id=(
                    module_candidate.module_id if module_candidate else None
                ),
                target_symbol_id=(
                    symbol_candidate.symbol_id if symbol_candidate else None
                ),
                target_reference=retained_reference,
                relation_type=reference.relation_type,
                source_start_line=source_line,
                source_end_line=source_end_line,
                source_start_byte=source_byte,
                source_end_byte=source_end_byte,
                binding_name=reference.binding_name,
                binding_kind=reference.binding_kind,
                binding_name_tokens_casefold=(
                    reference.binding_name_tokens_casefold
                ),
                confidence=0.0 if state == "unresolved" else 1.0,
                resolution_state=state,
                metadata_json="{}",
            ))
    return sort_relations(relations)
