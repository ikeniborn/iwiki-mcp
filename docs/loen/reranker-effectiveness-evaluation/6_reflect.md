# Reflect

Topic: `reranker-effectiveness-evaluation`

## Decision

Keep.

## Reason

The model is effective for the supplied multi-intent case: every measured
variant achieved intent recall@8 of `1.0`, and the best variant improved
nDCG@8 from `0.648482` to `0.670004`. Pool 16 was faster and better for this
seed, but relevant secondary-intent candidates occur below rank 16, so a global
pool reduction would trade recall safety on one labeled query.

Document context prefixes and three-query decomposition did not help. The
decomposed approach was slower and reduced nDCG. Provider `top_n` changes only
returned rows for the same submitted documents; therefore it is a safe response
boundary optimization, not an inference pair-count optimization.

The bounded fix is accepted because focused and full verification pass, partial
provider output preserves all remaining preliminary candidates, and public
rerank metadata remains unchanged.

## Next Step

Keep the current model and 32-candidate ceiling. For broader tuning, build a
multi-query labeled set and optimize pool size against nDCG, intent recall,
p95 latency, and request tokens. Do not adopt pool 16 globally from this single
case.
