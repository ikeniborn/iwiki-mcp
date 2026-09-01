"""Strict, secret-safe configuration for PostgreSQL-backed runtimes."""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping
from urllib.parse import urlsplit

from .auth import validate_domain_identifier

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


class ConfigError(RuntimeError):
    """Raised when PostgreSQL or hosted-server configuration is invalid."""


@dataclass(frozen=True)
class PostgresConfig:
    host: str
    port: int
    database: str
    user: str
    sslmode: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class ModelConfig:
    embed_model: str
    embed_dimensions: int
    rerank_model: str


@dataclass(frozen=True)
class HostedServerConfig:
    host: str
    port: int
    allowed_origins: tuple[str, ...]
    pool_min_size: int
    pool_max_size: int
    statement_timeout_ms: int
    lock_timeout_ms: int


@dataclass(frozen=True)
class HostedCodeGraphConfig:
    max_snapshot_age_seconds: int = 86400
    max_batch_rows: int = 1000
    max_batch_bytes: int = 1_000_000
    publication_session_ttl_seconds: int = 900
    staging_retention_seconds: int = 86400
    staging_cleanup_limit: int = 100
    require_session_binding: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.require_session_binding, bool):
            raise ConfigError("require_session_binding must be a boolean")
        bounds = {
            "max_snapshot_age_seconds": (0, None),
            "max_batch_rows": (1, 5000),
            "max_batch_bytes": (1, 5_000_000),
            "publication_session_ttl_seconds": (1, 3600),
            "staging_retention_seconds": (1, 604800),
            "staging_cleanup_limit": (1, 1000),
        }
        for name, (minimum, maximum) in bounds.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f"{name} must be an integer")
            if value < minimum or (maximum is not None and value > maximum):
                if maximum is None:
                    raise ConfigError(f"{name} must be at least {minimum}")
                raise ConfigError(
                    f"{name} must be between {minimum} and {maximum}"
                )

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any]) -> "HostedCodeGraphConfig":
        if not isinstance(config, Mapping):
            raise ConfigError("top-level storage, server, and code_graph must be tables")
        if set(config) - _HOSTED_CODE_GRAPH_FIELDS:
            raise ConfigError("code_graph configuration contains keys that are not allowed")
        return cls(**dict(config))


SpecificationMode = Literal["disabled", "optional", "strict"]


def _specification_mode(value: Any) -> SpecificationMode:
    if not isinstance(value, str) or value not in {
        "disabled",
        "optional",
        "strict",
    }:
        raise ConfigError("specification mode is invalid")
    return value


@dataclass(frozen=True)
class SpecificationOverride:
    iwiki_id: str
    domain: str
    mode: SpecificationMode

    def __post_init__(self) -> None:
        if not isinstance(self.iwiki_id, str) or not self.iwiki_id.strip():
            raise ConfigError("specification override iwiki_id is invalid")
        try:
            valid_domain = validate_domain_identifier(self.domain)
        except ValueError as exc:
            raise ConfigError("specification override domain is invalid") from exc
        object.__setattr__(self, "iwiki_id", self.iwiki_id.strip())
        object.__setattr__(self, "domain", valid_domain)
        object.__setattr__(self, "mode", _specification_mode(self.mode))


@dataclass(frozen=True)
class HostedSpecificationsConfig:
    default_mode: SpecificationMode = "optional"
    allow_project_mode: bool = True
    overrides: tuple[SpecificationOverride, ...] | list[SpecificationOverride] = ()
    _mode_by_pair: Mapping[tuple[str, str], SpecificationMode] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        default_mode = _specification_mode(self.default_mode)
        if type(self.allow_project_mode) is not bool:
            raise ConfigError("specification allow_project_mode is invalid")
        if not isinstance(self.overrides, (tuple, list)):
            raise ConfigError("specification overrides must be an array")
        overrides = tuple(self.overrides)
        if any(not isinstance(item, SpecificationOverride) for item in overrides):
            raise ConfigError("specification overrides must contain override records")

        modes: dict[tuple[str, str], SpecificationMode] = {}
        for override in overrides:
            pair = (override.iwiki_id, override.domain)
            if pair in modes:
                raise ConfigError("specification overrides contain a duplicate pair")
            modes[pair] = override.mode
        object.__setattr__(self, "default_mode", default_mode)
        object.__setattr__(self, "overrides", overrides)
        object.__setattr__(self, "_mode_by_pair", MappingProxyType(modes))

    def mode_for(self, iwiki_id: str, domain: str) -> SpecificationMode:
        return self._mode_by_pair.get((iwiki_id, domain), self.default_mode)

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any]) -> "HostedSpecificationsConfig":
        if not isinstance(config, Mapping):
            raise ConfigError("specification configuration must be a table")
        if set(config) - {"default_mode", "allow_project_mode", "overrides"}:
            raise ConfigError(
                "specification configuration contains keys that are not allowed"
            )
        default_mode = _specification_mode(config.get("default_mode", "optional"))
        allow_project_mode = config.get("allow_project_mode", True)
        raw_overrides = config.get("overrides", [])
        if not isinstance(raw_overrides, list):
            raise ConfigError("specification overrides must be an array")

        overrides: list[SpecificationOverride] = []
        for raw_override in raw_overrides:
            if not isinstance(raw_override, Mapping) or set(raw_override) != {
                "iwiki_id",
                "domain",
                "mode",
            }:
                raise ConfigError("specification override fields are invalid")
            overrides.append(
                SpecificationOverride(
                    iwiki_id=raw_override.get("iwiki_id"),
                    domain=raw_override.get("domain"),
                    mode=raw_override.get("mode"),
                )
            )
        return cls(
            default_mode=default_mode,
            allow_project_mode=allow_project_mode,
            overrides=tuple(overrides),
        )


