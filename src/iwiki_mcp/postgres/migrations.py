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


GRAPH_MIGRATION_STATEMENTS = (
    """
    ALTER TABLE iwiki.domains
        ADD COLUMN IF NOT EXISTS markdown_generation bigint NOT NULL DEFAULT 0
    """,
    """
    DO $body$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'domains_markdown_generation_nonnegative'
              AND conrelid = 'iwiki.domains'::regclass
        ) THEN
            ALTER TABLE iwiki.domains
                ADD CONSTRAINT domains_markdown_generation_nonnegative
                CHECK (markdown_generation >= 0);
        END IF;
    END
    $body$
    """,
    """
    CREATE TABLE IF NOT EXISTS iwiki.database_principal_domain_grants (
        principal text NOT NULL,
        iwiki_id text NOT NULL,
        domain_id bigint NOT NULL,
        runtime text NOT NULL CHECK (runtime IN ('hosted', 'direct')),
        can_read boolean NOT NULL,
        can_write boolean NOT NULL,
        created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (principal, iwiki_id, domain_id),
        CONSTRAINT database_principal_grants_write_requires_read
            CHECK (NOT can_write OR can_read),
        CONSTRAINT database_principal_grants_domain_fk
            FOREIGN KEY (iwiki_id, domain_id)
            REFERENCES iwiki.domains (iwiki_id, domain_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE SEQUENCE IF NOT EXISTS iwiki.code_graph_domain_lock_id_seq AS bigint
    """,
    """
    CREATE TABLE IF NOT EXISTS iwiki.code_graph_snapshots (
        iwiki_id text NOT NULL,
        domain_id bigint NOT NULL,
        snapshot_id text NOT NULL,
        state text NOT NULL CHECK (state IN ('staging', 'ready', 'failed')),
        header jsonb NOT NULL,
        graph_payload_revision text NOT NULL,
        snapshot_revision text,
        markdown_revision text NOT NULL,
        markdown_generation bigint NOT NULL CHECK (markdown_generation >= 0),
        counts jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
        ready_at timestamptz,
        PRIMARY KEY (iwiki_id, domain_id, snapshot_id),
        CONSTRAINT code_graph_snapshots_domain_fk
            FOREIGN KEY (iwiki_id, domain_id)
            REFERENCES iwiki.domains (iwiki_id, domain_id)
            ON DELETE CASCADE,
        CONSTRAINT code_graph_snapshots_state_key
            UNIQUE (iwiki_id, domain_id, snapshot_id, state)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS iwiki.code_graph_domain_state (
        iwiki_id text NOT NULL,
        domain_id bigint NOT NULL,
        domain_lock_id bigint NOT NULL DEFAULT
            nextval('iwiki.code_graph_domain_lock_id_seq'),
        active_snapshot_id text,
        active_snapshot_state text,
        updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (iwiki_id, domain_id),
        CONSTRAINT code_graph_domain_state_lock_key UNIQUE (domain_lock_id),
        CONSTRAINT code_graph_domain_state_domain_fk
            FOREIGN KEY (iwiki_id, domain_id)
            REFERENCES iwiki.domains (iwiki_id, domain_id)
            ON DELETE CASCADE,
        CONSTRAINT code_graph_domain_state_active_shape
            CHECK (
                (active_snapshot_id IS NULL AND active_snapshot_state IS NULL)
                OR (active_snapshot_id IS NOT NULL AND active_snapshot_state = 'ready')
            ),
        CONSTRAINT code_graph_domain_state_active_ready_fk
            FOREIGN KEY (
                iwiki_id, domain_id, active_snapshot_id, active_snapshot_state
            ) REFERENCES iwiki.code_graph_snapshots (
                iwiki_id, domain_id, snapshot_id, state
            ) ON DELETE NO ACTION
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS iwiki.code_graph_publication_sessions (
        iwiki_id text NOT NULL,
        domain_id bigint NOT NULL,
        session_id text NOT NULL,
        snapshot_id text NOT NULL,
        owner_id text NOT NULL,
        state text NOT NULL CHECK (
            state IN ('staging', 'ready', 'aborted', 'failed', 'expired')
        ),
        lease_expires_at timestamptz NOT NULL,
        base_snapshot_revision text,
        captured_markdown_generation bigint NOT NULL
            CHECK (captured_markdown_generation >= 0),
        terminal_result jsonb,
        created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (iwiki_id, domain_id, session_id),
        CONSTRAINT code_graph_sessions_snapshot_fk
            FOREIGN KEY (iwiki_id, domain_id, snapshot_id)
            REFERENCES iwiki.code_graph_snapshots (
                iwiki_id, domain_id, snapshot_id
            ) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS iwiki.code_graph_batches (
        iwiki_id text NOT NULL,
        domain_id bigint NOT NULL,
        session_id text NOT NULL,
        kind text NOT NULL CHECK (
            kind IN ('repositories', 'files', 'symbols', 'relations')
        ),
        ordinal integer NOT NULL CHECK (ordinal >= 0),
        payload_hash text NOT NULL,
        row_count integer NOT NULL CHECK (row_count >= 0),
        byte_count integer NOT NULL CHECK (byte_count >= 0),
        payload bytea NOT NULL,
        accepted_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (iwiki_id, domain_id, session_id, kind, ordinal),
        CONSTRAINT code_graph_batches_session_fk
            FOREIGN KEY (iwiki_id, domain_id, session_id)
            REFERENCES iwiki.code_graph_publication_sessions (
                iwiki_id, domain_id, session_id
            ) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS iwiki.code_graph_files (
        iwiki_id text NOT NULL,
        domain_id bigint NOT NULL,
        snapshot_id text NOT NULL,
        file_id text NOT NULL,
        repository_id text NOT NULL,
        row_data jsonb NOT NULL,
        PRIMARY KEY (iwiki_id, domain_id, snapshot_id, file_id),
        CONSTRAINT code_graph_files_snapshot_fk
            FOREIGN KEY (iwiki_id, domain_id, snapshot_id)
            REFERENCES iwiki.code_graph_snapshots (
                iwiki_id, domain_id, snapshot_id
            ) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS iwiki.code_graph_symbols (
        iwiki_id text NOT NULL,
        domain_id bigint NOT NULL,
        snapshot_id text NOT NULL,
        symbol_id text NOT NULL,
        file_id text NOT NULL,
        row_data jsonb NOT NULL,
        PRIMARY KEY (iwiki_id, domain_id, snapshot_id, symbol_id),
        CONSTRAINT code_graph_symbols_snapshot_fk
            FOREIGN KEY (iwiki_id, domain_id, snapshot_id)
            REFERENCES iwiki.code_graph_snapshots (
                iwiki_id, domain_id, snapshot_id
            ) ON DELETE CASCADE,
        CONSTRAINT code_graph_symbols_file_fk
            FOREIGN KEY (iwiki_id, domain_id, snapshot_id, file_id)
            REFERENCES iwiki.code_graph_files (
                iwiki_id, domain_id, snapshot_id, file_id
            ) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS iwiki.code_graph_relations (
        iwiki_id text NOT NULL,
        domain_id bigint NOT NULL,
        snapshot_id text NOT NULL,
        relation_id text NOT NULL,
        source_file_id text NOT NULL,
        source_symbol_id text,
        target_symbol_id text,
        row_data jsonb NOT NULL,
        PRIMARY KEY (iwiki_id, domain_id, snapshot_id, relation_id),
        CONSTRAINT code_graph_relations_snapshot_fk
            FOREIGN KEY (iwiki_id, domain_id, snapshot_id)
            REFERENCES iwiki.code_graph_snapshots (
                iwiki_id, domain_id, snapshot_id
            ) ON DELETE CASCADE,
        CONSTRAINT code_graph_relations_source_file_fk
            FOREIGN KEY (iwiki_id, domain_id, snapshot_id, source_file_id)
            REFERENCES iwiki.code_graph_files (
                iwiki_id, domain_id, snapshot_id, file_id
            ) ON DELETE CASCADE,
        CONSTRAINT code_graph_relations_source_symbol_fk
            FOREIGN KEY (iwiki_id, domain_id, snapshot_id, source_symbol_id)
            REFERENCES iwiki.code_graph_symbols (
                iwiki_id, domain_id, snapshot_id, symbol_id
            ) ON DELETE CASCADE,
        CONSTRAINT code_graph_relations_target_symbol_fk
            FOREIGN KEY (iwiki_id, domain_id, snapshot_id, target_symbol_id)
            REFERENCES iwiki.code_graph_symbols (
                iwiki_id, domain_id, snapshot_id, symbol_id
            ) ON DELETE NO ACTION
    )
    """,
    """
    DO $body$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'pages_iwiki_domain_page_key'
              AND conrelid = 'iwiki.pages'::regclass
        ) THEN
            ALTER TABLE iwiki.pages
                ADD CONSTRAINT pages_iwiki_domain_page_key
                UNIQUE (iwiki_id, domain_id, page_id);
        END IF;
    END
    $body$
    """,
    """
    CREATE TABLE IF NOT EXISTS iwiki.code_graph_wiki_links (
        iwiki_id text NOT NULL,
        domain_id bigint NOT NULL,
        snapshot_id text NOT NULL,
        relation_id text NOT NULL,
        page_id bigint NOT NULL,
        selector jsonb NOT NULL,
        provenance jsonb NOT NULL,
        PRIMARY KEY (
            iwiki_id, domain_id, snapshot_id, relation_id, page_id, selector
        ),
        CONSTRAINT code_graph_wiki_links_snapshot_fk
            FOREIGN KEY (iwiki_id, domain_id, snapshot_id)
            REFERENCES iwiki.code_graph_snapshots (
                iwiki_id, domain_id, snapshot_id
            ) ON DELETE CASCADE,
        CONSTRAINT code_graph_wiki_links_relation_fk
            FOREIGN KEY (iwiki_id, domain_id, snapshot_id, relation_id)
            REFERENCES iwiki.code_graph_relations (
                iwiki_id, domain_id, snapshot_id, relation_id
            ) ON DELETE CASCADE,
        CONSTRAINT code_graph_wiki_links_page_fk
            FOREIGN KEY (iwiki_id, domain_id, page_id)
            REFERENCES iwiki.pages (iwiki_id, domain_id, page_id)
            ON DELETE NO ACTION
    )
    """,
    """
    CREATE OR REPLACE FUNCTION iwiki.database_principal_can_access(
        requested_iwiki text,
        requested_domain bigint,
        requested_write boolean
    ) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, iwiki
    AS $function$
        SELECT EXISTS (
            SELECT 1
            FROM iwiki.database_principal_domain_grants g
            WHERE g.principal = session_user
              AND g.iwiki_id = requested_iwiki
              AND g.domain_id = requested_domain
              AND g.can_read
              AND (NOT requested_write OR g.can_write)
        )
    $function$
    """,
    """
    REVOKE ALL ON FUNCTION iwiki.database_principal_can_access(text, bigint, boolean)
        FROM PUBLIC
    """,
    """
    CREATE OR REPLACE FUNCTION iwiki.database_principal_runtime_domains(
        requested_runtime text
    ) RETURNS bigint
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, iwiki
    AS $function$
        SELECT count(*)
        FROM iwiki.database_principal_domain_grants g
        WHERE g.principal = session_user
          AND g.runtime = requested_runtime
          AND g.can_read
    $function$
    """,
    """
    REVOKE ALL ON FUNCTION iwiki.database_principal_runtime_domains(text)
        FROM PUBLIC
    """,
    """
    CREATE OR REPLACE FUNCTION iwiki.create_domain_for_principal(
        requested_iwiki text,
        requested_slug text
    ) RETURNS bigint
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = pg_catalog, iwiki
    AS $function$
    DECLARE
        created_domain bigint;
        runtime_kind text;
    BEGIN
        SELECT g.runtime INTO runtime_kind
        FROM iwiki.database_principal_domain_grants g
        WHERE g.principal = session_user
          AND g.iwiki_id = requested_iwiki
        LIMIT 1;
        IF runtime_kind IS NULL AND EXISTS (
            SELECT 1
            FROM iwiki.database_principal_domain_grants g
            WHERE g.principal = session_user
        ) THEN
            RAISE EXCEPTION 'principal is not provisioned for this wiki';
        END IF;
        INSERT INTO iwiki.domains (iwiki_id, slug)
        VALUES (requested_iwiki, requested_slug)
        ON CONFLICT (iwiki_id, slug) DO NOTHING
        RETURNING domain_id INTO created_domain;
        IF created_domain IS NULL THEN
            RETURN NULL;
        END IF;
        IF runtime_kind IS NOT NULL THEN
            INSERT INTO iwiki.database_principal_domain_grants
                (principal, iwiki_id, domain_id, runtime, can_read, can_write)
            VALUES (
                session_user, requested_iwiki, created_domain, runtime_kind,
                true, true
            );
        END IF;
        RETURN created_domain;
    END;
    $function$
    """,
    """
    REVOKE ALL ON FUNCTION iwiki.create_domain_for_principal(text, text)
        FROM PUBLIC
    """,
    *(f"ALTER TABLE iwiki.{table} ENABLE ROW LEVEL SECURITY" for table in (
        "domains", "pages", "chunks", "links", "code_graph_domain_state",
        "code_graph_publication_sessions", "code_graph_snapshots",
        "code_graph_batches", "code_graph_files", "code_graph_symbols",
        "code_graph_relations", "code_graph_wiki_links",
    )),
    """
    DROP POLICY IF EXISTS database_principal_scope ON iwiki.domains;
    CREATE POLICY database_principal_scope ON iwiki.domains
        USING (iwiki.database_principal_can_access(iwiki_id, domain_id, false))
        WITH CHECK (iwiki.database_principal_can_access(iwiki_id, domain_id, true))
    """,
    """
    DROP POLICY IF EXISTS database_principal_scope ON iwiki.pages;
    CREATE POLICY database_principal_scope ON iwiki.pages
        USING (iwiki.database_principal_can_access(iwiki_id, domain_id, false))
        WITH CHECK (iwiki.database_principal_can_access(iwiki_id, domain_id, true))
    """,
    """
    DROP POLICY IF EXISTS database_principal_scope ON iwiki.chunks;
    CREATE POLICY database_principal_scope ON iwiki.chunks
        USING (EXISTS (
            SELECT 1 FROM iwiki.pages p
            WHERE p.iwiki_id = chunks.iwiki_id AND p.page_id = chunks.page_id
              AND iwiki.database_principal_can_access(
                  p.iwiki_id, p.domain_id, false
              )
        ))
        WITH CHECK (EXISTS (
            SELECT 1 FROM iwiki.pages p
            WHERE p.iwiki_id = chunks.iwiki_id AND p.page_id = chunks.page_id
              AND iwiki.database_principal_can_access(
                  p.iwiki_id, p.domain_id, true
              )
        ))
    """,
    """
    DROP POLICY IF EXISTS database_principal_scope ON iwiki.links;
    CREATE POLICY database_principal_scope ON iwiki.links
        USING (EXISTS (
            SELECT 1 FROM iwiki.pages p
            WHERE p.iwiki_id = links.iwiki_id
              AND p.page_id = links.source_page_id
              AND iwiki.database_principal_can_access(
                  p.iwiki_id, p.domain_id, false
              )
        ))
        WITH CHECK (EXISTS (
            SELECT 1 FROM iwiki.pages p
            WHERE p.iwiki_id = links.iwiki_id
              AND p.page_id = links.source_page_id
              AND iwiki.database_principal_can_access(
                  p.iwiki_id, p.domain_id, true
              )
        ))
    """,
    *(f"""
    DROP POLICY IF EXISTS database_principal_scope ON iwiki.{table};
    CREATE POLICY database_principal_scope ON iwiki.{table}
        USING (iwiki.database_principal_can_access(iwiki_id, domain_id, false))
        WITH CHECK (iwiki.database_principal_can_access(iwiki_id, domain_id, true))
    """ for table in (
        "code_graph_domain_state", "code_graph_publication_sessions",
        "code_graph_snapshots", "code_graph_batches", "code_graph_files",
        "code_graph_symbols", "code_graph_relations", "code_graph_wiki_links",
    )),
)


