"""Small real Git/wiki projects for code-graph CLI coverage."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

from iwiki_mcp.engine.config import Config


def _git(directory: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
    )


def _postgres_config() -> Config:
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


def _embed(_config: Config, texts: list[str]) -> list[list[float]]:
    return [[0.0, 0.0, 1.0] for _text in texts]


class PostgresSyntheticProject:
    """Synthetic checkout without a printable database connection string."""

    def __init__(self, project: Path, store, markdown: str) -> None:
        self.project = project
        self._store = store
        self._markdown = markdown

    def __repr__(self) -> str:
        return "<redacted PostgreSQL synthetic project>"

    @property
    def markdown_bytes(self) -> bytes:
        return self._markdown.encode("utf-8")

    def stored_markdown_bytes(self) -> bytes:
        page = self._store.read_page("docs", "architecture")
        if page is None:
            raise AssertionError("synthetic Markdown page disappeared")
        return page["markdown"].encode("utf-8")


def create_sqlite_project(tmp_path: Path) -> Path:
    """Create a self-contained Git project with a Git-backed SQLite wiki."""
    project = tmp_path / "project"
    wiki = tmp_path / "wiki"
    docs = wiki / "docs"
    project.mkdir()
    docs.mkdir(parents=True)
    docs.joinpath("architecture.md").write_text(
        "---\n"
        "title: Architecture\n"
        "source: synthetic\n"
        "---\n\n"
        "# Architecture\n\n"
        "## Service\n\n"
        "Service.run coordinates helper work.\n",
        encoding="utf-8",
    )
    project.joinpath("service.py").write_text(
        "class Service:\n"
        "    def run(self):\n"
        "        return helper()\n\n\n"
        "def helper():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    project.joinpath(".iwiki.toml").write_text(
        "\n".join((
            f"base = {json.dumps(str(wiki))}",
            'read = ["docs"]',
            'write = ["docs"]',
            'primary = "docs"',
            "",
            "[code_graph]",
            "enabled = true",
            'languages = ["python"]',
            'publish_mode = "sqlite"',
            'read_mode = "sqlite"',
            "max_full_rebuild_seconds = 30",
            "",
        )),
        encoding="utf-8",
    )
    for directory in (project, wiki):
        _git(directory, "init", "-q")
    _git(project, "add", ".")
    _git(
        project,
        "-c",
        "user.name=iwiki-mcp",
        "-c",
        "user.email=iwiki-mcp@localhost",
        "commit",
        "-qm",
        "seed synthetic project",
    )
    _git(wiki, "add", ".")
    _git(
        wiki,
        "-c",
        "user.name=iwiki-mcp",
        "-c",
        "user.email=iwiki-mcp@localhost",
        "commit",
        "-qm",
        "seed synthetic wiki",
    )
    return project


def create_postgres_project(
    tmp_path: Path,
    clean_postgres,
    runtime_principal,
    monkeypatch,
) -> PostgresSyntheticProject:
    """Create a Git checkout bound to one disposable PostgreSQL wiki."""
    from psycopg.conninfo import conninfo_to_dict

    from iwiki_mcp.postgres.migrations import MigrationSettings, run_migrations
    from iwiki_mcp.postgres.store import PostgresStore

    config = _postgres_config()
    run_migrations(
        MigrationSettings(
            dsn=clean_postgres,
            embed_model=config.embed_model,
            embed_dimensions=config.dimensions,
            statement_timeout_ms=30_000,
            lock_timeout_ms=5_000,
        )
    )
    store = PostgresStore(
        clean_postgres,
        "wiki-a",
        config,
        embedder=_embed,
    )
    store.create_wiki("wiki-a")
    store.create_domain("docs")
    markdown = (
        "---\n"
        "type: concept\n"
        "title: Architecture\n"
        "description: Synthetic PostgreSQL publication fixture.\n"
        "tags: [fixture]\n"
        "status: stable\n"
        "---\n"
        "# Architecture\n\n"
        "## Service\n\n"
        "Service.run coordinates helper work.\n"
    )
    created = store.write_page("docs", "architecture", markdown)
    if created.get("page") != "docs/architecture.md":
        raise AssertionError("synthetic Markdown page was not created")

    role, password = runtime_principal(
        ["docs"],
        ["docs"],
        runtime="direct",
        iwiki_id="wiki-a",
    )
    values = conninfo_to_dict(clean_postgres)
    host = values.get("host")
    database = values.get("dbname")
    if not host or not database:
        raise AssertionError(
            "IWIKI_TEST_POSTGRES_DSN must include host and database"
        )

    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init", "-q")
    project.joinpath("service.py").write_text(
        "class Service:\n"
        "    def run(self):\n"
        "        return helper()\n\n\n"
        "def helper():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    project.joinpath(".iwiki.toml").write_text(
        "\n".join((
            'read = ["docs"]',
            'write = ["docs"]',
            'primary = "docs"',
            "",
            "[storage]",
            'type = "postgres"',
            f"host = {json.dumps(host)}",
            f"port = {int(values.get('port') or 5432)}",
            f"database = {json.dumps(database)}",
            f"user = {json.dumps(role)}",
            f"sslmode = {json.dumps(values.get('sslmode') or 'prefer')}",
            'iwiki_id = "wiki-a"',
            "",
            "[code_graph]",
            "enabled = true",
            'languages = ["python"]',
            'publish_mode = "postgres"',
            'read_mode = "postgres"',
            "max_full_rebuild_seconds = 30",
            "",
        )),
        encoding="utf-8",
    )
    _git(project, "add", ".")
    _git(
        project,
        "-c",
        "user.name=iwiki-mcp",
        "-c",
        "user.email=iwiki-mcp@localhost",
        "commit",
        "-qm",
        "seed synthetic project",
    )

    monkeypatch.setenv("IWIKI_DB_PASSWORD", password)
    monkeypatch.setenv("IWIKI_EMBED_MODEL", config.embed_model)
    monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", str(config.dimensions))
    monkeypatch.setenv("IWIKI_RERANK_MODEL", "")
    monkeypatch.setenv("IWIKI_CODE_GRAPH_ENABLED", "true")
    monkeypatch.delenv("IWIKI_CODE_GRAPH_MAX_FILE_BYTES", raising=False)
    monkeypatch.delenv("IWIKI_CODE_GRAPH_MAX_FILES", raising=False)
    monkeypatch.delenv("IWIKI_CODE_GRAPH_AUTO_REBUILD", raising=False)
    return PostgresSyntheticProject(project, store, markdown)
