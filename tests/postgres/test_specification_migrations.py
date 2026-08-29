"""PostgreSQL specification schema, rollback, and RLS contract."""
from __future__ import annotations

import pytest


pytestmark = pytest.mark.postgres_integration


_SPECIFICATION_TABLE_FLAGS_SQL = (
    "SELECT relname, relrowsecurity, relforcerowsecurity "
    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'iwiki' AND c.relkind = 'r' "
    "AND relname LIKE 'specification_%' "
    "ORDER BY relname"
)


def _settings(dsn):
    from iwiki_mcp.postgres.migrations import MigrationSettings

    return MigrationSettings(
        dsn=dsn,
        embed_model="fixture-model",
        embed_dimensions=3,
        statement_timeout_ms=30_000,
        lock_timeout_ms=5_000,
    )


def _seed_scenario(
    cursor, *, iwiki_id, domain_id, page_id, scenario_id, page_slug="spec"
):
    cursor.execute(
        "INSERT INTO iwiki.specification_scenarios "
        "(iwiki_id, domain_id, scenario_id, page_id, page_slug, title, heading, "
        "anchor, source_hash, items, page_revision) VALUES "
        "(%s, %s, %s, %s, %s, %s, 'Behavior', 'behavior', %s, '[]', 1)",
        (
            iwiki_id,
            domain_id,
            scenario_id,
            page_id,
            page_slug,
            f"Title {scenario_id}",
            "a" * 64,
        ),
    )


def test_v6_is_append_only_and_separate_from_code_graph_schema():
    from iwiki_mcp.postgres.migrations import MIGRATIONS
    from iwiki_mcp.postgres.store import _PROTECTED_TABLES

    assert tuple(item.version for item in MIGRATIONS) == (1, 2, 3, 4, 5, 6, 7)
    protected = set(_PROTECTED_TABLES)
    assert {
        "specification_scenarios",
        "specification_bindings",
        "specification_evidence",
        "specification_projection_state",
    } <= protected
    statements = "\n".join(MIGRATIONS[-1].statements).lower()
    assert "force row level security" not in statements
    assert "code_graph_" not in statements


def test_v7_metadata_migration_is_scoped_and_has_no_invented_backfill():
    from iwiki_mcp.postgres.migrations import MIGRATIONS

    statements = " ".join("\n".join(MIGRATIONS[6].statements).lower().split())
    assert "add column page_slug text" in statements
    assert "alter column page_slug set not null" in statements
    assert "alter column page_id drop not null" in statements
    assert "on delete set null (page_id)" in statements
    assert "create table iwiki.specification_projection_state" in statements
    assert "check (scenario_count >= 0)" in statements
    assert "check (binding_count >= 0)" in statements
    assert "insert into iwiki.specification_projection_state" not in statements
    assert "code_graph_" not in statements


def test_v7_metadata_uses_command_specific_read_and_write_rls_policies():
    from iwiki_mcp.postgres.migrations import MIGRATIONS

    statements = " ".join("\n".join(MIGRATIONS[6].statements).lower().split())
    table = "iwiki.specification_projection_state"
    assert f"create policy database_principal_select on {table} for select" in statements
    assert f"create policy database_principal_insert on {table} for insert" in statements
    assert f"create policy database_principal_update on {table} for update" in statements
    assert f"create policy database_principal_delete on {table} for delete" in statements
    assert statements.count(
        "database_principal_can_access(iwiki_id, domain_id, false)"
    ) == 1
    assert statements.count(
        "database_principal_can_access(iwiki_id, domain_id, true)"
    ) == 4


def test_v7_rollback_sql_is_reviewable_fail_closed_and_scoped():
    from iwiki_mcp.postgres.migrations import SCHEMA7_COMPATIBILITY_ROLLBACK_SQL

    normalized = " ".join(SCHEMA7_COMPATIBILITY_ROLLBACK_SQL.lower().split())
    assert "pg_advisory_xact_lock" in normalized
    assert "where page_id is null" in normalized
    assert "schema v7 contains detached specification rows" in normalized
    assert "drop table iwiki.specification_projection_state" in normalized
    assert "alter column page_id set not null" in normalized
    assert "on delete cascade" in normalized
    assert "drop column page_slug" in normalized
    assert "delete from iwiki.schema_migrations where version = 7" in normalized
    assert "code_graph" not in normalized
    assert "drop schema" not in normalized


