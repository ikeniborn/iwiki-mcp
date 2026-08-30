"""Schema-v4 marker-only rollback and compatibility maintenance artifact tests."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tarfile

import pytest


pytestmark = pytest.mark.postgres_integration
REPOSITORY = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPOSITORY / "compat" / "postgres-v4-runtime-guard.json"


def _settings(dsn):
    from iwiki_mcp.postgres.migrations import MigrationSettings

    return MigrationSettings(
        dsn=dsn,
        embed_model="fixture-model",
        embed_dimensions=3,
        statement_timeout_ms=30_000,
        lock_timeout_ms=5_000,
    )


def _source_tree_digest(tree: Path) -> str:
    entries = sorted(
        [
            path.relative_to(tree).as_posix(),
            hashlib.sha256(path.read_bytes()).hexdigest(),
        ]
        for path in tree.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(tree).parts
    )
    return hashlib.sha256(
        json.dumps(entries, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _export_pinned_commit(manifest, destination: Path) -> Path:
    archive = subprocess.run(
        ["git", "archive", "--format=tar", manifest["base_commit"]],
        cwd=REPOSITORY,
        check=True,
        capture_output=True,
    )
    destination.mkdir(parents=True)
    archive_path = destination.parent / f"{destination.name}.tar"
    archive_path.write_bytes(archive.stdout)
    with tarfile.open(archive_path) as tar:
        tar.extractall(destination, filter="data")
    return destination


def _build_maintenance_artifact(destination: Path) -> tuple[Path, dict]:
    """Reconstruct the reviewed compatibility build and verify every digest."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    patch_path = REPOSITORY / manifest["patch"]
    patch_bytes = patch_path.read_bytes()
    assert hashlib.sha256(patch_bytes).hexdigest() == manifest["patch_sha256"]

    tree = _export_pinned_commit(manifest, destination)
    subprocess.run(
        ["git", "init", "-q", "."], cwd=tree, check=True, capture_output=True
    )
    for command in (
        ["git", "apply", "--check", str(patch_path)],
        ["git", "apply", str(patch_path)],
    ):
        result = subprocess.run(cwd=tree, args=command, capture_output=True, text=True)
        if result.returncode != 0:
            pytest.fail(f"compatibility patch step failed: {result.stderr.strip()}")

    assert _source_tree_digest(tree) == manifest["source_tree_sha256"]
    return tree, manifest


def _write_server_config(path: Path, values, role: str) -> None:
    path.write_text(
        "[storage]\n"
        "type = \"postgres\"\n"
        f"host = {json.dumps(values['host'])}\n"
        f"port = {int(values.get('port', 5432))}\n"
        f"database = {json.dumps(values['dbname'])}\n"
        f"user = {json.dumps(role)}\n"
        f"sslmode = {json.dumps(values.get('sslmode', 'prefer'))}\n"
        "\n[server]\n"
        "host = \"127.0.0.1\"\n"
        "port = 8765\n"
        "allowed_origins = [\"https://iwiki.example\"]\n"
        "pool_min_size = 1\n"
        "pool_max_size = 2\n"
        "statement_timeout_ms = 30000\n"
        "lock_timeout_ms = 5000\n",
        encoding="utf-8",
    )


def _write_project_config(project: Path, values, role: str) -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / ".iwiki.toml").write_text(
        "read = [\"docs\"]\n"
        "write = [\"docs\"]\n"
        "primary = \"docs\"\n"
        "\n[storage]\n"
        "type = \"postgres\"\n"
        "iwiki_id = \"wiki-a\"\n"
        f"host = {json.dumps(values['host'])}\n"
        f"port = {int(values.get('port', 5432))}\n"
        f"database = {json.dumps(values['dbname'])}\n"
        f"user = {json.dumps(role)}\n"
        f"sslmode = {json.dumps(values.get('sslmode', 'prefer'))}\n",
        encoding="utf-8",
    )


