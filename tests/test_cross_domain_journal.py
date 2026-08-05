from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

import pytest

from iwiki_mcp import cross_domain


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _base(tmp_path: Path) -> Path:
    base = tmp_path / "wiki"
    (base / "alpha").mkdir(parents=True)
    (base / "alpha" / "page.md").write_text("before\n", encoding="utf-8")
    _git(base, "init", "-q")
    _git(base, "config", "user.email", "test@example.com")
    _git(base, "config", "user.name", "Test User")
    _git(base, "add", "-A")
    _git(base, "commit", "-q", "-m", "seed")
    return base


def test_journal_snapshots_files_and_transitions_atomically(tmp_path, monkeypatch):
    base = _base(tmp_path)
    fsync_calls = []
    replace_calls = []
    original_fsync = cross_domain.os.fsync
    original_replace = cross_domain.os.replace

    def record_fsync(fd):
        fsync_calls.append(fd)
        return original_fsync(fd)

    def record_replace(source, target):
        replace_calls.append((Path(source).name, Path(target).name))
        return original_replace(source, target)

    monkeypatch.setattr(cross_domain.os, "fsync", record_fsync)
    monkeypatch.setattr(cross_domain.os, "replace", record_replace)

    manifest = cross_domain.create_transaction(
        str(base),
        base_head=_git(base, "rev-parse", "HEAD"),
        affected_domains=("beta", "alpha", "alpha"),
        files=("alpha/page.md", "alpha/new.md"),
    )
    tx_dir = base / ".iwiki" / "transactions" / manifest.transaction_id

    assert re.fullmatch(r"[0-9a-f]{32}", manifest.transaction_id)
    assert manifest.state == "prepared"
    assert manifest.affected_domains == ("alpha", "beta")
    assert [item.path for item in manifest.files] == [
        "alpha/new.md",
        "alpha/page.md",
    ]
    assert (tx_dir / "manifest.json").is_file()
    assert list((tx_dir / "snapshots").glob("*.bin"))[0].read_bytes() == b"before\n"

    applied = cross_domain.transition_transaction(str(base), manifest, "applied")
    committed = cross_domain.transition_transaction(
        str(base), applied, "committed", commit_head="abc123"
    )
    assert committed.state == "committed"
    assert committed.commit_head == "abc123"
    assert fsync_calls
    assert any(target == "manifest.json" for _source, target in replace_calls)
    serialized = json.loads((tx_dir / "manifest.json").read_text())
    assert "remote" not in serialized

    cross_domain.finalize_transaction(str(base), committed)

    assert not tx_dir.exists()


def test_recovery_restores_applied_files_and_removes_created_paths(tmp_path):
    base = _base(tmp_path)
    original = (base / "alpha" / "page.md").read_bytes()
    manifest = cross_domain.create_transaction(
        str(base),
        base_head=_git(base, "rev-parse", "HEAD"),
        affected_domains=("alpha",),
        files=("alpha/page.md", "alpha/new.md"),
    )
    (base / "alpha" / "page.md").write_text("changed\n")
    (base / "alpha" / "new.md").write_text("created\n")
    cross_domain.transition_transaction(str(base), manifest, "applied")

    cross_domain.recover_pending_transactions(
        str(base), finalize_committed=lambda _manifest: True
    )
    cross_domain.recover_pending_transactions(
        str(base), finalize_committed=lambda _manifest: True
    )

    assert (base / "alpha" / "page.md").read_bytes() == original
    assert not (base / "alpha" / "new.md").exists()
    assert not (base / ".iwiki" / "transactions").exists()


