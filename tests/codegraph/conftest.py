"""Task-scoped code graph runtime test harnesses."""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import replace
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from iwiki_mcp.base import Binding
from iwiki_mcp.codegraph.config import CodeGraphConfig
from iwiki_mcp.codegraph.indexer import AdapterFactory
from iwiki_mcp.codegraph.languages.python import PythonAdapter
from iwiki_mcp.codegraph.location import CodeGraphLocationResolver
from iwiki_mcp.codegraph.runtime import (
    CodeGraphRuntime,
    shutdown_code_graph_workers,
)
from iwiki_mcp.codegraph.schema import CodeGraphStoreError
from iwiki_mcp.codegraph.store import code_graph_write_lock


def _distribution_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unavailable"


def _runtime(
    binding: Binding,
    *,
    parser_version: str | None = None,
    grammar_version: str | None = None,
    adapter_version: str = "python-adapter-v1",
    resolver_version: str = "resolver-v1",
) -> CodeGraphRuntime:
    parser_version = parser_version or (
        "tree-sitter-python:" + _distribution_version("tree-sitter-python")
    )
    grammar_version = grammar_version or ";".join((
        "tree-sitter:" + _distribution_version("tree-sitter"),
        "tree-sitter-language-pack:"
        + _distribution_version("tree-sitter-language-pack"),
        parser_version,
    ))
    return CodeGraphRuntime(
        binding,
        adapter_factories={
            "python": AdapterFactory(
                create=PythonAdapter,
                parser_version=parser_version,
                grammar_version=grammar_version,
                adapter_version=adapter_version,
            )
        },
        resolver_version=resolver_version,
    )


