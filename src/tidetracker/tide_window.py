"""Time-zone-correct tide chart and NOAA request windows."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .models import TidePrediction


class TideWindowError(ValueError):
    """Raised when a prediction series cannot cover the requested chart."""


@dataclass(frozen=True, slots=True)
class TideWindow:
    local_start: datetime
    local_end: datetime
    utc_start: datetime
    utc_end: datetime
    request_begin: date
    request_range_hours: int


def build_tide_window(
    generated_at: datetime,
    timezone: str,
    *,
    chart_hours: int,
    minimum_range_hours: int,
) -> TideWindow:
    """Return a local-noon chart window and a GMT NOAA request that covers it."""

    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise TideWindowError("chart generation time must be timezone-aware")
    if chart_hours <= 0 or minimum_range_hours <= 0:
        raise TideWindowError("chart and request ranges must be positive")
    zone = ZoneInfo(timezone)
    local_now = generated_at.astimezone(zone)
    local_start = (local_now - timedelta(days=1)).replace(
        hour=12,
        minute=0,
        second=0,
        microsecond=0,
    )
    # ZoneInfo arithmetic intentionally preserves the local wall-clock end.
    # Conversion to UTC below captures the 47/49-hour DST transition correctly.
    local_end = local_start + timedelta(hours=chart_hours)
    utc_start = local_start.astimezone(UTC)
    utc_end = local_end.astimezone(UTC)
    request_begin = utc_start.date()
    request_origin = datetime.combine(request_begin, time.min, tzinfo=UTC)
    required_hours = math.ceil((utc_end - request_origin).total_seconds() / 3600)
    request_range_hours = max(minimum_range_hours, required_hours)
    if request_range_hours > 240:
        raise TideWindowError("NOAA's 240-hour limit cannot cover the requested chart")
    return TideWindow(
        local_start=local_start,
        local_end=local_end,
        utc_start=utc_start,
        utc_end=utc_end,
        request_begin=request_begin,
        request_range_hours=request_range_hours,
    )


def validate_tide_coverage(
    predictions: tuple[TidePrediction, ...],
    window: TideWindow,
    *,
    interval_minutes: int,
) -> tuple[TidePrediction, ...]:
    """Reject truncated or internally gapped prediction series."""

    if len(predictions) < 2:
        raise TideWindowError("at least two tide predictions are required")
    ordered = tuple(sorted(predictions, key=lambda item: item.timestamp))
    if ordered[0].timestamp.astimezone(UTC) > window.utc_start:
        raise TideWindowError("tide predictions start after the chart window")
    if ordered[-1].timestamp.astimezone(UTC) < window.utc_end:
        raise TideWindowError("tide predictions end before the chart window")
    maximum_gap = timedelta(minutes=interval_minutes * 1.5)
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if current.timestamp.astimezone(UTC) - previous.timestamp.astimezone(UTC) > maximum_gap:
            raise TideWindowError("tide predictions contain a gap")
    return ordered


def hourly_chart_ticks(
    start: datetime,
    end: datetime,
    timezone: str,
) -> tuple[tuple[datetime, datetime], ...]:
    """Return equally spaced absolute-hour ticks paired with local labels."""

    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    tick = start_utc.replace(minute=0, second=0, microsecond=0)
    while tick < start_utc:
        tick += timedelta(hours=1)
    zone = ZoneInfo(timezone)
    result: list[tuple[datetime, datetime]] = []
    while tick <= end_utc:
        result.append((tick, tick.astimezone(zone)))
        tick += timedelta(hours=1)
    return tuple(result)
