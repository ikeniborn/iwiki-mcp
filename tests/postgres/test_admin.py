"""PostgreSQL administration CLI, import, and rollback-export contract."""
from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
import subprocess
import secrets

import pytest


pytestmark = pytest.mark.postgres_integration


class RedactedEnv(dict):
    """Environment mapping whose pytest representation never exposes secrets."""

    def __repr__(self):
        return "<redacted admin environment>"


@pytest.fixture
def admin_runtime(clean_postgres, tmp_path):
    from psycopg.conninfo import conninfo_to_dict

    values = conninfo_to_dict(clean_postgres)
    config = tmp_path / "server.toml"
    config.write_text(
        "[storage]\n"
        "type = \"postgres\"\n"
        f"host = {json.dumps(values['host'])}\n"
        f"port = {int(values.get('port', 5432))}\n"
        f"database = {json.dumps(values['dbname'])}\n"
        f"user = {json.dumps(values['user'])}\n"
        f"sslmode = {json.dumps(values.get('sslmode', 'prefer'))}\n"
        "\n[server]\n"
        "host = \"127.0.0.1\"\n"
        "port = 8765\n"
        "allowed_origins = [\"https://iwiki.example\"]\n"
        "pool_min_size = 1\n"
        "pool_max_size = 4\n"
        "statement_timeout_ms = 30000\n"
        "lock_timeout_ms = 5000\n",
        encoding="utf-8",
    )
    environ = RedactedEnv({
        "IWIKI_DB_PASSWORD": values["password"],
        "IWIKI_LLM_BASE_URL": "http://example.invalid/v1",
        "IWIKI_LLM_KEY": "fixture-key",
        "IWIKI_EMBED_MODEL": "fixture-model",
        "IWIKI_EMBED_DIMENSIONS": "3",
        "IWIKI_RERANK_MODEL": "",
    })
    return config, environ


def _run(argv, environ):
    from iwiki_mcp import admin

    stdout = StringIO()
    stderr = StringIO()
    code = admin.run(argv, environ=environ, stdout=stdout, stderr=stderr)
    return code, stdout.getvalue(), stderr.getvalue()


