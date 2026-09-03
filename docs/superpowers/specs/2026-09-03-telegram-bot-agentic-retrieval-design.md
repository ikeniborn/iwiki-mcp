---
review:
  spec_hash: edf17d653fd6c53b
  last_run: 2026-09-03
  phases:
    structure:
      status: passed
    coverage:
      status: passed
    clarity:
      status: passed
    consistency:
      status: passed
  findings: []
chain:
  intent: docs/superpowers/intents/2026-09-03-telegram-bot-agentic-retrieval-intent.md
---

# Design: telegram-bot-agentic-retrieval

**Date:** 2026-09-03
**Status:** draft
**Intent:** docs/superpowers/intents/2026-09-03-telegram-bot-agentic-retrieval-intent.md

## 1. Overview

Replace the Telegram bot's fixed single-pass RAG pipeline with an LLM-driven agentic
tool-use loop for every read path: text questions, transcribed voice questions, and the
retrieval that feeds `/create` drafts. The model drives `search_wiki` / `read_section`
tool calls through the standard OpenAI tool-calling protocol, selects and re-reads the
chunks it needs, and produces the final answer itself. A provider that does not support
tool calling is detected at startup (and demoted at runtime) and served by the current
single-pass pipeline unchanged. `/update` keeps its current behavior: the drafting
context is the target section.

Chosen approach: **native OpenAI tool calling with single-pass fallback** (approach A).
Text-based ReAct was rejected for parsing fragility under a trust-first priority; a
three-tier native→ReAct→single-pass ladder was rejected as speculative complexity —
the intent already fixes the fallback as the current pipeline.

## 2. Architecture

One new module, point changes elsewhere.

- **`src/iwiki_mcp/telegram_bot/agent.py`** (new): `AgentLoop` — owns the message
  transcript, the tool schemas, the tool dispatch, the iteration and budget limits, and
  the two system prompts (question answering, `/create` drafting). Depends only on the
  existing `RemoteIwikiClient` (search / read_page), `InferenceClient`, and
  `ContextBudget`. The domain is fixed by the bot when the loop starts; the model never
  chooses a domain and has no mutating tool.
- **`inference.py`**: adds `complete_with_tools(messages, tools, tool_choice)` — one
  `POST /chat/completions` carrying `tools`, returning either parsed `tool_calls` or a
  final `content` string. Extends `probe()`: after the existing `GET /models` check, one
  minimal completion with a declared tool and `max_tokens=1`; a provider refusal of the
  `tools` parameter sets `tools_supported = False` for the process lifetime. The
  existing `answer()` / `draft_markdown()` methods are untouched — they are the
  fallback.
- **`conversation.py`**: `_answer_selected` and `_draft_context` branch on
  `tools_supported`: supported → `AgentLoop.run(...)`; not supported → the current code
  path, unchanged. `propose_update` is untouched.
- **No new environment variables.** The tool-call limit is a module constant
  (`_MAX_TOOL_CALLS = 6`); the context budget is the existing process-wide
  `ContextBudget`.

## 3. Model tools

Two read-only tools, both scoped server-side to the selected domain:

- `search_wiki(query: str)` → list of `{slug, heading}` hits from
  `wiki_search(domains=[domain], k=config.search_k)`. Never sent with
  `intent="write"` (existing hard rule).
- `read_section(slug: str, heading: str, part: int = 0)` → the section body via
  `wiki_read_page`. `part=0` returns the section deterministically trimmed to its share
  of the remaining budget with the existing `select_context`/`_trim` machinery;
  `part=1..N` returns the full section in sequential untrimmed chunks for the case where
  the trim dropped something essential (the hybrid summarization decision — no separate
  summarization completions).

Repeated reads of the same `(slug, heading, part)` return a one-line
"already provided" marker instead of the text.

## 4. Loop mechanics

Transcript: `system` → `user` (question) → repeated `assistant(tool_calls)` /
`tool(results)` pairs → final `assistant(content)`.

- Each iteration is one `complete_with_tools` call. A response with `tool_calls`
  (parallel calls in one response are allowed) is executed and appended; a response with
  `content` is the final answer.
- **Termination is guaranteed** (intent hard constraint), by three rails:
  1. after `_MAX_TOOL_CALLS = 6` executed tool calls, the next completion is sent with
     `tool_choice: "none"` plus an appended instruction to answer now from the gathered
     context;
  2. when the transcript's character size exhausts the derived `ContextBudget.chars()`
     value, the same forced-answer completion is sent;
  3. a provider `context_overflow` drops the oldest half of the tool results (each
     replaced by a one-line `[dropped]` stub) and retries once; a second overflow
     returns the existing `Question context is too large. Ask a narrower question.`
- **Budget integration**: before every completion the transcript's character count is
  measured against the existing per-question derived budget; every tool result is
  trimmed on insertion to its share of the remaining budget. Ratio calibration from
  `usage.prompt_tokens` operates on every loop completion exactly as it does today.

## 5. Prompts

