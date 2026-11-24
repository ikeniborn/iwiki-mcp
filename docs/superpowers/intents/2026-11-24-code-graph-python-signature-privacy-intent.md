---
workflow:
  route: chain
  continuation: full
review:
  intent_hash: da526f3d30912d00
  last_run: 2026-11-24
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings: []
---
# Intent: code-graph-python-signature-privacy

**Date:** 2026-11-24
**Status:** approved

## Objective

Let hosted code graph users navigate Python functions without publishing values embedded in their signatures. The 0.7.329 publisher copies parameter and return annotation text into symbol rows. An existing upload checkpoint can replay those rows without rebuilding them.

## Desired Outcomes

- Preserve parameter names, kinds and the presence of defaults. Display each default as `...`, including expressions and simple constants.
- Apply the same annotation rules to parameters and return types, including nested annotations. Display `Literal["prod"]` as `Literal[…]` and `Annotated[UserId, "internal-id"]` as `UserId`. The ellipsis in a literal type is a display marker.
- Preserve statically understood type references. For example, display `list["User"]` as `list[User]`. Replace unrecognized annotation expressions with a marker without exposing their source text.
- Keep overloads distinguishable even when their published signatures look identical. Preserve their graph references.
- On resume of an unfinished pre-policy checkpoint, report `resume_incompatible` before sending any batches. Retain the checkpoint so the operator can check the hosted revision and explicitly start a new publication.
- For a completed pre-policy checkpoint, return the recorded revision locally. A valid current-policy checkpoint retains its normal recovery behavior.

## Health Metrics

- Zero occurrences of a synthetic private value or its case-folded form in published signature fields or serialized batch contents for the fixture, including builds using an existing cache.
- Two overload nodes for two overloads differing only in hidden literals. Their references resolve to the same logical targets as before publication.
- Zero payload batches sent and unchanged checkpoint contents for a refused legacy retry. Zero remote calls for a completed legacy checkpoint.

## Strategic Context

- Interacts with: Python extraction, symbol identity, signature search, cached parse results, snapshot encoding and resumable uploads.
- Priority trade-off: withhold concrete signature values while retaining navigation. An exact list of Literal alternatives is expendable in hosted display.

## Constraints

### Steering

- Use Python typing conventions for the displayed type and document the meaning of redaction markers.

### Hard

- Apply the policy to both signature and signature_casefold before new hosted payloads leave the publisher.
- Interpret annotations statically. Never evaluate a default or annotation to determine its display.
- Keep existing hosted snapshots and their retention outside this change. Preserve operator control over stale upload checkpoints.

## Autonomy Zones

- Full autonomy: select synthetic fixtures and internal verification tools.
- Guarded: adapt capture, cache reuse and publication internals while preserving these outcomes.
- Proposal-first: seek approval for changes to public identifiers or recovery semantics beyond the outcomes above.
- No autonomy: erase published snapshots, discard an operator checkpoint automatically or deploy to production under this intent.

## Stop Rules

- Stop if a hidden source value survives publication, equivalent displays collapse overloads, or a legacy retry sends a batch.
- Escalate if preserving references requires a change to publicly observable symbol identity.
- Done when: published rows and batches meet the privacy checks, both overloads remain navigable, and all three checkpoint outcomes pass executable tests.
