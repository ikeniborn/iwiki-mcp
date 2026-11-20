import os
from pathlib import Path

import pytest

from iwiki_mcp.codegraph.parse_checkpoint import ParseCheckpoint, ParseCheckpointError


def test_scope_prune_and_persistence(tmp_path):
    original = tmp_path / "repo"
    with ParseCheckpoint.open(tmp_path, original, "books") as cache:
        cache.save("a.py", {"hash": "a"}, {"data": 1})
        cache.save("b.py", {"hash": "b"}, {"data": 2})
    for checkout, domain in [(original, "other"), (tmp_path / "other", "books")]:
        with ParseCheckpoint.open(tmp_path, checkout, domain) as cache:
            assert cache.load("a.py", {"hash": "a"}) is None
    pin = tmp_path / ".iwiki" / "resume-pin"
    pin.write_bytes(b"pin")
    with ParseCheckpoint.open(tmp_path, original, "books") as cache:
        folder = Path(f"/proc/self/fd/{cache.directory}")
        (folder / "unknown").write_bytes(b"private")
        cache.prune(["a.py"])
        assert cache.load("b.py", {"hash": "b"}) is None
        assert cache.load("a.py", {"hash": "other"}) is None
        assert cache.load("a.py", {"hash": "a"}) == {"data": 1}
        cache.clear()
        assert (folder / "unknown").read_bytes() == b"private"
    assert pin.read_bytes() == b"pin"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public", "fifo"])
def test_unsafe_entry_is_rejected(tmp_path, kind):
    neighbor = tmp_path / "neighbor"
    neighbor.write_bytes(b"neighbor")
    with ParseCheckpoint.open(tmp_path, tmp_path, "books") as cache:
        cache.save("a", {}, {})
        folder = Path(f"/proc/self/fd/{cache.directory}")
        entry, = folder.iterdir()
        entry.unlink()
        if kind == "symlink":
            entry.symlink_to(neighbor)
        elif kind == "hardlink":
            os.link(neighbor, entry)
        elif kind == "fifo":
            os.mkfifo(entry)
        else:
            entry.write_bytes(b"unsafe")
            entry.chmod(0o644)
        for op in (lambda: cache.load("a", {}), lambda: cache.save("a", {}, {}), cache.clear):
            with pytest.raises(ParseCheckpointError):
                op()
        assert neighbor.read_bytes() == b"neighbor"


def test_complete_record_size_limit(tmp_path):
    from iwiki_mcp.codegraph.parse_checkpoint import MAX_RECORD_BYTES

    with ParseCheckpoint.open(tmp_path, tmp_path, "books") as cache:
        assert cache.save("a", {}, "")
        entry, = Path(f"/proc/self/fd/{cache.directory}").iterdir()
        padding = MAX_RECORD_BYTES - entry.stat().st_size
        for difference in (-1, 0, 1):
            value = "a" * (padding + difference)
            assert cache.save("a", {}, value) == (difference <= 0)
            if difference <= 0:
                assert entry.stat().st_size == MAX_RECORD_BYTES + difference
                assert cache.load("a", {}) == value
        assert entry.stat().st_size == MAX_RECORD_BYTES
        with entry.open("ab") as stream:
            stream.write(b"x")
        assert cache.load("a", {}) is None


def test_damage_or_wrong_format_is_a_miss(tmp_path):
    import json
    from iwiki_mcp.codegraph.canonical import canonical_sha256

    with ParseCheckpoint.open(tmp_path, tmp_path, "books") as cache:
        cache.save("a", {}, {"test": 1})
        entry, = Path(f"/proc/self/fd/{cache.directory}").iterdir()
        document = json.loads(entry.read_bytes())
        document["record"]["payload"] = {"test": 2}
        entry.write_text(json.dumps(document))
        assert cache.load("a", {}) is None
        document["record"]["version"] = 2
        document["digest"] = canonical_sha256(document["record"], prefix=False)
        entry.write_text(json.dumps(document))
        assert cache.load("a", {}) is None
        entry.write_bytes(b'{"incomplete')
        assert cache.load("a", {}) is None


def test_directory_symlink_and_permissions(tmp_path):
    with ParseCheckpoint.open(tmp_path, tmp_path, "books"):
        pass
    folder, = (tmp_path / ".iwiki").iterdir()
    folder.chmod(0o777)
    with pytest.raises(ParseCheckpointError), ParseCheckpoint.open(tmp_path, tmp_path, "books"):
        pass
    folder.rmdir()
    neighbor = tmp_path / "neighbor"
    neighbor.mkdir()
    (neighbor / "asset").write_bytes(b"unchanged")
    folder.symlink_to(neighbor, target_is_directory=True)
    with pytest.raises(ParseCheckpointError), ParseCheckpoint.open(tmp_path, tmp_path, "books"):
        pass
    assert (neighbor / "asset").read_bytes() == b"unchanged"


def test_failed_write_preserves_committed_result(tmp_path, monkeypatch):
    def fail(_fd):
        raise OSError("private path")

    with ParseCheckpoint.open(tmp_path, tmp_path, "books") as cache:
        cache.save("a", {}, 1)
        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", fail)
            with pytest.raises(ParseCheckpointError, match="^parse_checkpoint_storage_failed$"):
                cache.save("a", {}, 2)
    with ParseCheckpoint.open(tmp_path, tmp_path, "books") as cache:
        cache.prune(["a"])
        assert cache.load("a", {}) == 1
        entry, = Path(f"/proc/self/fd/{cache.directory}").iterdir()
        assert entry.is_file()


@pytest.mark.parametrize("point", ["before_replace", "after_replace", "after_directory_sync"])
def test_kill_during_replace(tmp_path, point):
    import signal
    import subprocess
    import sys

    with ParseCheckpoint.open(tmp_path, tmp_path, "books") as cache:
        cache.save("a", {}, "previous")
    child = """
import os, signal, sys
from iwiki_mcp.codegraph.parse_checkpoint import ParseCheckpoint
root, point = sys.argv[1:]
with ParseCheckpoint.open(root, root, "books") as cache:
    replace, fsync = os.replace, os.fsync
    def stop():
        os.kill(os.getpid(), signal.SIGKILL)
    def replace_and_stop(*args, **kwargs):
        if point == "before_replace":
            stop()
        replace(*args, **kwargs)
        if point == "after_replace":
            stop()
    def fsync_and_stop(fd):
        fsync(fd)
        if fd == cache.directory and point == "after_directory_sync":
            stop()
    os.replace, os.fsync = replace_and_stop, fsync_and_stop
    cache.save("a", {}, "next")
"""
    proc = subprocess.run([sys.executable, "-c", child, str(tmp_path), point], timeout=30)
    assert proc.returncode == -signal.SIGKILL
    with ParseCheckpoint.open(tmp_path, tmp_path, "books") as cache:
        assert cache.load("a", {}) == ("previous" if point == "before_replace" else "next")
        cache.prune(["a"])
        assert len(os.listdir(cache.directory)) == 1


def test_foreign_owned_file_if_permitted(tmp_path):
    with ParseCheckpoint.open(tmp_path, tmp_path, "books") as cache:
        cache.save("a", {}, {})
        entry, = Path(f"/proc/self/fd/{cache.directory}").iterdir()
        try:
            os.chown(entry, (os.geteuid() + 1) % 65535, -1)
        except PermissionError:
            pytest.skip("changing file ownership requires privilege")
        with pytest.raises(ParseCheckpointError):
            cache.load("a", {})
