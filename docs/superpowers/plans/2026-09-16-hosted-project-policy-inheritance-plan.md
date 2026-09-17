---
chain:
  intent: docs/superpowers/intents/2026-09-16-hosted-project-policy-inheritance-intent.md
  spec: docs/superpowers/specs/2026-09-16-hosted-project-policy-inheritance-design.md
review:
  plan_hash: 46248e04dd57b9ef
  last_run: 2026-09-16
  phases:
    - name: structure
      status: passed
    - name: coverage
      status: passed
    - name: dependencies
      status: passed
    - name: verifiability
      status: passed
    - name: consistency
      status: passed
  findings:
    - id: F-001
      phase: dependencies
      severity: WARNING
      section: "Task 1: The policy module"
      section_hash: 514eab31618ea090
      fragment: "_override_value"
      text: >-
        Task 1 read `override.values`, a mapping the override record only gains
        in Task 2. The implementer of Task 1 sees only their own task, so the
        step depended on an artifact that did not exist yet.
      fix: >-
        Task 1 returns None and says why; Task 2 supplies the body together with
        the record it reads.
      verdict: fixed
      verdict_at: 2026-09-16
    - id: F-002
      phase: verifiability
      severity: WARNING
      section: "Task 2: Policy override records in the server TOML"
      section_hash: 076417ae1c8b4762
      fragment: "load_server_config(path, _runtime_env())"
      text: >-
        The planned tests used load_server_config and ConfigError as module-level
        names and wrote the config file by hand, but tests/postgres/test_config.py
        imports both inside each test and owns a _write_config helper. The tests
        would have failed at collection rather than on behavior.
      fix: Adopt the module's function-local imports and its _write_config helper.
      verdict: fixed
      verdict_at: 2026-09-16
    - id: F-003
      phase: verifiability
      severity: CRITICAL
      section: "Task 3: Carry the project policy through the binding and the bind tool"
      section_hash: 91a0c660188fecc2
      fragment: "test_local_postgres_binding_refuses_a_project_policy(bound)"
      text: >-
        The test asserted the local-PostgreSQL refusal while using the `bound`
        fixture, which installs a Git binding. `_wiki_bind` never enters its
        _is_postgres branch there, so the assertion could not pass and the step's
        expected result was wrong.
      fix: >-
        Assert project_config_manual_edit_required against the Git binding the
        fixture provides, and move the local-PostgreSQL row of the error table to
        tests/postgres/test_tool_matrix.py.
      verdict: fixed
      verdict_at: 2026-09-16
---

# Hosted Project Policy Inheritance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hosted server's single-key specification-mode resolver with one per-field policy tier that covers three keys, supports a tenant-wide override record, and lets a bound project tighten what the operator allows.

**Architecture:** A new framework-free module `src/iwiki_mcp/postgres/policy.py` holds a field-descriptor table and a pure `resolve_policy` function. The hosted server TOML keeps its `[specifications]` table but its override records gain an optional `domain` and one optional key per policy field. `wiki_bind` carries the project's declared values in a new `project_policy` object. Every consumer — the specification policy reporter, the code reader, the session-binding gate, `PostgresStore`, and `admin` — resolves through that one function.

**Tech Stack:** Python 3.10+, `tomllib`/`tomli`, `psycopg`, `pytest` with `asyncio_mode = "auto"`, `flake8` at `max-line-length = 100`.

**Spec:** `docs/superpowers/specs/2026-09-16-hosted-project-policy-inheritance-design.md`

## Global Constraints

- The project tier may only tighten. Per field: `specification_mode` by rank `disabled < optional < strict`; `max_snapshot_age_seconds` by `min()` with `0` treated as infinity; `require_session_binding` by `false < true` — the last is not in the project tier at all.
- `POLICY_FIELDS` is the only allowlist. A field without both `parse` and `at_least_as_strict` fails the guard test.
- No resource limit enters the project tier or the bind contract: `max_batch_rows`, `max_batch_bytes`, `publication_session_ttl_seconds`, `staging_retention_seconds`, `staging_cleanup_limit`, `pool_min_size`, `pool_max_size`, `statement_timeout_ms`, `lock_timeout_ms`, `allowed_origins`, every `[storage]` key.
- The server TOML table stays named `[specifications]`; `default_mode`, `allow_project_mode`, and the override key `mode` keep working. An existing configuration file must load unedited.
- `wiki_bind` gains exactly one new parameter, `project_policy`. `specification_mode` stays accepted as a deprecated alias. Both supplied together is a validation error.
- Session scope only: the policy is never persisted and no database migration is introduced.
- `wiki_status`'s existing `specifications` block stays byte-for-byte; the new data goes in a sibling `policy` block.
- `flake8` at `max-line-length = 100` stays clean; no formatter is used, so match surrounding style by hand.
- Every `wiki_*` handler stays fail-soft: implementation functions are defined plain and registered with `mcp.tool()` at the bottom of `server.py`.

---

### Task 1: The policy module

**Files:**
- Create: `src/iwiki_mcp/postgres/policy.py`
- Test: `tests/test_policy_resolution.py`

