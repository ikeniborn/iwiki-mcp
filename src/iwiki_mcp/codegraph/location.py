"""Fixed, base-local locations for code graph files."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from iwiki_mcp import base as wiki_base

from .models import CodeGraphError


class CodeGraphLocationError(CodeGraphError):
    """Raised when a code graph location would be unsafe."""


def _validate_domain(domain: str) -> str:
    if not domain:
        raise CodeGraphLocationError("invalid domain: empty")
    if domain.startswith(".") or "/" in domain or "\\" in domain:
        raise CodeGraphLocationError(f"invalid domain '{domain}'")
    if domain in (".", ".."):
        raise CodeGraphLocationError(f"invalid domain '{domain}'")
    if Path(domain).is_absolute() or PureWindowsPath(domain).is_absolute():
        raise CodeGraphLocationError(f"invalid domain '{domain}'")
    if PureWindowsPath(domain).drive:
        raise CodeGraphLocationError(f"invalid domain '{domain}'")
    return domain


@dataclass(frozen=True)
class CodeGraphPaths:
    database: Path
    wal: Path
    shm: Path
    lock: Path
    metadata: Path


class CodeGraphLocationResolver:
    def __init__(self, base: str, domain: str, project_dir: str) -> None:
        self.base = base
        self.domain = domain
        self.project_dir = project_dir

    def resolve(self) -> CodeGraphPaths:
        domain = _validate_domain(self.domain)
        base_path = Path(self.base).resolve()
        graph_dir = base_path / ".iwiki"
        database = graph_dir / f"code-{domain}.sqlite3"
        wiki_base.ensure_graph_store_excluded(self.base)
        return CodeGraphPaths(
            database=database,
            wal=Path(f"{database}-wal"),
            shm=Path(f"{database}-shm"),
            lock=graph_dir / f"code-{domain}.lock",
            metadata=graph_dir / f"code-{domain}.metadata.json",
        )
