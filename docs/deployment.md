# Single-container deployment

This is the supported production path for hosted iwiki MCP with Telegram access. One
hardened application container runs hosted MCP, nginx, and the Telegram bot. PostgreSQL
and the literal HTTPS CONNECT proxy are external, operator-managed services.

## Route and ownership boundaries

The container has three supervised children:

1. `iwiki-mcp serve --transport streamable-http` on `127.0.0.1:8765`.
2. nginx on the operator-selected LAN/Traefik listener, forwarding to loopback MCP.
3. `iwiki-telegram-bot`, using the external HTTPS proxy for Telegram only.

The bot establishes TLS to the proxy, sends `CONNECT api.telegram.org:443`, then
establishes Telegram TLS inside that tunnel. Long polling, replies, callbacks, file
metadata, and file bytes all use the proxy. There is no direct Telegram fallback.

Accepted proxy settings are exactly these shapes:

```text
IWIKI_BOT_TELEGRAM_PROXY_URL=https://proxy.example:8443
IWIKI_BOT_TELEGRAM_PROXY_URL=https://user:password@proxy.example:9443
```

The value must begin with literal lowercase `https://` and contain a valid host and
explicit valid port. `http://`, `socks*`, paths other than optional `/`, query strings,
fragments, missing or invalid hosts, and missing, non-numeric, or out-of-range ports are
rejected. Errors and logs never include the URL or credentials.

Inference, remote iwiki, and PostgreSQL remain direct. Compose defines no standard
proxy environment variables, inference and remote-iwiki HTTPX clients use
`trust_env=False`, and psycopg connects directly to PostgreSQL.

## Operator files and templates

Keep production runtime configuration outside the checkout:

```text
/opt/iwiki-mcp/server.toml       hosted MCP and external PostgreSQL endpoint
/opt/iwiki-mcp/nginx.conf        LAN/Traefik listener and loopback upstream
/opt/iwiki-mcp/runtime.env       owner-only runtime secrets and bot settings
```

For a new target directory, create every file with final ownership and mode before
editing. These commands initialize empty files; do not rerun them over populated files.
Back up existing files or install reviewed source copies with the same metadata.

```bash
(
set -euo pipefail
sudo install -d -o root -g root -m 0755 /opt/iwiki-mcp
sudo install -o root -g root -m 0644 /dev/null /opt/iwiki-mcp/server.toml
sudo install -o root -g root -m 0644 deploy/nginx.conf.example /opt/iwiki-mcp/nginx.conf
sudo install -o root -g root -m 0600 /dev/null /opt/iwiki-mcp/runtime.env
)
```

Use this complete `server.toml` template, replacing every angle-bracket placeholder:

```toml
[storage]
type = "postgres"
host = "<postgres-host>"
port = <postgres-port>
database = "<postgres-database>"
user = "<least-privilege-runtime-role>"
sslmode = "verify-full"

[server]
host = "127.0.0.1"
port = 8765
allowed_origins = ["https://<allowed-origin-host>"]
pool_min_size = 2
pool_max_size = 10
statement_timeout_ms = 30000
lock_timeout_ms = 5000
```

A same-host PostgreSQL container must publish a host port such as
`127.0.0.1:55432`; configure that host and port rather than a bridge-only service name.
A remote database supplies its DNS name and custom port and should retain
`sslmode = "verify-full"` with a trusted CA and matching hostname.

Use this complete `runtime.env` template. Replace every placeholder before Compose
reads it; never commit the populated file.

```dotenv
IWIKI_DB_PASSWORD=<postgres-runtime-password>
IWIKI_LLM_BASE_URL=https://<server-inference-host>/v1
IWIKI_LLM_KEY=<server-inference-key>
IWIKI_EMBED_MODEL=<exact-embedding-model-id>
IWIKI_EMBED_DIMENSIONS=<exact-embedding-dimensions>
IWIKI_RERANK_MODEL=<exact-rerank-model-id-or-empty>

IWIKI_BOT_TELEGRAM_TOKEN=<telegram-bot-token>
IWIKI_BOT_IWIKI_URL=http://127.0.0.1:8765/mcp
IWIKI_BOT_IWIKI_TOKEN=<least-privilege-iwiki-token>
IWIKI_BOT_ALLOWED_TELEGRAM_IDS=<comma-separated-telegram-ids>
IWIKI_BOT_LLM_BASE_URL=https://<bot-inference-host>/v1
IWIKI_BOT_LLM_KEY=<bot-inference-only-key>
IWIKI_BOT_LLM_MODEL=<chat-model-id>
IWIKI_BOT_TRANSCRIPTION_MODEL=<transcription-model-id>
IWIKI_BOT_CONFIRMATION_TTL_SECONDS=<positive-seconds>
IWIKI_BOT_CONTEXT_BUDGET_CHARS=<positive-characters>
IWIKI_BOT_CONTEXT_WINDOW_TOKENS=<positive-tokens>
IWIKI_BOT_SEARCH_K=<positive-count>
IWIKI_BOT_MAX_OUTPUT_TOKENS=<positive-tokens>
IWIKI_BOT_INFERENCE_TIMEOUT_SECONDS=<positive-seconds>
IWIKI_BOT_LOG_LEVEL=<log-level>
IWIKI_BOT_TELEGRAM_PROXY_URL=https://<proxy-host>:<proxy-port>

IWIKI_INGRESS_HEALTH_HOST=<lan-listener-host>
IWIKI_INGRESS_HEALTH_PORT=<lan-listener-port>
IWIKI_BOT_HEARTBEAT_MAX_AGE_SECONDS=<heartbeat-window-seconds>
```

Copy `deploy/nginx.conf.example` unchanged except for its host-specific `listen`
address. Keep loopback upstream `127.0.0.1:8765`, explicit `Authorization` forwarding,
disabled request/response buffering, `client_max_body_size 16m`, and `access_log off`.
At this stage the token placeholder is intentionally unresolved. Initial validation
requires exactly one literal `IWIKI_BOT_IWIKI_TOKEN=<least-privilege-iwiki-token>` line,
rejects a missing, duplicate, or malformed token line, and rejects every other angle-
bracket placeholder. Final validation runs after secure token installation.

