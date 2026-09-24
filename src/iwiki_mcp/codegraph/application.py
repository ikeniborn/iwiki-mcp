"""One-shot local code-graph build and publication application service."""
from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
import logging
import os
from pathlib import Path
import secrets
import subprocess
import threading
import time
from typing import Callable, Mapping

from iwiki_mcp import base as wiki_base
from iwiki_mcp.postgres.codegraph import (
    PostgresCodeGraphReader,
    PostgresCodeGraphStore,
)
from iwiki_mcp.storage import GitBinding, PostgresBinding

from . import config as codegraph_config
from . import indexer as codegraph_indexer
from . import linking
from . import maintenance
from . import runtime as codegraph_runtime
from .context import validate_context_request
from .languages import bash, javascript, python, typescript
from .mcp_adapter import (
    CodeGraphAdapterError,
    McpCodeGraphReader,
    McpSnapshotPublisher,
    RemoteMcpTransport,
)
from .models import CodeGraphError
from .publication import (
    PublicationSession,
    SnapshotPublisher,
    iter_snapshot_batches,
)
from .query import validate_search_request
from .store import _is_canonical_revision
from .sqlite_adapter import SqliteCodeGraphReader
from iwiki_mcp.specifications import (
    UnavailableSpecificationGraphResolver,
    normalized_graph_state,
)

LOGGER = logging.getLogger(__name__)


class CodeGraphApplicationError(CodeGraphError):
    code = "invalid_config"


class CodeGraphReadModeError(CodeGraphError):
    """Raised when `code_graph.read_mode` names an unreachable read target.

    `parameter` is what `runtime.sanitized_error` turns into the response's
    `field`, so the caller is told which configuration key to fix without
    ever seeing a DSN, endpoint, or token.
    """

    code = "invalid_config"
    parameter = "read_mode"


class CodeGraphPublishError(CodeGraphError):
    """Redacted CLI failure after the publication mode is known."""

    def __init__(
        self,
        publish_mode: str,
        category: str,
    ) -> None:
        super().__init__(category)
        self.publish_mode = publish_mode
        self.category = category


@dataclass(frozen=True)
class CodeGraphPublishOutcome:
    publish_mode: str | None
    index: dict[str, object]
    publication: dict[str, object] = field(default_factory=dict)
    duration_ms: int = 0

    @property
    def ready(self) -> bool:
        if self.index.get("state") != "ready":
            return False
        return (
            self.publish_mode == "sqlite"
            or self.publication.get("state") == "ready"
        )

    @property
    def snapshot_revision(self) -> str | None:
        value = (
            self.index.get("revision")
            if self.publish_mode == "sqlite"
            else self.publication.get("snapshot_revision")
        )
        return value if isinstance(value, str) else None

    def tool_result(self) -> dict[str, object]:
        if (
            self.publish_mode in (None, "sqlite")
            or self.index.get("state") != "ready"
        ):
            return dict(self.index)
        return {**self.index, "publication": dict(self.publication)}


@dataclass(frozen=True)
class CodeGraphSourceContext:
    base: str
    project_dir: str
    primary: str
    wiki_base: str | None


def validate_target(
    binding: GitBinding | PostgresBinding, publish_mode: str
) -> None:
    if publish_mode == "sqlite" and isinstance(binding, PostgresBinding):
        raise CodeGraphApplicationError(
            "sqlite publication requires a Git Wiki binding"
        )
    if publish_mode == "postgres" and not isinstance(binding, PostgresBinding):
        raise CodeGraphApplicationError(
            "postgres publication requires PostgreSQL storage"
        )
    if publish_mode not in {"sqlite", "postgres", "mcp"}:
        raise CodeGraphApplicationError("unknown publish mode")


def source_context(
    binding: GitBinding | PostgresBinding,
) -> CodeGraphSourceContext:
    if binding.primary is None:
        raise CodeGraphApplicationError("primary domain is required")
    if isinstance(binding, PostgresBinding):
        if not wiki_base.ensure_graph_store_excluded(binding.project_dir):
            raise CodeGraphApplicationError(
                "local code graph cache exclusion is required"
            )
        return CodeGraphSourceContext(
            base=binding.project_dir,
            project_dir=binding.project_dir,
            primary=binding.primary,
            wiki_base=None,
        )
    return CodeGraphSourceContext(
        base=binding.base,
        project_dir=binding.project_dir,
        primary=binding.primary,
        wiki_base=binding.base,
    )


