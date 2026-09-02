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
memory, expire after the configured TTL, and are consumed before one mutation. A
deferred question is held under the same TTL; a selected domain is a preference rather
than a secret and carries no TTL at all.
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
| `IWIKI_BOT_CONTEXT_BUDGET_CHARS` | Optional positive hard ceiling on the assembled wiki context; default `48000`. |
| `IWIKI_BOT_CONTEXT_WINDOW_TOKENS` | Optional positive context window of the chat model; default `32768`. |
| `IWIKI_BOT_MAX_OUTPUT_TOKENS` | Optional positive `max_tokens` for chat completions; default `1024`. |
| `IWIKI_BOT_INFERENCE_TIMEOUT_SECONDS` | Optional positive read/write timeout for inference requests; default `180`. |
| `IWIKI_BOT_LOG_LEVEL` | Optional root log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`); default `INFO`. |
| `IWIKI_BOT_TELEGRAM_PROXY_URL` | Required literal HTTPS proxy URL with explicit host and port. |

The chat model's context window is the binding constraint. The bot retrieves the
section each search hit names rather than whole pages, and reads each section once even
when several search hits name it.

The budget it fills is derived rather than configured. From
`IWIKI_BOT_CONTEXT_WINDOW_TOKENS` the bot subtracts `IWIKI_BOT_MAX_OUTPUT_TOKENS`, a
fixed reserve for the chat template, and the question itself, then converts the remaining
tokens to characters through a tokens-per-character ratio. That ratio starts at the
dense-Markdown worst case, is calibrated from the `usage.prompt_tokens` every completion
reports, and is raised immediately whenever a provider refuses a prompt. The result is
clamped to `IWIKI_BOT_CONTEXT_BUDGET_CHARS`, which is now a hard ceiling rather than the
budget: lower it to restrict the bot further, never to make it safe.

Sections are appended in result order and assembly stops before the first one that would
exceed the derived budget; only when no section fits is the first one truncated to it. If
the provider still answers `context_length_exceeded`, the bot reassembles the sections it
already read at half the budget and sends exactly one more completion, without any
further wiki call. Only when that also overflows does the user get
`Question context is too large. Ask a narrower question.`

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
`choices[0].message.content`. For voice, the bot writes the downloaded Telegram
OGG/Opus bytes to tmpfs, invokes the image-provided `ffmpeg`, and produces mono 16 kHz
PCM WAV. The converter stops writing at the 50 MiB Framework boundary; the bot also
rejects output above that boundary or without a valid RIFF/WAVE signature. It then sends
`audio.wav` with media type
`audio/wav` to `POST /v1/audio/transcriptions` and expects a JSON `text` field.
Framework endpoint availability, model activation, and authentication remain external
deployment prerequisites rather than bot responsibilities.

## Bot commands

At startup the bot registers its command list with `setMyCommands` and points the chat
menu button at that list, so typing `/` offers the commands below and the menu button
opens the same list. A failed registration is logged and never stops polling.

- `/menu` and `/start` open an inline menu with Domains, Create page, Update section, and
  Help buttons.
- `/help` prints the same command reference as the Help button.
- `/domains` lists domains visible to the iwiki service token.
- A domain button selects the domain for later questions and changes. The selection is
  sticky: it lasts for the life of the bot process and is unrelated to the confirmation
  TTL.
- Any non-command text asks a question using only retrieved content from that domain.
  With no domain selected yet, the bot keeps the question, offers the domain buttons,
  and answers it as soon as a domain is chosen. A deferred question expires with the
  confirmation TTL. `/create` and `/update` are not deferred: they offer the same
  buttons and ask for the command again.
- A Telegram voice message is downloaded, transcribed, processed as a question, and
  removed after processing. Transcription happens before the domain check, so a voice
  message sent without a selected domain is deferred as text rather than discarded.

## Processing feedback

Every update that reaches iwiki or inference shows its progress, so a slow answer is
never a silent one.

- The incoming message gets the 👀 reaction, replaced by 👍 when the reply is delivered
  and 🤨 when it reports a dependency failure. A callback button carries no user
  message, so it gets no reaction.
- A `typing` chat action is sent immediately and refreshed every four seconds while the
  work runs; the same loop refreshes the liveness heartbeat, so a long transcription or
  completion cannot make the container look unhealthy.
- The first stage posts a status message — `⏳ Transcribing voice…`, `⏳ Searching
  wiki…`, `⏳ Generating answer…`, `⏳ Reading section…`, `⏳ Drafting Markdown…`, or
  `⏳ Saving page…` — and every later stage edits that same message. The final answer
  replaces it, so one update produces one message.
- All feedback is best-effort. A failed reaction, chat action, or status edit is logged
  and never fails the update; if the status message cannot be posted, the stages stay
  silent and the answer is sent as a new message. Static replies (`/menu`, `/start`,
  `/help`) skip the feedback entirely.
- `/create <slug>: <request>` drafts a new Markdown page and requests confirmation.
- `/update <slug>#<heading>: <request>` drafts one `##` section replacement and
  requests confirmation.

