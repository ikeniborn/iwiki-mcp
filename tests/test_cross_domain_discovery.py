from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import sqlite3
import subprocess

import pytest

import iwiki_mcp.graph as graph
from iwiki_mcp.engine import graph_store


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    base = tmp_path / "base"
    base.mkdir()
    _git(base, "init", "-q")
    _git(base, "config", "user.email", "test@example.com")
    _git(base, "config", "user.name", "Test User")
    (base / "alpha").mkdir()
    (base / "target").mkdir()
    (base / "alpha" / "source.md").write_text(
        "[Target](iwiki://target/page#Section)\n"
        "`[Code](iwiki://target/page#section)`\n",
        encoding="utf-8",
    )
    (base / "target" / "page.md").write_text(
        "# Page\n## Section\nbody\n", encoding="utf-8"
    )
    _git(base, "add", "-A")
    _git(base, "commit", "-q", "-m", "seed")
    return base


def test_ready_incoming_candidates_uses_index_then_canonical_reparse(
    tmp_path, monkeypatch
):
    base = _repo(tmp_path)
    assert graph.scoped_graph(str(base), ("alpha", "target")) is not None

    def fail_walk(*_args, **_kwargs):
        raise AssertionError("ready discovery walked Markdown scope")

    monkeypatch.setattr(Path, "rglob", fail_walk)
    monkeypatch.setattr(Path, "read_text", fail_walk)
    monkeypatch.setattr(graph_store.GraphStore, "load_ready_domain", fail_walk)

    result = graph.incoming_candidates(
        str(base), ("target", "alpha"), "target/page", "Section"
    )

    assert result == (graph.IncomingCandidate("alpha", "source.md"),)


def test_ready_incoming_candidates_rejects_stale_sqlite_edge(tmp_path):
    base = _repo(tmp_path)
    assert graph.scoped_graph(str(base), ("alpha", "target")) is not None
    store = graph_store.GraphStore(base)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE edges SET target_page_id = 'target/stale', "
            "raw_target = 'iwiki://target/stale' "
            "WHERE source_page_id = 'alpha/source'"
        )

    result = graph.incoming_candidates(
        str(base), ("alpha", "target"), "target/stale"
    )

    assert result == ()


def test_ready_incoming_candidates_rejects_out_of_domain_sqlite_file(tmp_path):
    base = _repo(tmp_path)
    (base / "hidden").mkdir()
    hidden = base / "hidden" / "secret.md"
    hidden.write_text("[Target](iwiki://target/page#section)\n", encoding="utf-8")
    assert graph.scoped_graph(str(base), ("alpha", "target")) is not None
    with graph_store.GraphStore(base).transaction() as connection:
        connection.execute(
            "UPDATE pages SET file = '../hidden/secret.md', content_hash = ? "
            "WHERE page_id = 'alpha/source'",
            (sha256(hidden.read_bytes()).hexdigest(),),
        )

    assert graph.incoming_candidates(
        str(base), ("alpha", "target"), "target/page"
    ) is None


def test_markdown_snapshot_is_scope_limited_deterministic_and_complete(
    tmp_path, monkeypatch
):
    base = _repo(tmp_path)
    (base / "alpha" / "other.md").write_text("# Other\n", encoding="utf-8")
    (base / "hidden").mkdir()
    (base / "hidden" / "secret.md").write_text(
        "[Target](iwiki://target/page#section)\n", encoding="utf-8"
    )
    original_rglob = Path.rglob
    walked: list[str] = []

    def record_walk(path: Path, pattern: str):
        walked.append(path.name)
        return original_rglob(path, pattern)

    monkeypatch.setattr(Path, "rglob", record_walk)

    snapshot = graph.markdown_incoming_snapshot(
        str(base), ("target", "alpha"), "target/page", "section"
    )

    assert snapshot.candidates == (
        graph.IncomingCandidate("alpha", "source.md"),
    )
    assert [(domain, file) for domain, file, _hash in snapshot.expected_hashes] == [
        ("alpha", "other.md"),
        ("alpha", "source.md"),
        ("target", "page.md"),
    ]
    assert snapshot.expected_hashes[0][2] == sha256(b"# Other\n").hexdigest()
    assert walked == ["alpha", "target"]


def test_markdown_snapshot_rejects_post_scan_change(tmp_path, monkeypatch):
    base = _repo(tmp_path)
    source = base / "alpha" / "source.md"
    original_read = graph._read_scoped_markdown
    reads = 0

    def change_after_snapshot_read(base_arg: str, domain: str, file: str):
        nonlocal reads
        data = original_read(base_arg, domain, file)
        if domain == "alpha" and file == "source.md" and reads == 0:
            reads += 1
            source.write_text("# Changed\n", encoding="utf-8")
        return data

    monkeypatch.setattr(graph, "_read_scoped_markdown", change_after_snapshot_read)

    with pytest.raises(graph.MarkdownSnapshotChanged):
        graph.markdown_incoming_snapshot(
            str(base), ("alpha", "target"), "target/page"
        )


def test_incoming_candidates_returns_none_for_missing_or_dirty_graph(tmp_path):
    base = _repo(tmp_path)

    assert graph.incoming_candidates(
        str(base), ("alpha", "target"), "target/page"
    ) is None


@pytest.mark.parametrize("state", ["dirty", "rebuilding"])
def test_incoming_candidates_rejects_non_ready_domain_state(tmp_path, state):
    base = _repo(tmp_path)
    assert graph.scoped_graph(str(base), ("alpha", "target")) is not None
    store = graph_store.GraphStore(base)
    if state == "dirty":
        store.mark_domain_dirty("target")
    else:
        store._mark_domain_rebuilding("target")

    assert graph.incoming_candidates(
        str(base), ("alpha", "target"), "target/page"
    ) is None


def test_incoming_candidates_fails_closed_for_corrupt_or_busy_store(
    tmp_path, monkeypatch
):
    base = _repo(tmp_path)
    store = graph_store.GraphStore(base)
    store.path.parent.mkdir()
    store.path.write_bytes(b"not sqlite")

    assert graph.incoming_candidates(
        str(base), ("alpha", "target"), "target/page"
    ) is None

    store.path.unlink()
    assert graph.scoped_graph(str(base), ("alpha", "target")) is not None

    def busy(*_args, **_kwargs):
        error = sqlite3.OperationalError("database is locked")
        raise graph_store.GraphStoreError("graph read failed") from error

    monkeypatch.setattr(graph_store.GraphStore, "query_incoming_pages", busy)
    assert graph.incoming_candidates(
        str(base), ("alpha", "target"), "target/page"
    ) is None

    assert graph.scoped_graph(str(base), ("alpha", "target")) is not None
    graph_store.GraphStore(base).mark_domain_dirty("alpha")

    assert graph.incoming_candidates(
        str(base), ("alpha", "target"), "target/page"
    ) is None
