import os
import subprocess
import sys

import pytest

from iwiki_mcp.codegraph.publication import (
    PublicationSession,
    SnapshotHeader,
    graph_payload_revision,
    iter_snapshot_batches,
)
from iwiki_mcp.codegraph import checkpoint


def snapshot():
    rows = {
        "repositories": [{"repository_id": "docs", "private": "/project"}],
        "files": [{"file_id": str(i), "path": f"src/{i}.py"} for i in (4, 3, 2, 1)],
        "symbols": [{"symbol_id": "x"}],
        "relations": [],
    }
    header = SnapshotHeader(
        1,
        1,
        "docs",
        "source",
        "parser",
        "version",
        "unicode",
        ("python",),
        {k: len(v) for k, v in rows.items()},
        graph_payload_revision(rows),
    )
    return header, rows


def opened(base, endpoint="example"):
    return checkpoint.Checkpoint.open(base, base, "docs", endpoint)


def test_durable_pin_replays_canonical_bounds_and_completion(tmp_path):
    header, rows = snapshot()
    session = PublicationSession("id", "later", None, 0, 2, 72)
    golden = list(iter_snapshot_batches(rows, max_rows=2, max_bytes=72))
    with opened(tmp_path) as pin:
        pin.prepare(header, rows, credential="test")
        pin.save_session(session)
        assert [pin.batch(item) for item in pin.record["batches"]] == golden
    with opened(tmp_path) as pin:
        pin.verify()
        assert [pin.batch(item) for item in pin.record["batches"]] == golden
        pin.complete("sha256:" + "b" * 64)
    with opened(tmp_path) as pin:
        assert pin.record["revision"] == "sha256:" + "b" * 64
        assert pin.forget() is False
    with opened(tmp_path) as pin:
        assert pin.record is None


def test_pin_rejects_corruption_and_target_substitution(tmp_path):
    with opened(tmp_path) as pin:
        pin.prepare(*snapshot(), credential="test")
    private = next((tmp_path / ".iwiki").glob("resume-*"))
    assert private.stat().st_mode & 0o077 == 0
    assert all(p.stat().st_mode & 0o077 == 0 for p in private.iterdir())
    assert b'"/project"' not in (private / "rows").read_bytes()
    with pytest.raises(checkpoint.CodeGraphResumeError, match="resume_target_mismatch"):
        with opened(tmp_path, "other"):
            pass
    (private / "rows").write_bytes(b"{}\n")
    with pytest.raises(checkpoint.CodeGraphResumeError, match="resume_invalid_checkpoint"):
        with opened(tmp_path) as pin:
            pin.verify()
    (private / "rows").unlink()
    (private / "rows").symlink_to(tmp_path / "outside")
    with pytest.raises(checkpoint.CodeGraphResumeError, match="resume_invalid_checkpoint"):
        with opened(tmp_path) as pin:
            pin.verify()


@pytest.mark.parametrize("after", [False, True])
@pytest.mark.parametrize("rename_number", [1, 2, 3, 4])
def test_process_exit_at_each_durable_rename(tmp_path, rename_number, after):
    script = """
import os, sys
from pathlib import Path
from tests.codegraph.test_resume_checkpoint import opened, snapshot
from iwiki_mcp.codegraph.publication import PublicationSession
base, number, after = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
replace = os.replace
calls = 0
def interrupt(*args, **kwargs):
    global calls
    calls += 1
    if calls == number and not after:
        os._exit(77)
    result = replace(*args, **kwargs)
    if calls == number and after:
        os._exit(77)
    return result
with opened(base) as pin:
    os.replace = interrupt
    pin.prepare(*snapshot(), credential='test')
    pin.save_session(PublicationSession("id", "later", None, 0, 2, 72))
    pin.complete("sha256:" + "b" * 64)
"""
    proc = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), str(rename_number), str(int(after))]
    )
    assert proc.returncode == 77
    committed = rename_number - (not after)
    with opened(tmp_path) as pin:
        if committed < 2:
            assert pin.record is None
        else:
            pin.verify()
            assert pin.record["state"] == ("ready" if committed == 4 else "pinned")
            assert ("session" in pin.record) == (committed == 3)


def test_disk_error_cannot_publish_partial_pin(tmp_path, monkeypatch):
    with opened(tmp_path) as pin:

        def disk_full(_fd):
            raise OSError(28, "test disk full")

        with monkeypatch.context() as scoped:
            scoped.setattr(os, "fsync", disk_full)
            with pytest.raises(OSError):
                pin.prepare(*snapshot(), credential="test")
    with opened(tmp_path) as pin:
        assert pin.record is None


def test_same_checkout_is_busy_and_kernel_releases_lock_on_exit(tmp_path):
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; from tests.codegraph.test_resume_checkpoint "
            "import opened; pin=opened(Path(sys.argv[1])); pin.__enter__(); "
            "print('locked',flush=True); sys.stdin.readline()",
            str(tmp_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline() == "locked\n"
        with pytest.raises(checkpoint.CodeGraphResumeError, match="resume_busy"):
            with opened(tmp_path):
                pass
    finally:
        child.kill()
        child.wait()
    with opened(tmp_path) as pin:
        assert pin.record is None


def test_forget_corrupt_manifest_clears_only_selected_endpoint(tmp_path):
    with opened(tmp_path) as pin:
        pin.prepare(*snapshot(), credential="test")
    directory = next((tmp_path / ".iwiki").glob("resume-*"))
    manifest = next(directory.glob("manifest-*"))
    manifest.write_bytes(b"invalid")
    with pytest.raises(checkpoint.CodeGraphResumeError, match="resume_target_mismatch"):
        with checkpoint.Checkpoint.open(tmp_path, tmp_path, "docs", "elsewhere", forget=True):
            pass
    assert manifest.exists()
    with checkpoint.Checkpoint.open(tmp_path, tmp_path, "docs", "example", forget=True) as pin:
        assert pin.forget() is True
    assert not list(directory.iterdir())


def test_unrecognized_encoding_blocks_resume(tmp_path):
    with opened(tmp_path) as pin:
        pin.prepare(*snapshot(), credential="test")
    import json
    from iwiki_mcp.codegraph.canonical import canonical_sha256

    file = next((tmp_path / ".iwiki").glob("resume-*/manifest-*"))
    envelope = json.loads(file.read_bytes())
    envelope["record"]["codec"] = 99
    envelope["sha256"] = canonical_sha256(envelope["record"], prefix=True)
    file.write_text(json.dumps(envelope))
    with pytest.raises(checkpoint.CodeGraphResumeError, match="resume_invalid_checkpoint"):
        with opened(tmp_path):
            pass


def test_client_clock_two_seconds_ahead_stays_within_server_deadline(tmp_path, monkeypatch):
    server_now = 1800000000
    monkeypatch.setattr(checkpoint.time, "time", lambda: server_now + 2)
    with opened(tmp_path) as pin:
        pin.prepare(*snapshot(), credential="test")
        assert server_now < pin.record["deadline"] < server_now + 86400
        assert pin.record["deadline"] == server_now + 2 + (23 * 60 + 55) * 60
