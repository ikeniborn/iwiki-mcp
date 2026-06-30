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
    return os.path.join(domain_dir(base, domain), ".iwiki", "index.jsonl")


def log_path(base: str, domain: str) -> str:
    return os.path.join(domain_dir(base, domain), ".iwiki", "log.jsonl")


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


def write_project_config(
    project_dir: str,
    read: list[str] | tuple[str, ...] | None = None,
    write: str | None = None,
) -> None:
    resolved_project_dir = resolve_project_dir(project_dir)
    os.makedirs(resolved_project_dir, exist_ok=True)
    existing = load_project_config(resolved_project_dir)

    lines: list[str] = []
    base_value = existing.get("base")
    if base_value:
        lines.append(f"base = {_toml_string(str(base_value))}")
    if read is not None:
        lines.append(f"read = {_toml_string_list(_as_str_tuple(read))}")
    if write is not None:
        lines.append(f"write = {_toml_string(write)}")

    config_path = os.path.join(resolved_project_dir, ".iwiki.toml")
    with open(config_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
        if lines:
            fh.write("\n")
