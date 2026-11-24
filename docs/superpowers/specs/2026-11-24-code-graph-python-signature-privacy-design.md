---
topic: code-graph-python-signature-privacy
stage: spec
status: approved
chain:
  intent: docs/superpowers/intents/2026-11-24-code-graph-python-signature-privacy-intent.md
review:
  spec_hash: 17f20105eebfbdc5
  last_run: 2026-11-24
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings: []
---
# Publish Python signatures without their embedded values

## Publication boundary

R1. Produce the hosted representation once in the publisher's shared SQLite export path. Copy one revision under the existing read guard. After release, project its Python symbol rows, then compute the header digest over the projected payload. Both ordinary publication (MCP and direct PostgreSQL) and preparation of a new resumable MCP upload consume those rows and that header. Local indexing, local queries, deterministic IDs and the parse cache continue to use the original records. Apply the same projection on a cache hit, without opening source files or extending the copy lock. Previously accepted hosted snapshots stay under their existing retention policy.

Select symbols using the captured file language and file ID. Preserve canonical row order, non-Python rows, null class signatures, repository revision and relation endpoints. Preserve each Python symbol ID byte for byte. SQLite validates the ID against the original signature; hosted reference validation checks endpoints against published IDs. Two overloads may have identical display text and different IDs. Digests identify the actual projected payload. Treat IDs and existing content fingerprints as identifiers, not as a guarantee against guessing a value from an independently known source.

## Display grammar

R2. Interpret the stored Python adapter signature as data. Preserve its kind and async indicators, parameter names and order, positional-only slash, keyword-only separator, variadics, annotations and return position. Render every present default as three ASCII dots. Never echo a default expression or invoke it. Use the existing normalization routine to derive signature_casefold exclusively from the new display. Do not send an auxiliary raw signature.

R3. Render annotation nodes recursively from a positive list of supported type syntax. Keep type names, dotted type references, unions and understood type constructors. A quoted forward reference in a known type position, including the element of list, can be parsed with the same restricted grammar. Render Literal arguments as one Unicode ellipsis regardless of value or count. For Annotated render only the recursively projected first argument. Apply these rules equally inside quoted type expressions and return annotations. Preserve only recognized type positions for parameterized forms. An unrecognized subscript or ambiguous alias can hide arbitrary metadata: replace that expression as a unit with the marker rather than interpreting its quoted arguments as names. Never import user modules, evaluate an expression or use the input text as fallback.

| Stored annotation or parameter | Hosted display fragment |
| --- | --- |
| token: str = get_token() | token:str=... |
| list["User"] | list[User] |
| typing.Literal["prod", "stage"] | Literal[…] |
| Annotated[list[Literal["prod"]], "internal"] | list[Literal[…]] |
| UnknownAlias[User, "internal"] | … |

Examples specify the displayed tokens; retain existing canonical spacing elsewhere. Preserve parameter structure when one annotation is unsupported. If the stored declaration itself cannot be safely decomposed, stop export with a sanitized failure before pin creation or remote begin. Do not include source text in diagnostics. Document that this view is for navigation and is intentionally lossy for typing.

## Upload recovery

R4. Mark each newly prepared pin with an explicit signature policy revision inside the existing checksum-protected manifest. Keep the current credential and target checks. Test compatibility after these checks and before remote capability checks, inspection, batch generation or transfer. Keep the distinction between an intact incompatible pin, a corrupt record and a credential mismatch. Never infer the policy from spelling, a matching digest or a newly rebuilt local graph.

| Verified local checkpoint | Publisher action |
| --- | --- |
| Absent | Capture, project and pin the new rows through the shared path. |
| Pinned with supported policy | Reuse the original bytes, attempt key and receipts, even after a local rebuild. |
| Pinned with absent or unsupported policy | Return resume_incompatible locally; preserve manifest and rows, including after repeat invocation. |
| Ready from the previous or current policy | Return its recorded revision and counts locally after normal validation. |

Preserve the marker when saving session progress. Document checking the hosted active revision after an incompatible attempt and the existing explicit forget operation before starting fresh. Discarding local evidence cannot undo accepted remote batches. A ready receipt represents a historical result.

## Verification and delivery

R5. Use synthetic markers, including mixed case. Inspect both signature fields, every serialized batch and newly pinned row bytes. Exercise ordinary publication, resumable capture and the shared direct PostgreSQL path. Check the hosted lookup by ID and signature search against the projected fields. Compare clean and warm-cache exports for identical projected payloads on the same input. Read the local graph again to verify the source signatures and IDs. Compare the two overload IDs and all existing reference endpoints with the originals. Use deterministic fixtures; do not require reference resolution to invent links that were absent from the original graph.

Cover quoted and nested annotations, alias ambiguity, functions and methods, async declarations, null signatures, all parameter kinds, an unsupported expression and a malformed declaration. Assert zero source marker bytes in transmitted payloads for fixtures with markers confined to signature values. Verify that rendered rows recompute to the advertised graph payload revision and that non-Python data remains equivalent. Reuse the SQLite single-revision and lock-release checks.

Exercise the recovery matrix through the CLI/application entry point with real on-disk manifests, including a partly acknowledged pre-policy upload, compatible interrupted transfer and old ready receipt. Check exact error category, remote call counts, unchanged refused checkpoint bytes and success after an explicit fresh start. Update publisher documentation and relevant wiki guidance before result reconciliation. No hosted schema migration or production deployment is required to validate the publisher.