**Interfaces:**
- Consumes: `ConfigError`, `HostedSpecificationsConfig`, `HostedCodeGraphConfig` from `iwiki_mcp.postgres.config`.
- Produces: `PolicyField`, `POLICY_FIELDS`, `PROJECT_TIER_FIELDS`, `ResolvedPolicy`, `resolve_policy(specifications, code_graph, iwiki_id, domain, project_policy)`, `parse_project_policy(value)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_policy_resolution.py
import pytest

from iwiki_mcp.postgres.config import (
    ConfigError,
    HostedCodeGraphConfig,
    HostedSpecificationsConfig,
)
from iwiki_mcp.postgres.policy import (
    POLICY_FIELDS,
    PROJECT_TIER_FIELDS,
    parse_project_policy,
    resolve_policy,
)


def _resolve(*, specifications=None, code_graph=None, domain="payments", project=None):
    return resolve_policy(
        specifications or HostedSpecificationsConfig(),
        code_graph or HostedCodeGraphConfig(),
        "team-wiki",
        domain,
        project,
    )


def test_built_in_defaults_apply_without_any_configuration():
    resolved = resolve_policy(None, None, "team-wiki", "payments", None)

    assert resolved.value("specification_mode") == "optional"
    assert resolved.source("specification_mode") == "built_in_default"
    assert resolved.value("max_snapshot_age_seconds") == 86400
    assert resolved.value("require_session_binding") is False
    assert resolved.suppressed == ()


def test_project_tier_applies_when_at_least_as_strict():
    resolved = _resolve(project={"specification_mode": "strict"})

    assert resolved.value("specification_mode") == "strict"
    assert resolved.source("specification_mode") == "project"
    assert resolved.suppressed == ()


def test_project_tier_suppressed_when_looser_than_hosted_default():
    specifications = HostedSpecificationsConfig(default_mode="optional")

    resolved = _resolve(
        specifications=specifications, project={"specification_mode": "disabled"}
    )

    assert resolved.value("specification_mode") == "optional"
    assert resolved.source("specification_mode") == "hosted_default"
    assert resolved.suppressed == ("specification_mode",)


def test_every_field_declares_its_comparison_and_parser():
    for field in POLICY_FIELDS:
        assert callable(field.parse)
        assert callable(field.at_least_as_strict)
        assert field.default_at[0] in {"specifications", "code_graph"}
    assert "require_session_binding" not in PROJECT_TIER_FIELDS


def test_parse_project_policy_rejects_an_unknown_member():
    with pytest.raises(ConfigError):
        parse_project_policy({"max_batch_rows": 1})


def test_parse_project_policy_rejects_an_operator_only_field():
    with pytest.raises(ConfigError):
        parse_project_policy({"require_session_binding": True})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_policy_resolution.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'iwiki_mcp.postgres.policy'`

- [ ] **Step 3: Write the module**

```python
# src/iwiki_mcp/postgres/policy.py
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
    """Return an operator override for one field, once records can carry one.

    Task 2 replaces this body: the override record only gains its `values`
    mapping there, so reading it here would depend on a type that does not
    exist yet.
    """
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_policy_resolution.py -v`
Expected: PASS, six tests

- [ ] **Step 5: Lint and commit**

```bash
uv run flake8 src/iwiki_mcp/postgres/policy.py tests/test_policy_resolution.py
git add src/iwiki_mcp/postgres/policy.py tests/test_policy_resolution.py
git commit -m "feat(policy): resolve hosted policy per field for one tenant and domain"
```

---

### Task 2: Policy override records in the server TOML

**Files:**
- Modify: `src/iwiki_mcp/postgres/config.py:106-189`
- Modify: `src/iwiki_mcp/postgres/policy.py` (`_override_value` gains the body Task 1 deferred)
- Modify: `tests/postgres/test_config.py:97-170`
- Test: `tests/test_policy_resolution.py`

**Interfaces:**
- Consumes: `PolicyField`, `FIELDS_BY_NAME`, `POLICY_FIELDS` from Task 1.
- Produces: `PolicyOverride(iwiki_id, domain: str | None, values: Mapping[str, Any])`; `HostedSpecificationsConfig(default_mode, allow_project_mode, overrides)` with `mode_for` **removed**.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_policy_resolution.py
from iwiki_mcp.postgres.config import PolicyOverride


def test_tenant_override_applies_to_every_domain():
    specifications = HostedSpecificationsConfig(
        overrides=(PolicyOverride("team-wiki", None, {"specification_mode": "disabled"}),)
    )

    for domain in ("payments", "billing"):
        resolved = _resolve(specifications=specifications, domain=domain)
        assert resolved.value("specification_mode") == "disabled"
        assert resolved.source("specification_mode") == "hosted_override"


def test_exact_override_beats_the_tenant_override():
    specifications = HostedSpecificationsConfig(
        overrides=(
            PolicyOverride("team-wiki", None, {"specification_mode": "disabled"}),
            PolicyOverride("team-wiki", "payments", {"specification_mode": "strict"}),
        )
    )

    assert _resolve(specifications=specifications).value("specification_mode") == "strict"
    assert (
        _resolve(specifications=specifications, domain="billing").value(
            "specification_mode"
        )
        == "disabled"
    )


def test_inheritance_is_per_field_not_per_record():
    specifications = HostedSpecificationsConfig(
        default_mode="optional",
        overrides=(PolicyOverride("team-wiki", None, {"require_session_binding": True}),),
    )

    resolved = _resolve(
        specifications=specifications, project={"specification_mode": "strict"}
    )

    assert resolved.value("require_session_binding") is True
    assert resolved.source("require_session_binding") == "hosted_override"
    assert resolved.value("specification_mode") == "strict"
    assert resolved.source("specification_mode") == "project"


def test_domainless_resolution_uses_the_tenant_override():
    specifications = HostedSpecificationsConfig(
        overrides=(PolicyOverride("team-wiki", None, {"require_session_binding": True}),)
    )

    resolved = _resolve(specifications=specifications, domain=None)

    assert resolved.value("require_session_binding") is True
