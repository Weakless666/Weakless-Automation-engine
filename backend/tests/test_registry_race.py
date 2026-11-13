"""The node registry must never be observed half-loaded by concurrent callers."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = """
import threading
from app.engine import registry

results = []
start = threading.Barrier(16)

def ask():
    start.wait()
    results.append(len(registry.all_specs()))

threads = [threading.Thread(target=ask) for _ in range(16)]
[t.start() for t in threads]
[t.join() for t in threads]
print(sorted(set(results)))
"""


def test_concurrent_first_use_sees_the_full_registry():
    # A fresh interpreter, so the registry really is unloaded when the threads start.
    out = subprocess.run([sys.executable, "-c", SCRIPT], cwd=Path(__file__).resolve().parent.parent,
                         capture_output=True, text=True, timeout=120, env={**__import__("os").environ, "WAE_ENV": "test"})
    assert out.returncode == 0, out.stderr
    from app.engine import registry

    registry.load_nodes()
    assert out.stdout.strip() == f"[{len(registry.all_specs())}]", out.stdout  # every thread saw the complete registry
