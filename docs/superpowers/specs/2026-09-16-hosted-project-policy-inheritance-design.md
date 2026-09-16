---
chain:
  intent: docs/superpowers/intents/2026-09-16-hosted-project-policy-inheritance-intent.md
review:
  spec_hash: 1bd28b22a34a9f80
  last_run: 2026-09-16
  phases:
    - name: structure
      status: passed
    - name: coverage
      status: passed
    - name: clarity
      status: passed
    - name: consistency
      status: passed
  findings:
    - id: F-001
      phase: clarity
      severity: WARNING
      section: 9. Testing
      section_hash: cfb59bbc03c438a6
      fragment: "tenant wildcard"
      text: >-
        One entity carried two names: sections 3 and 4 call it a tenant override,
        section 9 called it a tenant wildcard.
      fix: Use "tenant override" in section 9.
      verdict: fixed
      verdict_at: 2026-09-16
    - id: F-002
      phase: consistency
      severity: CRITICAL
      section: 5. Transport
      section_hash: 6ddf56bcee1df059
      fragment: "rejects `project_policy` the way it already rejects `specification_mode`, with `project_config_manual_edit_required`"
      text: >-
        The spec named one refusal where the code gives two. A local PostgreSQL
        binding answers "requires a hosted session"; only a Git binding reaches
        project_config_manual_edit_required. Found while writing the plan's
        Task 3 test.
      fix: >-
        State both answers in section 5 and split the row in section 8.
      verdict: fixed
      verdict_at: 2026-09-16
---

# Design: hosted project policy inheritance

**Date:** 2026-09-16
**Intent:** `docs/superpowers/intents/2026-09-16-hosted-project-policy-inheritance-intent.md`

## 1. Problem

The hosted server resolves exactly one policy key per `(iwiki_id, domain)` pair,
`specifications.mode`, and it resolves it in two independent places that disagree:

- `_specification_policy_details` (`src/iwiki_mcp/server.py:1101`) applies the full chain —
  exact override, project tier under a tighten-only rank guard, hosted default, built-in.
- `HostedSpecificationsConfig.mode_for` called from `_binding` (`src/iwiki_mcp/http.py:245`)
  applies exact override and hosted default only, against `context.primary`, and the result
  becomes `binding.specification_mode` for the whole request.

Every other hosted policy value lives in `HostedCodeGraphConfig` and is process-wide. An
override record covers exactly one domain, so a tenant with eighteen bound domains needs
eighteen records, each requiring a server TOML edit and a container recreate.

## 2. Acceptance (from intent)

Carried verbatim from the approved intent.

**Desired Outcomes**

- A project new to the deployment reaches any policy at least as strict as the resolved
  hosted value from `.iwiki.toml` plus `wiki_bind`, with no server TOML edit and no restart.
  Loosening below the hosted value stays an operator decision and is reached through the
  tenant override below, never through the project tier.
- One override record with the domain omitted applies to every domain of that `iwiki_id`;
  an exact `(iwiki_id, domain)` record still wins over it. This is what makes the originating
  case tractable: a tenant that must run below the hosted default costs the operator one
  record instead of one per bound domain.
- Inheritance is per field, not per record: a key absent from the project falls through to
  the tenant override, then the hosted default, then the built-in value, independently of
  the keys around it.
- `wiki_status` reports all three policy keys per domain, each with its resolving `source`
  (`hosted_override`, `project`, `hosted_default`, `built_in_default`) and a suppression flag
  when a declared project value did not take effect.
- A project value looser than the resolved hosted value changes nothing and is reported as
  suppressed, exactly as `specification_mode` behaves today.
- An existing hosted server TOML loads unchanged and resolves the same effective policy as
  before the change.

**Done when**

Against a disposable PostgreSQL server, a project whose `.iwiki.toml` declares all three
policy keys binds and `wiki_status` reports each key with `source: project` for every bound
domain, with no server TOML entry for that tenant; a single override record without `domain`
changes the resolution for every domain of that tenant; a looser project value is reported
suppressed with the hosted value in effect; the deployment's current server TOML loads
unedited and resolves what it resolves today; and `tests/postgres` plus `tests/deployment`
pass against that database rather than skipping.

## 3. The policy module

A new framework-free module `src/iwiki_mcp/postgres/policy.py`, importable without the MCP
runtime and unit-testable without a database.

```python
@dataclass(frozen=True)
class PolicyField:
    name: str                       # canonical key
    project_tier: bool              # may a bound project supply it
    parse: Callable[[Any], Any]     # value validator, raises ConfigError
    at_least_as_strict: Callable[[Any, Any], bool]
    default_at: tuple[str, str]     # server TOML table and key holding the default
    built_in: Any

POLICY_FIELDS: tuple[PolicyField, ...]
```

`POLICY_FIELDS` is the single allowlist. Its three members:

| key | project tier | ordering | default read from | built-in |
| --- | --- | --- | --- | --- |
| `specification_mode` | yes | `disabled < optional < strict` | `[specifications].default_mode` | `optional` |
| `max_snapshot_age_seconds` | yes | `min()`, `0` treated as infinity | `[code_graph].max_snapshot_age_seconds` | `86400` |
| `require_session_binding` | no | `false < true` | `[code_graph].require_session_binding` | `false` |