@pytest.mark.parametrize("confirm", ["true", 1, object(), False, None])
def test_v7_compatibility_rollback_requires_literal_true_without_sql(
    monkeypatch, confirm
):
    from iwiki_mcp.postgres import migrations

    def fail_connect(*_args, **_kwargs):
        pytest.fail("invalid confirmation must reject before SQL")

    monkeypatch.setattr(migrations.psycopg, "connect", fail_connect)
    with pytest.raises(ValueError, match="confirmation must be literal true"):
        migrations.rollback_v7_compatibility(
            _settings("not-used"), confirm=confirm
        )


def test_v6_uses_command_specific_read_and_write_rls_policies():
    from iwiki_mcp.postgres.migrations import MIGRATIONS

    statements = " ".join("\n".join(MIGRATIONS[5].statements).lower().split())
    for table in (
        "specification_scenarios",
        "specification_bindings",
        "specification_evidence",
    ):
        select = f"create policy database_principal_select on iwiki.{table}"
        insert = f"create policy database_principal_insert on iwiki.{table}"
        update = f"create policy database_principal_update on iwiki.{table}"
        delete = f"create policy database_principal_delete on iwiki.{table}"
        assert f"{select} for select using" in statements
        assert f"{insert} for insert with check" in statements
        assert f"{update} for update using" in statements
        assert f"{delete} for delete using" in statements
    assert statements.count(
        "database_principal_can_access(iwiki_id, domain_id, false)"
    ) == 3
    assert statements.count(
        "database_principal_can_access(iwiki_id, domain_id, true)"
    ) == 12


def test_rls_catalog_query_matches_only_ordinary_tables():
    assert "c.relkind = 'r'" in _SPECIFICATION_TABLE_FLAGS_SQL


def test_v6_creates_composite_integrity_rls_and_search_indexes(clean_postgres):
    import psycopg

    from iwiki_mcp.postgres.migrations import run_migrations

    run_migrations(_settings(clean_postgres))
    with psycopg.connect(clean_postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute(_SPECIFICATION_TABLE_FLAGS_SQL)
            flags = cursor.fetchall()
            cursor.execute(
                "SELECT conname FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "WHERE n.nspname = 'iwiki' AND t.relname LIKE 'specification_%'"
            )
            constraints = {row[0] for row in cursor.fetchall()}
            cursor.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'iwiki' "
                "AND tablename LIKE 'specification_%'"
            )
            indexes = {row[0] for row in cursor.fetchall()}

    assert flags == [
        ("specification_bindings", True, False),
        ("specification_evidence", True, False),
        ("specification_projection_state", True, False),
        ("specification_scenarios", True, False),
    ]
    assert {
        "specification_scenarios_domain_fk",
        "specification_scenarios_page_fk",
        "specification_scenarios_domain_identity_key",
        "specification_bindings_scenario_fk",
        "specification_evidence_binding_fk",
    } <= constraints
    assert {
        "specification_scenarios_title_idx",
        "specification_scenarios_items_idx",
        "specification_bindings_selector_idx",
    } <= indexes