```

`tests/postgres/test_config.py` imports `load_server_config` and `ConfigError` **inside**
each test function, and writes the file through its own `_write_config(tmp_path, text)`
helper (line 39). Follow that style exactly — the module has no top-level import of either
name.

```python
# append to tests/postgres/test_config.py
def test_hosted_override_accepts_an_omitted_domain(tmp_path):
    from iwiki_mcp.postgres.config import load_server_config

    text = (
        "[specifications]\n"
        "[[specifications.overrides]]\n"
        'iwiki_id = "team-wiki"\n'
        'specification_mode = "disabled"\n'
    ) + _server_toml()

    config = load_server_config(_write_config(tmp_path, text), _runtime_env())

    override = config.specifications.overrides[0]
    assert override.domain is None
    assert override.values == {"specification_mode": "disabled"}


def test_hosted_override_keeps_the_mode_alias(tmp_path):
    from iwiki_mcp.postgres.config import load_server_config

    text = (
        "[specifications]\n"
        "[[specifications.overrides]]\n"
        'iwiki_id = "team-wiki"\n'
        'domain = "payments"\n'
        'mode = "strict"\n'
    ) + _server_toml()

    config = load_server_config(_write_config(tmp_path, text), _runtime_env())

    assert config.specifications.overrides[0].values == {
        "specification_mode": "strict"
    }


def test_hosted_override_rejects_the_alias_beside_the_canonical_key(tmp_path):
    from iwiki_mcp.postgres.config import ConfigError, load_server_config

    text = (
        "[specifications]\n"
        "[[specifications.overrides]]\n"
        'iwiki_id = "team-wiki"\n'
        'domain = "payments"\n'
        'mode = "strict"\n'
        'specification_mode = "strict"\n'
    ) + _server_toml()

    with pytest.raises(ConfigError):
        load_server_config(_write_config(tmp_path, text), _runtime_env())


def test_hosted_overrides_reject_two_tenant_records(tmp_path):
    from iwiki_mcp.postgres.config import ConfigError, load_server_config

    text = (
        "[specifications]\n"
        "[[specifications.overrides]]\n"
        'iwiki_id = "team-wiki"\n'
        'specification_mode = "strict"\n'
        "[[specifications.overrides]]\n"
        'iwiki_id = "team-wiki"\n'
        'specification_mode = "disabled"\n'
    ) + _server_toml()

    with pytest.raises(ConfigError):
        load_server_config(_write_config(tmp_path, text), _runtime_env())
```

Replace every existing `config.specifications.mode_for(...)` assertion in
`tests/postgres/test_config.py` with a `resolve_policy(...)` call, because `mode_for`
disappears in Step 3. For example, `test_hosted_specification_exact_pair_overrides_default`
becomes:

```python
def _mode(config, iwiki_id, domain):
    return resolve_policy(
        config.specifications, config.code_graph, iwiki_id, domain, None
    ).value("specification_mode")


def test_hosted_specification_exact_pair_overrides_default(tmp_path):
    ...
    assert _mode(config, "team-wiki", "payments") == "strict"
    assert _mode(config, "team-wiki", "other") == "optional"
    assert _mode(config, "other-wiki", "payments") == "optional"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_policy_resolution.py tests/postgres/test_config.py -v`
Expected: FAIL with `ImportError: cannot import name 'PolicyOverride'`

- [ ] **Step 3: Rewrite the override record**

Replace `SpecificationOverride` and the override handling in `HostedSpecificationsConfig`
(`src/iwiki_mcp/postgres/config.py:106-189`) with:

```python
@dataclass(frozen=True)
class PolicyOverride:
    iwiki_id: str
    domain: str | None
    values: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.iwiki_id, str) or not self.iwiki_id.strip():
            raise ConfigError("specification override iwiki_id is invalid")
        object.__setattr__(self, "iwiki_id", self.iwiki_id.strip())
        if self.domain is not None:
            try:
                valid_domain = validate_domain_identifier(self.domain)
            except ValueError as exc:
                raise ConfigError("specification override domain is invalid") from exc
            object.__setattr__(self, "domain", valid_domain)
        if not isinstance(self.values, Mapping) or not self.values:
            raise ConfigError("specification override carries no policy value")
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


# Keep the historical name importable for one release.
SpecificationOverride = PolicyOverride
```

`HostedSpecificationsConfig.__post_init__` keeps validating `default_mode` and
`allow_project_mode`, keeps rejecting a non-sequence `overrides`, and replaces the
`_mode_by_pair` map with a duplicate check on `(iwiki_id, domain)` where `domain` may be
`None`. Delete `_mode_by_pair` and `mode_for` entirely.

`from_mapping` changes shape:

```python
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

        overrides: list[PolicyOverride] = []
        for raw_override in raw_overrides:
            overrides.append(_policy_override(raw_override))
        return cls(
            default_mode=default_mode,
            allow_project_mode=allow_project_mode,
            overrides=tuple(overrides),
        )
```

with the record parser beside it:

```python
def _policy_override(raw: Any) -> "PolicyOverride":
    """Parse one override record: identity keys plus any subset of policy keys."""
    from .policy import FIELDS_BY_NAME

    if not isinstance(raw, Mapping) or "iwiki_id" not in raw:
        raise ConfigError("specification override fields are invalid")
    allowed = {"iwiki_id", "domain", "mode", *FIELDS_BY_NAME}
    if set(raw) - allowed:
        raise ConfigError("specification override fields are invalid")
    if "mode" in raw and "specification_mode" in raw:
        raise ConfigError("specification override sets mode twice")
    values: dict[str, Any] = {}
    for name, field in FIELDS_BY_NAME.items():
        key = "mode" if name == "specification_mode" and "mode" in raw else name
        if key in raw:
            values[name] = field.parse(raw[key])
    return PolicyOverride(
        iwiki_id=raw.get("iwiki_id"),
        domain=raw.get("domain"),
        values=values,
    )
