"""Fail-soft request-scoped runtime facade for the project code graph."""
from __future__ import annotations

import atexit
from dataclasses import asdict
import json
import logging
from pathlib import Path
import re
import threading
import time
from typing import Mapping

from filelock import Timeout

from iwiki_mcp.base import Binding

from . import models as codegraph_models
from .config import CodeGraphConfig, CodeGraphConfigError, load_code_graph_config
from .context import (
    CodeGraphContext,
    CodeGraphContextError,
    ContextRequest,
    capture_project_root,
    validate_context_request,
)
from .fingerprint import config_fingerprint, parser_fingerprint
from .indexer import (
    AdapterFactory,
    BuildControl,
    CodeGraphIndexer,
    CodeGraphStaleError,
    CodeGraphStoreFailure,
    CodeGraphUnsafePathError,
    _wiki_read_lock,
    exact_ready_metadata,
    sanitize_warning_codes,
)
from .linking import selector_capture_budget
from .location import CodeGraphLocationError, CodeGraphLocationResolver
from .models import CodeGraphError
from .query import (
    CodeGraphQuery,
    CodeGraphQueryError,
    validate_search_request,
)
from .publication import SnapshotHeader, graph_payload_revision
from .schema import SCHEMA_VERSION, CodeGraphStoreError
from .store import (
    CodeGraphStore,
    _is_canonical_revision,
    code_graph_read_lock,
    code_graph_write_lock,
)


LOGGER = logging.getLogger(__name__)
_INDEX_HINT = "run wiki_code_index"
_DEFAULT_RESOLVER_VERSION = "resolver-v1"
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
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class _BuildJob:
    def __init__(self, domain_key: tuple[str, str]) -> None:
        self.domain_key = domain_key
        self.control = BuildControl()
        self.result: dict[str, object] = {}
        self.thread: threading.Thread | None = None


