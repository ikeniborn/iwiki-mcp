# Intent: hosted-project-policy-inheritance

**Date:** 2026-09-16
**Status:** approved

## Objective

The hosted server resolves exactly one policy key per `(iwiki_id, domain)` pair:
`specifications.mode`. Every other hosted policy value lives in `HostedCodeGraphConfig` and
is process-wide. A per-project decision therefore costs a server TOML edit plus a container
recreate, and an override record covers one domain at a time — a tenant with eighteen bound
domains needs eighteen records.

The trigger is concrete: a project declaring `[specifications] mode = "disabled"` was
answered with `optional / hosted_default`, because a project tier that is looser than the
hosted default is suppressed by design. The operator's only remedy today is an exact
override per domain.

Generalise the existing single-key mechanism into one inheritance tier that covers the whole
policy surface, so the server value is the default and the project fills in what it declares.
Adding a fourth policy key later must cost one allowlist entry, not a new mechanism.

## Desired Outcomes

- A project new to the deployment gets its policy from `.iwiki.toml` plus `wiki_bind`, with
  no server TOML edit and no restart.
- One override record with the domain omitted applies to every domain of that `iwiki_id`;
  an exact `(iwiki_id, domain)` record still wins over it.
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

## Health Metrics

- `specification_mode` resolution is unchanged for every input that exists today: the four
  current tiers, the rank guard, `allow_project_mode = false`, and `project_mode_suppressed`
  all keep their present outcomes.
- Resource limits stay outside the project tier: `max_batch_rows`, `max_batch_bytes`,
  `publication_session_ttl_seconds`, `staging_retention_seconds`, `staging_cleanup_limit`,
  `pool_min_size`, `pool_max_size`, `statement_timeout_ms`, `lock_timeout_ms`,
  `allowed_origins`, and every `[storage]` key remain server-only and uninfluenced by any
  client input.
- A client that sends no policy object observes today's behavior exactly.
- `tests/postgres` and `tests/deployment` pass against a real database, not by skipping.
- `uv run flake8 src tests` stays clean.

## Strategic Context

- Interacts with: hosted clients that build the bind call (iCodex, iClaude — the
  `hosted-specification-mode-mismatch` task established that a field the client omits is
  silently lost and reported as `hosted_default`); the `framework` deployment, which already
  relies on an exact override; the code-graph gates that read
  `require_session_binding` and `max_snapshot_age_seconds`; the operator documentation in
  `README.md`, `docs/README.ru.md`, `docs/architecture.md`, and the wiki pages
  `reference/specification-mode-project-precedence`, `concept/bdd-event-sourcing-specifications`,
  and `base-binding`.
- Priority trade-off: **trust**. This is the policy tier that decides what a caller may
  weaken. A wrong resolution is a policy failure, not a latency or cost regression.

## Constraints

### Steering (behavioral guidance)

- Generalise the existing resolver rather than writing a second one; `specification_mode`
  must end up as one entry in the new mechanism, not a parallel code path.
- Keep the resolver free of per-key special cases beyond a declared comparison function per
  field.
- Keep `postgres/config.py` strict: unknown keys stay a `ConfigError`, and bounds stay
  validated before use.
- Report suppression rather than silently substituting a value; an agent must be able to see
  that its declaration had no effect.

### Hard (architectural enforcement)

- The project tier may only tighten. Ordering per field: `specification_mode` by the existing
  rank `disabled < optional < strict`; `require_session_binding` by `false < true`;
  `max_snapshot_age_seconds` by `min()` with `0` treated as infinity, so a project may lower
  the age ceiling but never raise it and never disable age rejection.
- The set of keys reachable from the project tier is an explicit allowlist. A test fails when
  a key is added to that allowlist without a declared tighten comparison.
- No key that bounds server resource consumption ever enters the project tier or the bind
  contract.
- `allow_project_mode = false` continues to disable the project tier for every key at once.
- The `[specifications]` table keeps its name and its existing keys. New keys extend the
  override record, and `domain` becomes optional. No existing configuration file requires an
  edit to keep working.
- `wiki_bind` gains exactly one new optional parameter, an object `project_policy`. The
  existing `specification_mode` parameter stays accepted as a deprecated alias with identical
  behavior; sending both a conflicting alias and object is a validation error. A key the
  allowlist does not contain, or a value outside a key's declared domain, is rejected as a
  bind validation error and leaves the previous binding unchanged — an object member is never
  silently ignored, because the object hides its members from the tool schema.
- Session scope is unchanged: the policy is session state, never persisted, and lost on
  reconnect.
- No database migration and no schema change.

## Autonomy Zones

- Full autonomy (reversible, low risk): implementation in `src/`, tests, Given-When-Then
  scenarios, `README.md` / `docs/README.ru.md` / `docs/architecture.md`, wiki pages in the
  bound primary domain, the `dev-hosted-project-policy-inheritance` branch and its commits,
  the version bump in `pyproject.toml`.
- Guarded (log + confidence threshold): the shape of the `project_policy` object and the
  suppression reporting format — decide and record the reasoning in the ledger.
- Proposal-first (needs approval): any change to the allowlist beyond the three named keys;
  any change to the deprecation handling of the `specification_mode` parameter; opening the
  pull request.
- No autonomy (human only): editing the server TOML on the `framework` deployment, restarting
  or recreating that container, and merging the pull request.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions
> is marked HUMAN CHECKPOINT in the plan.

## Stop Rules

- Halt if: a hosted resolution path is found that reads a policy value without going through
  the generalised resolver, because the tier would then apply inconsistently. Audit every
  reader before continuing — the secondary finding on
  `reference/specification-mode-project-precedence` already flags `_binding()` in
  `http.py` as a candidate.
- Halt if: keeping `[specifications]` in place turns out to require a breaking change to an
  existing key after all.
- Escalate if: honouring the tighten-only rule for a key requires weakening it for another,
  or if a resource limit turns out to be already reachable from client input.
- Done when: against a disposable PostgreSQL server, a project whose `.iwiki.toml` declares
  all three policy keys binds and `wiki_status` reports each key with `source: project` for
  every bound domain, with no server TOML entry for that tenant; a single override record
  without `domain` changes the resolution for every domain of that tenant; a looser project
  value is reported suppressed with the hosted value in effect; the deployment's current
  server TOML loads unedited and resolves what it resolves today; and `tests/postgres` plus
  `tests/deployment` pass against that database rather than skipping.
