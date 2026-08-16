"""Fixtures for explicitly provisioned PostgreSQL integration tests."""
from __future__ import annotations

from contextlib import contextmanager
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


class GraphFixture:
    """Deterministic publication driver over one restricted PostgreSQL role."""

    session_ttl_seconds = 30
    staging_retention_seconds = 60
    staging_cleanup_limit = 2
    lock_timeout_ms = 500

    def __init__(
        self, dsn, admin_dsn, iwiki_id, domain, *, owner_id=None, rows=None
    ):
        from iwiki_mcp.codegraph import publication
        from iwiki_mcp.postgres.codegraph import PostgresCodeGraphStore

        self.dsn = dsn
        self.admin_dsn = admin_dsn
        self.iwiki_id = iwiki_id
        self.domain = domain
        self.owner_id = owner_id or f"owner-{secrets.token_hex(4)}"
        self._offset = 0.0
        self.rows = _graph_rows(domain) if rows is None else rows
        self.expected_counts = {
            kind: len(rows) for kind, rows in self.rows.items()
        }
        self.header = publication.SnapshotHeader(
            protocol_version=1,
            schema_version=2,
            repository_id=domain,
            source_fingerprint="source-fixture",
            parser_fingerprint="parser-fixture",
            normalizer_version="normalizer-1",
            unicode_data_version="15.1",
            languages=("python",),
            expected_counts=self.expected_counts,
            graph_payload_revision=publication.graph_payload_revision(self.rows),
        )
        self.batches = tuple(
            publication.iter_snapshot_batches(
                self.rows, max_rows=1000, max_bytes=1_000_000
            )
        )
        self.store = PostgresCodeGraphStore(
            dsn,
            iwiki_id,
            domain,
            self.owner_id,
            lock_timeout_ms=self.lock_timeout_ms,
            session_ttl_seconds=self.session_ttl_seconds,
            staging_retention_seconds=self.staging_retention_seconds,
            staging_cleanup_limit=self.staging_cleanup_limit,
            clock=self._now,
        )

    def __repr__(self):
        return f"<graph fixture {self.iwiki_id}/{self.domain}>"

    def _now(self):
        import datetime

        return datetime.datetime.now(datetime.timezone.utc) + (
            datetime.timedelta(seconds=self._offset)
        )

    def advance_clock(self, seconds):
        self._offset += seconds

    def header_with_revision(self, revision):
        from dataclasses import replace

        return replace(self.header, graph_payload_revision=revision)

    def tampered(self, batch):
        from dataclasses import replace

        from iwiki_mcp.codegraph.canonical import canonical_bytes_sha256

        payload = batch.payload[:-1] + b" ]"
        return replace(
            batch,
            payload=payload,
            byte_count=len(payload),
            payload_hash=canonical_bytes_sha256(payload, prefix=True),
        )

    def begin(self, header=None):
        return self.store.begin(header or self.header)

    def publish_batch(self, session, batch):
        return self.store.publish_batch(session, batch)

    def finalize(self, session):
        return self.store.finalize(session)

    def abort(self, session):
        return self.store.abort(session)

    def upload_all(self, session):
        for batch in self.batches:
            self.publish_batch(session, batch)

    def complete_session(self, header=None):
        session = self.begin(header)
        self.upload_all(session)
        return session

    def reopen_with_new_ephemeral_owner(self):
        return GraphFixture(
            self.dsn,
            self.admin_dsn,
            self.iwiki_id,
            self.domain,
            rows=self.rows,
        )

    def for_domain(self, domain):
        return GraphFixture(self.dsn, self.admin_dsn, self.iwiki_id, domain)

    def _query(self, statement, parameters=(), *, admin=False):
        import psycopg

        with psycopg.connect(self.admin_dsn if admin else self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(statement, parameters)
                return cursor.fetchall()

    def _domain_id(self):
        return self._query(
            "SELECT domain_id FROM iwiki.domains "
            "WHERE iwiki_id = %s AND slug = %s",
            (self.iwiki_id, self.domain),
            admin=True,
        )[0][0]

    def session(self, session):
        rows = self._query(
            "SELECT state, owner_id, lease_expires_at "
            "FROM iwiki.code_graph_publication_sessions "
            "WHERE iwiki_id = %s AND domain_id = %s AND session_id = %s",
            (self.iwiki_id, self._domain_id(), session.session_id),
            admin=True,
        )
        if not rows:
            return None
        return {
            "state": rows[0][0],
            "owner_id": rows[0][1],
            "lease_expires_at": rows[0][2].isoformat(),
        }

    def snapshot_state(self, session):
        return self._query(
            "SELECT s.state FROM iwiki.code_graph_snapshots s "
            "JOIN iwiki.code_graph_publication_sessions p "
            "ON p.iwiki_id = s.iwiki_id AND p.domain_id = s.domain_id "
            "AND p.snapshot_id = s.snapshot_id "
            "WHERE p.session_id = %s AND p.iwiki_id = %s "
            "AND p.domain_id = %s",
            (session.session_id, self.iwiki_id, self._domain_id()),
            admin=True,
        )[0][0]

    def batch_count(self, session):
        return self._query(
            "SELECT count(*) FROM iwiki.code_graph_batches "
            "WHERE iwiki_id = %s AND domain_id = %s AND session_id = %s",
            (self.iwiki_id, self._domain_id(), session.session_id),
            admin=True,
        )[0][0]

    def reader_status(self):
        return self.store.status()

    def active_rows(self):
        domain_id = self._domain_id()
        active = self._query(
            "SELECT active_snapshot_id FROM iwiki.code_graph_domain_state "
            "WHERE iwiki_id = %s AND domain_id = %s",
            (self.iwiki_id, domain_id),
            admin=True,
        )
        snapshot_id = active[0][0] if active else None
        counts = {"repositories": 1 if snapshot_id else 0}
        for kind, table in (
            ("files", "code_graph_files"),
            ("symbols", "code_graph_symbols"),
            ("relations", "code_graph_relations"),
        ):
            counts[kind] = self._query(
                f"SELECT count(*) FROM iwiki.{table} "
                "WHERE iwiki_id = %s AND domain_id = %s AND snapshot_id = %s",
                (self.iwiki_id, domain_id, snapshot_id),
                admin=True,
            )[0][0]
        return counts

    def write_markdown_page(self, slug, markdown):
        """Write one authoritative page through the real Markdown store."""
        from iwiki_mcp.postgres.auth import AuthContext
        from iwiki_mcp.postgres.store import PostgresStore

        store = PostgresStore(
            self.admin_dsn,
            self.iwiki_id,
            _cfg(),
            embedder=_embed,
            auth_context=AuthContext(
                iwiki_id=self.iwiki_id,
                token_id="fixture",
                read_domains=(self.domain,),
                write_domains=(self.domain,),
                primary=self.domain,
            ),
        )
        return store.write_page(self.domain, slug, markdown)

    def lint(self):
        from iwiki_mcp.postgres.store import PostgresStore

        return PostgresStore(
            self.admin_dsn, self.iwiki_id, _cfg(), embedder=_embed
        ).lint_domain(self.domain, [self.domain])

    def markdown_snapshot(self):
        from iwiki_mcp.postgres.store import PostgresStore

        return PostgresStore(
            self.admin_dsn, self.iwiki_id, _cfg(), embedder=_embed
        ).markdown_snapshot(self.domain)

    def wiki_links(self):
        return self._query(
            "SELECT relation_id, selector FROM iwiki.code_graph_wiki_links "
            "WHERE iwiki_id = %s AND domain_id = %s ORDER BY relation_id",
            (self.iwiki_id, self._domain_id()),
            admin=True,
        )

    def bump_markdown_generation(self):
        import psycopg

        with psycopg.connect(self.admin_dsn, autocommit=True) as connection:
            connection.execute(
                "UPDATE iwiki.domains SET markdown_generation = "
                "markdown_generation + 1 WHERE iwiki_id = %s AND slug = %s",
                (self.iwiki_id, self.domain),
            )

    def ready_at(self):
        return self._query(
            "SELECT s.ready_at FROM iwiki.code_graph_domain_state d "
            "JOIN iwiki.code_graph_snapshots s "
            "ON s.iwiki_id = d.iwiki_id AND s.domain_id = d.domain_id "
            "AND s.snapshot_id = d.active_snapshot_id "
            "WHERE d.iwiki_id = %s AND d.domain_id = %s",
            (self.iwiki_id, self._domain_id()),
            admin=True,
        )[0][0]

    def indexed_at_plus(self, seconds):
        import datetime

        return self.ready_at() + datetime.timedelta(seconds=seconds)

    def reader(self, *, max_snapshot_age_seconds=0, now=None):
        from iwiki_mcp.postgres.codegraph import PostgresCodeGraphReader

        return PostgresCodeGraphReader(
            self.dsn,
            self.iwiki_id,
            self.domain,
            max_snapshot_age_seconds=max_snapshot_age_seconds,
            clock=(lambda: now) if now is not None else self._now,
        )

    @property
    def search_request(self):
        from iwiki_mcp.codegraph.query import validate_search_request

        return validate_search_request(_RANKED_QUERY, limit=20)

    def context_request(self, **overrides):
        from iwiki_mcp.codegraph.context import validate_context_request

        arguments = {
            "seeds": [_ranked_entity_id("file", "source")],
            "direction": "out",
            "depth": 1,
        }
        arguments.update(overrides)
        return validate_context_request(**arguments)

    @contextmanager
    def hold_domain_advisory_lock(self):
        import psycopg

        lock_id = self._query(
            "SELECT domain_lock_id FROM iwiki.code_graph_domain_state "
            "WHERE iwiki_id = %s AND domain_id = %s",
            (self.iwiki_id, self._domain_id()),
            admin=True,
        )[0][0]
        with psycopg.connect(self.admin_dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)",
                    (0x4957494B, lock_id),
                )
                try:
                    yield
                finally:
                    connection.rollback()


