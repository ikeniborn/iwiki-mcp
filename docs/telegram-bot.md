# Telegram bot service

The `iwiki-telegram-bot` process is a separately deployed long-polling client for a
hosted iwiki MCP server. An administrator-owned Telegram ID allowlist gates domain
listing, selected-domain questions, voice transcription, and preview-confirmed page
changes before any iwiki or inference request is made.

## Trust model

The remote iwiki service token is the maximum domain scope. Every allowed employee
sees the same domains granted to that token; the bot cannot grant rights, create
domains, or widen scope. Use a dedicated token with read access to answerable domains
and write access only where Telegram-originated changes are permitted.

The inference credential must authorize only inference. Do not deploy the bot with a
credential that also grants runtime control, audit-log access, metrics, or GPU tool
administration. The provider must not retain prompts, wiki context, answers,
transcriptions, or audio after processing. In particular, the current Framework
single edge token and 30-day raw request/response audit do not satisfy this boundary;
an inference-only credential or gateway plus bot-request audit exclusion is required
before production use.

Unknown Telegram IDs receive no domain metadata and trigger no iwiki or inference
call. Page creation and section updates always show a preview with Confirm and Reject
buttons. Confirmation tokens are random, bound to one Telegram ID, kept only in
memory, expire after the configured TTL, and are consumed before one mutation.
Section updates re-read the target and send its fresh revision and section hash; a
conflict is never overwritten or retried.

## Configuration

Supply configuration through the process environment or an owner-only service-manager
environment file. Never commit these values.

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

Install the package from the checkout, then keep the runtime and environment outside
the Git repository. One possible host layout is `/opt/iwiki-telegram-bot/app` for the
checkout and `/etc/iwiki/telegram-bot.env` for owner-only secrets.

```bash
cd /opt/iwiki-telegram-bot/app
uv sync
uv run iwiki-telegram-bot --help
uv run iwiki-telegram-bot
```

A minimal systemd unit can supervise long polling:

```ini
[Unit]
Description=iwiki Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=iwiki-bot
Group=iwiki-bot
WorkingDirectory=/opt/iwiki-telegram-bot/app
EnvironmentFile=/etc/iwiki/telegram-bot.env
ExecStart=/usr/bin/uv run iwiki-telegram-bot
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Run exactly one bot process per Telegram token: long-poll offsets are process-local.
The service keeps selected domains and pending previews only in memory. It has no
database and does not persist Telegram updates, messages, user identifiers, prompts,
answers, transcriptions, or voice files. Selected domains and pending previews expire
after the confirmation TTL; the polling loop removes expired state even when no new
message arrives. Operational logs must contain only operation
type, outcome, elapsed time, and aggregate usage; never log content or credentials.

## Failure behavior

Missing configuration stops startup. Remote iwiki, inference, Telegram download, and
malformed-response failures return sanitized messages. An unavailable domain is not
selected. Empty retrieval produces no model answer. An expired, replayed, or
wrong-user confirmation performs no mutation. A write conflict requires a new preview.
