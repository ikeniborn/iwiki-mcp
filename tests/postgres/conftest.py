"""Fixtures for explicitly provisioned PostgreSQL integration tests."""
from __future__ import annotations

import os
import json
import secrets

import pytest

from iwiki_mcp.engine.config import Config


class SecretDsn(str):
    """Connection string whose pytest representation never reveals credentials."""

    def __repr__(self):
        return "<redacted PostgreSQL test DSN>"


class RedactedEnv(dict):
    """Environment mapping whose pytest representation never exposes secrets."""

    def __repr__(self):
        return "<redacted HTTP environment>"


class HostedFixture:
    """HTTP fixture whose pytest representation never reveals Bearer tokens."""

    def __init__(self, runtime, auth, token, revoked, disabled):
        self.runtime = runtime
        self.auth = auth
        self.token = token
        self.revoked = revoked
        self.disabled = disabled

    def __repr__(self):
        return "<redacted hosted HTTP fixture>"


def _cfg():
    return Config(
        base_url="http://example.invalid/v1",
        api_key="test",
        embed_model="test-embedding",
        dimensions=3,
        chunk_size=512,
        chunk_overlap=64,
        summary_max=400,
        top_k=8,
        score_threshold=0.0,
        graph_depth=2,
        ignore=None,
        seed_top_k=2,
        bfs_top_k=10,
        seed_threshold=0.0,
    )


def _embed(_cfg, texts):
    vectors = []
    for text in texts:
        lowered = text.lower()
        if "alpha" in lowered:
            vectors.append([1.0, 0.0, 0.0])
        elif "beta" in lowered:
            vectors.append([0.0, 1.0, 0.0])
        else:
            vectors.append([0.0, 0.0, 1.0])
    return vectors


def _write_server_config(path, dsn_values, *, storage_type="postgres"):
    if storage_type == "postgres":
        storage = (
            "[storage]\n"
            "type = \"postgres\"\n"
            f"host = {json.dumps(dsn_values['host'])}\n"
            f"port = {int(dsn_values.get('port', 5432))}\n"
            f"database = {json.dumps(dsn_values['dbname'])}\n"
            f"user = {json.dumps(dsn_values['user'])}\n"
            f"sslmode = {json.dumps(dsn_values.get('sslmode', 'prefer'))}\n"
        )
    else:
        storage = "[storage]\ntype = \"git\"\n"
    path.write_text(
        storage
        + "\n[server]\n"
        "host = \"127.0.0.1\"\n"
        "port = 8765\n"
        "allowed_origins = [\"https://iwiki.example\"]\n"
        "pool_min_size = 1\n"
        "pool_max_size = 2\n"
        "statement_timeout_ms = 30000\n"
        "lock_timeout_ms = 5000\n",
        encoding="utf-8",
    )


def validated_test_dsn(value: str) -> SecretDsn:
    """Validate the disposable target before any network operation."""
    from psycopg.conninfo import conninfo_to_dict

    database = conninfo_to_dict(value).get("dbname", "")
    if not database.endswith("_test"):
        raise ValueError("IWIKI_TEST_POSTGRES_DSN database must end in _test")
    return SecretDsn(value)


@pytest.fixture(scope="session")
def postgres_dsn():
    raw_dsn = os.environ.get("IWIKI_TEST_POSTGRES_DSN", "").strip()
    if not raw_dsn:
        pytest.skip("IWIKI_TEST_POSTGRES_DSN is not set")

    import psycopg

    try:
        dsn = validated_test_dsn(raw_dsn)
    except (ValueError, psycopg.ProgrammingError) as exc:
        pytest.fail(str(exc))

    try:
        with psycopg.connect(dsn, connect_timeout=10) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
                    ")"
                )
                vector_enabled = cursor.fetchone()[0]
    except psycopg.Error as exc:
        pytest.fail(
            f"disposable PostgreSQL database is unavailable: {type(exc).__name__}"
        )
    if not vector_enabled:
        pytest.fail("disposable PostgreSQL database must enable pgvector")
    return dsn


