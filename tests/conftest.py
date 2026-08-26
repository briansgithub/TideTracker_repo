"""Make source-layout imports available to subprocesses spawned by tests."""

from __future__ import annotations

import os
from pathlib import Path

SOURCE_ROOT = str(Path(__file__).parents[1] / "src")
PYTHON_PATH_ENTRIES = [
    entry for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep) if entry
]
if SOURCE_ROOT not in PYTHON_PATH_ENTRIES:
    os.environ["PYTHONPATH"] = os.pathsep.join((SOURCE_ROOT, *PYTHON_PATH_ENTRIES))