class _BuildWorkerRegistry:
    """Own the process's single bounded code-graph build worker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job: _BuildJob | None = None

    def start(self, domain_key, target):
        with self._lock:
            if (
                self._job is not None
                and self._job.thread is not None
                and self._job.thread.is_alive()
            ):
                return None
            job = _BuildJob(domain_key)

            def run() -> None:
                target(job.control, job.result)

            job.thread = threading.Thread(
                target=run,
                name="iwiki-code-graph-build",
                daemon=True,
            )
            self._job = job
            try:
                job.thread.start()
            except Exception:
                self._job = None
                raise
            return job

    def is_active(self, domain_key: tuple[str, str]) -> bool:
        with self._lock:
            return bool(
                self._job is not None
                and self._job.domain_key == domain_key
                and self._job.thread is not None
                and self._job.thread.is_alive()
            )

    @property
    def active_count(self) -> int:
        with self._lock:
            return int(
                self._job is not None
                and self._job.thread is not None
                and self._job.thread.is_alive()
            )

    def join(self, timeout: float | None = None) -> None:
        with self._lock:
            job = self._job
        if job is not None and job.thread is not None:
            job.thread.join(timeout)
            self.release(job)

    def release(self, job: _BuildJob) -> None:
        with self._lock:
            if (
                self._job is job
                and job.thread is not None
                and not job.thread.is_alive()
            ):
                self._job = None

    def shutdown(self, timeout: float = 1.0) -> None:
        with self._lock:
            job = self._job
        if job is None or job.thread is None:
            return
        job.control.cancel()
        job.thread.join(max(0.0, timeout))
        self.release(job)


_BUILD_WORKERS = _BuildWorkerRegistry()


def shutdown_code_graph_workers(timeout: float = 1.0) -> None:
    """Cooperatively cancel and boundedly join process build worker."""
    _BUILD_WORKERS.shutdown(timeout)


atexit.register(shutdown_code_graph_workers)


class _NoopWikiSelectorResolver:
    def resolve(self, **_kwargs):
        return ()


def _not_configured() -> dict[str, object]:
    return {
        "error": "code graph is not configured",
        "code": "not_configured",
        "hint": "configure a primary domain and enable code_graph",
    }


def _invalid_config() -> dict[str, object]:
    return {
        "error": "code graph configuration is invalid",
        "code": "invalid_config",
        "hint": "inspect code_graph project configuration",
    }


def _rebuild_failed() -> dict[str, object]:
    return {
        "error": "code graph rebuild failed",
        "code": "rebuild_failed",
        "hint": "inspect wiki_code_status and retry",
    }


def _typed_failure(error: CodeGraphError) -> dict[str, object]:
    responses = {
        "parse_failed": (
            "code graph parse failed",
            "inspect wiki_code_status and retry",
        ),
        "store_failed": (
            "code graph store failed",
            "inspect wiki_code_status and retry",
        ),
        "stale": ("code graph is stale", _INDEX_HINT),
        "unsafe_path": (
            "code graph source path is unsafe",
            "inspect code_graph project configuration",
        ),
    }
    code = getattr(error, "code", "rebuild_failed")
    if code not in responses:
        return _rebuild_failed()
    message, hint = responses[code]
    return {
        "error": message,
        "code": code,
        "hint": hint,
        "fresh": False,
    }


def sanitized_error(error: CodeGraphError) -> dict[str, object]:
    """Map typed graph failures without exposing exception text."""
    code = getattr(error, "code", "rebuild_failed")
    if code == "invalid_config":
        return _invalid_config()
    if code == "not_configured":
        return _not_configured()
    if code == "busy":
        return CodeGraphRuntime._busy_response()
    return _typed_failure(error)


def _metadata(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _safe_nonnegative(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def _safe_phase_timings(value: object) -> dict[str, int]:
    mapping = value if isinstance(value, Mapping) else {}
    return {
        phase: timing
        for phase in _PHASE_NAMES
        if type(timing := mapping.get(phase)) is int and timing >= 0
    }


def _pending_final_verify(metadata: Mapping[str, object]) -> bool:
    """Return whether final verification lacks durable success diagnostics."""
    duration = metadata.get("duration_ms")
    timings = _safe_phase_timings(metadata.get("phase_timings_ms"))
    return (
        metadata.get("state") == "ready"
        and metadata.get("publication_phase") == "pending_final_verify"
        and not (
            type(duration) is int
            and duration >= 0
            and set(timings) == set(_PHASE_NAMES)
        )
    )


class CodeGraphRuntime:
    """Resolve configuration once and isolate all optional graph failures."""

    def __init__(
        self,
        binding: Binding,
        *,
        adapter_factories: Mapping[str, AdapterFactory] | None = None,
        resolver_version: str = _DEFAULT_RESOLVER_VERSION,
    ) -> None:
        self.binding = binding
        self.config: CodeGraphConfig | None = None
        self.paths = None
        self._store = None
        self._indexer = None
        self._context_root = None
        self._configuration_error = False
        self._initialization_error = False
        self._unsafe_location = False
        self._worker_domain_key = (
            str(Path(binding.base).absolute()),
            binding.primary or "",
        )
        self._parser_version = ""
        self._grammar_version = ""
        self._adapter_version = ""
        self._resolver_version = resolver_version
        if binding.primary is None:
            return
        try:
            self.config = load_code_graph_config(binding.project_dir)
        except CodeGraphConfigError:
            self._configuration_error = True
            return
        if not self.config.enabled:
            return
        try:
            self._context_root = capture_project_root(binding.project_dir)
            self.paths = CodeGraphLocationResolver(
                binding.base,
                binding.primary,
                binding.project_dir,
            ).resolve(ensure_excluded=False)
            factories = adapter_factories or {}
            try:
                selected_factories = {
                    language: factories[language]
                    for language in self.config.languages
                }
            except KeyError:
                self._configuration_error = True
                self.paths = None
                return
            self._parser_version = ";".join(
                f"{language}:{factory.parser_version}"
                for language, factory in selected_factories.items()
            )
            self._grammar_version = ";".join(
                f"{language}:{factory.grammar_version}"
                for language, factory in selected_factories.items()
            )
            self._adapter_version = ";".join(
                f"{language}:{factory.adapter_version}"
                for language, factory in selected_factories.items()
            )
            self._store = CodeGraphStore(
                self.paths.database,
                cache_base=binding.base,
            )
            self._indexer = CodeGraphIndexer(
                cache_base=binding.base,
                project_dir=binding.project_dir,
                domain=binding.primary,
                config=self.config,
                paths=self.paths,
                adapter_factories=selected_factories,
                resolver_version=self._resolver_version,
                wiki_selector_resolver=_NoopWikiSelectorResolver(),
            )
        except CodeGraphLocationError:
            self.paths = None
            self._store = None
            self._indexer = None
            self._unsafe_location = True
        except Exception:
            self.paths = None
            self._store = None
            self._indexer = None
            self._initialization_error = True

    def _unavailable(self) -> dict[str, object] | None:
        if self._unsafe_location:
            return _typed_failure(CodeGraphUnsafePathError())
        if self._initialization_error:
            return _rebuild_failed()
        if self._configuration_error:
            return _invalid_config()
        if (
            self.binding.primary is None
            or self.config is None
            or not self.config.enabled
        ):
            return _not_configured()
        return None

    def _missing_status(
        self,
        normalization_versions: tuple[str, str] | None = None,
    ) -> dict[str, object]:
        if normalization_versions is None:
            normalization_versions = self._normalization_versions()
        return {
            "enabled": True,
            "domain": self.binding.primary,
            "state": "missing",
            "revision": None,
            "fresh": False,
            "schema_version": SCHEMA_VERSION,
            "parser_version": self._parser_version,
            "grammar_version": self._grammar_version,
            "adapter_version": self._adapter_version,
            "resolver_version": self._resolver_version,
            "normalizer_version": normalization_versions[0],
            "unicode_data_version": normalization_versions[1],
            "counts": {
                "languages": {},
                "files": 0,
                "modules": 0,
                "symbols": 0,
                "relations": 0,
                "entity_kinds": {},
                "symbol_kinds": {},
                "relation_types": {},
                "resolution_states": {},
            },
            "duration_ms": 0,
            "pending_final_verify": False,
            "module_warnings": 0,
            "excluded_files": 0,
            "truncated_files": 0,
            "truncated": False,
            "parser_errors": 0,
            "warnings": ["code_graph_missing"],
        }

    def _store_failure_status(self) -> dict[str, object]:
        return {
            **_typed_failure(CodeGraphStoreFailure()),
            "enabled": True,
            "domain": self.binding.primary,
            "state": "failed",
            "revision": None,
            "fresh": False,
            "duration_ms": 0,
            "pending_final_verify": False,
            "module_warnings": 0,
            "excluded_files": 0,
            "truncated_files": 0,
            "truncated": False,
            "parser_errors": 0,
            "warnings": ["code_graph_store_failed"],
            "hint": _INDEX_HINT,
        }

    @staticmethod
    def _normalization_versions() -> tuple[str, str]:
        return (
            codegraph_models.NORMALIZER_VERSION,
            codegraph_models.UNICODE_DATA_VERSION,
        )

    def _current_toolchain_fingerprint(
        self,
        normalization_versions: tuple[str, str],
    ) -> str:
        assert self.config is not None
        return parser_fingerprint(
            languages=self.config.languages,
            schema_version=SCHEMA_VERSION,
            parser_version=self._parser_version,
            grammar_version=self._grammar_version,
            adapter_version=self._adapter_version,
            resolver_version=self._resolver_version,
            normalizer_version=normalization_versions[0],
            unicode_data_version=normalization_versions[1],
        )

    def _with_rebuilding_state(
        self, status: dict[str, object], *, shared_writer: bool = False
    ) -> dict[str, object]:
        if (
            not shared_writer
            and not _BUILD_WORKERS.is_active(self._worker_domain_key)
        ):
            return status
        return {
            **status,
            "enabled": True,
            "state": "rebuilding",
            "fresh": False,
            "warnings": ["code_graph_rebuilding"],
            "hint": _INDEX_HINT,
        }

    def _read_status(
        self,
        *,
        persisted_metadata: Mapping[str, object] | None = None,
        normalization_versions: tuple[str, str] | None = None,
    ) -> dict[str, object]:
        """Read authoritative metadata/schema while publication cannot race."""
        assert self.paths is not None and self._store is not None
        if normalization_versions is None:
            normalization_versions = self._normalization_versions()
        if not self.paths.database.is_file():
            return self._with_rebuilding_state(
                self._missing_status(normalization_versions)
            )
        schema_version = self._store.inspect_schema_version()
        if schema_version is not None and schema_version != SCHEMA_VERSION:
            incompatible = self._missing_status(normalization_versions)
            incompatible["warnings"] = ["code_graph_incompatible"]
            incompatible["hint"] = _INDEX_HINT
            return self._with_rebuilding_state(incompatible)
        persisted = (
            persisted_metadata
            if persisted_metadata is not None
            else _metadata(self.paths.metadata)
        )
        try:
            with self._store.read_lease() as connection:
                data_version = connection.execute(
                    "PRAGMA data_version"
                ).fetchone()
                row = connection.execute(
                    "SELECT repository_id, git_commit, source_fingerprint, "
                    "config_fingerprint, parser_fingerprint, "
                    "normalizer_version, unicode_data_version, revision, state, "
                    "indexed_at FROM repositories WHERE repository_id = ?",
                    (self.binding.primary,),
                ).fetchone()
                if row is None:
                    return self._with_rebuilding_state(
                        self._missing_status(normalization_versions)
                    )
                storage_stamp = self._store.storage_stamp()
                row_after = connection.execute(
                    "SELECT repository_id, git_commit, source_fingerprint, "
                    "config_fingerprint, parser_fingerprint, "
                    "normalizer_version, unicode_data_version, revision, state, "
                    "indexed_at FROM repositories WHERE repository_id = ?",
                    (self.binding.primary,),
                ).fetchone()
                data_version_after = connection.execute(
                    "PRAGMA data_version"
                ).fetchone()
                if row != row_after or data_version != data_version_after:
                    raise CodeGraphStoreError(
                        "code graph changed during readiness proof"
                    )
        except Exception:
            return self._with_rebuilding_state(
                self._store_failure_status()
            )
        state = str(row[8])
        persisted_timings = _safe_phase_timings(
            persisted.get("phase_timings_ms")
        )
        persisted_duration = persisted.get("duration_ms")
        persisted_fingerprints = persisted.get("fingerprints")
        persisted_input = persisted.get("input_fingerprint")
        metadata_matches = (
            exact_ready_metadata(persisted)
            and persisted.get("domain") == row[0]
            and persisted.get("state") == "ready"
            and persisted.get("revision") == row[7]
            and _is_canonical_revision(row[7])
            and persisted.get("schema_version") == SCHEMA_VERSION
            and persisted_fingerprints == {
                "source": row[2],
                "config": row[3],
                "parser": row[4],
            }
            and isinstance(persisted_input, str)
            and _SHA256.fullmatch(persisted_input) is not None
            and persisted.get("git_commit") == row[1]
            and persisted.get("indexed_at") == row[9]
            and persisted.get("storage_stamp") == storage_stamp
        )
        persisted_parser_version = persisted.get("parser_version")
        persisted_grammar_version = persisted.get("grammar_version")
        persisted_adapter_version = persisted.get("adapter_version")
        persisted_resolver_version = persisted.get("resolver_version")
        persisted_normalizer_version = persisted.get("normalizer_version")
        persisted_unicode_data_version = persisted.get("unicode_data_version")
        toolchain_mismatch = (
            metadata_matches
            and (
                persisted_parser_version != self._parser_version
                or persisted_grammar_version != self._grammar_version
                or persisted_adapter_version != self._adapter_version
                or persisted_resolver_version != self._resolver_version
                or persisted_normalizer_version != normalization_versions[0]
                or persisted_unicode_data_version != normalization_versions[1]
                or row[3] != config_fingerprint(self.config)
                or row[5] != normalization_versions[0]
                or row[6] != normalization_versions[1]
            )
        ) or (
            not metadata_matches
            and (
                row[4] != self._current_toolchain_fingerprint(
                    normalization_versions
                )
                or row[5] != normalization_versions[0]
                or row[6] != normalization_versions[1]
            )
        )
        if not metadata_matches:
            persisted = {}
        if state == "ready" and not metadata_matches:
            effective_state = "failed"
        elif state == "ready" and toolchain_mismatch:
            effective_state = "dirty"
        else:
            effective_state = state
        if metadata_matches:
            counts = dict(persisted["counts"])
            resolution_ratios = dict(persisted["resolution_ratios"])
        else:
            counts = dict(
                self._missing_status(normalization_versions)["counts"]
            )
            resolution_ratios = {}
        result = {
            "enabled": True,
            "domain": self.binding.primary,
            "state": effective_state,
            "revision": row[7],
            "fresh": effective_state == "ready",
            "git_commit": row[1],
            "fingerprints": {
                "source": row[2],
                "config": row[3],
                "parser": row[4],
            },
            "schema_version": SCHEMA_VERSION,
            "parser_version": (
                persisted_parser_version
                if isinstance(persisted_parser_version, str)
                else None
            ),
            "grammar_version": (
                persisted_grammar_version
                if isinstance(persisted_grammar_version, str)
                else None
            ),
            "adapter_version": (
                persisted_adapter_version
                if isinstance(persisted_adapter_version, str)
                else None
            ),
            "resolver_version": (
                persisted_resolver_version
                if isinstance(persisted_resolver_version, str)
                else None
            ),
            "normalizer_version": (
                persisted_normalizer_version
                if isinstance(persisted_normalizer_version, str)
                else None
            ),
            "unicode_data_version": (
                persisted_unicode_data_version
                if isinstance(persisted_unicode_data_version, str)
                else None
            ),
            "counts": counts,
            "resolution_ratios": resolution_ratios,
            "excluded_files": _safe_nonnegative(
                persisted.get("excluded_files")
            ),
            "truncated": (
                persisted.get("truncated")
                if type(persisted.get("truncated")) is bool
                else False
            ),
            "truncated_files": _safe_nonnegative(
                persisted.get("truncated_files")
            ),
            "parser_errors": _safe_nonnegative(
                persisted.get("parser_errors")
            ),
            "module_warnings": _safe_nonnegative(
                persisted.get("module_warnings")
            ),
            "pending_final_verify": (
                persisted.get("pending_final_verify") is True
            ),
            "indexed_at": row[9],
            "warnings": sanitize_warning_codes(persisted.get("warnings")),
        }
        if metadata_matches:
            result["phase_timings_ms"] = persisted_timings
            result["duration_ms"] = persisted_duration
        if effective_state != "ready":
            result["warnings"] = [f"code_graph_{effective_state}"]
            if not metadata_matches:
                result["warnings"].append("metadata_reconstructed")
            result["hint"] = _INDEX_HINT
        elif not metadata_matches:
            result["warnings"] = [
                "metadata_reconstructed",
                "metrics_incomplete",
            ]
        return self._with_rebuilding_state(result)

    def export_snapshot(self) -> tuple[SnapshotHeader, dict] | dict[str, object]:
        """Return the canonical rows and header of the local ready snapshot."""
        unavailable = self._unavailable()
        if unavailable is not None:
            return unavailable
        assert self._store is not None
        try:
            rows = {
                kind: list(self._store.stable_rows(kind))
                for kind in ("repositories", "files", "symbols", "relations")
            }
        except CodeGraphStoreError:
            return self._store_failure_status()
        repository = next(
            (
                row
                for row in rows["repositories"]
                if row.get("repository_id") == self.binding.primary
            ),
            None,
        )
        if repository is None or repository.get("state") != "ready":
            return _not_configured()
        header = SnapshotHeader(
            protocol_version=1,
            schema_version=SCHEMA_VERSION,
            repository_id=str(repository["repository_id"]),
            source_fingerprint=str(repository["source_fingerprint"]),
            parser_fingerprint=str(repository["parser_fingerprint"]),
            normalizer_version=str(repository["normalizer_version"]),
            unicode_data_version=str(repository["unicode_data_version"]),
            languages=("python",),
            expected_counts={kind: len(rows[kind]) for kind in rows},
            graph_payload_revision=graph_payload_revision(rows),
        )
        return header, rows

    def status(self) -> dict[str, object]:
        """Read metadata and compatible schema only; never discover or parse."""
        unavailable = self._unavailable()
        if unavailable is not None:
            return {
                **unavailable,
                "enabled": unavailable.get("code") != "not_configured",
            }
        assert self.paths is not None and self._store is not None
        normalization_versions = self._normalization_versions()
        for _attempt in range(4):
            before = dict(_metadata(self.paths.metadata))
            try:
                with code_graph_read_lock(self.paths.lock):
                    locked_metadata = dict(_metadata(self.paths.metadata))
                    if before != locked_metadata:
                        continue
                    status = self._read_status(
                        persisted_metadata=locked_metadata,
                        normalization_versions=normalization_versions,
                    )
                    after = dict(_metadata(self.paths.metadata))
            except Timeout:
                return self._shared_rebuilding_status(
                    before, normalization_versions
                )
            except CodeGraphStoreError:
                return self._store_failure_status()
            if locked_metadata != after:
                continue
            if "error" in status:
                return status
            metadata_state = after.get("state")
            if metadata_state in {"rebuilding", "recovering"} or (
                _pending_final_verify(after)
            ):
                if self._local_build_active():
                    return self._with_rebuilding_state(
                        status, shared_writer=True
                    )
                try:
                    recovered = self._recover_stale_metadata(after)
                except CodeGraphStoreError:
                    return self._store_failure_status()
                if recovered:
                    continue
                return self._with_rebuilding_state(
                    status, shared_writer=True
                )
            if metadata_state == "failed":
                failed = {
                    **status,
                    "state": "failed",
                    "fresh": False,
                    "warnings": [
                        "code_graph_failed",
                        "metrics_incomplete",
                    ],
                    "hint": _INDEX_HINT,
                }
                failed.pop("duration_ms", None)
                failed.pop("phase_timings_ms", None)
                return failed
            metadata_revision = after.get("revision")
            if (
                metadata_state == "ready"
                and metadata_revision is not None
                and status.get("revision") != metadata_revision
            ):
                continue
            return status
        metadata = dict(_metadata(self.paths.metadata))
        try:
            with code_graph_read_lock(self.paths.lock):
                current = dict(_metadata(self.paths.metadata))
                status = self._read_status(
                    persisted_metadata=current,
                    normalization_versions=normalization_versions,
                )
        except Timeout:
            return self._shared_rebuilding_status(
                metadata, normalization_versions
            )
        except CodeGraphStoreError:
            return self._store_failure_status()
        metadata = current
        if (
            metadata.get("state") in {"rebuilding", "recovering"}
            or _pending_final_verify(metadata)
        ):
            if self._local_build_active():
                return self._with_rebuilding_state(status, shared_writer=True)
            try:
                recovered = self._recover_stale_metadata(metadata)
            except CodeGraphStoreError:
                return self._store_failure_status()
            if recovered:
                return self._read_status(
                    persisted_metadata=_metadata(self.paths.metadata),
                    normalization_versions=normalization_versions,
                )
            return self._with_rebuilding_state(
                status, shared_writer=True
            )
        return status

    def _shared_rebuilding_status(
        self,
        metadata: Mapping[str, object],
        normalization_versions: tuple[str, str] | None = None,
    ) -> dict[str, object]:
        revision = metadata.get("revision")
        status = self._missing_status(normalization_versions)
        status["revision"] = revision if isinstance(revision, str) else None
        return self._with_rebuilding_state(status, shared_writer=True)

    def _local_build_active(self) -> bool:
        """Report whether this process still owns a live publication worker."""
        return _BUILD_WORKERS.is_active(self._worker_domain_key)

    def _recover_stale_metadata(
        self,
        expected: Mapping[str, object],
    ) -> bool:
        """Replace crash-stale metadata from SQL while owning writer lock."""
        assert self.paths is not None and self._store is not None
        try:
            recovery = code_graph_write_lock(self.paths.lock, timeout=0)
            recovery.__enter__()
        except Timeout:
            return False
        try:
            current = dict(_metadata(self.paths.metadata))
            if (
                current != expected
                or (
                    current.get("state") not in {"rebuilding", "recovering"}
                    and not _pending_final_verify(current)
                )
            ):
                return True
            normalization_versions = self._normalization_versions()
            authoritative = self._read_status(
                persisted_metadata={},
                normalization_versions=normalization_versions,
            )
            authoritative_state = authoritative.get("state")
            generation = current.get("generation")
            if type(generation) is not int or generation < 0:
                generation = 0
            revision = authoritative.get("revision")
            safe_revision = revision if isinstance(revision, str) else None
            metadata_revision = current.get("revision")
            previous_revision = current.get("previous_revision")
            publication_phase = current.get("publication_phase")
            prior_state = current.get("prior_state")
            recovery_policy = current.get("recovery_policy")
            if _pending_final_verify(current):
                state = "failed"
            elif publication_phase == "provisional":
                state = "failed"
            elif publication_phase == "building":
                state = (
                    prior_state
                    if recovery_policy == "restore_prior"
                    and previous_revision == safe_revision
                    and prior_state in {
                        "missing", "ready", "dirty", "failed"
                    }
                    else "failed"
                )
            else:
                state = (
                    authoritative_state
                    if metadata_revision == safe_revision
                    and authoritative_state in {"missing", "ready", "failed"}
                    else "failed"
                )
            recovered = {
                key: value
                for key, value in authoritative.items()
                if key not in {
                    "error",
                    "code",
                    "hint",
                    "duration_ms",
                    "phase_timings_ms",
                }
            }
            recovered.update({
                "state": state,
                "generation": generation,
                "revision": safe_revision,
                "fresh": state == "ready",
                "warnings": (
                    ["metadata_reconstructed", "metrics_incomplete"]
                    if state == "ready"
                    else [f"code_graph_{state}"]
                ),
            })
            staging = self._store.prepare_metadata(
                self.paths.metadata, recovered
            )
            try:
                self._store.publish_metadata(
                    self.paths.metadata, staging
                )
            except Exception:
                self._store.discard_metadata(staging)
                return False
            return True
        finally:
            recovery.__exit__(None, None, None)

    @property
    def active_workers(self) -> int:
        return _BUILD_WORKERS.active_count

    def join_workers(self, timeout: float | None = None) -> None:
        _BUILD_WORKERS.join(timeout)

    @staticmethod
    def _busy_response() -> dict[str, object]:
        return {
            "error": "code graph is busy",
            "code": "busy",
            "hint": "retry wiki_code_index",
        }

    def _index_with_deadline(
        self,
        *,
        force: bool,
        languages: list[str] | None,
        deadline: float,
        restore_prior_on_abort: bool,
    ) -> dict[str, object]:
        assert self._indexer is not None
        if time.monotonic() >= deadline:
            return self._busy_response()

        def run_build(
            control: BuildControl,
            result: dict[str, object],
        ) -> None:
            try:
                built = self._indexer.build(
                    force=force,
                    languages=languages,
                    deadline=deadline,
                    restore_prior_on_abort=restore_prior_on_abort,
                    control=control,
                )
                counts = built.get("counts", {})
                file_count = (
                    counts.get("files", 0)
                    if isinstance(counts, dict)
                    else 0
                )
                LOGGER.info(
                    "code_graph_build code=ready files=%d duration_ms=%d",
                    file_count,
                    built.get("duration_ms", 0),
                )
                result.update(built)
            except Timeout:
                LOGGER.info("code_graph_build code=busy")
                result.update(self._busy_response())
            except CodeGraphError as exc:
                failure = _typed_failure(exc)
                LOGGER.error(
                    "code_graph_build code=%s", failure["code"]
                )
                result.update(failure)
            except Exception:
                LOGGER.error("code_graph_build code=rebuild_failed")
                result.update(_rebuild_failed())
        try:
            job = _BUILD_WORKERS.start(
                self._worker_domain_key,
                run_build,
            )
        except Exception:
            return _rebuild_failed()
        if job is None or job.thread is None:
            return self._busy_response()
        job.thread.join(max(0.0, deadline - time.monotonic()))
        if job.thread.is_alive():
            job.control.cancel()
            LOGGER.info("code_graph_build code=busy")
            return self._busy_response()
        _BUILD_WORKERS.release(job)
        return job.result or _rebuild_failed()

    def index(
        self,
        *,
        force: bool = False,
        languages: list[str] | None = None,
    ) -> dict[str, object]:
        if languages is not None and (
            not languages or any(language != "python" for language in languages)
        ):
            return _invalid_config()
        unavailable = self._unavailable()
        if unavailable is not None:
            return unavailable
        assert self._indexer is not None and self.config is not None
        full_rebuild_seconds = (
            self.config.max_full_rebuild_seconds
            or self.config.max_rebuild_seconds
        )
        deadline = time.monotonic() + full_rebuild_seconds
        return self._index_with_deadline(
            force=force,
            languages=languages,
            deadline=deadline,
            restore_prior_on_abort=False,
        )

    def query_guard(
        self,
        *,
        remaining_seconds: float | None = None,
        _selector_lock_held: bool = False,
        _selector_snapshot: object | None = None,
    ) -> dict[str, object]:
        """Prevent non-ready callers from observing rows from an old snapshot."""
        status = self.status()
        if "error" in status:
            return {**status, "fresh": False, "results": []}
        if status.get("state") == "ready":
            assert self.config is not None and self._indexer is not None
            freshness_budget = (
                self.config.max_rebuild_seconds
                if remaining_seconds is None
                else remaining_seconds
            )
            freshness_deadline = time.monotonic() + max(
                0.0,
                min(freshness_budget, self.config.max_rebuild_seconds),
            )
            freshness_started = time.monotonic()
            try:
                became_dirty = self._indexer.mark_dirty_if_stale(
                    deadline=freshness_deadline,
                    selector_lock_held=_selector_lock_held,
                    selector_snapshot=_selector_snapshot,
                )
            except Timeout:
                return {
                    **status,
                    "error": "code graph is busy",
                    "code": "busy",
                    "fresh": False,
                    "results": [],
                    "hint": "retry wiki_code_index",
                }
            except CodeGraphError as exc:
                return {
                    **status,
                    **_typed_failure(exc),
                    "fresh": False,
                    "results": [],
                }
            except Exception:
                duration_ms = max(
                    0,
                    int((time.monotonic() - freshness_started) * 1000),
                )
                LOGGER.error(
                    "code_graph_query_guard code=rebuild_failed "
                    "count=1 duration_ms=%d",
                    duration_ms,
                )
                return {
                    **status,
                    **_rebuild_failed(),
                    "fresh": False,
                    "results": [],
                }
            if became_dirty:
                current = self.status()
                if current.get("state") == "ready":
                    return {**current, "results": []}
                if current.get("state") != "dirty":
                    return {
                        **current,
                        "fresh": False,
                        "results": [],
                        "hint": _INDEX_HINT,
                    }
                return {
                    **current,
                    **_typed_failure(CodeGraphStaleError()),
                    "fresh": False,
                    "results": [],
                }
            return {**self.status(), "results": []}
        config = self.config
        state = status.get("state")
        budget = (
            config.max_rebuild_seconds
            if config is not None and remaining_seconds is None
            else remaining_seconds
        )
        if (
            config is not None
            and config.auto_rebuild == "bounded"
            and state in ("missing", "dirty")
            and budget is not None
            and budget >= config.max_rebuild_seconds
        ):
            deadline = time.monotonic() + min(
                budget, config.max_rebuild_seconds
            )
            rebuilt = self._index_with_deadline(
                force=False,
                languages=None,
                deadline=deadline,
                restore_prior_on_abort=True,
            )
            if rebuilt.get("state") == "ready":
                return {**self.status(), "results": []}
            status = self.status()
        return {
            **status,
            "fresh": False,
            "results": [],
            "hint": _INDEX_HINT,
        }

    def search(
        self,
        query: str,
        *,
        kinds: list[str] | None = None,
        path: str | None = None,
        languages: list[str] | None = None,
        limit: int = 20,
    ) -> dict[str, object]:
        """Search only one revision proven ready under the shared reader lock."""
        try:
            request = validate_search_request(
                query,
                kinds=kinds,
                path=path,
                languages=languages,
                limit=limit,
            )
        except CodeGraphQueryError:
            return _invalid_config()
        guarded = self.query_guard()
        if guarded.get("fresh") is not True:
            return guarded
        assert self.paths is not None and self._store is not None
        guarded_revision = guarded.get("revision")
        query_started = time.monotonic()
        try:
            query_engine = CodeGraphQuery(self.binding.primary or "")
            with code_graph_read_lock(self.paths.lock):
                before = dict(_metadata(self.paths.metadata))
                if (
                    _BUILD_WORKERS.is_active(self._worker_domain_key)
                    or not exact_ready_metadata(before)
                    or before.get("domain") != self.binding.primary
                    or before.get("state") != "ready"
                    or before.get("revision") != guarded_revision
                ):
                    return {
                        **self._with_rebuilding_state(
                            guarded,
                            shared_writer=_BUILD_WORKERS.is_active(
                                self._worker_domain_key
                            ),
                        ),
                        "fresh": False,
                        "results": [],
                        "hint": _INDEX_HINT,
                    }
                with self._store.read_lease() as connection:
                    data_version = connection.execute(
                        "PRAGMA data_version"
                    ).fetchone()
                    repository = connection.execute(
                        "SELECT state, revision FROM repositories "
                        "WHERE repository_id = ?",
                        (self.binding.primary,),
                    ).fetchone()
                    if repository != ("ready", guarded_revision):
                        return {
                            **guarded,
                            "fresh": False,
                            "results": [],
                            "hint": _INDEX_HINT,
                        }
                    sealed_stamp = before.get("storage_stamp")
                    if (
                        not isinstance(sealed_stamp, Mapping)
                        or self._store.storage_stamp() != sealed_stamp
                    ):
                        return {
                            **guarded,
                            "fresh": False,
                            "results": [],
                            "hint": _INDEX_HINT,
                        }
                    results = query_engine.search(
                        connection,
                        request,
                    )
                    repository_after = connection.execute(
                        "SELECT state, revision FROM repositories "
                        "WHERE repository_id = ?",
                        (self.binding.primary,),
                    ).fetchone()
                    data_version_after = connection.execute(
                        "PRAGMA data_version"
                    ).fetchone()
                    if (
                        self._store.storage_stamp() != sealed_stamp
                        or repository_after != repository
                        or data_version_after != data_version
                    ):
                        return {
                            **guarded,
                            "fresh": False,
                            "results": [],
                            "hint": _INDEX_HINT,
                        }
                    after = dict(_metadata(self.paths.metadata))
                    if (
                        before != after
                        or _BUILD_WORKERS.is_active(self._worker_domain_key)
                    ):
                        return {
                            **guarded,
                            "fresh": False,
                            "results": [],
                            "hint": _INDEX_HINT,
                        }
        except CodeGraphQueryError:
            return _invalid_config()
        except Timeout:
            return {
                **self._busy_response(),
                "fresh": False,
                "results": [],
            }
        except CodeGraphStoreError:
            return {
                **_typed_failure(CodeGraphStoreFailure()),
                "fresh": False,
                "results": [],
            }
        except CodeGraphError as exc:
            return {
                **sanitized_error(exc),
                "fresh": False,
                "results": [],
            }
        except Exception:
            duration_ms = max(
                0,
                int((time.monotonic() - query_started) * 1000),
            )
            LOGGER.error(
                "code_graph_query code=rebuild_failed count=1 duration_ms=%d",
                duration_ms,
            )
            return {
                **_rebuild_failed(),
                "fresh": False,
                "results": [],
            }
        return {
            "domain": guarded.get("domain"),
            "state": guarded.get("state"),
            "revision": guarded_revision,
            "fresh": True,
            "warnings": list(guarded.get("warnings", [])),
            "results": [asdict(item) for item in results],
        }

    def _empty_context_response(
        self,
        request: ContextRequest,
        guard: Mapping[str, object],
    ) -> dict[str, object]:
        """Return the exact empty Section 13.5 context response shape."""
        response = {
            "domain": guard.get("domain", self.binding.primary),
            "state": guard.get("state"),
            "revision": guard.get("revision"),
            "seeds": list(request.seeds),
            "nodes": [],
            "relations": [],
            "files": [],
            "wiki_pages": [],
            "limits": {
                "depth": request.depth,
                "max_nodes": request.max_nodes,
                "max_files": request.max_files,
                "max_source_bytes": request.max_source_bytes,
            },
            "truncated": False,
            "warnings": list(guard.get("warnings", [])),
            "fresh": False,
        }
        for key in ("error", "code", "hint"):
            if key in guard:
                response[key] = guard[key]
        return response

    def context(
        self,
        seeds: list[str],
        *,
        direction: str = "both",
        depth: int = 1,
        relations: list[str] | None = None,
        include_source: bool = False,
        include_wiki: bool = True,
        max_nodes: int = 50,
        max_files: int = 20,
        max_source_bytes: int = 200_000,
    ) -> dict[str, object]:
        """Hold Wiki selector generation stable while hydrating Wiki links."""
        arguments = {
            "direction": direction,
            "depth": depth,
            "relations": relations,
            "include_source": include_source,
            "include_wiki": include_wiki,
            "max_nodes": max_nodes,
            "max_files": max_files,
            "max_source_bytes": max_source_bytes,
        }
        if not include_wiki or self._indexer is None or self.config is None:
            return self._context_unleased(seeds, **arguments)
        try:
            request = validate_context_request(seeds, **arguments)
        except CodeGraphContextError:
            return _invalid_config()
        resolver = self._indexer.wiki_selector_resolver
        capture = getattr(resolver, "capture", None)
        verify = getattr(resolver, "verify_snapshot", None)
        if not callable(capture) or not callable(verify):
            return self._context_unleased(seeds, **arguments)
        timeout = self.config.max_rebuild_seconds
        deadline = time.monotonic() + timeout

        def selector_control() -> None:
            if time.monotonic() >= deadline:
                raise Timeout(str(self.paths.lock if self.paths else "code graph"))

        snapshot = None
        try:
            with _wiki_read_lock(self.binding.base, timeout):
                snapshot = capture(
                    domain=self.binding.primary or "",
                    check_control=selector_control,
                    max_bytes=selector_capture_budget(
                        self.config.max_file_bytes,
                        self.config.max_total_files,
                    ),
                )
                verify(snapshot, check_control=selector_control)
                response = self._context_unleased(
                    seeds,
                    _request=request,
                    _selector_lock_held=True,
                    _selector_snapshot=snapshot,
                    _deadline=deadline,
                    **arguments,
                )
                verify(snapshot, check_control=selector_control)
                return response
        except Timeout:
            return self._empty_context_response(
                request, {**self.status(), **self._busy_response()}
            )
        except Exception:
            return self._empty_context_response(
                request,
                {**self.status(), **_typed_failure(CodeGraphStaleError())},
            )
        finally:
            if snapshot is not None:
                close_snapshot = getattr(resolver, "close_snapshot", None)
                if callable(close_snapshot):
                    close_snapshot(snapshot)

    def _context_unleased(
        self,
        seeds: list[str],
        *,
        direction: str = "both",
        depth: int = 1,
        relations: list[str] | None = None,
        include_source: bool = False,
        include_wiki: bool = True,
        max_nodes: int = 50,
        max_files: int = 20,
        max_source_bytes: int = 200_000,
        _request: ContextRequest | None = None,
        _selector_lock_held: bool = False,
        _selector_snapshot: object | None = None,
        _deadline: float | None = None,
    ) -> dict[str, object]:
        """Compose context under one coherent ready-revision read lease."""
        if _request is None:
            try:
                request = validate_context_request(
                    seeds,
                    direction=direction,
                    depth=depth,
                    relations=relations,
                    include_source=include_source,
                    include_wiki=include_wiki,
                    max_nodes=max_nodes,
                    max_files=max_files,
                    max_source_bytes=max_source_bytes,
                )
            except CodeGraphContextError:
                return _invalid_config()
        else:
            request = _request
        remaining = (
            None if _deadline is None else max(0.0, _deadline - time.monotonic())
        )
        guarded = self.query_guard(
            remaining_seconds=remaining,
            _selector_lock_held=_selector_lock_held,
            _selector_snapshot=_selector_snapshot,
        )
        context_guarded = {
            key: value for key, value in guarded.items() if key != "results"
        }
        if guarded.get("fresh") is not True:
            return self._empty_context_response(request, context_guarded)
        assert (
            self.paths is not None
            and self._store is not None
            and self.config is not None
        )
        guarded_revision = guarded.get("revision")
        started = time.monotonic()
        try:
            engine = CodeGraphContext(
                self.binding.primary or "",
                self._context_root,
                self.config.max_file_bytes,
            )
            with code_graph_read_lock(self.paths.lock):
                before = dict(_metadata(self.paths.metadata))
                if (
                    _BUILD_WORKERS.is_active(self._worker_domain_key)
                    or not exact_ready_metadata(before)
                    or before.get("domain") != self.binding.primary
                    or before.get("state") != "ready"
                    or before.get("revision") != guarded_revision
                ):
                    return self._empty_context_response(
                        request,
                        self._with_rebuilding_state(
                            context_guarded,
                            shared_writer=_BUILD_WORKERS.is_active(
                                self._worker_domain_key
                            ),
                        ),
                    )
                with self._store.read_lease() as connection:
                    data_version = connection.execute(
                        "PRAGMA data_version"
                    ).fetchone()
                    repository = self._store.repository_state(
                        connection, self.binding.primary or ""
                    )
                    sealed_stamp = before.get("storage_stamp")
                    if (
                        repository != ("ready", guarded_revision)
                        or not isinstance(sealed_stamp, Mapping)
                        or self._store.storage_stamp() != sealed_stamp
                    ):
                        return self._empty_context_response(
                            request,
                            {**context_guarded, "hint": _INDEX_HINT},
                        )
                    response = engine.context(connection, request)
                    repository_after = self._store.repository_state(
                        connection, self.binding.primary or ""
                    )
                    data_version_after = connection.execute(
                        "PRAGMA data_version"
                    ).fetchone()
                    after = dict(_metadata(self.paths.metadata))
                    if (
                        self._store.storage_stamp() != sealed_stamp
                        or repository_after != repository
                        or data_version_after != data_version
                        or before != after
                        or _BUILD_WORKERS.is_active(self._worker_domain_key)
                    ):
                        return self._empty_context_response(
                            request,
                            {**context_guarded, "hint": _INDEX_HINT},
                        )
        except Timeout:
            return self._empty_context_response(
                request, {**context_guarded, **self._busy_response()}
            )
        except CodeGraphStoreError:
            return self._empty_context_response(
                request,
                {**context_guarded, **_typed_failure(CodeGraphStoreFailure())},
            )
        except CodeGraphError as exc:
            return self._empty_context_response(
                request, {**context_guarded, **sanitized_error(exc)}
            )
        except Exception:
            duration_ms = max(0, int((time.monotonic() - started) * 1000))
            LOGGER.error(
                "code_graph_context code=rebuild_failed count=1 duration_ms=%d",
                duration_ms,
            )
            return self._empty_context_response(
                request, {**context_guarded, **_rebuild_failed()}
            )
        source_unavailable = bool(
            {"source_unavailable", "source_changed"} & set(response["warnings"])
        )
        return {
            "domain": guarded.get("domain"),
            "state": guarded.get("state"),
            "revision": guarded_revision,
            "fresh": not source_unavailable,
            **response,
            "warnings": list(dict.fromkeys([
                *guarded.get("warnings", []),
                *response["warnings"],
            ])),
        }


__all__ = [
    "CodeGraphRuntime",
    "sanitized_error",
    "shutdown_code_graph_workers",
]
