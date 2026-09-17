"""Admin import resolves each domain's own specification policy.

``admin_runtime`` (duplicated here from ``tests/postgres/test_admin.py:23`` -- a local
fixture, not something importable from that module) yields ``(config, environ)``, not a
service object, so this test builds the service the same way
``test_import_export_round_trip_is_transactional_and_secret_free`` does: run the CLI
through ``admin.run`` and then reach into ``admin._service`` for direct store access.

``import_pages`` (``src/iwiki_mcp/postgres/store.py:436``) never touches
``iwiki.specification_scenarios`` by itself — it only inserts pages, chunks, and links; a
domain's projection is built the same way it always is, through ``PostgresStore.index_domain``
(``store.py:2093``). That method is what actually calls ``self._mode_for(domain)`` to decide
whether to persist a projection, so it is what proves the fix: before this change,
``AdminService._store`` built a plain ``PostgresStore`` with the constructor default
``specification_mode="optional"``, so *every* domain — including one whose hosted policy
says ``disabled`` — got a projection. This test imports a Git tree into two domains, indexes
both through the same resolver-backed store the CLI now builds, and asserts the ``disabled``
domain gets no rows in ``iwiki.specification_scenarios`` while the other domain does.

The projection table name and columns come from
``tests/postgres/test_specification_migrations.py`` (``iwiki.specification_scenarios``,
keyed by ``iwiki_id`` and ``domain_id``), not from a guess.
"""
from __future__ import annotations

from io import StringIO
import json
import subprocess

import psycopg
import pytest

from iwiki_mcp.postgres.config import HostedSpecificationsConfig, PolicyOverride


pytestmark = pytest.mark.postgres_integration


class RedactedEnv(dict):
    """Environment mapping whose pytest representation never exposes secrets."""

    def __repr__(self):
        return "<redacted admin environment>"


@pytest.fixture
def admin_runtime(clean_postgres, tmp_path):
    """Same shape as ``tests/postgres/test_admin.py:23`` -- (config path, environ)."""
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


def _page(scenario_id: str) -> str:
    """One specification page whose single fence is valid and complete.

    ``assemble_projection`` (``src/iwiki_mcp/specifications.py:198``) skips any page
    without ``type: specification`` frontmatter, so the page needs it even though the
    plan's sketch omitted it.
    """
    return (
        "---\n"
        "type: specification\n"
        "---\n"
        "# Billing\n\n## Scenario\n\n"
        "Lead paragraph under the heading.\n\n"
        "```iwiki-gwt\n"
        f'id = "{scenario_id}"\n'
        f'title = "{scenario_id}"\n'
        'given = [{ role = "state", name = "Ready" }]\n'
        'when = { role = "command", name = "Do" }\n'
        'then = [{ role = "event", name = "Done" }]\n'
        'code = [\n'
        '  { relation = "implements", phase = "when", symbol = "pkg.mod.fn" },\n'
        '  { relation = "verifies", file = "tests/test_x.py" },\n'
        ']\n'
        "```\n"
    )


def _run(argv, environ):
    from iwiki_mcp import admin

    stdout = StringIO()
    stderr = StringIO()
    code = admin.run(argv, environ=environ, stdout=stdout, stderr=stderr)
    return code, stdout.getvalue(), stderr.getvalue()


def _projection_rows(dsn, iwiki_id, domain):
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM iwiki.specification_scenarios s "
                "JOIN iwiki.domains d ON d.iwiki_id = s.iwiki_id "
                "AND d.domain_id = s.domain_id "
                "WHERE d.iwiki_id = %s AND d.slug = %s",
                (iwiki_id, domain),
            )
            return cursor.fetchone()[0]


def test_admin_store_honours_a_disabled_domain_after_a_git_import(
    admin_runtime, tmp_path, monkeypatch
):
    from iwiki_mcp import admin

    config, environ = admin_runtime
    prefix = ["--config", str(config)]
    source = tmp_path / "source"
    for domain in ("payments", "billing"):
        (source / domain).mkdir(parents=True)
        (source / domain / "scenario.md").write_text(
            _page(f"{domain}-scenario"), encoding="utf-8"
        )
    subprocess.run(
        ["git", "init", str(source)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    monkeypatch.setattr(
        admin,
        "_embed",
        lambda _cfg, texts: [[1.0, 0.0, 0.0] for _text in texts],
    )

    assert _run(["base", "create", *prefix, "--iwiki", "wiki-a"], environ)[0] == 0
    code, _output, error = _run(
        [
            "base", "import-git", *prefix, "--iwiki", "wiki-a",
            "--path", str(source), "--json",
        ],
        environ,
    )
    assert code == 0, error

    service = admin._service(str(config), environ)
    service.config = admin.ServerConfig(
        storage=service.config.storage,
        models=service.config.models,
        server=service.config.server,
        code_graph=service.config.code_graph,
        specifications=HostedSpecificationsConfig(
            overrides=(
                PolicyOverride("wiki-a", "billing", {"specification_mode": "disabled"}),
            )
        ),
    )
    store = service._store("wiki-a")
    for domain in ("payments", "billing"):
        store.index_domain(domain)

    assert _projection_rows(service.dsn, "wiki-a", "billing") == 0
    assert _projection_rows(service.dsn, "wiki-a", "payments") > 0