```

The import is function-local because `policy` imports `config` at module scope.

Now give `_override_value` in `src/iwiki_mcp/postgres/policy.py` the body Task 1 deferred,
since the record it reads exists from this task onward:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_policy_resolution.py tests/postgres/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Verify an existing configuration still loads**

```bash
uv run pytest tests/postgres/test_config.py -v -k "specification"
```
Expected: PASS, including the pre-existing default, `allow_project_mode`, exact-pair, supported-modes, and duplicate-pair tests.

- [ ] **Step 6: Lint and commit**

```bash
uv run flake8 src/iwiki_mcp/postgres/config.py src/iwiki_mcp/postgres/policy.py tests/postgres/test_config.py tests/test_policy_resolution.py
git add src/iwiki_mcp/postgres/config.py src/iwiki_mcp/postgres/policy.py tests/postgres/test_config.py tests/test_policy_resolution.py
git commit -m "feat(config): carry any policy key on an override record with an optional domain"
```

---

### Task 3: Carry the project policy through the binding and the bind tool

**Files:**
- Modify: `src/iwiki_mcp/storage.py:85-86`
- Modify: `src/iwiki_mcp/server.py:4960-5120`
- Modify: `src/iwiki_mcp/base.py:299-353`
- Create: `tests/conftest.py` (receives the relocated `hosted_session` fixture)
- Test: `tests/test_specification_tools.py`

**Interfaces:**
- Consumes: `parse_project_policy`, `PROJECT_TIER_FIELDS` from Task 1.
- Produces: `PostgresBinding.project_policy: Mapping[str, Any] | None` replacing `project_specification_mode`; `wiki_bind(read, write, primary, specification_mode=None, project_policy=None)`.

- [ ] **Step 0: Move the hosted-session fixture where later tasks can reach it**

`hosted_session` currently lives at `tests/test_specification_tools.py:292` and is a
**factory**: it yields `install(source)` and the test calls `hosted_session("session")`.
Tasks 4 and 7 add tests in `tests/test_specification_status_lint.py`, which has no fixtures
of its own, so move the fixture verbatim into a new `tests/conftest.py` and delete it from
`test_specification_tools.py`. Nothing about its body changes.

```bash
uv run pytest tests/test_specification_tools.py -q
```
Expected: PASS, unchanged, before any behavior edit.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_specification_tools.py
def test_bind_rejects_an_unknown_project_policy_member(hosted_session):
    hosted_session("session")

    result = server.wiki_bind(
        read=["payments"], write=["payments"], primary="payments",
        project_policy={"max_batch_rows": 10},
    )

    assert "project policy" in result["error"]


def test_bind_rejects_the_alias_beside_the_object(hosted_session):
    hosted_session("session")

    result = server.wiki_bind(
        read=["payments"], write=["payments"], primary="payments",
        specification_mode="strict",
        project_policy={"specification_mode": "strict"},
    )

    assert result["error"] == "specification mode is set twice"


def test_bind_stores_the_project_policy_on_the_session(hosted_session):
    hosted_session("session")

    server.wiki_bind(
        read=["payments"], write=["payments"], primary="payments",
        project_policy={"specification_mode": "strict",
                        "max_snapshot_age_seconds": 3600},
    )

    binding = server._resolved_binding()
    assert binding.project_policy == {
        "specification_mode": "strict",
        "max_snapshot_age_seconds": 3600,
    }


def test_git_binding_refuses_a_project_policy(bound):
    result = server.wiki_bind(
        read=["payments"], write=["payments"], primary="payments",
        project_policy={"specification_mode": "strict"},
    )

    assert result["code"] == "project_config_manual_edit_required"
```

The `bound` fixture (`tests/test_specification_tools.py:85`) installs a **Git** binding —
`server.base.Binding`, not `PostgresBinding` — so `_wiki_bind` falls through its
`_is_postgres` branch entirely and answers `project_config_manual_edit_required`. That is
the Git row of the spec's error table. The local-PostgreSQL row (`project policy requires a
hosted session`) is exercised by the `_is_postgres` branch and is covered where a local
PostgreSQL binding already exists, in `tests/postgres/test_tool_matrix.py`; add it there if
that file has no equivalent case.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_specification_tools.py -v -k project_policy`
Expected: FAIL with `TypeError: wiki_bind() got an unexpected keyword argument 'project_policy'`

- [ ] **Step 3: Rename the binding field**

In `src/iwiki_mcp/storage.py` replace

```python
    project_specification_mode: Literal["disabled", "optional", "strict"] | None = None
```

with

```python
    project_policy: Mapping[str, Any] | None = None
```

and add `Mapping` / `Any` to the module's `typing` import. In `src/iwiki_mcp/base.py`,
where the local binding is constructed (around line 299), pass
`project_policy={"specification_mode": specification_mode}` so a local PostgreSQL binding
keeps declaring what the project file said.

- [ ] **Step 4: Extend the bind tool**

In `_wiki_bind` replace the `specification_mode` validation block with:

```python
        if specification_mode is not None and project_policy is not None and (
            "specification_mode" in project_policy
        ):
            return {
                "error": "specification mode is set twice",
                "hint": "pass specification_mode or project_policy, not both",
            }
        declared = dict(project_policy or {})
        if specification_mode is not None:
            declared["specification_mode"] = specification_mode
        try:
            declared = _policy.parse_project_policy(declared) if declared else {}
        except ConfigError as exc:
            return {
                "error": str(exc),
                "hint": f"project policy accepts {', '.join(_policy.PROJECT_TIER_FIELDS)}",
            }
        session = _SESSION_BINDING.get()
        if declared and not isinstance(session, _HostedBindingState):
            return {
                "error": "project policy requires a hosted session",
                "hint": "omit project_policy for local PostgreSQL stdio",
            }
```

and the `replace(...)` call's policy argument with:

```python
            project_policy=(
                bind.project_policy if not declared else declared
            ),
```