def _graph_rows(repository_id):
    files = [
        {
            "file_id": f"file-{index}",
            "repository_id": repository_id,
            "path": f"src/pkg/module_{index}.py",
            "path_casefold": f"src/pkg/module_{index}.py",
            "language": "python",
            "content_hash": f"sha256:{index:064d}",
            "size_bytes": 128 + index,
            "start_line": 1,
            "end_line": 20,
            "module_id": f"module-{index}",
            "module_qualified_name": f"pkg.module_{index}",
        }
        for index in range(2)
    ]
    symbols = [
        {
            "symbol_id": f"symbol-{index}",
            "file_id": f"file-{index}",
            "kind": "function",
            "qualified_name": f"pkg.module_{index}.run",
            "local_name": "run",
            "start_line": 3,
            "end_line": 8,
        }
        for index in range(2)
    ]
    relations = [
        {
            "relation_id": "relation-0",
            "source_file_id": "file-0",
            "source_symbol_id": "symbol-0",
            "target_symbol_id": "symbol-1",
            "kind": "calls",
            "resolution": "resolved",
        }
    ]
    repositories = [
        {
            "repository_id": repository_id,
            "git_commit": "0" * 40,
            "source_fingerprint": "source-fixture",
            "config_fingerprint": "config-fixture",
            "parser_fingerprint": "parser-fixture",
            "normalizer_version": "normalizer-1",
            "unicode_data_version": "15.1",
            "state": "ready",
            "indexed_at": "2026-08-16T00:00:00+00:00",
        }
    ]
    return {
        "repositories": repositories,
        "files": files,
        "symbols": symbols,
        "relations": relations,
    }