def _distribution_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unavailable"


_PYTHON_PARSER_VERSION = (
    "tree-sitter-python:" + _distribution_version("tree-sitter-python")
)
_TYPESCRIPT_PARSER_VERSION = (
    "tree-sitter-typescript:" + _distribution_version("tree-sitter-typescript")
)
_BASH_PARSER_VERSION = (
    "tree-sitter-bash:" + _distribution_version("tree-sitter-bash")
)


def code_graph_adapter_factories(
    repository_id: str,
    config: codegraph_config.CodeGraphConfig | None = None,
    *,
    config_getter: Callable[
        [], codegraph_config.CodeGraphConfig | None
    ] | None = None,
) -> Mapping[str, codegraph_indexer.AdapterFactory]:
    def create_python_adapter(source_paths):
        return python.PythonAdapter(
            repository_id,
            source_paths,
            parser_version=_PYTHON_PARSER_VERSION,
        )

    def create_typescript_adapter(source_paths):
        active_config = config_getter() if config_getter is not None else config
        return typescript.TypeScriptAdapter(
            repository_id,
            source_paths,
            parser_version=_TYPESCRIPT_PARSER_VERSION,
            type_boost_enabled=bool(
                active_config is not None
                and active_config.typescript_type_boost
            ),
        )

    def create_javascript_adapter(source_paths):
        return javascript.JavaScriptAdapter(
            repository_id,
            source_paths,
            parser_version=_TYPESCRIPT_PARSER_VERSION,
        )

    def create_bash_adapter(source_paths):
        return bash.BashAdapter(
            repository_id,
            source_paths,
            parser_version=_BASH_PARSER_VERSION,
        )

    return {
        "python": codegraph_indexer.AdapterFactory(
            create=create_python_adapter,
            extensions=(".py",),
            parser_version=_PYTHON_PARSER_VERSION,
            grammar_version=";".join((
                "tree-sitter:" + _distribution_version("tree-sitter"),
                "tree-sitter-language-pack:"
                + _distribution_version("tree-sitter-language-pack"),
                _PYTHON_PARSER_VERSION,
            )),
            adapter_version="python-adapter-v2",
        ),
        "typescript": codegraph_indexer.AdapterFactory(
            create=create_typescript_adapter,
            extensions=(".ts", ".tsx"),
            parser_version=_TYPESCRIPT_PARSER_VERSION,
            grammar_version=";".join((
                "tree-sitter:" + _distribution_version("tree-sitter"),
                "tree-sitter-language-pack:"
                + _distribution_version("tree-sitter-language-pack"),
                _TYPESCRIPT_PARSER_VERSION,
            )),
            adapter_version="typescript-adapter-v1",
        ),
        "javascript": codegraph_indexer.AdapterFactory(
            create=create_javascript_adapter,
            extensions=(".js", ".jsx", ".mjs", ".cjs"),
            parser_version=_TYPESCRIPT_PARSER_VERSION,
            grammar_version=";".join((
                "tree-sitter:" + _distribution_version("tree-sitter"),
                "tree-sitter-language-pack:"
                + _distribution_version("tree-sitter-language-pack"),
                _TYPESCRIPT_PARSER_VERSION,
            )),
            adapter_version="javascript-adapter-v1",
        ),
        "bash": codegraph_indexer.AdapterFactory(
            create=create_bash_adapter,
            extensions=(".sh",),
            parser_version=_BASH_PARSER_VERSION,
            grammar_version=";".join((
                "tree-sitter:" + _distribution_version("tree-sitter"),
                _BASH_PARSER_VERSION,
            )),
            adapter_version="bash-adapter-v1",
        ),
    }


