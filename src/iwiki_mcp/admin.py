"""Explicit PostgreSQL administration, Git import, and rollback export CLI."""
from __future__ import annotations

import argparse
from datetime import date, datetime
import hashlib
import json
import logging
import os
import select
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Callable, Mapping, TextIO

import psycopg
from psycopg.conninfo import make_conninfo

from . import base as wiki_base
from .codegraph import application as _codegraph_application
from .codegraph.config import CodeGraphConfigError
from .codegraph.mcp_adapter import CodeGraphAdapterError
from .codegraph.publication import quota_resource
from .engine.config import Config
from .engine.config import ConfigError as _EngineConfigError
from .engine.config import (
    require_system1_connection,
    system1_guidance_settings,
    system1_record_settings,
)
from .engine.embed import EmbedError, embed_texts
from .postgres.auth import AuthStore, validate_domain_identifier
from .postgres.config import ConfigError, ServerConfig, load_server_config
from .postgres.migrations import (
    MigrationError,
    MigrationSettings,
    rollback_v5_compatibility,
    run_migrations,
)
from .postgres.policy import resolve_policy
from .postgres.store import (
    PostgresStore,
    _validate_identifier,
    inspect_runtime_principal,
    provision_runtime_grant,
    require_hosted_principal,
)


_embed = embed_texts
_ADMIN_COMMANDS = {
    "base", "code", "domain", "token", "principal", "schema", "serve"
}
_DOMAIN_MARKER = ".iwiki-domain"


class _StrictArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs) -> None:
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)


class _CodeUsageError(Exception):
    pass


class _CodeArgumentParser(_StrictArgumentParser):
    def error(self, _message: str) -> None:
        raise _CodeUsageError()


def _add_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config")