@dataclass(frozen=True)
class ServerConfig:
    storage: PostgresConfig
    models: ModelConfig
    server: HostedServerConfig
    code_graph: HostedCodeGraphConfig = field(default_factory=HostedCodeGraphConfig)
    specifications: HostedSpecificationsConfig = field(
        default_factory=HostedSpecificationsConfig
    )


_SSLMODES = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
_RUNTIME_ONLY_FIELDS = {
    "password",
    "embed_model",
    "embed_dimensions",
    "rerank_model",
    "llm_base_url",
    "llm_key",
}
_POSTGRES_FIELDS = {
    "type",
    "host",
    "port",
    "database",
    "user",
    "sslmode",
    "iwiki_id",
}
_SERVER_FIELDS = {
    "host",
    "port",
    "allowed_origins",
    "pool_min_size",
    "pool_max_size",
    "statement_timeout_ms",
    "lock_timeout_ms",
}
_HOSTED_CODE_GRAPH_FIELDS = {
    "max_snapshot_age_seconds",
    "max_batch_rows",
    "max_batch_bytes",
    "publication_session_ttl_seconds",
    "staging_retention_seconds",
    "staging_cleanup_limit",
    "require_session_binding",
}
_SERVER_TOP_LEVEL_FIELDS = {"storage", "server", "code_graph", "specifications"}


def _required_string(config: Mapping[str, Any], name: str) -> str:
    value = config.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"storage.{name} is required")
    return value.strip()


def _bounded_integer(
    config: Mapping[str, Any], name: str, minimum: int, maximum: int
) -> int:
    value = config.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def load_model_config(environ: Mapping[str, str] | None = None) -> ModelConfig:
    env = os.environ if environ is None else environ
    embed_model = env.get("IWIKI_EMBED_MODEL", "").strip()
    if not embed_model:
        raise ConfigError("embedding model is required")
    raw_dimensions = env.get("IWIKI_EMBED_DIMENSIONS", "").strip()
    try:
        dimensions = int(raw_dimensions)
    except ValueError as exc:
        raise ConfigError("embedding dimensions must be a positive integer") from exc
    if dimensions <= 0:
        raise ConfigError("embedding dimensions must be a positive integer")
    return ModelConfig(
        embed_model=embed_model,
        embed_dimensions=dimensions,
        rerank_model=env.get("IWIKI_RERANK_MODEL", "").strip(),
    )


def load_postgres_config(
    config: Mapping[str, Any], environ: Mapping[str, str] | None = None
) -> PostgresConfig:
    env = os.environ if environ is None else environ
    if _RUNTIME_ONLY_FIELDS.intersection(config):
        raise ConfigError("credentials and model settings must use the runtime environment")
    if set(config) - _POSTGRES_FIELDS:
        raise ConfigError("storage configuration contains keys that are not allowed")
    host = _required_string(config, "host")
    port = _bounded_integer(config, "port", 1, 65535)
    database = _required_string(config, "database")
    user = _required_string(config, "user")
    sslmode = _required_string(config, "sslmode")
    if sslmode not in _SSLMODES:
        raise ConfigError("storage.sslmode is invalid")
    password = env.get("IWIKI_DB_PASSWORD", "")
    if not password:
        raise ConfigError("database password is required")
    return PostgresConfig(
        host=host,
        port=port,
        database=database,
        user=user,
        sslmode=sslmode,
        password=password,
    )


