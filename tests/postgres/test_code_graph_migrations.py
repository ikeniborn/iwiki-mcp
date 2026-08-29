"""Migration-v4 tenant integrity and runtime-principal integration tests."""
from __future__ import annotations

from dataclasses import dataclass
import secrets

import pytest


pytestmark = pytest.mark.postgres_integration


def _settings(dsn):
    from iwiki_mcp.postgres.migrations import MigrationSettings

    return MigrationSettings(
        dsn=dsn,
        embed_model="fixture-model",
        embed_dimensions=3,
        statement_timeout_ms=30_000,
        lock_timeout_ms=5_000,
    )


@dataclass(frozen=True)
class RoleDatabase:
    admin_dsn: str
    owner_dsn: str
    bypass_dsn: str
    restricted_dsn: str
    unmapped_dsn: str
    hosted_dsn: str
    owner_role: str
    bypass_role: str
    restricted_role: str
    unmapped_role: str
    hosted_role: str
    schema_owner: str


@pytest.fixture
def role_database(clean_postgres):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from iwiki_mcp.postgres.migrations import run_migrations

    run_migrations(_settings(clean_postgres))
    suffix = secrets.token_hex(5)
    roles = {
        name: f"iwiki_t3_{name}_{suffix}"
        for name in ("owner", "bypass", "restricted", "unmapped", "hosted")
    }
    passwords = {name: secrets.token_urlsafe(24) for name in roles}
    params = conninfo_to_dict(clean_postgres)
    with psycopg.connect(clean_postgres, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            schema_owner = cursor.fetchone()[0]
            for name, role in roles.items():
                options = sql.SQL("BYPASSRLS") if name == "bypass" else sql.SQL("NOBYPASSRLS")
                try:
                    cursor.execute(
                        sql.SQL("CREATE ROLE {} LOGIN {} PASSWORD {}").format(
                            sql.Identifier(role),
                            options,
                            sql.Literal(passwords[name]),
                        )
                    )
                except psycopg.Error:
                    pytest.fail("disposable PostgreSQL role setup failed")
            cursor.execute(
                sql.SQL("ALTER TABLE iwiki.pages OWNER TO {}").format(
                    sql.Identifier(roles["owner"])
                )
            )
            cursor.execute(
                "INSERT INTO iwiki.iwikis (iwiki_id, slug) VALUES "
                "('wiki-a', 'wiki-a')"
            )
            cursor.execute(
                "INSERT INTO iwiki.domains (iwiki_id, slug) VALUES "
                "('wiki-a', 'docs'), ('wiki-a', 'private')"
            )
            cursor.execute(
                "INSERT INTO iwiki.pages (iwiki_id, domain_id, slug, markdown) "
                "SELECT 'wiki-a', domain_id, 'page', '# Page' "
                "FROM iwiki.domains WHERE iwiki_id = 'wiki-a'"
            )

    def role_dsn(name):
        return type(clean_postgres)(
            make_conninfo(
                **{
                    **params,
                    "user": roles[name],
                    "password": passwords[name],
                }
            )
        )

    fixture = RoleDatabase(
        admin_dsn=clean_postgres,
        owner_dsn=role_dsn("owner"),
        bypass_dsn=role_dsn("bypass"),
        restricted_dsn=role_dsn("restricted"),
        unmapped_dsn=role_dsn("unmapped"),
        hosted_dsn=role_dsn("hosted"),
        owner_role=roles["owner"],
        bypass_role=roles["bypass"],
        restricted_role=roles["restricted"],
        unmapped_role=roles["unmapped"],
        hosted_role=roles["hosted"],
        schema_owner=schema_owner,
    )
    yield fixture

    with psycopg.connect(clean_postgres, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("ALTER TABLE iwiki.pages OWNER TO {}").format(
                    sql.Identifier(schema_owner)
                )
            )
            for role in reversed(tuple(roles.values())):
                cursor.execute(
                    sql.SQL("DROP OWNED BY {} CASCADE").format(sql.Identifier(role))
                )
                cursor.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def _tables(dsn):
    import psycopg

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'iwiki'")
            return {row[0] for row in cursor.fetchall()}


def _visible_page_slugs(dsn):
    import psycopg

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT d.slug || '/' || p.slug FROM iwiki.pages p "
                "JOIN iwiki.domains d USING (iwiki_id, domain_id) ORDER BY 1"
            )
            return {row[0] for row in cursor.fetchall()}


def test_graph_migration_creates_v5_objects_and_composite_integrity(clean_postgres):
    import psycopg

    from iwiki_mcp.postgres.migrations import run_migrations

    result = run_migrations(_settings(clean_postgres))
    assert result.schema_version == 7
    assert result.applied_versions == (1, 2, 3, 4, 5, 6, 7)
    assert {
        "code_graph_domain_state",
        "code_graph_publication_sessions",
        "code_graph_snapshots",
        "code_graph_batches",
        "code_graph_files",
        "code_graph_symbols",
        "code_graph_relations",
        "code_graph_wiki_links",
        "database_principal_domain_grants",
    } <= _tables(clean_postgres)
    with psycopg.connect(clean_postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT column_default, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'iwiki' AND table_name = 'domains' "
                "AND column_name = 'markdown_generation'"
            )
            default, nullable = cursor.fetchone()
            assert default == "0"
            assert nullable == "NO"
            cursor.execute(
                "SELECT conname FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "WHERE n.nspname = 'iwiki' AND t.relname LIKE 'code_graph_%'"
            )
            constraints = {row[0] for row in cursor.fetchall()}
            assert {
                "code_graph_domain_state_lock_key",
                "code_graph_sessions_snapshot_fk",
                "code_graph_files_snapshot_fk",
                "code_graph_symbols_file_fk",
                "code_graph_relations_source_symbol_fk",
                "code_graph_wiki_links_page_fk",
                "code_graph_domain_state_active_ready_fk",
            } <= constraints


