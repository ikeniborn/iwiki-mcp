"""One-shot local code-graph build and publication application service."""
from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
import os
import secrets
import time
from typing import Mapping

from iwiki_mcp import base as wiki_base
from iwiki_mcp.postgres.codegraph import PostgresCodeGraphStore
from iwiki_mcp.storage import GitBinding, PostgresBinding

from . import config as codegraph_config
from . import indexer as codegraph_indexer
from . import linking
from . import runtime as codegraph_runtime
from .languages import javascript, python, typescript
from .mcp_adapter import McpSnapshotPublisher, RemoteMcpTransport
from .models import CodeGraphError
from .publication import (
    PublicationSession,
    SnapshotPublisher,
    iter_snapshot_batches,
)


class CodeGraphApplicationError(CodeGraphError):
    code = "invalid_config"


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


def code_graph_adapter_factories(
    repository_id: str,
    config: codegraph_config.CodeGraphConfig | None = None,
) -> Mapping[str, codegraph_indexer.AdapterFactory]:
    def create_python_adapter(source_paths):
        return python.PythonAdapter(
            repository_id,
            source_paths,
            parser_version=_PYTHON_PARSER_VERSION,
        )

    def create_typescript_adapter(source_paths):
        return typescript.TypeScriptAdapter(
            repository_id,
            source_paths,
            parser_version=_TYPESCRIPT_PARSER_VERSION,
            type_boost_enabled=bool(
                config is not None and config.typescript_type_boost
            ),
        )

    def create_javascript_adapter(source_paths):
        return javascript.JavaScriptAdapter(
            repository_id,
            source_paths,
            parser_version=_TYPESCRIPT_PARSER_VERSION,
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
    }


def code_runtime(
    source: CodeGraphSourceContext,
) -> codegraph_runtime.CodeGraphRuntime:
    try:
        config = codegraph_config.load_code_graph_config(source.project_dir)
    except codegraph_config.CodeGraphConfigError:
        config = None
    runtime = codegraph_runtime.CodeGraphRuntime(
        source,
        adapter_factories=code_graph_adapter_factories(source.primary, config),
    )
    if runtime._indexer is not None and source.wiki_base is not None:
        runtime._indexer.wiki_selector_resolver = linking.WikiSelectorResolver(
            source.wiki_base
        )
    return runtime


def create_postgres_publisher(
    binding: PostgresBinding,
    owner_id: str,
    settings,
    *,
    lock_timeout_ms: int = 5000,
) -> PostgresCodeGraphStore:
    if binding.primary is None:
        raise CodeGraphApplicationError("primary domain is required")
    return PostgresCodeGraphStore(
        binding.connection_dsn(),
        binding.iwiki_id,
        binding.primary,
        owner_id,
        lock_timeout_ms=lock_timeout_ms,
        session_ttl_seconds=settings.publication_session_ttl_seconds,
        staging_retention_seconds=settings.staging_retention_seconds,
        staging_cleanup_limit=settings.staging_cleanup_limit,
        require_database_principal=True,
    )


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
) -> None:
    try:
        publisher.abort(session)
    except Exception:
        return


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
        if (
            finalized.get("state") != "ready"
            or not isinstance(snapshot_revision, str)
            or not snapshot_revision
        ):
            _abort_preserving_failure(publisher, session)
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
) -> CodeGraphPublishOutcome:
    started = time.monotonic()
    runtime = code_runtime(source_context(binding))
    config = runtime.config
    mode = None if config is None else config.publish_mode
    if config is not None:
        validate_target(binding, config.publish_mode)
    indexed = runtime.index(force=force, languages=languages)
    publication: dict[str, object] = {}
    if config is not None and indexed.get("state") == "ready":
        publisher = publisher_for(binding, config, environ=environ)
        if publisher is not None:
            publication = publish_snapshot(runtime, publisher, config)
    return CodeGraphPublishOutcome(
        publish_mode=mode,
        index=dict(indexed),
        publication=publication,
        duration_ms=max(0, int((time.monotonic() - started) * 1000)),
    )
