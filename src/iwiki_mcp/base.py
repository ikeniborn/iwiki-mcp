"""Wiki base, domain, and project binding helpers."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Any

from . import ignore
from .postgres.config import ConfigError as PostgresConfigError
from .postgres.config import load_model_config, load_postgres_config
from .project_files import initialize_text_file
from .storage import GitBinding, PostgresBinding

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


class BaseError(RuntimeError):
    """Raised when wiki base binding cannot be resolved."""


_PROJECT_CONFIG_TEMPLATE = """\
# .iwiki.toml -- project binding and storage examples.
# The server creates this template only when the file is missing or empty.
# Uncomment one storage example, replace placeholders, then edit manually.

# --- Git storage (default) ---
# base = "/absolute/path/to/iwiki-base"
# read = ["project-domain", "shared-domain"]
# write = ["project-domain"]
# primary = "project-domain"
# [storage]
# type = "git"

# --- PostgreSQL storage ---
# PostgreSQL requires non-empty read/write, primary, and every storage field.
# Keep passwords and model credentials in environment variables.
# read = ["project-domain", "shared-domain"]
# write = ["project-domain"]
# primary = "project-domain"
# [storage]
# type = "postgres"
# host = "db.internal.example"
# port = 5432
# database = "iwiki"
# user = "iwiki_app"
# sslmode = "verify-full"
# iwiki_id = "team-wiki"

