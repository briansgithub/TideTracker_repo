from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tidetracker.models import TidePrediction
from tidetracker.tide_window import (
    TideWindowError,
    build_tide_window,
    hourly_chart_ticks,
    validate_tide_coverage,
)


def _predictions(start: datetime, end: datetime) -> tuple[TidePrediction, ...]:
    count = int((end - start).total_seconds() // 300) + 1
    return tuple(
        TidePrediction(start + timedelta(minutes=5 * index), float(index))
        for index in range(count)
    )


def test_default_new_york_request_expands_to_cover_local_chart() -> None:
    window = build_tide_window(
        datetime(2026, 8, 25, 12, tzinfo=UTC),
        "America/New_York",
        chart_hours=48,
        minimum_range_hours=60,
    )

    assert window.local_start.isoformat() == "2026-08-24T12:00:00-04:00"
    assert window.local_end.isoformat() == "2026-08-26T12:00:00-04:00"
    assert window.request_begin.isoformat() == "2026-08-24"
    assert window.request_range_hours == 64


@pytest.mark.parametrize(
    ("generated", "expected_absolute_hours", "missing_hour", "repeated_hour"),
    [
        (datetime(2026, 3, 8, 16, tzinfo=UTC), 47, 2, None),
        (datetime(2026, 11, 1, 17, tzinfo=UTC), 49, None, 1),
    ],
)
def test_dst_windows_keep_absolute_spacing_and_local_clock_labels(
    generated: datetime,
    expected_absolute_hours: int,
    missing_hour: int | None,
    repeated_hour: int | None,
) -> None:
    window = build_tide_window(
        generated,
        "America/New_York",
        chart_hours=48,
        minimum_range_hours=60,
    )
    ticks = hourly_chart_ticks(window.utc_start, window.utc_end, "America/New_York")

    assert (window.utc_end - window.utc_start).total_seconds() == expected_absolute_hours * 3600
    assert all(
        later[0] - earlier[0] == timedelta(hours=1)
        for earlier, later in zip(ticks, ticks[1:], strict=False)
    )
    local_hours = [local.hour for _, local in ticks]
    if missing_hour is not None:
        transition_day = window.local_start.date() + timedelta(days=1)
        assert not [
            local
            for _, local in ticks
            if local.date() == transition_day and local.hour == missing_hour
        ]
    if repeated_hour is not None:
        transition_day = window.local_start.date() + timedelta(days=1)
        repeated = [
            local
            for _, local in ticks
            if local.date() == transition_day and local.hour == repeated_hour
        ]
        assert [value.fold for value in repeated] == [0, 1]
    assert local_hours


def test_truncated_or_gapped_predictions_fail_coverage() -> None:
    window = build_tide_window(
        datetime(2026, 8, 25, 12, tzinfo=UTC),
        "America/New_York",
        chart_hours=48,
        minimum_range_hours=60,
    )
    complete = _predictions(
        datetime.combine(window.request_begin, datetime.min.time(), tzinfo=UTC),
        window.utc_end,
    )
    assert validate_tide_coverage(complete, window, interval_minutes=5) == complete

    with pytest.raises(TideWindowError, match="end before"):
        validate_tide_coverage(complete[:-2], window, interval_minutes=5)
    with pytest.raises(TideWindowError, match="gap"):
        validate_tide_coverage(
            complete[:20] + complete[22:],
            window,
            interval_minutes=5,
        )
