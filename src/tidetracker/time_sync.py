"""Trusted wall-clock recovery for RTC-less, power-cycled field devices."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .ports import Clock
from .storage import atomic_write_json

MIN_SANE_UTC = datetime(2024, 1, 1, tzinfo=UTC)
MAX_SANE_UTC = datetime(2100, 1, 1, tzinfo=UTC)


class TrustedTimeError(RuntimeError):
    """Trusted time could not be restored, synchronized, or persisted."""


def sane_utc(value: datetime) -> datetime:
    """Return an aware UTC value inside the field runtime's accepted era."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise TrustedTimeError("wall clock must be timezone-aware")
    normalized = value.astimezone(UTC)
    if not MIN_SANE_UTC <= normalized < MAX_SANE_UTC:
        raise TrustedTimeError("wall clock is outside the accepted UTC range")
    return normalized


class TrustedEpochStore:
    """Atomically retain a monotonically nondecreasing trusted UTC epoch."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> int | None:
        if not self.path.exists():
            return None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("schema_version") != 1:
                raise TrustedTimeError("trusted-time state has an unsupported schema")
            raw_epoch = value.get("trusted_utc_epoch")
            if isinstance(raw_epoch, bool) or not isinstance(raw_epoch, int):
                raise TrustedTimeError("trusted-time epoch must be an integer")
            sane_utc(datetime.fromtimestamp(raw_epoch, UTC))
            return raw_epoch
        except (OSError, ValueError, OverflowError, json.JSONDecodeError) as exc:
            raise TrustedTimeError(f"cannot read trusted-time state: {exc}") from exc

    def persist(self, value: datetime) -> int:
        candidate = int(sane_utc(value).timestamp())
        existing = self.load()
        trusted_epoch = max(candidate, existing) if existing is not None else candidate
        atomic_write_json(
            self.path,
            {
                "schema_version": 1,
                "trusted_utc_epoch": trusted_epoch,
                "trusted_utc": datetime.fromtimestamp(trusted_epoch, UTC).isoformat(),
            },
        )
        return trusted_epoch


class ClockSetter(Protocol):
    def set_epoch(self, epoch: int, *, timeout_seconds: float) -> None: ...


class CommandRunner(Protocol):
    def __call__(
        self,
        args: Sequence[str],
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]: ...


def _command_runner(
    args: Sequence[str], *, timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        capture_output=True,
        check=False,
        shell=False,
        text=True,
        timeout=timeout,
    )


class RootDateClockSetter:
    """Set UTC with one exact argv command; the service supplies CAP_SYS_TIME."""

    def __init__(
        self,
        *,
        runner: CommandRunner = _command_runner,
        date_binary: str = "/usr/bin/date",
    ) -> None:
        self.runner = runner
        self.date_binary = date_binary

    def set_epoch(self, epoch: int, *, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise TrustedTimeError("clock-set timeout budget is exhausted")
        try:
            completed = self.runner(
                [self.date_binary, "--utc", f"--set=@{epoch}"],
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TrustedTimeError(f"trusted-time clock restore failed: {exc}") from exc
        if completed.returncode != 0:
            detail = " ".join(completed.stderr.split())[:200]
            raise TrustedTimeError(
                f"trusted-time clock restore exited {completed.returncode}: {detail}"
            )


class TimeSyncPort(Protocol):
    def restore_before_network(self, *, timeout_seconds: float) -> bool: ...

    def wait_until_synchronized(self, *, timeout_seconds: float) -> datetime: ...

    def persist_trusted(self, value: datetime) -> int: ...


class TrustedTimeAuthority:
    """Restore an estimate, then accept time only after systemd NTP confirms it."""

    def __init__(
        self,
        store: TrustedEpochStore,
        clock: Clock,
        *,
        clock_setter: ClockSetter | None = None,
        runner: CommandRunner = _command_runner,
        timedatectl_binary: str = "/usr/bin/timedatectl",
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval_seconds: float = 0.25,
        restore_lag_seconds: float = 300.0,
    ) -> None:
        if poll_interval_seconds <= 0 or restore_lag_seconds < 0:
            raise ValueError("trusted-time timing bounds are invalid")
        self.store = store
        self.clock = clock
        self.clock_setter = clock_setter or RootDateClockSetter()
        self.runner = runner
        self.timedatectl_binary = timedatectl_binary
        self.monotonic = monotonic or clock.monotonic
        self.sleep = sleep
        self.poll_interval_seconds = poll_interval_seconds
        self.restore_lag_seconds = restore_lag_seconds

    def restore_before_network(self, *, timeout_seconds: float) -> bool:
        if timeout_seconds <= 0:
            raise TrustedTimeError("trusted-time restore timeout budget is exhausted")
        trusted_epoch = self.store.load()
        if trusted_epoch is None:
            return False
        now = self.clock.now()
        try:
            current_epoch = sane_utc(now).timestamp()
        except TrustedTimeError:
            current_epoch = float("-inf")
        if current_epoch < trusted_epoch - self.restore_lag_seconds:
            self.clock_setter.set_epoch(trusted_epoch, timeout_seconds=timeout_seconds)
            return True
        return False

    def wait_until_synchronized(self, *, timeout_seconds: float) -> datetime:
        if timeout_seconds <= 0:
            raise TrustedTimeError("time synchronization timeout budget is exhausted")
        deadline = self.monotonic() + timeout_seconds
        last_detail = "not synchronized"
        while True:
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise TrustedTimeError(f"time synchronization timed out: {last_detail}")
            command_timeout = min(2.0, remaining)
            try:
                completed = self.runner(
                    [
                        self.timedatectl_binary,
                        "show",
                        "--property=NTPSynchronized",
                        "--value",
                    ],
                    timeout=command_timeout,
                )
            except subprocess.TimeoutExpired:
                completed = None
                last_detail = "timedatectl timed out"
            except OSError as exc:
                raise TrustedTimeError(f"cannot query systemd time synchronization: {exc}") from exc
            if completed is not None:
                answer = completed.stdout.strip().lower()
                if completed.returncode == 0 and answer == "yes":
                    return sane_utc(self.clock.now())
                last_detail = (
                    f"timedatectl exit {completed.returncode}"
                    if completed.returncode
                    else f"NTPSynchronized={answer or 'unknown'}"
                )
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                continue
            self.sleep(min(self.poll_interval_seconds, remaining))

    def persist_trusted(self, value: datetime) -> int:
        return self.store.persist(sane_utc(value))


@dataclass(slots=True)
class AssumedSynchronizedTime:
    """Injected deterministic/no-op authority for unit tests and simulation."""

    clock: Clock

    def restore_before_network(self, *, timeout_seconds: float) -> bool:
        if timeout_seconds <= 0:
            raise TrustedTimeError("trusted-time restore timeout budget is exhausted")
        return False

    def wait_until_synchronized(self, *, timeout_seconds: float) -> datetime:
        if timeout_seconds <= 0:
            raise TrustedTimeError("time synchronization timeout budget is exhausted")
        return sane_utc(self.clock.now())

    def persist_trusted(self, value: datetime) -> int:
        return int(sane_utc(value).timestamp())


__all__ = [
    "AssumedSynchronizedTime",
    "MAX_SANE_UTC",
    "MIN_SANE_UTC",
    "RootDateClockSetter",
    "TimeSyncPort",
    "TrustedEpochStore",
    "TrustedTimeAuthority",
    "TrustedTimeError",
    "sane_utc",
]
