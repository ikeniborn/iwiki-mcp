---
review:
  spec_hash: 635f3b55e7eaa2f1
  last_run: 2026-07-09
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-07-08-wiki-auto-remediation-intent.md
---
# Design: wiki auto remediation planning

## 1. Summary
Add one read-only MCP planning tool, `wiki_remediation_plan(domain: str | None = None)`, that turns existing `wiki_lint` freshness findings into structured remediation work for coding agents. The tool does not write pages, update logs, delete files, commit, or re-index. It prepares enough context for Claude Code, Codex, or another MCP client agent to regenerate stale wiki content and then apply changes through the existing write/update/delete/index tools.

The design intentionally avoids a second apply/update API. `wiki_update_page`, `wiki_delete_page`, `wiki_write_page`, `wiki_index`, and `wiki_lint` remain the mutating and verification surface for the remediation workflow.

## 2. Acceptance (from intent)
Desired outcomes:
- A maintenance planning tool returns an explicit list of update candidates and delete candidates derived from ingest history.
- The planning result gives the calling agent the source content, current wiki page, authoring rules, and action metadata needed to generate updated wiki markdown for each `stale` candidate.
- The calling agent updates stale pages and deletes missing-source pages through existing tools (`wiki_update_page`, `wiki_delete_page`, `wiki_write_page`, and `wiki_index`) without asking the user at every step.
- The final response reports which pages were updated, which pages were deleted, which actions failed, and which lint findings remain after the run.
- User feedback or a request to restore old content can happen after the report, but it is not an approval gate for the maintenance workflow.

Done when: one agent-orchestrated maintenance workflow has planned every lint-backed update/delete candidate, applied every safe action it can handle through existing tools, re-indexed the affected domain, re-run lint, and returned a report containing `planned`, `updated`, `deleted`, `failed`, and `remaining_lint` sections.

## 3. Requirements
R1. `wiki_remediation_plan(domain=None)` resolves the target domain to the current project's bound write domain when `domain` is omitted.

R2. `wiki_remediation_plan(domain=...)` rejects any domain that is not the current project's bound write domain.

R3. The tool calls the existing lint engine for the target domain and returns the raw lint report under `lint`.

R4. The tool converts lint `stale` findings into `update_candidates`.

R5. The tool converts lint `missing_source` findings into `delete_candidates`.

R6. Each update candidate includes `domain`, `slug`, `page`, `source`, `current_markdown`, `source_content`, `current_headings`, `source_bytes`, `source_truncated`, and `recommended_tools`.

R7. Each delete candidate includes `domain`, `slug`, `page`, `source`, and `recommended_tools`.

R8. Unreadable or oversized stale sources do not fail the whole plan. They appear in `blocked_candidates` with a reason.

R9. The planning tool respects the project's `.iwikiignore`: if a stale source now matches ignore rules, the tool must not return `source_content` for that source and must place the item in `blocked_candidates`.

R10. The planning tool returns `AUTHORING_RULES` so the agent can generate valid wiki markdown without making an additional resource call.

R11. The planning tool returns `next_steps` that tell the agent to use existing tools: update compatible sections with `wiki_update_page`, replace changed full-page structures with `wiki_delete_page` plus `wiki_write_page`, delete missing-source pages with `wiki_delete_page`, then run `wiki_lint`.

R12. The planning tool never writes page files, modifies ingest logs, deletes pages, creates commits, calls embedding APIs, or re-indexes.

R13. Existing public contracts for `wiki_lint`, `wiki_update_page`, `wiki_delete_page`, `wiki_write_page`, and `wiki_index` remain unchanged.

## 4. API Shape
`wiki_remediation_plan(domain: str | None = None) -> dict`

