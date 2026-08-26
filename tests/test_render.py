from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tidetracker.display import FRAME_BYTES
from tidetracker.models import (
    AppConfig,
    RenderModel,
    SolarTimes,
    StationConfig,
    TidePrediction,
)
from tidetracker.render import (
    PillowRenderer,
    framebuffer_to_image,
    local_extrema,
    remove_leading_hour_zero,
)


def config() -> AppConfig:
    return AppConfig(
        schema_version=1,
        station=StationConfig(
            station_id="8531833",
            name="Navesink, NJ",
            latitude=40.376,
            longitude=-74.025,
            timezone="America/New_York",
        ),
    )


def model(*, offline: bool = False, previous_framebuffer: bytes | None = None) -> RenderModel:
    zone = ZoneInfo("America/New_York")
    now = datetime(2026, 8, 25, 8, 0, tzinfo=zone)
    prediction_start = datetime(2026, 8, 24, tzinfo=UTC)
    predictions = tuple(
        TidePrediction(
            timestamp=prediction_start + timedelta(minutes=5 * index),
            value=1.5 + 2.0 * __import__("math").sin(index / 18),
        )
        for index in range(769)
    )
    return RenderModel(
        generated_at=now,
        config=config(),
        predictions=predictions,
        solar=SolarTimes(
            local_date=now.date(),
            timezone="America/New_York",
            sunrise=now.replace(hour=6, minute=18),
            sunset=now.replace(hour=19, minute=39),
        ),
        previous_framebuffer=previous_framebuffer,
        offline=offline,
    )


def test_native_renderer_is_deterministic_and_controller_sized() -> None:
    asset_dir = Path(__file__).parents[1]
    renderer = PillowRenderer(asset_dir=asset_dir)

    first = renderer.render(model(), timeout_seconds=5)
    second = renderer.render(model(), timeout_seconds=5)

    assert first == second
    assert len(first) == FRAME_BYTES
    assert framebuffer_to_image(first).size == (800, 480)


def test_offline_overlay_changes_frame() -> None:
    renderer = PillowRenderer(asset_dir=Path(__file__).parents[1])

    assert renderer.render(model(), timeout_seconds=5) != renderer.render(
        model(offline=True), timeout_seconds=5
    )


def test_offline_overlay_uses_retained_frame_without_rerendering_chart() -> None:
    renderer = PillowRenderer(asset_dir=Path(__file__).parents[1])
    previous = renderer.render(model(), timeout_seconds=5)

    first = renderer.render(
        model(offline=True, previous_framebuffer=previous), timeout_seconds=5
    )
    second = renderer.render(
        model(offline=True, previous_framebuffer=previous), timeout_seconds=5
    )

    assert first == second
    assert first != previous
    assert len(first) == FRAME_BYTES


def test_legacy_time_format_and_extrema() -> None:
    assert remove_leading_hour_zero("08:03 AM") == "8:03 AM"
    assert remove_leading_hour_zero("10:03 AM") == "10:03 AM"
    assert local_extrema([0, 2, 1, -1, 0]) == ((1,), (3,))
    assert local_extrema([0, 2, 2, 2, 0, -1, -1, 0]) == ((2,), (5,))
