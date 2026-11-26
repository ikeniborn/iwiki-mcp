---
review:
  spec_hash: 06844a43afe62622
  last_run: 2026-11-26
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-11-26-okf-stale-after-lint-intent.md
---
# OKF page authorship, verification and review deadlines

## Scope

Implement the [approved intent](../intents/2026-11-26-okf-stale-after-lint-intent.md) for Git and PostgreSQL. Use the [pinned OKF v0.3 specification](https://github.com/open-knowledge-format/open-knowledge-format/blob/527e81aff55992be21507e97004286789126f88c/SPEC.md), sections 5 and 7. Keep the stored Markdown authoritative. No corpus rewrite or schema migration is needed.

## Actor format

Use one string of the form `<kind>:<id>` for the author and verifier arguments and for new live `generated.by` and `verified[].by` values. Separate the kind and identifier at the first colon. Preserve the identifier spelling.

| Actor | Example written to the page | Use |
|-------|-----------------------------|-----|
| Person | `human:alice` | Explicit human authorship or review. |
| Agent | `agent:release-editor/2.3` | The named editing or verifying agent and its version. |
| Automated process | `process:release-check` | An identified process that verified the content. |

The `agent:` scheme is an iWiki convention. OKF v0.3 section 7 permits additional schemes and requires consumers to treat them as non-human. The agent identifier retains the OKF producer and version. Only a valid `human:` event can establish human review. Fail live input validation for an empty identifier, a placeholder or an agent missing its producer or version.

On read or import, also accept the upstream agent spelling `release-editor/2.3`. It describes the same agent as `agent:release-editor/2.3` and has the same effect on trust. Keep stored actor strings and event timestamps intact on unrelated updates. Preserve other valid OKF schemes on import and apply their non-human trust semantics.

## Identify the author of a meaningful change

Add an optional author argument to creation and content-editing tools. Use the following precedence for new content and meaningful edits.

| Input | Stored generated.by |
|------|---------------------|
| The client explicitly identifies the person who wrote the change | The supplied `human:<id>`. |
| The client explicitly identifies the editing agent | The supplied `agent:<producer>/<version>` identifier. A model version may identify the implementation when the producer identifies the agent. |
| No author is supplied, and the current call identifies the editing agent | Construct `agent:<producer>/<version>` from the concrete agent identity. |
| The editing agent cannot be identified | Return author_required before writing. Ask the client to identify the editing agent or explicitly name the human author. |

Treat an invalid explicit author as an input error. Never silently substitute an agent for that value. A human asking an agent to compose a change does not make the human its author. Never infer authorship from a username, token principal, project, server version or previous revision. Never store a generic SDK label, an anonymous placeholder or a bare model name as the author of a live edit.

A client that knows the editing agent should supply its identity directly. Use initialize.clientInfo as a default only when it identifies that actual editor and includes a version. A generic host, proxy, shared SDK or absent handshake does not establish the editor. A client hosting more than one agent must send the actual editor. Treat client-supplied attribution as provenance, not authentication. A client may ask which person wrote the text. It can retry with the concrete editing agent when no person is specified. On a revision conflict, reread the page before resubmission.

The hosted server uses stateless HTTP SDK sessions. Do not assume the SDK retains initialization metadata for a later tool call. Scope any recovered agent identity to the same hosted session and authenticated owner. When identity is unavailable after a reconnect, apply the same author_required rule. Do not change hosted storage or persist new session state merely to avoid an explicit retry.

## Maintain generation and history

Create generated.by and generated.at together on new live content. For a meaningful change, replace both with the author and UTC time of the last meaningful change. Apply the rule to section insertion, deletion, replacement and changes to factual descriptions. Treat content changes as meaningful by default. Let the client explicitly declare a presentation-only correction, including a link repair, when facts and instructions stay unchanged. Do not classify significant whitespace in code examples as a formatting edit automatically. No-ops and metadata-only operations preserve generation time. A reorder that changes instructions is meaningful. Preserve legacy pages without generation until a meaningful write.

Keep source-supplied provenance on import. Preserve historical events, unknown event keys and unrelated frontmatter across normal edits. Preserve body bytes on metadata-only writes. Accept an individual verified mapping or a sequence. Accept RFC 3339 date-times with an offset and date-only values at midnight UTC, both quoted and YAML-native. Generate date-times with UTC offsets. Handle malformed fields without rejecting an otherwise readable page.

## Verify the observed revision

Expose a dedicated wiki_verify_page operation with a domain, slug, verifier and whole-page precondition. The call declares a successful check against a source or resource. Use a human identifier only after explicit confirmation of the reviewed text. Allow an identified agent or named process to confirm an actual source comparison. Write verified.by and verified.at together upon successful live confirmation. Preserve a supplied historical check time on import. Editing, successful structural lint, and confirming who authored an edit must never create verification events.

Use the existing PostgreSQL expected_revision guard. Return a whole-page content hash on Git page reads and require that hash for Git verification. Check it inside the existing locked mutation path. Preserve every previous event when appending. A mismatch, stale replay, unauthorized domain or storage failure must leave the page and its review history intact. A second reviewer who saw an obsolete revision must reread it. A verification-only write preserves generated and stale_after.

Return the resulting trust tier on page reads. Ignore events with an invalid actor or timestamp for trust calculation. If generated is absent, valid events count. When generated.at is valid, count events at or after it, including equality. A present malformed generation time cannot establish that a check is current. A qualifying human event yields human-reviewed. Other qualifying actors yield machine-confirmed. Otherwise return unverified. Keep older events readable. Timestamp precision must distinguish a new meaningful change from a preceding live verification.

## Lint the review deadline

Accept stale_after on page creation and guarded metadata updates. An omitted update preserves the deadline. An explicit empty value clears it. A successful verification does not change it. Never infer it from generated.at, verified.at or a fixed interval.

Add a freshness list to each authorized domain report from wiki_lint. Use a single UTC reference instant for all domains in a call. Return review_due when now is equal to or later than the parsed deadline. Return invalid_stale_after for a malformed nonempty value. Missing, null and empty deadlines produce no finding. Keep the existing stale source report, search behavior and lifecycle status intact. Apply the same calculation to both storage backends. Lint performs no page mutations.

## Acceptance and delivery

1. Exercise the same authorship examples through both public backends. Verify an explicit human, an explicit agent, a known editing client, and missing or generic client identity. Check refusal before any page, revision or index change. Exercise two different clients and a reconnect to detect leaked identity.
2. Read and round-trip singleton and multiple verification events, nested extension keys and timestamps with offsets. Cover new, legacy and imported pages. Compare meaningful edits, formatting, metadata-only changes and no-ops.
3. Confirm the current revision. Change another section and retry the old confirmation. Assert unchanged storage on conflict and prove that a named human remains historical after the next meaningful edit. Repeat with machine and human events in reverse time order. Check that new explicit and inferred agent values use the same prefix in stored Markdown. Compare trust for imported `release-editor/2.3` and new `agent:release-editor/2.3` events. Verify that appending the new event preserves the original actor string.
4. Run lint on both backends with the same fixed instant. Cover absent, null, empty, future, invalid, exact-boundary and past deadlines. Check that repeated lint leaves page bytes and revision unchanged.
5. Update the MCP runbook, tool reference and maintained iclaude and icodex instructions with the authored edit, author_required retry, revision-protected review and deadline workflow. Keep production publication separate from repository verification.
