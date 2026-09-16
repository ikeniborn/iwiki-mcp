"""Resolve hosted policy for one tenant and domain, one field at a time.

The hosted server owns policy; a bound project may tighten it. Every key a
project can influence is declared in `POLICY_FIELDS` together with the
comparison that decides what "tighter" means for it, so adding a key is one
entry rather than a new mechanism.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .config import ConfigError, HostedCodeGraphConfig, HostedSpecificationsConfig

_MODE_RANK = {"disabled": 0, "optional": 1, "strict": 2}


def _parse_mode(value: Any) -> str:
    if not isinstance(value, str) or value not in _MODE_RANK:
        raise ConfigError("specification mode is invalid")
    return value


def _parse_age(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError("max_snapshot_age_seconds must be a non-negative integer")
    return value


def _parse_flag(value: Any) -> bool:
    if type(value) is not bool:
        raise ConfigError("require_session_binding must be a boolean")
    return value


def _mode_at_least_as_strict(candidate: str, floor: str) -> bool:
    return _MODE_RANK[candidate] >= _MODE_RANK[floor]


def _age_at_least_as_strict(candidate: int, floor: int) -> bool:
    """`0` disables age rejection, so it is the weakest value, not the strictest."""
    if candidate == 0:
        return floor == 0
    return floor == 0 or candidate <= floor


def _flag_at_least_as_strict(candidate: bool, floor: bool) -> bool:
    return candidate or not floor


@dataclass(frozen=True)
class PolicyField:
    name: str
    project_tier: bool
    parse: Callable[[Any], Any]
    at_least_as_strict: Callable[[Any, Any], bool]
    default_at: tuple[str, str]
    built_in: Any


POLICY_FIELDS: tuple[PolicyField, ...] = (
    PolicyField(
        name="specification_mode",
        project_tier=True,
        parse=_parse_mode,
        at_least_as_strict=_mode_at_least_as_strict,
        default_at=("specifications", "default_mode"),
        built_in="optional",
    ),
    PolicyField(
        name="max_snapshot_age_seconds",
        project_tier=True,
        parse=_parse_age,
        at_least_as_strict=_age_at_least_as_strict,
        default_at=("code_graph", "max_snapshot_age_seconds"),
        built_in=86400,
    ),
    PolicyField(
        name="require_session_binding",
        project_tier=False,
        parse=_parse_flag,
        at_least_as_strict=_flag_at_least_as_strict,
        default_at=("code_graph", "require_session_binding"),
        built_in=False,
    ),
)

FIELDS_BY_NAME = {field.name: field for field in POLICY_FIELDS}
PROJECT_TIER_FIELDS = tuple(
    field.name for field in POLICY_FIELDS if field.project_tier
)


@dataclass(frozen=True)
class ResolvedPolicy:
    values: Mapping[str, Any]
    sources: Mapping[str, str]
    suppressed: tuple[str, ...]

    def value(self, name: str) -> Any:
        return self.values[name]

    def source(self, name: str) -> str:
        return self.sources[name]

    def as_status(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            name: {"value": self.values[name], "source": self.sources[name]}
            for name in self.values
        }
        record["suppressed"] = list(self.suppressed)
        return record


def parse_project_policy(value: Any) -> dict[str, Any]:
    """Validate a client-supplied policy object; never ignore a member."""
    if not isinstance(value, Mapping):
        raise ConfigError("project policy must be a table")
    unknown = set(value) - set(PROJECT_TIER_FIELDS)
    if unknown:
        raise ConfigError(f"project policy key '{sorted(unknown)[0]}' is not allowed")
    return {
        name: FIELDS_BY_NAME[name].parse(item) for name, item in value.items()
    }


def _override_value(specifications, iwiki_id: str, domain: str | None, name: str):
    """Return an exact-pair value first, then the tenant-wide one."""
    if specifications is None:
        return None
    exact = None
    tenant = None
    for override in specifications.overrides:
        if override.iwiki_id != iwiki_id:
            continue
        if override.domain is None:
            tenant = override
        elif domain is not None and override.domain == domain:
            exact = override
    for candidate in (exact, tenant):
        if candidate is not None:
            value = candidate.values.get(name)
            if value is not None:
                return value
    return None


def _hosted_default(field: PolicyField, specifications, code_graph):
    table, key = field.default_at
    source = specifications if table == "specifications" else code_graph
    if source is None:
        return field.built_in, "built_in_default"
    return getattr(source, key), "hosted_default"


def resolve_policy(
    specifications: HostedSpecificationsConfig | None,
    code_graph: HostedCodeGraphConfig | None,
    iwiki_id: str,
    domain: str | None,
    project_policy: Mapping[str, Any] | None,
) -> ResolvedPolicy:
    """Resolve every policy field for one tenant, and one domain when bound.

    `domain=None` is legitimate: the session-binding gate decides before any
    domain exists, so it resolves from the tenant override and the defaults.
    """
    allow_project = specifications is None or specifications.allow_project_mode
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    suppressed: list[str] = []
    for field in POLICY_FIELDS:
        override = _override_value(specifications, iwiki_id, domain, field.name)
        if override is not None:
            values[field.name] = override
            sources[field.name] = "hosted_override"
            continue
        default, default_source = _hosted_default(field, specifications, code_graph)
        declared = (
            None
            if project_policy is None or not field.project_tier
            else project_policy.get(field.name)
        )
        if declared is not None and allow_project and field.at_least_as_strict(
            declared, default
        ):
            values[field.name] = declared
            sources[field.name] = "project"
            continue
        if declared is not None:
            suppressed.append(field.name)
        values[field.name] = default
        sources[field.name] = default_source
    return ResolvedPolicy(values, sources, tuple(suppressed))
