# Hosted publication quota verification

## Scope

The baseline is 0.7.325 at b6e2c23. The candidate runtime is 10f98af. Both ran on the same host with PostgreSQL 16 and pgvector. Each trial initialized a fresh database schema and used the existing ranked snapshot fixture. Each publisher submitted 11,416 canonical bytes in seven batches through the authenticated HTTP MCP interface. Concurrent publishers used separate tokens and domains. This fixture measures request overhead. It supplies no production throughput estimate.

## Measurements

Five interleaved trials per revision and workload produced the following results. Median publication time covers begin through the finalize response. The p95 uses the nearest rank over all successful batch acknowledgements for that workload. All publications finished without a busy response.

| Publishers | Samples per revision (publishes / batches) | Median publication, baseline → candidate | p95 acknowledgement, baseline → candidate |
|---|---:|---:|---:|
| One | 5 / 35 | 220.25 → 151.13 ms (−31%) | 28.11 → 16.92 ms (−40%) |
| Two | 10 / 70 | 370.35 → 228.47 ms (−38%) | 47.17 → 27.02 ms (−43%) |

The first implementation exceeded the 15% threshold in both workloads. It opened publication connections and revalidated the role on each request. The candidate pools publication connections, verifies the credential pair at startup, and checks token and domain authority in each transaction. The table reports the measurements after these changes.

A separate single-publisher trial measured peak traced Python heap during finalize, including the in-process client and server. The baseline used 203,146 bytes and the candidate used 202,423 bytes. The measurement excludes PostgreSQL memory, native allocations and process RSS.

A PostgreSQL query probe added 20,000 synthetic zero-charge historical rows in a disposable transaction. With three retained positive balances, the byte aggregate used an index-only scan on publication_usage_owner_idx. The slot aggregate used publication_usage_live_idx. Across 100 warm local queries the median slot/byte round trips were 39.4/37.4 µs before adding the rows and 36.8/36.4 µs afterwards.

## Reproduction

Use independent checkouts of the two revisions and a disposable database ending in _test with pgvector enabled. Copy eval/code_graph/publication_quota_benchmark.py from the candidate into tests/postgres/ in each checkout. Run the following command from each checkout with its dependencies installed. Set BENCH_LABEL to the revision and BENCH_FILE to the same absolute JSONL output path. Set BENCH_PUBLISHERS to 1 or 2. Alternate the starting revision on successive rounds. Run the single-publisher heap trial separately with BENCH_HEAP=1.

```bash
uv run pytest -q tests/postgres/publication_quota_benchmark.py
```

## Rollback rehearsal

A disposable installation of the baseline accepted a recoverable pending batch and activated a separate snapshot. A PostgreSQL custom-format backup restored successfully into a second database at schema 10 with all five staged receipts. The candidate upgraded the original database, accepted a second pending batch and completed another publication. After rollback the baseline binary inspected both staging receipts and both terminal results, and read the latest active revision. Reapplying schema 11 preserved the direct login's attribution for its post-upgrade session. The regression tests also exercise identifier reuse while schema 10 is active.

## Integration checks

The final runtime commit is 82ba5d4. The import-only correction loads publication role checks eagerly when the server starts. Both disposable PostgreSQL endpoints had pgvector enabled. The general run below passed 4,252 tests and 31 subtests. The account lacked permission to change file ownership. That skipped test passed separately in a read-only disposable container with CAP_CHOWN. The generated 20,000-file test passed separately in 2,789.28 seconds. All 4,254 collected tests passed across the three runs. PostgreSQL 17 also passed 85 focused quota, publication and resume tests.

```bash
uv run pytest -q -rs --deselect=tests/eval/test_code_graph_publication_scale.py::test_publication_supports_twenty_thousand_files
uv run pytest -q tests/eval/test_code_graph_publication_scale.py::test_publication_supports_twenty_thousand_files
uv run flake8 src tests eval/code_graph/publication_quota_benchmark.py
uv run python -m compileall -q src/iwiki_mcp eval/code_graph/publication_quota_benchmark.py
git diff --check b6e2c23..HEAD
```

Set IWIKI_TEST_POSTGRES_DSN and IWIKI_TEST_POSTGRES_LOOPBACK_DSN to distinct disposable endpoints before running the general suite. The benchmark harness also uses IWIKI_TEST_POSTGRES_DSN.