Each field's default is read from the table that already holds it today, recorded in
`default_at`. No existing key moves, so no existing configuration file needs an edit.

`require_session_binding` is outside the project tier by construction, not by exception:
`_code_binding_blocked` (`server.py:916`) reads it precisely to judge a session that never
bound, so a value delivered through `wiki_bind` cannot exist at the moment the gate decides.

### Resolution

```python
def resolve_policy(
    specifications: HostedSpecificationsConfig | None,
    code_graph: HostedCodeGraphConfig | None,
    iwiki_id: str,
    domain: str | None,
    project_policy: Mapping[str, Any] | None,
) -> ResolvedPolicy
```

Per field, first match wins:

1. an exact `(iwiki_id, domain)` override that carries this key → `hosted_override`;
2. a tenant override (`iwiki_id`, no `domain`) that carries this key → `hosted_override`;
3. the project value, when the field is `project_tier`, `allow_project_mode` is true, and
   `at_least_as_strict(project_value, tier_3_value)` holds → `project`;
4. the hosted default from `default_at` → `hosted_default`;
5. `built_in` → `built_in_default`.

`domain=None` is a valid call: tiers 1 and 3 are skipped and the tenant override, hosted
default, and built-in decide. This is how `_code_binding_blocked` resolves its field.

A project value rejected at tier 3 — looser than the tier-4 value, or `allow_project_mode`
false — records the field in `ResolvedPolicy.suppressed`.

`ResolvedPolicy` exposes `value(field)`, `source(field)`, `suppressed`, and a `as_status()`
projection used by `wiki_status`.

### Project policy parsing

```python
def parse_project_policy(value: Any) -> dict[str, Any]
```

Accepts a mapping whose keys are a subset of the `project_tier` fields. An unknown key, a
non-mapping value, or a value rejected by a field's `parse` raises `ConfigError`. Nothing is
silently ignored — the object hides its members from the tool schema, so silence would strand
a typo until someone read `suppressed`.

## 4. Server configuration

`[specifications]` keeps its name, its `default_mode`, and its `allow_project_mode`.
`SpecificationOverride` becomes `PolicyOverride`:

```toml
[specifications]
default_mode = "optional"
allow_project_mode = true

[[specifications.overrides]]
iwiki_id = "team-wiki"
specification_mode = "disabled"          # whole tenant

[[specifications.overrides]]
iwiki_id = "team-wiki"
domain = "payments"
mode = "strict"                          # deprecated alias, still accepted
require_session_binding = true
```

- `iwiki_id` stays required. `domain` becomes optional; omitted means the whole tenant.
- A record carries any subset of the policy keys. An absent key is not a value — it falls
  through, which is what makes inheritance per field rather than per record.
- `mode` remains accepted as an alias for `specification_mode`. Both in one record is a
  `ConfigError`.
- Two records with the same `(iwiki_id, domain)`, including two tenant records for one
  `iwiki_id`, remain a duplicate `ConfigError`.
- Unknown keys stay a `ConfigError`, so a malformed server TOML still refuses to start.

The section name is now wider than its content: two of the three keys are code-graph
concerns. This is the deliberate cost of not breaking existing files; documentation carries
the explanation and no second table name is introduced.

## 5. Transport

`wiki_bind` gains one optional parameter:

```python
wiki_bind(read=…, write=…, primary=…, project_policy={"specification_mode": "strict",
                                                      "max_snapshot_age_seconds": 3600})
```

- Members are the `project_tier` fields. An unknown member or an invalid value is a
  validation error and the previous binding is unchanged.
- `specification_mode=` stays accepted as a deprecated alias. Supplying both the alias and
  `project_policy["specification_mode"]` is a validation error.
- The value is session state on `_HostedBindingState`, never persisted, lost on reconnect —
  unchanged from the current `project_specification_mode` behavior.
- The local stdio path rejects `project_policy` exactly where it already rejects
  `specification_mode`, and with the same two distinct answers the current code gives: a
  local PostgreSQL binding answers `project policy requires a hosted session`, while a Git
  binding falls through to `project_config_manual_edit_required`. That server reads
  `.iwiki.toml` itself, so a client override would be a second source of truth.
- `http.py`'s `wiki_bind` authorization is untouched — a policy object selects no domain.

`PostgresBinding.project_specification_mode` is replaced by
`project_policy: Mapping[str, Any] | None`.

## 6. Application points

**`_binding` stops resolving policy.** `HostedSpecificationsConfig.mode_for` is deleted and
`http.py:_binding` no longer sets `specification_mode`. Keeping it would leave two resolvers
disagreeing about a tenant override, since `mode_for` knows neither the project tier nor the
tighten guard.

**`_specification_policy_details`** becomes a thin adapter over `resolve_policy`, preserving
its current return shape for `wiki_status`.

