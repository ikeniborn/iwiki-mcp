# Code Graph Benchmark Method Evidence

## Purpose

This benchmark is an offline release gate for the Python code graph. It keeps
search latency evidence separate from production indexing, storage, quality,
determinism, context, startup, and memory evidence. The runner uses production
query, discovery, parser, resolver, indexer, store, publication, and context
paths without FTS, SQLite UDFs, projection tables, result caches, or candidate
caps.

## Reproduction

Run from the repository root:

```bash
uv run pytest -q tests/eval/test_code_graph_runner.py tests/eval/test_code_graph_report.py
uv run python -m eval.code_graph --fixture-root tests/fixtures/codegraph --output /tmp/iwiki-code-graph-evidence
```

The second command writes these files before returning success or raising a
blocking gate error:

```text
/tmp/iwiki-code-graph-evidence/code-graph-benchmark.json
/tmp/iwiki-code-graph-evidence/code-graph-benchmark.md
```

## Corpus ownership

The search corpus is a deterministic schema-v2 SQLite corpus containing
100,008 file, module, and symbol entities. Its fixed strata cover ASCII names,
Unicode names and signatures, shared Unicode path prefixes, duplicate dotted
modules, repeated aliases, and ambiguous aliases. It owns only nine-rank search
truth and latency evidence.

The production corpus is a generated Python source tree processed through real
source discovery, parsing, resolution, indexing, storage, and publication. Its
fixed quality sources contain independent declaration, method, import, static
call, unresolved-call, and Unicode truth. Quality is read from the published
canonical database, not from parser output or a separate fixture corpus. A
1,000-file corpus owns build, no-op, forced rebuild, context, database/source,
quality, and deterministic rebuild evidence. A separate 10,000-file production
tree owns peak-memory evidence.

The combined quality section has explicit provenance by metric group.
Declarations, methods, local imports, static calls, false resolved calls, and
deterministic rebuild belong to the canonical production database. Duplicate
modules, repeated aliases, ambiguous aliases, and Unicode search correctness
belong to the separate schema-v2 search database. Both corpus hashes and the
production revision are recorded in JSON and Markdown.

Accepted paths, bytes, and corpus hash come from production discovery. After
publication, canonical `files` paths and sizes must match that discovery result
exactly. The database/source gate uses the reconciled canonical byte total.

The database/source numerator is the canonical main SQLite file after a
benchmark-owned `PRAGMA wal_checkpoint(TRUNCATE)` and connection close. WAL,
SHM, and search-corpus bytes are excluded.

## Search timing and truth

Each search request is validated before timing. On one connection and snapshot,
each rank records one cold query, one untimed warm-up, and exactly ten warm
queries. The report publishes the warm median, nearest-rank p95, maximum, and
all ten samples. The strict first-release gate is warm maximum `<500 ms` for
every case. Each case also reports the strict `<150 ms` post-v1 target; that
comparison is non-blocking.

Expected ranks and result IDs are fixed independently of query output. Unicode
truth uses Python casefold without NFC or NFKC and expects the complete token
key `U+001F pkg U+001F strasse U+001F`. Canonical lexical truth persists the
complete tokens for the independent query `some token target`.

## Determinism

Two forced production builds compare revision, exact file/module/symbol/
relation/link ID sets, and canonical semantic rows. The semantic comparison
excludes only:

- `repositories.indexed_at`
- `repositories.state`
- `metadata.phase_timings`
- `metadata.transient_diagnostics`

Raw SQLite bytes are not a determinism requirement.

## Authoritative result

The 2026-08-13 authoritative run used CPython 3.11.13, SQLite 3.49.1,
tree-sitter-python 0.25.0, normalizer `casefold-token-v1`, and Unicode data
14.0.0 on Linux x86-64 with 16 reported CPUs. It passed every blocking gate.

| Evidence | Observed | Gate |
|---|---:|---:|
| Startup | 69.021063 ms | `<100 ms` |
| No-op | 39.251567 ms | `<200 ms` |
| 1,000-file build | 642.505562 ms | `<15,000 ms` |
| Search warm maximum | 270.311820 ms | `<500 ms` |
| Context | 0.099847 ms | `<300 ms` |
| Database/source | 1.022533x | `<3x` |
| 10,000-file peak memory | 49,192,510 bytes | `<1 GiB` |

Declarations, methods, local imports, static calls, duplicate modules, repeated
aliases, ambiguous aliases, Unicode truth, and deterministic rebuild measured
1.0. False resolved calls measured 0.0. Signature and path search missed only
the non-blocking post-v1 `<150 ms` target.

Constraint evidence inspects actual schema tables and explicit indexes, compares
SQLite function signatures before and after search-corpus setup, and inspects
generated production SQL for each rank. The run found no added SQLite function,
unapproved explicit index, FTS table, search projection, or extra rank-local
`LIMIT`; every rank contained only its final public-limit clause.

Evidence file hashes for that run:

```text
fd2bec377508aa0c181bb7c0e066e0ff61f3d6751ace586165f843858fe734e8  code-graph-benchmark.json
e0c658da3927cf923bab8c671c35b83ebf9bb30dcff5458bf00fec658e13aff2  code-graph-benchmark.md
```

## Stop behavior

Any blocking threshold miss, incorrect expected rank or result ID, contradictory
quality result, or determinism mismatch writes both reports and exits nonzero.
Release work then stops before version, Wiki, staging, or commit. Missing only
the reported `<150 ms` post-v1 target does not change the first-release verdict.