def test_v6_to_v7_backfills_slug_detaches_rows_and_does_not_invent_state(
    clean_postgres,
):
    import psycopg

    from iwiki_mcp.postgres.migrations import MIGRATIONS, run_migrations

    settings = _settings(clean_postgres)
    run_migrations(settings, migrations=MIGRATIONS[:6])
    with psycopg.connect(clean_postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO iwiki.iwikis VALUES ('wiki-a', 'wiki-a')")
            cursor.execute(
                "INSERT INTO iwiki.domains (iwiki_id, slug) "
                "VALUES ('wiki-a', 'docs') RETURNING domain_id"
            )
            domain_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO iwiki.pages (iwiki_id, domain_id, slug, markdown) "
                "VALUES ('wiki-a', %s, 'payments/open-account', '# Spec') "
                "RETURNING page_id",
                (domain_id,),
            )
            page_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO iwiki.specification_scenarios "
                "(iwiki_id, domain_id, scenario_id, page_id, title, heading, "
                "anchor, source_hash, items, page_revision) VALUES "
                "('wiki-a', %s, 'stable-id', %s, 'Stable', 'Behavior', "
                "'behavior', %s, '[]', 1)",
                (domain_id, page_id, "a" * 64),
            )
            cursor.execute(
                "INSERT INTO iwiki.specification_bindings "
                "(iwiki_id, domain_id, scenario_id, binding_id, relation, phase, "
                "selector_kind, selector) VALUES "
                "('wiki-a', %s, 'stable-id', 'binding-a', 'implements', "
                "'when', 'symbol', 'app.handle')",
                (domain_id,),
            )
            cursor.execute(
                "INSERT INTO iwiki.specification_evidence "
                "(iwiki_id, domain_id, scenario_id, binding_id, state, targets, "
                "unresolved_reference, graph_revision, graph_state_fingerprint, "
                "specification_source_hash, checked_at, reason) VALUES "
                "('wiki-a', %s, 'stable-id', 'binding-a', 'unresolved', '[]', "
                "'app.handle', 'graph-1', %s, %s, CURRENT_TIMESTAMP, NULL)",
                (domain_id, "sha256:" + "b" * 64, "a" * 64),
            )

    result = run_migrations(settings)
    assert result.applied_versions == (7,)
    with psycopg.connect(clean_postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM iwiki.specification_projection_state"
            )
            assert cursor.fetchone() == (0,)
            cursor.execute(
                "DELETE FROM iwiki.pages WHERE iwiki_id = 'wiki-a' "
                "AND domain_id = %s AND page_id = %s",
                (domain_id, page_id),
            )
            cursor.execute(
                "SELECT page_id, page_slug FROM iwiki.specification_scenarios"
            )
            assert cursor.fetchone() == (None, "payments/open-account")
            cursor.execute(
                "SELECT (SELECT count(*) FROM iwiki.specification_bindings), "
                "(SELECT count(*) FROM iwiki.specification_evidence)"
            )
            assert cursor.fetchone() == (1, 1)


def test_v7_catalog_has_nullable_page_fk_metadata_checks_and_no_force_rls(
    clean_postgres,
):
    import psycopg

    from iwiki_mcp.postgres.migrations import run_migrations

    run_migrations(_settings(clean_postgres))
    with psycopg.connect(clean_postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'iwiki' "
                "AND table_name = 'specification_scenarios' "
                "AND column_name = 'page_id'"
            )
            assert cursor.fetchone() == ("YES",)
            cursor.execute(
                "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "WHERE n.nspname = 'iwiki' "
                "AND t.relname = 'specification_scenarios' "
                "AND c.conname = 'specification_scenarios_page_fk'"
            )
            assert "ON DELETE SET NULL (page_id)" in cursor.fetchone()[0]
            cursor.execute(_SPECIFICATION_TABLE_FLAGS_SQL)
            assert ("specification_projection_state", True, False) in cursor.fetchall()
            cursor.execute(
                "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "WHERE n.nspname = 'iwiki' "
                "AND t.relname = 'specification_projection_state'"
            )
            definitions = " ".join(row[0] for row in cursor.fetchall())
            assert "scenario_count >= 0" in definitions
            assert "binding_count >= 0" in definitions


def test_v6_rejects_cross_domain_page_and_cascades_derived_rows(clean_postgres):
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
                "INSERT INTO iwiki.pages (iwiki_id, domain_id, slug, markdown) "
                "VALUES ('wiki-a', %s, 'spec', '# Spec') RETURNING page_id",
                (docs_id,),
            )
            page_id = cursor.fetchone()[0]
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                with connection.transaction():
                    _seed_scenario(
                        cursor,
                        iwiki_id="wiki-a",
                        domain_id=private_id,
                        page_id=page_id,
                        scenario_id="wrong-domain",
                    )
            _seed_scenario(
                cursor,
                iwiki_id="wiki-a",
                domain_id=docs_id,
                page_id=page_id,
                scenario_id="stable-id",
            )
            cursor.execute(
                "INSERT INTO iwiki.specification_bindings "
                "(iwiki_id, domain_id, scenario_id, binding_id, relation, phase, "
                "selector_kind, selector) VALUES "
                "('wiki-a', %s, 'stable-id', 'binding-a', 'implements', 'when', "
                "'symbol', 'app.handle')",
                (docs_id,),
            )
            cursor.execute(
                "INSERT INTO iwiki.specification_evidence "
                "(iwiki_id, domain_id, scenario_id, binding_id, state, targets, "
                "unresolved_reference, graph_revision, graph_state_fingerprint, "
                "specification_source_hash, checked_at, reason) VALUES "
                "('wiki-a', %s, 'stable-id', 'binding-a', 'resolved', "
                "'[\"symbol-a\"]', NULL, 'graph-1', %s, %s, CURRENT_TIMESTAMP, NULL)",
                (docs_id, "sha256:" + "b" * 64, "a" * 64),
            )
            cursor.execute(
                "DELETE FROM iwiki.specification_scenarios "
                "WHERE iwiki_id = 'wiki-a' AND domain_id = %s "
                "AND scenario_id = 'stable-id'",
                (docs_id,),
            )
            cursor.execute(
                "SELECT (SELECT count(*) FROM iwiki.specification_bindings), "
                "(SELECT count(*) FROM iwiki.specification_evidence)"
            )
            assert cursor.fetchone() == (0, 0)


