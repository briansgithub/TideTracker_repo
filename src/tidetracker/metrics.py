"""Low-overhead process and interface proxy measurements for field cycles."""

from __future__ import annotations

import importlib
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import CycleResult


def _integer(path: Path) -> int:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return 0


def _process_io() -> tuple[int, int]:
    try:
        values = {
            key.rstrip(":"): int(value)
            for key, value in (
                line.split(maxsplit=1)
                for line in Path("/proc/self/io").read_text(encoding="ascii").splitlines()
            )
        }
    except (OSError, ValueError):
        return 0, 0
    return values.get("read_bytes", 0), values.get("write_bytes", 0)


def _process_swap_kib() -> int:
    try:
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmSwap:"):
                fields = line.split()
                return int(fields[1]) if len(fields) >= 2 else 0
    except (OSError, ValueError):
        pass
    return 0


def _resource_usage() -> tuple[float, float, int]:
    try:
        resource = importlib.import_module("resource")
        usage = resource.getrusage(resource.RUSAGE_SELF)
    except (ImportError, OSError):
        return time.process_time(), 0.0, 0
    max_rss_kib = int(usage.ru_maxrss)
    # macOS reports bytes; Linux (the field target) reports KiB.
    if __import__("sys").platform == "darwin":
        max_rss_kib //= 1024
    return float(usage.ru_utime), float(usage.ru_stime), max_rss_kib


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    monotonic: float
    user_cpu_seconds: float
    system_cpu_seconds: float
    max_rss_kib: int
    process_read_bytes: int
    process_write_bytes: int
    network_rx_bytes: int
    network_tx_bytes: int
    process_swap_kib: int = 0

    @classmethod
    def capture(cls, interface: str = "wlan0") -> ResourceSnapshot:
        user, system, rss = _resource_usage()
        read_bytes, write_bytes = _process_io()
        statistics = Path("/sys/class/net") / interface / "statistics"
        return cls(
            monotonic=time.monotonic(),
            user_cpu_seconds=user,
            system_cpu_seconds=system,
            max_rss_kib=rss,
            process_read_bytes=read_bytes,
            process_write_bytes=write_bytes,
            network_rx_bytes=_integer(statistics / "rx_bytes"),
            network_tx_bytes=_integer(statistics / "tx_bytes"),
            process_swap_kib=_process_swap_kib(),
        )


def cycle_metric_fields(
    result: CycleResult,
    before: ResourceSnapshot,
    after: ResourceSnapshot,
    *,
    display_busy_seconds: float = 0.0,
    refresh_count: int = 0,
) -> dict[str, Any]:
    phase = result.phase_durations_seconds
    wifi_active_seconds = phase.get("NETWORK", 0.0) + phase.get("FETCH", 0.0)
    return {
        "run_id": result.run_id,
        "release_id": result.release_id,
        "execution_source": result.execution_source,
        "noaa_fault_kind": result.noaa_fault_kind,
        "injected_noaa_fault": result.injected_noaa_fault,
        "execution": {
            "release_id": result.release_id,
            "execution_source": result.execution_source,
            "noaa_fault_kind": result.noaa_fault_kind,
            "injected_noaa_fault": result.injected_noaa_fault,
        },
        "production_route_interface": result.production_route_interface,
        "wifi_active_observed_seconds": result.wifi_active_observed_seconds,
        "wifi_active_observation_source": result.wifi_active_observation_source,
        "wake_to_done_seconds": max(0.0, after.monotonic - before.monotonic),
        "cpu_user_seconds": max(0.0, after.user_cpu_seconds - before.user_cpu_seconds),
        "cpu_system_seconds": max(0.0, after.system_cpu_seconds - before.system_cpu_seconds),
        "max_rss_kib": max(before.max_rss_kib, after.max_rss_kib),
        "swap_used_kib": max(before.process_swap_kib, after.process_swap_kib),
        "wifi_active_seconds": wifi_active_seconds,
        "display_busy_seconds": max(0.0, display_busy_seconds),
        "network_rx_bytes": max(0, after.network_rx_bytes - before.network_rx_bytes),
        "network_tx_bytes": max(0, after.network_tx_bytes - before.network_tx_bytes),
        "process_read_bytes": max(0, after.process_read_bytes - before.process_read_bytes),
        "process_write_bytes": max(0, after.process_write_bytes - before.process_write_bytes),
        "display_refresh_count": refresh_count,
        "phase_count": len(result.phases),
        "success": result.success,
    }


def sync_all() -> None:
    sync = getattr(os, "sync", None)
    if sync is not None:
        sync()