def _add_publish_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", required=True)
    parser.add_argument("--json", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume", action="store_true")
    mode.add_argument("--forget-resume", action="store_true")


def _build_code_publish_parser() -> argparse.ArgumentParser:
    parser = _CodeArgumentParser(prog="iwiki-mcp code publish")
    _add_publish_arguments(parser)
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = _StrictArgumentParser(prog="iwiki-mcp")
    parser.add_argument("--project")
    commands = parser.add_subparsers(
        dest="command", parser_class=_StrictArgumentParser
    )

    serve = commands.add_parser("serve")
    _add_config(serve)
    serve.add_argument(
        "--transport", choices=("streamable-http",), default="streamable-http"
    )

    code = commands.add_parser("code")
    code_commands = code.add_subparsers(
        dest="code_command",
        required=True,
        parser_class=_CodeArgumentParser,
    )
    publish = code_commands.add_parser("publish")
    _add_publish_arguments(publish)

    base = commands.add_parser("base")
    base_commands = base.add_subparsers(
        dest="base_command", required=True, parser_class=_StrictArgumentParser
    )
    for name in ("create", "show", "disable", "enable"):
        action = base_commands.add_parser(name)
        _add_config(action)
        action.add_argument("--iwiki", required=True)
        if name == "show":
            action.add_argument("--json", action="store_true")
    list_action = base_commands.add_parser("list")
    _add_config(list_action)
    list_action.add_argument("--json", action="store_true")
    for name in ("import-git", "export-git"):
        action = base_commands.add_parser(name)
        _add_config(action)
        action.add_argument("--iwiki", required=True)
        action.add_argument("--path", required=True)
        action.add_argument("--dry-run", action="store_true")
        action.add_argument("--json", action="store_true")

    domain = commands.add_parser("domain")
    domain_commands = domain.add_subparsers(
        dest="domain_command", required=True, parser_class=_StrictArgumentParser
    )
    domain_create = domain_commands.add_parser("create")
    _add_config(domain_create)
    domain_create.add_argument("--iwiki", required=True)
    domain_create.add_argument("--domain", required=True)

    token = commands.add_parser("token")
    token_commands = token.add_subparsers(
        dest="token_command", required=True, parser_class=_StrictArgumentParser
    )
    token_create = token_commands.add_parser("create")
    _add_config(token_create)
    token_create.add_argument("--iwiki", required=True)
    token_create.add_argument("--owner", required=True)
    token_create.add_argument("--read-domain", action="append", default=[])
    token_create.add_argument("--write-domain", action="append", default=[])
    token_create.add_argument("--hosted-principal", required=True)
    token_create.add_argument("--can-create-domain", action="store_true")
    token_list = token_commands.add_parser("list")
    _add_config(token_list)
    token_list.add_argument("--iwiki", required=True)
    token_list.add_argument("--json", action="store_true")
    token_revoke = token_commands.add_parser("revoke")
    _add_config(token_revoke)
    token_revoke.add_argument("--token-id", required=True)

    principal = commands.add_parser("principal")
    principal_commands = principal.add_subparsers(
        dest="principal_command", required=True, parser_class=_StrictArgumentParser
    )
    principal_grant = principal_commands.add_parser("grant")
    _add_config(principal_grant)
    principal_grant.add_argument("--principal", required=True)
    principal_grant.add_argument("--iwiki", required=True)
    principal_grant.add_argument("--read-domain", action="append", required=True)
    principal_grant.add_argument("--write-domain", action="append", default=[])
    principal_grant.add_argument("--runtime", choices=("hosted", "direct"), required=True)
    principal_grant.add_argument("--json", action="store_true")
    publication_pair = principal_commands.add_parser("publication-pair")
    _add_config(publication_pair)
    publication_pair.add_argument("--issuer", required=True)
    publication_pair.add_argument("--publisher", required=True)
    publication_pair.add_argument("--json", action="store_true")
    principal_inspect = principal_commands.add_parser("inspect")
    _add_config(principal_inspect)
    principal_inspect.add_argument("--principal", required=True)
    principal_inspect.add_argument("--json", action="store_true")

    schema = commands.add_parser("schema")
    schema_commands = schema.add_subparsers(
        dest="schema_command", required=True, parser_class=_StrictArgumentParser
    )
    for name in ("rollback-v5-compat", "rollback-v11-compat"):
        rollback = schema_commands.add_parser(name)
        _add_config(rollback)
        rollback.add_argument("--confirm", action="store_true")
        rollback.add_argument("--json", action="store_true")

    for name in ("set-create-domain", "set-domain-management"):
        action = token_commands.add_parser(name)
        _add_config(action)
        action.add_argument("--iwiki", required=True)
        action.add_argument("--token-id", required=True)
        if name == "set-domain-management":
            action.add_argument("--domain", required=True)
        state = action.add_mutually_exclusive_group(required=True)
        state.add_argument("--enabled", action="store_true")
        state.add_argument("--disabled", action="store_true")
    return parser


def is_admin_command(argv: list[str]) -> bool:
    return bool(argv and argv[0] in _ADMIN_COMMANDS)


def _dsn(config: ServerConfig) -> str:
    storage = config.storage
    return make_conninfo(
        host=storage.host,
        port=storage.port,
        dbname=storage.database,
        user=storage.user,
        password=storage.password,
        sslmode=storage.sslmode,
    )


def _integer_env(
    environ: Mapping[str, str], name: str, default: int
) -> int:
    try:
        return int(environ.get(name, str(default)))
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def _domain_identifier(value: str) -> str:
    return validate_domain_identifier(value)


def _engine_config(
    server_config: ServerConfig, environ: Mapping[str, str]
) -> Config:
    base_url = environ.get("IWIKI_LLM_BASE_URL", "").strip().rstrip("/")
    system1_shadow = environ.get("IWIKI_SYSTEM1_SHADOW", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    system1_base_url = (
        environ.get("IWIKI_SYSTEM1_BASE_URL", "").strip().rstrip("/")
    )
    system1_api_key = environ.get("IWIKI_SYSTEM1_KEY", "").strip()
    try:
        guidance, search_boost, min_confidence = system1_guidance_settings(
            environ.get
        )
        assign_type, decision_log = system1_record_settings(environ.get)
        require_system1_connection(
            system1_shadow or guidance or assign_type or search_boost > 0,
            system1_base_url,
            system1_api_key,
        )
    except _EngineConfigError as exc:
        raise ConfigError(str(exc)) from None
    return Config(
        base_url=base_url,
        api_key=environ.get("IWIKI_LLM_KEY", "").strip(),
        embed_model=server_config.models.embed_model,
        dimensions=server_config.models.embed_dimensions,
        chunk_size=_integer_env(environ, "IWIKI_CHUNK_SIZE", 512),
        chunk_overlap=_integer_env(environ, "IWIKI_CHUNK_OVERLAP", 64),
        summary_max=_integer_env(environ, "IWIKI_SUMMARY_MAX_CHARS", 400),
        top_k=_integer_env(environ, "IWIKI_TOP_K", 8),
        score_threshold=float(environ.get("IWIKI_SCORE_THRESHOLD", "0.2")),
        graph_depth=_integer_env(environ, "IWIKI_GRAPH_DEPTH", 2),
        ignore=None,
        seed_top_k=_integer_env(environ, "IWIKI_SEED_TOP_K", 5),
        bfs_top_k=_integer_env(environ, "IWIKI_BFS_TOP_K", 10),
        seed_threshold=float(environ.get("IWIKI_SEED_THRESHOLD", "0.15")),
        write_seed_threshold=float(
            environ.get("IWIKI_WRITE_SEED_THRESHOLD", "0.35")
        ),
        rerank_model=server_config.models.rerank_model,
        system1_shadow=system1_shadow,
        system1_base_url=system1_base_url,
        system1_api_key=system1_api_key,
        system1_model=environ.get("IWIKI_SYSTEM1_MODEL", "").strip(),
        system1_guidance=guidance,
        system1_search_boost=search_boost,
        system1_min_confidence=min_confidence,
        system1_assign_type=assign_type,
        system1_decision_log=decision_log,
    )


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _write_json(stream: TextIO, value) -> None:
    print(
        json.dumps(value, sort_keys=True, default=_json_default),
        file=stream,
    )


def _write_code_json(stream: TextIO, value) -> None:
    print(json.dumps(value, separators=(",", ":")), file=stream)


_CODE_STDERR_DRAIN_SECONDS = 0.25


def _write_code_stderr(stream: TextIO, line: str, detached: threading.Event) -> bool:
    """Write one CLI line without letting a broken stderr replace the command's outcome."""
    try:
        if stream is sys.__stderr__ and os.name == "posix":
            # A daemon blocked inside the interpreter's stderr wrapper can
            # prevent shutdown. Write directly for every inherited descriptor.
            fd = stream.fileno()
            if hasattr(select, "poll"):
                writable = select.poll()
                writable.register(fd, select.POLLOUT)
                while not writable.poll(50):
                    if detached.is_set():
                        return False
            if detached.is_set():
                return False
            data = line.encode("ascii")
            return os.write(fd, data) == len(data)
        stream.write(line)
        stream.flush()
        return True
    except Exception:
        return False


def _write_code_failure(
    stream: TextIO,
    *,
    json_output: bool,
    publish_mode: str | None,
    error: str,
    duration_ms: int,
    checkpoint_failure: bool = False,
    export_busy: bool = False,
    resource: str | None = None,
) -> None:
    if json_output:
        _write_code_json(
            stream,
            {
                "state": "failed",
                "publish_mode": publish_mode,
                "error": error,
                "duration_ms": duration_ms,
                **({"code": "publication_quota_exceeded", "resource": resource}
                   if resource else {}),
                **({"code": "busy"} if export_busy else {}),
                **({"reason": "parse_checkpoint_storage_failed"} if checkpoint_failure else {}),
            },
        )
        return
    messages = {
        "invalid_usage": "invalid code publish usage",
        "invalid_config": "code graph configuration failed",
        "index_failed": "code graph indexing failed",
        "publication_failed": "code graph publication failed",
        "internal_error": "code graph publication failed",
        "resume_busy": "code graph or publication checkpoint is busy",
        "resume_expired": "the recovery window expired; check the active snapshot",
        "resume_conflict": "publication state conflicts with the pinned upload",
        "resume_incompatible": (
            "check peer and checkpoint compatibility. For an incompatible checkpoint, "
            "check the active snapshot before using --forget-resume to start a fresh upload"
        ),
        "resume_invalid_checkpoint": "the local recovery checkpoint failed verification",
        "resume_local_io_failed": "local storage I/O failed. Check storage and retry the command",
        "resume_target_mismatch": "the checkpoint belongs to a different publication target",
        "resume_access_denied": "the current publisher cannot access this attempt",
        "resume_transport_failed": "the peer is unavailable; retry with --resume",
        "resume_failed": "code graph recovery failed",
    }
    line = f"iwiki-mcp: {messages.get(error, 'code graph recovery failed')} (code={error})\n"
    if export_busy:
        line = "iwiki-mcp: local code graph is busy (code=publication_failed)\n"
    if resource:
        line = (f"iwiki-mcp: publication_quota_exceeded (resource={resource}, category={error}). "
                "Retry after capacity is available.\n")
    if checkpoint_failure:
        line = ("iwiki-mcp: parse_checkpoint_storage_failed: check local cache permissions "
                "and available space (code=index_failed)\n")
    detached = threading.Event()
    writer = threading.Thread(
        target=_write_code_stderr, args=(stream, line, detached),
        name="iwiki-code-diagnostic", daemon=True,
    )
    writer.start()
    writer.join(timeout=_CODE_STDERR_DRAIN_SECONDS)
    detached.set()


class _CodePublishProgress:
    """Render one invocation's job on a dedicated stderr consumer."""

    _COUNTERS = ("files_parsed", "files_total", "batches_acknowledged", "bytes_acknowledged")

    def __init__(self, stream: TextIO, *, clock: Callable[[], float] = time.monotonic):
        self._stream = stream
        self._clock = clock
        self._last_phase = None
        self._last_counts = None
        self._last_output = 0.0
        self._detached = threading.Event()

    def emit(self, job: dict) -> bool:
        progress = job["progress"]
        phase = (job["state"], progress["phase"])
        counts = {key: progress[key] for key in self._COUNTERS if key in progress}
        now = self._clock()
        if phase == self._last_phase and (
            counts == self._last_counts or now - self._last_output < 1
        ):
            return True
        fields = {
            "job": job["id"],
            "state": job["state"],
            "phase": progress["phase"],
            **counts,
            "updated_at": progress["updated_at"],
        }
        line = "code graph progress " + " ".join(f"{key}={value}" for key, value in fields.items())
        if not _write_code_stderr(self._stream, line + "\n", self._detached):
            return False
        self._last_phase = phase
        self._last_counts = counts
        self._last_output = self._clock()
        return True

    def observe(self, job):
        wake = threading.Event()

        def consume():
            cursor = 0
            while True:
                active = job
                if active is None:
                    return
                boundaries, latest = active.progress_observations(cursor)
                del active
                cursor += len(boundaries)
                for descriptor in (*boundaries, latest):
                    if job is None or not self.emit(descriptor):
                        return
                if latest["state"] != "running":
                    return
                wake.wait(0.05)

        consumer = threading.Thread(target=consume, name="iwiki-code-progress", daemon=True)
        consumer.start()

        def stop(completed: bool):
            nonlocal job
            if completed:
                wake.set()
                consumer.join(timeout=_CODE_STDERR_DRAIN_SECONDS)
            # A stalled custom stream can still own its current line. Drop
            # the worker and all future output before returning the result.
            job = None
            self._detached.set()
            wake.set()

        return stop


def _run_code_publish(
    args,
    env: Mapping[str, str],
    out: TextIO,
    err: TextIO,
) -> int:
    try:
        if args.forget_resume:
            result = _codegraph_application.forget_resume_project(args.project, environ=env)
            if args.json:
                _write_code_json(out, result)
            else:
                print("Local recovery state cleared." + (
                    " The remote outcome may still be unknown." if result["outcome_unknown"] else ""
                ), file=out)
            return 0
        publish = (
            _codegraph_application.resume_project if args.resume
            else _codegraph_application.publish_project
        )
        outcome = publish(
            args.project,
            environ=env,
            _observe_job=_CodePublishProgress(err).observe,
        )
    except _codegraph_application.CodeGraphResumeError as exc:
        _write_code_failure(out if args.json else err, json_output=args.json,
                            publish_mode=exc.publish_mode, error=exc.code, duration_ms=0,
                            resource=exc.quota_resource)
        return exc.exit_code
    except _codegraph_application.CodeGraphPublishError as exc:
        error, exit_code = {
            "configuration": ("invalid_config", 2),
            "internal": ("internal_error", 1),
        }[exc.category]
        _write_code_failure(
            out if args.json else err,
            json_output=args.json,
            publish_mode=exc.publish_mode,
            error=error,
            duration_ms=0,
        )
        return exit_code
    except (
        wiki_base.BaseError,
        CodeGraphConfigError,
        _codegraph_application.CodeGraphApplicationError,
        CodeGraphAdapterError,
    ):
        _write_code_failure(
            out if args.json else err,
            json_output=args.json,
            publish_mode=None,
            error="invalid_config",
            duration_ms=0,
        )
        return 2
    except psycopg.Error:
        _write_code_failure(
            out if args.json else err,
            json_output=args.json,
            publish_mode=None,
            error="internal_error",
            duration_ms=0,
        )
        return 1
    except Exception:
        _write_code_failure(
            out if args.json else err,
            json_output=args.json,
            publish_mode=None,
            error="internal_error",
            duration_ms=0,
        )
        return 1

    if not outcome.ready:
        error = (
            "index_failed"
            if outcome.index.get("state") != "ready"
            else "publication_failed"
        )
        checkpoint_failure = error == "index_failed" and (
            outcome.index.get("code") == "parse_checkpoint_storage_failed"
        )
        if args.json and checkpoint_failure:
            _write_code_failure(
                err, json_output=False, publish_mode=outcome.publish_mode, error=error,
                duration_ms=outcome.duration_ms, checkpoint_failure=True,
            )
        _write_code_failure(
            out if args.json else err,
            json_output=args.json,
            publish_mode=outcome.publish_mode,
            error=error,
            duration_ms=outcome.duration_ms,
            checkpoint_failure=checkpoint_failure,
            export_busy=(error == "publication_failed" and outcome.publication.get("code") == "busy"
                         and outcome.publication.get("error") == "code graph is busy"),
            resource=quota_resource(outcome.publication) if error == "publication_failed" else None,
        )
        return 1

    counts = outcome.index.get("counts", {})
    if args.json:
        _write_code_json(
            out,
            {
                "state": "ready",
                "publish_mode": outcome.publish_mode,
                "snapshot_revision": outcome.snapshot_revision,
                "counts": {
                    "files": counts.get("files", 0),
                    "symbols": counts.get("symbols", 0),
                    "relations": counts.get("relations", 0),
                },
                "duration_ms": outcome.duration_ms,
            },
        )
    else:
        print(
            f"code graph ready mode={outcome.publish_mode} "
            f"revision={outcome.snapshot_revision} "
            f"files={counts.get('files', 0)} "
            f"symbols={counts.get('symbols', 0)} "
            f"relations={counts.get('relations', 0)} "
            f"duration_ms={outcome.duration_ms}",
            file=out,
        )
    return 0


def _git_root(path: Path) -> Path:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("import path must be a Git repository root") from exc
    root = Path(result.stdout.strip()).resolve()
    if root != path.resolve():
        raise ValueError("import path must be a Git repository root")
    return root


def _scan_git(
    path: str,
) -> tuple[list[tuple[str, str, str]], tuple[str, ...], str]:
    root = Path(path)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("import path must be a directory")
    root = _git_root(root)
    pages = []
    fingerprint = hashlib.sha256()
    for candidate in sorted(root.rglob("*.md")):
        relative = candidate.relative_to(root)
        if relative.parts[0].startswith("."):
            continue
        if candidate.is_symlink() or len(relative.parts) < 2:
            raise ValueError("Git pages must be regular files inside a domain")
        domain = _domain_identifier(relative.parts[0])
        slug = _validate_identifier(
            Path(*relative.parts[1:]).with_suffix("").as_posix(),
            "page slug",
        )
        try:
            raw = candidate.read_bytes()
            markdown = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError("Git page could not be read as UTF-8") from exc
        authored_path = relative.as_posix().encode("utf-8")
        fingerprint.update(len(authored_path).to_bytes(8, "big"))
        fingerprint.update(authored_path)
        fingerprint.update(len(raw).to_bytes(8, "big"))
        fingerprint.update(raw)
        pages.append((domain, slug, markdown))
    domains = {page[0] for page in pages}
    manifest_path = root / ".iwiki-export.json"
    has_manifest = False
    if manifest_path.is_file() and not manifest_path.is_symlink():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
            raise ValueError("Git export manifest is invalid") from exc
        if (
            not isinstance(manifest, dict)
            or "domain_names" not in manifest
            or manifest.get("format_version") != 1
            or not isinstance(manifest["domain_names"], list)
        ):
            raise ValueError("Git export manifest is invalid")
        has_manifest = True
        domains.update(
            _domain_identifier(domain)
            for domain in manifest["domain_names"]
        )
    if not pages and not domains and not has_manifest:
        raise ValueError("Git repository contains no wiki pages")
    for domain in sorted(domains):
        fingerprint.update(b"domain\0")
        fingerprint.update(domain.encode("utf-8"))
    return pages, tuple(sorted(domains)), fingerprint.hexdigest()


def _destination(path: str, *, dry_run: bool) -> Path:
    destination = Path(path)
    if destination.is_symlink() or destination.is_file():
        raise ValueError("export destination must be absent or empty")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("export destination must be absent or empty")
    if not dry_run and not destination.parent.is_dir():
        raise ValueError("export destination parent does not exist")
    return destination


def _git_commit(destination: Path) -> None:
    commands = (
        ["git", "init", str(destination)],
        ["git", "-C", str(destination), "add", "."],
        [
            "git", "-C", str(destination),
            "-c", "user.name=iwiki-mcp",
            "-c", "user.email=iwiki-mcp@localhost",
            "commit", "-m", "iwiki: PostgreSQL rollback export",
        ],
    )
    try:
        for command in commands:
            subprocess.run(
                command,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("Git export commit failed") from exc


class AdminService:
    """Database administration bounded to validated server configuration."""

    def __init__(self, config: ServerConfig, engine_config: Config) -> None:
        self.config = config
        self.dsn = _dsn(config)
        self.engine_config = engine_config
        self.auth = AuthStore(self.dsn)

    def _store(self, iwiki_id: str) -> PostgresStore:
        valid_id = _validate_identifier(iwiki_id, "iwiki id")

        def _mode(domain: str) -> str:
            return resolve_policy(
                getattr(self.config, "specifications", None),
                getattr(self.config, "code_graph", None),
                valid_id,
                domain,
                None,
            ).value("specification_mode")

        return PostgresStore(
            self.dsn,
            valid_id,
            self.engine_config,
            embedder=_embed,
            specification_mode=_mode,
        )

    def create_base(self, iwiki_id: str) -> dict:
        iwiki_id = _validate_identifier(iwiki_id, "iwiki id")
        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO iwiki.iwikis (iwiki_id, slug) "
                    "VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING iwiki_id",
                    (iwiki_id, iwiki_id),
                )
                if cursor.fetchone() is None:
                    raise ValueError("wiki already exists")
        return {"created": iwiki_id}

    def list_bases(self) -> list[dict]:
        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT iwiki_id, active FROM iwiki.iwikis ORDER BY iwiki_id"
                )
                return [
                    {"iwiki": row[0], "active": row[1]}
                    for row in cursor.fetchall()
                ]

    def show_base(self, iwiki_id: str) -> dict:
        iwiki_id = _validate_identifier(iwiki_id, "iwiki id")
        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT active FROM iwiki.iwikis WHERE iwiki_id = %s",
                    (iwiki_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError("wiki not found")
                cursor.execute(
                    "SELECT "
                    "(SELECT count(*) FROM iwiki.domains WHERE iwiki_id = %s), "
                    "(SELECT count(*) FROM iwiki.pages WHERE iwiki_id = %s), "
                    "(SELECT count(*) FROM iwiki.tokens WHERE iwiki_id = %s)",
                    (iwiki_id, iwiki_id, iwiki_id),
                )
                domains, pages, tokens = cursor.fetchone()
        return {
            "iwiki": iwiki_id,
            "active": row[0],
            "domains": domains,
            "pages": pages,
            "tokens": tokens,
        }

    def set_base_active(self, iwiki_id: str, active: bool) -> dict:
        iwiki_id = _validate_identifier(iwiki_id, "iwiki id")
        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE iwiki.iwikis SET active = %s, "
                    "updated_at = CURRENT_TIMESTAMP WHERE iwiki_id = %s",
                    (active, iwiki_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("wiki not found")
        return {"iwiki": iwiki_id, "active": active}

    def create_domain(self, iwiki_id: str, domain: str) -> dict:
        iwiki_id = _validate_identifier(iwiki_id, "iwiki id")
        domain = _domain_identifier(domain)
        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO iwiki.domains (iwiki_id, slug) "
                    "VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING slug",
                    (iwiki_id, domain),
                )
                if cursor.fetchone() is None:
                    raise ValueError("wiki not found or domain already exists")
        return {"iwiki": iwiki_id, "created": domain}

    def grant_principal(
        self,
        principal: str,
        iwiki_id: str,
        read_domains: list[str],
        write_domains: list[str],
        runtime: str,
    ) -> dict:
        return provision_runtime_grant(
            self.dsn,
            principal=principal,
            iwiki_id=_validate_identifier(iwiki_id, "iwiki id"),
            read_domains=read_domains,
            write_domains=write_domains,
            runtime=runtime,
        )

    def create_token(
        self,
        iwiki_id: str,
        owner: str,
        *,
        read_domains: list[str],
        write_domains: list[str],
        hosted_principal: str,
        can_create_domain: bool = False,
    ) -> dict:
        iwiki_id = _validate_identifier(iwiki_id, "iwiki id")
        require_hosted_principal(
            self.dsn,
            principal=hosted_principal,
            iwiki_id=iwiki_id,
            read_domains=read_domains,
            write_domains=write_domains,
        )
        return self.auth.create_token(
            iwiki_id,
            owner,
            read_domains=read_domains,
            write_domains=write_domains,
            can_create_domain=can_create_domain,
        )

    def inspect_principal(self, principal: str) -> dict:
        return inspect_runtime_principal(self.dsn, principal)

    def migration_settings(self) -> MigrationSettings:
        return MigrationSettings(
            dsn=self.dsn,
            embed_model=self.config.models.embed_model,
            embed_dimensions=self.config.models.embed_dimensions,
            statement_timeout_ms=self.config.server.statement_timeout_ms,
            lock_timeout_ms=self.config.server.lock_timeout_ms,
        )

    def rollback_schema_v5(self, *, confirm: bool) -> dict:
        return rollback_v5_compatibility(self.migration_settings(), confirm=confirm)

    def import_git(self, iwiki_id: str, path: str, *, dry_run: bool) -> dict:
        if _embed is embed_texts and (
            not self.engine_config.base_url or not self.engine_config.api_key
        ):
            raise ConfigError(
                "IWIKI_LLM_BASE_URL and IWIKI_LLM_KEY are required for Git import"
            )
        pages, domains, fingerprint = _scan_git(path)
        return self._store(iwiki_id).import_pages(
            pages, fingerprint, domains=domains, dry_run=dry_run
        )

    def export_git(self, iwiki_id: str, path: str, *, dry_run: bool) -> dict:
        destination = _destination(path, dry_run=dry_run)
        snapshot = self._store(iwiki_id).export_snapshot()
        manifest = {
            "format_version": 1,
            "iwiki": snapshot["iwiki"],
            "domain_names": snapshot["domains"],
            **snapshot["counts"],
            "page_hashes": snapshot["page_hashes"],
        }
        if not dry_run:
            destination.mkdir(exist_ok=True)
            for domain in snapshot["domains"]:
                domain_path = destination / domain
                domain_path.mkdir(parents=True, exist_ok=True)
                (domain_path / _DOMAIN_MARKER).write_text(
                    "iwiki domain\n", encoding="utf-8"
                )
            for page in snapshot["pages"]:
                target = destination / page["domain"] / f"{page['slug']}.md"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(page["markdown"], encoding="utf-8")
            (destination / ".iwiki-export.json").write_text(
                json.dumps(manifest, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            _git_commit(destination)
        return {
            **manifest,
            "dry_run": dry_run,
            "exported": not dry_run,
        }


def _service(
    config_path: str,
    environ: Mapping[str, str],
    *,
    migrate: bool = True,
) -> AdminService:
    config = load_server_config(config_path, environ)
    dsn = _dsn(config)
    if migrate:
        run_migrations(
            MigrationSettings(
                dsn=dsn,
                embed_model=config.models.embed_model,
                embed_dimensions=config.models.embed_dimensions,
                statement_timeout_ms=config.server.statement_timeout_ms,
                lock_timeout_ms=config.server.lock_timeout_ms,
            )
        )
    return AdminService(config, _engine_config(config, environ))


def _config_path(args, environ: Mapping[str, str]) -> str:
    value = args.config or environ.get("IWIKI_SERVER_CONFIG", "")
    if not value:
        raise ConfigError("--config or IWIKI_SERVER_CONFIG is required")
    return value


def _dispatch(args, service: AdminService):
    if args.command == "base":
        if args.base_command == "create":
            return service.create_base(args.iwiki)
        if args.base_command == "list":
            return service.list_bases()
        if args.base_command == "show":
            return service.show_base(args.iwiki)
        if args.base_command == "disable":
            return service.set_base_active(args.iwiki, False)
        if args.base_command == "enable":
            return service.set_base_active(args.iwiki, True)
        if args.base_command == "import-git":
            return service.import_git(args.iwiki, args.path, dry_run=args.dry_run)
        if args.base_command == "export-git":
            return service.export_git(args.iwiki, args.path, dry_run=args.dry_run)
    if args.command == "domain" and args.domain_command == "create":
        return service.create_domain(args.iwiki, args.domain)
    if args.command == "token":
        if args.token_command == "create":
            return service.create_token(
                args.iwiki,
                args.owner,
                read_domains=args.read_domain,
                write_domains=args.write_domain,
                hosted_principal=args.hosted_principal,
                can_create_domain=args.can_create_domain,
            )
        if args.token_command == "list":
            return service.auth.list_tokens(args.iwiki)
        if args.token_command == "revoke":
            if not service.auth.revoke_token(args.token_id):
                raise ValueError("token not found or already revoked")
            return {"revoked": args.token_id}
        if args.token_command == "set-create-domain":
            return service.auth.set_create_domain(
                args.iwiki, args.token_id, args.enabled
            )
        if args.token_command == "set-domain-management":
            return service.auth.set_domain_management(
                args.iwiki,
                args.token_id,
                args.domain,
                args.enabled,
            )
    if args.command == "principal":
        if args.principal_command == "grant":
            return service.grant_principal(
                args.principal,
                args.iwiki,
                args.read_domain,
                args.write_domain,
                args.runtime,
            )
        if args.principal_command == "publication-pair":
            from .postgres.publication_roles import provision_publication_pair

            provision_publication_pair(
                service.dsn, args.issuer, args.publisher,
                service.config.code_graph.publication_limits, settings=service.config.code_graph,
            )
            return {"issuer": args.issuer, "publisher": args.publisher}
        if args.principal_command == "inspect":
            return service.inspect_principal(args.principal)
    if args.command == "schema" and args.schema_command == "rollback-v11-compat":
        from .postgres.migrations import rollback_v11_compatibility

        if not args.confirm:
            return {"dry_run": True, "target_schema_version": 10, "requires_quiescence": True}
        return rollback_v11_compatibility(service.migration_settings(), confirm=True)
    if args.command == "schema" and args.schema_command == "rollback-v5-compat":
        return service.rollback_schema_v5(confirm=args.confirm)
    raise ValueError("unsupported administration command")


def run(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if environ is None else environ
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    try:
        if argv[:2] == ["code", "publish"]:
            args = _build_code_publish_parser().parse_args(argv[2:])
            args.command = "code"
            args.code_command = "publish"
        else:
            args = build_parser().parse_args(argv)
    except _CodeUsageError:
        json_output = argv[:2] == ["code", "publish"] and "--json" in argv
        _write_code_failure(
            out if json_output else err,
            json_output=json_output,
            publish_mode=None,
            error="invalid_usage",
            duration_ms=0,
        )
        return 2
    is_code_publish = (
        args.command == "code" and args.code_command == "publish"
    )
    if args.command is not None and args.project is not None and not is_code_publish:
        print("iwiki-mcp: --project is accepted only for stdio", file=err)
        return 2
    if args.command is None:
        print("iwiki-mcp: administration command is required", file=err)
        return 2
    try:
        if is_code_publish:
            return _run_code_publish(args, env, out, err)
        if args.command == "serve":
            from .http import run_server

            run_server(_config_path(args, env), environ=env)
            return 0
        dry_run = (
            args.command == "base"
            and args.base_command in {"import-git", "export-git"}
            and args.dry_run
        )
        service = _service(
            _config_path(args, env),
            env,
            migrate=not dry_run and args.command != "schema",
        )
        result = _dispatch(args, service)
    except psycopg.Error:
        print("iwiki-mcp: database operation failed", file=err)
        return 1
    except EmbedError:
        print("iwiki-mcp: embedding operation failed", file=err)
        return 1
    except (ConfigError, MigrationError, ValueError) as exc:
        print(f"iwiki-mcp: {exc}", file=err)
        return 2 if isinstance(exc, ConfigError) else 1
    except RuntimeError:
        print("iwiki-mcp: administration operation failed", file=err)
        return 1
    if args.command == "token" and args.token_command == "create":
        print(result["token"], file=out)
    elif getattr(args, "json", False):
        _write_json(out, result)
    else:
        _write_json(out, result)
    return 0


def main() -> None:
    """Start one-shot publication before the MCP server configures stderr logging."""
    if sys.argv[1:3] == ["code", "publish"]:
        # Default logging's lastResort handler also writes synchronously to
        # stderr. The standalone CLI already reports job failures to the user.
        logger = logging.getLogger()
        quiet = logging.NullHandler()
        logger.addHandler(quiet)
        try:
            raise SystemExit(run(sys.argv[1:]))
        finally:
            logger.removeHandler(quiet)
    from .server import main as server_main
    server_main()
