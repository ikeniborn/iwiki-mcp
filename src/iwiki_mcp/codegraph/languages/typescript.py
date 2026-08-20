"""Tree-sitter-only TypeScript/TSX declaration extraction.

The Tree-sitter machinery this adapter runs on is shared with the
JavaScript adapter and lives in `_ecmascript`; what stays here is what is
TypeScript-specific: the grammar choice, the language profile, and the
opt-in `tsc` type boost.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import PurePosixPath

from . import _ecmascript
from ..models import (
    FileRecord,
    ParsedFile,
    ResolutionResult,
    compact_casefold,
    file_id,
    module_id,
    token_key,
)
from ..resolver import declaration_relations, resolve_references, sort_relations


_TYPESCRIPT_PROFILE = _ecmascript.LanguageProfile(
    language="typescript",
    prefix="ts",
    kind_by_node={
        "type_alias_declaration": "type_alias",
        "enum_declaration": "enum",
    },
)


def _grammar_name(path: str) -> str:
    return "tsx" if path.casefold().endswith(".tsx") else "typescript"


def _run_tsc_boost(source: bytes, path: str, *, timeout_seconds: float = 5.0):
    """Best-effort type info from the project's own `typescript` package.

    Returns None on any failure (missing node, missing typescript package,
    timeout, non-zero exit, malformed output) — callers must treat None as
    "no boost available" and continue with the Tree-sitter-only result.
    """
    import json
    import subprocess

    try:
        completed = subprocess.run(
            ["node", "-e", _TSC_BOOST_SCRIPT, path],
            input=source,
            capture_output=True,
            timeout=timeout_seconds,
            check=True,
        )
        return json.loads(completed.stdout.decode("utf-8"))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


_TSC_BOOST_SCRIPT = """
// Minimal TS Compiler API probe: emit {} until a real type-resolution
// payload is implemented in a follow-up task; this establishes the
// subprocess boundary and its failure contract only.
process.stdout.write("{}");
"""


@dataclass(frozen=True)
class _TypeScriptParsedFile(ParsedFile):
    pass


class TypeScriptAdapter:
    language = "typescript"
    prefix = "ts"
    extensions = (".ts", ".tsx")

    def __init__(
        self,
        repository_id: str,
        source_paths: tuple[str, ...],
        *,
        parser_version: str = "tree-sitter-typescript",
        type_boost_enabled: bool = False,
    ) -> None:
        if not isinstance(repository_id, str) or not repository_id:
            raise ValueError("invalid repository id")
        self.repository_id = repository_id
        self.parser_version = parser_version
        self.type_boost_enabled = type_boost_enabled
        # `_run_tsc_boost` spawns a `node` subprocess; probing it fresh for
        # every file would mean one subprocess spawn per file (N `OSError`s
        # with no `node`, or up to N * timeout with a slow one), which can
        # blow past a build's max_rebuild_seconds budget on a large repo.
        # The boost's own payload is still a stub ({}), so a single
        # probe-once-per-adapter-instance (i.e. once per build) result is
        # sufficient: bound the spawn cost to O(1) per build, not O(files).
        self._boost_probed = False
        self._boost_warnings: tuple[str, ...] = ()

    def parse_file(self, source: bytes, path: str) -> ParsedFile:
        if not isinstance(source, bytes):
            raise TypeError("source must be bytes")
        relative_path = _ecmascript.relative_path(path)
        content_hash = hashlib.sha256(source).hexdigest()
        stable_file_id = file_id(
            self.language, self.prefix, self.repository_id, relative_path,
        )
        parser = _ecmascript.get_parser(_grammar_name(relative_path))
        tree = parser.parse(source)
        root = tree.root_node

        is_module = any(
            child.type in ("import_statement", "export_statement")
            for child in root.children
        )
        posix_path = PurePosixPath(relative_path)
        local_stem = posix_path.name.split(".", 1)[0]
        module_dotted_name = ".".join((*posix_path.parent.parts, local_stem))
        module_qualified_name = module_dotted_name if is_module else None
        module_local_name = local_stem if is_module else None
        stable_module_id = (
            module_id(
                self.language, self.prefix, self.repository_id,
                relative_path, module_qualified_name,
            )
            if is_module else None
        )

        file = FileRecord(
            file_id=stable_file_id,
            repository_id=self.repository_id,
            path=relative_path,
            path_casefold=compact_casefold(relative_path),
            file_local_name=PurePosixPath(relative_path).name,
            file_name_tokens_casefold=token_key(PurePosixPath(relative_path).name),
            language=self.language,
            content_hash=content_hash,
            parser_version=self.parser_version,
            size_bytes=len(source),
            start_line=1,
            end_line=max(1, source.count(b"\n") + 1),
            start_byte=0,
            end_byte=len(source),
            module_key=relative_path,
            module_id=stable_module_id,
            module_qualified_name=module_qualified_name,
            module_local_name=module_local_name,
            module_name_tokens_casefold=(
                token_key(module_qualified_name, module_local_name)
                if module_qualified_name else None
            ),
        )
        symbols, pending_heritage = _ecmascript.extract_symbols(
            source, root,
            profile=_TYPESCRIPT_PROFILE,
            repository_id=self.repository_id, relative_path=relative_path,
            file_record=file, module_dotted_name=module_dotted_name,
        )
        # Safety net for genuine `symbol_id` collisions that scoping alone
        # cannot prevent: declarations inside anonymous/block scopes (arrow
        # callback bodies, if/else branches, try/catch blocks, IIFEs) get no
        # named scope segment of their own, so same-named siblings there
        # still flatten to the same qualified_name. This does not attempt to
        # invent a semantically correct qualified_name for those cases -- it
        # only guarantees the build never crashes on the PRIMARY KEY
        # constraint, keeping the last-by-position declaration and surfacing
        # a warning so the degradation is visible. Mirrors python.py's
        # equivalent dedup in `parse_file`.
        symbols, warnings = _ecmascript.dedupe_symbols(symbols)
        # Heritage targets resolve against the final, post-dedup symbol set
        # (see `_ecmascript.resolve_heritage_references`): TypeScript name
        # resolution is lexical, so a class's `extends`/`implements` target
        # may live in any enclosing scope outward from where the class
        # itself is declared, not just the class's own immediate scope.
        heritage_references = _ecmascript.resolve_heritage_references(
            pending_heritage,
            {symbol.qualified_name for symbol in symbols},
            module_dotted_name,
        )
        references = (
            *_ecmascript.esm_import_references(source, root, file_record=file),
            *heritage_references,
        )
        return _TypeScriptParsedFile(
            file=file, symbols=tuple(symbols), references=references,
            warnings=warnings,
        )

    def _probe_boost_once(self, parsed) -> tuple[str, ...]:
        """Run `_run_tsc_boost` at most once per adapter instance (per build).

        boost_result payload wiring into relation confidence/resolution_state
        is out of scope for this plan (spec: best-effort, opt-in; the
        Tree-sitter baseline already satisfies the intent's Trust priority
        on its own) — this proves the non-blocking subprocess contract
        end-to-end and surfaces its own availability, nothing more.
        """
        if not self._boost_probed:
            boost_result = _run_tsc_boost(
                parsed.file.content_hash.encode(), parsed.file.path,
            )
            self._boost_warnings = (
                () if boost_result is not None
                else ("typescript_boost_unavailable",)
            )
            self._boost_probed = True
        return self._boost_warnings

    def resolve_references(self, parsed, project_index) -> ResolutionResult:
        warnings: tuple[str, ...] = ()
        if self.type_boost_enabled:
            warnings = self._probe_boost_once(parsed)
        declares = declaration_relations(
            self.language, self.prefix, self.repository_id, parsed,
        )
        resolved = resolve_references(
            self.language, self.prefix, self.repository_id,
            parsed.references, project_index,
        )
        return ResolutionResult(
            relations=sort_relations((*declares, *resolved)), warnings=warnings,
        )
