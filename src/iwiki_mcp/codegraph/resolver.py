"""Language-neutral, conservative resolution of normalized references."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Mapping

from .models import ParsedFile, ReferenceRecord, RelationRecord, SymbolRecord, relation_id


@dataclass(frozen=True)
class SymbolIndex:
    """Project symbols indexed by normalized qualified and module-local names."""

    by_qualified: Mapping[str, tuple[SymbolRecord, ...]]
    by_module_local: Mapping[tuple[str, str], tuple[SymbolRecord, ...]]

    @classmethod
    def from_parsed_files(cls, parsed_files: Iterable[ParsedFile]) -> "SymbolIndex":
        return cls.from_symbols(
            symbol for parsed in parsed_files for symbol in parsed.symbols
        )

    @classmethod
    def from_symbols(cls, symbols: Iterable[SymbolRecord]) -> "SymbolIndex":
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
        )


def resolve_references(
    language: str, references: Iterable[ReferenceRecord], index: SymbolIndex
) -> tuple[RelationRecord, ...]:
    """Resolve only candidates already present in ``index``; never infer runtime values."""
    relations: list[RelationRecord] = []
    known_modules = {module for module, _local in index.by_module_local}
    for reference in references:
        target = reference.target_reference
        if not target:
            continue
        candidates = index.by_qualified.get(target, ())
        if reference.relation_type == "INHERITS":
            candidates = tuple(item for item in candidates if item.kind == "class")
        module, _separator, _local = target.rpartition(".")
        if not candidates and module:
            candidates = index.by_module_local.get((module, _local), ())
        if reference.relation_type == "INHERITS":
            candidates = tuple(item for item in candidates if item.kind == "class")
        module_target = reference.relation_type == "IMPORTS" and target in known_modules
        state = (
            "resolved" if len(candidates) == 1 or module_target else
            "ambiguous" if len(candidates) > 1 else
            "partially_resolved" if target in known_modules or module in known_modules
            else "unresolved"
        )
        targets = candidates or (None,)
        for candidate in targets:
            target_identity = candidate.symbol_id if candidate else target
            source_identity = reference.source_symbol_id or reference.source_file_id
            location = f"{reference.source_line or 0}:{reference.source_byte or 0}"
            relations.append(RelationRecord(
                relation_id=relation_id(language, source_identity, reference.relation_type,
                                        location, target_identity),
                source_symbol_id=reference.source_symbol_id,
                source_file_id=reference.source_file_id,
                target_symbol_id=candidate.symbol_id if candidate else None,
                target_reference=target,
                relation_type=reference.relation_type,
                source_line=reference.source_line,
                confidence=1.0 if state == "resolved" else 0.0,
                resolution_state=state,
                metadata_json="{}",
                source_byte=reference.source_byte,
            ))
    return tuple(sorted(relations, key=lambda item: (
        item.source_symbol_id or item.source_file_id, item.relation_type,
        item.source_line or -1, item.source_byte or -1,
        item.target_symbol_id or item.target_reference or "",
    )))
