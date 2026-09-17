# Given-When-Then specifications

*Part of the [iwiki-mcp documentation](../README.md#documentation). Русская версия: [specifications.ru.md](specifications.ru.md).*

Ordinary Wiki pages and explicit `type: specification` pages coexist in every domain.
Ordinary Wiki pages keep their existing write, index, search, and lint behavior in all
specification modes and never require a code graph.

The page type is what admits a scenario: only a page whose `type` normalizes to
`specification` is parsed into the projection. An `iwiki-gwt` fence anywhere else is
stored as ordinary Markdown and is never projected, searched, or resolved, so
`wiki_write_page` and `wiki_update_page` answer with a `warning` when a scenario lands
on such a page, and `wiki_lint` reports it as the advisory section finding
`unprojected_scenario`. Neither blocks the write — a documentation example that quotes
the fence is legitimate — but the fence language is the marker of intent, so quote
examples as ```` ```toml ```` when they are illustrations rather than scenarios.

Local Git and local PostgreSQL read the project policy from `.iwiki.toml`:

```toml
[specifications]
mode = "optional"
```

Hosted PostgreSQL reads operator policy from server TOML:

```toml
[specifications]
default_mode = "optional"
allow_project_mode = true

[[specifications.overrides]]
iwiki_id = "team-wiki"
specification_mode = "disabled"          # every domain of this tenant
require_session_binding = true           # operator-only; domain must stay omitted

[[specifications.overrides]]
iwiki_id = "team-wiki"
domain = "payments"
mode = "strict"                          # deprecated alias for specification_mode
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

Local policy applies to all visible domains; hosted policy is resolved separately for
each bound domain and is session-scoped. `disabled` stores specification pages as
ordinary Markdown and disables semantic tools. `optional` stores Markdown, reports
advisory findings, and projects only valid, complete, unique scenarios. `strict` rejects
an invalid target specification mutation before page or projection changes.

The three hosted policy fields are `specification_mode`, `max_snapshot_age_seconds`, and
`require_session_binding`. The first two accept a project value through
`wiki_bind(project_policy=…)`; `specification_mode` also accepts the deprecated
`wiki_bind.specification_mode` alias, and passing both is a validation error.
`require_session_binding` never accepts a project value, by construction rather than by
exception. `wiki_status`'s `specifications` block still reports only
`specification_mode`, unchanged; a sibling `policy` block reports every field's resolved
value and source, so `specification_mode` appears in both — that duplication is the
deliberate price of not breaking existing readers.

Each scenario is one fenced TOML block inside its H2 section:

```iwiki-gwt
id = "confirm-account-opening"
title = "Confirm account opening"
given = [{ role = "event", name = "AccountOpeningRequested" }]
when = { role = "command", name = "ConfirmAccountOpening" }
then = [{ role = "event", name = "AccountOpened" }]
code = [
  { relation = "implements", phase = "when", symbol = "accounts.Account.confirm" },
  { relation = "verifies", symbol = "tests.accounts.test_confirm_account_opening" }
]
```

`given` roles are `event`, `state`, or `fact`; `when` roles are `command`, `request`,
or `action`; `then` roles are `event`, `response`, `outcome`, or an exclusive
`exception`. A complete scenario has at least one `implements` and one `verifies`
binding. Its ID remains stable while wording or locations change without changing
observable behavior.

Grammar is closed and bounded. `id` is required, contains 1-128 UTF-8 bytes, and
matches `[a-z0-9]+(?:-[a-z0-9]+)*`. `title` is required and nonblank, contains no NUL,
and is at most 250 Unicode code points. Every phase-item `name` is required and
nonblank, contains no NUL, and is at most 1,024 UTF-8 bytes. Unknown keys at the top
level, in phase items, or in bindings are invalid; malformed TOML and duplicate TOML
keys are invalid.

`given` is required and accepts 0 or more items. `when` is required and contains
exactly one item. `then` is required and contains 1 or more items. `code` is required
and contains 1 or more bindings. In each binding, `phase` is optional and exactly one
of `symbol`, `file`, or `source_glob` is required. Completeness requires at least one
`implements` and one `verifies` binding.

Binding grammar is exact: relation is exactly `implements | verifies`; `phase` is
optional and exactly `given | when | then`. Every selector value is a nonempty UTF-8
string of at most 4,096 bytes with no NUL. `symbol` is a code-graph qualified-name
string, but the parser enforces only the shared selector scalar constraints and no
stricter symbol regex. `file` and `source_glob` are trimmed, safe, relative POSIX paths
or patterns with at most 256 path segments; they reject a backslash, absolute path,
Windows drive, empty segment, `.` or `..`. `file` forbids glob metacharacters `*`, `?`,
and `[`, while `source_glob` allows them. `code` is limited to at most 256 bindings.
Duplicate phase identity `(phase, role, name)` is invalid. Duplicate binding identity
`(relation, phase, selector kind, selector)` is invalid.

The mode matrix also governs existing invalid content. Disabled mode produces no
projection and no specification findings for missing, invalid, duplicate, or incomplete
specification pages. Optional mode makes every specification finding advisory. In
strict mode, syntax (`missing_scenario` and `invalid_scenario`),
`duplicate_scenario_id`, and `incomplete_bindings` findings are blocking only for future
mutations of the reported explicit specification page. Projection and resolution
findings remain advisory; ordinary Wiki pages remain unaffected in every mode.

The exact semantic tool surface is:

- `wiki_spec_search`: search projected scenarios inside the caller's read scope.
- `wiki_spec_context`: read one scenario plus persisted evidence and freshness without
  resolving or mutating it.
- `wiki_spec_resolve`: resolve declared bindings and persist sanitized evidence; it
  requires write scope and never expands binding or code-publication authority. Hosted,
  its `domain` must be the bound primary. A refusal names which of the caller's own
  binding relations answered — `invalid_domain`, `primary_not_selected`,
  `primary_not_writable`, or `not_bound_primary` — so a scenario living outside the
  bound primary is not mistaken for a missing grant.

`wiki_status` reports effective mode and projection state. `wiki_lint` adds independent
specification findings without hiding ordinary Wiki findings. Git stores the canonical
Markdown plus tracked, rebuildable `<domain>/specifications.jsonl`; PostgreSQL stores the
same logical projection transactionally under tenant/domain authorization. Markdown
remains canonical in both backends.

The `wiki_status` specification record contains `domain`, `mode`, `source`,
`projection_state`, `scenarios`, and `bindings`, plus
`project_mode_suppressed: true` when a received project mode is disabled by the server
switch or rejected by the tighten-only guard. Source is
`project | hosted_default | hosted_override | built_in_default`; projection state is
`disabled | absent | ready | stale | failed`. The complete `wiki_lint` finding taxonomy
is `missing_scenario`, `invalid_scenario`, `duplicate_scenario_id`,
`incomplete_bindings`, `projection_stale`, `projection_failed`, `binding_unresolved`,
`binding_ambiguous`, `resolution_not_checked`, `resolution_stale_spec`,
`resolution_stale_graph`, and `graph_unavailable`. Lint stays read-only and retains the
complete ordinary Wiki report.

Resolution evidence records `resolved`, `ambiguous`, `unresolved`, or
`graph_unavailable`. Context computes `fresh`, `stale_spec`, or `stale_graph` against
current specification and graph revisions. If the graph is absent or unusable, preserve
selectors and continue with repository search and executable tests; never block ordinary
Wiki work. Record the actual test command, exit status, and repository revision separately
because a `verifies` binding identifies a test but does not run it.

Run the deterministic, non-timing-gated path measurement with:

```bash
uv run pytest -q -m measurement tests/measurement/test_specification_paths.py -s
```