def test_recovery_recognizes_commit_trailer_before_committed_marker(tmp_path):
    base = _base(tmp_path)
    manifest = cross_domain.create_transaction(
        str(base),
        base_head=_git(base, "rev-parse", "HEAD"),
        affected_domains=("alpha",),
        files=("alpha/page.md",),
    )
    (base / "alpha" / "page.md").write_text("committed\n")
    _git(base, "add", "alpha/page.md")
    _git(
        base,
        "commit",
        "-q",
        "-m",
        f"rewrite\n\nIwiki-Transaction: {manifest.transaction_id}",
    )
    seen = []

    cross_domain.recover_pending_transactions(
        str(base), finalize_committed=lambda item: seen.append(item) or True
    )

    assert (base / "alpha" / "page.md").read_text() == "committed\n"
    assert [item.transaction_id for item in seen] == [manifest.transaction_id]


def test_committed_recovery_retains_unsafe_journal_then_can_retry(tmp_path):
    base = _base(tmp_path)
    manifest = cross_domain.create_transaction(
        str(base),
        base_head=_git(base, "rev-parse", "HEAD"),
        affected_domains=("alpha",),
        files=("alpha/page.md",),
    )
    applied = cross_domain.transition_transaction(str(base), manifest, "applied")
    committed = cross_domain.transition_transaction(
        str(base), applied, "committed", commit_head=_git(base, "rev-parse", "HEAD")
    )

    with pytest.raises(cross_domain.CrossDomainError) as error:
        cross_domain.recover_pending_transactions(
            str(base), finalize_committed=lambda _manifest: False
        )
    assert error.value.code == "manual_recovery_required"
    tx_dir = base / ".iwiki" / "transactions" / committed.transaction_id
    assert tx_dir.is_dir()

    cross_domain.recover_pending_transactions(
        str(base), finalize_committed=lambda _manifest: True
    )
    assert not tx_dir.exists()


def test_recovery_blocks_unexpected_head_and_rejects_unsafe_paths(tmp_path):
    base = _base(tmp_path)
    manifest = cross_domain.create_transaction(
        str(base),
        base_head=_git(base, "rev-parse", "HEAD"),
        affected_domains=("alpha",),
        files=("alpha/page.md",),
    )
    (base / "other.md").write_text("other\n")
    _git(base, "add", "other.md")
    _git(base, "commit", "-q", "-m", "unrelated")

    with pytest.raises(cross_domain.CrossDomainError) as error:
        cross_domain.recover_pending_transactions(
            str(base), finalize_committed=lambda _manifest: True
        )
    assert error.value.code == "manual_recovery_required"
    assert (
        base / ".iwiki" / "transactions" / manifest.transaction_id
    ).is_dir()

    with pytest.raises(cross_domain.CrossDomainError):
        cross_domain.create_transaction(
            str(base),
            base_head=None,
            affected_domains=("alpha",),
            files=("../secret.md",),
        )


def test_recovery_blocks_head_changed_after_committed_marker(tmp_path):
    base = _base(tmp_path)
    manifest = cross_domain.create_transaction(
        str(base),
        base_head=_git(base, "rev-parse", "HEAD"),
        affected_domains=("alpha",),
        files=("alpha/page.md",),
    )
    applied = cross_domain.transition_transaction(str(base), manifest, "applied")
    cross_domain.transition_transaction(
        str(base), applied, "committed", commit_head=_git(base, "rev-parse", "HEAD")
    )
    (base / "other.md").write_text("other\n")
    _git(base, "add", "other.md")
    _git(base, "commit", "-q", "-m", "unrelated")

    with pytest.raises(cross_domain.CrossDomainError) as error:
        cross_domain.recover_pending_transactions(
            str(base), finalize_committed=lambda _manifest: True
        )

    assert error.value.code == "manual_recovery_required"


def test_journal_rejects_symlinked_iwiki_root(tmp_path):
    base = _base(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (base / ".iwiki").symlink_to(outside, target_is_directory=True)

    with pytest.raises(cross_domain.CrossDomainError) as error:
        cross_domain.create_transaction(
            str(base),
            base_head=_git(base, "rev-parse", "HEAD"),
            affected_domains=("alpha",),
            files=("alpha/page.md",),
        )

    assert error.value.code == "invalid_path"
    assert list(outside.iterdir()) == []