Add `project_policy: dict | None = None` to both `_wiki_bind` and `wiki_bind`, thread it
through the two `_wiki_bind(...)` calls in `wiki_bind`, and import the module at the top of
`server.py` as `from .postgres import policy as _policy`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_specification_tools.py -v`
Expected: PASS

- [ ] **Step 6: Lint and commit**

```bash
uv run flake8 src/iwiki_mcp/storage.py src/iwiki_mcp/server.py src/iwiki_mcp/base.py tests/test_specification_tools.py
git add src/iwiki_mcp/storage.py src/iwiki_mcp/server.py src/iwiki_mcp/base.py tests/test_specification_tools.py
git commit -m "feat(bind): carry a project policy object through the hosted session"
```

---

### Task 4: Route every consumer through the resolver

**Files:**
- Modify: `src/iwiki_mcp/http.py:245-273`
- Modify: `src/iwiki_mcp/server.py:916-963`
- Modify: `src/iwiki_mcp/server.py:1101-1150`
- Test: `tests/test_specification_status_lint.py`

**Interfaces:**
- Consumes: `resolve_policy` from Task 1, `binding.project_policy` from Task 3.
- Produces: `_resolve_binding_policy(binding, domain)` in `server.py`, returning a `ResolvedPolicy`; `_specification_policy_details(binding, domain)` keeping its `(mode, source, suppressed)` return shape.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_specification_status_lint.py
from iwiki_mcp import server
from iwiki_mcp.postgres.config import HostedSpecificationsConfig, PolicyOverride


def test_code_binding_gate_reads_the_tenant_override(hosted_session, monkeypatch):
    hosted_session("token_default")
    monkeypatch.setattr(
        server,
        "_HOSTED_SPECIFICATIONS",
        HostedSpecificationsConfig(
            overrides=(PolicyOverride("wiki-a", None, {"require_session_binding": True}),)
        ),
    )

    assert server._code_binding_blocked() is True


def test_binding_no_longer_precomputes_a_specification_mode():
    assert not hasattr(HostedSpecificationsConfig, "mode_for")
```

The fixture's binding carries `iwiki_id="wiki-a"`, so the override names that tenant. The
`token_default` source is what the gate exists to refuse.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_specification_status_lint.py -v -k "gate or precompute"`
Expected: FAIL — `mode_for` still exists and the gate reads the process-wide setting.

- [ ] **Step 3: Stop resolving policy in `_binding`**

In `src/iwiki_mcp/http.py` delete the `specifications` / `specification_mode` block from
`_binding` and stop passing `specification_mode=` to `base.PostgresBinding`. The binding's
mode now comes only from `_specification_binding(binding, domain)`.

- [ ] **Step 4: Add the server-side adapter**

```python
def _resolve_binding_policy(binding, domain: str | None):
    """Resolve the hosted policy for one bound domain, or for the tenant alone."""
    from .postgres.policy import resolve_policy

    return resolve_policy(
        _HOSTED_SPECIFICATIONS,
        _HOSTED_CODE_GRAPH,
        binding.iwiki_id,
        domain,
        getattr(binding, "project_policy", None),
    )
```

Rewrite the hosted branch of `_specification_policy_details` to use it:

```python
    if _is_postgres(binding) and _SESSION_BINDING.get() is not None:
        resolved = _resolve_binding_policy(binding, domain)
        return (
            resolved.value("specification_mode"),
            resolved.source("specification_mode"),
            "specification_mode" in resolved.suppressed,
        )
```

Leave the local branch below it untouched.

- [ ] **Step 5: Rewire the two code-graph consumers**

```python
def _code_binding_blocked() -> bool:
    binding = _resolved_binding()
    if not _is_postgres(binding):
        return False
    resolved = _resolve_binding_policy(binding, None)
    if not resolved.value("require_session_binding"):
        return False
    return _hosted_binding_provenance().get("binding_source") == "token_default"


def _postgres_code_reader(binding: base.PostgresBinding):
    resolved = _resolve_binding_policy(binding, binding.primary)
    return _postgres_codegraph.PostgresCodeGraphReader(
        binding.connection_dsn(),
        binding.iwiki_id,
        binding.primary,
        max_snapshot_age_seconds=resolved.value("max_snapshot_age_seconds"),
    )
```

`_code_binding_blocked` passes `project_policy=None` implicitly by resolving with
`domain=None`; the resolver skips the project tier for a field that is not in it, so the
gate cannot be influenced by a caller.

- [ ] **Step 6: Run the suite**

Run: `uv run pytest tests/ -q -x --ignore=tests/postgres --ignore=tests/deployment`
Expected: PASS

- [ ] **Step 7: Lint and commit**

```bash
uv run flake8 src/iwiki_mcp/http.py src/iwiki_mcp/server.py tests/test_specification_status_lint.py
git add src/iwiki_mcp/http.py src/iwiki_mcp/server.py tests/test_specification_status_lint.py
git commit -m "refactor(policy): resolve every hosted policy consumer through one resolver"
```

---

### Task 5: Resolve the specification mode per domain inside the store

**Files:**
- Modify: `src/iwiki_mcp/postgres/store.py:343-378`
- Modify: `src/iwiki_mcp/postgres/store.py:1338-2164`
- Modify: `tests/postgres/conftest.py:216-243` (the `store_factory` fixture)
- Test: `tests/postgres/test_specifications.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `PostgresStore(..., specification_mode: str | Callable[[str], str] = "optional")` and the private `PostgresStore._mode_for(domain: str) -> str`.

- [ ] **Step 1: Write the failing test**

First extend the existing `store_factory` fixture (`tests/postgres/conftest.py:216`), which
today accepts only `iwiki_id` and `embedder`:

