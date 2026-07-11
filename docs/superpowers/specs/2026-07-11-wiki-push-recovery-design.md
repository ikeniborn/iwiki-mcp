---
review:
  spec_hash: 1dee6c900ad56834
  last_run: 2026-07-11
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-07-11-wiki-push-recovery-intent.md
---
# Wiki Push Recovery Design

**Date:** 2026-07-11
**Status:** approved

## 1. Scope

This change investigates the credential-context difference between an interactive
shell and the stdio MCP server process, then improves push recovery and failure
visibility for `wiki_write_page` and `wiki_update_page`. It does not make iwiki-mcp a
credential manager and does not automatically resolve content conflicts.

The implementation is limited to the existing git synchronization boundary in
`src/iwiki_mcp/sync.py`, result assembly in `src/iwiki_mcp/server.py`, focused tests,
and documentation of confirmed behavior.

## 2. Existing Behavior

`wiki_write_page` and `wiki_update_page` mutate and index the wiki base, then call
`sync.commit_and_push`. `commit_and_push` already converts either `sync.warning` or
`sync.error` into a fail-soft `warning` while retaining a successful local commit.
Both write tools currently copy only `committed` and `pushed` from that result, so a
push failure can be returned as `pushed: false` without its cause.

`sync.sync` currently allows three loop iterations but retries only a non-fast-forward
push. Authentication and other transport failures return after the first failed push.
Git subprocesses inherit the server process environment and do not explicitly disable
interactive credential prompts.

## 3. Architecture

### 3.1 Ownership boundaries

- `sync._run` remains the only low-level Git subprocess boundary. It executes an
  argument vector without a shell and supplies a non-interactive environment.
- `sync.sync` owns pull/rebase, push attempts, failure classification, retry timing,
  rebase abort, and safe synchronization metadata.
- `sync.commit_and_push` owns the one-time local commit and propagation of safe sync
  metadata. Retry never repeats the page mutation, index update, or local commit.
- `server.py` owns deterministic assembly of mutating tool results.
- Credential investigation is a sanitized verification procedure. Runtime code may
  inspect whether a mechanism is configured or usable, but must not read, return, or
  log credential values.

### 3.2 Retry flow

One mutating tool call performs one local commit followed by at most three
synchronization attempts under the existing base lock. Each attempt runs
`pull --rebase`, then `push` if the pull succeeds. Recoverable credential or transport
failure on either remote command consumes one attempt and may retry. A fixed 250 ms
delay occurs between recoverable failures; no delay follows the final attempt, so
retry delay adds at most 500 ms to one call.

The failure taxonomy is closed:

| Class | Behavior |
|---|---|
| `success` | Return immediately with `pushed: true`. |
| `non_fast_forward` | Retry pull/rebase and push while attempts remain. |
| `credential_unavailable` | Retry while attempts remain so an externally restored standard Git credential source can recover the same call. |
| `transport_unavailable` | Retry while attempts remain. |
| `rebase_conflict` | Abort the rebase, retain the local commit, and stop immediately. |
| `permanent` | Stop immediately. |
| `unknown` | Stop immediately; unknown failures are not assumed safe to retry. |

Classification uses a focused set of Git failure signatures covered by tests. It does
not expose the remote URL or credential material. Three is a maximum, not a promise to
repeat permanent failures.

### 3.3 Parallel-device behavior

For non-conflicting parallel writes, `pull --rebase` moves the local commit over the
new remote commit and push continues automatically. For a true content conflict,
`sync` runs `rebase --abort`, preserves the original local commit, returns conflict
metadata, and performs no further retry. A human resolves the base repository and then
runs `wiki_sync`. iwiki-mcp never selects conflicting page content automatically.

### 3.4 Result contract

Existing result fields retain their current meanings. Safe additive metadata is:

- `sync_attempts`: number of attempted pull/rebase synchronization cycles;
- `push_attempts`: number of attempted `git push` commands;
- `failure_class`: terminal failure category when push did not succeed;
- `conflict: true`: present only for a rebase conflict;
- `hint`: actionable conflict recovery guidance when applicable.

`commit_and_push` continues to expose a terminal sync `error` or `warning` as
`warning`. `wiki_write_page` and `wiki_update_page` propagate that warning and safe
metadata. Warning priority is commit/push, then freshness, then frontmatter, ensuring
that `pushed: false` always has its push cause when one exists. Existing fields are not
removed or renamed.

## 4. Credential Investigation and Recovery Boundary

The investigation compares the interactive shell with an MCP-like sanitized process
using only safe facts: Git transport type, whether a credential helper is configured,
whether an SSH agent variable is present and usable, and the sanitized Git failure
class. It records no environment values, socket paths, remote URLs, tokens, keys, or
credential-helper output.

Every Git child receives `GIT_TERMINAL_PROMPT=0` and no interactive stdin. Retry relies
on Git's already configured credential helper, OS keychain, or agent becoming usable;
it does not create credentials.

If evidence identifies one unambiguous, system-provided credential source that can be
used without changing MCP client configuration, a narrow adapter may be designed only
when it passes the security constraints below. The current design does not authorize
socket scanning, shell-profile loading, a background process, a credential broker, or
persistent credential state. If standard Git mechanisms cannot become available under
these constraints, the investigation records the root cause and safe operational
options and stops before adding an unsafe workaround.

## 5. Security and Stability Invariants

1. Git commands remain argument-vector subprocess calls; no shell is introduced.
2. Retry does not change the configured remote, branch, refspec, or Git verification
   settings.
3. `GIT_TERMINAL_PROMPT=0` prevents blocking credential prompts.
4. Credential values, socket paths, full process environments, and remote URLs are not
   returned or logged.