def code_runtime(
    source: CodeGraphSourceContext,
    *,
    environ: Mapping[str, str] | None = None,
) -> codegraph_runtime.CodeGraphRuntime:
    runtime_holder: list[codegraph_runtime.CodeGraphRuntime] = []

    def current_config() -> codegraph_config.CodeGraphConfig | None:
        return runtime_holder[0].config if runtime_holder else None

    runtime = codegraph_runtime.CodeGraphRuntime(
        source,
        adapter_factories=code_graph_adapter_factories(
            source.primary,
            config_getter=current_config,
        ),
        environ=environ,
    )
    runtime_holder.append(runtime)
    if runtime._indexer is not None and source.wiki_base is not None:
        runtime._indexer.wiki_selector_resolver = linking.WikiSelectorResolver(
            source.wiki_base
        )
    return runtime


# Human message and hint for each machine token a snapshot reader puts in
# `error`. The hosted answers those readers were written for carry the token
# alone; spec R4 requires `error` + `code` + `hint` on every non-ready
# query-path answer, with `error` the message and `code` the token.
_PUBLISHED_NON_READY: dict[str, tuple[str, str]] = {
    "missing_snapshot": (
        "no code graph snapshot is published",
        "publish a code graph snapshot for this domain",
    ),
    "stale_snapshot": (
        "code graph snapshot is stale",
        "republish the code graph snapshot",
    ),
    "remote_mcp_failed": (
        "the remote code graph call failed",
        "retry the remote code graph call",
    ),
}
_PUBLISHED_NON_READY_DEFAULT = (
    "code graph is not ready for this query",
    "inspect wiki_code_status and retry",
)


def _normalized_published_answer(answer: dict[str, object]) -> dict[str, object]:
    """Give a snapshot reader's non-ready answer the R4 error/code/hint shape.

    The wrapped readers report a machine token in `error` and carry no
    `code` -- the opposite of the runtime, whose `error` is the human
    message. S6 made those answers reachable from a local server through
    one `read_mode` key, so without this the same tool returns two
    incompatible non-ready shapes on one machine. An answer that already
    names its `code` (a hosted answer relayed through the MCP transit
    reader) is compliant and passes through untouched.
    """
    token = answer.get("error")
    if "code" in answer or not isinstance(token, str):
        return answer
    message, hint = _PUBLISHED_NON_READY.get(token, _PUBLISHED_NON_READY_DEFAULT)
    normalized = {**answer, "error": message, "code": token}
    normalized.setdefault("hint", hint)
    return normalized


