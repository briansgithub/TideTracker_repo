"""Deterministic adapters used by workstation tests and Pi 3 simulations."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from .models import (
    CycleMode,
    CycleResult,
    NetworkStatus,
    StationConfig,
    StoredCycle,
    TidePrediction,
)


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


@dataclass(slots=True)
class FixedClock:
    current: datetime
    monotonic_value: float = 0.0

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.monotonic_value


@dataclass(slots=True)
class StaticMode:
    mode: CycleMode = CycleMode.RUN

    def read_mode(self) -> CycleMode:
        return self.mode


@dataclass(slots=True)
class SimulatedNetwork:
    connected: bool = True
    interface: str = "wlan0"
    calls: list[tuple[CycleMode, float]] = field(default_factory=list)

    def prepare(self, mode: CycleMode, *, timeout_seconds: float) -> NetworkStatus:
        self.calls.append((mode, timeout_seconds))
        return NetworkStatus(self.connected, self.interface if self.connected else None)

    def release(self, *, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise TimeoutError("network release deadline exhausted")


@dataclass(slots=True)
class FixturePredictionSource:
    zone_name: str = "America/New_York"

    def fetch(
        self,
        station: StationConfig,
        *,
        begin: date,
        range_hours: int,
        interval_minutes: int,
        timeout_seconds: float,
    ) -> tuple[TidePrediction, ...]:
        del timeout_seconds
        del station
        start = datetime.combine(begin, datetime.min.time(), tzinfo=UTC)
        count = range_hours * 60 // interval_minutes + 1
        return tuple(
            TidePrediction(
                timestamp=start + timedelta(minutes=index * interval_minutes),
                value=1.1
                + 2.0 * math.sin(index * interval_minutes / (12.42 * 60) * math.tau),
            )
            for index in range(count)
        )


@dataclass(slots=True)
class RecordingDone:
    calls: int = 0

    def signal_done(self) -> None:
        self.calls += 1


@dataclass(slots=True)
class MemoryStateStore:
    last_good: StoredCycle | None = None
    results: list[CycleResult] = field(default_factory=list)
    sync_calls: int = 0

    def load_last_good(self) -> StoredCycle | None:
        return self.last_good

    def persist(self, snapshot: StoredCycle) -> None:
        self.last_good = snapshot

    def persist_result(self, result: CycleResult) -> None:
        self.results.append(result)

    def sync(self) -> None:
        self.sync_calls += 1


@dataclass(slots=True)
class MemoryTelemetry:
    events: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def emit(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))