# --- Optional local code graph ---
# [code_graph]
# enabled = true
# languages = ["python"]
# auto_rebuild = "bounded"  # "bounded" or "off"
# max_rebuild_seconds = 10
# max_file_bytes = 1000000
# max_total_files = 20000
# include_tests = true
# exclude = []
"""


@dataclass(frozen=True)
class Binding(GitBinding):
    """Backward-compatible name for the Git storage binding."""


def resolve_project_dir(explicit: str | None = None) -> str:
    project_dir = explicit or os.environ.get("IWIKI_PROJECT_DIR") or os.getcwd()
    return os.path.abspath(os.path.expanduser(project_dir))


def ensure_project_config(project_dir: str) -> bool:
    """Fill .iwiki.toml only when it is missing or contains whitespace."""
    config_path = os.path.join(project_dir, ".iwiki.toml")
    return initialize_text_file(config_path, _PROJECT_CONFIG_TEMPLATE)


def write_project_config(
    project_dir: str,
    read: list[str] | tuple[str, ...] | None = None,
    write: list[str] | tuple[str, ...] | None = None,
    primary: str | None = None,
) -> None:
    """Reject legacy requests to rewrite project configuration automatically."""
    del read, write, primary
    ensure_project_config(resolve_project_dir(project_dir))
    raise BaseError("project configuration cannot be changed automatically")


def load_project_config(project_dir: str) -> dict[str, Any]:
    ensure_project_config(project_dir)
    ignore.ensure_iwikiignore(project_dir)
    config_path = os.path.join(project_dir, ".iwiki.toml")
    try:
        with open(config_path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_storage_project_config(project_dir: str) -> dict[str, Any]:
    ensure_project_config(project_dir)
    ignore.ensure_iwikiignore(project_dir)
    config_path = os.path.join(project_dir, ".iwiki.toml")
    if not os.path.exists(config_path):
        return {}
    try:
        with open(config_path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise BaseError("project configuration could not be loaded") from exc
    if not isinstance(data, dict):
        raise BaseError("project configuration must be a TOML table")
    return data


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        item = value.strip()
        return (item,) if item else ()
    try:
        return tuple(str(item).strip() for item in value if str(item).strip())
    except TypeError:
        item = str(value).strip()
        return (item,) if item else ()


def _unique_str_tuple(value: Any) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for item in _as_str_tuple(value):
        if item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)


def _resolved_write_domains(
    wiki_base: str,
    read: tuple[str, ...],
    write: tuple[str, ...],
    primary: str | None,
) -> tuple[str, ...]:
    if primary is not None and primary not in write:
        raise BaseError("primary domain must belong to write")
    for domain in write:
        if not domain_exists(wiki_base, domain):
            raise BaseError(f"write domain '{domain}' not found")
        if read and domain not in read:
            raise BaseError(f"write domain '{domain}' is outside read scope")
    return write


def writable_domains(binding: Binding | PostgresBinding) -> tuple[str, ...]:
    return binding.write


def write_scope_error(binding: Binding | PostgresBinding, domain: str) -> dict | None:
    if domain in writable_domains(binding):
        return None
    return {
        "error": f"domain '{domain}' is outside bound write scope",
        "hint": "add the domain to write in .iwiki.toml manually before mutating it",
    }


def current_project_domain(project_dir: str) -> str:
    project_name = os.path.basename(os.path.normpath(resolve_project_dir(project_dir)))
    if not project_name:
        raise BaseError("cannot infer current project domain")
    return project_name


def merge_read_scope(
    existing: list[str] | tuple[str, ...] | None,
    requested: list[str] | tuple[str, ...] | None,
    current_domain: str,
) -> tuple[tuple[str, ...], str | None]:
    existing_read = _as_str_tuple(existing)
    requested_read = _as_str_tuple(requested)
    if not existing_read:
        return requested_read, None

    for domain in requested_read:
        if domain not in existing_read and domain != current_domain:
            return existing_read, "read scope is protected"

    if current_domain in requested_read and current_domain not in existing_read:
        return (*existing_read, current_domain), None
    return existing_read, None


def _postgres_binding(
    cfg: dict[str, Any], resolved_project_dir: str, storage: dict[str, Any]
) -> PostgresBinding:
    for name in ("read", "write"):
        if not isinstance(cfg.get(name), list) or not cfg[name]:
            raise BaseError(f"{name} must be a non-empty array for postgres storage")
        if any(not isinstance(domain, str) for domain in cfg[name]):
            raise BaseError(f"{name} elements must be strings for postgres storage")
    read = _unique_str_tuple(cfg["read"])
    write = _unique_str_tuple(cfg["write"])
    if not read:
        raise BaseError("read must be a non-empty array for postgres storage")
    if not write:
        raise BaseError("write must be a non-empty array for postgres storage")
    primary_value = cfg.get("primary")
    if primary_value is None or (
        isinstance(primary_value, str) and not primary_value.strip()
    ):
        raise BaseError("primary is required for postgres storage")
    if not isinstance(primary_value, str):
        raise BaseError("primary must be a string for postgres storage")
    primary = primary_value.strip()
    if any(domain not in read for domain in write):
        raise BaseError("write scope must be a subset of read scope")
    if primary not in write:
        raise BaseError("primary domain must belong to write scope")
    iwiki_id = storage.get("iwiki_id")
    if not isinstance(iwiki_id, str) or not iwiki_id.strip():
        raise BaseError("storage.iwiki_id is required for local postgres storage")
    try:
        database = load_postgres_config(storage)
        models = load_model_config()
    except PostgresConfigError as exc:
        raise BaseError(str(exc)) from exc
    return PostgresBinding(
        host=database.host,
        port=database.port,
        database=database.database,
        user=database.user,
        sslmode=database.sslmode,
        password=database.password,
        iwiki_id=iwiki_id.strip(),
        read=read,
        write=write,
        primary=primary,
        project_dir=resolved_project_dir,
        embed_model=models.embed_model,
        embed_dimensions=models.embed_dimensions,
        rerank_model=models.rerank_model,
    )


def resolve_storage_binding(
    project_dir: str | None = None,
) -> Binding | PostgresBinding:
    resolved_project_dir = resolve_project_dir(project_dir)
    cfg = _load_storage_project_config(resolved_project_dir)
    storage = cfg.get("storage")
    if storage is not None and not isinstance(storage, dict):
        raise BaseError("storage must be a table")
    storage_type = "git" if storage is None else storage.get("type")
    if storage_type == "postgres":
        if set(cfg) - {"read", "write", "primary", "storage", "code_graph"}:
            raise BaseError("project configuration contains keys that are not allowed")
        code_graph = cfg.get("code_graph")
        if code_graph is not None and not isinstance(code_graph, dict):
            raise BaseError("code_graph must be a table")
        if isinstance(code_graph, dict) and set(code_graph) & {
            "iwiki_id", "read", "write", "primary"
        }:
            raise BaseError("code_graph cannot override postgres binding identity")
        return _postgres_binding(cfg, resolved_project_dir, storage)
    if storage_type != "git":
        raise BaseError(f"unsupported storage type: {storage_type!r}")

    raw_base = cfg.get("base") or os.environ.get("IWIKI_BASE_DIR", "")
    wiki_base = str(raw_base).strip()
    if not wiki_base:
        raise BaseError(
            "no wiki base configured: set IWIKI_BASE_DIR or add `base` to .iwiki.toml"
        )
    wiki_base = os.path.abspath(os.path.expanduser(wiki_base))

    raw_write = cfg.get("write")
    if isinstance(raw_write, str):
        raise BaseError("write must be an array of domains")
    write = _unique_str_tuple(raw_write)
    primary = cfg.get("primary") or None
    if primary is not None:
        primary = str(primary).strip() or None

    read = _as_str_tuple(cfg.get("read"))
    write = _resolved_write_domains(wiki_base, read, write, primary)
    ignore.ensure_iwikiignore(resolved_project_dir)

    return Binding(
        base=wiki_base,
        read=read,
        write=write,
        primary=primary,
        project_dir=resolved_project_dir,
    )


def resolve_binding(project_dir: str | None = None) -> Binding | PostgresBinding:
    return resolve_storage_binding(project_dir)


def domain_dir(base: str, domain: str) -> str:
    return os.path.join(base, domain)


def pages_dir(base: str, domain: str) -> str:
    return domain_dir(base, domain)


def index_path(base: str, domain: str) -> str:
    return os.path.join(domain_dir(base, domain), "index.jsonl")


def log_path(base: str, domain: str) -> str:
    return os.path.join(domain_dir(base, domain), "log.jsonl")


def ensure_graph_store_excluded(base: str) -> bool:
    """Best-effort exclusion of the base-local graph cache from Git."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-path", "info/exclude"],
            cwd=base,
            check=True,
            capture_output=True,
            text=True,
        )
        raw_path = result.stdout.strip()
        if not raw_path:
            return False
        exclude_path = raw_path
        if not os.path.isabs(exclude_path):
            exclude_path = os.path.join(base, exclude_path)
        os.makedirs(os.path.dirname(exclude_path), exist_ok=True)
        try:
            with open(exclude_path, encoding="utf-8") as fh:
                existing = fh.read()
        except FileNotFoundError:
            existing = ""
        pattern = "/.iwiki/"
        if pattern in existing.splitlines():
            return True
        with open(exclude_path, "a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(pattern + "\n")
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def migrate_store_location(base: str, domain: str) -> None:
    """Best-effort one-time move of a domain's store/log out of the legacy
    ``.iwiki/`` subdir to the domain root. Idempotent, never clobbers an existing
    root file, never raises. Removes the legacy dir only when it is left empty."""
    dom = domain_dir(base, domain)
    legacy = os.path.join(dom, ".iwiki")
    for name in ("index.jsonl", "log.jsonl"):
        old = os.path.join(legacy, name)
        new = os.path.join(dom, name)
        try:
            if os.path.isfile(old) and not os.path.exists(new):
                os.replace(old, new)
        except OSError:
            pass
    try:
        if os.path.isdir(legacy) and not os.listdir(legacy):
            os.rmdir(legacy)
    except OSError:
        pass


def domain_exists(base: str, domain: str) -> bool:
    return (
        bool(domain)
        and not domain.startswith(".")
        and "/" not in domain
        and "\\" not in domain
        and not os.path.isabs(domain)
        and os.path.isdir(domain_dir(base, domain))
    )


def list_domains(base: str) -> list[str]:
    try:
        names = os.listdir(base)
    except OSError:
        return []
    return sorted(
        name
        for name in names
        if not name.startswith(".") and os.path.isdir(os.path.join(base, name))
    )


def resolve_scope(
    binding: Binding, scope: str, domains: list[str] | tuple[str, ...] | None
) -> list[str]:
    if domains is not None:
        return list(_as_str_tuple(domains))
    if scope == "all":
        return list_domains(binding.base)
    return list(binding.read) if binding.read else list_domains(binding.base)
