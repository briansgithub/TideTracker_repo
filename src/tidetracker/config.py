"""Versioned, non-secret TideTracker configuration."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, cast

from .models import AppConfig, PhaseTimeouts, StationConfig
from .storage import atomic_write_json


class ConfigError(ValueError):
    """Raised when configuration is malformed, unsafe, or unsupported."""


_SECRET_FRAGMENTS = ("password", "passwd", "secret", "credential", "token", "private_key")
_TOP_LEVEL_KEYS = {
    "schema_version",
    "station",
    "datum",
    "units",
    "prediction_interval_minutes",
    "prediction_range_hours",
    "chart_hours",
    "timeouts",
}
_STATION_KEYS = {"station_id", "name", "latitude", "longitude", "timezone"}
_TIMEOUT_KEYS = {
    "normal_cycle_seconds",
    "setup_session_seconds",
    "network_seconds",
    "fetch_seconds",
    "render_seconds",
    "display_seconds",
    "persist_seconds",
}
_LEGACY_STATION = re.compile(r"^\s*([0-9]{1,16})(?:\s+-\s+.*)?\s*$")


class ResolvedStation(Protocol):
    @property
    def station_id(self) -> str: ...

    @property
    def city(self) -> str: ...

    @property
    def state(self) -> str: ...

    @property
    def latitude(self) -> float: ...

    @property
    def longitude(self) -> float: ...

    @property
    def timezone(self) -> str: ...


class StationResolver(Protocol):
    def get(self, station_id: str) -> ResolvedStation: ...


def default_config() -> AppConfig:
    """Return the legacy deployment's non-secret defaults."""

    return AppConfig(
        schema_version=AppConfig.CURRENT_SCHEMA_VERSION,
        station=StationConfig(
            station_id="8531833",
            name="Navesink - Red Bank, NJ",
            latitude=40.355,
            longitude=-74.065,
            timezone="America/New_York",
        ),
    )


def _reject_secret_keys(value: object, path: str = "config") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).lower()
            if any(fragment in key_text for fragment in _SECRET_FRAGMENTS):
                raise ConfigError(
                    f"secret field is not permitted in application config: {path}.{key}"
                )
            _reject_secret_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secret_keys(child, f"{path}[{index}]")


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{path} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise ConfigError(f"{path} keys must be strings")
    return cast(Mapping[str, Any], value)


def _reject_unknown(mapping: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ConfigError(f"unknown {path} field(s): {', '.join(unknown)}")


def _integer(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{path} must be an integer")
    return value


def _number(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{path} must be a number")
    return float(value)


def _text(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{path} must be a string")
    return value


def config_from_mapping(raw: Mapping[str, Any]) -> AppConfig:
    """Validate and convert an untrusted mapping to :class:`AppConfig`."""

    _reject_secret_keys(raw)
    _reject_unknown(raw, _TOP_LEVEL_KEYS, "config")

    try:
        version = _integer(raw["schema_version"], "schema_version")
        station_raw = _mapping(raw["station"], "station")
    except KeyError as exc:
        raise ConfigError(f"missing required field: {exc.args[0]}") from exc

    _reject_unknown(station_raw, _STATION_KEYS, "station")
    missing_station = sorted(_STATION_KEYS - set(station_raw))
    if missing_station:
        raise ConfigError(f"missing station field(s): {', '.join(missing_station)}")

    timeout_raw = _mapping(raw.get("timeouts", {}), "timeouts")
    _reject_unknown(timeout_raw, _TIMEOUT_KEYS, "timeouts")
    timeout_defaults = PhaseTimeouts()

    try:
        station = StationConfig(
            station_id=_text(station_raw["station_id"], "station.station_id"),
            name=_text(station_raw["name"], "station.name"),
            latitude=_number(station_raw["latitude"], "station.latitude"),
            longitude=_number(station_raw["longitude"], "station.longitude"),
            timezone=_text(station_raw["timezone"], "station.timezone"),
        )
        timeouts = PhaseTimeouts(
            **{
                key: _number(
                    timeout_raw.get(key, getattr(timeout_defaults, key)), f"timeouts.{key}"
                )
                for key in _TIMEOUT_KEYS
            }
        )
        return AppConfig(
            schema_version=version,
            station=station,
            datum=_text(raw.get("datum", "MLLW"), "datum"),
            units=_text(raw.get("units", "english"), "units"),
            prediction_interval_minutes=_integer(
                raw.get("prediction_interval_minutes", 5), "prediction_interval_minutes"
            ),
            prediction_range_hours=_integer(
                raw.get("prediction_range_hours", 60), "prediction_range_hours"
            ),
            chart_hours=_integer(raw.get("chart_hours", 48), "chart_hours"),
            timeouts=timeouts,
        )
    except (KeyError, ValueError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(str(exc)) from exc


def config_to_mapping(config: AppConfig) -> dict[str, Any]:
    return {
        "schema_version": config.schema_version,
        "station": {
            "station_id": config.station.station_id,
            "name": config.station.name,
            "latitude": config.station.latitude,
            "longitude": config.station.longitude,
            "timezone": config.station.timezone,
        },
        "datum": config.datum,
        "units": config.units,
        "prediction_interval_minutes": config.prediction_interval_minutes,
        "prediction_range_hours": config.prediction_range_hours,
        "chart_hours": config.chart_hours,
        "timeouts": {key: getattr(config.timeouts, key) for key in _TIMEOUT_KEYS},
    }


def migrate_legacy_config(raw: Mapping[str, Any], station_resolver: StationResolver) -> AppConfig:
    """Convert the legacy station selection while discarding ``wifi_*`` data.

    Legacy releases placed NetworkManager credentials beside the station ID.
    Migration deliberately consumes only the station selection: every
    ``wifi_*`` field is ignored and must be reprovisioned into a mode-0600
    NetworkManager connection profile by the network adapter.
    """

    legacy = _mapping(raw, "legacy config")
    non_wifi = {key: value for key, value in legacy.items() if not key.lower().startswith("wifi_")}
    _reject_secret_keys(non_wifi, "legacy config")
    unknown = sorted(set(non_wifi) - {"station_id"})
    if unknown:
        raise ConfigError(f"unknown legacy config field(s): {', '.join(unknown)}")
    station_selection = _text(non_wifi.get("station_id"), "legacy config.station_id")
    match = _LEGACY_STATION.fullmatch(station_selection)
    if match is None:
        raise ConfigError("legacy station_id must start with a numeric NOAA station ID")
    station_id = match.group(1)
    try:
        resolved = station_resolver.get(station_id)
    except (KeyError, ValueError) as exc:
        raise ConfigError(f"cannot resolve legacy NOAA station {station_id}: {exc}") from exc
    if resolved.station_id != station_id:
        raise ConfigError("station resolver returned a mismatched station ID")
    try:
        return AppConfig(
            schema_version=AppConfig.CURRENT_SCHEMA_VERSION,
            station=StationConfig(
                station_id=resolved.station_id,
                name=f"{resolved.city}, {resolved.state}",
                latitude=resolved.latitude,
                longitude=resolved.longitude,
                timezone=resolved.timezone,
            ),
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def load_config(path: str | Path) -> AppConfig:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot load configuration: {exc}") from exc
    return config_from_mapping(_mapping(raw, "config"))


def save_config(path: str | Path, config: AppConfig) -> None:
    """Atomically persist config with owner-only permissions."""

    atomic_write_json(Path(path), config_to_mapping(config), mode=0o600)