HOSTED_DRIVER = r"""
import json
import sys
from pathlib import Path

from iwiki_mcp import http
from iwiki_mcp.postgres.store import PostgresStore

config_path, sentinel_path = sys.argv[1:]
try:
    runtime = http.prepare_runtime(config_path, probe=lambda _cfg: None)
except Exception as exc:
    cause = exc.__cause__ or exc
    print(f"STARTUP_CAUSE={type(cause).__name__}:{cause}", file=sys.stderr)
    raise
Path(sentinel_path).write_text("SCHEMA_GUARD_PASSED", encoding="utf-8")
runtime.close()
embed = lambda _cfg, texts: [[1.0, 0.0, 0.0] for _text in texts]
store = PostgresStore(runtime.dsn, "wiki-a", runtime.engine_config, embedder=embed)
markdown = (
    "---\ntype: concept\ntitle: Compat\ndescription: compat page\n"
    "tags: [fixture]\nstatus: stable\n---\n# Compat\n\n## Details\ncompat body\n"
)
changed = markdown.replace("compat body", "compat changed")
created = store.write_page("docs", "compat", markdown)
read = store.read_page("docs", "compat")
updated = store.update_page("docs", "compat", changed, read["revision"])
found = store.search(["docs"], "compat", top_k=5, threshold=0.0, mode="lexical")
deleted = store.delete_page("docs", "compat", updated["revision"])
assert created["revision"] == 1
assert read["markdown"] == markdown
assert updated["revision"] == 2
assert found and found[0]["domain"] == "docs"
assert deleted["deleted"] is True
print(json.dumps({"crud": True, "search": True}, sort_keys=True))
""".lstrip()


STDIO_DRIVER = r"""
import json
import sys
from pathlib import Path

from iwiki_mcp import server
from iwiki_mcp.engine.config import Config

sentinel_path = sys.argv[1]
cfg = Config.load()
try:
    server._initialize_postgres_storage(cfg)
except Exception as exc:
    cause = exc.__cause__ or exc
    print(f"STARTUP_CAUSE={type(cause).__name__}:{cause}", file=sys.stderr)
    raise
Path(sentinel_path).write_text("SCHEMA_GUARD_PASSED", encoding="utf-8")
print(json.dumps({"stdio_guard": True}, sort_keys=True))
""".lstrip()


def _run_driver(tree: Path, driver: Path, argv, environment, password):
    return subprocess.run(
        [sys.executable, str(driver), *argv],
        cwd=tree,
        env={**environment, "PYTHONPATH": str(tree / "src")},
        capture_output=True,
        text=True,
    )


def _cause(completed, password: str) -> str:
    diagnostic = completed.stderr.replace(password, "<redacted>").splitlines()
    return next(
        (line for line in diagnostic if line.startswith("STARTUP_CAUSE=")),
        diagnostic[-1] if diagnostic else "no stderr",
    )


