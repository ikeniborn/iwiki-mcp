"""PostgreSQL migration contract against a disposable pgvector database."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest


pytestmark = pytest.mark.postgres_integration


def _settings(dsn, *, model="lemonade-embeddings-bge-m3-q8", dimensions=1024):
    from iwiki_mcp.postgres.migrations import MigrationSettings

    return MigrationSettings(
        dsn=dsn,
        embed_model=model,
        embed_dimensions=dimensions,
        statement_timeout_ms=30_000,
        lock_timeout_ms=5_000,
    )


def test_empty_database_creates_only_iwiki_schema_objects(clean_postgres):
    import psycopg

    from iwiki_mcp.postgres.migrations import run_migrations

    with psycopg.connect(clean_postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT nspname FROM pg_namespace "
                "WHERE nspname !~ '^pg_' AND nspname <> 'information_schema'"
            )
            schemas_before = {row[0] for row in cursor.fetchall()}

    result = run_migrations(_settings(clean_postgres))

    with psycopg.connect(clean_postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT nspname FROM pg_namespace "
                "WHERE nspname !~ '^pg_' AND nspname <> 'information_schema'"
            )
            schemas_after = {row[0] for row in cursor.fetchall()}
            cursor.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'iwiki'"
            )
            tables = {row[0] for row in cursor.fetchall()}

    assert schemas_after - schemas_before == {"iwiki"}
    assert result.applied_versions == (1, 2, 3)
    assert tables == {
        "chunks",
        "domains",
        "git_imports",
        "iwikis",
        "links",
        "pages",
        "schema_migrations",
        "storage_metadata",
        "token_domain_grants",
        "tokens",
    }


def test_repeated_and_concurrent_startup_applies_one_ordered_history(clean_postgres):
    import psycopg

    from iwiki_mcp.postgres.migrations import run_migrations

    settings = _settings(clean_postgres)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _item: run_migrations(settings), range(2)))

    repeated = run_migrations(settings)
    with psycopg.connect(clean_postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT version FROM iwiki.schema_migrations ORDER BY version"
            )
            versions = tuple(row[0] for row in cursor.fetchall())

    assert sorted(result.applied_versions for result in results) == [(), (1, 2, 3)]
    assert repeated.applied_versions == ()
    assert versions == (1, 2, 3)


def test_unrelated_schema_survives_migration(clean_postgres):
    import psycopg

    from iwiki_mcp.postgres.migrations import run_migrations

    with psycopg.connect(clean_postgres, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("CREATE SCHEMA iwiki_test_probe")
            cursor.execute(
                "CREATE TABLE iwiki_test_probe.marker (value text NOT NULL)"
            )
            cursor.execute(
                "INSERT INTO iwiki_test_probe.marker (value) VALUES ('untouched')"
            )

    run_migrations(_settings(clean_postgres))

    with psycopg.connect(clean_postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT value FROM iwiki_test_probe.marker")
            assert cursor.fetchone() == ("untouched",)


def test_failed_migration_rolls_back_version_and_objects(clean_postgres):
    import psycopg

    from iwiki_mcp.postgres.migrations import (
        MIGRATIONS,
        Migration,
        MigrationError,
        run_migrations,
    )

    run_migrations(_settings(clean_postgres))
    broken = MIGRATIONS + (
        Migration(
            version=4,
            statements=(
                "CREATE TABLE iwiki.must_roll_back (id integer PRIMARY KEY)",
                "THIS IS NOT VALID SQL",
            ),
        ),
    )

    with pytest.raises(MigrationError, match="migration failed"):
        run_migrations(_settings(clean_postgres), migrations=broken)

    with psycopg.connect(clean_postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT to_regclass('iwiki.must_roll_back'), "
                "array_agg(version ORDER BY version) "
                "FROM iwiki.schema_migrations"
            )
            assert cursor.fetchone() == (None, [1, 2, 3])


def test_newer_schema_version_refuses_startup(clean_postgres):
    import psycopg

    from iwiki_mcp.postgres.migrations import MigrationError, run_migrations

    settings = _settings(clean_postgres)
    run_migrations(settings)
    with psycopg.connect(clean_postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO iwiki.schema_migrations (version) VALUES (999)"
            )
        connection.commit()

    with pytest.raises(MigrationError, match="newer schema version"):
        run_migrations(settings)


def test_embedding_metadata_and_vector_dimension_follow_runtime(clean_postgres):
    import psycopg

    from iwiki_mcp.postgres.migrations import run_migrations

    run_migrations(_settings(clean_postgres, model="example-embedding-v2", dimensions=3))

    with psycopg.connect(clean_postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT embed_model, embed_dimensions "
                "FROM iwiki.storage_metadata"
            )
            metadata = cursor.fetchone()
            cursor.execute(
                "SELECT format_type(a.atttypid, a.atttypmod) "
                "FROM pg_attribute a "
                "JOIN pg_class c ON c.oid = a.attrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'iwiki' AND c.relname = 'chunks' "
                "AND a.attname = 'embedding'"
            )
            vector_type = cursor.fetchone()[0]

    assert metadata == ("example-embedding-v2", 3)
    assert vector_type == "vector(3)"


def test_chunks_store_dequantized_int8_vectors_and_reject_wrong_dimension(
    clean_postgres,
):
    import numpy as np
    import psycopg
    from pgvector.psycopg import register_vector

    from iwiki_mcp.engine.store import dequantize, quantize
    from iwiki_mcp.postgres.migrations import run_migrations

    run_migrations(_settings(clean_postgres, dimensions=3))
    scale, quantized = quantize([0.2, -0.4, 0.8])
    embedding = np.array(dequantize(scale, quantized), dtype=np.float32)

    with psycopg.connect(clean_postgres) as connection:
        register_vector(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO iwiki.iwikis (iwiki_id, slug) "
                "VALUES ('vector-test', 'vector-test')"
            )
            cursor.execute(
                "INSERT INTO iwiki.domains (iwiki_id, slug) "
                "VALUES ('vector-test', 'docs') RETURNING domain_id"
            )
            domain_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO iwiki.pages "
                "(iwiki_id, domain_id, slug, markdown) "
                "VALUES ('vector-test', %s, 'page', '# Page') "
                "RETURNING page_id",
                (domain_id,),
            )
            page_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO iwiki.chunks "
                "(iwiki_id, page_id, section_id, heading, content, ordinal, "
                "quantization_scale, quantized_embedding, embedding) "
                "VALUES ('vector-test', %s, 'page', 'Page', 'body', 0, "
                "%s, %s, %s)",
                (page_id, scale, quantized, embedding),
            )
        connection.commit()

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT embedding FROM iwiki.chunks "
                "WHERE iwiki_id = 'vector-test'"
            )
            stored = cursor.fetchone()[0]
        assert np.allclose(stored.to_numpy(), embedding)

        with pytest.raises(psycopg.Error):
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO iwiki.chunks "
                        "(iwiki_id, page_id, section_id, heading, content, "
                        "ordinal, quantization_scale, quantized_embedding, "
                        "embedding) VALUES "
                        "('vector-test', %s, 'wrong', 'Wrong', 'body', 1, "
                        "1, ARRAY[1, 2], %s)",
                        (page_id, np.array([1.0, 2.0], dtype=np.float32)),
                    )


@pytest.mark.parametrize(
    ("model", "dimensions"),
    [("different-model", 1024), ("lemonade-embeddings-bge-m3-q8", 768)],
)
def test_embedding_metadata_mismatch_fails_closed(
    clean_postgres, model, dimensions
):
    from iwiki_mcp.postgres.migrations import MigrationError, run_migrations

    run_migrations(_settings(clean_postgres))

    with pytest.raises(MigrationError, match="embedding metadata mismatch"):
        run_migrations(_settings(clean_postgres, model=model, dimensions=dimensions))


def test_schema_uses_tenant_composite_constraints(clean_postgres):
    import psycopg

    from iwiki_mcp.postgres.migrations import run_migrations

    run_migrations(_settings(clean_postgres))
    with psycopg.connect(clean_postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT conname FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "WHERE n.nspname = 'iwiki' AND c.contype IN ('f', 'u')"
            )
            constraints = {row[0] for row in cursor.fetchall()}

    assert {
        "domains_iwiki_slug_key",
        "pages_iwiki_domain_slug_key",
        "chunks_iwiki_page_fk",
        "links_iwiki_source_page_fk",
        "links_iwiki_target_page_fk",
        "tokens_iwiki_fk",
        "tokens_token_id_key",
        "token_domain_grants_iwiki_token_fk",
        "token_domain_grants_iwiki_domain_fk",
    } <= constraints
