"""Provision and verify the two restricted logins used by hosted publication."""

from __future__ import annotations

import psycopg
from psycopg import sql

from .publication_schema import AUTHORIZE, CLEAN, ISSUE, SNAPSHOT_VISIBILITY, VISIBILITY


# The publication login cannot delete graph rows. Cleanup executes as the
# schema owner inside its bounded routines. Receipt removal has its row guard.
PUBLISHER_GRANTS = {
    "SELECT": (
        "domains",
        "pages",
        "code_graph_domain_state",
        "code_graph_publication_sessions",
        "code_graph_publication_attempts",
        "code_graph_snapshots",
        "code_graph_batches",
        "code_graph_files",
        "code_graph_symbols",
        "code_graph_relations",
        "code_graph_wiki_links",
    ),
    "INSERT": (
        "code_graph_domain_state",
        "code_graph_publication_sessions",
        "code_graph_publication_attempts",
        "code_graph_snapshots",
        "code_graph_batches",
        "code_graph_files",
        "code_graph_symbols",
        "code_graph_relations",
        "code_graph_wiki_links",
    ),
    "UPDATE": (
        "code_graph_domain_state",
        "code_graph_publication_sessions",
        "code_graph_publication_attempts",
        "code_graph_snapshots",
    ),
    "DELETE": ("code_graph_batches",),
}
PAIR_STATUS = "publication_pair_status()"
PUBLISHER_FUNCTIONS = (
    AUTHORIZE,
    *CLEAN,
    VISIBILITY,
    SNAPSHOT_VISIBILITY,
    "publication_now()",
    PAIR_STATUS,
    "database_principal_can_access(text,bigint,boolean)",
)
ISSUER_FUNCTIONS = (ISSUE, PAIR_STATUS)


def _lifecycle_policy(settings):
    if settings is None:
        from .config import HostedCodeGraphConfig

        settings = HostedCodeGraphConfig()
    return (
        settings.staging_retention_seconds,
        getattr(settings, "superseded_retention_seconds", 86400),
        settings.publication_session_ttl_seconds,
        settings.staging_cleanup_limit,
    )


def _set_grants(cursor, publisher, issuer, *, revoke=False):
    action = "REVOKE" if revoke else "GRANT"
    direction = "FROM" if revoke else "TO"

    def grant(privilege, objects, role):
        cursor.execute(
            sql.SQL(f"{action} {privilege} ON {objects} {direction} {{}}").format(
                sql.Identifier(role)
            )
        )

    for privilege, tables in PUBLISHER_GRANTS.items():
        grant(privilege, ", ".join("iwiki." + table for table in tables), publisher)
    grant("USAGE", "SCHEMA iwiki", publisher)
    grant("USAGE", "SEQUENCE iwiki.code_graph_domain_lock_id_seq", publisher)
    for role, functions in ((issuer, ISSUER_FUNCTIONS), (publisher, PUBLISHER_FUNCTIONS)):
        grant("EXECUTE", "FUNCTION " + ", ".join("iwiki." + name for name in functions), role)


def _validate_login(cursor, role):
    cursor.execute(
        "SELECT rolcanlogin AND NOT (rolsuper OR rolbypassrls OR rolcreaterole OR rolreplication "
        "OR rolcreatedb OR has_schema_privilege(r.oid,'iwiki','CREATE') "
        "OR has_database_privilege(r.oid,current_database(),'CREATE') "
        "OR EXISTS (SELECT FROM pg_auth_members m WHERE m.member=r.oid) "
        "OR EXISTS (SELECT FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='iwiki' AND c.relowner=r.oid) "
        "OR EXISTS (SELECT FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname='iwiki' AND p.proowner=r.oid)) FROM pg_roles r WHERE rolname=%s",
        (role,),
    )
    if cursor.fetchone() != (True,):
        raise ValueError("publication pair requires independent restricted login roles")


