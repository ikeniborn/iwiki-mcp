"""Pure unit coverage for PostgresStore._vector_is_current — no DB dependency."""
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
