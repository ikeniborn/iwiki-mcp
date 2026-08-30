from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import subprocess

import pytest

from iwiki_mcp import cross_domain, indexer, sync
from iwiki_mcp.base import Binding


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
        (root / "index.jsonl").write_text("old index\n")
        (root / "log.jsonl").write_text("old log\n")
    _git(base, "init", "-q")
    _git(base, "config", "user.email", "test@example.com")
    _git(base, "config", "user.name", "Test User")
    _git(base, "add", "-A")
    _git(base, "commit", "-q", "-m", "seed")
    binding = Binding(
        str(base), ("alpha", "beta"), ("alpha", "beta"), str(tmp_path), "alpha"
    )
    return base, binding


def _plan(base: Path):
    edits = []
    for domain, file, content in (
        ("beta", "b.md", b"# beta changed\n"),
        ("alpha", "a.md", b"# alpha changed\n"),
    ):
        before = (base / domain / file).read_bytes()
        edits.append(
            cross_domain.PlannedEdit(
                domain, file, sha256(before).hexdigest(), content
            )
        )
    return cross_domain.MutationPlan(
        operation="test",
        transaction_id="a" * 32,
        base_head=_git(base, "rev-parse", "HEAD"),
        edits=tuple(edits),
        affected_domains=("beta", "alpha"),
        rewritten_pages=("beta/b.md", "alpha/a.md"),
        rewritten_links=2,
    )


def test_execute_plan_commits_exact_paths_and_returns_evidence(
    tmp_path, monkeypatch
):
    base, binding = _base(tmp_path)
    plan = _plan(base)

    def index_domain(_cfg, base_arg, domain):
        root = Path(base_arg) / domain
        (root / "index.jsonl").write_text(f"new index {domain}\n")
        (root / "log.jsonl").write_text(f"new log {domain}\n")
        return {"indexed_chunks": 1, "reused": 1, "embedded": 0, "bytes": 1, "over_cap": False}

    monkeypatch.setattr(indexer, "index_domain", index_domain)
    monkeypatch.setattr(cross_domain.Config, "load", lambda: object())
    monkeypatch.setattr(
        sync,
        "sync",
        lambda _base: {
            "pulled": False,
            "pushed": False,
            "warning": "no remote",
            "sync_attempts": 0,
            "push_attempts": 0,
        },
    )

    result = cross_domain.execute_plan(str(base), binding, plan)

    committed = _git(base, "show", "--name-only", "--pretty=format:", "HEAD").split()
    message = _git(base, "log", "-1", "--format=%B")
    assert committed == [
        "alpha/a.md",
        "alpha/index.jsonl",
        "alpha/log.jsonl",
        "beta/b.md",
        "beta/index.jsonl",
        "beta/log.jsonl",
    ]
    assert "Iwiki-Transaction: " + plan.transaction_id in message
    assert result["rewritten_pages"] == ["alpha/a.md", "beta/b.md"]
    assert result["affected_domains"] == ["alpha", "beta"]
    assert result["rewritten_links"] == 2
    assert result["committed"] is True
    assert result["pushed"] is False
    assert not (base / ".iwiki" / "transactions").exists()


def test_execute_plan_rolls_back_bytes_head_and_index_on_index_failure(
    tmp_path, monkeypatch
):
    base, binding = _base(tmp_path)
    plan = _plan(base)
    before = {
        path.relative_to(base).as_posix(): path.read_bytes()
        for path in base.rglob("*")
        if path.is_file() and ".git" not in path.parts and ".iwiki" not in path.parts
    }

    def fail_second(_cfg, _base, domain):
        if domain == "beta":
            raise RuntimeError("index failed")
        (base / domain / "index.jsonl").write_text("changed index\n")
        return {}

    monkeypatch.setattr(indexer, "index_domain", fail_second)
    monkeypatch.setattr(cross_domain.Config, "load", lambda: object())

    with pytest.raises(cross_domain.CrossDomainError) as error:
        cross_domain.execute_plan(str(base), binding, plan)

    after = {
        path.relative_to(base).as_posix(): path.read_bytes()
        for path in base.rglob("*")
        if path.is_file() and ".git" not in path.parts and ".iwiki" not in path.parts
    }
    assert error.value.code == "mutation_failed"
    assert before == after
    assert _git(base, "rev-parse", "HEAD") == plan.base_head
    assert _git(base, "diff", "--cached", "--name-only") == ""
    assert not (base / ".iwiki" / "transactions").exists()


