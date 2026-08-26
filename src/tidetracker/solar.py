"""Deterministic sunrise/sunset calculation with IANA timezone conversion."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import SolarTimes

OFFICIAL_ZENITH_DEGREES = 90.833


def _normalize(value: float, maximum: float) -> float:
    return value % maximum


def _event_utc_hour(
    local_date: date,
    latitude: float,
    longitude: float,
    *,
    sunrise: bool,
    zenith_degrees: float,
) -> float | None:
    day = local_date.timetuple().tm_yday
    longitude_hour = longitude / 15.0
    approximate = day + ((6.0 if sunrise else 18.0) - longitude_hour) / 24.0
    mean_anomaly = (0.9856 * approximate) - 3.289
    true_longitude = _normalize(
        mean_anomaly
        + 1.916 * math.sin(math.radians(mean_anomaly))
        + 0.020 * math.sin(math.radians(2.0 * mean_anomaly))
        + 282.634,
        360.0,
    )
    right_ascension = math.degrees(math.atan(0.91764 * math.tan(math.radians(true_longitude))))
    right_ascension = _normalize(right_ascension, 360.0)
    longitude_quadrant = math.floor(true_longitude / 90.0) * 90.0
    ascension_quadrant = math.floor(right_ascension / 90.0) * 90.0
    right_ascension = (right_ascension + longitude_quadrant - ascension_quadrant) / 15.0

    sin_declination = 0.39782 * math.sin(math.radians(true_longitude))
    cos_declination = math.cos(math.asin(sin_declination))
    latitude_radians = math.radians(latitude)
    denominator = cos_declination * math.cos(latitude_radians)
    if abs(denominator) < 1e-15:
        return None
    cosine_hour = (
        math.cos(math.radians(zenith_degrees)) - sin_declination * math.sin(latitude_radians)
    ) / denominator
    if cosine_hour > 1.0 or cosine_hour < -1.0:
        return None
    hour_angle = math.degrees(math.acos(cosine_hour))
    if sunrise:
        hour_angle = 360.0 - hour_angle
    hour_angle /= 15.0
    local_mean_time = hour_angle + right_ascension - (0.06571 * approximate) - 6.622
    return _normalize(local_mean_time - longitude_hour, 24.0)


def _utc_hour_to_local_date(requested_date: date, utc_hour: float, zone: tzinfo) -> datetime:
    seconds = int(round(utc_hour * 3600.0)) % (24 * 3600)
    for day_offset in (0, 1, -1, 2, -2):
        utc_midnight = datetime.combine(
            requested_date + timedelta(days=day_offset), time.min, tzinfo=UTC
        )
        candidate = (utc_midnight + timedelta(seconds=seconds)).astimezone(zone)
        if candidate.date() == requested_date:
            return candidate
    raise ValueError("could not map solar event to requested local date")


def calculate_sun_times(
    local_date: date,
    latitude: float,
    longitude: float,
    timezone_name: str,
    *,
    zenith_degrees: float = OFFICIAL_ZENITH_DEGREES,
) -> SolarTimes:
    """Calculate local civil sunrise and sunset for ``local_date``.

    The algorithm is the public NOAA/USNO-style approximation traditionally
    used for sunrise tables. ZoneInfo performs the DST conversion; polar days
    and nights are represented by ``None`` events.
    """

    if not math.isfinite(latitude) or not -90.0 <= latitude <= 90.0:
        raise ValueError("latitude must be between -90 and 90")
    if not math.isfinite(longitude) or not -180.0 <= longitude <= 180.0:
        raise ValueError("longitude must be between -180 and 180")
    if not 90.0 <= zenith_degrees <= 100.0:
        raise ValueError("zenith_degrees is outside the supported range")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown IANA timezone: {timezone_name}") from exc

    rise_hour = _event_utc_hour(
        local_date,
        latitude,
        longitude,
        sunrise=True,
        zenith_degrees=zenith_degrees,
    )
    set_hour = _event_utc_hour(
        local_date,
        latitude,
        longitude,
        sunrise=False,
        zenith_degrees=zenith_degrees,
    )
    sunrise = (
        _utc_hour_to_local_date(local_date, rise_hour, zone) if rise_hour is not None else None
    )
    sunset = _utc_hour_to_local_date(local_date, set_hour, zone) if set_hour is not None else None
    return SolarTimes(
        local_date=local_date,
        timezone=timezone_name,
        sunrise=sunrise,
        sunset=sunset,
    )
