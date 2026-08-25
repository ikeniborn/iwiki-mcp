"""Read-only code-aware Wiki lint contracts."""
from __future__ import annotations

from pathlib import Path
import json

import pytest

from iwiki_mcp import server
from iwiki_mcp.codegraph import linking


_MATRIX_MARKDOWN = """---
code:
  symbols:
    - qualified_name: pkg.unknown
    - qualified_name: pkg.service.Service.run
  files:
    - src/pkg/service.py
    - missing.py
    - ../outside.py
    - ignored.py
    - credentials.py
  source_globs:
    - src/pkg/**
    - no/such/**
---
# Overview

## Links
[Missing](missing.md)
"""


@pytest.fixture
def seed_code_lint(seed_runtime, monkeypatch):
    project = seed_runtime.project_dir
    for root in ("root_a", "root_b"):
        package = project / root / "pkg"
        package.mkdir(parents=True)
        package.joinpath("__init__.py").write_text("", encoding="utf-8")
        package.joinpath("service.py").write_text(
            "class Service:\n    def run(self):\n        return None\n",
            encoding="utf-8",
        )
    project.joinpath("ignored.py").write_text("value = 1\n", encoding="utf-8")
    project.joinpath("credentials.py").write_text(
        "TOKEN = 'fixture'\n", encoding="utf-8"
    )
    project.joinpath(".gitignore").write_text("ignored.py\n", encoding="utf-8")

    page = Path(seed_runtime.binding.base) / "project" / "overview.md"
    page.write_text(
        "---\ncode:\n  files:\n    - src/pkg/service.py\n---\n"
        "# Overview\n\n## Links\n[Missing](missing.md)\n",
        encoding="utf-8",
    )
    assert seed_runtime.index(force=True)["state"] == "ready"
    page.write_text(_MATRIX_MARKDOWN, encoding="utf-8")

    monkeypatch.setattr(
        server.base, "resolve_binding", lambda: seed_runtime.binding
    )
    monkeypatch.setattr(
        server._codegraph_application,
        "code_runtime",
        lambda _source: seed_runtime.runtime,
    )
    return seed_runtime, page


def test_code_lint_finding_matrix_preserves_markdown_report(seed_code_lint):
    runtime, page = seed_code_lint
    before = page.read_bytes()

    report = server.wiki_lint("project")["reports"]["project"]

    assert {item["type"] for item in report["code_graph"]["findings"]} == {
        "unknown_symbol",
        "ambiguous_symbol",
        "missing_file",
        "empty_glob",
        "unsafe_selector",
        "ignored_selector",
        "secret_selector",
        "conflicting_selectors",
        "stale_revision",
    }
    assert report["code_graph"]["available"] is True
    assert report["code_graph"]["state"] == "ready"
    assert report["code_graph"]["revision"] == runtime.status()["revision"]
    assert report["broken"] == [{
        "page": str(page),
        "ref": "missing",
    }]
    assert page.read_bytes() == before
    assert runtime.build_attempts == 0


def test_unavailable_code_graph_does_not_block_ordinary_lint(
    seed_runtime, monkeypatch
):
    page = Path(seed_runtime.binding.base) / "project" / "overview.md"
    page.write_text(
        "# Overview\n\n## Links\n[Missing](missing.md)\n", encoding="utf-8"
    )
    missing = seed_runtime.with_state("missing", auto_rebuild="off")
    monkeypatch.setattr(server.base, "resolve_binding", lambda: missing.binding)
    monkeypatch.setattr(
        server._codegraph_application,
        "code_runtime",
        lambda _source: missing.runtime,
    )

    report = server.wiki_lint("project")["reports"]["project"]

    assert report["code_graph"] == {
        "available": False,
        "state": "missing",
        "revision": None,
        "findings": [],
        "hint": "run wiki_code_index",
    }
    assert report["broken"] == [{"page": str(page), "ref": "missing"}]
    assert missing.build_attempts == 0


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ({"enabled": False, "code": "not_configured"}, "disabled"),
        ({"enabled": True, "state": "missing", "revision": None}, "missing"),
        ({"enabled": True, "state": "dirty", "revision": "rev"}, "dirty"),
        ({"enabled": True, "state": "rebuilding", "revision": "rev"}, "rebuilding"),
        ({"enabled": True, "state": "failed", "revision": "rev"}, "failed"),
        (
            {
                "enabled": True,
                "state": "missing",
                "revision": None,
                "warnings": ["code_graph_incompatible"],
            },
            "incompatible",
        ),
    ],
)
def test_lint_domain_maps_non_ready_states_without_opening_graph(status, state):
    class Runtime:
        _store = None

        def status(self):
            return status

    report = linking.lint_domain(
        "unused", domain="project", runtime=Runtime()
    )

    assert report == {
        "available": False,
        "state": state,
        "revision": status.get("revision"),
        "findings": [],
        "hint": (
            "enable code_graph" if state == "disabled" else "run wiki_code_index"
        ),
    }


