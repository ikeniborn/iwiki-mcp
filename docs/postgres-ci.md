# PostgreSQL CI

## Required execution

The `PostgreSQL tests` GitHub Actions workflow runs on pull requests, pushes to
`master`, and manual dispatch. Its `PostgreSQL integration` job uses Python 3.12
and a disposable PostgreSQL 16 service with pgvector. Dependencies come from
`uv.lock` through `uv sync --frozen --extra dev`.

The job creates the `vector` extension in `iwiki_test`, sets
`IWIKI_TEST_POSTGRES_DSN`, and runs every test in `tests/postgres`. It does not
use production credentials or connect to a production database. The service
password is a disposable CI fixture value.

Pytest failures fail the job. The JUnit gate also rejects a missing, malformed,
or empty report, a report without `tests.postgres` cases, and any skipped,
failed, or errored case. This prevents a successful pytest exit with skipped
database tests from being treated as PostgreSQL verification. No skip allowlist
is configured. The `postgres-junit` artifact is uploaded even after test failure.

Tests in `tests/deployment` require additional Docker and network topology and
are outside this job. This job is not a claim that the entire project suite ran.

## Local equivalent

Provision a disposable database using [PostgreSQL setup](postgres-setup.md).
Its name must end in `_test`, and the `vector` extension must exist. Set
`IWIKI_TEST_POSTGRES_DSN` to that database before running these commands.

```bash
uv sync --frozen --extra dev
uv run --frozen pytest -q tests/test_postgres_ci_report.py
uv run --frozen pytest -q -ra tests/postgres --junitxml=reports/postgres.xml
uv run --frozen python scripts/check_postgres_junit.py reports/postgres.xml
```

Without the DSN, database-dependent tests normally skip during local runs.
The report gate deliberately rejects that outcome. A supplied but unreachable
database or missing pgvector already fails the PostgreSQL fixture.

## Merge enforcement

The workflow defines a check; it does not change repository branch protection.
After the first successful GitHub run, an authorized administrator can require
`PostgreSQL integration` for merges into `master`. Local validation alone does
not prove that GitHub executed the job.