```bash
(
set -euo pipefail
sudoedit /opt/iwiki-mcp/server.toml
sudoedit /opt/iwiki-mcp/nginx.conf
sudoedit /opt/iwiki-mcp/runtime.env
sudo awk 'index($0, "<") || index($0, ">") {invalid=1} END{exit invalid}' /opt/iwiki-mcp/server.toml
sudo awk '
BEGIN {
    expected = "IWIKI_BOT_IWIKI_TOKEN=<least-privilege-iwiki-token>"
}
/IWIKI_BOT_IWIKI_TOKEN/ {
    occurrences++
    if ($0 == expected) {
        exact++
    } else {
        invalid=1
    }
}
index($0, "<") || index($0, ">") {
    if ($0 != expected) {
        invalid=1
    }
}
END {
    exit !(occurrences == 1 && exact == 1 && invalid == 0)
}
' /opt/iwiki-mcp/runtime.env
)
```

Expected metadata is `root:root 0755` for `/opt/iwiki-mcp`, `root:root 0644` for
`server.toml` and `nginx.conf`, and `root:root 0600` for `runtime.env`. Container UID
`10001` reads the two non-secret configuration files through read-only bind mounts.
Compose reads the owner-only environment file on the host; it is not mounted.

## Out-of-band schema migration and principal provisioning

Application Compose and runtime create no PostgreSQL service, database, or schema
objects and run no migrations. Runtime calls `require_schema_version`, validates its
least-privilege principal, and refuses startup on a mismatch.

The runtime pins one exact schema version, currently 8. An image is therefore not
deployable against a database the operator has not migrated to that version, and an older
image is not deployable against a newer database. Version 8 releases the foreign key that
let derived code-graph links block deletion of the Markdown page they were derived from:
before it, a page that carried a `code` selector at publication time could never be
deleted again. Stepping back is `rollback_v8_compatibility`, which restores version 7 and
with it that behaviour; it exists to reach the version an older runtime pins, not to
repair anything.

Create a separate admin configuration by copying `server.toml`, then replace only
`storage.user` with the administration-only schema-owner/migrator role. Give a dedicated
operator group read access as `root:<admin-group> 0640`; the operator running the CLI
must already belong to that group. The file contains no password, is never mounted into
the application container, and must never name the runtime role.

```bash
(
set -euo pipefail
ADMIN_GROUP='replace-with-admin-operator-group'
sudo install -o root -g "$ADMIN_GROUP" -m 0640 /opt/iwiki-mcp/server.toml /opt/iwiki-mcp/admin-server.toml
sudoedit /opt/iwiki-mcp/admin-server.toml
)
```

`base list` has a bounded read-only requested action, but the CLI intentionally
initializes or advances the schema before that read; there is no generic `migrate`
subcommand. Dry-run import/export and the schema compatibility command are separate
non-migrating paths. The password is read without echo, never enters argv/history, and
disappears when the subshell exits. Model identity must exactly match the intended
database-wide metadata.

```bash
(
set -euo pipefail
read -r -s -p 'PostgreSQL schema-owner password: ' IWIKI_DB_PASSWORD
printf '\n'
export IWIKI_DB_PASSWORD
export IWIKI_EMBED_MODEL='replace-with-exact-embedding-model-id'
export IWIKI_EMBED_DIMENSIONS='replace-with-exact-embedding-dimensions'
export IWIKI_RERANK_MODEL='replace-with-exact-rerank-model-id-or-empty'
iwiki-mcp base list --config /opt/iwiki-mcp/admin-server.toml --json
)
```