def test_lint_domain_sanitizes_unexpected_failure():
    class Runtime:
        def status(self):
            raise RuntimeError("do not leak this")

    report = linking.lint_domain(
        "unused", domain="project", runtime=Runtime()
    )

    assert report == {
        "available": False,
        "state": "failed",
        "revision": None,
        "findings": [],
        "hint": "inspect wiki_code_status and retry",
    }
    assert "do not leak this" not in repr(report)


def test_lint_domain_does_not_recover_or_rewrite_rebuilding_metadata(
    seed_runtime
):
    rebuilding = seed_runtime.with_state("rebuilding", auto_rebuild="off")
    before = rebuilding.paths.metadata.read_bytes()

    report = linking.lint_domain(
        str(Path(rebuilding.binding.base) / "project"),
        domain="project",
        runtime=rebuilding.runtime,
    )

    assert report["state"] == "rebuilding"
    assert rebuilding.paths.metadata.read_bytes() == before
    assert rebuilding.build_attempts == 0


def test_missing_lint_does_not_initialize_cache_or_lock(seed_runtime):
    missing = seed_runtime.with_state("missing", auto_rebuild="off")
    before = {
        path.name for path in missing.paths.database.parent.glob("code-project*")
    }

    report = linking.lint_domain(
        str(Path(missing.binding.base) / "project"),
        domain="project",
        runtime=missing.runtime,
    )

    after = {
        path.name for path in missing.paths.database.parent.glob("code-project*")
    }
    assert report["state"] == "missing"
    assert after == before


def test_include_tests_exclusion_is_ignored_not_missing(
    seed_runtime
):
    test_file = seed_runtime.project_file("tests/hidden.py")
    test_file.parent.mkdir()
    test_file.write_text("value = 1\n", encoding="utf-8")
    runtime = seed_runtime.with_config(include_tests=False, auto_rebuild="off")
    assert runtime.index(force=True)["state"] == "ready"
    page = Path(runtime.binding.base) / "project" / "overview.md"
    page.write_text(
        "---\ncode:\n  files:\n    - tests/hidden.py\n"
        "  source_globs:\n    - '**/hidden.py'\n---\n# Overview\n",
        encoding="utf-8",
    )

    report = linking.lint_domain(
        str(page.parent), domain="project", runtime=runtime.runtime
    )

    types = [item["type"] for item in report["findings"]]
    assert types.count("ignored_selector") == 2
    assert "missing_file" not in types
    assert "empty_glob" not in types


def test_gitignored_only_glob_is_an_ignored_selector(seed_runtime):
    hidden = seed_runtime.project_file("ignored_target.py")
    hidden.write_text("value = 1\n", encoding="utf-8")
    seed_runtime.project_file(".gitignore").write_text(
        "ignored_target.py\n", encoding="utf-8"
    )
    assert seed_runtime.index(force=True)["state"] == "ready"
    page = Path(seed_runtime.binding.base) / "project" / "overview.md"
    page.write_text(
        "---\ncode:\n  source_globs:\n"
        "    - ignored_*.py\n---\n# Overview\n",
        encoding="utf-8",
    )

    report = linking.lint_domain(
        str(page.parent), domain="project", runtime=seed_runtime.runtime
    )

    assert "ignored_selector" in {
        item["type"] for item in report["findings"]
    }
    assert "empty_glob" not in {
        item["type"] for item in report["findings"]
    }


def test_ready_lint_discards_findings_when_metadata_changes(
    ready_runtime, monkeypatch
):
    real = linking._lint_ready_domain
    calls = 0

    def change_after_read(*args, **kwargs):
        nonlocal calls
        result = real(*args, **kwargs)
        calls += 1
        metadata = json.loads(
            ready_runtime.paths.metadata.read_text(encoding="utf-8")
        )
        metadata["state"] = "rebuilding"
        ready_runtime.paths.metadata.write_text(
            json.dumps(metadata, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        return result

    monkeypatch.setattr(linking, "_lint_ready_domain", change_after_read)

    report = linking.lint_domain(
        str(Path(ready_runtime.binding.base) / "project"),
        domain="project",
        runtime=ready_runtime.runtime,
    )

    assert calls == 1
    assert report["available"] is False
    assert report["state"] == "rebuilding"
    assert report["findings"] == []


def test_duplicate_code_frontmatter_is_an_unsafe_selector(seed_runtime):
    assert seed_runtime.index(force=True)["state"] == "ready"
    page = Path(seed_runtime.binding.base) / "project" / "overview.md"
    page.write_text(
        "---\ncode:\n  files:\n    - src/pkg/service.py\n"
        "code:\n  files:\n    - src/pkg/other.py\n---\n# Overview\n",
        encoding="utf-8",
    )

    report = linking.lint_domain(
        str(page.parent), domain="project", runtime=seed_runtime.runtime
    )

    assert "unsafe_selector" in {
        item["type"] for item in report["findings"]
    }
