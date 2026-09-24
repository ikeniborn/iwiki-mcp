---
review:
  intent_hash: 5e1daf8b54995e58
  last_run: 2026-09-24
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

# Intent: system1-page-type-shadow-pilot

**Date:** 2026-09-24
**Status:** approved

## Objective

Determine on real iwiki pages whether LAYA can classify the existing six page types
faster and more reliably than the current generative chat classifier, while leaving
production behavior unchanged. The pilot is needed now because a separate local GPU
endpoint is becoming available and the existing classification seam provides a bounded
place to measure System 1 value before granting it any decision authority.

## Desired Outcomes

- For each evaluated real iwiki page, the shadow result records one of `architecture`,
  `api`, `guide`, `reference`, `runbook`, or `concept`, its class probabilities, and
  request latency.
- The evaluation report compares LAYA with the current generative chat baseline using
  accuracy, macro-F1, p50 latency, and p95 latency, and reports LAYA calibration using
  Brier score and expected calibration error (ECE).
- Enabling the shadow produces the same explicit type, tags, page path, write result,
  and user-visible write error as the current flow; the LAYA result is observational
  only.
- An unavailable, failed, or timed-out System One endpoint does not change whether the
  current write succeeds or fails.
- A configured System One base URL ending in `/v1` sends the shadow request to exactly
  `<base>/systemone`, without adding a second version segment.
- The final evidence supports exactly one recommendation: `go`, `fine-tune`, or
  `reject`, with the measured quality, calibration, and latency results attached.

## Health Metrics

- Explicit `type` and `tags` remain authoritative and byte-equivalent at the write
  boundary with shadow mode enabled or disabled.
- With shadow mode disabled, tests observe zero System One HTTP requests; benchmark
  results report no additional network wait in the write path.
- With the endpoint unavailable, the current write's success or error outcome is
  unchanged in focused failure-path tests.
- Focused request-construction tests observe exactly one `/v1` segment when the
  configured System One base URL already ends in `/v1`.
- Logs contain no API credentials, page body, or raw endpoint diagnostic payload.
- Focused classification and write tests pass, followed by the full test suite on the
  final unchanged code-state fingerprint.
- The report records baseline and LAYA p50/p95 latency. The guarded benchmark step
  freezes the numeric p95 acceptance threshold immediately after the baseline
  measurement and before LAYA results are scored.

## Strategic Context

- Interacts with: the Framework-hosted local GPU API root ending in `/v1`, its
  `/systemone` resource, iwiki's server-side write classifier, the evaluation harness,
  endpoint operators, and authoring agents that submit pages.
- Priority trade-off: trust first, then speed, then cost.

## Constraints

### Steering (behavioral guidance)

- Start with a shadow-only evaluation on real iwiki pages.
- Reuse the current six-type taxonomy and its existing labels.
- Add the smallest dedicated System One client needed for the pilot; do not introduce a
  generic AI-provider abstraction.
- Derive quality and latency decisions from the measured baseline, and freeze the
  resulting thresholds before scoring LAYA.

### Hard (architectural enforcement)

- System One uses a separate endpoint and separate credentials from the current
  embedding and chat configuration.
- `IWIKI_SYSTEM1_BASE_URL` is the API root and ends in `/v1`; the client appends only
  `/systemone` and never inserts another version segment.
- LAYA never changes frontmatter, tags, page path, write result, or write error during
  this pilot; explicit type and tags remain authoritative.
- Failure and timeout are fail-open for the existing write flow.
- Automated tests make no network calls.
- Model training, fine-tuning, and a retrieval sufficiency gate are outside this pilot.
- Page bodies, credentials, and raw endpoint errors are never written to logs.

## Autonomy Zones

- Full autonomy (reversible, low risk): implement the isolated shadow client, offline
  fixtures, metric calculation, report generation, focused tests, and documentation
  inside this intent.
- Guarded (log + confidence threshold): call the configured local `/v1/systemone`
  endpoint, select and redact the real-page evaluation corpus, freeze measured quality
  and latency thresholds before scoring LAYA, and retain only aggregate evaluation
  metrics.
- Proposal-first (needs approval): let LAYA influence production type, tags, page path,
  write behavior, or retrieval; expand the public API or configuration beyond the
  separate System One endpoint and credential settings required by this pilot.
- No autonomy (human only): switch production decisions to LAYA, train or fine-tune the
  model, disclose page content or credentials, or change ACL, project binding, or
  specification enforcement.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions
> is marked HUMAN CHECKPOINT in the plan.

## Stop Rules

- Halt if: the configured API root does not end in `/v1`, its System One resource is not
  `<base>/systemone`, credentials or page content cannot be isolated, or shadow
  execution changes any observable write behavior.
- Halt if: focused or regression tests show a write-path regression that cannot be
  removed without granting LAYA production authority.
- Escalate if: the six labels cannot be mapped unambiguously to endpoint output,
  probability calibration cannot be measured, or the required scope crosses into
  production mutation or retrieval decisions.
- Done when: focused and full tests pass; a base ending in `/v1` produces exactly one
  `/v1/systemone` request path; a live or explicitly identified recorded-replay benchmark
  over real iwiki pages reports accuracy, macro-F1, Brier score, ECE, and p50/p95 latency;
  write behavior remains unchanged; and an evidence-backed `go`, `fine-tune`, or
  `reject` recommendation is recorded.
