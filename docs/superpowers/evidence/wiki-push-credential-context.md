# Wiki Push Credential Context Evidence

## Scope and safety boundary

This evidence checks whether a credential-context difference can explain wiki push failures. Probes emitted categories and booleans only. They did not print or store environment values, authentication socket paths, remote URLs, usernames, tokens, credential-helper names, or helper output.

The connected MCP process environment has no safe public introspection interface. This procedure deliberately does not inspect another process's environment or process metadata. Exact MCP credential-context comparison is therefore blocked by design.

## Sanitized probes

| Probe | Current shell / repository result |
| --- | --- |
| Remote transport category | HTTP(S) |
| Credential helper configured | Yes |
| `SSH_AUTH_SOCK` present | Yes |
| `SSH_AUTH_SOCK` usable | Yes |
| Exact connected MCP credential context | Not probed; blocked |

Commands were bounded to these output forms:

- Parse the configured remote in memory and emit only `ssh`, `http(s)`, `local/file`, `other`, or `none`.
- Test whether any credential helper is configured while discarding helper names and output; emit one boolean.
- Test the current shell's `SSH_AUTH_SOCK` presence and Unix-socket connectivity without emitting its value; emit two booleans.

### Repository and current-shell probe

Run from the repository root. The remote URL and helper output are captured inside the process and never printed.

```bash
uv run --quiet python - <<'PY'
import os
import socket
import stat
import subprocess


def socket_state(value):
    present = bool(value)
    usable = False
    if present:
        try:
            if stat.S_ISSOCK(os.stat(value).st_mode):
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.settimeout(0.2)
                try:
                    client.connect(value)
                    usable = True
                finally:
                    client.close()
        except OSError:
            pass
    return present, usable


remote = subprocess.run(
    ["git", "remote", "get-url", "origin"],
    capture_output=True,
    text=True,
).stdout.strip()
if remote.startswith(("https://", "http://")):
    transport = "https"
elif remote.startswith(("ssh://", "git+ssh://")) or (
    ":" in remote and not remote.startswith(("/", "./", "../", "file://"))
):
    transport = "ssh"
else:
    transport = "other"
helper_configured = subprocess.run(
    ["git", "config", "--get-all", "credential.helper"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
).returncode == 0
present, usable = socket_state(os.environ.get("SSH_AUTH_SOCK"))
print(f"transport_category={transport}")
print(f"credential_helper_configured={str(helper_configured).lower()}")
print(f"current_shell_ssh_auth_sock_present={str(present).lower()}")
print(f"current_shell_ssh_auth_sock_usable={str(usable).lower()}")
PY
```

## Verdict

**Blocked.** The reproducible facts establish HTTP(S) transport and current-shell credential configuration only. SSH-agent state does not establish HTTP(S) credential availability. The evidence cannot confirm or disprove whether the exact connected MCP process can obtain HTTP(S) credentials: no safe public interface exposes that context, a configured helper is not proof of successful non-interactive credential retrieval, and inspecting a target process environment is outside the safety boundary.

## Implementation boundary

`wiki_sync` retries the standard Git source. It does not alter Git client configuration, source shell profiles, scan for authentication sockets, or broker credentials. Git runs with terminal prompts disabled and stdin closed. Recoverable remote failures receive at most three sync attempts with 250 ms between attempts; response metadata reports sync and push attempt counts plus the classified failure. Rebase conflicts stop immediately for manual resolution.

## Safe operational options

- Configure a standard non-interactive Git credential helper for the account and HTTP(S) transport used to launch the MCP server.
- Launch the MCP server from a trusted environment where the standard Git credential context is already available.
- Run `wiki_sync` from a trusted terminal that has the required credential context.
- Keep tokens, passwords, credential-bearing remote URLs, authentication socket paths, and helper output out of MCP configuration, evidence, and logs.

## Version decision

The initial evidence follow-up bumped the repository from `0.6.5` to `0.6.6`.
Subsequent verification fixes advanced it through `0.6.7`; the final metadata
consistency fix aligns the project, lockfile, and runtime package version at `0.6.8`.