With the same secret-safe boundary, create the base and domains only after the migration
trigger succeeds. See [PostgreSQL provisioning and least
privilege](../README.md#postgresql-provisioning-and-least-privilege) for background; do
not follow its later token step until this runbook has registered and inspected the exact
runtime principal.

```bash
(
set -euo pipefail
read -r -s -p 'PostgreSQL schema-owner password: ' IWIKI_DB_PASSWORD
printf '\n'
export IWIKI_DB_PASSWORD
export IWIKI_EMBED_MODEL='replace-with-exact-embedding-model-id'
export IWIKI_EMBED_DIMENSIONS='replace-with-exact-embedding-dimensions'
export IWIKI_RERANK_MODEL='replace-with-exact-rerank-model-id-or-empty'
iwiki-mcp base create --config /opt/iwiki-mcp/admin-server.toml --iwiki replace-with-iwiki-id
iwiki-mcp domain create --config /opt/iwiki-mcp/admin-server.toml --iwiki replace-with-iwiki-id --domain replace-with-domain
)
```

Next, ensure the PostgreSQL runtime login named `replace-with-runtime-role` exists. This
is an out-of-band database/platform operation: iwiki does not create the login or accept
its password. Keep its password and runtime configuration separate from the schema-owner
configuration. The schema-owner configuration and credential are never mounted into the
application container.

Only after that login, base, and domains exist, register and inspect the exact runtime
role, then issue the least-privilege runtime token. Choose a unique, non-secret owner
label tied to this deployment attempt. `token create` prints plaintext by contract.
Disable shell tracing, do not use a terminal-recorded session, and capture stdout into a
non-exported variable. Immediately before issuance, the block rechecks `root:root 0600`
and the exact single placeholder, then queries the actual token list and requires zero
active records with that exact owner. Here active means `revoked_at` is JSON `null`;
inactive records are ignored, but the label must still be freshly generated and retained
in the change record for recovery. The block then sends the token through stdin to a
root Python process that rechecks the file, writes and fsyncs a same-directory temporary
file, atomically renames it, and fsyncs the directory. The token never enters argv,
history, or terminal output. Final validation requires no placeholders and exactly one
non-empty token line. Create a bootstrap token separately only when an actual bootstrap
operation requires one, and feed it directly to an approved secret manager through
stdin; do not persist it in a general file.

```bash
(
set -euo pipefail
set +x
RUNTIME_ENV=/opt/iwiki-mcp/runtime.env
TOKEN_OWNER="deploy-$(date -u +%Y%m%dT%H%M%SZ)-$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
case "$TOKEN_OWNER" in
    ''|replace-with-*|*[!A-Za-z0-9._-]*) exit 2 ;;
esac
printf 'token_owner=%s status=candidate\n' "$TOKEN_OWNER"
read -r -s -p 'PostgreSQL schema-owner password: ' IWIKI_DB_PASSWORD
printf '\n'
export IWIKI_DB_PASSWORD
export IWIKI_EMBED_MODEL='replace-with-exact-embedding-model-id'
export IWIKI_EMBED_DIMENSIONS='replace-with-exact-embedding-dimensions'
export IWIKI_RERANK_MODEL='replace-with-exact-rerank-model-id-or-empty'
iwiki-mcp principal grant --config /opt/iwiki-mcp/admin-server.toml --principal replace-with-runtime-role --iwiki replace-with-iwiki-id --read-domain replace-with-domain --write-domain replace-with-domain --runtime hosted --json
iwiki-mcp principal inspect --config /opt/iwiki-mcp/admin-server.toml --principal replace-with-runtime-role --json
unset RUNTIME_TOKEN 2>/dev/null || true
TOKEN_ISSUANCE_STARTED=0
cleanup_token() {
    status=$1
    trap - EXIT INT TERM HUP
    unset RUNTIME_TOKEN
    if [ "$TOKEN_ISSUANCE_STARTED" -eq 1 ] && [ "$status" -ne 0 ]; then
        printf 'WARNING token_install_failed owner=%s action=run_exact_owner_recovery_block_before_retry\n' "$TOKEN_OWNER" >&2
    fi
    exit "$status"
}
trap 'cleanup_token $?' EXIT
trap 'cleanup_token 130' INT
trap 'cleanup_token 143' TERM HUP
RUNTIME_METADATA="$(sudo stat --format='%U:%G %a' "$RUNTIME_ENV")"
test "$RUNTIME_METADATA" = 'root:root 600'
sudo awk '
BEGIN {
    expected = "IWIKI_BOT_IWIKI_TOKEN=<least-privilege-iwiki-token>"
}
/IWIKI_BOT_IWIKI_TOKEN/ {
    occurrences++
    if ($0 == expected) {
        exact++
    } else {
        invalid=1
    }
}
index($0, "<") || index($0, ">") {
    if ($0 != expected) {
        invalid=1
    }
}
END {
    exit !(occurrences == 1 && exact == 1 && invalid == 0)
}
' "$RUNTIME_ENV"
ACTIVE_OWNER_MATCHES="$(
    iwiki-mcp token list --config /opt/iwiki-mcp/admin-server.toml --iwiki replace-with-iwiki-id --json |
    TOKEN_OWNER="$TOKEN_OWNER" python3 -c '
import json
import os
import sys

records = json.load(sys.stdin)
if not isinstance(records, list):
    raise SystemExit("invalid token list")
owner = os.environ["TOKEN_OWNER"]
matches = [record for record in records if isinstance(record, dict) and record.get("owner") == owner and record.get("revoked_at") is None]
print(len(matches))
'
)"
case "$ACTIVE_OWNER_MATCHES" in
    ''|*[!0-9]*) exit 1 ;;
esac
if [ "$ACTIVE_OWNER_MATCHES" -ne 0 ]; then
    printf 'ERROR token_owner_not_unique owner=%s active_matches=%s action=choose_fresh_owner\n' "$TOKEN_OWNER" "$ACTIVE_OWNER_MATCHES" >&2
    exit 1
fi
TOKEN_ISSUANCE_STARTED=1
RUNTIME_TOKEN="$(iwiki-mcp token create --config /opt/iwiki-mcp/admin-server.toml --iwiki replace-with-iwiki-id --owner "$TOKEN_OWNER" --hosted-principal replace-with-runtime-role --read-domain replace-with-domain --write-domain replace-with-domain)"
test -n "$RUNTIME_TOKEN"
case "$RUNTIME_TOKEN" in
    *$'\n'*) exit 1 ;;
esac
printf '%s\n' "$RUNTIME_TOKEN" | sudo python3 -c '
import os
from pathlib import Path
import stat
import sys
import tempfile

target = Path("/opt/iwiki-mcp/runtime.env")
placeholder = b"IWIKI_BOT_IWIKI_TOKEN=<least-privilege-iwiki-token>"
payload = sys.stdin.buffer.read()
if not payload.endswith(b"\n") or payload.count(b"\n") != 1:
    raise SystemExit("invalid token input")
token = payload[:-1]
if not token or b"\r" in token:
    raise SystemExit("invalid token input")
metadata = target.lstat()
if (
    not stat.S_ISREG(metadata.st_mode)
    or metadata.st_uid != 0
    or metadata.st_gid != 0
    or stat.S_IMODE(metadata.st_mode) != 0o600
):
    raise SystemExit("runtime.env metadata mismatch")
lines = target.read_bytes().splitlines(keepends=True)
matches = [index for index, line in enumerate(lines) if line.rstrip(b"\n") == placeholder]
if len(matches) != 1:
    raise SystemExit("runtime.env token placeholder mismatch")
index = matches[0]
ending = b"\n" if lines[index].endswith(b"\n") else b""
lines[index] = b"IWIKI_BOT_IWIKI_TOKEN=" + token + ending
descriptor, temporary_name = tempfile.mkstemp(prefix=".runtime.env.", dir=target.parent)
temporary = Path(temporary_name)
try:
    os.fchmod(descriptor, 0o600)
    os.fchown(descriptor, 0, 0)
    with os.fdopen(descriptor, "wb") as output:
        descriptor = -1
        output.write(b"".join(lines))
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, target)
    directory_descriptor = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
finally:
    if descriptor >= 0:
        os.close(descriptor)
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
'
RUNTIME_METADATA="$(sudo stat --format='%U:%G %a' "$RUNTIME_ENV")"
test "$RUNTIME_METADATA" = 'root:root 600'
sudo awk 'index($0, "<") || index($0, ">") {invalid=1} END{exit invalid}' /opt/iwiki-mcp/server.toml "$RUNTIME_ENV"
sudo awk '
/IWIKI_BOT_IWIKI_TOKEN/ {
    occurrences++
    if ($0 ~ /^IWIKI_BOT_IWIKI_TOKEN=.+$/) {
        exact++
    } else {
        invalid=1
    }
}
END {
    exit !(occurrences == 1 && exact == 1 && invalid == 0)
}
' "$RUNTIME_ENV"
TOKEN_ISSUANCE_STARTED=0
unset RUNTIME_TOKEN
trap - EXIT INT TERM HUP
)
```

If the stable `token_install_failed` warning appears, do not retry issuance. The token
value remains suppressed, but a token may exist. Use the retained owner label with the
recovery block below. It queries the actual token list, ignores inactive records, and
requires exactly one active exact-owner match. Zero or multiple matches stop with a safe
diagnostic. The matching non-secret ID, owner, and active status are printed before that
exact ID is revoked; the operator never chooses an arbitrary ID. `token list` never
returns plaintext token material.

Only a successful revoke reaches the placeholder restore helper. The helper requires
successful revoke state and never reads a token into a shell variable. On its first
attempt it accepts exactly one installed non-empty token line, atomically replaces it
with the literal placeholder, and fsyncs the file and directory. If rename became
visible but directory fsync failed, the same helper also accepts exactly one literal
placeholder and no token line; it reopens and fsyncs the file and directory before the
metadata and full placeholder checks. Every other state is rejected. A failure offers
one bounded manual retry through that same idempotent helper. Do not issue another token
until it returns success. Repair only an ownership, mode, or filesystem cause before
that retry, without displaying file content; an occurrence-state mismatch requires
stopping for the protected secret-recovery procedure.

```bash
(
set -euo pipefail
set +x
RUNTIME_ENV=/opt/iwiki-mcp/runtime.env
TOKEN_OWNER='replace-with-retained-token-owner-label'
case "$TOKEN_OWNER" in
    ''|replace-with-*|*[!A-Za-z0-9._-]*) exit 2 ;;
esac
read -r -s -p 'PostgreSQL schema-owner password: ' IWIKI_DB_PASSWORD
printf '\n'
export IWIKI_DB_PASSWORD
export IWIKI_EMBED_MODEL='replace-with-exact-embedding-model-id'
export IWIKI_EMBED_DIMENSIONS='replace-with-exact-embedding-dimensions'
export IWIKI_RERANK_MODEL='replace-with-exact-rerank-model-id-or-empty'
TOKEN_REVOKED=0
cleanup_recovery() {
    status=$1
    trap - EXIT INT TERM HUP
    if [ "$TOKEN_REVOKED" -eq 1 ] && [ "$status" -ne 0 ]; then
        printf 'WARNING token_revoked_placeholder_not_restored owner=%s action=repair_metadata_or_filesystem_then_retry_restore_before_issuance\n' "$TOKEN_OWNER" >&2
    fi
    exit "$status"
}
trap 'cleanup_recovery $?' EXIT
trap 'cleanup_recovery 130' INT
trap 'cleanup_recovery 143' TERM HUP
TOKEN_ID="$(
    iwiki-mcp token list --config /opt/iwiki-mcp/admin-server.toml --iwiki replace-with-iwiki-id --json |
    TOKEN_OWNER="$TOKEN_OWNER" python3 -c '
import json
import os
import sys

records = json.load(sys.stdin)
if not isinstance(records, list):
    raise SystemExit("invalid token list")
owner = os.environ["TOKEN_OWNER"]
matches = [record for record in records if isinstance(record, dict) and record.get("owner") == owner and record.get("revoked_at") is None]
if len(matches) != 1:
    print(f"ERROR token_recovery_cardinality owner={owner} active_matches={len(matches)} action=stop", file=sys.stderr)
    raise SystemExit(1)
token_id = matches[0].get("token_id")
if not isinstance(token_id, str) or not token_id:
    raise SystemExit("invalid token id")
print(token_id)
'
)"
test -n "$TOKEN_ID"
case "$TOKEN_ID" in
    *$'\n'*) exit 1 ;;
esac
printf 'token_recovery_candidate token_id=%s owner=%s status=active\n' "$TOKEN_ID" "$TOKEN_OWNER"
iwiki-mcp token revoke --config /opt/iwiki-mcp/admin-server.toml --token-id "$TOKEN_ID" >/dev/null
TOKEN_REVOKED=1
restore_runtime_placeholder() {
    test "$TOKEN_REVOKED" -eq 1 || return $?
    sudo python3 -c '
import os
from pathlib import Path
import stat
import tempfile

target = Path("/opt/iwiki-mcp/runtime.env")
key = b"IWIKI_BOT_IWIKI_TOKEN"
placeholder = key + b"=<least-privilege-iwiki-token>"
metadata = target.lstat()
if (
    not stat.S_ISREG(metadata.st_mode)
    or metadata.st_uid != 0
    or metadata.st_gid != 0
    or stat.S_IMODE(metadata.st_mode) != 0o600
):
    raise SystemExit("runtime.env metadata mismatch")
content = target.read_bytes()
lines = content.splitlines(keepends=True)
matches = [index for index, line in enumerate(lines) if key in line]
if len(matches) != 1:
    raise SystemExit("runtime.env token occurrence mismatch")
index = matches[0]
stripped = lines[index].rstrip(b"\n")
prefix = key + b"="
if stripped == placeholder:
    for line_number, line in enumerate(lines):
        if line_number != index and (b"<" in line or b">" in line):
            raise SystemExit("runtime.env placeholder state mismatch")
    file_descriptor = os.open(target, os.O_RDONLY)
    try:
        os.fsync(file_descriptor)
    finally:
        os.close(file_descriptor)
    directory_descriptor = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
elif stripped.startswith(prefix) and len(stripped) > len(prefix):
    if b"<" in content or b">" in content:
        raise SystemExit("runtime.env installed token state mismatch")
    ending = b"\n" if lines[index].endswith(b"\n") else b""
    lines[index] = placeholder + ending
    descriptor, temporary_name = tempfile.mkstemp(prefix=".runtime.env.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, 0, 0)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(b"".join(lines))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        directory_descriptor = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
else:
    raise SystemExit("runtime.env recovery state mismatch")
' || return $?
    sudo chown root:root "$RUNTIME_ENV" || return $?
    sudo chmod 0600 "$RUNTIME_ENV" || return $?
    sudo python3 -c '
import os
from pathlib import Path

target = Path("/opt/iwiki-mcp/runtime.env")
file_descriptor = os.open(target, os.O_RDONLY)
try:
    os.fsync(file_descriptor)
finally:
    os.close(file_descriptor)
directory_descriptor = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
try:
    os.fsync(directory_descriptor)
finally:
    os.close(directory_descriptor)
' || return $?
    RUNTIME_METADATA="$(sudo stat --format='%U:%G %a' "$RUNTIME_ENV")" || return $?
    test "$RUNTIME_METADATA" = 'root:root 600' || return $?
    sudo awk '
BEGIN {
    expected = "IWIKI_BOT_IWIKI_TOKEN=<least-privilege-iwiki-token>"
}
/IWIKI_BOT_IWIKI_TOKEN/ {
    occurrences++
    if ($0 == expected) {
        exact++
    } else {
        invalid=1
    }
}
index($0, "<") || index($0, ">") {
    if ($0 != expected) {
        invalid=1
    }
}
END {
    exit !(occurrences == 1 && exact == 1 && invalid == 0)
}
' "$RUNTIME_ENV" || return $?
    return 0
}
if ! restore_runtime_placeholder; then
    printf 'WARNING token_restore_failed owner=%s action=repair_metadata_or_filesystem_then_type_retry_once\n' "$TOKEN_OWNER" >&2
    read -r -p 'Type retry to run the same restore helper once more: ' RESTORE_ACTION
    test "$RESTORE_ACTION" = retry
    if ! restore_runtime_placeholder; then
        printf 'ERROR token_restore_retry_failed owner=%s action=stop_before_new_token\n' "$TOKEN_OWNER" >&2
        exit 1
    fi
fi
TOKEN_REVOKED=0
trap - EXIT INT TERM HUP
)
```

## Acceptance helper

Hosted MCP uses JSON responses, so it cannot produce a truthful multi-event streaming
probe. The helper below uses real MCP initialize semantics for Authorization and body
limits, and a separate two-event stdlib canary for nginx buffering. It never prints the
token or response content. Save it as `/tmp/iwiki-acceptance.py` and remove it after the
change.

```bash
(
set -euo pipefail
HELPER=/tmp/iwiki-acceptance.py
cleanup_helper() {
    status=$1
    trap - EXIT INT TERM HUP
    sudo rm -f "$HELPER"
    exit "$status"
}
trap 'cleanup_helper $?' EXIT
trap 'cleanup_helper 130' INT
trap 'cleanup_helper 143' TERM HUP
sudo install -o root -g root -m 0644 /dev/null /tmp/iwiki-acceptance.py
sudoedit /tmp/iwiki-acceptance.py
python3 -c 'from pathlib import Path; compile(Path("/tmp/iwiki-acceptance.py").read_text(), "/tmp/iwiki-acceptance.py", "exec")'
trap - EXIT INT TERM HUP
)
```

```python
import argparse
import getpass
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import time


MIB = 1024 * 1024


def initialize_body(total_bytes=None):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "operator-acceptance", "version": "1"},
        },
    }
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if total_bytes is None:
        return encoded
    padding = total_bytes - len(encoded)
    if padding < 0:
        raise RuntimeError("target body is too small")
    payload["params"]["clientInfo"]["name"] += "x" * padding
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(encoded) != total_bytes:
        raise RuntimeError("body size construction failed")
    return encoded


def request(method, host, port, body, host_header, origin, token=None, session=None):
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Host": host_header,
        "Origin": origin,
    }
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if session is not None:
        headers["Mcp-Session-Id"] = session
    connection = http.client.HTTPConnection(host, port, timeout=90)
    try:
        connection.request(method, "/mcp", body=body, headers=headers)
        response = connection.getresponse()
        status = response.status
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        response_body = response.read()
        return status, response_headers, response_body
    finally:
        connection.close()


def check_mcp(args):
    token = getpass.getpass("MCP bearer token: ")
    small = initialize_body()
    missing, _, _ = request(
        "POST", args.host, args.port, small, args.host_header, args.origin
    )
    invalid, _, _ = request(
        "POST", args.host, args.port, small, args.host_header, args.origin,
        "invalid-acceptance-token"
    )
    exact, headers, body = request(
        "POST", args.host, args.port, initialize_body(16 * MIB),
        args.host_header, args.origin, token
    )
    oversized, _, _ = request(
        "POST", args.host, args.port, initialize_body(16 * MIB + 1),
        args.host_header, args.origin, token
    )
    if (missing, invalid, exact, oversized) != (401, 401, 200, 413):
        raise RuntimeError(
            "unexpected status tuple: " + repr((missing, invalid, exact, oversized))
        )
    parsed = json.loads(body)
    if parsed.get("result", {}).get("serverInfo", {}).get("name") != "iwiki":
        raise RuntimeError("initialize response is not iwiki")
    if not headers.get("mcp-session-id"):
        raise RuntimeError("initialize response has no MCP session")
    print("mcp_acceptance_ok statuses=401,401,200,413 exact_bytes=16777216")


def wait_mcp(args):
    deadline = time.monotonic() + args.timeout
    body = initialize_body()
    while time.monotonic() < deadline:
        try:
            status, _, _ = request(
                "POST", args.host, args.port, body, args.host_header, args.origin
            )
            if status == 401:
                print("mcp_backend_ready status=401")
                return
        except OSError:
            pass
        time.sleep(1)
    raise RuntimeError("MCP backend did not become ready")


class StreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        pass

    def do_GET(self):
        if self.path != "/stream":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b"first\n")
        self.wfile.flush()
        time.sleep(4)
        self.wfile.write(b"second\n")
        self.wfile.flush()


def stream_server(args):
    HTTPServer((args.host, args.port), StreamHandler).handle_request()


def stream_client(args):
    deadline = time.monotonic() + 15
    while True:
        connection = http.client.HTTPConnection(args.host, args.port, timeout=10)
        started = time.monotonic()
        try:
            connection.request("GET", "/stream", headers={"Host": args.host_header})
            response = connection.getresponse()
            first = response.read(len(b"first\n"))
            first_at = time.monotonic()
            rest = response.read()
            completed_at = time.monotonic()
            break
        except OSError:
            connection.close()
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)
    connection.close()
    if response.status != 200 or first != b"first\n" or rest != b"second\n":
        raise RuntimeError("unexpected stream response")
    if first_at - started >= 3 or completed_at - first_at < 3:
        raise RuntimeError("first event was buffered until completion")
    print("nginx_streaming_ok first_event_before_completion=true")


parser = argparse.ArgumentParser()
commands = parser.add_subparsers(dest="command", required=True)
for name in ("mcp", "wait-mcp"):
    command = commands.add_parser(name)
    command.add_argument("--host", required=True)
    command.add_argument("--port", type=int, required=True)
    command.add_argument("--host-header", required=True)
    command.add_argument("--origin", required=True)
commands.choices["wait-mcp"].add_argument("--timeout", type=int, default=60)
server = commands.add_parser("stream-server")
server.add_argument("--host", default="127.0.0.1")
server.add_argument("--port", type=int, default=8765)
client = commands.add_parser("stream-client")
client.add_argument("--host", required=True)
client.add_argument("--port", type=int, required=True)
client.add_argument("--host-header", required=True)
arguments = parser.parse_args()
{
    "mcp": check_mcp,
    "wait-mcp": wait_mcp,
    "stream-server": stream_server,
    "stream-client": stream_client,
}[arguments.command](arguments)
```

## Isolated pre-cutover validation

The fixed production contract uses host networking and MCP port
`127.0.0.1:8765`. Changing only nginx's listener does not remove that collision with
the current MCP owner. A full combined-container precheck therefore runs on a separate
host or VM with its own network namespace, using the same architecture and image.

The isolated environment must use all of these non-production boundaries:

- a dedicated validation Telegram bot token and validation-only allowlist, never the
  production bot token;
- an isolated PostgreSQL database/schema already migrated to the exact version, never
  the production database;
- a least-privilege validation iwiki token and scope with no production write scope or
  production content;
- the same external HTTPS CONNECT proxy only when operator policy permits the
  validation credential;
- a non-production LAN listener, Host, and allowed Origin.

Do not log secrets, raw Telegram content, MCP responses, or proxy credentials. Confirm
Telegram CONNECT through the proxy operator's credential-free destination/count
telemetry; this project cannot define the external proxy's command.

On that isolated host, use one failure-safe subshell for render, build, validation, and
the stream canary. It refuses to reuse a pre-existing validation Compose project or
canary container. Cleanup traps are installed before any container or background server
is created; failure or interruption removes only resources created by this block. The
bounded health and stream-server waits exit nonzero on timeout. Resolved Compose config,
raw logs, and container environments are never printed.

```bash
(
set -euo pipefail
VALIDATION_INGRESS_HOST='replace-with-validation-listener-host'
VALIDATION_INGRESS_PORT='replace-with-validation-listener-port'
VALIDATION_HOST_HEADER='replace-with-validation-allowed-host'
VALIDATION_ORIGIN='https://replace-with-validation-origin'
VALIDATION_PROJECT=iwiki-mcp-validation
STREAM_CONTAINER=iwiki-nginx-stream-check
VALIDATION_OWNED=0
STREAM_CONTAINER_OWNED=0
STREAM_SERVER_PID=
cleanup_validation() {
    status=$1
    trap - EXIT INT TERM HUP
    set +e
    if [ "$STREAM_CONTAINER_OWNED" -eq 1 ]; then
        sudo docker stop "$STREAM_CONTAINER" >/dev/null 2>&1
        sudo docker rm "$STREAM_CONTAINER" >/dev/null 2>&1
    fi
    if [ -n "$STREAM_SERVER_PID" ]; then
        kill "$STREAM_SERVER_PID" >/dev/null 2>&1
        wait "$STREAM_SERVER_PID" >/dev/null 2>&1
    fi
    if [ "$VALIDATION_OWNED" -eq 1 ]; then
        sudo docker compose -p "$VALIDATION_PROJECT" down >/dev/null 2>&1
    fi
    exit "$status"
}
trap 'cleanup_validation $?' EXIT
trap 'cleanup_validation 130' INT
trap 'cleanup_validation 143' TERM HUP
EXISTING_VALIDATION="$(sudo docker compose -p "$VALIDATION_PROJECT" ps -aq)"
test -z "$EXISTING_VALIDATION"
VALIDATION_SERVICES="$(sudo docker compose -p "$VALIDATION_PROJECT" config --services)"
test "$VALIDATION_SERVICES" = iwiki
sudo docker compose -p "$VALIDATION_PROJECT" config --quiet
sudo docker compose -p "$VALIDATION_PROJECT" build iwiki
VALIDATION_OWNED=1
sudo docker compose -p "$VALIDATION_PROJECT" up -d
VALIDATION_CONTAINER="$(sudo docker compose -p "$VALIDATION_PROJECT" ps -q iwiki)"
test -n "$VALIDATION_CONTAINER"
VALIDATION_HEALTH=
for attempt in {1..60}; do
    VALIDATION_HEALTH="$(sudo docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$VALIDATION_CONTAINER")"
    if [ "$VALIDATION_HEALTH" = healthy ]; then
        break
    fi
    sleep 2
done
if [ "$VALIDATION_HEALTH" != healthy ]; then
    sudo docker inspect --format '{{range .State.Health.Log}}{{.ExitCode}} {{.Output}}{{println}}{{end}}' "$VALIDATION_CONTAINER"
    exit 1
fi
sudo docker inspect --format 'health={{.State.Health.Status}}' "$VALIDATION_CONTAINER"
sudo docker exec "$VALIDATION_CONTAINER" supervisorctl -c /etc/supervisor/supervisord.conf status
python3 /tmp/iwiki-acceptance.py mcp --host "$VALIDATION_INGRESS_HOST" --port "$VALIDATION_INGRESS_PORT" --host-header "$VALIDATION_HOST_HEADER" --origin "$VALIDATION_ORIGIN"
VALIDATION_IMAGE="$(sudo docker compose -p "$VALIDATION_PROJECT" images -q iwiki)"
test -n "$VALIDATION_IMAGE"
sudo docker compose -p "$VALIDATION_PROJECT" down
VALIDATION_OWNED=0
if sudo docker container inspect "$STREAM_CONTAINER" >/dev/null 2>&1; then
    exit 1
fi
PREEXISTING_STREAM_LISTENER="$(sudo ss -H -ltn '( sport = :8765 )')"
test -z "$PREEXISTING_STREAM_LISTENER"
python3 /tmp/iwiki-acceptance.py stream-server --host 127.0.0.1 --port 8765 &
STREAM_SERVER_PID=$!
STREAM_LISTENER=
for attempt in {1..50}; do
    STREAM_LISTENER="$(sudo ss -H -ltn '( sport = :8765 )')"
    if [ -n "$STREAM_LISTENER" ]; then
        break
    fi
    sleep 0.2
done
test -n "$STREAM_LISTENER"
STREAM_CONTAINER_OWNED=1
sudo docker run --detach --name "$STREAM_CONTAINER" --network host --read-only --user 10001:10001 --tmpfs /run:uid=10001,gid=10001,mode=0750 --tmpfs /tmp:uid=10001,gid=10001,mode=1770 --volume /opt/iwiki-mcp/nginx.conf:/etc/nginx/nginx.conf:ro --entrypoint /usr/sbin/nginx "$VALIDATION_IMAGE" -g 'daemon off;'
python3 /tmp/iwiki-acceptance.py stream-client --host "$VALIDATION_INGRESS_HOST" --port "$VALIDATION_INGRESS_PORT" --host-header "$VALIDATION_HOST_HEADER"
sudo docker stop "$STREAM_CONTAINER"
sudo docker rm "$STREAM_CONTAINER"
STREAM_CONTAINER_OWNED=0
for attempt in {1..50}; do
    if ! kill -0 "$STREAM_SERVER_PID" 2>/dev/null; then
        break
    fi
    sleep 0.2
done
if kill -0 "$STREAM_SERVER_PID" 2>/dev/null; then
    exit 1
fi
wait "$STREAM_SERVER_PID"
STREAM_SERVER_PID=
trap - EXIT INT TERM HUP
)
```

Validation succeeds only when health prints `healthy`, all three supervisor children
are `RUNNING`, MCP acceptance reports `401,401,200,413`, and the stream client reports
`first_event_before_completion=true`. Missing and invalid Authorization are rejected,
an exact 16 MiB initialize reaches MCP with 200, and the larger request receives 413.

If no isolated host or VM exists, a zero-downtime full-container precheck is impossible
with the current fixed host-network port. Schedule maintenance downtime and rely on
rollback; do not claim concurrent healthy validation.

## Production inventory and cutover

Before any production change, identify the process currently owning host port 8765 and
record its service manager plus exact stop/start commands in the change record. These
commands show identity and state without command arguments or environment values:

```bash
(
set -euo pipefail
LISTENER_OUTPUT="$(sudo ss -H -ltnp '( sport = :8765 )')"
test -n "$LISTENER_OUTPUT"
printf '%s\n' "$LISTENER_OUTPUT"
sudo docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
)
```

For a systemd-owned MCP process, set and verify the actual unit:

```bash
(
set -euo pipefail
OLD_MCP_UNIT='replace-with-systemd-unit'
sudo systemctl show "$OLD_MCP_UNIT" --property=Id --property=ActiveState --property=SubState --property=MainPID
)
```

For a Docker-owned MCP process, set and verify the actual container:

```bash
(
set -euo pipefail
OLD_MCP_CONTAINER='replace-with-container-name'
sudo docker inspect --format 'name={{.Name}} image={{.Config.Image}} pid={{.State.Pid}} status={{.State.Status}}' "$OLD_MCP_CONTAINER"
)
```

Render and build before maintenance. Never run unfiltered `compose config` because it
resolves the secret environment file.

```bash
(
set -euo pipefail
PRODUCTION_SERVICES="$(sudo docker compose -p iwiki-mcp config --services)"
test "$PRODUCTION_SERVICES" = iwiki
sudo docker compose -p iwiki-mcp config --quiet
sudo docker compose -p iwiki-mcp build iwiki
)
```

The service assertion must accept only `iwiki`. During cutover, use exactly one complete
alternative below. Each block stops the old nginx proxy and recorded MCP owner, captures
`ss` output without a pipeline, requires port 8765 to be free, and starts production.
Its cleanup trap is installed before `compose up`: a failed command, failed health check,
timeout, or interruption stops the partially started combined project before returning.
The trap is cleared only after healthy state and supervisor status succeed, so no target
switch can follow a failed block.

Systemd-owned old MCP:

```bash
(
set -euo pipefail
OLD_NGINX_CONTAINER='iwiki-mcp-proxy-1'
OLD_MCP_UNIT='replace-with-systemd-unit'
COMBINED_OWNED=0
cleanup_combined() {
    status=$1
    trap - EXIT INT TERM HUP
    set +e
    if [ "$COMBINED_OWNED" -eq 1 ]; then
        sudo docker compose -p iwiki-mcp down >/dev/null 2>&1
    fi
    exit "$status"
}
trap 'cleanup_combined $?' EXIT
trap 'cleanup_combined 130' INT
trap 'cleanup_combined 143' TERM HUP
EXISTING_COMBINED="$(sudo docker compose -p iwiki-mcp ps -aq)"
test -z "$EXISTING_COMBINED"
sudo docker stop "$OLD_NGINX_CONTAINER"
sudo systemctl stop "$OLD_MCP_UNIT"
LISTENER_OUTPUT="$(sudo ss -H -ltn '( sport = :8765 )')"
test -z "$LISTENER_OUTPUT"
COMBINED_OWNED=1
sudo docker compose -p iwiki-mcp up -d
COMBINED_CONTAINER="$(sudo docker compose -p iwiki-mcp ps -q iwiki)"
test -n "$COMBINED_CONTAINER"
COMBINED_HEALTH=
for attempt in {1..60}; do
    COMBINED_HEALTH="$(sudo docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$COMBINED_CONTAINER")"
    if [ "$COMBINED_HEALTH" = healthy ]; then
        break
    fi
    sleep 2
done
test "$COMBINED_HEALTH" = healthy
sudo docker inspect --format 'health={{.State.Health.Status}}' "$COMBINED_CONTAINER"
sudo docker exec "$COMBINED_CONTAINER" supervisorctl -c /etc/supervisor/supervisord.conf status
COMBINED_OWNED=0
trap - EXIT INT TERM HUP
)
```

Docker-owned old MCP:

```bash
(
set -euo pipefail
OLD_NGINX_CONTAINER='iwiki-mcp-proxy-1'
OLD_MCP_CONTAINER='replace-with-container-name'
COMBINED_OWNED=0
cleanup_combined() {
    status=$1
    trap - EXIT INT TERM HUP
    set +e
    if [ "$COMBINED_OWNED" -eq 1 ]; then
        sudo docker compose -p iwiki-mcp down >/dev/null 2>&1
    fi
    exit "$status"
}
trap 'cleanup_combined $?' EXIT
trap 'cleanup_combined 130' INT
trap 'cleanup_combined 143' TERM HUP
EXISTING_COMBINED="$(sudo docker compose -p iwiki-mcp ps -aq)"
test -z "$EXISTING_COMBINED"
sudo docker stop "$OLD_NGINX_CONTAINER"
sudo docker stop "$OLD_MCP_CONTAINER"
LISTENER_OUTPUT="$(sudo ss -H -ltn '( sport = :8765 )')"
test -z "$LISTENER_OUTPUT"
COMBINED_OWNED=1
sudo docker compose -p iwiki-mcp up -d
COMBINED_CONTAINER="$(sudo docker compose -p iwiki-mcp ps -q iwiki)"
test -n "$COMBINED_CONTAINER"
COMBINED_HEALTH=
for attempt in {1..60}; do
    COMBINED_HEALTH="$(sudo docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$COMBINED_CONTAINER")"
    if [ "$COMBINED_HEALTH" = healthy ]; then
        break
    fi
    sleep 2
done
test "$COMBINED_HEALTH" = healthy
sudo docker inspect --format 'health={{.State.Health.Status}}' "$COMBINED_CONTAINER"
sudo docker exec "$COMBINED_CONTAINER" supervisorctl -c /etc/supervisor/supervisord.conf status
COMBINED_OWNED=0
trap - EXIT INT TERM HUP
)
```

Only after health is `healthy` and all three children are `RUNNING`, switch Traefik/LAN
to the production nginx listener. Then run MCP acceptance with production listener,
Host, and Origin values; `getpass` keeps the token out of argv and history.

```bash
(
set -euo pipefail
PRODUCTION_INGRESS_HOST='replace-with-production-listener-host'
PRODUCTION_INGRESS_PORT='replace-with-production-listener-port'
PRODUCTION_HOST_HEADER='replace-with-production-allowed-host'
PRODUCTION_ORIGIN='https://replace-with-production-origin'
python3 /tmp/iwiki-acceptance.py mcp --host "$PRODUCTION_INGRESS_HOST" --port "$PRODUCTION_INGRESS_PORT" --host-header "$PRODUCTION_HOST_HEADER" --origin "$PRODUCTION_ORIGIN"
)
```

Acceptance requires `401,401,200,413`, fresh health, three RUNNING children, and
credential-free proxy telemetry showing Telegram CONNECT destination/count activity.
Streaming was separately proven against the same image and nginx directives on the
isolated host.

## Rollback before old-component removal

The stopped old MCP owner and `iwiki-mcp-proxy-1` remain restartable through the agreed
acceptance/rollback window. On any failed cutover or acceptance check:

1. Stop the combined container.
2. Restart the recorded old MCP owner with its exact service manager.
3. Wait for old loopback MCP to return the expected unauthenticated 401.
4. Restart the old nginx proxy.
5. Restore the previous Traefik/LAN target and run MCP acceptance through it.

Systemd-owned old MCP:

```bash
(
set -euo pipefail
OLD_NGINX_CONTAINER='iwiki-mcp-proxy-1'
OLD_MCP_UNIT='replace-with-systemd-unit'
PRODUCTION_HOST_HEADER='replace-with-production-allowed-host'
PRODUCTION_ORIGIN='https://replace-with-production-origin'
sudo docker compose -p iwiki-mcp down
sudo systemctl start "$OLD_MCP_UNIT"
python3 /tmp/iwiki-acceptance.py wait-mcp --host 127.0.0.1 --port 8765 --host-header "$PRODUCTION_HOST_HEADER" --origin "$PRODUCTION_ORIGIN" --timeout 60
sudo docker start "$OLD_NGINX_CONTAINER"
)
```

Docker-owned old MCP:

```bash
(
set -euo pipefail
OLD_NGINX_CONTAINER='iwiki-mcp-proxy-1'
OLD_MCP_CONTAINER='replace-with-container-name'
PRODUCTION_HOST_HEADER='replace-with-production-allowed-host'
PRODUCTION_ORIGIN='https://replace-with-production-origin'
sudo docker compose -p iwiki-mcp down
sudo docker start "$OLD_MCP_CONTAINER"
python3 /tmp/iwiki-acceptance.py wait-mcp --host 127.0.0.1 --port 8765 --host-header "$PRODUCTION_HOST_HEADER" --origin "$PRODUCTION_ORIGIN" --timeout 60
sudo docker start "$OLD_NGINX_CONTAINER"
)
```

Only after the selected block succeeds, restore the old Traefik/LAN target with its
recorded operator command. Then run the target verification below; a failed assertion
or request exits nonzero and stops rollback verification.

```bash
(
set -euo pipefail
PRODUCTION_INGRESS_HOST='replace-with-restored-listener-host'
PRODUCTION_INGRESS_PORT='replace-with-restored-listener-port'
PRODUCTION_HOST_HEADER='replace-with-production-allowed-host'
PRODUCTION_ORIGIN='https://replace-with-production-origin'
python3 /tmp/iwiki-acceptance.py mcp --host "$PRODUCTION_INGRESS_HOST" --port "$PRODUCTION_INGRESS_PORT" --host-header "$PRODUCTION_HOST_HEADER" --origin "$PRODUCTION_ORIGIN"
)
```

Rollback succeeds only when backend readiness reports 401, old ingress returns
`401,401,200,413`, and normal authorized MCP traffic is restored. Rollback never changes
PostgreSQL.

## Removal after the rollback window

Remove old components only after production acceptance and the agreed rollback window.
Before removing Docker components, print only name, image, and Compose identity; never
use raw `docker inspect`, which includes environment values.

```bash
(
set -euo pipefail
OLD_NGINX_CONTAINER='iwiki-mcp-proxy-1'
sudo docker inspect --format 'name={{.Name}} image={{.Config.Image}} project={{index .Config.Labels "com.docker.compose.project"}} service={{index .Config.Labels "com.docker.compose.service"}}' "$OLD_NGINX_CONTAINER"
read -r -p 'Type the exact old nginx container name to remove: ' REMOVE_NAME
test "$REMOVE_NAME" = "$OLD_NGINX_CONTAINER"
sudo docker rm "$OLD_NGINX_CONTAINER"
)
```

For a Docker-owned old MCP, inspect and remove its recorded container separately:

```bash
(
set -euo pipefail
OLD_MCP_CONTAINER='replace-with-container-name'
sudo docker inspect --format 'name={{.Name}} image={{.Config.Image}} project={{index .Config.Labels "com.docker.compose.project"}} service={{index .Config.Labels "com.docker.compose.service"}}' "$OLD_MCP_CONTAINER"
read -r -p 'Type the exact old MCP container name to remove: ' REMOVE_NAME
test "$REMOVE_NAME" = "$OLD_MCP_CONTAINER"
sudo docker rm "$OLD_MCP_CONTAINER"
)
```

For systemd, retain or remove the recorded unit through the operator's service-management
change process; this runbook does not invent a unit name. Remove the temporary helper
and retain the privileged admin config only under the operator's protected credential
policy.

```bash
(
set -euo pipefail
HELPER=/tmp/iwiki-acceptance.py
cleanup_helper() {
    sudo rm -f "$HELPER"
}
trap cleanup_helper EXIT
trap "exit 130" INT
trap "exit 143" TERM HUP
sudo rm -f "$HELPER"
trap - EXIT INT TERM HUP
)
```

## Health, recovery, and privacy

Compose uses `restart: unless-stopped`, `stop_grace_period: 60s`, a read-only root
filesystem, and tmpfs mounts for `/run` and `/tmp`. Supervisor restarts every unexpected
exit of hosted MCP, nginx, or the Telegram bot, including exit status zero; an explicit
`supervisorctl stop` remains stopped. Each child receives `TERM` with a 55-second wait
before process-group kill, leaving five seconds inside the Compose graceful window.
Health verifies all three children, loopback MCP, nginx ingress, and a Telegram
heartbeat newer than the configured window; it makes no extra Telegram request.

If a running MCP session fails, the bot closes that session, reconnects and initializes
a replacement with bounded backoff, and resumes the same failed Telegram update without
repeating updates whose offsets were already committed. This does not restart the bot
or container and does not change direct iwiki routing, single-use confirmation, or
revision/section-hash compare-and-swap behavior.

A proxy outage makes the heartbeat stale and the container unhealthy while the bot
keeps retrying through the same proxy. It never reroutes Telegram directly. Telegram
updates, user identifiers, message content, prompts, answers, transcriptions, voice
files, selected domains, and pending confirmation previews live only in memory or tmpfs
and do not survive restart. Confirmation tokens remain user-bound, expiring, and
single-use; page updates retain revision and section-hash compare-and-swap.

Container logs expose stable operational fields and health codes only. They must not
contain secrets, proxy URLs, credentials, request/response bodies, Telegram content,
filenames, audio, or transcriptions. PostgreSQL remains the only durable application
state and is outside container lifecycle.