def _audit_publisher(cursor, role, *, installed):
    """Check effective column permissions, including inherited and PUBLIC grants."""
    cursor.execute(
        "SELECT c.relname, p.priv FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','TRIGGER',"
        "'REFERENCES']) p(priv) "
        "WHERE n.nspname='iwiki' AND c.relkind IN ('r','p','v','m','f') AND "
        "(has_table_privilege(%s,c.oid,p.priv) OR CASE WHEN p.priv IN ('SELECT','INSERT',"
        "'UPDATE','REFERENCES') "
        "THEN has_any_column_privilege(%s,c.oid,p.priv) ELSE false END)",
        (role, role),
    )
    expected = {
        (table, privilege) for privilege, tables in PUBLISHER_GRANTS.items() for table in tables
    }
    if set(cursor.fetchall()) != (expected if installed else set()):
        raise ValueError("publication login table privileges are inconsistent")
    cursor.execute(
        "SELECT p.oid::regprocedure::text FROM pg_proc p JOIN pg_namespace n ON "
        "n.oid=p.pronamespace "
        "WHERE n.nspname='iwiki' AND p.prosecdef AND has_function_privilege(%s,p.oid,'EXECUTE')",
        (role,),
    )
    actual = {row[0] for row in cursor}
    allowed = {"iwiki." + name for name in PUBLISHER_FUNCTIONS}
    if actual - (allowed if installed else set()):
        raise ValueError("publication login routine privileges are inconsistent")


def provision_publication_pair(admin_dsn, issuer, publisher, limits, *, settings=None):
    if (
        issuer == publisher
        or len(limits) != 4
        or any(type(x) is not int or not 0 < x < 2**63 for x in limits)
    ):
        raise ValueError("invalid publication pair configuration")
    with psycopg.connect(admin_dsn) as conn, conn.cursor() as cur:
        cur.execute("LOCK TABLE iwiki.publication_principals IN EXCLUSIVE MODE")
        for role in (issuer, publisher):
            _validate_login(cur, role)
        cur.execute(
            "SELECT EXISTS(SELECT FROM iwiki.database_principal_domain_grants "
            "WHERE principal=%s AND runtime='hosted') AND NOT EXISTS "
            "(SELECT FROM iwiki.database_principal_domain_grants WHERE principal=%s "
            "OR (principal=%s AND runtime='direct'))",
            (issuer, publisher, issuer),
        )
        if cur.fetchone() != (True,):
            raise ValueError("publication issuer must be a configured hosted login")
        cur.execute(
            "SELECT p.issuer=%s::regrole AND p.publisher=%s::regrole "
            "FROM iwiki.publication_principals p WHERE p.issuer IN (%s::regrole,%s::regrole) "
            "OR p.publisher IN (%s::regrole,%s::regrole)",
            tuple([issuer, publisher] * 3),
        )
        rows = cur.fetchall()
        if rows not in ([], [(True,)]):
            raise ValueError("publication pair conflicts with existing role assignments")
        _audit_publisher(cur, publisher, installed=bool(rows))
        if not rows:
            cur.execute(
                "INSERT INTO iwiki.publication_principals (issuer,publisher) "
                "VALUES (%s::regrole,%s::regrole)",
                (issuer, publisher),
            )
        _set_grants(cur, publisher, issuer)
        _audit_publisher(cur, publisher, installed=True)
        cur.execute("SELECT pg_advisory_xact_lock(7595435311942266218::bigint)")
        cur.execute(
            "UPDATE iwiki.publication_policy SET limits=%s, staging_retention_seconds=%s, "
            "superseded_retention_seconds=%s, session_ttl_seconds=%s, staging_cleanup_limit=%s "
            "WHERE singleton",
            (list(limits), *_lifecycle_policy(settings)),
        )


def require_publication_pair(issuer_dsn, publication_dsn, limits, *, settings=None):
    snapshots = []
    for dsn, member in ((issuer_dsn, "issuer"), (publication_dsn, "publisher")):
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT session_user, pair_id, member, limits, lifecycle "
                "FROM iwiki.publication_pair_status()"
            )
            row = cur.fetchone()
            if (
                not row
                or row[2] != member
                or (
                    member == "issuer"
                    and (
                        tuple(row[3]) != tuple(limits)
                        or tuple(row[4]) != _lifecycle_policy(settings)
                    )
                )
            ):
                raise ValueError("publication pair or operator policy differs from database")
            _validate_login(cur, row[0])
            if member == "publisher":
                _audit_publisher(cur, row[0], installed=True)
            snapshots.append(row[1])
    if snapshots[0] != snapshots[1]:
        raise ValueError("publication credentials belong to different pairs")
