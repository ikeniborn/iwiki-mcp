# Result

Topic: `reranker-effectiveness-evaluation`

## Outcome

Complete. `lemonade-reranker-bge-reranker-v2-m3` is effective for the supplied
multi-intent case and should be retained.

Accepted request strategy:

- keep one natural-language query;
- keep plain document text without file/heading prefixes;
- keep the 32-candidate safety ceiling until a broader labeled set exists;
- submit the full hydrated candidate batch once;
- set provider `top_n` to requested final `k`;
- append every unscored, stale, or unhydrated candidate in preliminary order;
- keep fail-soft fallback and sanitized public metadata.

This strategy avoids the measured quality and latency regression from query
decomposition. It reduces provider response size but does not claim to reduce
the model's query-document scoring work.

Verification: 46 focused tests and 510 full tests passed; flake8, CLI help, and
wiki freshness checks passed.

## Deferred Follow-up

Lexical scoring currently matches complete H2 sections and maps each lexical
hit to `chunk=0`. Aligning lexical scoring with indexed section chunks is a
separate task. This loop intentionally keeps chunk size `512`, overlap `64`,
and plain reranker document representation unchanged.

## Evidence Files

- `evidence/live-matrix.json`
- `evidence/latest-test.json`
- `evidence/latest-test.log`
- `evidence/latest-full-test.log`
- `evidence/latest-lint.log`
