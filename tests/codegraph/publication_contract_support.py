"""Route-agnostic publication contract harness shared by adapter suites."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Iterator

from iwiki_mcp.codegraph.publication import (
    SnapshotBatch,
    SnapshotHeader,
    canonical_batch,
    iter_snapshot_batches,
)


@dataclass
class PublicationContractHarness:
    """Drive one publication route through the shared lifecycle contract."""

    route: str
    header: SnapshotHeader
    rows: dict
    publisher: Any
    reader: Any
    supports_commit_uncertain: bool
    max_batch_rows: int = 1
    max_batch_bytes: int = 65_536
    owner_factory: Callable[[str], Any] | None = None
    replacement_factory: Callable[[], Any] | None = None
    raw_rows: Callable[[], dict] | None = None
    finalize_lock: Callable[[], Any] | None = None
    session_ref: Callable[[str], Any] | None = None
    _observed: set = field(default_factory=set)
    _failures: set = field(default_factory=set)

    def __repr__(self) -> str:
        return f"<publication contract route {self.route}>"

    # -- batches --------------------------------------------------------

    def batches(self, header: SnapshotHeader | None = None) -> tuple:
        del header
        return tuple(
            iter_snapshot_batches(
                self.rows,
                max_rows=self.max_batch_rows,
                max_bytes=self.max_batch_bytes,
            )
        )

    def mismatched_hash(self, batch: SnapshotBatch) -> SnapshotBatch:
        """Keep the rows but declare a hash they cannot produce."""
        return replace(batch, payload_hash="sha256:" + "0" * 64)

    def divergent(self, batch: SnapshotBatch) -> SnapshotBatch:
        """Return one honestly hashed batch whose rows differ at the ordinal."""
        rows = json.loads(bytes(batch.payload).decode("utf-8"))
        assert rows, "divergent batch needs at least one row"
        field_name = next(
            (
                name
                for name in ("path", "git_commit", "qualified_name", "kind")
                if isinstance(rows[0].get(name), str)
            ),
            None,
        )
        assert field_name is not None, f"no mutable field in {batch.kind} row"
        mutated = [
            {**rows[0], field_name: rows[0][field_name] + "-diverged"},
            *rows[1:],
        ]
        return canonical_batch(batch.kind, batch.ordinal, mutated)

    def miscounted(self, batch: SnapshotBatch) -> SnapshotBatch:
        return replace(batch, row_count=batch.row_count + 1)

    # -- lifecycle ------------------------------------------------------

    def begin(self, _tag: str = "contract"):
        result = self.publisher.begin(self.header)
        self._record(result)
        return result

    def publish_one(self, session) -> dict:
        result = self.publisher.publish_batch(session, self.batches()[0])
        self._record(result)
        return result

    def publish_half(self, session) -> None:
        batches = self.batches()
        for batch in batches[: max(1, len(batches) // 2)]:
            self._record(self.publisher.publish_batch(session, batch))

    def publish_complete_batches(self, session) -> None:
        for batch in self.batches():
            self._record(self.publisher.publish_batch(session, batch))

    def finalize(self, session) -> dict:
        result = self.publisher.finalize(session)
        self._record(result)
        revision = result.get("snapshot_revision")
        if isinstance(revision, str):
            self._observed.add(revision)
        return result

    def abort(self, session) -> dict:
        result = self.publisher.abort(session)
        self._record(result)
        return result

    def publish_complete(self, tag: str = "contract") -> str:
        session = self.begin(tag)
        self.publish_complete_batches(session)
        return self.finalize(session)["snapshot_revision"]

    def finish(self, session) -> str:
        self.publish_complete_batches(session)
        return self.finalize(session)["snapshot_revision"]

    # -- observation ----------------------------------------------------

    def status(self) -> dict:
        result = self.reader.status()
        revision = result.get("snapshot_revision")
        if isinstance(revision, str):
            self._observed.add(revision)
        self._record(result)
        return result

    def observed_revisions(self) -> set:
        self.status()
        return {revision for revision in self._observed if revision}

    def observable_failure_codes(self) -> set:
        """Return only the codes real injected failures produced on this route."""
        return set(self._failures)

    def _record(self, result) -> None:
        if isinstance(result, dict):
            code = result.get("error")
            if isinstance(code, str):
                self._failures.add(code)

    # -- ownership and contention ---------------------------------------

    def as_owner(self, owner: str):
        if self.owner_factory is None:
            raise NotImplementedError("route has no alternate owner")
        return _OwnedRoute(self, self.owner_factory(owner))

    def replacement_process(self):
        if self.replacement_factory is None:
            raise NotImplementedError("route has no replacement process")
        return _OwnedRoute(self, self.replacement_factory())

    @contextmanager
    def hold_finalize_lock(self) -> Iterator[None]:
        if self.finalize_lock is None:
            yield
            return
        with self.finalize_lock():
            yield

    def reference(self, session_id: str):
        """Build the route-native handle for one opaque session identifier."""
        if self.session_ref is not None:
            return self.session_ref(session_id)
        from iwiki_mcp.codegraph.publication import PublicationSession

        return PublicationSession(
            session_id=session_id,
            lease_expires_at="",
            base_snapshot_revision=None,
            base_markdown_token=0,
        )

    def persisted_rows(self) -> dict:
        if self.raw_rows is None:
            return {}
        return self.raw_rows()


class _OwnedRoute:
    """Replay publication calls under a different publisher identity."""

    def __init__(self, harness: PublicationContractHarness, publisher) -> None:
        self._harness = harness
        self._publisher = publisher

    def __repr__(self) -> str:
        return "<publication contract replacement owner>"

    def publish_one(self, session) -> dict:
        result = self._publisher.publish_batch(session, self._harness.batches()[0])
        self._harness._record(result)
        return result

    def finalize(self, session) -> dict:
        result = self._publisher.finalize(session)
        self._harness._record(result)
        return result

    def abort(self, session) -> dict:
        result = self._publisher.abort(session)
        self._harness._record(result)
        return result


def generate_python_project(root: Path, count: int) -> Path:
    """Write one deterministic Python corpus of exactly `count` modules."""
    if count < 1:
        raise ValueError("count must be positive")
    package = Path(root) / "pkg"
    package.mkdir(parents=True, exist_ok=True)
    package.joinpath("__init__.py").write_text("", encoding="utf-8")
    for index in range(count):
        digest = hashlib.sha256(f"module-{index}".encode("utf-8")).hexdigest()
        neighbor = (index + 1) % count
        package.joinpath(f"module_{index}.py").write_text(
            "\n".join(
                (
                    f'"""Generated module {index} ({digest[:12]})."""',
                    "" if index == neighbor else (
                        f"from pkg.module_{neighbor} import Worker"
                        f" as Neighbor{neighbor}"
                    ),
                    "",
                    f"class Worker{index}:",
                    "    def run(self, value: int) -> int:",
                    f"        return value + {index}",
                    "",
                    "",
                    "Worker = Worker" + str(index),
                    "",
                )
            ),
            encoding="utf-8",
        )
    return package


_PROJECT_CONFIG = """base = {base}
read = ["project"]
write = ["project"]
primary = "project"

