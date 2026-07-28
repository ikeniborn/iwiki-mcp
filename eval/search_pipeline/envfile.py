from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import shlex
import subprocess
from collections.abc import Iterator

from iwiki_mcp.engine.config import Config


def load_env_file(path: str | Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if not value:
            values[key] = ""
            continue
        parts = shlex.split(value, posix=True)
        values[key] = parts[0] if parts else ""
    return values


@contextmanager
def apply_env_file(path: str | Path) -> Iterator[dict[str, str]]:
    values = load_env_file(path)
    previous = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update(values)
        yield values
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _git_root() -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    root = result.stdout.strip()
    return Path(root).resolve() if root else None


def _is_tracked(path: Path) -> bool:
    root = _git_root()
    if root is None:
        return False
    try:
        relative = path.resolve().relative_to(root)
    except ValueError:
        return False
    try:
        subprocess.run(
            ["git", "-C", str(root), "ls-files", "--error-unmatch", str(relative)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def validate_env_file_path(path: str | Path, out_dir: str | Path) -> dict:
    env_path = Path(path).resolve()
    out_path = Path(out_dir).resolve()
    errors: list[str] = []
    warnings: list[str] = []

    if not env_path.is_file():
        errors.append("env file not found")
    try:
        env_path.relative_to(out_path)
    except ValueError:
        pass
    else:
        errors.append("env file is inside output directory")
    if _is_tracked(env_path):
        warnings.append("env file appears tracked by git")
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def safe_config_fingerprint(cfg: Config) -> dict:
    return {
        "embed_model": cfg.embed_model,
        "dimensions": cfg.dimensions,
        "chunk_size": cfg.chunk_size,
        "chunk_overlap": cfg.chunk_overlap,
        "summary_max": cfg.summary_max,
        "top_k": cfg.top_k,
        "score_threshold": cfg.score_threshold,
        "graph_depth": cfg.graph_depth,
        "seed_top_k": cfg.seed_top_k,
        "bfs_top_k": cfg.bfs_top_k,
        "seed_threshold": cfg.seed_threshold,
        "write_seed_threshold": cfg.write_seed_threshold,
        "chat_model": cfg.chat_model,
        "search_mode": cfg.search_mode,
        "rerank_enabled": bool(cfg.rerank_model),
    }
