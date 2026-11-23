"""Schema 11 requires private, transaction-bound publication accounting."""

import pytest

from iwiki_mcp.postgres.migrations import run_migrations
from iwiki_mcp.postgres.migrations import MigrationSettings

pytestmark = pytest.mark.postgres_integration


def test_quota_storage_is_private(clean_postgres):
    import psycopg

    assert (
        run_migrations(MigrationSettings(clean_postgres, "fixture", 3, 30000, 5000)).schema_version
        == 11
    )
    with psycopg.connect(clean_postgres) as conn:
        for name in ("publication_usage", "publication_tickets", "publication_authorizations"):
            assert conn.execute(
                "SELECT NOT has_table_privilege('public', %s, 'select')", ("iwiki." + name,)
            ).fetchone() == (True,)


def _login_dsn(admin_dsn, credential):
    from psycopg.conninfo import make_conninfo

    return make_conninfo(admin_dsn, user=credential[0], password=credential[1])


def test_ticket_binds_operation_principal_and_current_grant(clean_postgres, publication_pair):
    import hashlib
    import secrets

    import psycopg
    from psycopg.types.json import Jsonb

    from iwiki_mcp.postgres.auth import AuthStore
    from iwiki_mcp.postgres.publication_roles import require_publication_pair
    from iwiki_mcp.postgres.config import HostedCodeGraphConfig

    run_migrations(MigrationSettings(clean_postgres, "fixture", 3, 30000, 5000))
    auth = AuthStore(clean_postgres)
    auth.create_wiki("wiki-a", "wiki-a")
    auth.create_domain("wiki-a", "docs")
    owner = auth.create_token("wiki-a", "publisher", read_domains=["docs"], write_domains=["docs"])[
        "token_id"
    ]
    issuer, publisher = publication_pair("wiki-a", ["docs"])
    issuer_dsn = _login_dsn(clean_postgres, issuer)
    publisher_dsn = _login_dsn(clean_postgres, publisher)
    require_publication_pair(issuer_dsn, publisher_dsn, HostedCodeGraphConfig().publication_limits)
    claims = {"session_id": "absent"}
    issue_sql = "SELECT iwiki.issue_publication_ticket(%s,'wiki-a',%s,'docs','abort',%s)"
    consume_sql = "SELECT iwiki.authorize_code_publication('wiki-a',%s,%s,'abort',%s,%s)"
    with (
        psycopg.connect(issuer_dsn, autocommit=True) as mint,
        psycopg.connect(publisher_dsn, autocommit=True) as pub,
        psycopg.connect(clean_postgres, autocommit=True) as admin,
    ):
        domain = admin.execute("SELECT domain_id FROM iwiki.domains WHERE slug='docs'").fetchone()[
            0
        ]

        def issue():
            secret = secrets.token_bytes(32)
            mint.execute(issue_sql, (hashlib.sha256(secret).digest(), owner, Jsonb(claims)))
            return secret

        def consume(secret, arguments=claims, subject=owner):
            pub.execute(consume_sql, (domain, subject, Jsonb(arguments), secret))

        ticket = issue()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pub.execute(issue_sql, (hashlib.sha256(ticket).digest(), owner, Jsonb(claims)))
        for table in (
            "publication_usage",
            "publication_tickets",
            "publication_authorizations",
            "tokens",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                pub.execute("SELECT * FROM iwiki." + table)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            consume(ticket, {"session_id": "different"})
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            consume(ticket, subject="different")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            consume(None)
        with pub.transaction(force_rollback=True):
            consume(ticket)
        consume(ticket)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            consume(ticket)

        ticket = issue()
        admin.execute("UPDATE iwiki.publication_tickets SET expires_at=clock_timestamp()")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            consume(ticket)

        ticket = issue()
        admin.execute(
            "UPDATE iwiki.token_domain_grants SET can_write=false WHERE token_id=%s", (owner,)
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            consume(ticket)


def test_backfill_and_rollback_preserve_receipts_and_active_pointer(clean_postgres):
    import psycopg

    from iwiki_mcp.postgres.auth import AuthStore
    from iwiki_mcp.postgres.migrations import MIGRATIONS, rollback_v11_compatibility

    settings = MigrationSettings(clean_postgres, "fixture", 3, 30000, 5000)
    run_migrations(settings, migrations=MIGRATIONS[:-1])
    auth = AuthStore(clean_postgres)
    auth.create_wiki("wiki-a", "wiki-a")
    auth.create_domain("wiki-a", "docs")
    token = auth.create_token("wiki-a", "publisher", read_domains=["docs"], write_domains=["docs"])[
        "token_id"
    ]
    with psycopg.connect(clean_postgres) as conn:
        domain = conn.execute("SELECT domain_id FROM iwiki.domains WHERE slug='docs'").fetchone()[0]
        for sid, state, owner, lease in (
            ("terminal", "ready", token, "1 hour"),
            ("expired", "staging", "legacy", "-1 day"),
            ("empty", "staging", token, "1 hour"),
            ("live", "staging", token, "1 hour"),
        ):
            conn.execute(
                "INSERT INTO iwiki.code_graph_snapshots "
                "(iwiki_id,domain_id,snapshot_id,state,header,graph_payload_revision,"
                "markdown_revision,markdown_generation,counts) "
                "VALUES ('wiki-a',%s,%s,%s,'{}','rev','md',0,'{}')",
                (domain, sid, state),
            )
            conn.execute(
                "INSERT INTO iwiki.code_graph_publication_sessions "
                "(iwiki_id,domain_id,session_id,snapshot_id,owner_id,state,lease_expires_at,"
                "captured_markdown_generation) VALUES ('wiki-a',%s,%s,%s,%s,%s,"
                "clock_timestamp()+%s::interval,0)",
                (domain, sid, sid, owner, state, lease),
            )
            if sid != "empty":
                conn.execute(
                    "INSERT INTO iwiki.code_graph_batches "
                    "(iwiki_id,domain_id,session_id,kind,ordinal,payload_hash,row_count,"
                    "byte_count,payload) "
                    "VALUES ('wiki-a',%s,%s,'files',0,'digest',0,2,'[]')",
                    (domain, sid),
                )
        conn.execute(
            "INSERT INTO iwiki.code_graph_domain_state "
            "(iwiki_id,domain_id,active_snapshot_id,active_snapshot_state) "
            "VALUES ('wiki-a',%s,'terminal','ready')",
            (domain,),
        )

    assert run_migrations(settings).applied_versions == (11,)
    with psycopg.connect(clean_postgres) as conn:
        assert conn.execute(
            "SELECT owner_kind,count(*) FILTER (WHERE is_staging AND expires_at>now()),"
            "sum(charged_bytes) FROM iwiki.publication_usage GROUP BY owner_kind "
            "ORDER BY owner_kind"
        ).fetchall() == [("hosted", 2, 4), ("legacy", 0, 2)]
        assert (
            conn.execute(
                "SELECT s.session_id FROM iwiki.code_graph_publication_sessions s "
                "JOIN iwiki.publication_usage u USING(iwiki_id,domain_id,session_id) "
                "LEFT JOIN iwiki.code_graph_batches b USING(iwiki_id,domain_id,session_id) "
                "GROUP BY s.session_id,u.charged_bytes HAVING u.charged_bytes<>coalesce(sum("
                "b.byte_count),0)"
            ).fetchall()
            == []
        )
        conn.execute(
            "INSERT INTO iwiki.code_graph_batches "
            "(iwiki_id,domain_id,session_id,kind,ordinal,payload_hash,row_count,byte_count,"
            "payload) "
            "VALUES ('wiki-a',%s,'live','files',1,'post-upgrade',0,2,'[]')",
            (domain,),
        )
    assert rollback_v11_compatibility(settings, confirm=True)["schema_version"] == 10
    with psycopg.connect(clean_postgres) as conn:
        assert conn.execute("SELECT count(*) FROM iwiki.code_graph_batches").fetchone() == (4,)
        assert conn.execute(
            "SELECT active_snapshot_id FROM iwiki.code_graph_domain_state"
        ).fetchone() == ("terminal",)
    assert run_migrations(settings).applied_versions == (11,)


def test_rollback_rehearses_normal_publication_and_removes_publisher_rights(
    pg_graph, clean_postgres, publication_pair
):
    import psycopg

    from iwiki_mcp.postgres.migrations import rollback_v11_compatibility

    issuer, publisher = publication_pair("wiki-a", ["docs"])
    active = pg_graph.finalize(pg_graph.complete_session())
    staging = pg_graph.store.begin(
        pg_graph.header,
        attempt_key="rollback-livesession",
        max_batch_rows=1000,
        max_batch_bytes=1_000_000,
        recovery_deadline=int(pg_graph._now().timestamp()) + 300,
    )
    replaced = pg_graph.begin()
    pg_graph.publish_batch(staging, pg_graph.batches[0])
    settings = MigrationSettings(clean_postgres, "fixture-model", 3, 30000, 5000)
    rollback_v11_compatibility(settings, confirm=True)
    with psycopg.connect(_login_dsn(clean_postgres, publisher)) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM iwiki.code_graph_publication_sessions")
    assert pg_graph.reader_status()["snapshot_revision"] == active["snapshot_revision"]
    assert pg_graph.batch_count(staging) == 1
    with psycopg.connect(clean_postgres) as conn:
        conn.execute(
            "CREATE TEMP TABLE tmp_session AS SELECT * FROM "
            "iwiki.code_graph_publication_sessions WHERE session_id=%s",
            (replaced.session_id,),
        )
        conn.execute(
            "DELETE FROM iwiki.code_graph_publication_sessions WHERE session_id=%s",
            (replaced.session_id,),
        )
        conn.execute("INSERT INTO iwiki.code_graph_publication_sessions SELECT * FROM tmp_session")
    assert run_migrations(settings).applied_versions == (11,)
    with psycopg.connect(clean_postgres) as conn:
        assert conn.execute(
            "SELECT owner_kind FROM iwiki.publication_usage WHERE session_id=%s",
            (staging.session_id,),
        ).fetchone() == ("direct",)
        assert conn.execute(
            "SELECT quota_owner_kind FROM iwiki.code_graph_publication_attempts "
            "WHERE session_id=%s",
            (staging.session_id,),
        ).fetchone() == ("direct",)
        assert conn.execute(
            "SELECT owner_kind FROM iwiki.publication_usage WHERE session_id=%s",
            (replaced.session_id,),
        ).fetchone() == ("legacy",)
    assert pg_graph.reader_status()["snapshot_revision"] == active["snapshot_revision"]
    assert pg_graph.batch_count(staging) == 1
