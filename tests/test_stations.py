from __future__ import annotations

from pathlib import Path

import pytest

from tidetracker.stations import StationCatalog


def test_loads_navesink_station() -> None:
    catalog = StationCatalog.from_csv(Path(__file__).parents[1] / "stations.csv")

    station = catalog.get("8531833")

    assert station.city == "Navesink - Red Bank"
    assert station.state == "NJ"
    assert station.timezone == "America/New_York"


def test_unknown_station_is_rejected() -> None:
    catalog = StationCatalog.from_csv(Path(__file__).parents[1] / "stations.csv")

    with pytest.raises(ValueError, match="unknown NOAA station"):
        catalog.get("not-a-station")