```python
    def factory(iwiki_id="wiki-a", *, embedder=_embed, specification_mode="optional"):
        store = PostgresStore(
            clean_postgres,
            iwiki_id,
            cfg,
            embedder=embedder,
            specification_mode=specification_mode,
        )
        store.create_wiki(iwiki_id)
        store.create_domain("docs")
        return store
```

Then the tests:

```python
# tests/postgres/test_specifications.py
def test_store_resolves_the_mode_per_domain(store_factory):
    store = store_factory(
        specification_mode=lambda domain: "disabled" if domain == "billing" else "strict"
    )

    assert store._mode_for("docs") == "strict"
    assert store._mode_for("billing") == "disabled"


def test_store_still_accepts_a_plain_string(store_factory):
    store = store_factory(specification_mode="strict")

    assert store._mode_for("docs") == "strict"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `IWIKI_TEST_POSTGRES_DSN=... uv run pytest tests/postgres/test_specifications.py -v -k mode_for`
Expected: FAIL with `AttributeError: 'PostgresStore' object has no attribute '_mode_for'`

- [ ] **Step 3: Accept a resolver in the constructor**

```python
        if callable(specification_mode):
            self._specification_mode_for = specification_mode
        else:
            if specification_mode not in {"disabled", "optional", "strict"}:
                raise ValueError("specification mode is invalid")
            self._specification_mode_for = lambda _domain: specification_mode

    def _mode_for(self, domain: str) -> str:
        return self._specification_mode_for(domain)
```

Keep the public attribute assignment `self.specification_mode = specification_mode` only if
another module reads it; grep first with `grep -rn "\.specification_mode" src/ tests/` and
delete the attribute if nothing outside the store reads it.

- [ ] **Step 4: Replace every branch**

Each `self.specification_mode` site becomes `self._mode_for(domain)` with the domain already
in scope:

- `store.py:1338` inside `_prepare_specification_projection(self, domain, ...)` → `self._mode_for(domain)`
- `store.py:1373, 1393, 1397, 1411` → the enclosing method's `domain` argument
- `store.py:1435` — the retry wrapper has no domain; add a `domain: str` parameter and pass it from both call sites
- `store.py:1443` `replace_specification_projection` → `self._mode_for(projection.domain)`
- `store.py:1459, 1476` `specification_context` / its neighbour → the method's `domain`
- `store.py:1541, 1553` → the method's `domain`
- `store.py:2100, 2128, 2129, 2157, 2160, 2164` inside the index path → that path's `domain`
- `store.py:378` — the `replace`-style constructor call forwards `self._specification_mode_for`

`search_specifications(self, domains, query, limit)` changes behaviour deliberately: instead
of returning `()` when one scalar mode is `disabled`, it filters the searched set.

```python
        searchable = tuple(
            domain for domain in valid_domains if self._mode_for(domain) != "disabled"
        )
        if not searchable:
            return ()
```

and the projection loop iterates `searchable`.

- [ ] **Step 5: Run the store suite**

```bash
docker run -d --name iwiki-pgtest -e POSTGRES_PASSWORD=pgtest -e POSTGRES_DB=iwiki_test -p 127.0.0.1:55432:5432 pgvector/pgvector:pg16
docker exec iwiki-pgtest psql -U postgres -d iwiki_test -c "CREATE EXTENSION IF NOT EXISTS vector;"
IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55432/iwiki_test" uv run pytest -q tests/postgres
```
Expected: PASS, no skips in `tests/postgres`

- [ ] **Step 6: Lint and commit**

```bash
uv run flake8 src/iwiki_mcp/postgres/store.py tests/postgres/test_specifications.py
git add src/iwiki_mcp/postgres/store.py tests/postgres/test_specifications.py
git commit -m "refactor(store): resolve the specification mode per domain"
```

---

### Task 6: Honour per-domain policy in the admin import

**Files:**
- Modify: `src/iwiki_mcp/admin.py:505-511`
- Test: `tests/postgres/test_admin_import_policy.py`

**Interfaces:**
- Consumes: `resolve_policy` from Task 1, `PostgresStore`'s callable mode from Task 5.
- Produces: no new public surface.

- [ ] **Step 1: Write the failing test**

The fixture is `admin_runtime` (`tests/postgres/test_admin.py:23`), not a service object;
read that file first and follow how its existing import tests build a Git tree and assert on
the result — this task adds one test in the same shape, in its own module.

```python
# tests/postgres/test_admin_import_policy.py
import psycopg

from iwiki_mcp.postgres.config import HostedSpecificationsConfig, PolicyOverride


def _page(scenario_id):
    """One specification page whose single fence is valid and complete."""
    return (
        "# Billing\n\n## Scenario\n\n"
        "Lead paragraph under the heading.\n\n"
        "```iwiki-gwt\n"
        f'id = "{scenario_id}"\n'
        f'title = "{scenario_id}"\n'
        'given = [{ role = "state", name = "Ready" }]\n'
        'when = { role = "command", name = "Do" }\n'
        'then = [{ role = "event", name = "Done" }]\n'
        'code = [\n'
        '  { relation = "implements", phase = "when", symbol = "pkg.mod.fn" },\n'
        '  { relation = "verifies", file = "tests/test_x.py" },\n'
        ']\n'
        "```\n"
    )


def _projection_rows(dsn, iwiki_id, domain):
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM iwiki.specification_scenarios s "
                "JOIN iwiki.domains d ON d.domain_id = s.domain_id "
                "WHERE d.iwiki_id = %s AND d.slug = %s",
                (iwiki_id, domain),
            )
            return cursor.fetchone()[0]


