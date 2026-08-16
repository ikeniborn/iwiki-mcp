"""Configuration for the optional project code graph."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from iwiki_mcp import base
from .publication import PublishMode

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


class CodeGraphConfigError(RuntimeError):
    """Raised when code graph configuration is invalid."""


_FIELDS = {
    "enabled",
    "languages",
    "auto_rebuild",
    "max_rebuild_seconds",
    "max_file_bytes",
    "max_total_files",
    "include_tests",
    "exclude",
    "publish_mode",
    "read_mode",
    "max_snapshot_age_seconds",
    "max_batch_rows",
    "max_batch_bytes",
    "publication_session_ttl_seconds",
    "staging_retention_seconds",
    "staging_cleanup_limit",
}
_FORBIDDEN_FIELDS = {
    "database",
    "project_id",
    "project_uuid",
    "incremental",
    "dsn",
    "mcp_token",
    "mcp_url",
    "password",
    "token",
    "url",
}


def _bool(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise CodeGraphConfigError(f"code_graph.{field} must be a boolean")
    return value


def _positive_int(value: Any, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise CodeGraphConfigError(f"code_graph.{field} must be a positive integer")
    return value


def _non_negative_int(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise CodeGraphConfigError(
            f"code_graph.{field} must be a non-negative integer"
        )
    return value


def _mode(value: Any, field: str) -> PublishMode:
    if value not in ("sqlite", "postgres", "mcp"):
        raise CodeGraphConfigError(
            f"code_graph.{field} must be sqlite, postgres, or mcp"
        )
    return value


def _languages(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise CodeGraphConfigError("code_graph.languages must be a non-empty array")
    result = tuple(value)
    if any(type(item) is not str or item != "python" for item in result):
        raise CodeGraphConfigError("code_graph.languages supports only python")
    return result


def _exclude(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise CodeGraphConfigError("code_graph.exclude must be an array")
    result = tuple(value)
    for item in result:
        if type(item) is not str or not item or os.path.isabs(item):
            raise CodeGraphConfigError("code_graph.exclude contains an unsafe path")
        if ".." in item.replace("\\", "/").split("/"):
            raise CodeGraphConfigError("code_graph.exclude contains an unsafe path")
    return result


@dataclass(frozen=True)
class CodeGraphConfig:
    enabled: bool = True
    languages: tuple[str, ...] = ("python",)
    auto_rebuild: str = "bounded"
    max_rebuild_seconds: int = 10
    max_file_bytes: int = 1_000_000
    max_total_files: int = 20_000
    include_tests: bool = True
    exclude: tuple[str, ...] = ()
    publish_mode: PublishMode = "sqlite"
    read_mode: PublishMode = "sqlite"
    max_snapshot_age_seconds: int = 86400
    max_batch_rows: int = 1000
    max_batch_bytes: int = 1_000_000
    publication_session_ttl_seconds: int = 900
    staging_retention_seconds: int = 86400
    staging_cleanup_limit: int = 100

    def __post_init__(self) -> None:
        object.__setattr__(self, "enabled", _bool(self.enabled, "enabled"))
        object.__setattr__(self, "languages", _languages(self.languages))
        if self.auto_rebuild not in ("off", "bounded"):
            raise CodeGraphConfigError("code_graph.auto_rebuild must be off or bounded")
        object.__setattr__(
            self,
            "max_rebuild_seconds",
            _positive_int(self.max_rebuild_seconds, "max_rebuild_seconds"),
        )
        object.__setattr__(
            self, "max_file_bytes", _positive_int(self.max_file_bytes, "max_file_bytes")
        )
        object.__setattr__(
            self, "max_total_files", _positive_int(self.max_total_files, "max_total_files")
        )
        object.__setattr__(self, "include_tests", _bool(self.include_tests, "include_tests"))
        object.__setattr__(self, "exclude", _exclude(self.exclude))
        object.__setattr__(self, "publish_mode", _mode(self.publish_mode, "publish_mode"))
        object.__setattr__(self, "read_mode", _mode(self.read_mode, "read_mode"))
        object.__setattr__(
            self,
            "max_snapshot_age_seconds",
            _non_negative_int(
                self.max_snapshot_age_seconds, "max_snapshot_age_seconds"
            ),
        )
        for field in (
            "max_batch_rows",
            "max_batch_bytes",
            "publication_session_ttl_seconds",
            "staging_retention_seconds",
            "staging_cleanup_limit",
        ):
            object.__setattr__(self, field, _positive_int(getattr(self, field), field))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CodeGraphConfig":
        if not isinstance(raw, Mapping):
            raise CodeGraphConfigError("code_graph must be a table")
        keys = set(raw)
        forbidden = keys & _FORBIDDEN_FIELDS
        if forbidden:
            raise CodeGraphConfigError("code_graph connection fields are not supported")
        unknown = keys - _FIELDS
        if unknown:
            raise CodeGraphConfigError(
                f"unknown code_graph field: {sorted(unknown)[0]}"
            )
        return cls(**dict(raw))


def _environment_overrides() -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if "IWIKI_CODE_GRAPH_ENABLED" in os.environ:
        raw = os.environ["IWIKI_CODE_GRAPH_ENABLED"]
        if raw not in ("true", "false"):
            raise CodeGraphConfigError("IWIKI_CODE_GRAPH_ENABLED must be true or false")
        overrides["enabled"] = raw == "true"
    if "IWIKI_CODE_GRAPH_MAX_FILE_BYTES" in os.environ:
        try:
            overrides["max_file_bytes"] = int(os.environ["IWIKI_CODE_GRAPH_MAX_FILE_BYTES"])
        except ValueError as exc:
            raise CodeGraphConfigError(
                "IWIKI_CODE_GRAPH_MAX_FILE_BYTES must be an integer"
            ) from exc
    if "IWIKI_CODE_GRAPH_MAX_FILES" in os.environ:
        try:
            overrides["max_total_files"] = int(os.environ["IWIKI_CODE_GRAPH_MAX_FILES"])
        except ValueError as exc:
            raise CodeGraphConfigError("IWIKI_CODE_GRAPH_MAX_FILES must be an integer") from exc
    if "IWIKI_CODE_GRAPH_AUTO_REBUILD" in os.environ:
        overrides["auto_rebuild"] = os.environ["IWIKI_CODE_GRAPH_AUTO_REBUILD"]
    return overrides


def load_code_graph_config(project_dir: str) -> CodeGraphConfig:
    """Load project configuration, then apply the supported environment overrides."""
    try:
        project_config = base.load_project_config(project_dir)
    except UnicodeDecodeError as exc:
        raise CodeGraphConfigError("invalid project configuration") from exc
    config_path = Path(project_dir) / ".iwiki.toml"
    try:
        with config_path.open("rb") as fh:
            tomllib.load(fh)
    except FileNotFoundError:
        pass
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise CodeGraphConfigError("invalid project configuration") from exc
    raw = project_config.get("code_graph", {})
    if not isinstance(raw, Mapping):
        raise CodeGraphConfigError("code_graph must be a table")
    configured = dict(raw)
    configured.update(_environment_overrides())
    return CodeGraphConfig.from_mapping(configured)