_RANKED_QUERY = "needle"


def _ranked_entity_id(kind, identity):
    """Return one canonical typed entity ID accepted by context validation."""
    import hashlib

    digest = hashlib.sha256(f"{kind}:{identity}".encode("utf-8")).hexdigest()
    return f"py:{kind}:{digest}"


def _ranked_file(repository_id, identity, path, *, module=None):
    from iwiki_mcp.codegraph import models

    local_name = path.rsplit("/", 1)[-1]
    return {
        "file_id": _ranked_entity_id("file", identity),
        "repository_id": repository_id,
        "path": path,
        "path_casefold": models.compact_casefold(path),
        "file_local_name": local_name,
        "file_name_tokens_casefold": models.token_key(local_name),
        "language": "python",
        "content_hash": f"hash:{identity}",
        "parser_version": "fixture",
        "size_bytes": 10,
        "start_line": 1,
        "end_line": 1,
        "start_byte": 0,
        "end_byte": 10,
        "module_key": path,
        "module_id": None if module is None else _ranked_entity_id(
            "module", identity
        ),
        "module_qualified_name": module,
        "module_local_name": (
            None if module is None else module.rsplit(".", 1)[-1]
        ),
        "module_name_tokens_casefold": (
            None
            if module is None
            else models.token_key(module, module.rsplit(".", 1)[-1])
        ),
    }


def _ranked_symbol(identity, file_identity, qualified, local, *, signature="()"):
    from iwiki_mcp.codegraph import models

    return {
        "symbol_id": _ranked_entity_id("symbol", identity),
        "file_id": _ranked_entity_id("file", file_identity),
        "kind": "method",
        "qualified_name": qualified,
        "local_name": local,
        "name_tokens_casefold": models.token_key(qualified, local),
        "start_line": 1,
        "end_line": 1,
        "start_byte": 0,
        "end_byte": 10,
        "signature": signature,
        "signature_casefold": models.compact_casefold(signature),
        "visibility": "public",
        "content_hash": f"symbol-hash:{identity}",
        "metadata_json": "{}",
    }