def _loopback_host(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("server.host is required")
    host = value.strip()
    if host == "localhost":
        return host
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError as exc:
        raise ConfigError("server.host must be a loopback address") from exc
    if not loopback:
        raise ConfigError("server.host must be a loopback address")
    return host


def _allowed_origins(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError("server.allowed_origins must be a non-empty array")
    origins: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError("server.allowed_origins must contain non-empty strings")
        origins.append(normalize_origin(item.strip()))
    if len(origins) != len(set(origins)):
        raise ConfigError("server.allowed_origins must not contain duplicates")
    return tuple(origins)


def normalize_origin(origin: str) -> str:
    """Return the canonical form used for hosted Origin comparisons."""
    try:
        parsed = urlsplit(origin)
        host = parsed.hostname
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise ConfigError("server.allowed_origins contains an invalid origin") from exc
    scheme = parsed.scheme.lower()
    if (
        scheme not in {"http", "https"}
        or not parsed.netloc
        or host is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.netloc.rsplit("@", 1)[-1].endswith(":")
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ConfigError("server.allowed_origins contains an invalid origin")

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            normalized_host = host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ConfigError(
                "server.allowed_origins contains an invalid origin"
            ) from exc
        labels = normalized_host.split(".")
        if any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not all(char.isalnum() or char == "-" for char in label)
            for label in labels
        ):
            raise ConfigError("server.allowed_origins contains an invalid origin")
        authority = normalized_host
    else:
        if "%" in host:
            raise ConfigError("server.allowed_origins contains an invalid origin")
        normalized_host = address.compressed
        authority = f"[{normalized_host}]" if address.version == 6 else normalized_host

    if scheme == "http" and normalized_host not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ConfigError("server.allowed_origins contains an invalid origin")
    if port is not None and port != {"http": 80, "https": 443}[scheme]:
        authority = f"{authority}:{port}"
    return f"{scheme}://{authority}"


def _load_toml(path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        with Path(path).open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError("server configuration could not be loaded") from exc
    if not isinstance(data, dict):
        raise ConfigError("server configuration must be a TOML table")
    return data


def load_server_config(
    path: str | os.PathLike[str], environ: Mapping[str, str] | None = None
) -> ServerConfig:
    data = _load_toml(path)
    if set(data) - _SERVER_TOP_LEVEL_FIELDS:
        raise ConfigError("server configuration contains keys that are not allowed")
    storage = data.get("storage")
    if storage is not None and not isinstance(storage, dict):
        raise ConfigError("top-level storage and server must be tables")
    if not isinstance(storage, dict) or storage.get("type") != "postgres":
        raise ConfigError("hosted server requires postgres storage")
    if "iwiki_id" in storage:
        raise ConfigError("storage.iwiki_id is not allowed in hosted configuration")
    server = data.get("server")
    if server is not None and not isinstance(server, dict):
        raise ConfigError("top-level storage and server must be tables")
    if not isinstance(server, dict):
        raise ConfigError("server configuration is required")
    if set(server) - _SERVER_FIELDS:
        raise ConfigError("server configuration contains keys that are not allowed")

    pool_min_size = _bounded_integer(server, "pool_min_size", 1, 100)
    pool_max_size = _bounded_integer(server, "pool_max_size", 1, 100)
    if pool_min_size > pool_max_size:
        raise ConfigError("pool_min_size must not exceed pool_max_size")
    hosted = HostedServerConfig(
        host=_loopback_host(server.get("host")),
        port=_bounded_integer(server, "port", 1, 65535),
        allowed_origins=_allowed_origins(server.get("allowed_origins")),
        pool_min_size=pool_min_size,
        pool_max_size=pool_max_size,
        statement_timeout_ms=_bounded_integer(
            server, "statement_timeout_ms", 1, 300000
        ),
        lock_timeout_ms=_bounded_integer(server, "lock_timeout_ms", 1, 300000),
    )
    code_graph = data.get("code_graph", {})
    if not isinstance(code_graph, dict):
        raise ConfigError("top-level storage, server, and code_graph must be tables")
    specifications = data.get("specifications", {})
    if not isinstance(specifications, dict):
        raise ConfigError("specification configuration must be a table")
    return ServerConfig(
        storage=load_postgres_config(storage, environ),
        models=load_model_config(environ),
        server=hosted,
        code_graph=HostedCodeGraphConfig.from_mapping(code_graph),
        specifications=HostedSpecificationsConfig.from_mapping(specifications),
    )
