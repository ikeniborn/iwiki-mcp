"""PostgreSQL personal-token authentication and authorization contract."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import base64
import hashlib

import pytest

from iwiki_mcp.engine.config import Config


pytestmark = pytest.mark.postgres_integration


def _cfg():
    return Config(
        base_url="http://example.invalid/v1",
        api_key="test",
        embed_model="fixture-model",
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


def _embed(_config, texts):
    return [[1.0, 0.0, 0.0] for _text in texts]


def _markdown(title):
    return (
        "---\n"
        "type: concept\n"
        f"title: {title}\n"
        f"description: {title} description\n"
        "tags: [fixture]\n"
        "status: stable\n"
        "---\n"
        f"# {title}\n\n"
        f"## Details\n{title} body\n"
    )


@pytest.fixture
def auth_store(clean_postgres):
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
    store = AuthStore(clean_postgres)
    store.create_wiki("wiki-a", "wiki-a")
    store.create_domain("wiki-a", "docs")
    store.create_domain("wiki-a", "private")
    store.create_wiki("wiki-b", "wiki-b")
    store.create_domain("wiki-b", "docs")
    return store


def test_token_is_256_bit_one_time_secret_and_database_stores_only_digest(
    auth_store,
):
    import psycopg

    created = auth_store.create_token(
        "wiki-a", "alice", read_domains=["docs"], write_domains=["docs"]
    )
    token = created["token"]
    token_id, secret = auth_store.parse_token(token)

    assert len(secret) == 32
    assert created["token_id"] == token_id
    assert created["read_domains"] == ["docs"]
    assert created["write_domains"] == ["docs"]

    with psycopg.connect(auth_store.dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT token_digest, owner FROM iwiki.tokens WHERE token_id = %s",
                (token_id,),
            )
            digest, owner = cursor.fetchone()
    assert bytes(digest) == hashlib.sha256(token.encode()).digest()
    assert token.encode() not in bytes(digest)
    assert secret not in bytes(digest)
    assert owner == "alice"


def test_authentication_returns_immutable_one_wiki_context(auth_store):
    created = auth_store.create_token(
        "wiki-a", "alice", read_domains=["docs", "private"], write_domains=["docs"]
    )

    context = auth_store.authenticate(created["token"])

    assert context.iwiki_id == "wiki-a"
    assert context.read_domains == ("docs", "private")
    assert context.write_domains == ("docs",)
    assert context.can_create_domain is False
    assert context.managed_domains == ()
    assert context.can_manage_grants("docs") is False
    with pytest.raises(FrozenInstanceError):
        context.iwiki_id = "wiki-b"


def test_domain_identifier_is_strict_and_shared():
    from iwiki_mcp.postgres.auth import validate_domain_identifier

    assert validate_domain_identifier("new-project") == "new-project"
    for value in ("", " docs", "docs ", ".hidden", "a/b", "a\\b"):
        with pytest.raises(ValueError, match="domain identifier is invalid"):
            validate_domain_identifier(value)


def test_context_narrowing_preserves_management_authority():
    from iwiki_mcp.postgres.auth import AuthContext

    context = AuthContext(
        iwiki_id="wiki-a",
        token_id="token-a",
        read_domains=("docs",),
        write_domains=("docs",),
        primary="docs",
        can_create_domain=True,
        managed_domains=("private",),
    )

    narrowed = context.narrow(read_domains=("docs",), write_domains=())

    assert narrowed.primary is None
    assert narrowed.can_create_domain is True
    assert narrowed.managed_domains == ("private",)
    assert narrowed.can_manage_grants("private") is True


@pytest.mark.parametrize(
    ("read_domains", "write_domains", "message"),
    [
        ([], [], "read grant is required"),
        (["missing"], [], "domain grant does not exist"),
        (["docs"], ["private"], "write grant must also be readable"),
    ],
)
def test_token_creation_requires_explicit_existing_safe_grants(
    auth_store, read_domains, write_domains, message
):
    with pytest.raises(ValueError, match=message):
        auth_store.create_token(
            "wiki-a",
            "alice",
            read_domains=read_domains,
            write_domains=write_domains,
        )


def test_create_only_token_requires_creation_authority(auth_store):
    created = auth_store.create_token(
        "wiki-a",
        "provisioner",
        read_domains=[],
        write_domains=[],
        can_create_domain=True,
    )

    context = auth_store.authenticate(created["token"])

    assert context.can_create_domain is True
    assert context.read_domains == ()
    assert context.write_domains == ()
    assert context.primary is None

    with pytest.raises(ValueError, match="write grant must also be readable"):
        auth_store.create_token(
            "wiki-a",
            "invalid provisioner",
            read_domains=[],
            write_domains=["docs"],
            can_create_domain=True,
        )


def test_authentication_loads_content_and_management_authority(auth_store):
    import psycopg

    created = auth_store.create_token(
        "wiki-a", "manager", read_domains=["docs"], write_domains=[]
    )
    with psycopg.connect(auth_store.dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE iwiki.tokens SET can_create_domain = true "
                "WHERE iwiki_id = %s AND token_id = %s",
                ("wiki-a", created["token_id"]),
            )
            cursor.execute(
                "INSERT INTO iwiki.token_domain_management_grants "
                "(iwiki_id, token_id, domain_id, can_manage_grants) "
                "SELECT %s, %s, domain_id, true FROM iwiki.domains "
                "WHERE iwiki_id = %s AND slug = %s",
                (
                    "wiki-a",
                    created["token_id"],
                    "wiki-a",
                    "private",
                ),
            )

    context = auth_store.authenticate(created["token"])

    assert context.can_create_domain is True
    assert context.read_domains == ("docs",)
    assert context.write_domains == ()
    assert context.managed_domains == ("private",)
    assert context.primary is None


def test_authentication_keeps_three_statements_with_combined_authority_query():
    from iwiki_mcp.postgres.auth import AuthStore

    secret = b"x" * 32
    encoded = base64.urlsafe_b64encode(secret).decode().rstrip("=")
    token = f"iwiki_token-a_{encoded}"
    digest = hashlib.sha256(token.encode()).digest()
    statements = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, _parameters=None):
            statements.append(" ".join(statement.split()))

        def fetchone(self):
            return "wiki-a", digest, True

        def fetchall(self):
            return [
                ("docs", True, True, False),
                ("private", None, None, True),
            ]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return Cursor()

    store = AuthStore("unused", connection_factory=Connection)

    context = store.authenticate(token)

    assert len(statements) == 3
    assert "token_domain_grants" in statements[1]
    assert "token_domain_management_grants" in statements[1]
    assert context.can_create_domain is True
    assert context.read_domains == ("docs",)
    assert context.write_domains == ("docs",)
    assert context.managed_domains == ("private",)
    assert context.primary == "docs"


def test_token_is_bound_to_one_wiki_even_when_slugs_overlap(auth_store):
    created = auth_store.create_token(
        "wiki-a", "alice", read_domains=["docs"], write_domains=[]
    )

    context = auth_store.authenticate(created["token"])

    assert context.iwiki_id == "wiki-a"
    assert context.can_read("docs")
    assert not context.can_write("docs")


def test_unknown_malformed_revoked_and_disabled_tokens_are_rejected(auth_store):
    created = auth_store.create_token(
        "wiki-a", "alice", read_domains=["docs"], write_domains=[]
    )

    assert auth_store.authenticate("") is None
    assert auth_store.authenticate("not-a-token") is None
    assert auth_store.authenticate(created["token"] + "x") is None
    auth_store.revoke_token(created["token_id"])
    assert auth_store.authenticate(created["token"]) is None

    second = auth_store.create_token(
        "wiki-a", "alice", read_domains=["docs"], write_domains=[]
    )
    auth_store.set_wiki_active("wiki-a", False)
    assert auth_store.authenticate(second["token"]) is None


def test_last_used_is_updated_at_most_once_per_five_minutes(auth_store):
    created = auth_store.create_token(
        "wiki-a", "alice", read_domains=["docs"], write_domains=[]
    )
    start = datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc)

    assert auth_store.authenticate(created["token"], now=start) is not None
    first = auth_store.last_used_at(created["token_id"])
    assert auth_store.authenticate(
        created["token"], now=start + timedelta(minutes=4)
    ) is not None
    assert auth_store.last_used_at(created["token_id"]) == first
    assert auth_store.authenticate(
        created["token"], now=start + timedelta(minutes=5)
    ) is not None
    assert auth_store.last_used_at(created["token_id"]) == start + timedelta(minutes=5)


def test_context_narrowing_never_expands_grants(auth_store):
    created = auth_store.create_token(
        "wiki-a", "alice", read_domains=["docs", "private"], write_domains=["docs"]
    )
    context = auth_store.authenticate(created["token"])

    narrowed = context.narrow(
        read_domains=["docs"], write_domains=["docs"], primary="docs"
    )
    assert narrowed.read_domains == ("docs",)
    assert narrowed.write_domains == ("docs",)
    assert narrowed.primary == "docs"
    with pytest.raises(PermissionError, match="read scope cannot be expanded"):
        narrowed.narrow(
            read_domains=["docs", "private"],
            write_domains=["docs"],
            primary="docs",
        )


def test_postgres_backend_rechecks_tenant_and_domain_acl(auth_store):
    import psycopg

    from iwiki_mcp.postgres.store import PostgresStore

    admin = PostgresStore(auth_store.dsn, "wiki-a", _cfg(), embedder=_embed)
    admin.write_page("docs", "visible", _markdown("Visible"))
    admin.write_page("private", "secret", _markdown("Secret"))
    created = auth_store.create_token(
        "wiki-a", "alice", read_domains=["docs"], write_domains=["docs"]
    )
    context = auth_store.authenticate(created["token"])
    scoped = PostgresStore(
        auth_store.dsn,
        "wiki-a",
        _cfg(),
        embedder=_embed,
        auth_context=context,
    )

    assert scoped.list_domains() == ["docs"]
    assert scoped.read_page("docs", "visible")["slug"] == "visible"
    linked = _markdown("Linked").replace(
        "Linked body", "[Secret](iwiki://private/secret.md)"
    )
    scoped.write_page("docs", "linked", linked)
    with psycopg.connect(auth_store.dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT target_page_id FROM iwiki.links "
                "WHERE iwiki_id = %s AND target_domain = %s",
                ("wiki-a", "private"),
            )
            assert cursor.fetchone() == (None,)
    with pytest.raises(PermissionError, match="access denied"):
        scoped.read_page("private", "secret")
    with pytest.raises(PermissionError, match="access denied"):
        scoped.write_page("private", "new", _markdown("New"))
    with pytest.raises(PermissionError, match="access denied"):
        scoped.create_domain("new-domain")
    with pytest.raises(PermissionError, match="access denied"):
        PostgresStore(
            auth_store.dsn,
            "wiki-b",
            _cfg(),
            embedder=_embed,
            auth_context=context,
        )


def test_bearer_boundary_returns_safe_401_and_403(auth_store):
    from iwiki_mcp.postgres.auth import (
        AccessError,
        authenticate_bearer,
        authorize_domains,
    )

    created = auth_store.create_token(
        "wiki-a", "alice", read_domains=["docs"], write_domains=[]
    )
    token = created["token"]
    for header in (None, "", "Basic opaque", "Bearer", "Bearer invalid"):
        with pytest.raises(AccessError) as caught:
            authenticate_bearer(auth_store, header)
        assert caught.value.status_code == 401
        assert token not in str(caught.value)
        assert "wiki-a" not in str(caught.value)

    context = authenticate_bearer(auth_store, f"Bearer {token}")
    with pytest.raises(AccessError) as caught:
        authorize_domains(context, read_domains=("private",))
    assert caught.value.status_code == 403
    assert str(caught.value) == "access denied"
    with pytest.raises(AccessError) as caught:
        authorize_domains(context, write_domains=("docs",))
    assert caught.value.status_code == 403

    auth_store.revoke_token(created["token_id"])
    with pytest.raises(AccessError) as caught:
        authenticate_bearer(auth_store, f"Bearer {token}")
    assert caught.value.status_code == 401
