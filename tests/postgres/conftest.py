"""Fixtures for explicitly provisioned PostgreSQL integration tests."""
from __future__ import annotations

import os

import pytest


class SecretDsn(str):
    """Connection string whose pytest representation never reveals credentials."""

    def __repr__(self):
        return "<redacted PostgreSQL test DSN>"


def validated_test_dsn(value: str) -> SecretDsn:
    """Validate the disposable target before any network operation."""
    from psycopg.conninfo import conninfo_to_dict

    database = conninfo_to_dict(value).get("dbname", "")
    if not database.endswith("_test"):
        raise ValueError("IWIKI_TEST_POSTGRES_DSN database must end in _test")
    return SecretDsn(value)


@pytest.fixture(scope="session")
def postgres_dsn():
    raw_dsn = os.environ.get("IWIKI_TEST_POSTGRES_DSN", "").strip()
    if not raw_dsn:
        pytest.skip("IWIKI_TEST_POSTGRES_DSN is not set")

    import psycopg

    try:
        dsn = validated_test_dsn(raw_dsn)
    except (ValueError, psycopg.ProgrammingError) as exc:
        pytest.fail(str(exc))

    try:
        with psycopg.connect(dsn, connect_timeout=10) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
                    ")"
                )
                vector_enabled = cursor.fetchone()[0]
    except psycopg.Error as exc:
        pytest.fail(
            f"disposable PostgreSQL database is unavailable: {type(exc).__name__}"
        )
    if not vector_enabled:
        pytest.fail("disposable PostgreSQL database must enable pgvector")
    return dsn


@pytest.fixture
def clean_postgres(postgres_dsn):
    import psycopg

    with psycopg.connect(postgres_dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS iwiki CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS iwiki_test_probe CASCADE")
    yield postgres_dsn
    with psycopg.connect(postgres_dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS iwiki CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS iwiki_test_probe CASCADE")