Successful response:
```json
{
  "domain": "iwiki-mcp",
  "lint": { "wiki_present": true, "stale": [], "missing_source": [] },
  "update_candidates": [
    {
      "domain": "iwiki-mcp",
      "slug": "mcp-server",
      "page": "/base/iwiki-mcp/mcp-server.md",
      "source": "/project/src/iwiki_mcp/server.py",
      "current_markdown": "# MCP server...",
      "source_content": "...",
      "source_bytes": 12345,
      "source_truncated": false,
      "current_headings": ["Overview", "Tool surface"],
      "recommended_tools": ["wiki_update_page", "wiki_delete_page", "wiki_write_page", "wiki_lint"]
    }
  ],
  "delete_candidates": [
    {
      "domain": "iwiki-mcp",
      "slug": "old-page",
      "page": "/base/iwiki-mcp/old-page.md",
      "source": "/project/deleted.py",
      "recommended_tools": ["wiki_delete_page", "wiki_lint"]
    }
  ],
  "blocked_candidates": [],
  "authoring_rules": "...",
  "next_steps": [
    "Regenerate stale wiki markdown from source semantics.",
    "Use wiki_update_page for compatible section-body edits.",
    "Use wiki_delete_page then wiki_write_page when the article structure must change.",
    "Use wiki_delete_page for missing_source delete candidates.",
    "Run wiki_lint and report planned, updated, deleted, failed, and remaining_lint."
  ]
}
```

Fail-soft errors follow the existing server pattern and return `{ "error": "...", "hint": "..." }`.

## 5. Component Design
`server.py` owns the MCP function and registration, matching the existing pattern where raw functions stay unit-testable and are then registered via `mcp.tool()`.

`engine.lint.lint` remains the source of health findings. No lint behavior changes are required.

The plan tool needs small helper logic for:
- converting an absolute page path under the domain directory into a slug;
- reading current wiki markdown fail-soft;
- reading source content with a response size cap;
- extracting current `##` headings for agent guidance.
- checking `.iwikiignore` before exposing source content.

These helpers should stay private to `server.py` unless implementation pressure shows they belong in `engine/`.

## 6. Data Flow
1. Agent calls `wiki_remediation_plan`.
2. Server resolves binding and validates that the target is the current project's write domain.
3. Server runs lint for that domain.
4. Server enriches `stale` findings into update candidates by reading the current wiki page and live source content.
5. Server enriches `missing_source` findings into delete candidates.
6. Agent regenerates wiki markdown using the returned source, current page, headings, and authoring rules.
7. Agent applies changes through existing tools.
8. Agent runs `wiki_lint`.
9. Agent reports `planned`, `updated`, `deleted`, `failed`, and `remaining_lint`.

## 7. Error Handling
No bound write domain returns an error and hint to bind a project write domain.

A requested domain that differs from the bound write domain returns an error and does not inspect or expose that domain's content.

Unreadable source content for a stale page creates a blocked candidate with `reason: "source_unreadable"`.

Unreadable current page content creates a blocked candidate with `reason: "page_unreadable"`.

Ignored source content creates a blocked candidate with `reason: "source_ignored"` and does not include source bytes.

Source content larger than the response cap is truncated in the candidate, with `source_truncated: true` and the full `source_bytes` count. Truncation does not block the candidate because the local agent may still read the full source from disk when needed.

Absent or empty wiki lint reports return an empty plan instead of an error.

## 8. Source Size Cap
Use a conservative constant such as `SOURCE_CONTENT_MAX_BYTES = 200_000` for MCP response safety. The exact value is an implementation detail, but tests must prove truncation is explicit and non-fatal.

## 9. Testing
Focused tests should cover:
- empty lint result returns empty candidates;
- stale finding produces an update candidate with slug, source, current markdown, source content, headings, and recommended tools;
- missing-source finding produces a delete candidate;
- unreadable source produces a blocked candidate without failing the whole plan;
- ignored source produces a blocked candidate without source content;
- oversized source is truncated and marked with `source_truncated: true`;
- requested non-write domain is rejected;
- planning does not modify page files, logs, indexes, or commits.

Existing tests for `wiki_lint`, `wiki_update_page`, `wiki_delete_page`, `wiki_write_page`, and `wiki_index` should remain valid without behavior changes.

## 10. Documentation
Update the wiki/tool-surface documentation after implementation to describe `wiki_remediation_plan` as a read-only planning helper. The docs must state that mutation still happens through existing write/update/delete/index tools and that the calling agent performs semantic regeneration.

## 11. Out of Scope
No server-side chat or LLM client is added.

No `wiki_remediation_apply` tool is added.

No changes are made to `wiki_update_page` section semantics.

No automatic user approval gate is introduced after planning.
