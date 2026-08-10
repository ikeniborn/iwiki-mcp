"""Deterministic source, configuration, parser, and build fingerprints."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

from .config import CodeGraphConfig
from .discovery import SourceFile


_COMMIT = re.compile(r"^[0-9a-fA-F]{40,64}$")


@dataclass(frozen=True)
class FingerprintSet:
    source: str
    config: str
    parser: str
    inputs: str


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _source_rows(files: Iterable[SourceFile]) -> list[tuple[str, str]]:
    return sorted((item.path, item.content_hash) for item in files)


def source_fingerprint(files: Iterable[SourceFile]) -> str:
    """Hash exact canonical JSON rows of relative path and content hash."""
    return _hash(_source_rows(files))


def normalized_config(config: CodeGraphConfig) -> dict[str, Any]:
    """Return all fields canonically while preserving ordered ignore rules."""
    values = asdict(config)
    values["languages"] = sorted(set(values["languages"]))
    values["exclude"] = list(values["exclude"])
    return {key: values[key] for key in sorted(values)}


def config_fingerprint(config: CodeGraphConfig) -> str:
    return _hash(normalized_config(config))


def _parser_inputs(
    *,
    languages: Iterable[str],
    schema_version: int | str,
    parser_version: str = "",
    grammar_version: str,
    adapter_version: str,
    resolver_version: str,
) -> dict[str, Any]:
    return {
        "adapter_version": adapter_version,
        "grammar_version": grammar_version,
        "languages": sorted(set(languages)),
        "parser_version": parser_version,
        "resolver_version": resolver_version,
        "schema_version": schema_version,
    }


def parser_fingerprint(
    *,
    languages: Iterable[str],
    schema_version: int | str,
    parser_version: str = "",
    grammar_version: str,
    adapter_version: str,
    resolver_version: str,
) -> str:
    return _hash(
        _parser_inputs(
            languages=languages,
            schema_version=schema_version,
            parser_version=parser_version,
            grammar_version=grammar_version,
            adapter_version=adapter_version,
            resolver_version=resolver_version,
        )
    )


def _git_run(
    project: os.PathLike[str] | str, arguments: list[str]
) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=os.fspath(project),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return None


def git_commit(project: os.PathLike[str] | str) -> str | None:
    """Return a validated commit hash, or None without exposing Git diagnostics."""
    result = _git_run(project, ["rev-parse", "--verify", "HEAD"])
    if result is None or result.returncode != 0:
        return None
    commit = result.stdout.strip()
    return commit.lower() if _COMMIT.fullmatch(commit) else None


def git_dirty(project: os.PathLike[str] | str) -> bool | None:
    """Return dirty state, or None for unavailable and non-Git projects."""
    result = _git_run(
        project,
        ["status", "--porcelain=v1", "--untracked-files=normal"],
    )
    if result is None or result.returncode != 0:
        return None
    return bool(result.stdout)


def git_dirty_marker(project: os.PathLike[str] | str) -> str:
    dirty = git_dirty(project)
    if dirty is None:
        return "unavailable"
    return "dirty" if dirty else "clean"


def compose_fingerprints(
    files: Iterable[SourceFile],
    config: CodeGraphConfig,
    *,
    repository_id: str,
    git_commit: str | None,
    dirty_marker: str,
    schema_version: int | str,
    parser_version: str = "",
    grammar_version: str,
    adapter_version: str,
    resolver_version: str,
) -> FingerprintSet:
    """Compose portable fingerprints for later build/no-op consumption."""
    rows = _source_rows(files)
    normalized = normalized_config(config)
    parser_inputs = _parser_inputs(
        languages=config.languages,
        schema_version=schema_version,
        parser_version=parser_version,
        grammar_version=grammar_version,
        adapter_version=adapter_version,
        resolver_version=resolver_version,
    )
    source = _hash(rows)
    configuration = _hash(normalized)
    parser = _hash(parser_inputs)
    composed_inputs: Mapping[str, Any] = {
        "config": normalized,
        "dirty_marker": dirty_marker,
        "git_commit": git_commit,
        "parser": parser_inputs,
        "repository_id": repository_id,
        "sources": rows,
    }
    return FingerprintSet(
        source=source,
        config=configuration,
        parser=parser,
        inputs=_hash(composed_inputs),
    )
