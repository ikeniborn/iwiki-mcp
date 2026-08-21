---
review:
  intent_hash: db91c7602e458f9d
  last_run: 2026-08-21
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings: []
workflow:
  route: chain
  continuation: full
---

# Intent: telegram-domain-bot-service

**Date:** 2026-08-21
**Status:** approved

## Objective

Provide authorized employees with access to the iwiki knowledge base through Telegram so they can work with selected wiki domains without using a separate MCP client, using an OpenAI-compatible inference module to formulate answers from retrieved wiki content.

## Desired Outcomes

- An authorized employee can select an available domain, ask a question, and receive an answer based on retrieved content from that domain through the configured inference endpoint.
- An authorized employee can create or modify a page in the selected domain after explicitly confirming a preview of the change.
- An authorized employee can send a voice message and receive the corresponding result.

## Health Metrics

- Write authorization correctness: no request from an unauthorized Telegram ID reaches an iwiki write operation.
- Domain isolation: no request reads or writes a domain outside the server-side iwiki scope.
- Answer quality: answers remain grounded in the user-selected domain.
- Response latency: end-to-end latency remains observable separately for text and voice requests.
- Voice-recognition cost: transcription usage and cost remain observable.

## Strategic Context

- Interacts with: authorized employees, an access administrator, Telegram, the remote iwiki server, an OpenAI-compatible inference endpoint, and a voice-recognition service.
- Priority trade-off: trust.

## Constraints

### Steering (behavioral guidance)

- Show a preview and require explicit user confirmation before saving a page creation or modification.

### Hard (architectural enforcement)

- Allow only employees whose Telegram IDs are authorized by an administrator.
- Do not bypass or expand the server-side iwiki scope.
- Do not retain Telegram or voice-message data longer than processing.
- Isolate inference behind a module that uses an OpenAI-compatible API; endpoint and model selection are deployment configuration, not bot commands.
- Keep inference credentials in server-side configuration and do not expose or log them.
- Send inference only the user request and wiki content retrieved from the selected domain within the authorized scope.

## Autonomy Zones

- Full autonomy (reversible, low risk): list authorized domains, select a domain, search and read, and answer text requests.
- Guarded (log + confidence threshold): transcribe voice and formulate an answer; request a text clarification when the result is not sufficiently actionable.
- Proposal-first (needs approval): create or modify a page only after showing a preview and receiving explicit confirmation.
- No autonomy (human only): grant access, change Telegram-ID mappings, expand scope, change credentials, or change data-retention rules.

## Stop Rules

- Halt if: the Telegram ID is unauthorized, the selected domain is outside server-side scope, voice recognition does not produce an actionable request, or the inference endpoint cannot provide a grounded answer.
- Escalate if: a page-write target is ambiguous, a page-write conflict occurs, or a request needs new access rights.
- Done when: in a real scenario, an authorized employee selects an available domain, receives an answer, confirms a page change, and receives an equivalent result from a voice request; an unauthorized user cannot access the bot.
