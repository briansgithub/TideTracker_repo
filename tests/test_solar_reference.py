from __future__ import annotations

from datetime import date, datetime

import pytest

from tidetracker.solar import calculate_sun_times

# Frozen against PyEphem 4.2.1 with a -0:34 civil horizon and pressure disabled.
# Runtime code does not import or require PyEphem.

@pytest.mark.parametrize(
    ("day", "latitude", "longitude", "zone", "sunrise", "sunset"),
    [
        (
            date(2026, 3, 8),
            40.355,
            -74.065,
            "America/New_York",
            "2026-03-08T07:18:55-04:00",
            "2026-03-08T18:55:35-04:00",
        ),
        (
            date(2026, 11, 1),
            40.355,
            -74.065,
            "America/New_York",
            "2026-11-01T06:26:02-05:00",
            "2026-11-01T16:53:06-05:00",
        ),
        (
            date(2026, 6, 21),
            21.3069,
            -157.8583,
            "Pacific/Honolulu",
            "2026-06-21T05:50:25-10:00",
            "2026-06-21T19:16:16-10:00",
        ),
    ],
)
def test_lightweight_solar_calculation_matches_frozen_reference_within_two_minutes(
    day: date,
    latitude: float,
    longitude: float,
    zone: str,
    sunrise: str,
    sunset: str,
) -> None:
    result = calculate_sun_times(day, latitude, longitude, zone)

    assert result.sunrise is not None
    assert result.sunset is not None
    assert abs((result.sunrise - datetime.fromisoformat(sunrise)).total_seconds()) <= 120
    assert abs((result.sunset - datetime.fromisoformat(sunset)).total_seconds()) <= 120