SCHEMA5_COMPATIBILITY_ROLLBACK_SQL = f"""
SELECT pg_advisory_xact_lock({_MIGRATION_LOCK});
DELETE FROM iwiki.schema_migrations WHERE version = 5;
"""


SPECIFICATION_MIGRATION_STATEMENTS = (
    """
    CREATE TABLE iwiki.specification_scenarios (
        iwiki_id text NOT NULL,
        domain_id bigint NOT NULL,
        scenario_id text NOT NULL,
        page_id bigint NOT NULL,
        title text NOT NULL,
        heading text NOT NULL,
        anchor text NOT NULL,
        source_hash text NOT NULL,
        items jsonb NOT NULL CHECK (jsonb_typeof(items) = 'array'),
        page_revision bigint NOT NULL CHECK (page_revision > 0),
        CONSTRAINT specification_scenarios_domain_identity_key
            PRIMARY KEY (iwiki_id, domain_id, scenario_id),
        CONSTRAINT specification_scenarios_domain_fk
            FOREIGN KEY (iwiki_id, domain_id)
            REFERENCES iwiki.domains (iwiki_id, domain_id)
            ON DELETE CASCADE,
        CONSTRAINT specification_scenarios_page_fk
            FOREIGN KEY (iwiki_id, domain_id, page_id)
            REFERENCES iwiki.pages (iwiki_id, domain_id, page_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX specification_scenarios_title_idx
        ON iwiki.specification_scenarios (iwiki_id, domain_id, lower(title))
    """,
    """
    CREATE INDEX specification_scenarios_items_idx
        ON iwiki.specification_scenarios USING gin (items jsonb_path_ops)
    """,
    """
    CREATE TABLE iwiki.specification_bindings (
        iwiki_id text NOT NULL,
        domain_id bigint NOT NULL,
        scenario_id text NOT NULL,
        binding_id text NOT NULL,
        relation text NOT NULL CHECK (relation IN ('implements', 'verifies')),
        phase text CHECK (phase IN ('given', 'when', 'then')),
        selector_kind text NOT NULL CHECK (
            selector_kind IN ('symbol', 'file', 'source_glob')
        ),
        selector text NOT NULL,
        PRIMARY KEY (iwiki_id, domain_id, scenario_id, binding_id),
        CONSTRAINT specification_bindings_scenario_fk
            FOREIGN KEY (iwiki_id, domain_id, scenario_id)
            REFERENCES iwiki.specification_scenarios (
                iwiki_id, domain_id, scenario_id
            ) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX specification_bindings_selector_idx
        ON iwiki.specification_bindings (
            iwiki_id, domain_id, selector_kind, selector
        )
    """,
    """
    CREATE TABLE iwiki.specification_evidence (
        iwiki_id text NOT NULL,
        domain_id bigint NOT NULL,
        scenario_id text NOT NULL,
        binding_id text NOT NULL,
        state text NOT NULL CHECK (
            state IN ('resolved', 'ambiguous', 'unresolved', 'graph_unavailable')
        ),
        targets jsonb NOT NULL CHECK (jsonb_typeof(targets) = 'array'),
        unresolved_reference text,
        graph_revision text,
        graph_state_fingerprint text NOT NULL,
        specification_source_hash text NOT NULL,
        checked_at timestamptz NOT NULL,
        reason text,
        PRIMARY KEY (iwiki_id, domain_id, scenario_id, binding_id),
        CONSTRAINT specification_evidence_binding_fk
            FOREIGN KEY (iwiki_id, domain_id, scenario_id, binding_id)
            REFERENCES iwiki.specification_bindings (
                iwiki_id, domain_id, scenario_id, binding_id
            ) ON DELETE CASCADE
    )
    """,
    *(f"ALTER TABLE iwiki.{table} ENABLE ROW LEVEL SECURITY" for table in (
        "specification_scenarios",
        "specification_bindings",
        "specification_evidence",
    )),
    *(f"""
    CREATE POLICY database_principal_select ON iwiki.{table}
        FOR SELECT
        USING (iwiki.database_principal_can_access(iwiki_id, domain_id, false))
    """ for table in (
        "specification_scenarios",
        "specification_bindings",
        "specification_evidence",
    )),
    *(f"""
    CREATE POLICY database_principal_insert ON iwiki.{table}
        FOR INSERT
        WITH CHECK (iwiki.database_principal_can_access(iwiki_id, domain_id, true))
    """ for table in (
        "specification_scenarios",
        "specification_bindings",
        "specification_evidence",
    )),
    *(f"""
    CREATE POLICY database_principal_update ON iwiki.{table}
        FOR UPDATE
        USING (iwiki.database_principal_can_access(iwiki_id, domain_id, true))
        WITH CHECK (iwiki.database_principal_can_access(iwiki_id, domain_id, true))
    """ for table in (
        "specification_scenarios",
        "specification_bindings",
        "specification_evidence",
    )),
    *(f"""
    CREATE POLICY database_principal_delete ON iwiki.{table}
        FOR DELETE
        USING (iwiki.database_principal_can_access(iwiki_id, domain_id, true))
    """ for table in (
        "specification_scenarios",
        "specification_bindings",
        "specification_evidence",
    )),
)


