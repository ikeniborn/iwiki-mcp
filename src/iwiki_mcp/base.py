"""Wiki base, domain, and project binding helpers."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


class BaseError(RuntimeError):
    """Raised when wiki base binding cannot be resolved."""


@dataclass(frozen=True)
class Binding:
    base: str
    read: tuple[str, ...]
    write: str | None
    project_dir: str


def resolve_project_dir(explicit: str | None = None) -> str:
    project_dir = explicit or os.environ.get("IWIKI_PROJECT_DIR") or os.getcwd()
    return os.path.abspath(os.path.expanduser(project_dir))


def load_project_config(project_dir: str) -> dict[str, Any]:
    config_path = os.path.join(project_dir, ".iwiki.toml")
    if not os.path.exists(config_path):
        return {}
    try:
        with open(config_path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


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


def resolve_binding(project_dir: str | None = None) -> Binding:
    resolved_project_dir = resolve_project_dir(project_dir)
    cfg = load_project_config(resolved_project_dir)
    raw_base = cfg.get("base") or os.environ.get("IWIKI_BASE_DIR", "")
    wiki_base = str(raw_base).strip()
    if not wiki_base:
        raise BaseError(
            "no wiki base configured: set IWIKI_BASE_DIR or add `base` to .iwiki.toml"
        )
    wiki_base = os.path.abspath(os.path.expanduser(wiki_base))

    write = cfg.get("write") or None
    if write is not None:
        write = str(write).strip() or None

    return Binding(
        base=wiki_base,
        read=_as_str_tuple(cfg.get("read")),
        write=write,
        project_dir=resolved_project_dir,
    )


def domain_dir(base: str, domain: str) -> str:
    return os.path.join(base, domain)


def pages_dir(base: str, domain: str) -> str:
    return domain_dir(base, domain)


def index_path(base: str, domain: str) -> str:
    return os.path.join(domain_dir(base, domain), "index.jsonl")


def log_path(base: str, domain: str) -> str:
    return os.path.join(domain_dir(base, domain), "log.jsonl")


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
    return not domain.startswith(".") and os.path.isdir(domain_dir(base, domain))


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


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_string_list(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _core_config_lines(config: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if "base" in config and config["base"] is not None:
        lines.append(f"base = {_toml_string(str(config['base']))}")
    if "read" in config:
        lines.append(f"read = {_toml_string_list(_as_str_tuple(config.get('read')))}")
    if "write" in config and config["write"] is not None:
        lines.append(f"write = {_toml_string(str(config['write']))}")
    return lines


def _top_level_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", "[")):
        return None
    if "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip()


def _multiline_string_delimiter(value: str) -> str | None:
    stripped = value.lstrip()
    for delimiter in ('"""', "'''"):
        if stripped.startswith(delimiter) and stripped.count(delimiter) < 2:
            return delimiter
    return None


def _core_assignment_closed(value: str, delimiter: str | None) -> bool:
    if delimiter is not None:
        return value.count(delimiter) >= 2
    stripped = value.lstrip()
    if stripped.startswith("["):
        return value.count("[") <= value.count("]")
    return True


def _preserved_top_level_lines(lines: list[str]) -> list[str]:
    preserved: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        key = _top_level_key(line)
        if key not in {"base", "read", "write"}:
            preserved.append(line)
            i += 1
            continue

        value = line.split("=", 1)[1]
        delimiter = _multiline_string_delimiter(value)
        i += 1
        while i < len(lines) and not _core_assignment_closed(value, delimiter):
            value += "\n" + lines[i]
            i += 1
    return preserved


def _write_preserving_unknown_config(config_path: str, config: dict[str, Any]) -> None:
    try:
        with open(config_path, encoding="utf-8") as fh:
            original = fh.read().splitlines()
    except OSError:
        original = []

    first_table = next(
        (i for i, line in enumerate(original) if line.strip().startswith("[")),
        len(original),
    )
    prefix = _preserved_top_level_lines(original[:first_table])
    suffix = original[first_table:]
    lines = [*prefix, *_core_config_lines(config), *suffix]

    with open(config_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
        if lines:
            fh.write("\n")


def write_project_config(
    project_dir: str,
    read: list[str] | tuple[str, ...] | None = None,
    write: str | None = None,
) -> None:
    resolved_project_dir = resolve_project_dir(project_dir)
    os.makedirs(resolved_project_dir, exist_ok=True)
    config = dict(load_project_config(resolved_project_dir))
    if read is not None:
        config["read"] = list(_as_str_tuple(read))
    if write is not None:
        config["write"] = write

    config_path = os.path.join(resolved_project_dir, ".iwiki.toml")
    _write_preserving_unknown_config(config_path, config)
