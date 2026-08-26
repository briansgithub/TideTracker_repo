"""Injected interfaces at every TideTracker hardware or side-effect boundary."""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol, runtime_checkable

from .models import (
    CycleMode,
    CycleResult,
    JSONValue,
    NetworkStatus,
    RenderModel,
    SolarTimes,
    StationConfig,
    StoredCycle,
    TidePrediction,
)


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


@runtime_checkable
class ModePort(Protocol):
    def read_mode(self) -> CycleMode: ...


@runtime_checkable
class NetworkPort(Protocol):
    def prepare(self, mode: CycleMode, *, timeout_seconds: float) -> NetworkStatus: ...

    def release(self, *, timeout_seconds: float) -> None: ...


@runtime_checkable
class PredictionSource(Protocol):
    def fetch(
        self,
        station: StationConfig,
        *,
        begin: date,
        range_hours: int,
        interval_minutes: int,
        timeout_seconds: float,
    ) -> tuple[TidePrediction, ...]: ...


@runtime_checkable
class Renderer(Protocol):
    def render(self, model: RenderModel, *, timeout_seconds: float) -> bytes: ...


@runtime_checkable
class DisplayPort(Protocol):
    def display(self, framebuffer: bytes, *, timeout_seconds: float) -> None: ...


@runtime_checkable
class DonePort(Protocol):
    def signal_done(self) -> None: ...


@runtime_checkable
class StateStore(Protocol):
    def load_last_good(self) -> StoredCycle | None: ...

    def load_display_state(self) -> str | None: ...

    def invalidate_display_state(self) -> None: ...

    def mark_display_state(self, framebuffer_sha256: str) -> None: ...

    def persist(self, snapshot: StoredCycle) -> None: ...

    def persist_result(self, result: CycleResult) -> None: ...

    def sync(self) -> None: ...


@runtime_checkable
class TelemetrySink(Protocol):
    def emit(self, event: str, **fields: JSONValue) -> None: ...


@runtime_checkable
class SolarCalculator(Protocol):
    def __call__(
        self,
        local_date: date,
        latitude: float,
        longitude: float,
        timezone_name: str,
    ) -> SolarTimes: ...
