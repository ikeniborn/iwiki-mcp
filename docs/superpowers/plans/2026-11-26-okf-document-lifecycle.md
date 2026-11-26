---
review:
  plan_hash: b9d8fdc35d507df6
  last_run: 2026-11-26
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-11-25-okf-document-lifecycle-intent.md
  spec: docs/superpowers/specs/2026-11-25-okf-document-lifecycle-design.md
---
# OKF document lifecycle implementation plan

**Goal.** Look up current operational guidance using an obsolete document name and retain exact-address and historical access on both backends.

**Authority.** The [approved design](../specs/2026-11-25-okf-document-lifecycle-design.md) and [approved intent](../intents/2026-11-25-okf-document-lifecycle-intent.md) define acceptance. The [OKF v0.3 specification](https://github.com/open-knowledge-format/open-knowledge-format/blob/main/SPEC.md) defines lifecycle metadata (§5.4) and paths (§6). The MCP API, ranking, tag exclusions and authorization rules below implement the approved iwiki design.

## Execution order

Use inline execution in the three existing worktrees with superpowers:executing-plans. Follow steps 1 through 17 in order. Within each implementation step, first add a failing focused case that distinguishes the new behavior. Implement that step and run its verification. Record command, revision or working-tree fingerprint, exit status, test count and observed result in the existing task ledger. Begin the next step only after the preceding check demonstrates its expected result. If a check fails, diagnose it, repair that step and rerun the narrow check. Do not mark skipped database cases as passes. Leave implementation checkboxes open until evidence exists.

Run commands from the iwiki-mcp worktree except for the two labeled client steps. Keep public positional argument order, existing revision and scope checks, code selectors, specification bindings and unrelated body bytes. Apply each repository's version rules. A persistent migration, change to the approved default, bulk production edit or expanded authorization requires a human decision before dependent work.

## Acceptance coverage

| Approved design area | Implementation and verification steps |
|---|---|
| Lifecycle status, absent status, path resolution and authority | 2–3, 7–8, 13 |
| Tool write, exact read, combined edit, revision and rollback | 4–7, 13 |
| Old-name search, facets, candidate limits, hydration and all mode | 9–13 |
| Historical task discovery and generated agent instructions | 12–16 |
| Both wire transports, production boundary and final evidence | 1, 13, 17 |

## Step 1. Establish the test environment

**Outcome.** Prepare isolated evidence.

- [ ] Use the three existing dev-okf-document-lifecycle worktrees. Record each branch, HEAD and working-tree state. In iwiki-mcp use the frozen dev environment. Before the first database case, provision a disposable pgvector database following docs/postgres-ci.md and tests/postgres/conftest.py. Set IWIKI_TEST_POSTGRES_DSN privately. Use the deployment-suite network topology when running deployment checks. Create the reports directory for JUnit artifacts. Before changing public behavior, use wiki_spec_search and wiki_spec_context in the bound server project to locate existing scenarios for write, read, search, CAS and scope. Propose changes to an existing scenario contract or ID before altering it. Pair new scenarios with their executable tests.
- [ ] Verify. Execute a real store test with the disposable database. The CI checker must report at least one database case and zero skips. Keep the report and base SHAs. The command for this preflight uses an existing real-store roundtrip; subsequent filtered commands name tests to add in the corresponding step.

```bash
uv sync --frozen --extra dev
mkdir -p reports
uv run --frozen pytest -q tests/postgres/test_store.py::test_create_list_and_read_page_return_numeric_revision --junitxml=reports/baseline.xml
uv run --frozen python scripts/check_postgres_junit.py reports/baseline.xml
```

## Step 2. Interpret declared and effective status

**Outcome.** Resolve OKF §5.4.

- [ ] Add scalar lifecycle interpretation to src/iwiki_mcp/engine/lifecycle.py and tests/test_document_lifecycle.py. For an absent key, expose absence on direct read and treat it as stable in current lookup. For stable, draft, deprecated, stub, developing and a custom value, preserve the declaration. Only stable and absent are terminal current states. Ignore a successor on a non-deprecated imported page during traversal.
- [ ] Verify. The table-driven status test distinguishes raw absence from explicit stable and rejects every noncurrent terminal.

```bash
uv run --frozen pytest -q tests/test_document_lifecycle.py -k status
```

## Step 3. Resolve bundle-relative successors

**Outcome.** Resolve OKF §6 paths.

- [ ] In the same module take a source identity and authorized in-bundle lookup. Resolve ./next.md from its parent, /guide/next.md from the domain root, and valid ../ paths that stay inside the bundle. Respect case and percent-encoded UTF-8. Validate the decoded path before lookup. Never use a URL fetcher. Never let cross-domain wiki-link syntax switch bundles. Use one authorized request-local cache; a successful retirement must be visible to the next search.
- [ ] Verify. Test a multi-hop chain, valid neighboring files, missing page, self-loop, longer cycle, escaping path, unsafe encoded separators and external URL. A spy must record zero reads outside the allowed bundle; unrelated stable pages must still resolve.

```bash
uv run --frozen pytest -q tests/test_document_lifecycle.py
```

## Step 4. Write lifecycle metadata on creation

**Outcome.** Create contract on both backends.

- [ ] Update wiki_write_page, the PostgreSQL frontmatter builder in src/iwiki_mcp/server.py, src/iwiki_mcp/okf.py and the frontmatter vocabulary in engine/frontmatter.py. Keep positional arguments compatible. Explicit tool values win over authored fields. A new page defaults to stable. Reject a nonempty replacement unless the resulting status is deprecated. Distinguish create defaults from read, update, export and migration defaults.
- [ ] Verify. Check omitted, authored and explicit combinations on both stores. An invalid request leaves zero pages and index entries. Import/read a legacy fixture and perform an unrelated edit or applicable Git maintenance: preserve its original lifecycle status and replacement. Include explicit draft and legacy stub fixtures.

```bash
uv run --frozen pytest -q tests/test_server_write_frontmatter.py tests/test_okf_build_frontmatter.py tests/test_export_okf.py
uv run --frozen pytest -q tests/postgres/test_tool_matrix.py -k lifecycle_create --junitxml=reports/create.xml
uv run --frozen python scripts/check_postgres_junit.py reports/create.xml
```

## Step 5. Expose metadata on exact-address reads

**Outcome.** Direct read contract.

- [ ] Extend wiki_read_page on Git and PostgreSQL for whole-page and heading reads. Return raw lifecycle declarations from the requested source. Preserve markdown, heading body, revision and section hash wherever the current contract provides them. Do not follow a replacement during a direct read.
- [ ] Verify. Read a retired page and a heading present only on the old page. Compare original payloads. Compare authorized source responses for ungranted existing versus nonexistent external targets; target availability must be indistinguishable.

```bash
uv run --frozen pytest -q tests/test_server_read.py
uv run --frozen pytest -q tests/postgres/test_read_section.py -k lifecycle --junitxml=reports/read.xml
uv run --frozen python scripts/check_postgres_junit.py reports/read.xml
```

## Step 6. Validate and apply Git retirement

**Outcome.** Guarded metadata and body preservation.

- [ ] Extend both _prepare_update_page_call and _prepare_update_page_request in server.py. Append superseded_by after existing public parameters. Admit lifecycle-only updates and section edits with lifecycle changes. Preserve existing code-only constraints and section-hash guards. Use the existing raw-body preservation and Git mutation/indexing path. Omission preserves the successor, explicit empty clears it, transition away from deprecated clears it. Validate before writes.
- [ ] Verify. Cover status alone, replace, clear, return to stable, invalid pair, mixed section update and an unrelated section edit. Compare exact CRLF/UTF-8 bytes on metadata-only operations; compare selectors, tags and GWT fences. Inject the existing index failure and verify rollback of file, log and index. Also verify stale section-hash refusal leaves metadata untouched on a combined edit.

```bash
uv run --frozen pytest -q tests/test_server_update.py tests/codegraph/test_frontmatter_roundtrip.py tests/test_specification_git.py
```

## Step 7. Apply PostgreSQL retirement atomically

**Outcome.** CAS and transaction semantics.

- [ ] Reuse store.update_page and existing projection transaction, passing expected_revision. Run the same transition matrix as Git. Compete two updates against one source revision. Inject a transaction failure after page update using the existing store fixtures.
- [ ] Verify. Exactly one competing write succeeds. Verify page revision, body, section hash, selectors, spec bindings and visible index following conflict, injected failure and subsequent successful retry. Verify expected_revision_required for omission.

```bash
uv run --frozen pytest -q tests/postgres/test_tool_concurrency.py tests/postgres/test_tool_matrix.py tests/postgres/test_specifications.py --junitxml=reports/retire.xml
uv run --frozen python scripts/check_postgres_junit.py reports/retire.xml
```

## Step 8. Report invalid replacement chains in lint

**Outcome.** Source-scoped diagnostics.

- [ ] Use the shared lifecycle traversal from Git engine/lint.py and PostgreSQL store.lint_domain. Emit findings for broken, malformed and cyclic declared replacement chains against readable source pages. Retired pages without a successor are valid.
- [ ] Verify. Test a permitted chain alongside each defect. Compare results across Git and PostgreSQL. For an external reference the report identifies the authorized source without probing the external target. Verify that ordinary link and section findings are retained.

```bash
uv run --frozen pytest -q tests/test_server_lint_sync.py tests/test_lint_frontmatter.py
uv run --frozen pytest -q tests/postgres/test_tool_matrix.py -k lifecycle_lint --junitxml=reports/lint.xml
uv run --frozen python scripts/check_postgres_junit.py reports/lint.xml
```

## Step 9. Map search evidence to terminal locators

**Outcome.** Ownership of ranking and section selection.

- [ ] In src/iwiki_mcp/retrieval.py introduce the shared current-mode candidate transformation for both backends. Use old title, path or section as evidence. Gather any existing eligible direct terminal hits. Otherwise select the matching terminal heading then the first indexed terminal section by ordinal. Enforce requested heading, positive type and tags, and normalized exclusions on the terminal. Ignore terminals without an eligible indexed section.
- [ ] Verify. Create tests/test_retrieval_lifecycle.py with old-heading-only matches, direct matches from two terminal sections, a no-section target and conflicting source/terminal tags. Assert complete domain/file/heading/chunk tuples. Suppress alias hits when a direct hit exists and retain the best alias rank when several aliases lead to the same terminal. Use the requested heading only on the successor, a matching old heading on a different chunk ordinal, and no matching heading with no explicit heading constraint to exercise fallback.

```bash
uv run --frozen pytest -q tests/test_retrieval_lifecycle.py -k terminal
```

## Step 10. Apply pre-limit grouping in Git

**Outcome.** Protect candidate diversity.

- [ ] In _domain_signals and prepare_read_candidates apply the shared rule to lexical page/section, semantic summary/chunk and graph-derived evidence before the applicable seed or candidate cap. Include a fixture whose old slug alone matches the query. Keep ordinary direct hits at section granularity. Keep all-mode original-page ranking.
- [ ] Verify. Seed more aliases than both the internal seed bound and the candidate bound, plus an independent eligible page. With k=2 both terminal pages survive; duplication of an alias cannot promote the successor by repeated evidence. Exercise lexical, semantic and hybrid with deterministic embeddings, and exercise graph expansion with an unauthorized neighboring bundle.

```bash
uv run --frozen pytest -q tests/test_retrieval_lifecycle.py tests/test_retrieval.py tests/test_retrieval_facets.py
```

## Step 11. Apply the same ranking to PostgreSQL

**Outcome.** Storage parity.

- [ ] Wire the same policy into postgres/store.py prepare_read_candidates and retrieval.prepare_storage_candidates. Use authorized page data and valid indexed sections from the selected scope. Distinguish a missing target from a page with no eligible terminal section internally. Avoid a persisted reverse-link index.
- [ ] Verify. Repeat the Git fixture through PostgreSQL with identical embeddings and controlled rank ties. Compare eligible identities, direct section granularity, alias fallback and limited-k results. Check both existing and absent out-of-scope targets. Refuse to accept a skipped suite.

```bash
uv run --frozen pytest -q tests/postgres/test_store.py -k lifecycle --junitxml=reports/ranking.xml
uv run --frozen python scripts/check_postgres_junit.py reports/ranking.xml
```

## Step 12. Wire read-search arguments through MCP and reranking

**Outcome.** Public search contract.

- [ ] Append lifecycle and exclude_tags to wiki_search. Forward read heading through both candidate paths. Keep write-intent target selection and duplicate discovery across statuses. Validate unsupported lifecycle values and normalize excluded tags. Pass only eligible terminal locators to hydration, reranking and final slicing.
- [ ] Verify. Assert omitted equals explicit current across all modes. All returns original locators, including legacy task pages. An explicit missing terminal heading yields no alias hit. Capture the actual reranker input: it contains the successor text. Verify exact-address readback, binding provenance and unchanged write-target selection.

```bash
uv run --frozen pytest -q tests/test_server_search.py tests/test_server_search_facets.py tests/test_server_search_binding_provenance.py tests/test_server_search_write_intent.py tests/engine/test_rerank.py
uv run --frozen pytest -q tests/postgres/test_http.py -k "write_intent or lifecycle_search" --junitxml=reports/search.xml
uv run --frozen python scripts/check_postgres_junit.py reports/search.xml
```

## Step 13. Replay the complete public workflow

**Outcome.** Wire protocol and accepted GWT behavior.

- [ ] Use tests/test_mcp_smoke.py for Git stdio and tests/postgres/test_http.py for bound hosted HTTP. Inspect tools/list. Run create → read revision → retire → lookup obsolete name → read returned heading → historical task lookup. Include unauthorized and invalid paths, an update with a stale revision, and a separate unchanged page. Use a private temporary bundle for each backend. For the last lookup create old and active task documents with the same task tag. Confirm all plus the positive tag returns both. Confirm exclude_tags omits both from working guidance.
- [ ] Verify. Both transports must exercise tools/call and return the expected locators, historical record and error conditions. Save sanitized observed results. Match these wire cases to the scenario IDs identified in step 1. Confirm applicable implements/verifies bindings and retain checks for the original contract.

```bash
uv run --frozen pytest -q tests/test_mcp_smoke.py
uv run --frozen pytest -q tests/postgres/test_http.py -k lifecycle --junitxml=reports/mcp.xml
uv run --frozen python scripts/check_postgres_junit.py reports/mcp.xml
```

## Step 14. Update server reference and MCP runbook examples

**Outcome.** Server documentation.

- [ ] Update docs/tools-reference.md, docs/tools-reference.ru.md, docs/okf-compatibility.md, docs/okf-compatibility.ru.md, templates/AGENTS.md.snippet, templates/CLAUDE.md.snippet and the served instructions in src/iwiki_mcp/resources.py. Document the new-page default, current versus all, the effect on stub/developing, metadata-only retirement, omitted versus empty replacement and server tool-signature prerequisites.
- [ ] Verify. Check the EN/RU examples against tools/list and the successful wire fixtures from step 13. Verify the two templates resolve old names, retrieve all task statuses and direct readers to the actual original page.

```bash
uv run --frozen pytest -q tests/test_resources.py tests/test_resources_frontmatter.py tests/test_mcp_smoke.py
```

## Step 15. Update and generate iclaude instructions

**Outcome.** First client rollout.

- [ ] In the iclaude worktree update docs/iwiki-mcp-modes.md, .claude-isolated/CLAUDE.md and .claude-isolated/hooks/iwiki-remote-scope.js. Inspect active context-awareness and task-ledger skills for contradictory examples. Change the maintained producer when emitted text requires correction.
- [ ] Verify. Extend generated-output assertions and render instructions into a disposable project. Confirm current working-guidance searches exclude task and task-history, task discovery uses lifecycle=all with tag task, and binding/permission text survives. Exercise runbook arguments on the verified server contract.

```bash
bash tests/test_iwiki_remote_scope.sh
bash tests/test_iwiki_mcp.sh
```

## Step 16. Update and generate icodex instructions

**Outcome.** Second client rollout.

- [ ] In the icodex worktree update docs/iwiki-mcp-modes.md, .codex-isolated/AGENTS.md, lib/iwiki/iwiki.sh, and the maintained context-awareness and task-ledger instructions. Use the same public contract and server capability prerequisite as step 15.
- [ ] Verify. Generate instructions in a disposable project. Assert the emitted task lookup explicitly uses all even when document status is stub; current queries can exclude both task tags. Check binding reuse, module wiring and the two runbook workflows.

```bash
bash tests/test_iwiki_agent_contract.sh
bash tests/test_iwiki_remote_scope.sh
bash tests/test_iwiki_call_economy.sh
```

## Step 17. Verify the integrated code and close the chain

**Outcome.** Final regression and outcome evidence.

- [ ] Run required client regression checks in each client worktree on its final fingerprint. For icodex run its documented full Bash test loop. For iclaude run the project-required suite from that checkout and record the resolved command and test count. In iwiki-mcp run the full suite once on its stable state using the disposable database and an environment meeting tests/deployment requirements. Extract database cases from that same JUnit report with the command below and apply the repository checker. Do not silently accept skipped acceptance cases. Recover a failed check with a focused test before repeating invalidated evidence.
- [ ] Verify. Record each final revision, command, test count, skip reason and observed MCP outcome. Update affected bound wiki pages, refresh or publish code graph under the project rules and record any unavailable resolution. Run lint after the final writes. Run check-chain result on this plan before branch finishing. No live grants, reclassification, migration or deployment is authorized. Check that every named lifecycle test from the focused runs appears and executes in the final report.

```bash
uv run --frozen pytest -q -ra --junitxml=reports/full.xml
python3 -c 'import xml.etree.ElementTree as E; root=E.parse("reports/full.xml"); pg=E.Element("testsuite"); pg.extend(t for t in root.iter("testcase") if t.get("classname","").startswith("tests.postgres.")); E.ElementTree(pg).write("reports/postgres-final.xml")'
uv run --frozen python scripts/check_postgres_junit.py reports/postgres-final.xml
```