**`_postgres_code_reader(binding)`** takes `max_snapshot_age_seconds` from
`resolve_policy(..., domain=binding.primary, project_policy=binding.project_policy)`. The
value is already echoed to clients by `postgres/codegraph.py:1500`, so its per-project
resolution is observable.

**`_code_binding_blocked()`** takes `require_session_binding` from
`resolve_policy(..., domain=None, project_policy=None)`.

**`PostgresStore` stops holding a scalar mode.** Its constructor argument accepts either a
string — wrapped into a constant resolver, preserving every existing call — or a
`Callable[[str], str]`. Every `self.specification_mode` site becomes `self._mode_for(domain)`
with the domain already in scope, except:

- `search_specifications(domains: tuple[str, ...])`, where a `disabled` domain drops out of
  the searched set instead of emptying the whole result;
- the retry wrapper at `store.py:1435`, which receives the domain as a parameter.

**`admin.py`** resolves policy per domain. `_store(iwiki_id)` passes a resolver built from
`resolve_policy(..., project_policy=None)`, so `import_git` — which imports many domains in
one atomic transaction — honours each domain's own mode instead of the constructor default
`optional`. This closes a divergence that predates this work: an import into a `disabled`
domain currently builds a specification projection anyway.

## 7. Reporting

`wiki_status` keeps `specifications.domains[]` byte-for-byte as it is today, because the
SessionStart protocol and existing clients read it. A new sibling block carries the full
policy:

```json
"policy": {"domains": [
  {"domain": "iwiki-mcp",
   "specification_mode": {"value": "strict", "source": "project"},
   "max_snapshot_age_seconds": {"value": 86400, "source": "hosted_default"},
   "require_session_binding": {"value": false, "source": "hosted_default"},
   "suppressed": ["max_snapshot_age_seconds"]}
]}
```

A field named in `suppressed` always reports a `source` other than `project`: the project
declared it and the tier rejected it, so the value shown is the one that actually applies.
Here the project asked to raise the age ceiling above the hosted `86400` and was refused.

`specification_mode` therefore appears twice. That duplication is the price of leaving the
existing block untouched, and it is stated in the operator documentation rather than hidden.

## 8. Errors

| Condition | Result |
| --- | --- |
| Unknown key in the server TOML | `ConfigError`, the server does not start |
| `mode` and `specification_mode` in one override record | `ConfigError` |
| Duplicate `(iwiki_id, domain)` override, tenant records included | `ConfigError` |
| Unknown member in `project_policy` | bind validation error, previous binding unchanged |
| Invalid value in `project_policy` | bind validation error, previous binding unchanged |
| Alias and object member both supplied | bind validation error |
| `project_policy` on local stdio PostgreSQL | `project policy requires a hosted session` |
| `project_policy` on a Git binding | `project_config_manual_edit_required` |

## 9. Testing

- Unit tests for `policy.py` with no database and no MCP runtime: per-field precedence, the
  tenant override, `domain=None`, suppression, `allow_project_mode = false`, and the
  `0`-as-infinity rule for `max_snapshot_age_seconds`.
- A guard test asserting that every member of `POLICY_FIELDS` carries `at_least_as_strict`
  and `parse`, and that every `project_tier` member has a suppression test. Adding a field
  without them fails the suite.
- A compatibility test loading the current production-shaped server TOML unedited and
  asserting the resolved policy equals today's.
- `tests/postgres` against a disposable pgvector container, covering the bind contract, the
  `wiki_status` policy block, and the admin import honouring a `disabled` domain.
- Given-When-Then scenarios in the `iwiki-mcp` domain, which is `strict`: tenant-wide
  resolution, suppression of a looser project value, and the deprecated alias.

## 10. Out of scope

- Durable per-domain policy in the database, a `wiki_set_specification_mode` tool, and a
  grant model for who may set a domain's policy. `reference/specification-mode-project-precedence`
  already records these as deliberately excluded; session scope is unchanged here.
- Resource limits — `max_batch_rows`, `max_batch_bytes`, `publication_session_ttl_seconds`,
  `staging_retention_seconds`, `staging_cleanup_limit`, `pool_*`, `statement_timeout_ms`,
  `lock_timeout_ms`, `allowed_origins`, and every `[storage]` key. They bound what a caller
  may consume and never enter the project tier or the bind contract.
- Project `[code_graph]` extraction keys. They describe a checkout the hosted server cannot
  read.
- A second server TOML table name. `[specifications]` is extended in place.

## 11. Documentation

- `README.md` and `docs/README.ru.md`: the hosted policy table, the override record shape
  with the optional `domain`, the `project_policy` parameter, and the `policy` status block.
- `docs/architecture.md`: the precedence chain and the new module boundary.
- `src/iwiki_mcp/base.py:54` project configuration template comments.
- `src/iwiki_mcp/resources.py`: the mode description, if the precedence wording changes.
- Wiki pages `reference/specification-mode-project-precedence`, `base-binding`, and
  `concept/bdd-event-sourcing-specifications`, whose precedence sentences become wrong.
- A patch version bump in `pyproject.toml`.
