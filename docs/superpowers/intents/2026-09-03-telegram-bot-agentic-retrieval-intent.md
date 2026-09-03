---
review:
  intent_hash: 99fc2e63c324a3c3
  last_run: 2026-09-03
  phases:
    structure: passed
    completeness: passed
    clarity: passed
    consistency: passed
    alignment: passed
  findings:
    - id: F-001
      phase: clarity
      severity: WARNING
      section: Desired Outcomes
      section_hash: a87313049d2d1ef6
      fragment: "answers more completely and accurately than the current single-pass pipeline (manual comparison)"
      text: "No explicit criterion for 'better' on the benchmark set"
      fix: "Fix the benchmark question list and a simple comparison rubric before recording implementation evidence"
      verdict: open
      verdict_at: null
    - id: F-002
      phase: clarity
      severity: WARNING
      section: Health Metrics
      section_hash: d04b1dab97ddb9ee
      fragment: "must not become noticeably slower"
      text: "'noticeably' has no threshold"
      fix: "Anchor to an observable bound, e.g. a simple question costs at most one extra completion round trip"
      verdict: open
      verdict_at: null
---

# Intent: telegram-bot-agentic-retrieval

**Date:** 2026-09-03
**Status:** approved

## Objective

The Telegram wiki bot answers every question through a fixed single-pass RAG pipeline:
one `wiki_search` with the raw user question, section reads for the returned hits, a
word-overlap trim, and one stateless completion with a two-line system prompt. That
pipeline produces weak or incomplete answers for multi-step questions — comparisons,
"how do I configure X for Y", anything that needs a reformulated follow-up search or a
synthesis across several pages. Replace it with an LLM-driven agentic tool-use loop
(search, section read, chunk selection, summarization) so answer quality and coverage of
complex questions approach what iclaude/icodex achieve against the iwiki MCP server.

## Desired Outcomes

- On a complex question the bot observably performs several search/read iterations
  (visible in the progress status message and operational logs), and the answer
  synthesizes several pages with `page#heading` attribution.
- On a benchmark set of real-domain questions the agentic mode answers more completely
  and accurately than the current single-pass pipeline (manual comparison).
- With a provider that does not support tool calling, the bot automatically falls back
  to the current single-pass pipeline with no user-visible error.

## Health Metrics

- Privacy: prompts, wiki context, answers, transcriptions never logged, never persisted
  beyond processing (unchanged from today).
- Context-window discipline: the derived budget, tokens-per-character calibration from
  `usage.prompt_tokens`, and the `IWIKI_BOT_CONTEXT_BUDGET_CHARS` hard ceiling keep
  holding for every completion the loop issues.
- Write safety: preview-confirm flow, single-use confirmation tokens, and
  revision/section-hash compare-and-swap remain unchanged.
- Liveness: heartbeat/typing refresh keeps the container healthy during long loops.
- Error sanitization: dependency failures keep returning sanitized messages.
- Latency of simple questions: a question answerable from one search must not become
  noticeably slower (the model may answer without extra iterations).

## Strategic Context

- Interacts with: remote iwiki MCP (`wiki_bind`, `wiki_search`, `wiki_read_page`,
  `wiki_write_page`, `wiki_update_page`), the OpenAI-compatible inference provider
  (vLLM/llama.cpp class), the Telegram transport behind the mandatory HTTPS CONNECT
  proxy, Supervisor health/liveness, and the employees using the bot.
- Priority trade-off: **trust** — answer fidelity (wiki-only, attributed) and safety
  outrank speed and inference cost.

## Constraints

### Steering (behavioral guidance)

- Reuse the existing `RemoteIwikiClient` and `ContextBudget` machinery rather than
  building parallel clients or budgets.
- Add the minimum of new environment variables; prefer derived behavior over knobs.

### Hard (architectural enforcement)

- Every existing invariant stays: privacy (no prompt/context logging or persistence),
  bind-to-server's-own-answer scope selection, never `wiki_search(intent="write")`,
  preview-confirm + compare-and-swap for every mutation, environment-only configuration,
  `trust_env=False` HTTP clients.
- The agentic loop gets read-only tools (search, section read) — never a mutation tool.
- The loop runs under a hard tool-call limit and the existing context/output budgets;
  it always terminates with an answer or an honest refusal.
- A provider without tool calling falls back to the current single-pass pipeline.

## Autonomy Zones

- Full autonomy (reversible, low risk): loop internals, prompts, chunk
  selection/summarization strategy, tests, new environment variables, status-message
  wording, write-path context assembly (drafting context may use the loop; the
  confirm/CAS write flow itself is untouched), dependency choices within the existing
  stack.
- Guarded (log + confidence threshold): none.
- Proposal-first (needs approval): none.
- No autonomy (human only): weakening the privacy guarantees or the trust model
  (allowlist, token scope, bind discipline, mutation confirmation).

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions
> is marked HUMAN CHECKPOINT in the plan.

## Stop Rules

- Halt if: an implementation step would require weakening a privacy or trust-model
  invariant to proceed.
- Escalate if: the configured provider rejects tool calling and the single-pass
  fallback cannot preserve current behavior.
- Done when: (1) a complex benchmark question demonstrably triggers a multi-iteration
  loop and yields a fuller answer than the current pipeline on the same question;
  (2) the no-tool-calling fallback is demonstrated against a provider (or fake) without
  tool support with no user-visible error; (3) the focused Telegram test suite passes;
  (4) simple-question latency is not noticeably degraded.