5. Runtime code does not scan arbitrary sockets, read shell profiles, or start a
   credential broker.
6. Existing base locking encloses synchronization; one write produces at most one
   local commit.
7. Rebase conflict always triggers abort and immediate stop.
8. Unknown failures are not retried.
9. Retry is bounded to three synchronization attempts, no more than three push
   commands, and two fixed delays.
10. Existing fail-soft local-write behavior and existing result fields remain stable.

## 6. Requirements

### R1. Non-interactive Git

All Git subprocesses used by synchronization must execute without a shell and with
interactive terminal credential prompting disabled.

**Acceptance:** tests observe argument-vector execution, `GIT_TERMINAL_PROMPT=0`, and
no interactive stdin path.

### R2. Bounded recovery

`sync.sync` must perform no more than three synchronization attempts and no more than
three push commands. It retries only `non_fast_forward`, `credential_unavailable`, or
`transport_unavailable` failures from pull or push.

**Acceptance:** parametrized tests prove first-, second-, and third-attempt success,
three-attempt exhaustion, and immediate stop for permanent or unknown failures.

### R3. Conflict preservation

A rebase conflict must be aborted immediately. The pre-existing local commit must
remain available and no automatic content selection may occur.

**Acceptance:** an integration test creates conflicting parallel edits and proves
remote state is unchanged, the local commit remains, the rebase state is absent, and
the result contains conflict guidance.

### R4. Warning propagation

`wiki_write_page` and `wiki_update_page` must propagate the terminal
`commit_and_push.warning` whenever push fails.

**Acceptance:** focused tests return `pushed: false` with the exact sanitized push
warning for both tools while retaining `committed: true`.

### R5. Safe synchronization metadata

The result must expose `sync_attempts` and `push_attempts`; failed synchronization must
expose a safe `failure_class`; rebase conflict must additionally expose
`conflict: true` and a recovery `hint`.

**Acceptance:** result-shape tests cover success, exhausted retry, permanent failure,
and conflict without exposing test credential values or remote URLs.

### R6. Single mutation and commit

Retry must not repeat page writes, index updates, log updates, or local commit creation.

**Acceptance:** tests count one mutation and one commit across a three-attempt push
scenario.

### R7. Credential-context evidence

The development result must include a reproducible, sanitized comparison of shell and
MCP-like credential contexts and state whether the reported environment difference is
confirmed, disproved, or blocked by unavailable evidence.

**Acceptance:** recorded evidence identifies transport and mechanism availability by
boolean/category only, states the root cause when confirmed, and lists safe solution
options without credential material.

### R8. Documentation consistency

README documentation and the `iwiki-mcp` wiki pages for git synchronization and MCP
write results must describe only confirmed behavior. `wiki_lint` must introduce no new
broken references, stale pages, or orphans.

**Acceptance:** repository documentation matches the implemented result contract and
retry/conflict behavior; bound-domain lint has no new blocking findings.

## 7. Error Handling

- A failed local commit skips synchronization and retains its existing fail-soft
  warning behavior with `sync_attempts: 0` and `push_attempts: 0`.
- Pull failure without rebase state is classified and returned; it is not silently
  treated as a push success.
- Pull failure with rebase state triggers `rebase --abort`; abort failure is included
  in sanitized terminal guidance without discarding the original conflict class.
- Lock timeout retains fail-soft behavior and performs no unbounded retry.
- Subprocess timeout is terminal for that call unless its tested failure class is
  explicitly recoverable within the remaining attempt budget.
- An empty Git stderr falls back to the existing safe `git command failed` message.

## 8. Verification Strategy

Focused unit tests cover classification, attempt counting, warning propagation,
metadata, prompt disabling, secret redaction, and one-commit behavior. Repository-level
integration tests use temporary local bare remotes for non-conflicting and conflicting
parallel-device scenarios. Credential recovery is simulated deterministically by a
Git subprocess test double or isolated helper that becomes available on a later
attempt; no real credential enters the test suite.

Required regression checks are:

```bash
uv run pytest -q
uv run flake8 src tests
uv run iwiki-mcp --help
```

Manual evidence uses a sanitized MCP-like environment and records categories/booleans
only. Any security or stability regression blocks completion.

## 9. Acceptance (from intent)

- Reproduce the shell/server credential difference, identify its root cause, and
  select a verifiable fix that requires no MCP client configuration change.
- After credentials become available, the same mutating tool call automatically
  recovers and pushes within at most three attempts.
- If all push attempts fail, `wiki_write_page` and `wiki_update_page` return an
  accurate push warning with `pushed: false` while preserving the successful local
  page write and commit.

Health and completion constraints carried from the approved intent:

- Page writes and local commits remain fail-soft when the remote push is unavailable.
- The MCP server never opens an interactive credential prompt.
- Existing inter-process locking for git operations remains effective.
- Existing tool result fields retain their current meaning; the warning is additive.
- Retry work is bounded to three attempts per mutating tool call.
- Done when the server-process credential failure has a reproduced and evidenced root
  cause; an allowed fix recovers push within three attempts after credentials become
  available; and exhausted attempts return an exact warning from both write tools while
  retaining the local commit.

## 10. Out of Scope

- Storing, copying, provisioning, or rotating credentials.
- MCP client configuration changes.
- Shell-profile evaluation or login-shell Git execution.
- Arbitrary SSH-agent socket discovery.
- Background credential processes, brokers, or persistent state.
- Automatic resolution of conflicting wiki content.
- Changes to unrelated mutating tools unless later evidence proves they share a
  required result-contract defect and the spec is re-approved.