def test_failed_v6_migration_rolls_back_objects_and_marker(clean_postgres):
    import psycopg

    from iwiki_mcp.postgres.migrations import (
        MIGRATIONS,
        Migration,
        MigrationError,
        run_migrations,
    )

    run_migrations(_settings(clean_postgres), migrations=MIGRATIONS[:5])
    broken_v6 = Migration(
        version=6,
        statements=MIGRATIONS[5].statements + ("THIS IS NOT VALID SQL",),
    )
    with pytest.raises(MigrationError, match="migration failed"):
        run_migrations(
            _settings(clean_postgres), migrations=MIGRATIONS[:5] + (broken_v6,)
        )

    with psycopg.connect(clean_postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT to_regclass('iwiki.specification_scenarios'), "
                "max(version) FROM iwiki.schema_migrations"
            )
            assert cursor.fetchone() == (None, 5)


def test_v6_compatibility_rollback_drops_only_v6_and_reapplies(clean_postgres):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from tests.postgres.conftest import create_runtime_role, drop_runtime_role

    from iwiki_mcp.postgres.migrations import (
        require_schema_version,
        rollback_v6_compatibility,
        rollback_v7_compatibility,
        run_migrations,
    )
    from iwiki_mcp.postgres.store import provision_runtime_grant

    settings = _settings(clean_postgres)
    run_migrations(settings)
    with psycopg.connect(clean_postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO iwiki.iwikis VALUES ('wiki-a', 'wiki-a')"
            )
            cursor.execute(
                "INSERT INTO iwiki.domains (iwiki_id, slug) "
                "VALUES ('wiki-a', 'docs')"
            )
    role, password = create_runtime_role(clean_postgres, prefix="spec-reapply")
    try:
        provision_runtime_grant(
            clean_postgres,
            principal=role,
            iwiki_id="wiki-a",
            read_domains=["docs"],
            write_domains=["docs"],
            runtime="direct",
        )
        assert rollback_v7_compatibility(settings, confirm=True) == {
            "dry_run": False,
            "schema_version": 6,
            "removed_marker": 7,
        }
        result = rollback_v6_compatibility(settings, confirm=True)
        assert result == {
            "dry_run": False,
            "schema_version": 5,
            "removed_marker": 6,
        }
        require_schema_version(clean_postgres, expected_version=5)
        with psycopg.connect(clean_postgres) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT to_regclass('iwiki.specification_scenarios'), "
                    "to_regclass('iwiki.code_graph_snapshots')"
                )
                assert cursor.fetchone() == (None, "iwiki.code_graph_snapshots")

        reapplied = run_migrations(settings)
        assert reapplied.applied_versions == (6, 7)
        require_schema_version(clean_postgres, expected_version=7)
        provision_runtime_grant(
            clean_postgres,
            principal=role,
            iwiki_id="wiki-a",
            read_domains=["docs"],
            write_domains=["docs"],
            runtime="direct",
        )
        values = conninfo_to_dict(clean_postgres)
        role_dsn = make_conninfo(
            **{**values, "user": role, "password": password}
        )
        with psycopg.connect(role_dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM iwiki.specification_scenarios"
                )
                assert cursor.fetchone() == (0,)
        with psycopg.connect(clean_postgres) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT max(version), rolbypassrls "
                    "FROM iwiki.schema_migrations, pg_roles "
                    "WHERE rolname = %s GROUP BY rolbypassrls",
                    (role,),
                )
                assert cursor.fetchone() == (7, False)
    finally:
        drop_runtime_role(clean_postgres, role)


