# PostgreSQL CI

## Required execution

The `PostgreSQL tests` workflow reports a check on every pull request, push to
`master`, and manual dispatch. A lightweight selection job tests and runs
`scripts/select_postgres_ci.py` before any database service starts. The
`PostgreSQL suite` job runs only when database coverage is selected. It uses
Python 3.12, PostgreSQL 16 with pgvector, and locked dependencies.

The final `PostgreSQL integration` check keeps its existing name. It succeeds
only when selection succeeded and either the selected database suite passed,
or selection explicitly found no database impact and the suite was skipped.
A selection failure or missing output fails this check. This is a job condition,
not workflow path filtering that can leave required checks pending.

## Change selection

The selector checks the complete PR diff from its merge base. For a master push,
it checks the entire `before` to `after` range. Renames are treated as deletion
plus addition. Manual runs always execute PostgreSQL. Missing comparison history
and unclassified changes also require PostgreSQL.

| Changed input | PostgreSQL required? |
|---|---|
| PostgreSQL implementation, migrations, auth, policy, storage | Yes |
| Shared server, HTTP, admin, engine, codegraph, specifications | Yes |
| PostgreSQL tests, common fixtures, test data and support modules | Yes |
| Eval runner, compatibility artifacts, CI workflow or scripts | Yes |
| Dependencies, lockfile dependencies, build or pytest configuration | Yes |
| Root Markdown and Markdown under `docs/` | No |
| The two agent Markdown snippets | No |
| Independent `test_*.py` files outside PostgreSQL and fixture directories | No |
| Only the literal `AUTHORING_RULES` text in `resources.py` | No |
| Only project version in `pyproject.toml`, local package version in `uv.lock`, or literal `__version__` | No |
| Unknown path or malformed metadata | Yes |

Text/version exceptions compare parsed contents before and after the change.
They do not exclude whole source or configuration files: executable changes to
`resources.py` or `__init__.py`, dependency changes, and test configuration changes
still require PostgreSQL. Existing imports of test helpers from the PostgreSQL
suite are checked by a selector regression test.

`tests/postgres` depends on common runtime code, not just `src/iwiki_mcp/postgres`.
For example, it imports the server, HTTP transport, codegraph publication, engine
configuration, `tests/codegraph/publication_contract_support.py`,
`tests/codegraph/synthetic_wiki.py`, and `eval/code_graph/runner.py`. Rollback tests
also consume `compat/` artifacts. These inputs retain database coverage.

A mixed diff requires PostgreSQL if any input requires it. Selection skips only
database integration; it does not claim that other project tests passed. The
lightweight selection tests always run without a database. Other local checks
remain required according to the changed behavior.

## Database execution

Checkout includes the full Git history because rollback compatibility tests
archive a pinned historical source commit. A shallow checkout cannot run them.

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
An authorized administrator can require `PostgreSQL integration` for merges into
`master`. Require this final check rather than the conditional `PostgreSQL suite`.
The final check rejects failed selection and missing selected coverage. Local
validation alone does not prove that GitHub executed the jobs.
