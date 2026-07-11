# Wiki Push Credential Context Evidence

## Scope and safety boundary

This evidence checks whether a credential-context difference can explain wiki push failures. Probes emitted categories and booleans only. They did not print or store environment values, authentication socket paths, remote URLs, usernames, tokens, credential-helper names, or helper output.

The connected MCP process environment is not an introspection contract. A running MCP-like process was the best available comparison target, but process matching cannot prove that it is the exact process serving the current connection.

## Sanitized probes

| Probe | Current shell | Best available MCP-like context |
| --- | --- | --- |
| Remote transport category | HTTP(S) | Same repository configuration |
| Credential helper configured | Yes | Configuration-level fact only |
| `SSH_AUTH_SOCK` present | Yes | No |
| `SSH_AUTH_SOCK` usable | Yes | No |

Commands were bounded to these output forms:

- Parse the configured remote in memory and emit only `ssh`, `http(s)`, `local/file`, `other`, or `none`.
- Test whether any credential helper is configured while discarding helper names and output; emit one boolean.
- Test `SSH_AUTH_SOCK` presence and Unix-socket connectivity without emitting its value; emit two booleans per context.
- Search process metadata only to select an MCP-like process; emit no PID, command line, environment value, or local path.

## Verdict

**Blocked.** The evidence disproves an SSH-agent-context mismatch as the explanation for this repository's HTTP(S) transport. It does not confirm or disprove whether the exact connected MCP process can obtain HTTP(S) credentials: a configured helper is not proof of successful non-interactive credential retrieval, and safely collecting helper output or a live push failure could disclose protected data or change remote state.

## Implementation boundary

`wiki_sync` retries the standard Git source. It does not alter Git client configuration, source shell profiles, scan for authentication sockets, or broker credentials. Git runs with terminal prompts disabled and stdin closed. Recoverable remote failures receive at most three sync attempts with 250 ms between attempts; response metadata reports sync and push attempt counts plus the classified failure. Rebase conflicts stop immediately for manual resolution.

## Safe operational options

- Configure a standard non-interactive Git credential helper for the account and HTTP(S) transport used to launch the MCP server.
- Launch the MCP server from a trusted environment where the standard Git credential context is already available.
- Run `wiki_sync` from a trusted terminal that has the required credential context.
- Keep tokens, passwords, credential-bearing remote URLs, authentication socket paths, and helper output out of MCP configuration, evidence, and logs.

## Version decision

The repository version remains `0.6.4`. This documentation task is part of the change set that already selected that version; an additional bump in this docs-only subtask would conflict with the coordinated release change.
