"""Transactional, forward-only PostgreSQL schema migrations."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import psycopg
from psycopg import sql


class MigrationError(RuntimeError):
    """Safe startup error for PostgreSQL migration failures."""


@dataclass(frozen=True)
class MigrationSettings:
    dsn: str = field(repr=False)
    embed_model: str
    embed_dimensions: int
    statement_timeout_ms: int
    lock_timeout_ms: int
    connect_timeout_s: int = 10

    def __post_init__(self) -> None:
        if not self.dsn:
            raise ValueError("database connection is required")
        if not self.embed_model.strip():
            raise ValueError("embedding model is required")
        if not 1 <= self.embed_dimensions <= 2000:
            raise ValueError("embedding dimensions must be between 1 and 2000")
        if self.statement_timeout_ms <= 0 or self.lock_timeout_ms <= 0:
            raise ValueError("database timeouts must be positive")
        if self.connect_timeout_s <= 0:
            raise ValueError("database connection timeout must be positive")


@dataclass(frozen=True)
class Migration:
    version: int
    statements: tuple[str, ...]


@dataclass(frozen=True)
class MigrationResult:
    applied_versions: tuple[int, ...]
    schema_version: int


_MIGRATION_LOCK = 7595435311942266217


MIGRATIONS = (
    Migration(
        version=1,
        statements=(
            """
            CREATE TABLE iwiki.storage_metadata (
                singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
                embed_model text NOT NULL,
                embed_dimensions integer NOT NULL CHECK (embed_dimensions > 0),
                created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE iwiki.iwikis (
                iwiki_id text PRIMARY KEY,
                slug text NOT NULL UNIQUE,
                active boolean NOT NULL DEFAULT true,
                created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE iwiki.domains (
                iwiki_id text NOT NULL,
                domain_id bigint GENERATED ALWAYS AS IDENTITY,
                slug text NOT NULL,
                created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (iwiki_id, domain_id),
                CONSTRAINT domains_iwiki_fk
                    FOREIGN KEY (iwiki_id) REFERENCES iwiki.iwikis (iwiki_id)
                    ON DELETE CASCADE,
                CONSTRAINT domains_iwiki_slug_key UNIQUE (iwiki_id, slug)
            )
            """,
            """
            CREATE TABLE iwiki.pages (
                iwiki_id text NOT NULL,
                page_id bigint GENERATED ALWAYS AS IDENTITY,
                domain_id bigint NOT NULL,
                slug text NOT NULL,
                markdown text NOT NULL,
                revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
                created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (iwiki_id, page_id),
                CONSTRAINT pages_iwiki_domain_fk
                    FOREIGN KEY (iwiki_id, domain_id)
                    REFERENCES iwiki.domains (iwiki_id, domain_id)
                    ON DELETE CASCADE,
                CONSTRAINT pages_iwiki_domain_slug_key
                    UNIQUE (iwiki_id, domain_id, slug)
            )
            """,
            """
            CREATE TABLE iwiki.chunks (
                iwiki_id text NOT NULL,
                chunk_id bigint GENERATED ALWAYS AS IDENTITY,
                page_id bigint NOT NULL,
                section_id text NOT NULL,
                heading text NOT NULL,
                content text NOT NULL,
                ordinal integer NOT NULL CHECK (ordinal >= 0),
                quantization_scale double precision NOT NULL
                    CHECK (quantization_scale >= 0),
                quantized_embedding smallint[] NOT NULL,
                embedding {vector_type} NOT NULL,
                PRIMARY KEY (iwiki_id, chunk_id),
                CONSTRAINT chunks_iwiki_page_fk
                    FOREIGN KEY (iwiki_id, page_id)
                    REFERENCES iwiki.pages (iwiki_id, page_id)
                    ON DELETE CASCADE,
                CONSTRAINT chunks_iwiki_page_ordinal_key
                    UNIQUE (iwiki_id, page_id, ordinal)
            )
            """,
            """
            CREATE INDEX chunks_embedding_cosine_idx
                ON iwiki.chunks USING hnsw
                (embedding {vector_cosine_ops})
            """,
            """
            CREATE TABLE iwiki.links (
                iwiki_id text NOT NULL,
                link_id bigint GENERATED ALWAYS AS IDENTITY,
                source_page_id bigint NOT NULL,
                target_page_id bigint,
                target_domain text NOT NULL,
                target_slug text NOT NULL,
                PRIMARY KEY (iwiki_id, link_id),
                CONSTRAINT links_iwiki_source_page_fk
                    FOREIGN KEY (iwiki_id, source_page_id)
                    REFERENCES iwiki.pages (iwiki_id, page_id)
                    ON DELETE CASCADE,
                CONSTRAINT links_iwiki_target_page_fk
                    FOREIGN KEY (iwiki_id, target_page_id)
                    REFERENCES iwiki.pages (iwiki_id, page_id)
                    ON DELETE CASCADE,
                CONSTRAINT links_iwiki_target_key
                    UNIQUE (
                        iwiki_id, source_page_id, target_domain, target_slug
                    )
            )
            """,
            """
            CREATE TABLE iwiki.tokens (
                iwiki_id text NOT NULL,
                token_id text NOT NULL,
                token_digest bytea NOT NULL UNIQUE,
                label text NOT NULL,
                created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_used_at timestamptz,
                revoked_at timestamptz,
                PRIMARY KEY (iwiki_id, token_id),
                CONSTRAINT tokens_iwiki_fk
                    FOREIGN KEY (iwiki_id) REFERENCES iwiki.iwikis (iwiki_id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE iwiki.token_domain_grants (
                iwiki_id text NOT NULL,
                token_id text NOT NULL,
                domain_id bigint NOT NULL,
                can_read boolean NOT NULL,
                can_write boolean NOT NULL,
                PRIMARY KEY (iwiki_id, token_id, domain_id),
                CONSTRAINT token_domain_grants_write_requires_read
                    CHECK (NOT can_write OR can_read),
                CONSTRAINT token_domain_grants_iwiki_token_fk
                    FOREIGN KEY (iwiki_id, token_id)
                    REFERENCES iwiki.tokens (iwiki_id, token_id)
                    ON DELETE CASCADE,
                CONSTRAINT token_domain_grants_iwiki_domain_fk
                    FOREIGN KEY (iwiki_id, domain_id)
                    REFERENCES iwiki.domains (iwiki_id, domain_id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE iwiki.git_imports (
                iwiki_id text NOT NULL,
                source_fingerprint text NOT NULL,
                counts jsonb NOT NULL,
                completed_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (iwiki_id, source_fingerprint),
                CONSTRAINT git_imports_iwiki_fk
                    FOREIGN KEY (iwiki_id) REFERENCES iwiki.iwikis (iwiki_id)
                    ON DELETE CASCADE
            )
            """,
        ),
    ),
    Migration(
        version=2,
        statements=(
            """
            ALTER TABLE iwiki.links
                DROP CONSTRAINT links_iwiki_target_page_fk
            """,
            """
            ALTER TABLE iwiki.links
                ADD CONSTRAINT links_iwiki_target_page_fk
                FOREIGN KEY (iwiki_id, target_page_id)
                REFERENCES iwiki.pages (iwiki_id, page_id)
                ON DELETE NO ACTION
            """,
        ),
    ),
)


def _validate_migrations(migrations: Sequence[Migration]) -> None:
    versions = tuple(migration.version for migration in migrations)
    if not versions or versions != tuple(range(1, len(versions) + 1)):
        raise ValueError("migration versions must be ordered and contiguous")
    if any(not migration.statements for migration in migrations):
        raise ValueError("migration statements must not be empty")


def _vector_schema(cursor) -> str:
    cursor.execute(
        """
        SELECT n.nspname
        FROM pg_extension e
        JOIN pg_namespace n ON n.oid = e.extnamespace
        WHERE e.extname = 'vector'
        """
    )
    row = cursor.fetchone()
    if row is None:
        raise MigrationError("pgvector extension is not enabled")
    return row[0]


def _render_statement(
    connection, statement: str, vector_schema: str, dimensions: int
) -> str:
    schema = sql.Identifier(vector_schema).as_string(connection)
    return statement.replace(
        "{vector_type}", f"{schema}.vector({dimensions})"
    ).replace(
        "{vector_cosine_ops}", f"{schema}.vector_cosine_ops"
    )


def _validate_metadata(cursor, settings: MigrationSettings) -> None:
    cursor.execute(
        """
        INSERT INTO iwiki.storage_metadata (
            singleton, embed_model, embed_dimensions
        )
        VALUES (true, %s, %s)
        ON CONFLICT (singleton) DO NOTHING
        """,
        (settings.embed_model, settings.embed_dimensions),
    )
    cursor.execute(
        """
        SELECT embed_model, embed_dimensions
        FROM iwiki.storage_metadata
        WHERE singleton = true
        """
    )
    row = cursor.fetchone()
    expected = (settings.embed_model, settings.embed_dimensions)
    if row != expected:
        raise MigrationError("embedding metadata mismatch")


def run_migrations(
    settings: MigrationSettings,
    *,
    migrations: Sequence[Migration] = MIGRATIONS,
) -> MigrationResult:
    """Apply pending migrations in one locked transaction and validate metadata."""
    _validate_migrations(migrations)
    latest = migrations[-1].version
    applied: list[int] = []
    try:
        with psycopg.connect(
            settings.dsn,
            connect_timeout=settings.connect_timeout_s,
        ) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        (str(settings.statement_timeout_ms),),
                    )
                    cursor.execute(
                        "SELECT set_config('lock_timeout', %s, true)",
                        (str(settings.lock_timeout_ms),),
                    )
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK,)
                    )
                    vector_schema = _vector_schema(cursor)
                    cursor.execute("CREATE SCHEMA IF NOT EXISTS iwiki")
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS iwiki.schema_migrations (
                            version integer PRIMARY KEY,
                            applied_at timestamptz NOT NULL
                                DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                    cursor.execute(
                        "SELECT COALESCE(MAX(version), 0) "
                        "FROM iwiki.schema_migrations"
                    )
                    current = cursor.fetchone()[0]
                    if current > latest:
                        raise MigrationError("newer schema version is not supported")

                    for migration in migrations:
                        if migration.version <= current:
                            continue
                        for statement in migration.statements:
                            cursor.execute(
                                _render_statement(
                                    connection,
                                    statement,
                                    vector_schema,
                                    settings.embed_dimensions,
                                )
                            )
                        cursor.execute(
                            "INSERT INTO iwiki.schema_migrations (version) VALUES (%s)",
                            (migration.version,),
                        )
                        applied.append(migration.version)

                    _validate_metadata(cursor, settings)
    except MigrationError:
        raise
    except (psycopg.Error, ValueError) as exc:
        raise MigrationError("migration failed") from exc
    return MigrationResult(
        applied_versions=tuple(applied),
        schema_version=latest,
    )