def test_execute_plan_rejects_read_only_domain_and_changed_preimage(tmp_path):
    base, binding = _base(tmp_path)
    plan = _plan(base)
    read_only = Binding(
        binding.base, binding.read, ("alpha",), binding.project_dir, "alpha"
    )

    with pytest.raises(cross_domain.CrossDomainError) as blocked:
        cross_domain.execute_plan(str(base), read_only, plan)
    assert blocked.value.code == "write_scope_blocked"

    (base / "alpha" / "a.md").write_text("external change\n")
    with pytest.raises(cross_domain.CrossDomainError) as changed:
        cross_domain.execute_plan(str(base), binding, plan)
    assert changed.value.code == "source_changed"


def test_execute_plan_releases_lock_before_fail_soft_publication(
    tmp_path, monkeypatch
):
    base, binding = _base(tmp_path)
    plan = _plan(base)
    calls = []

    def index_domain(_cfg, base_arg, domain):
        root = Path(base_arg) / domain
        (root / "index.jsonl").write_text(f"new index {domain}\n")
        return {
            "indexed_chunks": 1,
            "reused": 1,
            "embedded": 0,
            "bytes": 1,
            "over_cap": False,
        }

    real_commit_locked = sync.commit_locked

    def commit_locked(*args, **kwargs):
        calls.append("commit")
        return real_commit_locked(*args, **kwargs)

    def publish_committed(_base, commit, after_commit=None):
        calls.append("publish")
        assert commit["committed"] is True
        assert not (base / ".iwiki" / "transactions").exists()
        if after_commit is not None:
            try:
                after_commit()
            except RuntimeError:
                pass
        return {
            "committed": True,
            "pushed": False,
            "sync_attempts": 1,
            "push_attempts": 1,
            "warning": "sanitized post-commit warning",
        }

    monkeypatch.setattr(indexer, "index_domain", index_domain)
    monkeypatch.setattr(cross_domain.Config, "load", lambda: object())
    monkeypatch.setattr(sync, "commit_locked", commit_locked)
    monkeypatch.setattr(sync, "publish_committed", publish_committed)
    monkeypatch.setattr(
        indexer,
        "finalize_graph_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("private post-commit failure")
        ),
    )

    result = cross_domain.execute_plan(str(base), binding, plan)

    assert calls == ["commit", "publish"]
    assert result["committed"] is True
    assert result["warning"] == "sanitized post-commit warning"
    assert _git(base, "rev-parse", "HEAD") != plan.base_head
    assert not (base / ".iwiki" / "transactions").exists()


def test_execute_plan_rejects_unrelated_same_domain_dirty_file(tmp_path, monkeypatch):
    base, binding = _base(tmp_path)
    plan = _plan(base)
    unrelated = base / "alpha" / "unrelated.md"
    unrelated.write_text("uncommitted\n")
    head_before = _git(base, "rev-parse", "HEAD")
    monkeypatch.setattr(cross_domain.Config, "load", lambda: object())
    monkeypatch.setattr(
        indexer,
        "index_domain",
        lambda *_args, **_kwargs: {
            "indexed_chunks": 0,
            "reused": 0,
            "embedded": 0,
            "bytes": 0,
            "over_cap": False,
        },
    )
    monkeypatch.setattr(
        sync,
        "publish_committed",
        lambda *_args, **_kwargs: {
            "committed": True,
            "pushed": False,
            "sync_attempts": 0,
            "push_attempts": 0,
        },
    )

    with pytest.raises(cross_domain.CrossDomainError) as error:
        cross_domain.execute_plan(str(base), binding, plan)

    assert error.value.code == "mutation_failed"
    assert _git(base, "rev-parse", "HEAD") == head_before
    assert unrelated.read_text() == "uncommitted\n"
    assert _git(base, "diff", "--cached", "--name-only") == ""