def test_active_pointer_rejects_staging_and_cross_domain_snapshot(clean_postgres):
    import psycopg

    from iwiki_mcp.postgres.migrations import run_migrations

    run_migrations(_settings(clean_postgres))
    with psycopg.connect(clean_postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO iwiki.iwikis VALUES ('wiki-a', 'wiki-a')")
            cursor.execute(
                "INSERT INTO iwiki.domains (iwiki_id, slug) VALUES "
                "('wiki-a', 'docs'), ('wiki-a', 'private') RETURNING domain_id"
            )
            docs_id, private_id = [row[0] for row in cursor.fetchall()]
            cursor.execute(
                "INSERT INTO iwiki.code_graph_snapshots "
                "(iwiki_id, domain_id, snapshot_id, state, header, "
                "graph_payload_revision, markdown_revision, markdown_generation, counts) "
                "VALUES ('wiki-a', %s, 'snapshot-a', 'staging', '{}', 'g', 'm', 0, '{}')",
                (docs_id,),
            )
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                with connection.transaction():
                    cursor.execute(
                        "INSERT INTO iwiki.code_graph_domain_state "
                        "(iwiki_id, domain_id, active_snapshot_id, active_snapshot_state) "
                        "VALUES ('wiki-a', %s, 'snapshot-a', 'ready')",
                        (private_id,),
                    )


def test_owner_and_bypass_roles_are_invalid_direct_principals(role_database):
    from iwiki_mcp.postgres.store import validate_direct_principal

    assert validate_direct_principal(role_database.owner_dsn) == {
        "error": "invalid_config"
    }
    assert validate_direct_principal(role_database.bypass_dsn) == {
        "error": "invalid_config"
    }
    assert validate_direct_principal(role_database.restricted_dsn) is None


def test_hosted_and_direct_roles_are_rls_scoped(role_database):
    import psycopg

    from iwiki_mcp.postgres.store import provision_runtime_grant

    provision_runtime_grant(
        role_database.admin_dsn,
        principal=role_database.hosted_role,
        iwiki_id="wiki-a",
        read_domains=["docs"],
        write_domains=["docs"],
        runtime="hosted",
    )
    provision_runtime_grant(
        role_database.admin_dsn,
        principal=role_database.restricted_role,
        iwiki_id="wiki-a",
        read_domains=["docs"],
        write_domains=["docs"],
        runtime="direct",
    )
    assert _visible_page_slugs(role_database.hosted_dsn) == {"docs/page"}
    assert _visible_page_slugs(role_database.restricted_dsn) == {"docs/page"}
    with psycopg.connect(role_database.admin_dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT domain_id FROM iwiki.domains "
                "WHERE iwiki_id = 'wiki-a' AND slug = 'private'"
            )
            private_id = cursor.fetchone()[0]
    with psycopg.connect(role_database.unmapped_dsn) as connection:
        with connection.cursor() as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute("SELECT slug FROM iwiki.pages")
    with psycopg.connect(role_database.hosted_dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT d.domain_id FROM iwiki.domains d "
                "WHERE d.iwiki_id = 'wiki-a' AND d.slug = 'private'"
            )
            assert cursor.fetchone() is None
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with connection.transaction():
                    cursor.execute(
                        "INSERT INTO iwiki.pages (iwiki_id, domain_id, slug, markdown) "
                        "VALUES ('wiki-a', %s, 'forbidden', '# Forbidden')",
                        (private_id,),
                    )


def test_rls_is_enabled_not_forced_and_uses_one_qualified_scope_function(
    role_database,
):
    import psycopg

    with psycopg.connect(role_database.admin_dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT relname, relrowsecurity, relforcerowsecurity "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'iwiki' AND relname IN "
                "('domains', 'pages', 'chunks', 'links', 'code_graph_domain_state', "
                "'code_graph_publication_sessions', 'code_graph_snapshots', "
                "'code_graph_batches', 'code_graph_files', 'code_graph_symbols', "
                "'code_graph_relations', 'code_graph_wiki_links')"
            )
            flags = cursor.fetchall()
            assert flags and all(enabled and not forced for _, enabled, forced in flags)
            cursor.execute(
                "SELECT prosecdef, proconfig FROM pg_proc p JOIN pg_namespace n "
                "ON n.oid = p.pronamespace WHERE n.nspname = 'iwiki' "
                "AND proname = 'database_principal_can_access'"
            )
            assert cursor.fetchone() == (
                True,
                ["search_path=pg_catalog, iwiki"],
            )