def _ranked_alias(
    identity, alias, *, target_module=None, target_symbol=None, start_byte=0
):
    from iwiki_mcp.codegraph import models

    return {
        "relation_id": _ranked_entity_id("symbol", f"relation:{identity}"),
        "source_file_id": _ranked_entity_id("file", "source"),
        "source_module_id": None,
        "source_symbol_id": None,
        "target_module_id": target_module,
        "target_symbol_id": target_symbol,
        "target_reference": None,
        "relation_type": "IMPORTS",
        "source_start_line": 1,
        "source_end_line": 1,
        "source_start_byte": start_byte,
        "source_end_byte": start_byte + 1,
        "binding_name": alias,
        "binding_kind": "explicit_alias",
        "binding_name_tokens_casefold": models.token_key(alias),
        "confidence": 1.0,
        "resolution_state": "resolved",
        "metadata_json": "{}",
    }


def _ranked_rows(repository_id):
    """Seed one distinct hit for every declared search rank in both stores."""
    files = [
        _ranked_file(repository_id, "qualified-file", "needle"),
        _ranked_file(
            repository_id, "local-module", "typed/local.py", module="pkg.needle"
        ),
        _ranked_file(repository_id, "alias-exact", "typed/alias_exact.py"),
        _ranked_file(repository_id, "canonical-prefix", "typed/prefix.py"),
        _ranked_file(repository_id, "alias-prefix", "typed/alias_prefix.py"),
        _ranked_file(repository_id, "canonical-lexical", "typed/lexical.py"),
        _ranked_file(
            repository_id,
            "alias-lexical",
            "typed/alias_lexical.py",
            module="pkg.AliasLexical",
        ),
        _ranked_file(repository_id, "signature", "typed/signature.py"),
        _ranked_file(repository_id, "path", "typed/needle-assets/asset.py"),
        _ranked_file(repository_id, "source", "typed/source.py"),
    ]
    symbols = [
        _ranked_symbol(
            "alias-exact", "alias-exact", "pkg.AliasExact", "alias_exact_target"
        ),
        _ranked_symbol(
            "canonical-prefix", "canonical-prefix", "needle.prefix",
            "prefix_target",
        ),
        _ranked_symbol(
            "alias-prefix", "alias-prefix", "pkg.AliasPrefix",
            "alias_prefix_target",
        ),
        _ranked_symbol(
            "canonical-lexical", "canonical-lexical", "pkg.canonical_needle",
            "canonical_needle",
        ),
        _ranked_symbol(
            "signature", "signature", "pkg.Signature", "signature_target",
            signature="(needle: str)",
        ),
    ]
    relations = [
        _ranked_alias(
            "needle-exact",
            "needle",
            target_symbol=_ranked_entity_id("symbol", "alias-exact"),
        ),
        _ranked_alias(
            "needle-prefix",
            "needle_alias",
            target_symbol=_ranked_entity_id("symbol", "alias-prefix"),
            start_byte=10,
        ),
        _ranked_alias(
            "needle-lexical",
            "alias_needle",
            target_module=_ranked_entity_id("module", "alias-lexical"),
            start_byte=20,
        ),
    ]
    repositories = [
        {
            "repository_id": repository_id,
            "root_path": ".",
            "git_remote": None,
            "git_commit": "0" * 40,
            "source_fingerprint": "source-fixture",
            "config_fingerprint": "config-fixture",
            "parser_fingerprint": "parser-fixture",
            "normalizer_version": "normalizer-1",
            "unicode_data_version": "15.1",
            "revision": "sha256:fixture",
            "state": "ready",
            "indexed_at": "2026-08-16T00:00:00+00:00",
        }
    ]
    return {
        "repositories": repositories,
        "files": files,
        "symbols": symbols,
        "relations": relations,
    }


class SqliteRankedReader:
    """Rank the shared fixture through the authoritative SQLite query path."""

    def __init__(self, connection, domain):
        self._connection = connection
        self._domain = domain

    def search(self, request):
        from dataclasses import asdict

        from iwiki_mcp.codegraph.query import CodeGraphQuery

        results = CodeGraphQuery(self._domain).search(self._connection, request)
        return {"results": [asdict(item) for item in results]}


class PostgresRankedReader:
    """Project the PostgreSQL reader response onto its ranked result list."""

    def __init__(self, reader):
        self._reader = reader

    def search(self, request):
        return {"results": self._reader.search(request)["results"]}


