"""Small real Git/wiki projects for code-graph CLI coverage."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess


def _git(directory: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
    )


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
