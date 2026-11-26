---
workflow:
  route: chain
  continuation: full
review:
  intent_hash: 2e48d19896da892e
  last_run: 2026-11-26
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings: []
---
# Intent: okf-stale-after-lint

**Date:** 2026-11-26
**Status:** approved

## Objective

Let readers distinguish verified content from earlier review history. Let maintainers find documents due for review through the existing MCP lint operation.

## Desired Outcomes

- A verifier can confirm a specific, unchanged page after checking its sources. Readers can see who checked it and when.
- A meaningful change invalidates earlier confirmations for trust. A formatting-only correction preserves their applicability.
- A reader can distinguish unverified, machine-confirmed and human-reviewed content. Old events and independent checks remain available.
- A maintainer sees expired and malformed deadlines in domain lint. A missing deadline is valid. Review and deadline management remain independent.

## Health Metrics

- Both backends pass the same public verification and deadline examples.
- A conflict changes zero stored events. Ordinary edits preserve all existing events.
- Lint changes zero pages or verification times. Equality at a deadline yields a review finding.

## Strategic Context

- Interacts with: MCP page authoring, section mutation, page reads, domain lint, Git, PostgreSQL and client instructions.
- Priority trade-off: preserve truthful review evidence when a writer or a verification time is unknown.

## Constraints

### Steering

- Use OKF v0.3 trust and lifecycle semantics.
- Keep default search and document retirement behavior intact.

### Hard

- Attribute a meaningful edit to a concrete human or identifiable agent. Default to the editing agent when a human author is unspecified. Ask the client for its identity when the agent cannot be identified. Refuse anonymous placeholders.
- Require a real review before recording a successful verification. Identify the reviewer and record the time of the check.
- Keep historical events, optional trust metadata and both standard verification representations readable.
- Protect a confirmation against concurrent changes to any part of the reviewed document.
- Determine expiry from the explicit absolute deadline. Never infer a review interval.

## Autonomy Zones

- Full autonomy: choose internal representations and fixtures.
- Guarded: extend the public contract with focused tests on Git and real PostgreSQL.
- Proposal-first: introduce obligations for existing writers or change the caller identity model.
- No autonomy: assign retroactive human approvals or migrate live content in bulk.

## Stop Rules

- Escalate a compatibility decision that would reject an existing content writer or falsely attribute a change to an actor.
- Done when: public reads show the correct trust tier after review, meaningful edits and formatting corrections on both backends. A stale verification attempt changes nothing. Lint distinguishes expired, malformed and missing deadlines at the UTC boundary.
