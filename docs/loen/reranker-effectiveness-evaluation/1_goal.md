# Goal

Topic: `reranker-effectiveness-evaluation`

## User Request

Evaluate the effectiveness of `lemonade-reranker-bge-reranker-v2-m3` using the
provided 32-document rerank request. Determine when reranking improves iwiki
retrieval, identify inefficient request or ranking choices, find the strongest
quality/latency approach, and apply only bounded, verified fixes.

## Objective

Produce reproducible evidence comparing the current RRF-only order and current
32-candidate rerank path with bounded alternatives for candidate count, query
form, document representation, and result cutoff. Select the Pareto-efficient
approach and apply it only when quality gates justify a product change.

## Success Criteria

- Preserve the supplied request as a sanitized fixture without credentials,
  private headers, or unrelated LiteLLM metadata.
- Add graded relevance judgments for the supplied query and a small stratified
  iwiki query set covering exact identifiers, semantic paraphrases, multi-intent
  queries, distractors, duplicate evidence, and long chunks.
- Record pre-rerank candidate order, reranker order and scores, request bytes,
  candidate count, response coverage, failure rate, and latency distribution.
- Compare no rerank, current 32-candidate rerank, and bounded variants for
  candidate count, query form, and document representation.
- Report Recall@k, MRR@k, nDCG@k, Precision@k, p50/p95 latency, payload bytes,
  timeout/error rate, and score separation.
- Select a Pareto-efficient approach. Auto-fix only when offline and live
  evidence passes all quality gates; otherwise produce a report with no product
  change.
- Leave full verifier evidence and a clear recommendation in this topic.

## Launch Policy

- Mode: `governance`
- Subtype: `auto-fix`
- Trigger: manual
- Owner: repository owner
- Auto-merge or release: forbidden
- First automated run: requires human review