def _git(directory: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_config(project: Path, base: Path, **overrides) -> None:
    values = {
        "enabled": True,
        "languages": ["python"],
        "auto_rebuild": "off",
        "max_rebuild_seconds": 2,
        "max_file_bytes": 1_000_000,
        "max_total_files": 100,
        "include_tests": True,
        **overrides,
    }
    languages = ", ".join(json.dumps(item) for item in values["languages"])
    project.joinpath(".iwiki.toml").write_text(
        "\n".join(
            (
                f"base = {json.dumps(str(base))}",
                'read = ["project"]',
                'write = ["project"]',
                'primary = "project"',
                "",
                "[code_graph]",
                f"enabled = {str(values['enabled']).lower()}",
                f"languages = [{languages}]",
                f"auto_rebuild = {json.dumps(values['auto_rebuild'])}",
                f"max_rebuild_seconds = {values['max_rebuild_seconds']}",
                f"max_file_bytes = {values['max_file_bytes']}",
                f"max_total_files = {values['max_total_files']}",
                f"include_tests = {str(values['include_tests']).lower()}",
                "",
            )
        ),
        encoding="utf-8",
    )


class RuntimeHarness:
    def __init__(
        self,
        binding: Binding,
        monkeypatch: pytest.MonkeyPatch,
        *,
        runtime: CodeGraphRuntime | None = None,
        owned_runtimes: list[CodeGraphRuntime] | None = None,
    ) -> None:
        self.binding = binding
        self.project_dir = Path(binding.project_dir)
        self.paths = CodeGraphLocationResolver(
            binding.base, binding.primary or "project", binding.project_dir
        ).resolve()
        self.database_accesses: list[str] = []
        self.embedding_requests: list[object] = []
        self.build_attempts = 0
        self._monkeypatch = monkeypatch
        self.runtime = runtime or _runtime(binding)
        self._owned_runtimes = (
            owned_runtimes if owned_runtimes is not None else []
        )
        self._owned_runtimes.append(self.runtime)

    def status(self):
        return self.runtime.status()

    def index(self, *, force=False, languages=None):
        return self.runtime.index(force=force, languages=languages)

    def query_guard(self):
        return self.runtime.query_guard()

    def with_config(self, **overrides):
        _write_config(self.project_dir, Path(self.binding.base), **overrides)
        return RuntimeHarness(
            self.binding,
            self._monkeypatch,
            owned_runtimes=self._owned_runtimes,
        )

    def with_toolchain_versions(
        self, *, parser, adapter, resolver, grammar=None
    ):
        return RuntimeHarness(
            self.binding,
            self._monkeypatch,
            runtime=_runtime(
                self.binding,
                parser_version=parser,
                grammar_version=grammar,
                adapter_version=adapter,
                resolver_version=resolver,
            ),
            owned_runtimes=self._owned_runtimes,
        )

    def with_state(self, state, **config):
        harness = self.with_config(**config)
        if state == "missing":
            for path in (harness.paths.database, harness.paths.wal,
                         harness.paths.shm, harness.paths.metadata):
                path.unlink(missing_ok=True)
            return harness
        built = harness.index(force=True)
        assert built["state"] == "ready"
        with closing(sqlite3.connect(harness.paths.database)) as connection:
            connection.execute(
                "UPDATE repositories SET state = ? WHERE repository_id = ?",
                (state, harness.binding.primary),
            )
            connection.commit()
        metadata = json.loads(harness.paths.metadata.read_text(encoding="utf-8"))
        metadata["state"] = state
        harness.paths.metadata.write_text(
            json.dumps(metadata, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        original = harness.runtime._indexer.build

        def counted_build(**kwargs):
            harness.build_attempts += 1
            return original(**kwargs)

        self._monkeypatch.setattr(
            harness.runtime._indexer, "build", counted_build
        )
        return harness

    @property
    def fail_before_publish(self):
        return False

    @fail_before_publish.setter
    def fail_before_publish(self, enabled):
        if not enabled:
            return

        def fail(*_args, **_kwargs):
            raise CodeGraphStoreError("injected publication failure")

        self._monkeypatch.setattr(self.runtime._indexer.store, "replace_staging", fail)

    def fail_with_message(self, message):
        def fail(*_args, **_kwargs):
            raise CodeGraphStoreError(message)

        self._monkeypatch.setattr(self.runtime._indexer, "build", fail)

    def project_file(self, relative_path):
        return self.project_dir / relative_path

    def git_status(self):
        return _git(Path(self.binding.base), "status", "--porcelain=v1")

    def wiki_hashes(self):
        domain = Path(self.binding.base) / (self.binding.primary or "project")
        return {
            path.relative_to(domain).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(domain.rglob("*"))
            if path.is_file()
        }

    @contextmanager
    def hold_publication_lock(self):
        with code_graph_write_lock(self.paths.lock, timeout=1):
            yield


class FakeRuntime:
    """Small server-facing double; production-only behavior stays out of fixtures."""

    def __init__(self, binding, calls=None, config=None, state="missing"):
        self.binding = binding
        self.calls = calls if calls is not None else []
        self.config = config or CodeGraphConfig()
        self.state = state
        self.embedding_requests = []
        self.database_accesses = []
        self.build_attempts = 0
        self._failure = None
        self.project_dir = Path(binding.project_dir)

    def with_config(self, **changes):
        return FakeRuntime(
            self.binding, self.calls, replace(self.config, **changes), self.state
        )

    def with_state(self, state, **changes):
        return FakeRuntime(
            self.binding, self.calls, replace(self.config, **changes), state
        )

    def fail_with_message(self, message):
        self._failure = message

    def status(self):
        self.calls.append(("status", {}))
        if not self.binding.primary or not self.config.enabled:
            return {
                "error": "code graph is not configured",
                "code": "not_configured",
                "hint": "configure a primary domain and enable code_graph",
            }
        self.database_accesses.append("status")
        return {"domain": self.binding.primary, "state": self.state}

    def index(self, *, force=False, languages=None):
        self.calls.append(("index", {"force": force, "languages": languages}))
        self.build_attempts += 1
        if self._failure:
            return {
                "error": "code graph rebuild failed",
                "code": "rebuild_failed",
                "hint": "inspect wiki_code_status and retry",
            }
        self.database_accesses.append("index")
        self.state = "ready"
        return {"domain": self.binding.primary, "state": "ready", "fresh": True}

    def query_guard(self):
        if self.binding.primary and self.config.enabled:
            self.database_accesses.append("query")
        return {
            "domain": self.binding.primary,
            "state": self.state,
            "fresh": self.state == "ready",
            "results": [],
            "hint": "run wiki_code_index" if self.state != "ready" else None,
        }

    def project_file(self, relative_path):
        return self.project_dir / relative_path

    def git_status(self):
        return ""

    def wiki_hashes(self):
        return {}

    @contextmanager
    def hold_publication_lock(self):
        yield


@pytest.fixture
def seed_binding(tmp_path):
    base = tmp_path / "base"
    project = tmp_path / "project"
    base.mkdir()
    project.mkdir()
    _git(base, "init", "-q")
    _git(base, "config", "user.email", "test@example.com")
    _git(base, "config", "user.name", "Test User")
    (base / "project").mkdir()
    (base / "project" / "overview.md").write_text("# Project\n", encoding="utf-8")
    _git(base, "add", "-A")
    _git(base, "commit", "-q", "-m", "seed wiki")

    _git(project, "init", "-q")
    _git(project, "config", "user.email", "test@example.com")
    _git(project, "config", "user.name", "Test User")
    (project / "src" / "pkg").mkdir(parents=True)
    (project / "src" / "pkg" / "service.py").write_text(
        "class Service:\n    def run(self, value: int = 1) -> str:\n"
        "        return str(value)\n",
        encoding="utf-8",
    )
    _write_config(project, base)
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "seed project")
    return Binding(
        base=str(base),
        read=("project",),
        write=("project",),
        primary="project",
        project_dir=str(project),
    )


@pytest.fixture(autouse=True)
def reset_code_graph_worker_registry():
    shutdown_code_graph_workers(timeout=5)
    yield
    shutdown_code_graph_workers(timeout=5)


@pytest.fixture
def seed_without_primary(seed_binding):
    return Binding(
        base=seed_binding.base,
        read=seed_binding.read,
        write=(),
        primary=None,
        project_dir=seed_binding.project_dir,
    )


@pytest.fixture
def seed_runtime(seed_binding, monkeypatch):
    harness = RuntimeHarness(seed_binding, monkeypatch)
    yield harness
    for runtime in harness._owned_runtimes:
        runtime.join_workers(timeout=5)
        assert runtime.active_workers == 0


@pytest.fixture
def ready_runtime(seed_runtime):
    assert seed_runtime.index(force=True)["state"] == "ready"
    return seed_runtime


@pytest.fixture
def fake_runtime_factory():
    return lambda binding, calls=None: FakeRuntime(binding, calls)


@pytest.fixture
def production_runtime_factory():
    return _runtime
