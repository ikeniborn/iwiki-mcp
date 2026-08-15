"""Explicit PostgreSQL administration, Git import, and rollback export CLI."""
from __future__ import annotations

import argparse
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Mapping, TextIO

import psycopg
from psycopg.conninfo import make_conninfo

from .engine.config import Config
from .engine.embed import EmbedError, embed_texts
from .postgres.auth import AuthStore, validate_domain_identifier
from .postgres.config import ConfigError, ServerConfig, load_server_config
from .postgres.migrations import MigrationError, MigrationSettings, run_migrations
from .postgres.store import PostgresStore, _validate_identifier


_embed = embed_texts
_ADMIN_COMMANDS = {"base", "domain", "token", "serve"}
_DOMAIN_MARKER = ".iwiki-domain"


class _StrictArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs) -> None:
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)


def _add_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config")


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
    token_create.add_argument("--can-create-domain", action="store_true")
    token_list = token_commands.add_parser("list")
    _add_config(token_list)
    token_list.add_argument("--iwiki", required=True)
    token_list.add_argument("--json", action="store_true")
    token_revoke = token_commands.add_parser("revoke")
    _add_config(token_revoke)
    token_revoke.add_argument("--token-id", required=True)
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
        return PostgresStore(
            self.dsn,
            _validate_identifier(iwiki_id, "iwiki id"),
            self.engine_config,
            embedder=_embed,
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
            return service.auth.create_token(
                args.iwiki,
                args.owner,
                read_domains=args.read_domain,
                write_domains=args.write_domain,
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
    args = build_parser().parse_args(argv)
    if args.command is not None and args.project is not None:
        print("iwiki-mcp: --project is accepted only for stdio", file=err)
        return 2
    if args.command is None:
        print("iwiki-mcp: administration command is required", file=err)
        return 2
    try:
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
            _config_path(args, env), env, migrate=not dry_run
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