class PublishedSnapshotReader:
    """Answer the three read tools from a published snapshot.

    Adapts a snapshot reader -- the direct PostgreSQL one or the remote MCP
    transit one -- to the argument shape `CodeGraphRuntime` exposes, so the
    server's local dispatch is one call site per tool whatever `read_mode`
    selected. It never indexes and never runs the runtime's rebuild guard:
    freshness is whatever the published snapshot itself reports.

    The hosted `PostgresBinding` dispatch in `server.py` does not go through
    this adapter and keeps its own answer shape, which hosted clients pin.
    """

    def __init__(
        self,
        reader,
        config: codegraph_config.CodeGraphConfig,
        *,
        snapshot_scoped_languages: bool = False,
    ) -> None:
        self._reader = reader
        self._config = config
        self._snapshot_scoped_languages = snapshot_scoped_languages

    def status(self) -> dict[str, object]:
        return _normalized_published_answer(self._reader.status())

    def search(
        self,
        query: str,
        *,
        kinds: list[str] | None = None,
        path: str | None = None,
        languages: list[str] | None = None,
        limit: int = 20,
    ) -> dict[str, object]:
        """Search the published snapshot under the caller's own filters.

        The published snapshot, not this project's `code_graph.languages`,
        declares the unfiltered language scope. A reader that can report
        that scope gets the builder form the hosted dispatch uses; the
        remote transit reader instead omits the key entirely (see
        `McpCodeGraphReader.search`) so the remote applies its own.
        """
        def build(configured: tuple[str, ...], source: str):
            return validate_search_request(
                query,
                kinds=kinds,
                path=path,
                languages=languages,
                configured_languages=configured,
                languages_source=source,
                limit=limit,
            )

        if self._snapshot_scoped_languages:
            return _normalized_published_answer(self._reader.search(
                lambda snapshot_languages: build(snapshot_languages, "snapshot")
            ))
        return _normalized_published_answer(
            self._reader.search(build(self._config.languages, "config"))
        )

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
        return _normalized_published_answer(self._reader.context(
            validate_context_request(
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
        ))


def code_reader(binding: GitBinding | PostgresBinding):
    """Select the one reader `code_graph.read_mode` names, with no fallback.

    The runtime is built first because it owns the single configuration
    load (see `code_runtime`); only `read_mode = "sqlite"` -- the default --
    goes on to query it, so that path stays exactly what shipped before
    this key routed anything. A mode whose prerequisite is absent raises
    `CodeGraphReadModeError` rather than degrading to another target.
    """
    runtime = code_runtime(source_context(binding))
    config = runtime.config
    if config is None or not config.enabled or config.read_mode == "sqlite":
        return runtime
    if config.read_mode == "postgres":
        if not isinstance(binding, PostgresBinding):
            raise CodeGraphReadModeError(
                "postgres reads require PostgreSQL storage"
            )
        return PublishedSnapshotReader(
            PostgresCodeGraphReader(
                binding.connection_dsn(),
                binding.iwiki_id,
                binding.primary,
                max_snapshot_age_seconds=config.max_snapshot_age_seconds,
            ),
            config,
            snapshot_scoped_languages=True,
        )
    try:
        transport = RemoteMcpTransport(primary=binding.primary)
    except CodeGraphAdapterError as exc:
        raise CodeGraphReadModeError(
            "remote reads require a code graph endpoint and token"
        ) from exc
    return PublishedSnapshotReader(McpCodeGraphReader(transport), config)


def specification_graph_resolver(
    runtime: codegraph_runtime.CodeGraphRuntime,
):
    """Compose a private local specification resolver without widening runtime."""
    config = runtime.config
    if config is not None and config.publish_mode != "sqlite":
        return UnavailableSpecificationGraphResolver("source_unavailable")
    status = normalized_graph_state(runtime.status())
    if (
        status["state"] != "ready"
        or runtime.paths is None
        or runtime._store is None
        or config is None
    ):
        return UnavailableSpecificationGraphResolver(
            str(status["reason"] or "missing"),
            status["revision"] if isinstance(status["revision"], str) else None,
        )
    return SqliteCodeGraphReader(
        store=runtime._store,
        domain=runtime.binding.primary,
        private_root=runtime._context_root,
        lock_path=runtime.paths.lock,
        max_file_bytes=config.max_file_bytes,
        selector_resolver=linking.WikiSelectorResolver(runtime.binding.base),
    )


def create_postgres_publisher(
    binding: PostgresBinding,
    owner_id: str,
    settings,
    *,
    lock_timeout_ms: int = 5000,
    domain: str | None = None,
    connection_factory=None,
    schedule_local_cleanup: bool = False,
) -> PostgresCodeGraphStore:
    """Build one store for exactly one domain.

    `schedule_local_cleanup` is for `publisher_for`'s direct
    (`publish_mode = "postgres"`) publisher only: that path runs with no
    hosted server and therefore no request to sweep on its behalf, so its
    store schedules its own R7 fallback from `begin`. Every other caller
    (the hosted per-request store, and the store a maintenance worker
    builds to run one cleanup job) already has its sweep triggered
    elsewhere and must leave this off, or cleanup would double-schedule.
    """
    target = domain or binding.primary
    if target is None:
        raise CodeGraphApplicationError("primary domain is required")
    kwargs = dict(
        lock_timeout_ms=lock_timeout_ms,
        session_ttl_seconds=settings.publication_session_ttl_seconds,
        staging_retention_seconds=settings.staging_retention_seconds,
        staging_cleanup_limit=settings.staging_cleanup_limit,
        superseded_retention_seconds=getattr(
            settings, "superseded_retention_seconds", 86400
        ),
        superseded_cleanup_limit=getattr(
            settings, "superseded_cleanup_limit", 2
        ),
        connection_factory=connection_factory,
        require_database_principal=True,
    )
    if schedule_local_cleanup:
        kwargs["cleanup_binding"] = binding
        kwargs["cleanup_settings"] = settings
    return PostgresCodeGraphStore(
        binding.connection_dsn(),
        binding.iwiki_id,
        target,
        owner_id,
        **kwargs,
    )


_SWEEP_LOCK = threading.Lock()
# Any authenticated request may kick a sweep, so the floor keeps an idle wiki
# from re-sweeping continuously: without it the next request after a sweep
# finishes would start another one.
_SWEEP_MIN_INTERVAL_SECONDS = 900.0
_SWEEP_LAST: dict[str, float] = {}


def cleanup_sweep_due(iwiki_id: str) -> bool:
    """Cheap enough to ask on every request: a lock and a float compare.

    Deliberately not a timer. A background schedule would have no request, no
    binding and no token, so it would have to act with the service role's
    whole reach -- substituting connection privileges for a mandate, which is
    exactly what the per-domain store design rejects. A request already
    carries the mandate the sweep needs.

    In-flight deduplication now lives in the maintenance queue, so this
    function answers one question only: has this wiki been queued recently.
    """
    now = time.monotonic()
    with _SWEEP_LOCK:
        last = _SWEEP_LAST.get(iwiki_id)
        due = last is None or now - last >= _SWEEP_MIN_INTERVAL_SECONDS
    if not due:
        LOGGER.debug("code graph cleanup skipped, inside the throttle interval")
    return due


def run_cleanup_job(job, connection_factory) -> int:
    """Run one domain's cleanup cycle under that domain's own store.

    A store cleans only the domain it was built for and validated against,
    so one job is one store: widening a store's reach would let it delete
    rows in a domain whose principal it never checked. `binding.write` is the
    mandate -- exactly the domains this caller may already write -- not
    everything the connection happens to see.
    """
    store = create_postgres_publisher(
        job.binding,
        job.owner_id,
        job.settings,
        lock_timeout_ms=job.lock_timeout_ms,
        domain=job.domain,
        connection_factory=connection_factory,
    )
    if connection_factory is None:
        return store.run_cleanup_cycle()
    with connection_factory() as connection:
        return store.run_cleanup_cycle(connection)


# Local-only dedup for the stdio fallback: no MaintenanceRuntime exists on
# that path, so this is what keeps a burst of requests for the same domain
# from spawning one thread each -- the `_cleanup_active` property the
# deleted class-scoped guard used to hold, kept in the one place that still
# spawns a thread per job. Guarded by `_SWEEP_LOCK` rather than a lock of
# its own: one lock, not a second guard mechanism.
_LOCAL_CLEANUP_ACTIVE: set[tuple[str, str]] = set()


def _run_local_cleanup(job) -> bool:
    """The stdio path: no pool exists, so one daemon thread per job.

    Bounded by construction rather than by a queue -- one stdio process
    serves one client -- and deduplicated through `_LOCAL_CLEANUP_ACTIVE`,
    the same `(iwiki_id, domain)` key set `MaintenanceRuntime._scheduled`
    plays on the hosted path. Returns False, starting no thread, when this
    job's key is already running; the caller counts only what it started.
    """
    key = job.key
    with _SWEEP_LOCK:
        if key in _LOCAL_CLEANUP_ACTIVE:
            LOGGER.debug(
                "code graph cleanup already running for this domain"
            )
            return False
        _LOCAL_CLEANUP_ACTIVE.add(key)

    def run() -> None:
        try:
            run_cleanup_job(job, None)
        except Exception as exc:  # noqa: BLE001 - maintenance must not escape
            rows = getattr(exc, "rows_removed", None)
            LOGGER.warning(
                "code graph cleanup failed for one domain, "
                "%s rows removed before the failure: %s",
                "unknown" if rows is None else rows,
                type(exc).__name__,
            )
        finally:
            with _SWEEP_LOCK:
                _LOCAL_CLEANUP_ACTIVE.discard(key)

    threading.Thread(
        target=run, name="iwiki-code-graph-cleanup", daemon=True
    ).start()
    return True


def schedule_wiki_cleanup(
    binding: PostgresBinding,
    owner_id: str,
    settings,
    *,
    lock_timeout_ms: int = 5000,
    runtime=None,
) -> int:
    """Queue one cleanup job per writable domain. Never blocks the caller.

    The publication or read that triggered this is already committed, and
    cleanup is maintenance rather than a precondition for work someone else
    succeeded at.
    """
    queued = 0
    for domain in binding.write:
        job = maintenance.CleanupJob(
            iwiki_id=binding.iwiki_id,
            domain=domain,
            binding=binding,
            owner_id=owner_id,
            settings=settings,
            lock_timeout_ms=lock_timeout_ms,
        )
        if runtime is not None:
            if runtime.submit(job):
                queued += 1
            continue
        if _run_local_cleanup(job):
            queued += 1
    with _SWEEP_LOCK:
        _SWEEP_LAST[binding.iwiki_id] = time.monotonic()
    LOGGER.info(
        "code graph cleanup queued %s of %s writable domains",
        queued,
        len(binding.write),
    )
    return queued


def publisher_for(
    binding: GitBinding | PostgresBinding,
    config,
    *,
    environ: Mapping[str, str] | None = None,
) -> SnapshotPublisher | None:
    validate_target(binding, config.publish_mode)
    if config.publish_mode == "sqlite":
        return None
    if config.publish_mode == "postgres":
        assert isinstance(binding, PostgresBinding)
        return create_postgres_publisher(
            binding,
            secrets.token_hex(16),
            config,
            schedule_local_cleanup=True,
        )
    return McpSnapshotPublisher(
        RemoteMcpTransport(
            environ=os.environ if environ is None else environ,
            primary=binding.primary,
        )
    )


def effective_batch_bounds(
    session: PublicationSession, config
) -> tuple[int, int]:
    """Use valid hosted bounds and fall back to local config bounds."""
    rows_limit = session.max_batch_rows
    if (
        not isinstance(rows_limit, int)
        or isinstance(rows_limit, bool)
        or not 1 <= rows_limit <= 5000
    ):
        rows_limit = config.max_batch_rows
    bytes_limit = session.max_batch_bytes
    if (
        not isinstance(bytes_limit, int)
        or isinstance(bytes_limit, bool)
        or not 1 <= bytes_limit <= 5_000_000
    ):
        bytes_limit = config.max_batch_bytes
    return rows_limit, bytes_limit


def _abort_preserving_failure(
    publisher: SnapshotPublisher, session: PublicationSession
) -> dict | None:
    try:
        return publisher.abort(session)
    except Exception:
        return None


def _activated_despite_the_failure(result: dict | None) -> bool:
    """Report whether an abort answered with a finished activation.

    A finalize the client could not read the answer of -- a timeout, a dropped
    connection -- may still have activated the snapshot on the target. The
    abort of an already terminal session replays that terminal result, which is
    the only way the client can tell "never ran" from "ran and finished".
    """
    return (
        isinstance(result, dict)
        and result.get("state") == "ready"
        and _is_canonical_revision(result.get("snapshot_revision"))
    )


def publish_snapshot(
    runtime: codegraph_runtime.CodeGraphRuntime,
    publisher: SnapshotPublisher,
    config,
) -> dict[str, object]:
    exported = runtime.export_snapshot()
    if isinstance(exported, dict):
        return exported
    header, rows = exported
    session = None
    try:
        opened = publisher.begin(header)
        if isinstance(opened, dict):
            return opened
        session = opened
        max_rows, max_bytes = effective_batch_bounds(session, config)
        for batch in iter_snapshot_batches(
            rows,
            max_rows=max_rows,
            max_bytes=max_bytes,
        ):
            accepted = publisher.publish_batch(session, batch)
            if accepted.get("accepted") is not True:
                _abort_preserving_failure(publisher, session)
                return accepted
        finalized = publisher.finalize(session)
        snapshot_revision = finalized.get("snapshot_revision")
        if finalized.get("state") != "ready":
            aborted = _abort_preserving_failure(publisher, session)
            if _activated_despite_the_failure(aborted):
                return aborted
            return finalized
        if not _is_canonical_revision(snapshot_revision):
            _abort_preserving_failure(publisher, session)
            return {"state": "failed", "error": "publication_failed"}
        return finalized
    except Exception:
        if session is not None:
            _abort_preserving_failure(publisher, session)
        raise


def index_and_publish(
    binding: GitBinding | PostgresBinding,
    *,
    force: bool = False,
    languages: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
    redact_failures: bool = False,
    wait_seconds: float | None = None,
) -> CodeGraphPublishOutcome:
    """Build the project's graph and publish the snapshot it produced.

    The build publishes itself, through the callback below: a caller that
    detached at its `wait_seconds` expiry is no longer here to publish for it,
    and a detached build that never published would leave the hosted snapshot
    stale with nothing anywhere reporting it. There is deliberately one
    publication path, not one per wait outcome -- this call reads the result
    out of what the build returned.
    """
    started = time.monotonic()
    runtime = code_runtime(
        source_context(binding),
        environ=environ,
    )
    if redact_failures and getattr(runtime, "_configuration_error", False):
        raise codegraph_config.CodeGraphConfigError(
            "code graph configuration is invalid"
        )
    config = runtime.config
    mode = None if config is None else config.publish_mode
    failure_category = None

    try:
        if config is not None:
            validate_target(binding, config.publish_mode)
        # Select the publisher on *this* thread, before the build starts.
        # Selecting it is `validate_target` plus object construction: no
        # connection is opened, no request is sent, no session begins --
        # `RemoteMcpTransport.__init__` reads two environment values and
        # `PostgresCodeGraphStore.__init__` stores a DSN and a factory it does
        # not call. So this keeps the invariant that matters (a build which
        # never reaches `ready` publishes nothing) while leaving the one
        # failure it *can* raise -- a missing endpoint or token, which is a
        # configuration error -- on the caller's thread, where it becomes an
        # `invalid_config` answer and the CLI's exit 2. Discovered on the
        # worker instead, it could only ever be recorded as a runtime
        # publication failure, and a deployment would retry forever against a
        # configuration error retrying cannot fix.
        publisher = (
            None
            if config is None
            else publisher_for(binding, config, environ=environ)
        )

        def publish() -> dict[str, object]:
            """Publish the snapshot the build just produced.

            Runs on the build worker's thread, after a `ready` build and
            before the job reports terminality. Installed only when a
            publisher exists, so reaching it always means there is something
            to publish to.
            """
            assert config is not None and publisher is not None
            return publish_snapshot(runtime, publisher, config)

        # `CodeGraphQueryError` (raised by `runtime.index`'s own `wait_seconds`
        # validation) is deliberately not caught here: it falls through to
        # the generic `except Exception` below like any other `CodeGraphError`
        # and reaches the caller as a raised exception when `redact_failures`
        # is `False`. The tool layer (`wiki_code_index`'s `_code_safe`
        # decorator) turns it into the same sanitized `invalid_config` answer
        # every other typed graph failure gets via `sanitized_error`.
        indexed = dict(runtime.index(
            force=force,
            languages=languages,
            wait_seconds=wait_seconds,
            publish=None if publisher is None else publish,
        ))
        publication: dict[str, object] = indexed.pop("publication", {})
    except (
        wiki_base.BaseError,
        codegraph_config.CodeGraphConfigError,
        CodeGraphApplicationError,
        CodeGraphAdapterError,
    ):
        if not redact_failures:
            raise
        failure_category = "configuration"
    except Exception:
        if not redact_failures:
            raise
        failure_category = "internal"
    if failure_category is not None:
        raise CodeGraphPublishError(mode, failure_category) from None
    return CodeGraphPublishOutcome(
        publish_mode=mode,
        index=dict(indexed),
        publication=publication,
        duration_ms=max(0, int((time.monotonic() - started) * 1000)),
    )


def checkout_root(value: str) -> Path:
    candidate = Path(value).absolute()
    try:
        result = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CodeGraphApplicationError(
            "project must be a Git checkout root"
        ) from exc
    root = Path(result.stdout.strip()).absolute()
    if root != candidate or candidate.is_symlink():
        raise CodeGraphApplicationError(
            "project must be a Git checkout root"
        )
    return root


def publish_project(
    project_dir: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> CodeGraphPublishOutcome:
    root = checkout_root(project_dir)
    binding = wiki_base.resolve_storage_binding(str(root), environ=environ)
    return index_and_publish(
        binding,
        environ=environ,
        redact_failures=True,
    )