Two English system prompts, constants in `agent.py`.

Question answering — contents:
- Role: assistant answering questions from the `<domain>` wiki domain only.
- Rules: answer only from tool results; attribute statements as `page#heading`; if
  searching finds nothing relevant, say so — never invent.
- Strategy: start with a search; reformulate with narrower or different terms when hits
  are weak; read only the sections you need; request a `part` continuation only when
  the trimmed section visibly cut something essential.
- Limits: at most 6 tool calls; answer as soon as you have enough.
- Answer in the user's language.

`/create` drafting — the same search strategy and limits; the final message must be
only the Markdown page body. The preview → Confirm/Reject → `wiki_write_page` contract
is unchanged.

## 6. Capability detection and fallback

- **Startup**: `probe()` sends one tool-declaring completion with `max_tokens=1`. A
  tool-related refusal (HTTP 400 / unknown-parameter / provider wording naming `tools`)
  → `tools_supported = False`; the process serves every question through the current
  single-pass pipeline. Transient probe failures keep the existing retry/startup
  semantics and do not decide capability.
- **Runtime demotion**: a tool-related client error on a live agentic request demotes
  the process the same way, answers that request via the fallback path, and logs one
  WARNING (`operation=agent`, `outcome=demoted`). The user sees a normal answer, never
  an error.
- The fallback path is byte-for-byte the current code: `_collect_sections` →
  `select_context` → `answer()`.

## 7. Error handling

- `InferenceError` classification is unchanged: transient failures retry once after
  0.5 s inside `_post_json`; permanent failures keep the current user messages.
- A retryable `RemoteIwikiError` inside the loop propagates as today (session reconnect
  and update replay). A non-retryable one becomes a tool result telling the model the
  wiki is unavailable — it may answer from what it already gathered, otherwise the
  user receives the existing `Wiki service is unavailable.`
- Invalid tool arguments (missing/empty query, malformed slug or heading, unknown tool)
  become an error tool result for the model and count toward the 6-call limit.
- Privacy: the transcript lives only in the memory of the update being processed. Logs
  carry operation, outcome, elapsed time, usage numbers, prompt size in characters, and
  the iteration count — never message content, tool results, or prompts.

## 8. Progress feedback

The existing single status message is edited per iteration:
`⏳ Searching wiki (2/6)…`, `⏳ Reading section (3/6)…`, `⏳ Generating answer…` — the
counter is the executed tool-call count over the limit. The final answer replaces the
status message; one update still yields one message. The `Context: n of m sections used
in full.` line is not emitted on the agentic path: the model chooses what it reads, and
the absence of any truncation line remains the completeness signal.

## 9. Testing

Existing patterns: fake inference and fake remote clients, no network
(`tests/telegram_bot/`).

- `tests/telegram_bot/test_agent.py` (new), against a scripted fake provider:
  - search → read → answer happy path with attribution in the transcript;
  - the 6-call limit forces `tool_choice: "none"` and yields an answer;
  - duplicate `(slug, heading, part)` reads return the marker, not the body;
  - invalid tool arguments become error tool results and count toward the limit;
  - `context_overflow` drops the oldest results and retries once, second overflow
    returns the too-large message;
  - a mid-loop non-retryable `RemoteIwikiError` still produces an answer or the
    sanitized wiki-unavailable message.
- Capability: probe refusal → `tools_supported=False` → the question is served by the
  current pipeline (the untouched existing tests are the fallback regression net);
  runtime tool-related 400 → this request answered via fallback, the next request
  skips the loop entirely.
- `/create` through the loop keeps the preview/Confirm contract (existing confirmation
  tests must pass unchanged).
- Focused suite: `uv run pytest -q tests/telegram_bot` green.

## 10. Acceptance (from intent)

Desired Outcomes (verbatim):

- On a complex question the bot observably performs several search/read iterations
  (visible in the progress status message and operational logs), and the answer
  synthesizes several pages with `page#heading` attribution.
- On a benchmark set of real-domain questions the agentic mode answers more completely
  and accurately than the current single-pass pipeline (manual comparison).
- With a provider that does not support tool calling, the bot automatically falls back
  to the current single-pass pipeline with no user-visible error.

Done when (verbatim):

- (1) a complex benchmark question demonstrably triggers a multi-iteration loop and
  yields a fuller answer than the current pipeline on the same question;
- (2) the no-tool-calling fallback is demonstrated against a provider (or fake) without
  tool support with no user-visible error;
- (3) the focused Telegram test suite passes;
- (4) simple-question latency is not noticeably degraded.

Thresholds fixed by this design (closing the intent gate's two warnings):

- Benchmark rubric: five real-domain questions, judged on whether every relevant page
  is represented in the answer (completeness) and whether every claim traces to a
  cited `page#heading` (accuracy); agentic must not lose to single-pass on any of the
  five and must win on at least the two multi-page questions.
- Simple-question latency bound: a question answerable from one search must complete in
  at most 2 loop iterations, i.e. at most one completion more than the current
  pipeline.
