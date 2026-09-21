"""A startup failure must name its own cause, and never a credential.

These run without a database: the connection is replaced, because what is
under test is which message the caller gets, not what PostgreSQL does.
"""
import psycopg
import pytest

from iwiki_mcp.postgres.migrations import MigrationError, require_schema_version


DSN = "postgresql://iwiki_svc:s3cret@db.invalid:5432/iwiki?sslmode=disable"


def test_unreachable_server_is_not_reported_as_a_schema_mismatch(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg, "connect", refuse)

    with pytest.raises(MigrationError) as raised:
        require_schema_version(DSN, expected_version=8)

    message = str(raised.value)
    assert "cannot read the schema version" in message
    assert "OperationalError" in message
    assert "db.invalid:5432/iwiki" in message
    assert "version 8 is required" not in message


def test_version_mismatch_reports_the_version_it_found(monkeypatch):
    monkeypatch.setattr(psycopg, "connect", _connection_returning(7))

    with pytest.raises(MigrationError) as raised:
        require_schema_version(DSN, expected_version=8)

    message = str(raised.value)
    assert "version 8 is required" in message
    assert "reports version 7" in message


def test_matching_version_raises_nothing(monkeypatch):
    monkeypatch.setattr(psycopg, "connect", _connection_returning(8))

    require_schema_version(DSN, expected_version=8)


@pytest.mark.parametrize(
    "failure",
    [psycopg.OperationalError("refused"), psycopg.ProgrammingError("no schema")],
)
def test_no_failure_message_carries_the_password(monkeypatch, failure):
    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(psycopg, "connect", fail)

    with pytest.raises(MigrationError) as raised:
        require_schema_version(DSN, expected_version=8)

    assert "s3cret" not in str(raised.value)
    assert "postgresql://" not in str(raised.value)


def test_an_unparseable_dsn_still_produces_a_message(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg, "connect", refuse)

    with pytest.raises(MigrationError) as raised:
        require_schema_version("not a dsn at all", expected_version=8)

    assert "the configured PostgreSQL server" in str(raised.value)


class _Cursor:
    def __init__(self, version):
        self._version = version

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, *_args, **_kwargs):
        return None

    def fetchone(self):
        return (self._version,)


class _Connection:
    def __init__(self, version):
        self._version = version

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def cursor(self):
        return _Cursor(self._version)


def _connection_returning(version):
    def connect(*_args, **_kwargs):
        return _Connection(version)

    return connect
