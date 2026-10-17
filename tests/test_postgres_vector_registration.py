"""Repeated store checkout must not accumulate pgvector adapter classes."""
from contextlib import contextmanager
from types import SimpleNamespace

import numpy as np
import psycopg
from psycopg.adapt import AdaptersMap, PyFormat
from psycopg.pq import Format
from psycopg.types import TypeInfo
import pytest

from iwiki_mcp.postgres.store import PostgresStore


def _connection():
    return SimpleNamespace(adapters=AdaptersMap(psycopg.adapters))


def _store(connection):
    @contextmanager
    def checkout():
        yield connection

    return PostgresStore("unused", "fixture", None, connection_factory=checkout)


@pytest.fixture
def vector_types(monkeypatch):
    infos = {
        name: TypeInfo(name, 20000 + index * 2, 20001 + index * 2)
        for index, name in enumerate(("vector", "bit", "halfvec", "sparsevec"))
    }
    monkeypatch.setattr(
        TypeInfo, "fetch", classmethod(lambda cls, connection, name: infos[name])
    )
    return infos


def test_repeated_checkout_reuses_adapters_across_store_instances(vector_types):
    connection = _connection()
    with _store(connection)._connect():
        original = connection.adapters.get_dumper_by_oid(20000, Format.BINARY)

    for _ in range(100):
        with _store(connection)._connect():
            assert connection.adapters.get_dumper_by_oid(20000, Format.BINARY) is original


def test_each_physical_connection_gets_its_database_vector_oid(vector_types):
    first, second = _connection(), _connection()
    with _store(first)._connect():
        assert first.adapters.get_dumper(np.ndarray, PyFormat.BINARY).oid == 20000
    vector_types["vector"] = TypeInfo("vector", 30000, 30001)
    with _store(second)._connect():
        assert second.adapters.get_dumper(np.ndarray, PyFormat.BINARY).oid == 30000


def test_registration_failure_is_retried_on_next_checkout(vector_types, monkeypatch):
    from iwiki_mcp.postgres import store

    connection = _connection()
    register = store.register_vector

    def fail_registration(connection):
        raise psycopg.ProgrammingError("registration failed")

    monkeypatch.setattr(store, "register_vector", fail_registration)
    with pytest.raises(psycopg.ProgrammingError, match="registration failed"):
        with _store(connection)._connect():
            pytest.fail("failed registration must not yield the connection")

    monkeypatch.setattr(store, "register_vector", register)
    with _store(connection)._connect():
        assert connection.adapters.get_dumper(np.ndarray, PyFormat.BINARY).oid == 20000