def test_git_import_skips_the_projection_for_a_disabled_domain(admin_runtime, tmp_path):
    service, dsn, iwiki_id = admin_runtime
    service.config.specifications = HostedSpecificationsConfig(
        overrides=(
            PolicyOverride(iwiki_id, "billing", {"specification_mode": "disabled"}),
        )
    )
    for domain in ("payments", "billing"):
        (tmp_path / domain).mkdir(parents=True)
        (tmp_path / domain / "scenario.md").write_text(
            _page(f"{domain}-scenario"), encoding="utf-8"
        )

    service.import_git(iwiki_id, str(tmp_path), dry_run=False)

    assert _projection_rows(dsn, iwiki_id, "billing") == 0
    assert _projection_rows(dsn, iwiki_id, "payments") > 0


```

Adjust the unpacking of `admin_runtime` and the projection table name to whatever
`tests/postgres/test_admin.py` and `tests/postgres/test_specification_migrations.py`
actually use; those two files are the authority on both.

- [ ] **Step 2: Run test to verify it fails**

Run: `IWIKI_TEST_POSTGRES_DSN=... uv run pytest tests/postgres/test_admin_import_policy.py -v`
Expected: FAIL — `billing` receives a projection because the store defaults to `optional`

- [ ] **Step 3: Pass a resolver from admin**

```python
    def _store(self, iwiki_id: str) -> PostgresStore:
        valid_id = _validate_identifier(iwiki_id, "iwiki id")

        def _mode(domain: str) -> str:
            return resolve_policy(
                getattr(self.config, "specifications", None),
                getattr(self.config, "code_graph", None),
                valid_id,
                domain,
                None,
            ).value("specification_mode")

        return PostgresStore(
            self.dsn,
            valid_id,
            self.engine_config,
            embedder=_embed,
            specification_mode=_mode,
        )
```

Import `resolve_policy` at the top of `admin.py` from `.postgres.policy`. `project_policy`
is `None` here by construction: an administrative import has no bound project.

- [ ] **Step 4: Run test to verify it passes**

Run: `IWIKI_TEST_POSTGRES_DSN=... uv run pytest tests/postgres/test_admin_import_policy.py -v`
Expected: PASS

- [ ] **Step 5: Lint and commit**

```bash
uv run flake8 src/iwiki_mcp/admin.py tests/postgres/test_admin_import_policy.py
git add src/iwiki_mcp/admin.py tests/postgres/test_admin_import_policy.py
git commit -m "fix(admin): honour each domain's specification mode during a Git import"
```

---

### Task 7: Report the resolved policy in `wiki_status`

**Files:**
- Modify: `src/iwiki_mcp/server.py:2589-2600`
- Test: `tests/test_specification_status_lint.py`

**Interfaces:**
- Consumes: `_resolve_binding_policy` from Task 4, `ResolvedPolicy.as_status` from Task 1.
- Produces: the `policy` block in the `wiki_status` answer.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_specification_status_lint.py
def test_status_reports_every_policy_field_with_its_source(hosted_session):
    hosted_session("session")

    server.wiki_bind(
        read=["payments"], write=["payments"], primary="payments",
        project_policy={"specification_mode": "strict"},
    )

    status = server.wiki_status()

    record = status["policy"]["domains"][0]
    assert record["domain"] == "payments"
    assert record["specification_mode"] == {"value": "strict", "source": "project"}
    assert record["require_session_binding"]["source"] in {
        "hosted_default", "built_in_default",
    }
    assert record["suppressed"] == []
    assert status["specifications"]["domains"][0]["mode"] == "strict"


def test_status_names_a_suppressed_field(hosted_session):
    hosted_session("session")

    server.wiki_bind(
        read=["payments"], write=["payments"], primary="payments",
        project_policy={"specification_mode": "disabled"},
    )

    record = server.wiki_status()["policy"]["domains"][0]

    assert record["specification_mode"]["source"] != "project"
    assert record["suppressed"] == ["specification_mode"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_specification_status_lint.py -v -k policy`
Expected: FAIL with `KeyError: 'policy'`

- [ ] **Step 3: Add the block**

Beside the existing `specifications` assembly in `wiki_status`:

```python
        if _is_postgres(bind) and _SESSION_BINDING.get() is not None:
            result["policy"] = {
                "domains": [
                    {
                        "domain": domain,
                        **_resolve_binding_policy(bind, domain).as_status(),
                    }
                    for domain in bind.read
                ]
            }
```

The existing `specifications` block is not touched.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_specification_status_lint.py -v`
Expected: PASS

- [ ] **Step 5: Lint and commit**

```bash
uv run flake8 src/iwiki_mcp/server.py tests/test_specification_status_lint.py
git add src/iwiki_mcp/server.py tests/test_specification_status_lint.py
git commit -m "feat(status): report every resolved policy field beside its source"
```

---

### Task 8: Scenarios and the live-database verification

**Files:**
- Create: wiki page `reference/hosted-policy-inheritance` in the `iwiki-mcp` domain (via MCP tools, not a repository file)
- Test: the full suite against a disposable PostgreSQL container

**Interfaces:**
- Consumes: every earlier task.
- Produces: three Given-When-Then scenarios bound to the implementation and the tests.

- [ ] **Step 1: Write the scenarios**

The bound domain is `strict`, so new observable behavior needs scenarios. Author them on
one `type: specification` page with `wiki_write_page`, one `iwiki-gwt` fence per `##`
section, with these IDs and bindings:

```toml
id = "resolve-tenant-wide-policy-override"
title = "Resolve a tenant-wide policy override"
given = [
  { role = "state", name = "HostedOverrideWithoutDomain" }
]
when = { role = "request", name = "ResolvePolicyForDomain" }
then = [
  { role = "outcome", name = "OverrideValueAppliesToEveryDomain" }
]
code = [
  { relation = "implements", phase = "when", symbol = "iwiki_mcp.postgres.policy.resolve_policy" },
  { relation = "verifies", file = "tests/test_policy_resolution.py" }
]
```