## Deployment

Use the one-service `compose.yaml` path documented in the
[deployment runbook](deployment.md). The required host inputs are
`/opt/iwiki-mcp/server.toml`, `/opt/iwiki-mcp/nginx.conf`, and owner-only
`/opt/iwiki-mcp/runtime.env`; do not run a separate bot unit.

Supervisor runs exactly one bot process per Telegram token together with hosted MCP and
nginx. The runtime image includes `ffmpeg` for local voice conversion. Compose uses
`restart: unless-stopped`, a 60-second graceful stop, a read-only root filesystem, and
tmpfs mounts for `/run` and `/tmp`. Supervisor restarts every unexpected child exit,
including exit status zero; an explicit `supervisorctl stop` remains stopped until an
explicit start. Each child receives `TERM` and has 55 seconds to stop before Supervisor
can force its process group, inside the Compose 60-second window. Health covers all
three children, loopback MCP, nginx ingress, and the Telegram polling heartbeat within
the configured liveness window.

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

An inference failure is recorded as one WARNING carrying the HTTP status, the request
path, the provider error code, whether it was retryable, and the elapsed time — never
the prompt, the wiki context, the transcript, a response body, or a credential. A
completed chat request also logs the assembled prompt size in characters, which is what
makes a context refusal diagnosable without logging the prompt.

Context refusals are recognized by code (`context_length_exceeded`,
`string_above_max_length`) and by provider wording, including the vLLM and llama.cpp
phrasing that never says "exceeded": `maximum context length`, `context window`,
`reduce the length`, `too many tokens`, `input is too long`, and `prompt is too long`.
Whatever HTTP status carried it, the refusal is a client error, is never retried, and
the user is told `Question context is too large. Ask a narrower question.`

A transient failure — a timeout, a network error, a connection the provider closed
between requests, HTTP 429, or a 5xx — is retried once after 0.5 seconds before the user
sees anything. This is what a voice question needs most: the large multipart upload
often leaves a keep-alive connection the provider has already closed, and the following
completion used to surface as an outright outage. If the retry also fails the user is
told `Inference service is busy or too slow. Send the question again.`, which is
distinct from the permanent `Inference service is unavailable.` The HTTP client uses a
10-second connect timeout and an `IWIKI_BOT_INFERENCE_TIMEOUT_SECONDS` read/write
timeout (default 180), so a slow completion over a large context is not mistaken for an
outage.

Startup verifies that both the chat model and the transcription model are present in
`GET /models` and names the missing role in the log.

Missing configuration stops startup. Remote iwiki, inference, Telegram download, and
audio conversion, oversize WAV, malformed-response failures never expose dependency
details. Source OGG/Opus, converted WAV, and their temporary directory are removed on
success, failure, and cancellation. A failed voice update returns a sanitized message
without stopping polling. After a retryable MCP session failure, the running bot closes
only that session, reconnects and initializes a new one with bounded backoff, then
replays only the failed Telegram update; already completed updates keep their committed
offsets. Conversation selection and pending confirmation state remain in memory during
this reconnect. An unavailable domain is not selected. Empty retrieval produces no
model answer. An expired, replayed, or wrong-user confirmation performs no mutation. A
write conflict requires a new preview, and single-use confirmation consumption still
prevents ambiguous writes from replaying.
