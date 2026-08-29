# Telegram bot service

The `iwiki-telegram-bot` process is the Telegram child in the supported application
container alongside hosted iwiki MCP and nginx. An administrator-owned Telegram ID
allowlist gates domain listing, selected-domain questions, voice transcription, and
preview-confirmed page changes before any iwiki or inference request is made.

## Trust model

The remote iwiki service token is the maximum domain scope. Every allowed employee
sees the same domains granted to that token; the bot cannot grant rights, create
domains, or widen scope. Use a dedicated token with read access to answerable domains
and write access only where Telegram-originated changes are permitted.

The inference credential must authorize only inference. Do not deploy the bot with a
credential that also grants runtime control, audit-log access, metrics, or GPU tool
administration. Provider-side inference audit retention is an operator policy and is
not a bot acceptance gate. The bot process itself still must not persist prompts,
wiki context, answers, transcriptions, or audio after processing. The current
Framework edge token remains unsuitable because it grants non-inference capabilities;
production requires an inference-only credential or gateway.

Unknown Telegram IDs receive no domain metadata and trigger no iwiki or inference
call. Page creation and section updates always show a preview with Confirm and Reject
buttons. Confirmation tokens are random, bound to one Telegram ID, kept only in
memory, expire after the configured TTL, and are consumed before one mutation.
Section updates re-read the target and send its fresh revision and section hash; a
conflict is never overwritten or retried.

## Configuration

Supply configuration through the owner-only `/opt/iwiki-mcp/runtime.env` file read by
`compose.yaml`. Never commit these values.

| Variable | Purpose |
| --- | --- |
| `IWIKI_BOT_TELEGRAM_TOKEN` | Bot token issued by BotFather. |
| `IWIKI_BOT_ALLOWED_TELEGRAM_IDS` | Comma-separated numeric employee IDs. |
| `IWIKI_BOT_IWIKI_URL` | Hosted Streamable HTTP MCP URL, including `/mcp`. |
| `IWIKI_BOT_IWIKI_TOKEN` | Dedicated least-privilege iwiki service token. |
| `IWIKI_BOT_LLM_BASE_URL` | OpenAI-compatible base URL ending in `/v1`. |
| `IWIKI_BOT_LLM_KEY` | Inference-only bearer credential. |
| `IWIKI_BOT_LLM_MODEL` | Public model alias for chat completions. |
| `IWIKI_BOT_TRANSCRIPTION_MODEL` | Model accepted by `/audio/transcriptions`. |
| `IWIKI_BOT_CONFIRMATION_TTL_SECONDS` | Optional positive TTL; default `300`. |
| `IWIKI_BOT_TELEGRAM_PROXY_URL` | Required literal HTTPS proxy URL with explicit host and port. |

## Telegram HTTPS proxy boundary

Accepted values have one of these exact shapes:

```text
IWIKI_BOT_TELEGRAM_PROXY_URL=https://proxy.example:8443
IWIKI_BOT_TELEGRAM_PROXY_URL=https://user:password@proxy.example:9443
```

The bot establishes TLS to the operator-managed proxy, sends
`CONNECT api.telegram.org:443`, then establishes Telegram TLS inside the tunnel. Every
Telegram Bot API and file request uses that proxy. There is no direct Telegram
fallback: a proxy outage makes Telegram liveness unhealthy while the bot keeps retrying
the same route.

The literal lowercase `https://` prefix, valid host, and explicit valid port are
required. The parser rejects `http://`, any `socks*` scheme, paths other than optional
`/`, query strings, fragments, missing or invalid hosts, and missing, non-numeric, or
out-of-range ports. Validation errors and logs never include proxy credentials or the
supplied URL.

Inference, remote iwiki, and PostgreSQL remain direct. The Compose service defines no
standard proxy environment variables; the inference and remote-iwiki HTTPX clients use
`trust_env=False`, while psycopg connects to PostgreSQL directly.

The inference API must implement `POST /v1/chat/completions` and return
`choices[0].message.content`. Voice uses `POST /v1/audio/transcriptions` with an OGG
multipart upload and expects a JSON `text` field. Framework currently publishes chat
completions but not audio transcriptions, so text workflows can be configured there
only after the credential/audit boundary above is fixed; live voice remains
unavailable until the transcription endpoint and model route exist.

## Bot commands

- `/domains` lists domains visible to the iwiki service token.
- A domain button selects the domain for later questions and changes.
- Any non-command text asks a question using only retrieved content from that domain.
- A Telegram voice message is downloaded, transcribed, processed as a question, and
  removed after processing.
- `/create <slug>: <request>` drafts a new Markdown page and requests confirmation.
- `/update <slug>#<heading>: <request>` drafts one `##` section replacement and
  requests confirmation.

## Deployment

Use the one-service `compose.yaml` path documented in the
[deployment runbook](deployment.md). The required host inputs are
`/opt/iwiki-mcp/server.toml`, `/opt/iwiki-mcp/nginx.conf`, and owner-only
`/opt/iwiki-mcp/runtime.env`; do not run a separate bot unit.

Supervisor runs exactly one bot process per Telegram token together with hosted MCP and
nginx. Compose uses `restart: unless-stopped`, a 60-second graceful stop, a read-only
root filesystem, and tmpfs mounts for `/run` and `/tmp`. Supervisor restarts every
unexpected child exit, including exit status zero; an explicit `supervisorctl stop`
remains stopped until an explicit start. Each child receives `TERM` and has 55 seconds
to stop before Supervisor can force its process group, inside the Compose 60-second
window. Health covers all three children, loopback MCP, nginx ingress, and the Telegram
polling heartbeat within the configured liveness window.

The application runtime creates no PostgreSQL database or schema objects and runs no
migrations. It requires the exact compatible schema prepared out of band by the
operator's separate administration/migrator role; that privileged credential is never
the runtime login.

Full-container pre-cutover validation runs only on a separate isolated host or VM with
a dedicated validation bot token, database, iwiki scope, listener, and Origin. The
fixed host-network MCP port prevents a concurrent full-container precheck on the
production host; without isolated infrastructure, use a maintenance window and the
[documented rollback](deployment.md#rollback-before-old-component-removal).

Selected domains, Telegram updates, user identifiers, message content, prompts,
answers, voice files, and confirmation previews stay in memory or tmpfs and do not
survive restart. Operational logs contain stable fields such as operation, outcome,
elapsed time, and numeric usage only; they exclude message content, audio,
transcriptions, response bodies, tokens, proxy URLs, and credentials. PostgreSQL is the
external durable service. Existing user-bound, single-use confirmation consumption and
revision/section-hash compare-and-swap remain unchanged.

## Failure behavior

Missing configuration stops startup. Remote iwiki, inference, Telegram download, and
malformed-response failures never expose dependency details. After a retryable MCP
session failure, the running bot closes only that session, reconnects and initializes a
new one with bounded backoff, then replays only the failed Telegram update; already
completed updates keep their committed offsets. Conversation selection and pending
confirmation state remain in memory during this reconnect. An unavailable domain is
not selected. Empty retrieval produces no model answer. An expired, replayed, or
wrong-user confirmation performs no mutation. A write conflict requires a new preview,
and single-use confirmation consumption still prevents ambiguous writes from replaying.
