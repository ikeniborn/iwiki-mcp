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

### MCP-like process probe

This probe selects the first readable `/proc` entry whose command line contains `iwiki-mcp` or `iwiki_mcp`. It emits only selection and socket-state booleans. It does not emit the PID, command line, environment value, or socket path. Selection is best-effort and cannot establish that the chosen process serves the current MCP connection.

```bash
uv run --quiet python - <<'PY'
import os
import socket
import stat
from pathlib import Path


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


selected_value = None
selected = False
for proc in Path("/proc").iterdir():
    if not proc.name.isdigit() or int(proc.name) == os.getpid():
        continue
    try:
        command = (proc / "cmdline").read_bytes().lower()
        if b"iwiki-mcp" not in command and b"iwiki_mcp" not in command:
            continue
        for item in (proc / "environ").read_bytes().split(b"\0"):
            if item.startswith(b"SSH_AUTH_SOCK="):
                selected_value = os.fsdecode(item.split(b"=", 1)[1])
                break
        selected = True
        break
    except (OSError, ValueError):
        continue
present, usable = socket_state(selected_value)
print(f"mcp_like_context_available={str(selected).lower()}")
print(f"mcp_like_ssh_auth_sock_present={str(present).lower()}")
print(f"mcp_like_ssh_auth_sock_usable={str(usable).lower()}")
PY
```

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

This documentation follow-up bumps the repository patch version to `0.6.5`, as required for every repository change.
