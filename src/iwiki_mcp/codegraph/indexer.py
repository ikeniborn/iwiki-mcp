"""Deterministic full-build indexing and atomic code graph publication."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Callable, Iterable, Mapping, Protocol, Sequence

from filelock import Timeout

from iwiki_mcp import base as wiki_base

from . import models as codegraph_models
from .config import CodeGraphConfig
from .discovery import DiscoveryError, DiscoverySnapshot, discover_sources
from .fingerprint import (
    FingerprintSet,
    compose_fingerprints,
    git_commit as current_git_commit,
    git_dirty_marker,
)
from .languages.base import LanguageAdapter
from .location import CodeGraphPaths
from .models import (
    CodeGraphError,
    ParsedFile,
    RelationRecord,
    relation_id,
)
from .resolver import SymbolIndex, sort_relations
from .schema import SCHEMA_VERSION, TABLES, CodeGraphStoreError
from .store import (
    CodeGraphStore,
    _is_canonical_revision,
    _snapshot_revision,
    code_graph_read_lock,
    code_graph_write_lock,
)


_PHASE_NAMES = (
    "discovery",
    "fingerprint",
    "parsing",
    "normalization",
    "resolution",
    "persistence",
    "validation",
    "canonical_verification_1",
    "final_verification",
    "publication",
)
KNOWN_WARNING_CODES = frozenset({
    "duplicate_symbol_identity",
    "directory_changed",
    "directory_depth_limit",
    "directory_limit_reached",
    "directory_unavailable",
    "entry_excluded",
    "entry_limit_reached",
    "entry_unavailable",
    "file_changed",
    "file_limit_reached",
    "file_too_large",
    "file_unavailable",
    "ignored",
    "metadata_unavailable",
    "metadata_reconstructed",
    "metrics_incomplete",
    "module_name_unavailable",
    "parse_error",
    "secret_excluded",
    "symlink_excluded",
})
_FILE_EXCLUSION_CODES = frozenset({
    "entry_excluded",
    "file_changed",
    "file_limit_reached",
    "file_too_large",
    "file_unavailable",
    "ignored",
    "secret_excluded",
    "symlink_excluded",
})
_FILE_TRUNCATION_CODES = frozenset({"file_limit_reached"})
_READY_METADATA_KEYS = frozenset({
    "adapter_version",
    "counts",
    "domain",
    "duration_ms",
    "excluded_files",
    "fingerprints",
    "fresh",
    "generation",
    "git_commit",
    "grammar_version",
    "indexed_at",
    "input_fingerprint",
    "metadata_digest",
    "module_warnings",
    "normalizer_version",
    "parser_errors",
    "parser_version",
    "pending_final_verify",
    "phase_timings_ms",
    "publication_phase",
    "recovery_policy",
    "resolution_ratios",
    "resolver_version",
    "revision",
    "schema_version",
    "state",
    "storage_stamp",
    "truncated",
    "truncated_files",
    "unicode_data_version",
    "warnings",
})

_DATABASE_STAMP_KEYS = frozenset({
    "change_counter",
    "ctime_ns",
    "device",
    "inode",
    "mtime_ns",
    "page_count",
    "schema_cookie",
    "size",
    "version_valid_for",
})
_WAL_STAMP_KEYS = frozenset({
    "ctime_ns",
    "device",
    "frames",
    "inode",
    "magic",
    "mtime_ns",
    "page_size",
    "salt_1",
    "salt_2",
    "size",
    "version",
})


class CodeGraphBuildError(CodeGraphError):
    """Raised when a full graph snapshot cannot be built safely."""

    code = "rebuild_failed"


class CodeGraphParseError(CodeGraphError):
    """Raised when a configured language adapter cannot parse safely."""

    code = "parse_failed"


class CodeGraphStoreFailure(CodeGraphError):
    """Raised when snapshot storage fails before safe publication."""

    code = "store_failed"


class CodeGraphStaleError(CodeGraphError):
    """Raised when persisted inputs no longer match project sources."""

    code = "stale"


class CodeGraphUnsafePathError(CodeGraphError):
    """Raised when discovery rejects an unsafe project path."""

    code = "unsafe_path"


class BuildControl:
    """Linearize caller cancellation against atomic publication entry."""

    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self.publication_entered = threading.Event()
        self._publication_gate = threading.Lock()

    def cancel(self) -> None:
        if self._publication_gate.acquire(blocking=False):
            try:
                self.cancelled.set()
            finally:
                self._publication_gate.release()
        else:
            self.cancelled.set()

    def enter_publication(
        self,
        *,
        deadline: float,
        lock_path: Path,
    ) -> None:
        self._publication_gate.acquire()
        if self.cancelled.is_set() or time.monotonic() >= deadline:
            self._publication_gate.release()
            raise Timeout(str(lock_path))
        self.publication_entered.set()

    def leave_publication(self) -> None:
        if self.publication_entered.is_set():
            self._publication_gate.release()


@dataclass(frozen=True)
class AdapterBinding:
    """Composition-root supplied adapter and deterministic version inputs."""

    adapter: LanguageAdapter
    parser_version: str
    grammar_version: str
    adapter_version: str


@dataclass(frozen=True)
class AdapterFactory:
    """Lazy composition input for one language adapter implementation."""

    create: Callable[[tuple[str, ...]], LanguageAdapter]
    extensions: tuple[str, ...]
    parser_version: str
    grammar_version: str
    adapter_version: str

    def bind(
        self,
        source_paths: tuple[str, ...] = (),
    ) -> AdapterBinding:
        return AdapterBinding(
            adapter=self.create(source_paths),
            parser_version=self.parser_version,
            grammar_version=self.grammar_version,
            adapter_version=self.adapter_version,
        )


class WikiSelectorResolver(Protocol):
    """Task 9 extension seam for deterministic Wiki selector resolution."""

    def resolve(
        self,
        *,
        domain: str,
        project_dir: str,
        parsed_files: tuple[ParsedFile, ...],
        relations: tuple[RelationRecord, ...],
    ) -> Sequence[Mapping[str, object]]:
        """Return normalized wiki_code_links rows for final persistence."""


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))


def _check_deadline(deadline: float | None, lock_path: Path) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise Timeout(str(lock_path))


def _lock_timeout(maximum: float, deadline: float | None) -> float:
    if deadline is None:
        return maximum
    return max(0.0, min(maximum, deadline - time.monotonic()))


def _metadata_digest(metadata: Mapping[str, object]) -> str:
    payload = {
        key: value for key, value in metadata.items()
        if key not in {"duration_ms", "metadata_digest", "phase_timings_ms"}
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _seal_ready_metadata(metadata: dict[str, object]) -> None:
    metadata["metadata_digest"] = _metadata_digest(metadata)


def _valid_storage_stamp(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"database", "wal"}:
        return False
    database = value.get("database")
    if (
        not isinstance(database, Mapping)
        or set(database) != _DATABASE_STAMP_KEYS
        or any(type(item) is not int or item < 0 for item in database.values())
    ):
        return False
    wal = value.get("wal")
    if wal is None:
        return True
    if not isinstance(wal, Mapping) or set(wal) != _WAL_STAMP_KEYS:
        return False
    if any(
        key not in {"magic", "version", "page_size", "salt_1", "salt_2"}
        and (type(item) is not int or item < 0)
        for key, item in wal.items()
    ):
        return False
    optional = tuple(
        wal[key] for key in ("magic", "version", "page_size", "salt_1", "salt_2")
    )
    return all(item is None for item in optional) or all(
        type(item) is int and item >= 0 for item in optional
    )


def exact_ready_metadata(metadata: Mapping[str, object]) -> bool:
    """Validate the complete final metadata envelope and its diagnostics."""
    timings = _safe_phase_timings(metadata.get("phase_timings_ms"))
    warnings = metadata.get("warnings")
    return (
        set(metadata) == _READY_METADATA_KEYS
        and metadata.get("state") == "ready"
        and metadata.get("fresh") is True
        and metadata.get("publication_phase") == "pending_final_verify"
        and metadata.get("pending_final_verify") is True
        and metadata.get("recovery_policy") == "failed"
        and _valid_storage_stamp(metadata.get("storage_stamp"))
        and type(metadata.get("generation")) is int
        and metadata["generation"] >= 0
        and isinstance(metadata.get("counts"), Mapping)
        and isinstance(metadata.get("resolution_ratios"), Mapping)
        and type(metadata.get("excluded_files")) is int
        and metadata["excluded_files"] >= 0
        and type(metadata.get("truncated_files")) is int
        and metadata["truncated_files"] >= 0
        and type(metadata.get("truncated")) is bool
        and type(metadata.get("parser_errors")) is int
        and metadata["parser_errors"] >= 0
        and type(metadata.get("module_warnings")) is int
        and metadata["module_warnings"] >= 0
        and isinstance(warnings, list)
        and warnings == sanitize_warning_codes(warnings)
        and set(timings) == set(_PHASE_NAMES)
        and type(metadata.get("duration_ms")) is int
        and metadata["duration_ms"] >= 0
        and metadata.get("metadata_digest") == _metadata_digest(metadata)
    )


def _safe_metadata(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _safe_phase_timings(value: object) -> dict[str, int]:
    mapping = value if isinstance(value, Mapping) else {}
    return {
        phase: timing
        for phase in _PHASE_NAMES
        if type(timing := mapping.get(phase)) is int and timing >= 0
    }


def sanitize_warning_codes(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({
        item for item in value
        if isinstance(item, str) and item in KNOWN_WARNING_CODES
    })


class CodeGraphIndexer:
    """Build one immutable snapshot for a request-scoped primary domain."""

    def __init__(
        self,
        *,
        cache_base: str,
        project_dir: str,
        domain: str,
        config: CodeGraphConfig,
        paths: CodeGraphPaths,
        adapter_factories: Mapping[str, AdapterFactory],
        resolver_version: str,
        wiki_selector_resolver: WikiSelectorResolver,
    ) -> None:
        self.cache_base = cache_base
        self.project_dir = project_dir
        self.domain = domain
        self.config = config
        self.paths = paths
        self.adapter_factories = dict(adapter_factories)
        self.resolver_version = resolver_version
        self.wiki_selector_resolver = wiki_selector_resolver
        self.store = CodeGraphStore(
            paths.database,
            cache_base=cache_base,
        )

    def _config_for_languages(
        self,
        config: CodeGraphConfig,
        languages: list[str] | None,
    ) -> CodeGraphConfig:
        if languages is None:
            return config
        if not languages or any(
            item not in self.adapter_factories for item in languages
        ):
            raise CodeGraphBuildError("invalid code graph languages")
        return replace(config, languages=tuple(languages))

    def _factories(
        self, config: CodeGraphConfig
    ) -> tuple[tuple[str, AdapterFactory], ...]:
        try:
            return tuple(
                (language, self.adapter_factories[language])
                for language in config.languages
            )
        except KeyError as exc:
            raise CodeGraphBuildError(
                "invalid code graph languages"
            ) from exc

    def _parser_version(self, config: CodeGraphConfig) -> str:
        return ";".join(
            f"{language}:{factory.parser_version}"
            for language, factory in self._factories(config)
        )

    def _grammar_version(self, config: CodeGraphConfig) -> str:
        return ";".join(
            f"{language}:{factory.grammar_version}"
            for language, factory in self._factories(config)
        )

    def _adapter_version(self, config: CodeGraphConfig) -> str:
        return ";".join(
            f"{language}:{factory.adapter_version}"
            for language, factory in self._factories(config)
        )

    def _extensions(self, config: CodeGraphConfig) -> tuple[str, ...]:
        return tuple(sorted({
            extension
            for _language, factory in self._factories(config)
            for extension in factory.extensions
        }))

    @staticmethod
    def _normalization_versions() -> tuple[str, str]:
        return (
            codegraph_models.NORMALIZER_VERSION,
            codegraph_models.UNICODE_DATA_VERSION,
        )

    def _fingerprints(
        self,
        discovered: DiscoverySnapshot,
        config: CodeGraphConfig,
        *,
        normalization_versions: tuple[str, str],
    ) -> tuple[FingerprintSet, str | None, str]:
        commit = current_git_commit(self.project_dir)
        dirty = git_dirty_marker(self.project_dir)
        fingerprints = compose_fingerprints(
            discovered.files,
            config,
            repository_id=self.domain,
            git_commit=commit,
            dirty_marker=dirty,
            schema_version=SCHEMA_VERSION,
            parser_version=self._parser_version(config),
            grammar_version=self._grammar_version(config),
            adapter_version=self._adapter_version(config),
            resolver_version=self.resolver_version,
            normalizer_version=normalization_versions[0],
            unicode_data_version=normalization_versions[1],
        )
        return fingerprints, commit, dirty

    def _quarantine_unusable_canonical(self) -> None:
        if not self.paths.database.exists():
            return
        if self.store.inspect_compatibility() == "compatible":
            return
        if not self.store.canonical_handles_available():
            raise Timeout(str(self.paths.lock))
        self.store.quarantine_corrupt()

    def _ready_metadata(
        self,
        fingerprints: FingerprintSet,
        commit: str | None,
        config: CodeGraphConfig,
        normalization_versions: tuple[str, str],
        *,
        persisted: Mapping[str, object] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, object] | None:
        if connection is None:
            try:
                with self.store.read_lease() as leased:
                    return self._ready_metadata(
                        fingerprints,
                        commit,
                        config,
                        normalization_versions,
                        persisted=persisted,
                        connection=leased,
                    )
            except (CodeGraphStoreError, OSError):
                return None
        metadata = dict(
            persisted
            if persisted is not None
            else (_safe_metadata(self.paths.metadata) or {})
        )
        try:
            data_version = connection.execute(
                "PRAGMA data_version"
            ).fetchone()
            row = connection.execute(
                "SELECT source_fingerprint, config_fingerprint, "
                "parser_fingerprint, normalizer_version, "
                "unicode_data_version, git_commit, revision, state, "
                "indexed_at "
                "FROM repositories WHERE repository_id = ?",
                (self.domain,),
            ).fetchone()
            storage_stamp = self.store.storage_stamp()
            row_after = connection.execute(
                "SELECT source_fingerprint, config_fingerprint, "
                "parser_fingerprint, normalizer_version, "
                "unicode_data_version, git_commit, revision, state, "
                "indexed_at "
                "FROM repositories WHERE repository_id = ?",
                (self.domain,),
            ).fetchone()
            data_version_after = connection.execute(
                "PRAGMA data_version"
            ).fetchone()
        except (CodeGraphStoreError, OSError, sqlite3.DatabaseError):
            return None
        if row != row_after or data_version != data_version_after:
            return None
        expected = (
            fingerprints.source,
            fingerprints.config,
            fingerprints.parser,
            normalization_versions[0],
            normalization_versions[1],
            commit,
            row[6] if row is not None else None,
            "ready",
        )
        if row is None or row[:8] != expected:
            return None
        metadata_timings = _safe_phase_timings(
            metadata.get("phase_timings_ms")
        )
        metadata_duration = metadata.get("duration_ms")
        metadata_fingerprints = metadata.get("fingerprints")
        metadata_matches = (
            exact_ready_metadata(metadata)
            and metadata.get("domain") == self.domain
            and metadata.get("state") == "ready"
            and metadata.get("revision") == row[6]
            and metadata.get("schema_version") == SCHEMA_VERSION
            and metadata_fingerprints == {
                "source": fingerprints.source,
                "config": fingerprints.config,
                "parser": fingerprints.parser,
            }
            and metadata.get("input_fingerprint") == fingerprints.inputs
            and metadata.get("git_commit") == commit
            and metadata.get("indexed_at") == row[8]
            and metadata.get("parser_version") == self._parser_version(config)
            and metadata.get("grammar_version") == self._grammar_version(config)
            and metadata.get("adapter_version") == self._adapter_version(config)
            and metadata.get("resolver_version") == self.resolver_version
            and metadata.get("publication_phase") == "pending_final_verify"
            and metadata.get("pending_final_verify") is True
            and metadata.get("normalizer_version") == normalization_versions[0]
            and metadata.get("unicode_data_version") == normalization_versions[1]
            and metadata.get("storage_stamp") == storage_stamp
        )
        if not metadata_matches:
            return None
        if not _is_canonical_revision(row[6]):
            return None
        counts = dict(metadata["counts"])
        resolution_ratios = dict(metadata["resolution_ratios"])
        excluded_files = metadata.get("excluded_files")
        parser_errors = metadata.get("parser_errors")
        module_warnings = metadata.get("module_warnings")
        result = {
            "domain": self.domain,
            "state": "ready",
            "revision": row[6],
            "fresh": True,
            "git_commit": commit,
            "fingerprints": {
                "source": fingerprints.source,
                "config": fingerprints.config,
                "parser": fingerprints.parser,
            },
            "input_fingerprint": fingerprints.inputs,
            "schema_version": SCHEMA_VERSION,
            "parser_version": self._parser_version(config),
            "grammar_version": self._grammar_version(config),
            "adapter_version": self._adapter_version(config),
            "resolver_version": self.resolver_version,
            "normalizer_version": normalization_versions[0],
            "unicode_data_version": normalization_versions[1],
            "counts": counts,
            "resolution_ratios": resolution_ratios,
            "excluded_files": (
                excluded_files
                if type(excluded_files) is int and excluded_files >= 0
                else 0
            ),
            "truncated": (
                metadata.get("truncated")
                if type(metadata.get("truncated")) is bool
                else False
            ),
            "truncated_files": metadata["truncated_files"],
            "parser_errors": (
                parser_errors
                if type(parser_errors) is int and parser_errors >= 0
                else 0
            ),
            "module_warnings": (
                module_warnings
                if type(module_warnings) is int and module_warnings >= 0
                else max(0, counts["files"] - counts["modules"])
            ),
            "pending_final_verify": True,
            "indexed_at": row[8],
            "warnings": (
                sanitize_warning_codes(metadata.get("warnings"))
                if metadata_matches
                else ["metadata_reconstructed", "metrics_incomplete"]
            ),
        }
        if metadata_matches:
            result["phase_timings_ms"] = metadata_timings
            result["duration_ms"] = metadata_duration
        return result

    def _parse(
        self,
        discovered: DiscoverySnapshot,
        config: CodeGraphConfig,
    ) -> tuple[
        tuple[ParsedFile, ...],
        tuple[str, ...],
        Mapping[str, AdapterBinding],
    ]:
        adapters = {}
        for language, factory in self._factories(config):
            source_paths = tuple(sorted(
                source.path
                for source in discovered.files
                if any(
                    source.path.casefold().endswith(extension.casefold())
                    for extension in factory.extensions
                )
            ))
            adapters[language] = factory.bind(source_paths)
        parsed_files = []
        warnings = []
        for source in discovered.files:
            binding = next(
                (
                    candidate
                    for candidate in adapters.values()
                    if any(
                        source.path.casefold().endswith(extension.casefold())
                        for extension in candidate.adapter.extensions
                    )
                ),
                None,
            )
            if binding is None:
                raise CodeGraphParseError("source adapter is unavailable")
            adapter = binding.adapter
            try:
                parsed = adapter.parse_file(source.content, source.path)
            except Exception as exc:
                raise CodeGraphParseError("code graph parse failed") from exc
            expected = (
                self.domain,
                source.path,
                source.content_hash,
                source.size_bytes,
                binding.parser_version,
                adapter.language,
            )
            actual = (
                parsed.file.repository_id,
                parsed.file.path,
                parsed.file.content_hash,
                parsed.file.size_bytes,
                parsed.file.parser_version,
                parsed.file.language,
            )
            if actual != expected:
                raise CodeGraphParseError("adapter record context mismatch")
            parsed_files.append(parsed)
            warnings.extend(parsed.warnings)
        return tuple(parsed_files), tuple(warnings), adapters

    def _resolve(self, parsed_files, adapters):
        index = SymbolIndex.from_parsed_files(parsed_files)
        relations = []
        warnings = []
        for parsed in parsed_files:
            try:
                adapter = adapters[parsed.file.language].adapter
                result = adapter.resolve_references(parsed, index)
            except KeyError as exc:
                raise CodeGraphBuildError(
                    "source adapter is unavailable"
                ) from exc
            relations.extend(result.relations)
            warnings.extend(result.warnings)
        return tuple(sorted(relations, key=lambda item: item.relation_id)), tuple(warnings)

    @staticmethod
    def _relation_rows(
        relations: Iterable[RelationRecord],
    ) -> tuple[dict[str, object], ...]:
        rows = []
        for relation in relations:
            rows.append(asdict(relation))
        return tuple(sorted(rows, key=lambda row: str(row["relation_id"])))

    def _unresolved_relations(
        self,
        parsed_files: tuple[ParsedFile, ...],
        adapters: Mapping[str, AdapterBinding],
    ) -> tuple[RelationRecord, ...]:
        relations = []
        for parsed in parsed_files:
            for reference in parsed.references:
                source_identity = (
                    reference.source_symbol_id
                    or reference.source_module_id
                    or reference.source_file_id
                )
                target = reference.target_reference
                if not target:
                    continue
                adapter = adapters[parsed.file.language].adapter
                source_line = reference.source_line or 1
                source_byte = reference.source_byte or 0
                source_end_line = reference.source_end_line or source_line
                source_end_byte = (
                    reference.source_end_byte
                    if reference.source_end_byte is not None else source_byte
                )
                relations.append(RelationRecord(
                    relation_id=relation_id(
                        parsed.file.language,
                        adapter.prefix,
                        self.domain,
                        source_identity,
                        reference.relation_type,
                        source_line,
                        source_end_line,
                        source_byte,
                        source_end_byte,
                        None,
                        target,
                        reference.binding_kind,
                        reference.binding_name,
                    ),
                    source_file_id=reference.source_file_id,
                    source_module_id=reference.source_module_id,
                    source_symbol_id=reference.source_symbol_id,
                    target_module_id=None,
                    target_symbol_id=None,
                    target_reference=target,
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
                    confidence=0.0,
                    resolution_state="unresolved",
                    metadata_json="{}",
                ))
        return sort_relations(relations)

    def _initial_snapshot(
        self,
        *,
        parsed_files: tuple[ParsedFile, ...],
        unresolved_relations: tuple[RelationRecord, ...],
        fingerprints: FingerprintSet,
        commit: str | None,
        indexed_at: str,
        normalization_versions: tuple[str, str],
    ) -> dict[str, tuple[dict[str, object], ...]]:
        files = tuple(asdict(item.file) for item in parsed_files)
        symbols = tuple(
            asdict(symbol)
            for parsed in parsed_files
            for symbol in parsed.symbols
        )
        snapshot = {
            "repositories": (
                {
                    "repository_id": self.domain,
                    "root_path": str(Path(self.project_dir).resolve()),
                    "git_remote": None,
                    "git_commit": commit,
                    "source_fingerprint": fingerprints.source,
                    "config_fingerprint": fingerprints.config,
                    "parser_fingerprint": fingerprints.parser,
                    "normalizer_version": normalization_versions[0],
                    "unicode_data_version": normalization_versions[1],
                    "revision": fingerprints.inputs,
                    "state": "rebuilding",
                    "indexed_at": indexed_at,
                },
            ),
            "files": files,
            "symbols": symbols,
            "relations": self._relation_rows(unresolved_relations),
            "wiki_code_links": (),
        }
        if set(snapshot) != set(TABLES):
            raise CodeGraphBuildError("invalid code graph snapshot")
        return snapshot

    @staticmethod
    def _revision(
        *,
        repository: Mapping[str, object],
        parsed_files: tuple[ParsedFile, ...],
        relations: tuple[RelationRecord, ...],
        wiki_code_links: Sequence[Mapping[str, object]],
    ) -> str:
        files = tuple(asdict(item.file) for item in parsed_files)
        symbols = tuple(
            asdict(symbol)
            for parsed in parsed_files
            for symbol in parsed.symbols
        )
        return _snapshot_revision(
            repository,
            files,
            symbols,
            CodeGraphIndexer._relation_rows(relations),
            wiki_code_links,
        )

    @staticmethod
    def _counts(
        parsed_files: tuple[ParsedFile, ...], relations: Iterable[object]
    ) -> dict[str, object]:
        relation_rows = tuple(relations)
        symbols = tuple(
            symbol for parsed in parsed_files for symbol in parsed.symbols
        )
        symbol_kinds = dict(sorted(Counter(
            item.kind for item in symbols
        ).items()))
        modules = sum(item.file.module_id is not None for item in parsed_files)
        return {
            "languages": dict(sorted(Counter(
                item.file.language for item in parsed_files
            ).items())),
            "files": len(parsed_files),
            "modules": modules,
            "symbols": len(symbols),
            "relations": len(relation_rows),
            "entity_kinds": {
                "file": len(parsed_files),
                "module": modules,
                **symbol_kinds,
            },
            "symbol_kinds": symbol_kinds,
            "relation_types": dict(sorted(Counter(
                item.relation_type for item in relation_rows
            ).items())),
            "resolution_states": dict(sorted(Counter(
                item.resolution_state for item in relation_rows
            ).items())),
        }

    def _metadata(
        self,
        *,
        revision: str,
        fingerprints: FingerprintSet,
        commit: str | None,
        counts: Mapping[str, object],
        indexed_at: str,
        discovered: DiscoverySnapshot,
        parser_warnings: tuple[str, ...],
        resolver_warnings: tuple[str, ...],
        timings: Mapping[str, int],
        config: CodeGraphConfig,
        duration_ms: int,
        normalization_versions: tuple[str, str],
    ) -> dict[str, object]:
        warnings = sanitize_warning_codes(sorted(
            {item.code for item in discovered.warnings}
            | set(parser_warnings)
            | set(resolver_warnings)
        ))
        resolution_states = counts.get("resolution_states", {})
        if not isinstance(resolution_states, Mapping):
            resolution_states = {}
        resolution_total = sum(
            value for value in resolution_states.values()
            if isinstance(value, int) and value >= 0
        )
        resolution_ratios = {
            state: count / resolution_total
            for state, count in resolution_states.items()
            if resolution_total and isinstance(count, int) and count >= 0
        }
        return {
            "domain": self.domain,
            "state": "ready",
            "revision": revision,
            "fresh": True,
            "git_commit": commit,
            "fingerprints": {
                "source": fingerprints.source,
                "config": fingerprints.config,
                "parser": fingerprints.parser,
            },
            "input_fingerprint": fingerprints.inputs,
            "schema_version": SCHEMA_VERSION,
            "parser_version": self._parser_version(config),
            "grammar_version": self._grammar_version(config),
            "adapter_version": self._adapter_version(config),
            "resolver_version": self.resolver_version,
            "normalizer_version": normalization_versions[0],
            "unicode_data_version": normalization_versions[1],
            "counts": dict(counts),
            "resolution_ratios": resolution_ratios,
            "excluded_files": sum(
                warning.code in _FILE_EXCLUSION_CODES
                for warning in discovered.warnings
            ),
            "truncated_files": sum(
                warning.code in _FILE_TRUNCATION_CODES
                for warning in discovered.warnings
            ),
            "truncated": discovered.truncated,
            "parser_errors": parser_warnings.count("parse_error"),
            "module_warnings": parser_warnings.count(
                "module_name_unavailable"
            ),
            "indexed_at": indexed_at,
            "phase_timings_ms": dict(timings),
            "duration_ms": duration_ms,
            "warnings": warnings,
        }

    @staticmethod
    def _generation(metadata: Mapping[str, object]) -> int:
        value = metadata.get("generation")
        return value if type(value) is int and value >= 0 else 0

    def _transition_metadata(
        self,
        *,
        state: str,
        generation: int,
        revision: str | None,
        prior_state: str | None = None,
        publication_phase: str | None = None,
        recovery_policy: str | None = None,
    ) -> dict[str, object]:
        metadata = {
            "domain": self.domain,
            "state": state,
            "generation": generation,
            "revision": revision,
            "fresh": False,
            "schema_version": SCHEMA_VERSION,
            "warnings": ["metrics_incomplete"],
        }
        if prior_state is not None:
            metadata["prior_state"] = prior_state
            metadata["previous_revision"] = revision
        if publication_phase is not None:
            metadata["publication_phase"] = publication_phase
        if recovery_policy is not None:
            metadata["recovery_policy"] = recovery_policy
        return metadata

    def _publish_metadata_record(
        self,
        metadata: Mapping[str, object],
        *,
        deadline: float | None,
    ) -> None:
        staging = self.store.prepare_metadata(self.paths.metadata, metadata)
        try:
            _check_deadline(deadline, self.paths.lock)
            self.store.publish_metadata(
                self.paths.metadata,
                staging,
                deadline=deadline,
            )
        except CodeGraphStoreError:
            try:
                self.store.discard_metadata(staging)
            except CodeGraphError:
                pass
            _check_deadline(deadline, self.paths.lock)
            raise
        except BaseException:
            try:
                self.store.discard_metadata(staging)
            except CodeGraphError:
                pass
            raise

    def _verify_published(self, revision: str) -> None:
        self.store.verify_canonical(self.domain, revision)

    def mark_dirty_if_stale(self, *, deadline: float | None = None) -> bool:
        """Discover fingerprints for a query and persist ready-to-dirty drift."""
        config = self.config
        normalization_versions = self._normalization_versions()
        if deadline is None:
            deadline = time.monotonic() + config.max_rebuild_seconds

        def ready_now() -> bool:
            _check_deadline(deadline, self.paths.lock)
            discovered = discover_sources(
                self.project_dir,
                config,
                extensions=self._extensions(config),
            )
            _check_deadline(deadline, self.paths.lock)
            fingerprints, commit, _dirty = self._fingerprints(
                discovered,
                config,
                normalization_versions=normalization_versions,
            )
            _check_deadline(deadline, self.paths.lock)
            return self._ready_metadata(
                fingerprints,
                commit,
                config,
                normalization_versions,
            ) is not None

        try:
            with code_graph_read_lock(self.paths.lock):
                if ready_now():
                    return False
            with code_graph_write_lock(
                self.paths.lock,
                timeout=_lock_timeout(config.max_rebuild_seconds, deadline),
            ):
                if ready_now():
                    return False
                self.store.set_repository_state(self.domain, "dirty")
                return True
        except DiscoveryError as exc:
            raise CodeGraphUnsafePathError(
                "unsafe code graph source path"
            ) from exc
        except CodeGraphStoreError as exc:
            raise CodeGraphStoreFailure("code graph store failed") from exc

    def build(
        self,
        *,
        force: bool = False,
        languages: list[str] | None = None,
        deadline: float | None = None,
        restore_prior_on_abort: bool = False,
        control: BuildControl | None = None,
    ) -> dict[str, object]:
        """Run the bounded eighteen-step full-build/publication lifecycle."""
        started = time.monotonic()
        staging = None
        generation = None
        previous_revision = None
        prior_state = None
        publication_entered = False
        timings = {phase: 0 for phase in _PHASE_NAMES}
        try:
            config = self._config_for_languages(self.config, languages)
            normalization_versions = self._normalization_versions()
            if deadline is None:
                deadline = started + config.max_rebuild_seconds
            _check_deadline(deadline, self.paths.lock)
            with code_graph_write_lock(
                self.paths.lock,
                timeout=_lock_timeout(config.max_rebuild_seconds, deadline),
            ):
                _check_deadline(deadline, self.paths.lock)
                previous_metadata = _safe_metadata(self.paths.metadata) or {}
                generation = self._generation(previous_metadata) + 1
                try:
                    with self.store.read_lease() as connection:
                        row = connection.execute(
                            "SELECT revision, state FROM repositories "
                            "WHERE repository_id = ?",
                            (self.domain,),
                        ).fetchone()
                    authoritative = (
                        {"revision": row[0], "state": row[1]}
                        if row is not None
                        else {}
                    )
                except CodeGraphStoreError:
                    authoritative = {}
                revision_value = authoritative.get("revision")
                previous_revision = (
                    revision_value if isinstance(revision_value, str) else None
                )
                state_value = authoritative.get("state")
                prior_state = (
                    state_value
                    if state_value in {"missing", "ready", "dirty", "failed"}
                    else "missing"
                )
                wiki_base.ensure_graph_store_excluded(self.cache_base)
                _check_deadline(deadline, self.paths.lock)
                self._quarantine_unusable_canonical()
                _check_deadline(deadline, self.paths.lock)
                phase = time.monotonic()
                discovered = discover_sources(
                    self.project_dir,
                    config,
                    extensions=self._extensions(config),
                )
                timings["discovery"] = _elapsed_ms(phase)
                _check_deadline(deadline, self.paths.lock)

                phase = time.monotonic()
                fingerprints, commit, _dirty = self._fingerprints(
                    discovered,
                    config,
                    normalization_versions=normalization_versions,
                )
                timings["fingerprint"] = _elapsed_ms(phase)
                _check_deadline(deadline, self.paths.lock)
                if not force:
                    try:
                        with self.store.read_lease() as connection:
                            ready = self._ready_metadata(
                                fingerprints,
                                commit,
                                config,
                                normalization_versions,
                                persisted=previous_metadata,
                                connection=connection,
                            )
                            final_ready = self._ready_metadata(
                                fingerprints,
                                commit,
                                config,
                                normalization_versions,
                                connection=connection,
                            ) if ready is not None else None
                            if final_ready is not None:
                                result = dict(final_ready)
                                result["no_op"] = True
                                result["duration_ms"] = _elapsed_ms(started)
                                result["phase_timings_ms"] = {
                                    "no_op": result["duration_ms"]
                                }
                                return result
                    except CodeGraphStoreError:
                        pass

                self._publish_metadata_record(
                    self._transition_metadata(
                        state="rebuilding",
                        generation=generation,
                        revision=previous_revision,
                        prior_state=prior_state,
                        publication_phase="building",
                        recovery_policy=(
                            "restore_prior"
                            if restore_prior_on_abort
                            else "failed"
                        ),
                    ),
                    deadline=deadline,
                )
                _check_deadline(deadline, self.paths.lock)

                staging = self.store.create_staging_path()
                _check_deadline(deadline, self.paths.lock)
                phase = time.monotonic()
                parsed_files, parser_warnings, adapters = self._parse(
                    discovered,
                    config,
                )
                timings["parsing"] = _elapsed_ms(phase)
                normalization_started = time.monotonic()
                parsed_files = tuple(parsed_files)
                timings["normalization"] = _elapsed_ms(
                    normalization_started
                )
                _check_deadline(deadline, self.paths.lock)

                indexed_at = datetime.now(timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                )
                unresolved_relations = self._unresolved_relations(
                    parsed_files,
                    adapters,
                )
                snapshot = self._initial_snapshot(
                    parsed_files=parsed_files,
                    unresolved_relations=unresolved_relations,
                    fingerprints=fingerprints,
                    commit=commit,
                    indexed_at=indexed_at,
                    normalization_versions=normalization_versions,
                )
                phase = time.monotonic()
                staging_store = CodeGraphStore(
                    staging,
                    cache_base=self.cache_base,
                )
                staging_store.insert_snapshot(snapshot)
                timings["persistence"] = _elapsed_ms(phase)
                _check_deadline(deadline, self.paths.lock)

                phase = time.monotonic()
                relations, resolver_warnings = self._resolve(
                    parsed_files,
                    adapters,
                )
                persistence_phase = time.monotonic()
                staging_store.replace_relations(self._relation_rows(relations))
                timings["persistence"] += _elapsed_ms(persistence_phase)
                _check_deadline(deadline, self.paths.lock)
                wiki_code_links = tuple(self.wiki_selector_resolver.resolve(
                    domain=self.domain,
                    project_dir=self.project_dir,
                    parsed_files=parsed_files,
                    relations=relations,
                ))
                _check_deadline(deadline, self.paths.lock)
                revision = self._revision(
                    repository=snapshot["repositories"][0],
                    parsed_files=parsed_files,
                    relations=relations,
                    wiki_code_links=wiki_code_links,
                )
                persistence_phase = time.monotonic()
                staging_store.finalize_snapshot(
                    repository_id=self.domain,
                    revision=revision,
                    indexed_at=indexed_at,
                    wiki_code_links=wiki_code_links,
                )
                timings["persistence"] += _elapsed_ms(persistence_phase)
                timings["resolution"] = _elapsed_ms(phase)
                counts = self._counts(parsed_files, relations)
                _check_deadline(deadline, self.paths.lock)

                phase = time.monotonic()
                self.store.prepare_staging(
                    staging,
                    repository_id=self.domain,
                    expected_revision=revision,
                )
                timings["validation"] = _elapsed_ms(phase)
                _check_deadline(deadline, self.paths.lock)

                phase = time.monotonic()
                while not self.store.canonical_handles_available():
                    if time.monotonic() >= deadline:
                        raise Timeout(str(self.paths.lock))
                    time.sleep(0.01)
                _check_deadline(deadline, self.paths.lock)
                if control is not None:
                    assert deadline is not None
                    control.enter_publication(
                        deadline=deadline,
                        lock_path=self.paths.lock,
                    )
                    publication_entered = True
                    deadline = None
                try:
                    self.store.replace_staging(staging)
                except CodeGraphStoreError as exc:
                    raise CodeGraphBuildError(
                        "code graph publication failed"
                    ) from exc
                staging = None
                _check_deadline(deadline, self.paths.lock)

                incomplete_metadata = self._metadata(
                    revision=revision,
                    fingerprints=fingerprints,
                    commit=commit,
                    counts=counts,
                    indexed_at=indexed_at,
                    discovered=discovered,
                    parser_warnings=parser_warnings,
                    resolver_warnings=resolver_warnings,
                    timings=timings,
                    config=config,
                    duration_ms=_elapsed_ms(started),
                    normalization_versions=normalization_versions,
                )
                incomplete_metadata["state"] = "rebuilding"
                incomplete_metadata["generation"] = generation
                incomplete_metadata["fresh"] = False
                incomplete_metadata["prior_state"] = prior_state
                incomplete_metadata["previous_revision"] = previous_revision
                incomplete_metadata["publication_phase"] = "provisional"
                incomplete_metadata["recovery_policy"] = "failed"
                incomplete_metadata.pop("duration_ms", None)
                incomplete_metadata.pop("phase_timings_ms", None)
                incomplete_metadata["warnings"] = sorted({
                    *incomplete_metadata["warnings"],
                    "metrics_incomplete",
                })
                self._publish_metadata_record(
                    incomplete_metadata,
                    deadline=deadline,
                )
                _check_deadline(deadline, self.paths.lock)
                canonical_verification_started = time.monotonic()
                self._verify_published(revision)
                timings["canonical_verification_1"] = _elapsed_ms(
                    canonical_verification_started
                )
                _check_deadline(deadline, self.paths.lock)
                ready_metadata = self._metadata(
                    revision=revision,
                    fingerprints=fingerprints,
                    commit=commit,
                    counts=counts,
                    indexed_at=indexed_at,
                    discovered=discovered,
                    parser_warnings=parser_warnings,
                    resolver_warnings=resolver_warnings,
                    timings=timings,
                    config=config,
                    duration_ms=_elapsed_ms(started),
                    normalization_versions=normalization_versions,
                )
                ready_metadata["generation"] = generation
                ready_metadata["publication_phase"] = (
                    "pending_final_verify"
                )
                ready_metadata["pending_final_verify"] = True
                ready_metadata["recovery_policy"] = "failed"
                ready_metadata["storage_stamp"] = self.store.storage_stamp()
                ready_metadata.pop("duration_ms", None)
                ready_metadata.pop("phase_timings_ms", None)
                _seal_ready_metadata(ready_metadata)
                self._publish_metadata_record(
                    ready_metadata,
                    deadline=deadline,
                )
                _check_deadline(deadline, self.paths.lock)
                final_verification_started = time.monotonic()
                self._verify_published(revision)
                timings["final_verification"] = _elapsed_ms(
                    final_verification_started
                )
                _check_deadline(deadline, self.paths.lock)
                timings["publication"] = _elapsed_ms(phase)
                metadata = self._metadata(
                    revision=revision,
                    fingerprints=fingerprints,
                    commit=commit,
                    counts=counts,
                    indexed_at=indexed_at,
                    discovered=discovered,
                    parser_warnings=parser_warnings,
                    resolver_warnings=resolver_warnings,
                    timings=timings,
                    config=config,
                    duration_ms=_elapsed_ms(started),
                    normalization_versions=normalization_versions,
                )
                metadata["generation"] = generation
                metadata["publication_phase"] = "pending_final_verify"
                metadata["pending_final_verify"] = True
                metadata["recovery_policy"] = "failed"
                metadata["storage_stamp"] = ready_metadata["storage_stamp"]
                _seal_ready_metadata(metadata)
                diagnostics_staging = None
                try:
                    _check_deadline(deadline, self.paths.lock)
                    diagnostics_staging = self.store.prepare_metadata(
                        self.paths.metadata, metadata
                    )
                    _check_deadline(deadline, self.paths.lock)
                    self.store.refresh_metadata_diagnostics(
                        self.paths.metadata,
                        diagnostics_staging,
                        deadline=deadline,
                    )
                    diagnostics_staging = None
                except Timeout:
                    if diagnostics_staging is not None:
                        self.store.discard_metadata(diagnostics_staging)
                    raise
                except CodeGraphError as exc:
                    if diagnostics_staging is not None:
                        self.store.discard_metadata(diagnostics_staging)
                    raise CodeGraphStoreFailure(
                        "code graph diagnostics publication failed"
                    ) from exc
                result = dict(metadata)
                result.pop("storage_stamp", None)
                result["no_op"] = False
                return result
        except Timeout:
            if staging is not None:
                try:
                    self.store.discard_staging(staging)
                except CodeGraphError:
                    pass
            raise
        except DiscoveryError as exc:
            if staging is not None:
                try:
                    self.store.discard_staging(staging)
                except CodeGraphError:
                    pass
            raise CodeGraphUnsafePathError(
                "unsafe code graph source path"
            ) from exc
        except CodeGraphStoreError as exc:
            if staging is not None:
                try:
                    self.store.discard_staging(staging)
                except CodeGraphError:
                    pass
            raise CodeGraphStoreFailure("code graph store failed") from exc
        except CodeGraphError:
            if staging is not None:
                try:
                    self.store.discard_staging(staging)
                except CodeGraphError:
                    pass
            raise
        except Exception as exc:
            if staging is not None:
                try:
                    self.store.discard_staging(staging)
                except CodeGraphError:
                    pass
            raise CodeGraphBuildError("code graph rebuild failed") from exc
        finally:
            if publication_entered and control is not None:
                control.leave_publication()


__all__ = [
    "AdapterBinding",
    "AdapterFactory",
    "BuildControl",
    "KNOWN_WARNING_CODES",
    "CodeGraphBuildError",
    "CodeGraphIndexer",
    "CodeGraphParseError",
    "CodeGraphStaleError",
    "CodeGraphStoreFailure",
    "CodeGraphUnsafePathError",
    "WikiSelectorResolver",
    "sanitize_warning_codes",
]
