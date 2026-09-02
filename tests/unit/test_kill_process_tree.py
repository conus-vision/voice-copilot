"""Stopping the wrapped CLI must take its orphans with it.

Codex forks sub-agents that outlive `codex exec`; left running they hold
connections through the proxy (so `vc` never exited) and keep spending quota.
"""

import subprocess
import sys
import time

import psutil

from voice_copilot.adapters.pty_adapter import kill_process_tree

_GRANDCHILD = "import time; time.sleep(60)"
_CHILD = (
    "import subprocess, sys, time; "
    f"subprocess.Popen([sys.executable, '-c', {_GRANDCHILD!r}]); "
    "time.sleep(60)"
)


def test_descendants_die_with_the_root() -> None:
    root = subprocess.Popen([sys.executable, "-c", _CHILD])
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not psutil.Process(root.pid).children():
            time.sleep(0.05)
        grandchildren = psutil.Process(root.pid).children(recursive=True)
        assert grandchildren, "test setup: no grandchild spawned"
        killed = kill_process_tree(root.pid)
        assert killed >= 2
        assert root.poll() is not None
        assert all(not p.is_running() for p in grandchildren)
    finally:
        kill_process_tree(root.pid)


def test_missing_process_is_a_noop() -> None:
    assert kill_process_tree(None) == 0
    assert kill_process_tree(2**22) == 0