def test_v6_rollback_sql_is_reviewable_and_scoped():
    from iwiki_mcp.postgres.migrations import SCHEMA6_COMPATIBILITY_ROLLBACK_SQL

    normalized = " ".join(SCHEMA6_COMPATIBILITY_ROLLBACK_SQL.lower().split())
    assert "pg_advisory_xact_lock" in normalized
    assert "delete from iwiki.schema_migrations where version = 6" in normalized
    for table in (
        "specification_evidence",
        "specification_bindings",
        "specification_scenarios",
    ):
        assert f"drop table iwiki.{table}" in normalized
    assert "code_graph" not in normalized
    assert "drop schema" not in normalized


@pytest.mark.parametrize("confirm", ["false", 1, object(), False])
def test_v6_compatibility_rollback_requires_literal_true_without_sql(
    monkeypatch, confirm
):
    from iwiki_mcp.postgres import migrations

    def fail_connect(*_args, **_kwargs):
        pytest.fail("invalid confirmation must reject before SQL")

    monkeypatch.setattr(migrations.psycopg, "connect", fail_connect)
    with pytest.raises(ValueError, match="confirmation must be literal true"):
        migrations.rollback_v6_compatibility(
            _settings("not-used"), confirm=confirm
        )


def test_v7_compatibility_rollback_restores_v6_and_reapplies(clean_postgres):
    import psycopg

    from iwiki_mcp.postgres.migrations import (
        require_schema_version,
        rollback_v7_compatibility,
        run_migrations,
    )

    settings = _settings(clean_postgres)
    run_migrations(settings)
    result = rollback_v7_compatibility(settings, confirm=True)

    assert result == {
        "dry_run": False,
        "schema_version": 6,
        "removed_marker": 7,
    }
    require_schema_version(clean_postgres, expected_version=6)
    with psycopg.connect(clean_postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT to_regclass('iwiki.specification_projection_state'), "
                "is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'iwiki' "
                "AND table_name = 'specification_scenarios' "
                "AND column_name = 'page_id'"
            )
            assert cursor.fetchone() == (None, "NO")
            cursor.execute(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema = 'iwiki' "
                "AND table_name = 'specification_scenarios' "
                "AND column_name = 'page_slug'"
            )
            assert cursor.fetchone() == (0,)

    reapplied = run_migrations(settings)
    assert reapplied.applied_versions == (7,)
    require_schema_version(clean_postgres, expected_version=7)


def test_v7_compatibility_rollback_rejects_detached_rows_without_ddl(
    clean_postgres,
):
    import psycopg

    from iwiki_mcp.postgres.migrations import (
        MigrationError,
        rollback_v7_compatibility,
        run_migrations,
    )

    settings = _settings(clean_postgres)
    run_migrations(settings)
    with psycopg.connect(clean_postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO iwiki.iwikis VALUES ('wiki-a', 'wiki-a')")
            cursor.execute(
                "INSERT INTO iwiki.domains (iwiki_id, slug) "
                "VALUES ('wiki-a', 'docs') RETURNING domain_id"
            )
            domain_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO iwiki.pages (iwiki_id, domain_id, slug, markdown) "
                "VALUES ('wiki-a', %s, 'spec', '# Spec') RETURNING page_id",
                (domain_id,),
            )
            page_id = cursor.fetchone()[0]
            _seed_scenario(
                cursor,
                iwiki_id="wiki-a",
                domain_id=domain_id,
                page_id=page_id,
                scenario_id="stable-id",
            )
            cursor.execute(
                "DELETE FROM iwiki.pages WHERE iwiki_id = 'wiki-a' "
                "AND page_id = %s",
                (page_id,),
            )

    with pytest.raises(
        MigrationError, match="schema v7 contains detached specification rows"
    ):
        rollback_v7_compatibility(settings, confirm=True)

    with psycopg.connect(clean_postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT max(version), "
                "to_regclass('iwiki.specification_projection_state'), "
                "page_id, page_slug FROM iwiki.schema_migrations, "
                "iwiki.specification_scenarios "
                "GROUP BY page_id, page_slug"
            )
            assert cursor.fetchone() == (
                7,
                "iwiki.specification_projection_state",
                None,
                "spec",
            )
