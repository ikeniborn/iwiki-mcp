---
review:
  spec_hash: 137d58b64db3a71e
  last_run: 2026-08-21
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-08-21-telegram-domain-bot-service-intent.md
---

# Design: telegram-domain-bot-service

## Purpose and Scope

Deliver a separately deployed Python Telegram bot service that lets an authorized employee select an iwiki domain, ask a text or voice question, and create or update wiki pages after explicit confirmation. The service talks to a remote hosted iwiki MCP server and uses an OpenAI-compatible inference module to turn retrieved domain content into answers and user requests into page drafts.

The first delivery uses Telegram long polling. It is one independently deployable process and does not add Telegram transport, OpenAI inference, or bot state to the `iwiki-mcp` hosted server.

Out of scope: Telegram-based access administration, per-user iwiki tokens, webhook ingress, persistent chat history, autonomous writes, domain creation, and changes to iwiki server authorization.

## Acceptance (from intent)

- An authorized employee can select an available domain, ask a question, and receive an answer based on retrieved content from that domain through the configured inference endpoint.
- An authorized employee can create or modify a page in the selected domain after explicitly confirming a preview of the change.
- An authorized employee can send a voice message and receive the corresponding result.
- Done when: in a real scenario, an authorized employee selects an available domain, receives an answer, confirms a page change, and receives an equivalent result from a voice request; an unauthorized user cannot access the bot.

## Architecture Decision

The service uses one least-privilege iwiki service token and an administrator-managed Telegram-ID allowlist. The service token defines the maximum remote read/write scope; the bot never expands it. Every bot request must pass the Telegram-ID allowlist before it reaches either the remote iwiki client or the inference client.

This baseline is preferred over one iwiki token per employee because it avoids storing user tokens and introduces no token-management surface in Telegram. All authorized employees therefore see the same domains exposed by the service token. Per-user domain entitlements are explicitly deferred to a future design because they need a durable policy store and a token lifecycle.

The bot is composed of five bounded modules:

- `TelegramTransport` receives updates, renders domain and confirmation choices, and carries no business authorization.
- `AccessPolicy` loads administrator-owned Telegram-ID allowlist configuration and denies unknown identities.
- `RemoteIwikiClient` maintains the authenticated Streamable HTTP MCP connection and exposes typed operations: list domains, search/read selected-domain content, create a page, and update one existing section with optimistic-concurrency values.
- `InferenceClient` is the only module that calls an OpenAI-compatible API. It uses the configured endpoint and model for text inference and the audio transcription API for voice input. No other module depends on a provider-specific request shape.
- `ConversationService` holds only short-lived in-memory selected-domain and pending-confirmation state, orchestrates the flows below, and deletes it after completion, rejection, or timeout.

## Configuration and Secrets

Deployment configuration supplies the Telegram bot credential, remote iwiki endpoint and service token, administrator-managed Telegram-ID allowlist, OpenAI-compatible base URL, inference model, audio-transcription model, and inference credential. These values are server-side secrets or deployment configuration; they are never bot commands, wiki pages, logs, or Git-tracked values.

The service starts only when all mandatory configuration is present. It must fail closed if the iwiki scope cannot be established or the inference client cannot authenticate. The initial deployment runs one process with long polling; an operator supplies process supervision and secret injection.

## Read and Answer Flow

1. `AccessPolicy` authenticates the Telegram sender against the allowlist.
2. `ConversationService` asks `RemoteIwikiClient` for visible domains; the employee selects one, which becomes short-lived in-memory conversation state.
3. For a question, `RemoteIwikiClient` searches only the selected domain and reads the returned content needed to answer.
4. `ConversationService` sends the question and only the selected-domain retrieval context to `InferenceClient`.
5. `InferenceClient` returns an answer; `TelegramTransport` returns it to the employee.

The service must not send whole-domain content, other domains, service credentials, or prior chat history to inference. If retrieval or inference cannot produce a grounded answer, it reports failure or asks for a narrower question; it does not fabricate an answer.

## Voice Flow

1. `TelegramTransport` downloads the voice attachment into process-local transient storage.
2. `InferenceClient` calls the configured OpenAI-compatible audio transcription API.
3. The service processes the transcription through the normal read-and-answer flow.
4. The temporary audio and transcription are deleted immediately after the response or failure.

Voice processing is not Realtime streaming in the first delivery. Audio transcription is a separate API operation from answer generation, which keeps its cost and latency measurable independently.

## Confirmed Write Flow

1. An authorized employee asks to create a page or to modify a selected page section in the selected domain.
2. `RemoteIwikiClient` obtains the current target content and, for an update, its revision and section hash.
3. `InferenceClient` drafts Markdown limited to the user request and selected-domain target context.
4. `TelegramTransport` shows a preview and a confirm/reject choice. Rejection destroys the draft.
5. Only confirmation invokes a remote mutation. Creation calls the iwiki create-page operation. Update first re-reads the target and uses the current revision and section hash for the remote compare-and-swap operation.
6. The service reports success, conflict, or validation failure. A conflict or ambiguous target stops the flow and requires a new user action; no automatic overwrite or retry is permitted.

## Data Handling and Observability

The service keeps selected-domain and pending-write state only in process memory until a response, rejection, or bounded timeout. It stores no Telegram messages, voice files, transcriptions, prompts, answers, or user identifiers after processing.

Content-free operational telemetry records operation type, outcome, elapsed time, and aggregated inference usage. It supports the intent health metrics for write authorization, domain isolation, answer quality review, text/voice latency, and transcription cost without retaining message or voice content.

## Error and Safety Rules

- Unknown Telegram IDs receive no domain metadata and cause no remote or inference request.
- A domain outside remote iwiki scope is never displayed or requested.
- Missing configuration, remote authorization failure, scope rejection, transcription failure, inference failure, unsafe Markdown, validation failure, or optimistic-concurrency conflict produces a sanitized user-visible error and ends the active operation.
- Page creation and modification always require a fresh explicit confirmation; a stale, replayed, or expired confirmation is rejected.
- Inference output is untrusted draft content. The service validates required target and page/section shape through remote iwiki operations rather than treating a model response as authority.

## Verification Strategy

Focused tests will use fake Telegram, remote iwiki, and OpenAI-compatible clients. They will prove allowlist denial before outbound calls; selected-domain-only retrieval context; answer and voice success paths; transient-data cleanup; preview-before-write; no write on rejection or expiry; compare-and-swap conflict handling; and sanitized failures for remote and inference errors.

An integration scenario against a disposable hosted iwiki instance and OpenAI-compatible test endpoint will prove the intent Done-when path: select a domain, answer a text question, confirm a page change, process equivalent voice input, and deny an unauthorized Telegram ID.
