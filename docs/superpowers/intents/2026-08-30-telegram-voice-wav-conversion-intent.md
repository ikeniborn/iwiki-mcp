---
review:
  intent_hash: 103b5ff0a075352b
  last_run: 2026-08-30
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings: []
workflow:
  route: chain
  continuation: execute
---
# Intent: telegram-voice-wav-conversion

**Date:** 2026-08-30
**Status:** approved

## Objective

Enable the existing Telegram bot voice workflow against the already available Framework GPU Tools transcription endpoint by converting each downloaded Telegram OGG/Opus voice message to WAV inside the bot service before upload. Framework endpoint availability, model activation, authentication, and API behavior remain outside this task.

## Desired Outcomes

- An authorized user sends a Telegram voice message and receives the resulting wiki-backed answer as a Telegram text message through the existing question flow.

## Health Metrics

- Existing Telegram text-question behavior and its focused tests remain unchanged and passing.
- Temporary OGG/Opus and WAV files are absent after every completed or failed voice-message handling attempt.
- Bot logs and persistent storage contain no voice bytes, converted audio, transcript text, or generated answer text.
- A conversion or transcription failure for one voice message does not stop Telegram polling or prevent the next update from being handled.

## Strategic Context

- Interacts with: authorized Telegram users, Telegram voice-file download, the bot voice handler, an in-container audio converter, the existing Framework GPU Tools `/v1/audio/transcriptions` endpoint, the existing iwiki retrieval and answer flow, the runtime container, and deployment operators.
- Priority trade-off: reliability and privacy over speed and cost.

## Constraints

### Steering (behavioral guidance)

- Keep conversion at the bot client boundary and preserve the existing voice-to-question flow after transcription.
- Prefer the smallest conversion path that produces deterministic Framework-compatible WAV output.
- Return a sanitized user-facing failure for conversion, size, or transcription errors while keeping polling available for later updates.

### Hard (architectural enforcement)

- Perform conversion inside the existing bot container; do not add an external conversion service.
- Use the system `ffmpeg` executable as the permitted converter.
- Accept Telegram OGG/Opus input and submit only WAV output with a valid RIFF/WAVE signature.
- Check the converted WAV size before upload and never send a file larger than 50 MiB to Framework GPU Tools.
- Keep source and converted audio temporary and delete both after success, conversion failure, size rejection, transcription failure, or cancellation.
- Do not log or persist voice bytes, converted audio, transcript text, or generated answer text.
- Do not change the Framework API, transcription model, authentication, endpoint availability, or deployment topology.

## Autonomy Zones

- Full autonomy (reversible, low risk): change bot code, focused tests, and documentation needed for local OGG/Opus-to-WAV conversion.
- Guarded (log + confidence threshold): add `ffmpeg` to the runtime image and change sanitized voice-error handling while preserving current privacy and polling behavior.
- Proposal-first (needs approval): change the Framework API, authentication, deployment topology, model route, or privacy contract.
- No autonomy (human only): perform production rollout, handle production credentials, or activate the Framework transcription backend model.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions
> is marked HUMAN CHECKPOINT in the plan.

## Stop Rules

- Halt if: the solution requires a Framework API, authentication, model-route, deployment-topology, or privacy-contract change.
- Escalate if: the bot container cannot include `ffmpeg`, valid WAV output cannot be guaranteed, the 50 MiB pre-upload boundary cannot be enforced, or temporary audio cannot be deleted on every exit path.
- Done when: a representative Telegram OGG/Opus fixture is converted to a RIFF/WAVE-signature-valid file, the Framework transcription request receives that WAV within the 50 MiB limit, the bot returns the resulting wiki-backed text answer, both temporary audio forms are removed on success and failure, the next update survives a voice error, and existing text-question behavior remains passing.
