"""Importing the server must pull every package module into sys.modules.

A module left out of the startup closure is loaded lazily on first use. For a
long-lived stdio server that is a hazard: upgrading the installed package on
disk mid-session makes the lazy import load NEW source against STALE cached
modules, e.g. `ImportError: cannot import name 'parse_heading_anchors' from
'iwiki_mcp.engine.links'` when new lint.py met a pre-upgrade links module.
Eager imports pin one consistent code snapshot per process.

Runs in a subprocess so sys.modules is not polluted by other tests.
"""
import json
import subprocess
import sys

_PROBE = """
import json, pkgutil, sys
import iwiki_mcp.server  # noqa: F401  (startup, as the stdio process does)
import iwiki_mcp
missing = [
    m.name
    for m in pkgutil.walk_packages(iwiki_mcp.__path__, prefix="iwiki_mcp.")
    if m.name not in sys.modules
]
print(json.dumps(missing))
"""


def test_server_import_closure_covers_all_package_modules():
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True, text=True, check=True,
    )
    missing = json.loads(proc.stdout)
    assert missing == [], (
        f"modules outside the startup import closure: {missing}; "
        "lazy-loading them breaks a live server after an on-disk upgrade"
    )