def test_rollback_v5_compat_is_dry_run_then_marker_only_and_reapplicable(
    clean_postgres,
):
    import psycopg

    from iwiki_mcp.postgres.migrations import (
        MIGRATIONS,
        rollback_v5_compatibility,
        run_migrations,
    )

    run_migrations(_settings(clean_postgres), migrations=MIGRATIONS[:5])
    with psycopg.connect(clean_postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO iwiki.iwikis VALUES ('wiki-a', 'wiki-a')")
            cursor.execute(
                "INSERT INTO iwiki.domains (iwiki_id, slug, markdown_generation) "
                "VALUES ('wiki-a', 'docs', 7) RETURNING domain_id"
            )
            domain_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO iwiki.code_graph_snapshots "
                "(iwiki_id, domain_id, snapshot_id, state, header, "
                "graph_payload_revision, markdown_revision, markdown_generation, counts) "
                "VALUES ('wiki-a', %s, 'staged', 'staging', '{}', 'g', 'm', 7, '{}')",
                (domain_id,),
            )
        connection.commit()

    dry_run = rollback_v5_compatibility(_settings(clean_postgres), confirm=False)
    assert dry_run == {
        "dry_run": True,
        "schema_version": 5,
        "would_remove_marker": 5,
    }
    with psycopg.connect(clean_postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT max(version) FROM iwiki.schema_migrations")
            assert cursor.fetchone() == (5,)

    result = rollback_v5_compatibility(_settings(clean_postgres), confirm=True)
    assert result == {
        "dry_run": False,
        "schema_version": 4,
        "removed_marker": 5,
    }
    with psycopg.connect(clean_postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT to_regclass('iwiki.code_graph_snapshots') IS NOT NULL"
            )
            assert cursor.fetchone() == (True,)
            cursor.execute("SELECT markdown_generation FROM iwiki.domains")
            assert cursor.fetchone() == (7,)
            cursor.execute("SELECT snapshot_id FROM iwiki.code_graph_snapshots")
            assert cursor.fetchone() == ("staged",)

    reapplied = run_migrations(
        _settings(clean_postgres), migrations=MIGRATIONS[:5]
    )
    assert reapplied.applied_versions == (5,)
    with psycopg.connect(clean_postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM iwiki.schema_migrations WHERE version = 5"
            )
            assert cursor.fetchone() == (1,)
            cursor.execute("SELECT snapshot_id FROM iwiki.code_graph_snapshots")
            assert cursor.fetchone() == ("staged",)


def test_rollback_sql_is_reviewable_marker_only():
    from iwiki_mcp.postgres.migrations import SCHEMA5_COMPATIBILITY_ROLLBACK_SQL

    normalized = " ".join(SCHEMA5_COMPATIBILITY_ROLLBACK_SQL.lower().split())
    assert "pg_advisory_xact_lock" in normalized
    assert "delete from iwiki.schema_migrations" in normalized
    assert "drop table" not in normalized
    assert "drop policy" not in normalized
    assert "truncate" not in normalized


def test_compatibility_patch_is_read_only_and_reviewable(tmp_path):
    patch = (
        REPOSITORY / json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["patch"]
    ).read_text(encoding="utf-8")
    added = "\n".join(
        line[1:] for line in patch.splitlines() if line.startswith("+")
    ).lower()
    assert "require_schema_version" in added
    assert "set transaction read only" in added
    for forbidden in (
        "create table",
        "alter table",
        "create schema",
        "run_migrations(",
        "insert into iwiki.schema_migrations",
        "password",
    ):
        assert forbidden not in added

    tree, manifest = _build_maintenance_artifact(tmp_path / "artifact")
    assert manifest["schema_version"] == 4
    guarded = (tree / "src" / "iwiki_mcp" / "http.py").read_text(encoding="utf-8")
    assert "require_schema_version(dsn)" in guarded
    assert "run_migrations(" not in guarded


def test_compatibility_artifact_serves_pre_v5_runtime_under_restricted_roles(
    clean_postgres, tmp_path
):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict

    from iwiki_mcp.postgres.auth import AuthStore
    from iwiki_mcp.postgres.migrations import (
        rollback_v6_compatibility,
        rollback_v7_compatibility,
        rollback_v5_compatibility,
        run_migrations,
    )
    from iwiki_mcp.postgres.store import provision_runtime_grant

    settings = _settings(clean_postgres)
    run_migrations(settings)
    auth = AuthStore(clean_postgres)
    auth.create_wiki("wiki-a", "wiki-a")
    auth.create_domain("wiki-a", "docs")
    suffix = secrets.token_hex(5)
    roles = {
        "hosted": f"iwiki_t3_compathosted_{suffix}",
        "direct": f"iwiki_t3_compatdirect_{suffix}",
    }
    passwords = {name: secrets.token_urlsafe(24) for name in roles}
    with psycopg.connect(clean_postgres, autocommit=True) as connection:
        with connection.cursor() as cursor:
            for name, role in roles.items():
                try:
                    cursor.execute(
                        sql.SQL("CREATE ROLE {} LOGIN NOBYPASSRLS PASSWORD {}").format(
                            sql.Identifier(role), sql.Literal(passwords[name])
                        )
                    )
                except psycopg.Error:
                    pytest.fail("disposable compatibility role setup failed")
    try:
        for name, role in roles.items():
            provision_runtime_grant(
                clean_postgres,
                principal=role,
                iwiki_id="wiki-a",
                read_domains=["docs"],
                write_domains=["docs"],
                runtime=name,
            )
        rollback_v7_compatibility(settings, confirm=True)
        rollback_v6_compatibility(settings, confirm=True)
        with psycopg.connect(clean_postgres) as connection:
            with connection.cursor() as cursor:
                for role in roles.values():
                    cursor.execute(
                        "SELECT has_database_privilege(%s, current_database(), "
                        "'CREATE'), has_schema_privilege(%s, 'iwiki', 'CREATE'), "
                        "has_table_privilege(%s, 'iwiki.schema_migrations', "
                        "'INSERT, UPDATE, DELETE')",
                        (role, role, role),
                    )
                    assert cursor.fetchone() == (False, False, False)

        rollback_v5_compatibility(settings, confirm=True)
        tree, _manifest = _build_maintenance_artifact(tmp_path / "artifact")
        values = conninfo_to_dict(clean_postgres)
        base_environment = {
            **os.environ,
            "IWIKI_LLM_BASE_URL": "http://example.invalid/v1",
            "IWIKI_LLM_KEY": "fixture-key",
            "IWIKI_EMBED_MODEL": "fixture-model",
            "IWIKI_EMBED_DIMENSIONS": "3",
            "IWIKI_RERANK_MODEL": "",
        }

        hosted_config = tmp_path / "server.toml"
        _write_server_config(hosted_config, values, roles["hosted"])
        hosted_sentinel = tmp_path / "hosted-guard"
        hosted_driver = tmp_path / "hosted_driver.py"
        hosted_driver.write_text(HOSTED_DRIVER, encoding="utf-8")
        hosted = _run_driver(
            tree,
            hosted_driver,
            [str(hosted_config), str(hosted_sentinel)],
            {**base_environment, "IWIKI_DB_PASSWORD": passwords["hosted"]},
            passwords["hosted"],
        )
        if hosted.returncode != 0:
            pytest.fail(
                "hosted compatibility smoke failed: "
                f"exit={hosted.returncode}, sentinel={hosted_sentinel.exists()}, "
                f"cause={_cause(hosted, passwords['hosted'])}"
            )
        assert hosted_sentinel.read_text(encoding="utf-8") == "SCHEMA_GUARD_PASSED"
        assert json.loads(hosted.stdout.splitlines()[-1]) == {
            "crud": True,
            "search": True,
        }

        project = tmp_path / "project"
        _write_project_config(project, values, roles["direct"])
        stdio_sentinel = tmp_path / "stdio-guard"
        stdio_driver = tmp_path / "stdio_driver.py"
        stdio_driver.write_text(STDIO_DRIVER, encoding="utf-8")
        stdio = _run_driver(
            tree,
            stdio_driver,
            [str(stdio_sentinel)],
            {
                **base_environment,
                "IWIKI_DB_PASSWORD": passwords["direct"],
                "IWIKI_PROJECT_DIR": str(project),
            },
            passwords["direct"],
        )
        if stdio.returncode != 0:
            pytest.fail(
                "stdio compatibility smoke failed: "
                f"exit={stdio.returncode}, sentinel={stdio_sentinel.exists()}, "
                f"cause={_cause(stdio, passwords['direct'])}"
            )
        assert stdio_sentinel.read_text(encoding="utf-8") == "SCHEMA_GUARD_PASSED"

        raw = _export_pinned_commit(
            json.loads(MANIFEST_PATH.read_text(encoding="utf-8")), tmp_path / "raw"
        )
        control_sentinel = tmp_path / "control-guard"
        control = _run_driver(
            raw,
            hosted_driver,
            [str(hosted_config), str(control_sentinel)],
            {**base_environment, "IWIKI_DB_PASSWORD": passwords["hosted"]},
            passwords["hosted"],
        )
        assert control.returncode != 0
        assert control_sentinel.exists() is False

        with psycopg.connect(clean_postgres) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT version FROM iwiki.schema_migrations ORDER BY 1")
                assert [row[0] for row in cursor.fetchall()] == [1, 2, 3, 4]

        reapplied = run_migrations(settings)
        assert reapplied.applied_versions == (5, 6, 7)
    finally:
        with psycopg.connect(clean_postgres, autocommit=True) as connection:
            with connection.cursor() as cursor:
                for role in roles.values():
                    cursor.execute(
                        "DELETE FROM iwiki.database_principal_domain_grants "
                        "WHERE principal = %s",
                        (role,),
                    )
                    cursor.execute(
                        sql.SQL("DROP OWNED BY {} CASCADE").format(
                            sql.Identifier(role)
                        )
                    )
                    cursor.execute(
                        sql.SQL("DROP ROLE {}").format(sql.Identifier(role))
                    )