[code_graph]
enabled = true
languages = ["python"]
auto_rebuild = "off"
max_rebuild_seconds = 10
max_file_bytes = 1000000
max_total_files = 100
include_tests = true
"""


def _git(directory: Path, *arguments: str) -> None:
    import subprocess

    subprocess.run(
        ("git", *arguments),
        cwd=str(directory),
        check=True,
        capture_output=True,
        text=True,
    )


def seed_local_project(tmp_path: Path, *, modules: int = 2):
    """Create one git wiki base and one indexable project without fixtures."""
    from iwiki_mcp.base import Binding

    base = Path(tmp_path) / "base"
    project = Path(tmp_path) / "project"
    base.mkdir()
    project.mkdir()
    for directory in (base, project):
        _git(directory, "init", "-q")
        _git(directory, "config", "user.email", "test@example.com")
        _git(directory, "config", "user.name", "Test User")
    domain = base / "project"
    domain.mkdir()
    domain.joinpath("overview.md").write_text(
        "---\ntype: concept\ncode:\n  symbols:\n"
        "    - qualified_name: pkg.module_0.Worker0.run\n---\n# Project\n",
        encoding="utf-8",
    )
    domain.joinpath("index.jsonl").write_text("", encoding="utf-8")
    domain.joinpath("log.jsonl").write_text("", encoding="utf-8")
    _git(base, "add", "-A")
    _git(base, "commit", "-q", "-m", "seed wiki")

    generate_python_project(project / "src", modules)
    project.joinpath(".iwiki.toml").write_text(
        _PROJECT_CONFIG.format(base=json.dumps(str(base))), encoding="utf-8"
    )
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "seed project")
    return Binding(
        base=str(base),
        read=("project",),
        write=("project",),
        primary="project",
        project_dir=str(project),
    )


def local_runtime(binding):
    """Build one production code-graph runtime over a seeded local binding."""
    from iwiki_mcp.codegraph.indexer import AdapterFactory
    from iwiki_mcp.codegraph.languages.python import PythonAdapter
    from iwiki_mcp.codegraph.linking import WikiSelectorResolver
    from iwiki_mcp.codegraph.runtime import CodeGraphRuntime

    def create_python_adapter(source_paths):
        return PythonAdapter(
            binding.primary, source_paths, parser_version="contract-parser"
        )

    runtime = CodeGraphRuntime(
        binding,
        adapter_factories={
            "python": AdapterFactory(
                create=create_python_adapter,
                extensions=(".py",),
                parser_version="contract-parser",
                grammar_version="contract-grammar",
                adapter_version="contract-adapter",
            )
        },
    )
    runtime._indexer.wiki_selector_resolver = WikiSelectorResolver(binding.base)
    return runtime


def sqlite_route(tmp_path: Path) -> PublicationContractHarness:
    """Build the local SQLite publication route over a temporary base."""
    from iwiki_mcp.codegraph.linking import WikiSelectorResolver
    from iwiki_mcp.codegraph.sqlite_adapter import (
        SqliteCodeGraphReader,
        SqliteSnapshotPublisher,
    )

    binding = seed_local_project(tmp_path)
    runtime = local_runtime(binding)
    indexer = runtime._indexer
    built = indexer.build_rows()

    def publisher_for():
        return SqliteSnapshotPublisher(
            store=indexer.store,
            domain=binding.primary,
            private_root=built.private_root,
            selector_resolver=WikiSelectorResolver(binding.base),
            lock_path=indexer.paths.lock,
            config=replace(
                indexer.config,
                max_batch_rows=1,
                max_batch_bytes=65_536,
                publication_session_ttl_seconds=60,
                staging_retention_seconds=120,
                staging_cleanup_limit=1,
            ),
            diagnostics=built.diagnostics,
        )

    reader = SqliteCodeGraphReader(
        store=indexer.store,
        domain=binding.primary,
        private_root=built.private_root,
        lock_path=indexer.paths.lock,
        max_file_bytes=indexer.config.max_file_bytes,
        selector_resolver=WikiSelectorResolver(binding.base),
    )
    harness = PublicationContractHarness(
        route="sqlite",
        header=built.header,
        rows=built.tables,
        publisher=publisher_for(),
        reader=reader,
        supports_commit_uncertain=True,
        replacement_factory=publisher_for,
        raw_rows=lambda: _sqlite_rows(indexer.paths.database),
    )
    harness.binding = binding
    harness.runtime = runtime
    harness.store = indexer.store
    return harness


def _sqlite_rows(database) -> dict:
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(database)) as connection:
        return {
            table: [
                dict(zip([column[0] for column in cursor.description], row))
                for row in cursor
            ]
            for table, cursor in (
                (name, connection.execute(f"SELECT * FROM {name}"))
                for name in ("files", "symbols", "relations")
            )
        }


def fail_next_directory_sync(harness) -> None:
    """Make the next SQLite canonical directory sync fail exactly once."""
    publisher = harness.publisher
    original = publisher._sync_canonical_directory
    state = {"pending": True}

    def failing():
        if state["pending"]:
            state["pending"] = False
            raise OSError("directory sync unavailable")
        original()

    publisher._sync_canonical_directory = failing
    harness._restore_sync = lambda: setattr(
        publisher, "_sync_canonical_directory", original
    )


def restore_directory_sync(harness) -> None:
    """Restore the real canonical directory sync after an injected failure."""
    restore = getattr(harness, "_restore_sync", None)
    if restore is not None:
        restore()
        harness._restore_sync = None