class RankedPair:
    """Hold both ranked readers over one identical nine-rank fixture."""

    def __init__(self, sqlite, postgres, request):
        self.sqlite = sqlite
        self.postgres = postgres
        self.request = request


def _provisioned_graph(clean_postgres, runtime_principal, rows=None):
    from iwiki_mcp.postgres.auth import AuthStore
    from iwiki_mcp.postgres.migrations import MigrationSettings, run_migrations

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
    role, password = runtime_principal(["docs", "private"], ["docs", "private"])
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    values = conninfo_to_dict(clean_postgres)
    role_dsn = SecretDsn(
        make_conninfo(**{**values, "user": role, "password": password})
    )
    return GraphFixture(role_dsn, clean_postgres, "wiki-a", "docs", rows=rows)


@pytest.fixture
def pg_graph(clean_postgres, runtime_principal):
    return _provisioned_graph(clean_postgres, runtime_principal)


@pytest.fixture
def pg_ranked_graph(clean_postgres, runtime_principal):
    return _provisioned_graph(
        clean_postgres, runtime_principal, rows=_ranked_rows("docs")
    )


@pytest.fixture
def pg_ready_graph(pg_ranked_graph):
    result = pg_ranked_graph.finalize(pg_ranked_graph.complete_session())
    assert result["state"] == "ready"
    return pg_ranked_graph


class HostedCodeBinding:
    """Bind one hosted PostgreSQL request context onto the server tool surface."""

    def __init__(self, graph, binding):
        self.graph = graph
        self.binding = binding

    def __repr__(self):
        return "<redacted hosted code graph binding>"


def _bind_hosted_code(graph, monkeypatch, token_id="token-a"):
    from psycopg.conninfo import conninfo_to_dict

    from iwiki_mcp import server
    from iwiki_mcp.postgres.auth import AuthContext
    from iwiki_mcp.postgres.config import HostedCodeGraphConfig
    from iwiki_mcp.storage import PostgresBinding

    values = conninfo_to_dict(str(graph.dsn))
    binding = PostgresBinding(
        host=values.get("host", "127.0.0.1"),
        port=int(values.get("port", 5432)),
        database=values["dbname"],
        user=values["user"],
        sslmode=values.get("sslmode", "prefer"),
        password=values.get("password", ""),
        iwiki_id=graph.iwiki_id,
        read=(graph.domain,),
        write=(graph.domain,),
        primary=graph.domain,
        project_dir="/hosted-without-checkout",
        embed_model="fixture-model",
        embed_dimensions=3,
        rerank_model="",
    )
    monkeypatch.setattr(server, "_LOCAL_POSTGRES_BINDING", None)
    monkeypatch.setattr(server.base, "resolve_binding", lambda: binding)
    monkeypatch.setattr(server, "_HOSTED_CODE_GRAPH", HostedCodeGraphConfig())
    monkeypatch.setattr(server, "_HOSTED_CONFIG", _cfg())
    token = server._AUTH_CONTEXT.set(
        AuthContext(
            iwiki_id=graph.iwiki_id,
            token_id=token_id,
            read_domains=(graph.domain,),
            write_domains=(graph.domain,),
            primary=graph.domain,
        )
    )
    monkeypatch.setattr(
        server,
        "_SESSION_BINDING",
        server._SESSION_BINDING,
    )
    return HostedCodeBinding(graph, binding), token


@pytest.fixture
def hosted_ready_code(pg_ready_graph, monkeypatch):
    from iwiki_mcp import server

    bound, token = _bind_hosted_code(pg_ready_graph, monkeypatch)
    yield bound
    server._AUTH_CONTEXT.reset(token)


@pytest.fixture
def hosted_empty_code(pg_ranked_graph, monkeypatch):
    from iwiki_mcp import server

    bound, token = _bind_hosted_code(pg_ranked_graph, monkeypatch)
    yield bound
    server._AUTH_CONTEXT.reset(token)


@pytest.fixture
def ranked_graph_pair(pg_ready_graph, tmp_path):
    from iwiki_mcp.codegraph.store import CodeGraphStore

    snapshot = {**_ranked_rows(pg_ready_graph.domain), "wiki_code_links": ()}
    store = CodeGraphStore(tmp_path / "ranked-code.sqlite3")
    store.insert_snapshot(snapshot)
    connection = store.open_existing()
    yield RankedPair(
        SqliteRankedReader(connection, pg_ready_graph.domain),
        PostgresRankedReader(pg_ready_graph.reader()),
        pg_ready_graph.search_request,
    )
    connection.close()