def test_parser_keeps_stdio_boundary_and_has_no_destructive_options():
    from iwiki_mcp import admin

    parser = admin.build_parser()
    stdio = parser.parse_args(["--project", "/tmp/project"])
    serve = parser.parse_args(["serve", "--config", "server.toml"])
    grant = parser.parse_args(
        [
            "principal", "grant", "--config", "server.toml",
            "--principal", "runtime", "--iwiki", "wiki-a",
            "--read-domain", "docs", "--write-domain", "docs",
            "--runtime", "direct",
        ]
    )
    rollback = parser.parse_args(
        ["schema", "rollback-v4-compat", "--config", "server.toml"]
    )

    assert stdio.command is None
    assert stdio.project == "/tmp/project"
    assert serve.transport == "streamable-http"
    assert grant.principal_command == "grant"
    assert rollback.confirm is False
    for argv in (
        ["serve", "--project", "/tmp/project"],
        ["base", "list", "--project", "/tmp/project"],
        ["domain", "create", "--project", "/tmp/project"],
        ["token", "list", "--project", "/tmp/project"],
        ["base", "delete", "--iwiki", "wiki-a"],
        ["token", "revoke", "--token", "secret"],
        ["base", "export-git", "--iwiki", "wiki-a", "--force"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


def test_server_main_routes_admin_without_starting_stdio(monkeypatch):
    from iwiki_mcp import admin, server

    monkeypatch.setattr(
        server.sys,
        "argv",
        ["iwiki-mcp", "base", "list", "--config", "server.toml"],
    )
    monkeypatch.setattr(admin, "run", lambda argv: 7)
    monkeypatch.setattr(
        server.mcp,
        "run",
        lambda: pytest.fail("stdio must not start for an admin command"),
    )

    with pytest.raises(SystemExit) as caught:
        server.main()

    assert caught.value.code == 7


def test_serve_dispatches_streamable_http_runtime(monkeypatch):
    from iwiki_mcp import http

    called = {}

    def run_server(config_path, *, environ):
        called.update(config_path=config_path, environ=environ)

    monkeypatch.setattr(http, "run_server", run_server)
    environ = RedactedEnv({})

    code, output, error = _run(
        ["serve", "--config", "server.toml"], environ
    )

    assert code == 0
    assert output == ""
    assert error == ""
    assert called == {"config_path": "server.toml", "environ": environ}


def test_admin_commands_require_config_or_environment(admin_runtime):
    _config, environ = admin_runtime
    environ = RedactedEnv(environ)

    code, _stdout, stderr = _run(["base", "list", "--json"], environ)

    assert code == 2
    assert "--config or IWIKI_SERVER_CONFIG is required" in stderr

    code, _stdout, stderr = _run(
        ["--project", "/tmp/project", "base", "list"], environ
    )
    assert code == 2
    assert "accepted only for stdio" in stderr


def _hosted_principal(config, environ, iwiki_id, read_domains, write_domains):
    """Provision one disposable hosted role for CLI token creation."""
    from iwiki_mcp import admin
    from iwiki_mcp.postgres.store import provision_runtime_grant

    from conftest import create_runtime_role

    service = admin._service(str(config), environ)
    role, _password = create_runtime_role(service.dsn, prefix="hostedcli")
    provision_runtime_grant(
        service.dsn,
        principal=role,
        iwiki_id=iwiki_id,
        read_domains=list(read_domains),
        write_domains=list(write_domains),
        runtime="hosted",
    )
    return role


def test_base_domain_and_token_lifecycle_is_explicit_and_secret_safe(
    admin_runtime,
):
    config, environ = admin_runtime
    prefix = ["--config", str(config)]

    assert _run(["base", "create", *prefix, "--iwiki", "wiki-a"], environ)[0] == 0
    code, output, _error = _run(["base", "show", *prefix, "--iwiki", "wiki-a", "--json"], environ)
    assert code == 0
    assert json.loads(output) == {
        "active": True,
        "domains": 0,
        "iwiki": "wiki-a",
        "pages": 0,
        "tokens": 0,
    }

    assert _run(
        ["domain", "create", *prefix, "--iwiki", "wiki-a", "--domain", "docs"],
        environ,
    )[0] == 0
    hosted_a = _hosted_principal(config, environ, "wiki-a", ["docs"], ["docs"])
    code, output, _error = _run(
        [
            "token", "create", *prefix, "--iwiki", "wiki-a", "--owner", "alice",
            "--read-domain", "docs", "--write-domain", "docs",
            "--hosted-principal", hosted_a,
        ],
        environ,
    )
    assert code == 0
    token = output.strip()
    assert token.startswith("iwiki_")
    assert output.count(token) == 1

    code, output, _error = _run(["token", "list", *prefix, "--iwiki", "wiki-a", "--json"], environ)
    listed = json.loads(output)
    assert code == 0
    assert listed == [
        {
            "created_at": listed[0]["created_at"],
            "last_used_at": None,
            "owner": "alice",
            "read_domains": ["docs"],
            "revoked_at": None,
            "token_id": listed[0]["token_id"],
            "write_domains": ["docs"],
        }
    ]
    assert token not in output
    assert "digest" not in output

    token_id = listed[0]["token_id"]
    assert _run(["token", "revoke", *prefix, "--token-id", token_id], environ)[0] == 0
    assert _run(["base", "disable", *prefix, "--iwiki", "wiki-a"], environ)[0] == 0
    code, output, _error = _run(["base", "show", *prefix, "--iwiki", "wiki-a", "--json"], environ)
    assert code == 0
    assert json.loads(output)["active"] is False
    assert _run(["base", "enable", *prefix, "--iwiki", "wiki-a"], environ)[0] == 0

    assert _run(["base", "create", *prefix, "--iwiki", "wiki-b"], environ)[0] == 0
    assert _run(
        ["domain", "create", *prefix, "--iwiki", "wiki-b", "--domain", "docs"],
        environ,
    )[0] == 0
    hosted_b = _hosted_principal(config, environ, "wiki-b", ["docs"], [])
    code, second_token, _error = _run(
        [
            "token", "create", *prefix, "--iwiki", "wiki-b", "--owner", "bob",
            "--read-domain", "docs",
            "--hosted-principal", hosted_b,
        ],
        environ,
    )
    assert code == 0
    service = __import__("iwiki_mcp.admin", fromlist=["admin"])._service(
        str(config), environ
    )
    context = service.auth.authenticate(second_token.strip())
    assert context is not None and context.iwiki_id == "wiki-b"
    second_id = service.auth.parse_token(second_token.strip())[0]
    assert _run(
        ["token", "revoke", *prefix, "--token-id", second_id], environ
    )[0] == 0
    assert service.auth.authenticate(second_token.strip()) is None


def test_principal_grant_inspect_and_schema_rollback_commands(admin_runtime):
    import psycopg
    from psycopg import sql

    from iwiki_mcp import admin

    config, environ = admin_runtime
    prefix = ["--config", str(config)]
    assert _run(["base", "create", *prefix, "--iwiki", "wiki-a"], environ)[0] == 0
    assert _run(
        ["domain", "create", *prefix, "--iwiki", "wiki-a", "--domain", "docs"],
        environ,
    )[0] == 0
    role = f"iwiki_t3_cli_{secrets.token_hex(5)}"
    service = admin._service(str(config), environ)
    with psycopg.connect(service.dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE ROLE {} LOGIN NOBYPASSRLS").format(
                    sql.Identifier(role)
                )
            )
    try:
        code, output, error = _run(
            [
                "principal", "grant", *prefix, "--principal", role,
                "--iwiki", "wiki-a", "--read-domain", "docs",
                "--write-domain", "docs", "--runtime", "direct", "--json",
            ],
            environ,
        )
        assert (code, error) == (0, "")
        assert json.loads(output)["principal"] == role
        code, output, error = _run(
            [
                "principal", "inspect", *prefix, "--principal", role, "--json",
            ],
            environ,
        )
        assert (code, error) == (0, "")
        inspected = json.loads(output)
        assert inspected["valid_runtime_shape"] is True
        assert inspected["grants"] == [
            {
                "can_read": True,
                "can_write": True,
                "domain": "docs",
                "iwiki": "wiki-a",
                "runtime": "direct",
            }
        ]

        code, output, error = _run(
            ["schema", "rollback-v4-compat", *prefix, "--json"], environ
        )
        assert (code, error) == (0, "")
        assert json.loads(output)["dry_run"] is True
        code, output, error = _run(
            [
                "schema", "rollback-v4-compat", *prefix, "--confirm", "--json",
            ],
            environ,
        )
        assert (code, error) == (0, "")
        assert json.loads(output)["schema_version"] == 3
    finally:
        with psycopg.connect(service.dsn, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP OWNED BY {} CASCADE").format(sql.Identifier(role))
                )
                cursor.execute(
                    sql.SQL("DROP ROLE {}").format(sql.Identifier(role))
                )


def _token_count(dsn):
    import psycopg

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM iwiki.tokens")
            return cursor.fetchone()[0]


def test_token_create_requires_the_exact_deployed_hosted_principal(admin_runtime):
    import psycopg
    from psycopg import sql

    from iwiki_mcp import admin
    from iwiki_mcp.postgres.store import provision_runtime_grant

    config, environ = admin_runtime
    prefix = ["--config", str(config)]
    assert _run(["base", "create", *prefix, "--iwiki", "wiki-a"], environ)[0] == 0
    for domain in ("docs", "private"):
        assert _run(
            ["domain", "create", *prefix, "--iwiki", "wiki-a", "--domain", domain],
            environ,
        )[0] == 0

    service = admin._service(str(config), environ)
    suffix = secrets.token_hex(5)
    hosted = f"iwiki_t3_hostedcli_{suffix}"
    narrow = f"iwiki_t3_narrowcli_{suffix}"
    direct = f"iwiki_t3_directcli_{suffix}"
    owner = f"iwiki_t3_ownercli_{suffix}"
    with psycopg.connect(service.dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            for role in (hosted, narrow, direct, owner):
                cursor.execute(
                    sql.SQL("CREATE ROLE {} LOGIN NOBYPASSRLS").format(
                        sql.Identifier(role)
                    )
                )
            cursor.execute(
                sql.SQL("ALTER TABLE iwiki.pages OWNER TO {}").format(
                    sql.Identifier(owner)
                )
            )
    try:
        provision_runtime_grant(
            service.dsn,
            principal=hosted,
            iwiki_id="wiki-a",
            read_domains=["docs", "private"],
            write_domains=["docs"],
            runtime="hosted",
        )
        provision_runtime_grant(
            service.dsn,
            principal=narrow,
            iwiki_id="wiki-a",
            read_domains=["docs"],
            write_domains=[],
            runtime="hosted",
        )
        provision_runtime_grant(
            service.dsn,
            principal=direct,
            iwiki_id="wiki-a",
            read_domains=["docs", "private"],
            write_domains=["docs"],
            runtime="direct",
        )

        before = _token_count(service.dsn)
        with pytest.raises(SystemExit) as caught:
            _run(
                [
                    "token", "create", *prefix, "--iwiki", "wiki-a",
                    "--owner", "alice", "--read-domain", "docs",
                ],
                environ,
            )
        assert caught.value.code == 2
        assert _token_count(service.dsn) == before

        for rejected, reason in (
            (f"iwiki_t3_absent_{suffix}", "unregistered"),
            (narrow, "under-granted"),
            (direct, "direct runtime"),
            (owner, "table owner"),
        ):
            code, output, error = _run(
                [
                    "token", "create", *prefix, "--iwiki", "wiki-a",
                    "--owner", "alice", "--read-domain", "docs",
                    "--write-domain", "docs",
                    "--hosted-principal", rejected,
                ],
                environ,
            )
            assert code != 0, reason
            assert output == ""
            assert "iwiki_" not in output
            assert _token_count(service.dsn) == before

        code, output, error = _run(
            [
                "token", "create", *prefix, "--iwiki", "wiki-a",
                "--owner", "alice", "--read-domain", "docs",
                "--write-domain", "docs",
                "--hosted-principal", hosted,
            ],
            environ,
        )
        assert (code, error) == (0, "")
        assert output.strip().startswith("iwiki_")
        assert _token_count(service.dsn) == before + 1
    finally:
        with psycopg.connect(service.dsn, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("ALTER TABLE iwiki.pages OWNER TO {}").format(
                        sql.Identifier(service_owner(service.dsn))
                    )
                )
                for role in (hosted, narrow, direct, owner):
                    cursor.execute(
                        sql.SQL("DROP OWNED BY {} CASCADE").format(
                            sql.Identifier(role)
                        )
                    )
                    cursor.execute(
                        sql.SQL("DROP ROLE {}").format(sql.Identifier(role))
                    )


def service_owner(dsn):
    import psycopg

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            return cursor.fetchone()[0]


def test_explicit_config_precedes_environment_config(admin_runtime, tmp_path):
    config, environ = admin_runtime
    environ = RedactedEnv(
        {**environ, "IWIKI_SERVER_CONFIG": str(tmp_path / "missing.toml")}
    )
    environ.pop("IWIKI_LLM_BASE_URL")
    environ.pop("IWIKI_LLM_KEY")

    code, output, error = _run(
        ["base", "list", "--config", str(config), "--json"], environ
    )

    assert code == 0
    assert json.loads(output) == []
    assert error == ""


def test_database_errors_are_redacted(admin_runtime, monkeypatch):
    import psycopg

    from iwiki_mcp import admin

    config, environ = admin_runtime

    def fail_service(*_args, **_kwargs):
        raise psycopg.OperationalError(
            "password=fixture-secret SQL SELECT private"
        )

    monkeypatch.setattr(admin, "_service", fail_service)
    code, output, error = _run(
        ["base", "list", "--config", str(config), "--json"], environ
    )

    assert code == 1
    assert output == ""
    assert error == "iwiki-mcp: database operation failed\n"


def test_embedding_errors_are_redacted(admin_runtime, tmp_path, monkeypatch):
    from iwiki_mcp import admin
    from iwiki_mcp.engine.embed import EmbedError

    config, environ = admin_runtime
    prefix = ["--config", str(config)]
    source = tmp_path / "source"
    _write_git_fixture(source)
    assert _run(["base", "create", *prefix, "--iwiki", "wiki-a"], environ)[0] == 0

    def fail_embed(*_args, **_kwargs):
        raise EmbedError(
            "backend https://user:secret@example.invalid/?token=secret failed"
        )

    monkeypatch.setattr(admin, "_embed", fail_embed)
    code, output, error = _run(
        [
            "base", "import-git", *prefix, "--iwiki", "wiki-a",
            "--path", str(source), "--json",
        ],
        environ,
    )

    assert code == 1
    assert output == ""
    assert error == "iwiki-mcp: embedding operation failed\n"


def _write_git_fixture(path: Path) -> dict[str, str]:
    pages = {
        "docs/alpha.md": "# Alpha\n\n## Body\nalpha [Beta](beta.md)\n",
        "docs/beta.md": "# Beta\n\n## Body\nbeta\n",
        "private/secret.md": "# Secret\n\n## Body\nprivate\n",
        "docs/concept/.draft/page.md": (
            "# Draft\n\n## Body\nhidden nested page\n"
        ),
    }
    for relative, markdown in pages.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown, encoding="utf-8")
    subprocess.run(
        ["git", "init", str(path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return pages


def test_import_export_round_trip_is_transactional_and_secret_free(
    admin_runtime, tmp_path, monkeypatch
):
    from iwiki_mcp import admin

    config, environ = admin_runtime
    prefix = ["--config", str(config)]
    source = tmp_path / "source"
    pages = _write_git_fixture(source)
    monkeypatch.setattr(
        admin,
        "_embed",
        lambda _cfg, texts: [[1.0, 0.0, 0.0] for _text in texts],
    )
    assert _run(["base", "create", *prefix, "--iwiki", "wiki-a"], environ)[0] == 0

    code, output, error = _run(
        [
            "base", "import-git", *prefix, "--iwiki", "wiki-a",
            "--path", str(source), "--dry-run", "--json",
        ],
        environ,
    )
    dry_run = json.loads(output)
    assert code == 0
    assert error == ""
    assert dry_run["dry_run"] is True
    assert dry_run["domains"] == 2
    assert dry_run["pages"] == 4

    code, output, _error = _run(
        ["base", "import-git", *prefix, "--iwiki", "wiki-a", "--path", str(source), "--json"],
        environ,
    )
    imported = json.loads(output)
    assert code == 0
    assert imported["imported"] is True
    assert imported["links"] == 1
    service = admin._service(str(config), environ)
    import psycopg

    with psycopg.connect(service.dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT target_domain, target_slug FROM iwiki.links "
                "WHERE iwiki_id = %s",
                ("wiki-a",),
            )
            assert cursor.fetchall() == [("docs", "beta")]
    monkeypatch.setattr(
        admin,
        "_embed",
        lambda _cfg, _texts: pytest.fail(
            "completed fingerprint must bypass embedding"
        ),
    )
    code, output, _error = _run(
        ["base", "import-git", *prefix, "--iwiki", "wiki-a", "--path", str(source), "--json"],
        environ,
    )
    assert code == 0
    assert json.loads(output)["already_imported"] is True
    monkeypatch.setattr(
        admin,
        "_embed",
        lambda _cfg, texts: [[1.0, 0.0, 0.0] for _text in texts],
    )

    (source / "docs" / "alpha.md").write_text(
        pages["docs/alpha.md"] + "changed\n", encoding="utf-8"
    )
    code, _output, error = _run(
        [
            "base", "import-git", *prefix, "--iwiki", "wiki-a",
            "--path", str(source), "--json",
        ],
        environ,
    )
    assert code == 1
    assert "target wiki is not empty" in error
    (source / "docs" / "alpha.md").write_text(
        pages["docs/alpha.md"], encoding="utf-8"
    )
    assert _run(
        [
            "domain", "create", *prefix, "--iwiki", "wiki-a",
            "--domain", "empty",
        ],
        environ,
    )[0] == 0

    dry_destination = tmp_path / "dry-export"
    code, output, _error = _run(
        [
            "base", "export-git", *prefix, "--iwiki", "wiki-a",
            "--path", str(dry_destination), "--dry-run", "--json",
        ],
        environ,
    )
    assert code == 0
    assert json.loads(output)["dry_run"] is True
    assert not dry_destination.exists()

    destination = tmp_path / "export"
    code, output, error = _run(
        ["base", "export-git", *prefix, "--iwiki", "wiki-a", "--path", str(destination), "--json"],
        environ,
    )
    exported = json.loads(output)
    assert code == 0
    assert error == ""
    assert exported["pages"] == 4
    assert exported["domains"] == 3
    assert exported["links"] == 1
    assert (destination / ".git").is_dir()
    commit_count = subprocess.run(
        ["git", "-C", str(destination), "rev-list", "--count", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert commit_count == "1"
    tracked = subprocess.run(
        ["git", "-C", str(destination), "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert "empty/.iwiki-domain" in tracked
    manifest = json.loads((destination / ".iwiki-export.json").read_text())
    assert manifest["page_hashes"] == exported["page_hashes"]
    assert {
        relative: (destination / relative).read_text(encoding="utf-8")
        for relative in pages
    } == pages
    exported_text = "\n".join(
        path.read_text(errors="ignore")
        for path in destination.rglob("*")
        if path.is_file() and ".git" not in path.parts
    )
    assert environ["IWIKI_DB_PASSWORD"] not in exported_text
    assert "token_digest" not in exported_text

    assert _run(["base", "create", *prefix, "--iwiki", "wiki-b"], environ)[0] == 0
    code, output, error = _run(
        [
            "base", "import-git", *prefix, "--iwiki", "wiki-b",
            "--path", str(destination), "--json",
        ],
        environ,
    )
    round_trip = json.loads(output)
    assert code == 0
    assert error == ""
    assert round_trip["pages"] == manifest["pages"]
    assert round_trip["links"] == manifest["links"]
    assert round_trip["domains"] == 3
    with psycopg.connect(service.dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT l.target_domain, l.target_slug FROM iwiki.links l "
                "WHERE l.iwiki_id = %s",
                ("wiki-b",),
            )
            assert cursor.fetchall() == [("docs", "beta")]

    repeated = tmp_path / "repeated"
    repeated.mkdir()
    (repeated / "occupied").write_text("keep", encoding="utf-8")
    code, _output, error = _run(
        [
            "base", "export-git", *prefix, "--iwiki", "wiki-a",
            "--path", str(repeated), "--json",
        ],
        environ,
    )
    assert code == 1
    assert "absent or empty" in error

    from iwiki_mcp import indexer

    monkeypatch.setattr(indexer, "embed_texts", admin._embed)
    engine_config = admin._engine_config(
        admin.load_server_config(str(config), environ), environ
    )
    stats = [
        indexer.index_domain(engine_config, str(destination), domain)
        for domain in ("docs", "private")
    ]
    assert sum(item["indexed_chunks"] for item in stats) == exported["chunks"]

    assert _run(
        ["base", "create", *prefix, "--iwiki", "wiki-empty"], environ
    )[0] == 0
    empty_destination = tmp_path / "empty-export"
    assert _run(
        [
            "base", "export-git", *prefix, "--iwiki", "wiki-empty",
            "--path", str(empty_destination), "--json",
        ],
        environ,
    )[0] == 0
    assert _run(
        ["base", "create", *prefix, "--iwiki", "wiki-empty-copy"], environ
    )[0] == 0
    code, output, error = _run(
        [
            "base", "import-git", *prefix, "--iwiki", "wiki-empty-copy",
            "--path", str(empty_destination), "--json",
        ],
        environ,
    )
    assert code == 0
    assert error == ""
    assert json.loads(output)["domains"] == 0
    assert json.loads(output)["pages"] == 0


def test_import_failure_rolls_back_every_domain_page_and_derived_row(
    admin_runtime, tmp_path, monkeypatch
):
    from iwiki_mcp import admin

    config, environ = admin_runtime
    prefix = ["--config", str(config)]
    source = tmp_path / "source"
    _write_git_fixture(source)
    monkeypatch.setattr(
        admin,
        "_embed",
        lambda _cfg, texts: [[1.0, 0.0, 0.0] for _text in texts],
    )
    assert _run(["base", "create", *prefix, "--iwiki", "wiki-a"], environ)[0] == 0

    original = admin.PostgresStore._replace_derived
    calls = 0

    def fail_after_first_page(store, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("fixture import failure")
        return original(store, *args, **kwargs)

    monkeypatch.setattr(
        admin.PostgresStore, "_replace_derived", fail_after_first_page
    )
    code, _output, error = _run(
        [
            "base", "import-git", *prefix, "--iwiki", "wiki-a",
            "--path", str(source), "--json",
        ],
        environ,
    )

    assert code == 1
    assert error == "iwiki-mcp: administration operation failed\n"
    code, output, _error = _run(
        ["base", "show", *prefix, "--iwiki", "wiki-a", "--json"],
        environ,
    )
    assert code == 0
    assert json.loads(output)["domains"] == 0
    assert json.loads(output)["pages"] == 0


def test_first_dry_run_never_initializes_database_schema(
    admin_runtime, tmp_path
):
    import psycopg
    from psycopg.conninfo import make_conninfo

    config, environ = admin_runtime
    source = tmp_path / "source"
    _write_git_fixture(source)

    code, output, error = _run(
        [
            "base", "import-git", "--config", str(config),
            "--iwiki", "wiki-a", "--path", str(source), "--dry-run", "--json",
        ],
        environ,
    )

    assert code == 1
    assert output == ""
    assert error == "iwiki-mcp: database operation failed\n"
    server_config = __import__(
        "iwiki_mcp.admin", fromlist=["admin"]
    ).load_server_config(str(config), environ)
    dsn = make_conninfo(
        host=server_config.storage.host,
        port=server_config.storage.port,
        dbname=server_config.storage.database,
        user=server_config.storage.user,
        password=server_config.storage.password,
        sslmode=server_config.storage.sslmode,
    )
    with psycopg.connect(dsn) as connection:
        assert connection.execute(
            "SELECT to_regnamespace('iwiki')"
        ).fetchone() == (None,)


@pytest.mark.parametrize("manifest", ["[]", "null", '"invalid"'])
def test_import_rejects_non_object_manifest_safely(
    admin_runtime, tmp_path, manifest
):
    config, environ = admin_runtime
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(
        ["git", "init", str(source)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    (source / ".iwiki-export.json").write_text(manifest, encoding="utf-8")

    code, output, error = _run(
        [
            "base", "import-git", "--config", str(config),
            "--iwiki", "wiki-a", "--path", str(source), "--dry-run", "--json",
        ],
        environ,
    )

    assert code == 1
    assert output == ""
    assert error == "iwiki-mcp: Git export manifest is invalid\n"


def test_import_ignores_top_level_metadata_but_keeps_nested_hidden_page(
    tmp_path,
):
    from iwiki_mcp import admin

    source = tmp_path / "source"
    pages = _write_git_fixture(source)
    metadata = source / ".github" / "instructions.md"
    metadata.parent.mkdir()
    metadata.write_text("# Metadata\n", encoding="utf-8")

    scanned, domains, _fingerprint = admin._scan_git(str(source))

    assert domains == ("docs", "private")
    assert {f"{domain}/{slug}.md" for domain, slug, _markdown in scanned} == set(
        pages
    )