@pytest.fixture
def clean_postgres(postgres_dsn):
    import psycopg

    with psycopg.connect(postgres_dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS iwiki CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS iwiki_test_probe CASCADE")
    yield postgres_dsn
    with psycopg.connect(postgres_dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS iwiki CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS iwiki_test_probe CASCADE")


def create_runtime_role(admin_dsn, *, prefix):
    """Create one disposable non-owner login role and return its credentials."""
    import psycopg
    from psycopg import sql

    role = f"iwiki_t3_{prefix}_{secrets.token_hex(5)}"
    password = secrets.token_urlsafe(24)
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            try:
                cursor.execute(
                    sql.SQL("CREATE ROLE {} LOGIN NOBYPASSRLS PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
            except psycopg.Error:
                pytest.fail("disposable PostgreSQL role setup failed")
    return role, password


def drop_runtime_role(admin_dsn, role):
    """Drop one disposable role and everything it owns."""
    import psycopg
    from psycopg import sql

    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP OWNED BY {} CASCADE").format(sql.Identifier(role))
            )
            cursor.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


@pytest.fixture
def runtime_principal(clean_postgres):
    """Provision disposable hosted/direct principals against existing domains."""
    from iwiki_mcp.postgres.store import provision_runtime_grant

    created = []

    def provision(read_domains, write_domains=(), *, runtime="direct", iwiki_id="wiki-a"):
        role, password = create_runtime_role(clean_postgres, prefix=runtime)
        created.append(role)
        provision_runtime_grant(
            clean_postgres,
            principal=role,
            iwiki_id=iwiki_id,
            read_domains=list(read_domains),
            write_domains=list(write_domains),
            runtime=runtime,
        )
        return role, password

    yield provision

    for role in created:
        drop_runtime_role(clean_postgres, role)


@pytest.fixture
def store_factory(clean_postgres):
    from iwiki_mcp.postgres.migrations import MigrationSettings, run_migrations
    from iwiki_mcp.postgres.store import PostgresStore

    cfg = _cfg()
    run_migrations(
        MigrationSettings(
            dsn=clean_postgres,
            embed_model=cfg.embed_model,
            embed_dimensions=cfg.dimensions,
            statement_timeout_ms=30_000,
            lock_timeout_ms=5_000,
        )
    )

    def factory(iwiki_id="wiki-a", *, embedder=_embed):
        store = PostgresStore(
            clean_postgres,
            iwiki_id,
            cfg,
            embedder=embedder,
        )
        store.create_wiki(iwiki_id)
        store.create_domain("docs")
        return store

    return factory


@pytest.fixture
def hosted_runtime(clean_postgres, tmp_path, monkeypatch):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict

    from iwiki_mcp import http, server
    from iwiki_mcp.postgres.auth import AuthStore
    from iwiki_mcp.postgres.migrations import MigrationSettings, run_migrations
    from iwiki_mcp.postgres.store import provision_runtime_grant

    values = conninfo_to_dict(clean_postgres)
    run_migrations(
        MigrationSettings(
            dsn=clean_postgres,
            embed_model="fixture-model",
            embed_dimensions=3,
            statement_timeout_ms=30_000,
            lock_timeout_ms=5_000,
        )
    )
    auth = AuthStore(clean_postgres)
    auth.create_wiki("wiki-a", "wiki-a")
    auth.create_domain("wiki-a", "docs")
    auth.create_domain("wiki-a", "private")
    token = auth.create_token(
        "wiki-a",
        "alice",
        read_domains=["docs"],
        write_domains=["docs"],
    )["token"]
    revoked = auth.create_token(
        "wiki-a",
        "revoked",
        read_domains=["docs"],
        write_domains=[],
    )
    auth.revoke_token(revoked["token_id"])
    disabled = auth.create_token(
        "wiki-a",
        "disabled",
        read_domains=["docs"],
        write_domains=[],
    )["token"]
    hosted_role = f"iwiki_t3_hosted_{secrets.token_hex(5)}"
    hosted_password = secrets.token_urlsafe(24)
    with psycopg.connect(clean_postgres, autocommit=True) as connection:
        with connection.cursor() as cursor:
            try:
                cursor.execute(
                    sql.SQL("CREATE ROLE {} LOGIN NOBYPASSRLS PASSWORD {}").format(
                        sql.Identifier(hosted_role), sql.Literal(hosted_password)
                    )
                )
            except psycopg.Error:
                pytest.fail("disposable hosted PostgreSQL role setup failed")
    provision_runtime_grant(
        clean_postgres,
        principal=hosted_role,
        iwiki_id="wiki-a",
        read_domains=["docs", "private"],
        write_domains=["docs", "private"],
        runtime="hosted",
    )
    runtime_values = {
        **values,
        "user": hosted_role,
        "password": hosted_password,
    }
    config_path = tmp_path / "server.toml"
    _write_server_config(config_path, runtime_values)
    environ = RedactedEnv(
        {
            "IWIKI_DB_PASSWORD": hosted_password,
            "IWIKI_LLM_BASE_URL": "http://provider.internal/v1",
            "IWIKI_LLM_KEY": "server-only-model-key",
            "IWIKI_EMBED_MODEL": "fixture-model",
            "IWIKI_EMBED_DIMENSIONS": "3",
            "IWIKI_RERANK_MODEL": "",
        }
    )
    for name, value in environ.items():
        monkeypatch.setenv(name, value)

    server.mcp._session_manager = None
    runtime = http.prepare_runtime(
        str(config_path), environ=environ, probe=lambda _cfg: None
    )

    yield HostedFixture(runtime, auth, token, revoked["token"], disabled)

    auth.set_wiki_active("wiki-a", True)
    runtime.close()
    server.mcp._session_manager = None
    with psycopg.connect(clean_postgres, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP OWNED BY {} CASCADE").format(
                    sql.Identifier(hosted_role)
                )
            )
            cursor.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(hosted_role)))
