---
topic: code-graph-python-signature-privacy
stage: plan
status: approved
chain:
  intent: docs/superpowers/intents/2026-11-24-code-graph-python-signature-privacy-intent.md
  spec: docs/superpowers/specs/2026-11-24-code-graph-python-signature-privacy-design.md
review:
  plan_hash: 894859ddfcca50a3
  last_run: 2026-11-25
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings: []
result_check:
  verdict: OK
  source: plan
  plan_hash: 894859ddfcca50a3
  last_run: 2026-11-25
  reviewed: true
  docs_checked: true
---
# Hosted Python signature privacy implementation plan

**Goal:** Keep default values and annotation metadata out of newly published Python signatures while preserving navigation and recovery.

**Architecture:** Project detached rows at the shared export boundary. Version the local upload policy separately from the wire protocol. The source of truth is the [approved design](../specs/2026-11-24-code-graph-python-signature-privacy-design.md).

**Environment:** Python 3.10 or later, SQLite, MCP and PostgreSQL. Use the existing uv and pytest setup. Execute the numbered tasks in order, with TDD and result reconciliation. The renderer and checkpoint unit work can have separate owners if the user selects delegated execution.

## Global constraints

Retain local signatures, stable symbol IDs, relations and the current lock window. Never import project modules or execute defaults or annotations. Keep the PostgreSQL schema and wire fields compatible. Use only synthetic private values in tests. Existing hosted snapshots and production deployment are outside this delivery.

## Task 1. Render the hosted display (R2, R3, R5)

- [ ] Create `tests/codegraph/test_signature_privacy.py`. Pass stored strings from the Python adapter to a new pure function, `project_python_signature(signature: str) -> str`. Use the actual compact kind/async prefix in test cases. Start with these assertions:

```python
assert project_python_signature('function|(token:str="MiXeD")->list["User"]') == 'function|(token:str=...)->list[User]'
assert project_python_signature('method|(self,x:Unknown[User,"MiXeD"])') == 'method|(self,x:…)'
assert project_python_signature('async_function|async(x:Annotated[Literal["MiXeD"],meta()])') == 'async_function|async(x:Literal[…])'
```

- [ ] Run the focused tests and record the initial failure. Add `src/iwiki_mcp/codegraph/signature_privacy.py`. Separate the stored prefix and parse a synthetic declaration statically with a compatible Python grammar. Render parameter categories and default presence from syntax nodes. Cover positional-only and keyword-only separators, variadics and absent defaults. For Python syntax accepted by the project grammar but outside the host interpreter grammar, exercise the minimum supported runtime before relying on its parser.
- [ ] Handle known annotation constructors by their type positions. Render names, dotted references, unions and safely understood forward references recursively. Replace Literal's entire argument list; descend only into the first Annotated argument. Include qualified names, quoted nested types, ambiguous aliases, calls and unexpected syntax. Unsupported annotations produce one marker. Reject a malformed declaration with a sanitized exception. Apply the same rule to returns. Add a fixture with a side-effecting expression and assert it never runs.
- [ ] Run the commands below. Require passing renderer cases and unchanged local adapter expectations before integrating.

```bash
uv run pytest -q tests/codegraph/test_signature_privacy.py tests/codegraph/test_python_adapter.py
```

## Task 2. Project one captured revision before hashing (R1, R2, R3, R5)

- [ ] Add a failing integration test in `tests/codegraph/test_application.py` using a real ready SQLite fixture, two overloads differentiated by Literal values, and a non-Python control. Capture local symbol rows and relation endpoints. Record ordinary publisher batches and compare every published row kind against its local counterpart, allowing differences in the two Python signature fields and the projected target text of Python calls.
- [ ] In `src/iwiki_mcp/codegraph/runtime.py`, call the renderer after `export_rows()` releases its read lock and before `graph_payload_revision(rows)`. Map file ID to its captured language once. For each Python symbol with a signature, replace the display and derive its folded value using `models.compact_casefold`. Leave null signatures and unrelated rows intact. Map a rendering failure to a sanitized export failure before begin or pin preparation.
- [ ] Prove both overload IDs, all reference endpoints, null signatures, and the original on-disk SQLite signatures survive. Confirm the frozen cache and freshly parsed graph with the same inputs produce identical projected rows. Recompute the header digest from those rows. Assert the marker and its case-folded spelling are absent from all serialized batches. Exercise current SQLite capture/activation interleaving during a stalled transfer and failure before begin.

