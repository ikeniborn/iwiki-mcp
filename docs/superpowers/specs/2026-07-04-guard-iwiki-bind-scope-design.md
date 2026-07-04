# Design: guard iwiki bind scope

## Acceptance (from intent)
- Opening a project no longer triggers iwiki-driven bootstrap or automatic isolated-environment loading through agent instructions.
- `wiki_bind(read=...)` does not overwrite an existing non-empty `read` list.
- If `read` contains only other domains and does not contain the current project domain, `wiki_bind` may add the current project domain while preserving the existing domains.
- `wiki_bind(write=...)` only accepts the current project domain as the write target.
- Tests cover forbidden `read` replacement, allowed current-project-domain append, and rejected non-current `write`.
- Done when: the problematic bind scenarios are reproduced in tests, tests pass, docs/wiki are updated, and `.iwiki.toml` `read`/`write` can no longer be changed contrary to the constraints above.

## 1. Architecture
Enforce the binding policy inside `server.wiki_bind`, backed by small pure helpers in `base.py`. The server is the trust boundary: agent instructions can reduce accidental calls, but only server-side validation prevents installed agents or direct MCP callers from rewriting `.iwiki.toml` scope.

The current project domain is derived from `basename(project_dir)` and validated with the existing domain validator. This matches the project convention already used by AGENTS.md: domain name equals project basename. If the inferred current domain is invalid or absent from the wiki base when requested as `read` or `write`, `wiki_bind` returns an error and does not write config.

## 2. Binding Semantics
Requirement R1: `wiki_bind(write=...)` must accept only the current project domain. A non-current write target returns an error before any `.iwiki.toml` write.

Requirement R2: When `.iwiki.toml` has no existing `read` list, `wiki_bind(read=...)` may set the requested validated read list. This preserves first-time binding behavior.

Requirement R3: When `.iwiki.toml` has an existing non-empty `read` list, `wiki_bind(read=...)` must never remove or replace existing entries.

Requirement R4: When an existing non-empty `read` list lacks the current project domain and the requested `read` includes the current project domain, `wiki_bind` appends only the current project domain and preserves all existing entries in order.

Requirement R5: When an existing non-empty `read` list already contains the current project domain, `wiki_bind(read=...)` keeps the existing list unchanged, even if the request contains only the current project domain.

Requirement R6: When an existing non-empty `read` list receives a request to add any non-current domain, `wiki_bind` rejects the request with no config write.

Requirement R7: Domain existence validation remains mandatory for every requested `read` entry and `write` target before a config write.

## 3. Components
- `base.current_project_domain(project_dir) -> str`: derives the current project domain from the project directory basename. It performs no filesystem writes and is covered by unit tests.
- `base.merge_read_scope(existing, requested, current_domain)`: returns the preserved or append-only read list, or a failure reason that `server.wiki_bind` maps to an MCP error response.
- `server.wiki_bind`: resolves binding, infers the current domain, validates requested domains, applies the read merge policy, enforces `write == current_domain`, then calls `base.write_project_config`.
- `templates/AGENTS.md.snippet` and `templates/CLAUDE.md.snippet`: remove ordinary-startup bootstrap wording and tell agents not to mutate binding automatically.
- `README.md` and `docs/README.ru.md`: document protected `read` semantics, current-project-only `write`, and explicit manual bootstrapping.

## 4. Data Flow
1. MCP caller invokes `wiki_bind(read=?, write=?)`.
2. `server.wiki_bind` resolves the existing binding and derives `current_domain` from `bind.project_dir`.
3. Requested domains are validated syntactically and checked for existence in the wiki base.
4. Requested `read` is merged with the existing read list using append-only current-domain semantics.
5. Requested `write` is rejected unless it equals `current_domain`.
6. `base.write_project_config` is called only after all guards pass.
7. `ignore.ensure_iwikiignore` remains after successful config writes only.

## 5. Error Handling
- Non-current `write` returns an error such as `write domain must match current project domain`, with a hint to use the current project domain or adjust the MCP project directory.
- Protected `read` replacement returns an error such as `read scope is protected`, with a hint that only the current project domain may be appended automatically.
- Missing current project domain in the wiki base uses the existing missing-domain error path; domain creation remains explicit and is not performed by project-opening instructions.
- If current project domain inference fails validation, `wiki_bind` returns an error and performs no config write.

## 6. Documentation And Agent Instructions
Agent snippets should still recommend `wiki_status` and `wiki_search` before work, but must not instruct agents to create domains, bind projects, or index source areas during ordinary startup. Bootstrapping a new domain becomes an explicit manual setup step, not a project-open side effect.

README files should describe `wiki_bind` as a guarded operation:
- first-time binding may set `read` and `write`;
- existing `read` is protected from replacement;
- current project domain may be appended to `read`;
- `write` must be current project domain.

## 7. Testing
Add focused tests near existing binding tests.

Success criteria:
- Existing `read = ["foreign"]`, `wiki_bind(read=["foreign", "proj"])` appends `proj`.
- Existing `read = ["foreign"]`, `wiki_bind(read=["proj"])` preserves `foreign` and adds `proj`.
- Existing `read = ["foreign", "proj"]`, `wiki_bind(read=["proj"])` preserves `foreign`.
- Existing `read = ["foreign"]`, `wiki_bind(read=["other"])` rejects if `other` is not the current project domain.
- `wiki_bind(write="foreign")` rejects.
- `wiki_bind(write="proj")` succeeds.
- Existing tests for missing domains, unknown TOML preservation, `.iwikiignore` creation, and full pytest suite pass.

## 8. Out Of Scope
- Automatically creating the current project domain.
- Editing `.iwiki.toml` files in other projects.
- Changing search scope resolution outside the protected bind behavior.
- Changing base repository git sync behavior.
