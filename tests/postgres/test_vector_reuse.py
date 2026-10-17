"""Vector reuse and pooled adapter registration coverage."""
from __future__ import annotations

from iwiki_mcp.postgres.store import PostgresStore


def test_vector_is_current_same_hash_and_dim():
    assert PostgresStore._vector_is_current("abc", 3, "abc", 3) is True


def test_vector_is_current_different_hash():
    assert PostgresStore._vector_is_current("abc", 3, "def", 3) is False


def test_vector_is_current_different_dim():
    assert PostgresStore._vector_is_current("abc", 3, "abc", 4) is False


def test_vector_is_current_different_hash_and_dim():
    assert PostgresStore._vector_is_current("abc", 3, "def", 4) is False


def test_pooled_checkouts_reuse_working_vector_adapters(postgres_dsn):
    import numpy as np
    from psycopg.pq import Format
    from psycopg.types import TypeInfo
    from psycopg_pool import ConnectionPool

    with ConnectionPool(postgres_dsn, min_size=1, max_size=1) as pool:
        with pool.connection() as connection:
            oid = TypeInfo.fetch(connection, "vector").oid
        original = None
        for _ in range(30):
            store = PostgresStore(
                postgres_dsn, "fixture", None, connection_factory=pool.connection
            )
            with store._connect() as connection:
                dumper = connection.adapters.get_dumper_by_oid(oid, Format.BINARY)
                if original is None:
                    original = dumper
                assert dumper is original
                assert connection.execute(
                    "SELECT %s::vector = '[1,2,3]'::vector",
                    (np.array([1, 2, 3], dtype=np.float32),),
                ).fetchone() == (True,)
