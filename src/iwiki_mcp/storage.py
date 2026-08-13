"""Immutable bindings for the configured wiki storage backend."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GitBinding:
    """Resolved local Git wiki binding."""

    base: str
    read: tuple[str, ...]
    write: tuple[str, ...] | str | None
    project_dir: str
    primary: str | None = None

    def __post_init__(self) -> None:
        raw_write = self.write
        if not raw_write:
            domains: tuple[str, ...] = ()
        else:
            values = (raw_write,) if isinstance(raw_write, str) else raw_write
            domains = tuple(
                dict.fromkeys(
                    text
                    for item in values
                    if (text := str(item).strip())
                )
            )
        object.__setattr__(self, "write", domains)
        if self.primary is None and domains:
            object.__setattr__(self, "primary", domains[0])

    @property
    def storage(self) -> str:
        return "git"


@dataclass(frozen=True)
class PostgresBinding:
    """Resolved local PostgreSQL wiki binding with immutable maximum scope."""

    host: str
    port: int
    database: str
    user: str
    sslmode: str
    iwiki_id: str
    read: tuple[str, ...]
    write: tuple[str, ...]
    primary: str
    project_dir: str
    embed_model: str
    embed_dimensions: int
    rerank_model: str
    password: str = field(repr=False)

    @property
    def storage(self) -> str:
        return "postgres"
