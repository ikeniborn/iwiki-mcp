import os

import pytest
from filelock import Timeout

from iwiki_mcp.lock import base_lock


def test_base_lock_creates_meta_dir_and_locks(tmp_path):
    base = str(tmp_path)
    lock = base_lock(base)
    assert os.path.isdir(os.path.join(base, ".iwiki"))
    with lock:
        assert lock.is_locked


def test_base_lock_second_acquire_times_out_while_held(tmp_path):
    base = str(tmp_path)
    with base_lock(base, timeout=15.0):
        with pytest.raises(Timeout):
            base_lock(base, timeout=0.1).acquire()


def test_base_lock_acquired_after_holder_releases(tmp_path):
    base = str(tmp_path)
    with base_lock(base):
        pass  # held, then released on block exit
    second = base_lock(base, timeout=1.0)
    with second:  # a fresh waiter acquires once the base is free
        assert second.is_locked
