"""Station catalogue access without importing rendering or hardware dependencies."""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True, slots=True)
class Station:
    station_id: str
    city: str
    state: str
    latitude: float
    longitude: float
    timezone: str

    def zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unsupported timezone for station {self.station_id}: {self.timezone}") from exc


class StationCatalog:
    def __init__(self, stations: Iterable[Station]) -> None:
        items = tuple(stations)
        self._stations = {station.station_id: station for station in items}
        if len(self._stations) != len(items):
            raise ValueError("station catalogue contains duplicate station IDs")

    @classmethod
    def from_csv(cls, path: Path) -> StationCatalog:
        with path.open("r", newline="", encoding="utf-8-sig") as source:
            rows = csv.DictReader(source)
            required = {
                "Station ID",
                "City",
                "State",
                "decimal_latitude",
                "decimal_longitude",
                "time_zone",
            }
            if not rows.fieldnames or not required.issubset(rows.fieldnames):
                raise ValueError("station catalogue is missing required columns")
            stations = [
                Station(
                    station_id=row["Station ID"].strip(),
                    city=row["City"].strip(),
                    state=row["State"].strip(),
                    latitude=float(row["decimal_latitude"]),
                    longitude=float(row["decimal_longitude"]),
                    timezone=row["time_zone"].strip(),
                )
                for row in rows
            ]
        return cls(stations)

    def get(self, station_id: str) -> Station:
        try:
            return self._stations[station_id]
        except KeyError as exc:
            raise ValueError(f"unknown NOAA station: {station_id}") from exc

    def all(self) -> tuple[Station, ...]:
        return tuple(sorted(self._stations.values(), key=lambda item: (item.state, item.city)))


def default_catalog_path() -> Path:
    configured = __import__("os").environ.get("TIDETRACKER_STATIONS")
    if configured:
        return Path(configured)
    source_checkout = Path(__file__).resolve().parents[2] / "stations.csv"
    if source_checkout.exists():
        return source_checkout
    return Path("/usr/share/tidetracker/stations.csv")
