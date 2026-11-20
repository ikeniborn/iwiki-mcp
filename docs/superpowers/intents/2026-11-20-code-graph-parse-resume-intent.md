---
topic: code-graph-parse-resume
stage: intent
workflow:
  route: chain
  continuation: full
review:
  intent_hash: 8a5c8dc9b94c99b8
  last_run: 2026-11-20
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings: []
---
# Resume an interrupted local code graph build

**Date:** 2026-11-20
**Status:** approved

## Objective

Let an operator restart a graph build after the publisher or local MCP process exits during parsing. Reuse completed file analysis when its inputs still match the checkout.

## Desired Outcomes

- After a process interruption, a fresh process reuses durably completed, compatible file results. It parses unfinished, new and invalidated files. Deleted files disappear.
- After edits, renames or deletions between runs, the completed graph matches a clean build of the final checkout. Cross-file references reflect the current files and symbols, including references from unchanged files.
- An interruption leaves the previously ready graph readable. Existing progress reporting and recovery of an already pinned upload continue to work.

## Health Metrics

Count parser calls by file in the interrupted, resumed and clean runs. Expect zero calls for reused results and no stale files, symbols or relations. Compare canonical graph payloads for identical final inputs. Report parsing time separately from end-to-end time and record checkpoint disk usage.

## Strategic Context

- Interacts with: local code graph indexing for CLI and stdio MCP, configured language adapters, project-wide reference resolution, and stage 2 publication.
- Priority trade-off: preserve graph correctness and access boundaries first, then reduce repeated parsing. In the stage 1 audit the 125-file checkout spent 107 seconds parsing, while project-wide resolution dominated the synthetic 20,000-file test.

## Constraints

### Steering

Use existing build entry points and configured language selection. Keep checkpoint storage local to the publisher. Evaluate cold-build overhead alongside restart savings.

### Hard

Reuse results only when their source identity and parsing inputs are compatible. A partial, corrupt or incompatible checkpoint must never contribute unverified rows to a ready graph. Preserve project isolation, existing publication ownership checks and atomic graph visibility.

## Autonomy Zones

- Full autonomy: inspect the existing pipeline, build disposable fixtures and measure parsing, resolution and storage.
- Guarded: choose reversible internal changes and test the release on a disposable environment before the authorized framework deployment.
- Proposal-first: agree checkpoint compatibility, retention, concurrent build behavior and the meaning of forced rebuild before implementation.
- No autonomy: widening database or MCP grants, discarding operator data, or changing unrelated production services.

## Stop Rules

- Halt reuse when the builder cannot establish compatibility or integrity. Verify recovery against a clean build.
- Stop rollout on a failed correctness check or a failed release rehearsal.
- Done when: a real restarted process passes the changed-checkout comparison with zero parser calls for compatible completed files, regression tests pass, and the tested release is healthy on framework. Verify parse recovery on a machine with a checkout and hosted compatibility through the deployed MCP.
