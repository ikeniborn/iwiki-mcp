from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import subprocess
from contextlib import contextmanager

import iwiki_mcp.graph as graph
from iwiki_mcp import indexer
from iwiki_mcp.engine.graph_store import GraphStore


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _base(tmp_path: Path):
    base = tmp_path / "wiki"
    for domain, file in (("alpha", "a.md"), ("beta", "b.md")):
        root = base / domain
        root.mkdir(parents=True)
        (root / file).write_text(f"# {domain}\n", encoding="utf-8")
    _git(base, "init", "-q")
    _git(base, "config", "user.email", "test@example.com")
    _git(base, "config", "user.name", "Test User")
    _git(base, "add", "-A")
    _git(base, "commit", "-q", "-m", "seed")
    assert graph.scoped_graph(str(base), ("alpha", "beta")) is not None
    mutations = tuple(
        indexer.prepare_graph_mutation(str(base), domain)
        for domain in ("alpha", "beta")
    )
    return base, mutations


def _metadata(base: Path):
    with GraphStore(base).read_snapshot() as connection:
        return {
            row[0]: (row[1], row[2])
            for row in connection.execute(
                "SELECT domain, markdown_fingerprint, state FROM domains "
                "ORDER BY domain"
            )
        }


def _content_hashes(base: Path):
    with GraphStore(base).read_snapshot() as connection:
        return {
            (row[0], row[1]): row[2]
            for row in connection.execute(
                "SELECT domain, file, content_hash FROM pages ORDER BY domain, file"
            )
        }


def test_finalize_graph_batch_refreshes_all_domains_atomically(tmp_path):
    base, mutations = _base(tmp_path)
    alpha = "# alpha changed\n"
    beta = "# beta changed\n"
    (base / "alpha" / "a.md").write_text(alpha, encoding="utf-8")
    (base / "beta" / "b.md").write_text(beta, encoding="utf-8")
    _git(base, "add", "-A")
    _git(base, "commit", "-q", "-m", "canonical change")

    warning = indexer.finalize_graph_batch(
        mutations,
        {"alpha": ("a.md",), "beta": ("b.md",)},
        {},
    )

    assert warning is None
    assert _content_hashes(base) == {
        ("alpha", "a.md"): sha256(alpha.encode()).hexdigest(),
        ("beta", "b.md"): sha256(beta.encode()).hexdigest(),
    }
    assert all(state == "ready" for _fingerprint, state in _metadata(base).values())


def test_finalize_graph_batch_rolls_back_rows_and_dirties_all_on_second_failure(
    tmp_path,
):
    base, mutations = _base(tmp_path)
    before = _content_hashes(base)
    with GraphStore(base).transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_beta BEFORE INSERT ON pages "
            "WHEN NEW.domain = 'beta' "
            "BEGIN SELECT RAISE(ABORT, 'beta failed'); END"
        )
    (base / "alpha" / "a.md").write_text("# changed alpha\n")
    (base / "beta" / "b.md").write_text("# changed beta\n")
    _git(base, "add", "-A")
    _git(base, "commit", "-q", "-m", "canonical change")

    warning = indexer.finalize_graph_batch(
        mutations,
        {"alpha": ("a.md",), "beta": ("b.md",)},
        {},
    )

    assert warning == indexer.GRAPH_FALLBACK_WARNING
    assert _content_hashes(base) == before
    assert {state for _fingerprint, state in _metadata(base).values()} == {"dirty"}


def test_failed_graph_batch_is_rejected_then_rebuilds_without_embeddings(
    tmp_path, monkeypatch
):
    base, mutations = _base(tmp_path)
    (base / "alpha" / "a.md").write_text("# changed alpha\n")
    (base / "beta" / "b.md").write_text("# changed beta\n")
    _git(base, "add", "-A")
    _git(base, "commit", "-q", "-m", "canonical change")
    original = graph.markdown_fingerprint
    calls = {"beta": 0}

    def race(base_arg: str, domain: str):
        fingerprint = original(base_arg, domain)
        if domain == "beta":
            calls["beta"] += 1
            if calls["beta"] > 1:
                return graph.MarkdownFingerprint("changed-during-finalize", None)
        return fingerprint

    monkeypatch.setattr(graph, "markdown_fingerprint", race)
    warning = indexer.finalize_graph_batch(
        mutations,
        {"alpha": ("a.md",), "beta": ("b.md",)},
        {},
    )
    monkeypatch.setattr(graph, "markdown_fingerprint", original)
    monkeypatch.setattr(
        indexer,
        "embed_texts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("graph rebuild embedded content")
        ),
    )

    assert warning == indexer.GRAPH_FALLBACK_WARNING
    assert graph.incoming_candidates(
        str(base), ("alpha", "beta"), "alpha/missing"
    ) is None
    assert graph.scoped_graph(str(base), ("alpha", "beta")) is not None


def test_graph_batch_fingerprint_mismatch_rejects_old_ready_rows_when_dirty_fails(
    tmp_path, monkeypatch
):
    base, mutations = _base(tmp_path)
    with GraphStore(base).transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_refresh BEFORE INSERT ON pages "
            "BEGIN SELECT RAISE(ABORT, 'refresh failed'); END"
        )
    (base / "alpha" / "a.md").write_text("# changed alpha\n")
    (base / "beta" / "b.md").write_text("# changed beta\n")
    _git(base, "add", "-A")
    _git(base, "commit", "-q", "-m", "canonical change")
    store = mutations[0].store
    original_transaction = store.transaction
    calls = 0

    @contextmanager
    def fail_dirty_transaction():
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("dirty write failed")
        with original_transaction() as connection:
            yield connection

    monkeypatch.setattr(store, "transaction", fail_dirty_transaction)

    warning = indexer.finalize_graph_batch(
        mutations,
        {"alpha": ("a.md",), "beta": ("b.md",)},
        {},
    )

    assert warning == indexer.GRAPH_FALLBACK_WARNING
    assert {state for _fingerprint, state in _metadata(base).values()} == {"ready"}
    assert graph.incoming_candidates(
        str(base), ("alpha", "beta"), "alpha/missing"
    ) is None
