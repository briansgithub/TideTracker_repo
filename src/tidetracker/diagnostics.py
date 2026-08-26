"""Read-only diagnostics safe to collect from a field or lab device."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

Runner = Callable[..., subprocess.CompletedProcess[str]]


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").rstrip("\x00\n")
    except OSError:
        return None


def _command(runner: Runner, args: Sequence[str]) -> dict[str, Any]:
    try:
        result = runner(
            list(args),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "error": type(exc).__name__}
    return {
        "available": True,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def collect_diagnostics(
    *,
    runner: Runner = subprocess.run,
    data_dir: Path = Path("/var/lib/tidetracker"),
) -> dict[str, Any]:
    """Collect bounded system facts without reading configuration secrets."""

    usage_path = data_dir if data_dir.exists() else Path.cwd()
    usage = shutil.disk_usage(usage_path)
    return {
        "schema_version": 1,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "board_model": _read_text(Path("/proc/device-tree/model")),
        "os_release": _read_text(Path("/etc/os-release")),
        "boot_id": _read_text(Path("/proc/sys/kernel/random/boot_id")),
        "data_dir": {
            "path": str(data_dir),
            "exists": data_dir.exists(),
            "free_bytes": usage.free,
        },
        "root_mount": _command(runner, ["findmnt", "-no", "FSTYPE,OPTIONS", "/"]),
        "memory": _command(runner, ["free", "-b"]),
        "interfaces": _command(runner, ["ip", "-json", "address"]),
        "routes": _command(runner, ["ip", "-json", "route"]),
        "usb_gadget": {
            "interface_present": Path("/sys/class/net/usb0").exists(),
            "udc_state": _first_udc_state(),
        },
        "environment": {
            "asset_dir_set": bool(os.environ.get("TIDETRACKER_ASSET_DIR")),
            "data_dir_set": bool(os.environ.get("TIDETRACKER_DATA_DIR")),
        },
    }


def _first_udc_state() -> str | None:
    states = sorted(Path("/sys/class/udc").glob("*/state"))
    return _read_text(states[0]) if states else None


def diagnostics_json(**kwargs: Any) -> str:
    return json.dumps(collect_diagnostics(**kwargs), sort_keys=True, indent=2)
