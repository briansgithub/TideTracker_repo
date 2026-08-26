"""Hardware-neutral domain models used by the TideTracker runtime."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import TypeAlias
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]


class CyclePhase(StrEnum):
    MODE = "MODE"
    NETWORK = "NETWORK"
    FETCH = "FETCH"
    VALIDATE = "VALIDATE"
    COMPUTE = "COMPUTE"
    RENDER = "RENDER"
    DISPLAY = "DISPLAY"
    PERSIST = "PERSIST"
    DONE = "DONE"


class CycleMode(StrEnum):
    RUN = "run"
    SETUP = "setup"


@dataclass(frozen=True, slots=True)
class StationConfig:
    station_id: str
    name: str
    latitude: float
    longitude: float
    timezone: str

    def __post_init__(self) -> None:
        if not (self.station_id.isascii() and self.station_id.isdigit()):
            raise ValueError("station_id must contain only ASCII digits")
        if not 1 <= len(self.station_id) <= 16:
            raise ValueError("station_id must contain between 1 and 16 digits")
        if not self.name.strip():
            raise ValueError("station name must not be empty")
        if not math.isfinite(self.latitude) or not -90.0 <= self.latitude <= 90.0:
            raise ValueError("latitude must be between -90 and 90")
        if not math.isfinite(self.longitude) or not -180.0 <= self.longitude <= 180.0:
            raise ValueError("longitude must be between -180 and 180")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone: {self.timezone}") from exc


@dataclass(frozen=True, slots=True)
class PhaseTimeouts:
    normal_cycle_seconds: float = 180.0
    setup_session_seconds: float = 900.0
    network_seconds: float = 45.0
    fetch_seconds: float = 20.0
    render_seconds: float = 60.0
    display_seconds: float = 120.0
    persist_seconds: float = 10.0

    def __post_init__(self) -> None:
        maximums = {
            "normal_cycle_seconds": 180.0,
            "setup_session_seconds": 900.0,
            "network_seconds": 45.0,
            "fetch_seconds": 20.0,
            "render_seconds": 60.0,
            "display_seconds": 120.0,
            "persist_seconds": 10.0,
        }
        for field_name in self.__dataclass_fields__:
            value = getattr(self, field_name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{field_name} must be a positive finite number")
            if value > maximums[field_name]:
                raise ValueError(
                    f"{field_name} exceeds the supervised service safety bound"
                )


@dataclass(frozen=True, slots=True)
class AppConfig:
    schema_version: int
    station: StationConfig
    datum: str = "MLLW"
    units: str = "english"
    prediction_interval_minutes: int = 5
    prediction_range_hours: int = 60
    chart_hours: int = 48
    timeouts: PhaseTimeouts = field(default_factory=PhaseTimeouts)

    CURRENT_SCHEMA_VERSION = 1

    def __post_init__(self) -> None:
        if self.schema_version != self.CURRENT_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported config schema_version {self.schema_version}; "
                f"expected {self.CURRENT_SCHEMA_VERSION}"
            )
        if self.datum.upper() not in {"MLLW", "MLW", "MSL", "MTL", "MHW", "MHHW", "NAVD"}:
            raise ValueError(f"unsupported NOAA datum: {self.datum}")
        if self.units not in {"english", "metric"}:
            raise ValueError("units must be 'english' or 'metric'")
        if self.prediction_interval_minutes not in {1, 5, 6, 10, 15, 30, 60}:
            raise ValueError("prediction_interval_minutes is not supported")
        if not 1 <= self.prediction_range_hours <= 240:
            raise ValueError("prediction_range_hours must be between 1 and 240")
        if not 1 <= self.chart_hours <= self.prediction_range_hours:
            raise ValueError("chart_hours must fit within prediction_range_hours")


@dataclass(frozen=True, slots=True)
class TidePrediction:
    timestamp: datetime
    value: float

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("prediction timestamp must be timezone-aware")
        if not math.isfinite(self.value):
            raise ValueError("prediction value must be finite")


@dataclass(frozen=True, slots=True)
class SolarTimes:
    local_date: date
    timezone: str
    sunrise: datetime | None
    sunset: datetime | None


@dataclass(frozen=True, slots=True)
class NetworkStatus:
    connected: bool
    interface: str | None = None


@dataclass(frozen=True, slots=True)
class RenderModel:
    generated_at: datetime
    config: AppConfig
    predictions: tuple[TidePrediction, ...]
    solar: SolarTimes
    solar_days: tuple[SolarTimes, ...] = ()
    previous_framebuffer: bytes | None = None
    offline: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StoredCycle:
    station_id: str
    generated_at: datetime
    predictions: tuple[TidePrediction, ...]
    framebuffer: bytes
    framebuffer_sha256: str


@dataclass(frozen=True, slots=True)
class CycleResult:
    run_id: str
    success: bool
    mode: CycleMode
    started_at: datetime
    ended_at: datetime
    phases: tuple[CyclePhase, ...]
    phase_durations_seconds: Mapping[str, float]
    framebuffer_sha256: str | None = None
    display_refreshed: bool = False
    used_cached_data: bool = False
    network_interface: str | None = None
    production_route_interface: str | None = None
    release_id: str = "built-in"
    execution_source: str = "built-in"
    noaa_fault_kind: str | None = None
    injected_noaa_fault: str | None = None
    wifi_active_observed_seconds: float | None = None
    wifi_active_observation_source: str | None = None
    error_phase: CyclePhase | None = None
    error_type: str | None = None
    error_message: str | None = None