SCHEMA6_COMPATIBILITY_ROLLBACK_SQL = f"""
SELECT pg_advisory_xact_lock({_MIGRATION_LOCK});
DROP TABLE iwiki.specification_evidence;
DROP TABLE iwiki.specification_bindings;
DROP TABLE iwiki.specification_scenarios;
DELETE FROM iwiki.schema_migrations WHERE version = 6;
"""


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
    Migration(
        version=3,
        statements=(
            "ALTER TABLE iwiki.tokens RENAME COLUMN label TO owner",
            "ALTER TABLE iwiki.tokens ADD CONSTRAINT "
            "tokens_token_id_key UNIQUE (token_id)",
        ),
    ),
    Migration(
        version=4,
        statements=(
            "ALTER TABLE iwiki.tokens ADD COLUMN "
            "can_create_domain boolean NOT NULL DEFAULT false",
            """
            CREATE TABLE iwiki.token_domain_management_grants (
                iwiki_id text NOT NULL,
                token_id text NOT NULL,
                domain_id bigint NOT NULL,
                can_manage_grants boolean NOT NULL,
                PRIMARY KEY (iwiki_id, token_id, domain_id),
                CONSTRAINT token_domain_management_grants_enabled
                    CHECK (can_manage_grants),
                CONSTRAINT token_domain_management_grants_iwiki_token_fk
                    FOREIGN KEY (iwiki_id, token_id)
                    REFERENCES iwiki.tokens (iwiki_id, token_id)
                    ON DELETE CASCADE,
                CONSTRAINT token_domain_management_grants_iwiki_domain_fk
                    FOREIGN KEY (iwiki_id, domain_id)
                    REFERENCES iwiki.domains (iwiki_id, domain_id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX token_domain_grants_domain_idx
                ON iwiki.token_domain_grants (iwiki_id, domain_id)
            """,
            """
            CREATE INDEX token_domain_management_grants_domain_idx
                ON iwiki.token_domain_management_grants (iwiki_id, domain_id)
            """,
        ),
    ),
    Migration(version=5, statements=GRAPH_MIGRATION_STATEMENTS),
    Migration(version=6, statements=SPECIFICATION_MIGRATION_STATEMENTS),
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