```toml
id = "suppress-a-looser-project-policy"
title = "Suppress a looser project policy"
given = [
  { role = "state", name = "HostedDefaultStricterThanProjectValue" }
]
when = { role = "command", name = "BindProjectPolicy" }
then = [
  { role = "outcome", name = "HostedValueStandsAndFieldIsSuppressed" }
]
code = [
  { relation = "implements", phase = "when", symbol = "iwiki_mcp.postgres.policy.resolve_policy" },
  { relation = "verifies", file = "tests/test_specification_status_lint.py" }
]
```

```toml
id = "accept-the-deprecated-specification-mode-alias"
title = "Accept the deprecated specification mode alias"
given = [
  { role = "state", name = "HostedSessionWithoutProjectPolicy" }
]
when = { role = "command", name = "BindWithSpecificationModeAlias" }
then = [
  { role = "outcome", name = "AliasResolvesLikeTheObjectMember" }
]
code = [
  { relation = "implements", phase = "when", symbol = "iwiki_mcp.server._wiki_bind" },
  { relation = "verifies", file = "tests/test_specification_tools.py" }
]
```

- [ ] **Step 2: Run the default suite**

Run: `uv run pytest -q`
Expected: PASS with `tests/postgres` and `tests/deployment` skipping

- [ ] **Step 3: Run the PostgreSQL suite against a live database**

```bash
docker run -d --name iwiki-pgtest -e POSTGRES_PASSWORD=pgtest -e POSTGRES_DB=iwiki_test -p 127.0.0.1:55432:5432 pgvector/pgvector:pg16
docker exec iwiki-pgtest psql -U postgres -d iwiki_test -c "CREATE EXTENSION IF NOT EXISTS vector;"
IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55432/iwiki_test" uv run pytest -q tests/postgres
docker rm -f iwiki-pgtest
```
Expected: PASS, zero skips inside `tests/postgres`

A loopback DSN errors `tests/deployment` at its own fixture precondition
(`IWIKI_TEST_POSTGRES_DSN must use a non-loopback host`). That is an environment limit, not
a regression — record it as such rather than treating it as a failure.

- [ ] **Step 4: Resolve the scenarios**

Call `wiki_spec_resolve` for each of the three IDs and record the outcome (`resolved`,
`ambiguous`, `unresolved`, or `graph_unavailable`) on the task page.

- [ ] **Step 5: Lint and commit**

```bash
uv run flake8 src tests
git add -A
git commit -m "test: cover hosted policy inheritance against a live database"
```

---

### Task 9: Documentation and version

**Files:**
- Modify: `README.md:1146-1173`
- Modify: `docs/README.ru.md` (the matching section)
- Modify: `docs/architecture.md:265-285`
- Modify: `src/iwiki_mcp/base.py:54-78`
- Modify: `src/iwiki_mcp/resources.py:170-180`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: the final behavior of every earlier task.
- Produces: no code surface.

- [ ] **Step 1: Rewrite the hosted policy documentation**

In `README.md`, replace the hosted policy block with the new record shape and the new
precedence sentence:

````markdown
```toml
[specifications]
default_mode = "optional"
allow_project_mode = true

[[specifications.overrides]]
iwiki_id = "team-wiki"
specification_mode = "disabled"          # every domain of this tenant

[[specifications.overrides]]
iwiki_id = "team-wiki"
domain = "payments"
mode = "strict"                          # deprecated alias for specification_mode
require_session_binding = true
```

Hosted precedence is resolved per field: an exact `(iwiki_id, domain)` override, a
tenant-wide override with `domain` omitted, the project value carried by
`wiki_bind(project_policy=…)`, the hosted default, then the built-in value. A key absent
from a record falls through to the next tier rather than taking the record's other values
with it. The project tier applies only when its value is at least as strict as the hosted
default and `allow_project_mode` is true; otherwise `wiki_status` names the field in
`policy.domains[].suppressed`. `require_session_binding` is operator-only: the gate that
reads it judges a session that never bound, so a value carried by a bind cannot exist when
it decides.
````

Apply the identical change to `docs/README.ru.md` in Russian.

- [ ] **Step 2: Update the architecture page**

In `docs/architecture.md`, replace the precedence paragraph with the per-field chain and add
`postgres/policy.py` to the module list as the framework-free policy resolver.

- [ ] **Step 3: Update the project configuration template**

In `src/iwiki_mcp/base.py`, extend the commented template so a reader learns which keys the
hosted transport carries:

```python
# [specifications]
# mode = "optional"  # disabled | optional | strict
# Hosted transports carry this value and [code_graph] max_snapshot_age_seconds
# to the server through wiki_bind(project_policy=...). The server may only be
# tightened, never loosened, by a project.
```

- [ ] **Step 4: Bump the version**

Patch bump in `pyproject.toml`, per the repository's versioning rule.

- [ ] **Step 5: Verify the documentation matches the code**

```bash
uv run pytest tests/test_resources.py -q
uv run flake8 src tests
```
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add README.md docs/README.ru.md docs/architecture.md src/iwiki_mcp/base.py src/iwiki_mcp/resources.py pyproject.toml
git commit -m "docs: describe per-field hosted policy inheritance"
```

---

## Verification checklist

Run before requesting the result gate.

- [ ] `uv run pytest -q` — green, with only `tests/postgres` and `tests/deployment` skipping
- [ ] `IWIKI_TEST_POSTGRES_DSN=… uv run pytest -q tests/postgres` — green, zero skips
- [ ] `uv run flake8 src tests` — clean
- [ ] A server TOML in today's shape loads unedited and resolves what it resolved before
- [ ] `wiki_status` reports `policy.domains[]` with a `source` per field and an unchanged `specifications` block