```bash
uv run pytest -q tests/codegraph/test_application.py tests/codegraph/test_publication.py tests/codegraph/test_runtime.py
```

- [ ] R3a/R5. Cover literal-bearing call labels in default and body expressions at both publication transports and in pinned bytes. Project detached Python CALLS labels before hashing. Check retained benign labels, IDs and endpoints. Compare full export on the same 20,000-file SQLite fixture against 0.7.329, alternating order with one warm-up and five timed runs per version. Record the median, scope and limitations.

## Task 3. Refuse incompatible pins before contacting the peer (R4, R5)

- [ ] Extend `tests/codegraph/test_resume_checkpoint.py` and `test_resume_application.py` with real manifests. Construct an authentic previous-format pinned record with valid checksum, row hash and credential proof, including a partly acknowledged session. Use the existing recording peer. Save manifest and row file bytes before the call. Require `resume_incompatible`, unchanged bytes and an empty call list on two consecutive retries. Include a future policy marker, a corrupt manifest and a wrong credential as distinct cases.
- [ ] In `checkpoint.py` set an explicit integer signature-policy revision when preparing a new pin. Include it in the checksum. Preserve it when saving progress, validate it for pinned records and reject unrecognized revisions. Avoid marking a legacy record as current through the generic save path. In `application.resume_project`, perform the compatibility decision after normal target and credential checks, before constructing or calling the peer. Permit a valid legacy ready receipt to return its stored revision and counts locally.
- [ ] Test compatible retry after a lost reply and a changed local graph: compare pinned row bytes, attempt key and the set of transferred batch ordinals with the original attempt. Inspect freshly pinned bytes for the same markers as ordinary publication. Exercise explicit forget followed by a fresh projected attempt. Verify CLI JSON category, existing exit code and actionable text without exposing source or credentials. Adjust `src/iwiki_mcp/admin.py` wording only if the recovery diagnostic needs it.

```bash
uv run pytest -q tests/codegraph/test_resume_checkpoint.py tests/codegraph/test_resume_application.py tests/test_code_publish_cli.py
```

## Task 4. Prove the hosted contract and reconcile delivery (R1–R5)

- [ ] Extend existing PostgreSQL graph publication/reader tests with rows emitted by the production exporter. Exercise direct publication and the MCP application path. Query active symbols by ID and use search terms that match only the signature to distinguish retained type text from hidden values. Compare published IDs and associated relations with the locally captured baseline. Use an isolated database and check the accepted final revision. Ensure these tests execute with PostgreSQL enabled.

```bash
uv run pytest -q -rs tests/postgres/test_code_graph_publication.py tests/postgres/test_code_graph_reader.py tests/postgres/test_code_graph_http.py
```

- [ ] Update both `docs/code-graph-publishing.md` and `docs/code-graph-publishing.ru.md` with display examples and legacy pin recovery. Explain that a ready receipt describes its historical revision. Apply the repository patch version bump across package metadata, lockfile and package test. Update the project wiki with observable scenarios, implementation/test bindings and verification evidence.
- [ ] Run static checks, then one full suite on the final implementation with disposable PostgreSQL and Docker fixtures. Explain any skipped cases and execute missing required coverage separately. Record commands, exits and tested commit in the task page. Reconcile the result against this plan before integration.

```bash
uv run python -m compileall -q src tests
uv run flake8 src tests
git diff --check
uv run pytest -q -rs
```

**Human checkpoint:** Stop if passing the fixtures requires changing public IDs, exposing a hidden value, or revising the approved checkpoint contract. Return the affected decision to the user. Complete the implementation when all acceptance checks have evidence and both language guides match the verified behavior.