def require_schema_version(
    dsn: str,
    expected_version: int = 6,
    *,
    connect_timeout_s: int = 10,
) -> None:
    """Require one exact installed schema version without mutating the database."""
    try:
        with psycopg.connect(
            dsn, connect_timeout=connect_timeout_s
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute(
                    "SELECT COALESCE(MAX(version), 0) "
                    "FROM iwiki.schema_migrations"
                )
                current = cursor.fetchone()[0]
    except psycopg.Error as exc:
        raise MigrationError(
            f"PostgreSQL schema version {expected_version} is required"
        ) from exc
    if current != expected_version:
        raise MigrationError(
            f"PostgreSQL schema version {expected_version} is required"
        )


def rollback_v5_compatibility(
    settings: MigrationSettings,
    *,
    confirm: bool,
) -> dict[str, int | bool]:
    """Remove only migration marker 4 after validating mapped runtime roles."""
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
                    cursor.execute(
                        "SELECT COALESCE(MAX(version), 0) "
                        "FROM iwiki.schema_migrations"
                    )
                    current = cursor.fetchone()[0]
                    if current != 5:
                        raise MigrationError(
                            "schema version 5 compatibility rollback is unavailable"
                        )
                    cursor.execute(
                        """
                        SELECT g.principal
                        FROM iwiki.database_principal_domain_grants g
                        LEFT JOIN pg_roles r ON r.rolname = g.principal
                        WHERE r.rolname IS NULL OR r.rolsuper OR r.rolbypassrls
                           OR EXISTS (
                               SELECT 1
                               FROM pg_class c
                               JOIN pg_namespace n ON n.oid = c.relnamespace
                               WHERE n.nspname = 'iwiki'
                                 AND c.relname IN (
                                     'domains', 'pages', 'chunks', 'links',
                                     'code_graph_domain_state',
                                     'code_graph_publication_sessions',
                                     'code_graph_snapshots', 'code_graph_batches',
                                     'code_graph_files', 'code_graph_symbols',
                                     'code_graph_relations', 'code_graph_wiki_links'
                                 )
                                 AND c.relowner = r.oid
                           )
                        LIMIT 1
                        """
                    )
                    if cursor.fetchone() is not None:
                        raise MigrationError("runtime principal validation failed")
                    if not confirm:
                        return {
                            "dry_run": True,
                            "schema_version": 5,
                            "would_remove_marker": 5,
                        }
                    cursor.execute(
                        "DELETE FROM iwiki.schema_migrations WHERE version = 5"
                    )
                    if cursor.rowcount != 1:
                        raise MigrationError("migration marker removal failed")
    except MigrationError:
        raise
    except psycopg.Error as exc:
        raise MigrationError("compatibility rollback failed") from exc
    return {
        "dry_run": False,
        "schema_version": 4,
        "removed_marker": 5,
    }


def rollback_v6_compatibility(
    settings: MigrationSettings,
    *,
    confirm: bool,
) -> dict[str, int | bool]:
    """Drop only v6 specification objects and its migration marker."""
    if confirm is not True:
        raise ValueError("confirmation must be literal true")
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
                    cursor.execute(
                        "SELECT COALESCE(MAX(version), 0) "
                        "FROM iwiki.schema_migrations"
                    )
                    current = cursor.fetchone()[0]
                    if current != 6:
                        raise MigrationError(
                            "schema version 6 compatibility rollback is unavailable"
                        )
                    cursor.execute("DROP TABLE iwiki.specification_evidence")
                    cursor.execute("DROP TABLE iwiki.specification_bindings")
                    cursor.execute("DROP TABLE iwiki.specification_scenarios")
                    cursor.execute(
                        "DELETE FROM iwiki.schema_migrations WHERE version = 6"
                    )
                    if cursor.rowcount != 1:
                        raise MigrationError("migration marker removal failed")
    except MigrationError:
        raise
    except psycopg.Error as exc:
        raise MigrationError("compatibility rollback failed") from exc
    return {
        "dry_run": False,
        "schema_version": 5,
        "removed_marker": 6,
    }
