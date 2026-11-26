# PostgreSQL finalize measurements

On 2026-11-26, five paired runs compared master at `c207c09` (0.7.335) with the integrated implementation on the same base (later released as 0.7.337). Each run published the same local export through hosted MCP into a disposable PostgreSQL 16.15 database. The fixture contained one repository, 136 files, 2,440 symbols and 23,137 relations. Eight transport batches carried 17,751,627 canonical bytes.

The order alternated by pair. The harness observed SQL executions without modifying their payloads. It sampled resident memory of the specific PostgreSQL backend and the hosted Python process from Linux procfs at a nominal 2 ms interval. Time covers the finalize transaction through commit. Samples can miss short peaks.

| Median over five runs | Master | Bounded inserts |
|---|---:|---:|
| PostgreSQL backend peak RSS (MiB) | 302.75 | 97.16 |
| PostgreSQL RSS increase over transaction entry (MiB) | 261.22 | 55.02 |
| Hosted process peak RSS (MiB) | 274.59 | 242.52 |
| Transaction duration (s) | 1.978 | 1.807 |

| Pair | Master backend peak (MiB) | Bounded backend peak (MiB) | Master time (s) | Bounded time (s) |
|---|---:|---:|---:|---:|
| 1 | 302.05 | 96.46 | 2.185 | 1.868 |
| 2 | 206.64 | 96.87 | 1.978 | 1.801 |
| 3 | 302.75 | 97.16 | 2.073 | 1.843 |
| 4 | 303.64 | 97.43 | 1.964 | 1.787 |
| 5 | 303.74 | 97.46 | 1.972 | 1.807 |

Master issued one insert of 23,137 relations. The integrated implementation issued 23 inserts of 1,000 and one of 137. Each run compared all persisted file, symbol and relation payloads with the canonical input. All ten runs returned the same reader and publisher revision, `sha256:a1836655b91ddb5c6ef11557d1c3c7caf4351fe6cbfc379992f19f869186037d`.

On this fixture, median database backend peak RSS fell by 67.9% and median transaction duration fell by 8.6%. These figures describe a single publication on the measured host. File and symbol inserts and in-process snapshot materialization still scale with their input.
