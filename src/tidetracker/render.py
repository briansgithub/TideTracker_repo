"""Native-resolution deterministic renderer for the 800x480 monochrome panel."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from .display import FRAME_BYTES, FRAME_HEIGHT, FRAME_WIDTH
from .models import RenderModel, TidePrediction
from .tide_window import build_tide_window, hourly_chart_ticks, validate_tide_coverage


class RenderTimeoutError(TimeoutError):
    pass


def remove_leading_hour_zero(value: str) -> str:
    """Preserve the legacy renderer's intentionally narrow zero-removal behavior."""

    for hour in range(1, 10):
        value = value.replace(f"0{hour}:", f"{hour}:")
    return value


def local_extrema(values: Iterable[float]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Return SciPy-compatible default peaks and valleys without importing SciPy."""

    samples = tuple(values)

    def find(*, maximum: bool) -> tuple[int, ...]:
        result: list[int] = []
        index = 1
        while index < len(samples) - 1:
            rises_from_left = (
                samples[index] > samples[index - 1]
                if maximum
                else samples[index] < samples[index - 1]
            )
            if not rises_from_left:
                index += 1
                continue
            plateau_end = index
            while (
                plateau_end + 1 < len(samples)
                and samples[plateau_end + 1] == samples[index]
            ):
                plateau_end += 1
            falls_to_right = (
                plateau_end + 1 < len(samples)
                and (
                    samples[index] > samples[plateau_end + 1]
                    if maximum
                    else samples[index] < samples[plateau_end + 1]
                )
            )
            if falls_to_right:
                result.append((index + plateau_end) // 2)
            index = plateau_end + 1
        return tuple(result)

    return find(maximum=True), find(maximum=False)


def _font(asset_dir: Path, name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = asset_dir / name
    if path.exists():
        return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def default_asset_dir() -> Path:
    configured = os.environ.get("TIDETRACKER_ASSET_DIR")
    if configured:
        return Path(configured)
    checkout = Path(__file__).resolve().parents[2]
    if (checkout / "Ubuntu-Regular.ttf").exists():
        return checkout
    return Path("/usr/share/tidetracker")


@dataclass(frozen=True, slots=True)
class ChartBox:
    left: int = 92
    top: int = 88
    right: int = 777
    bottom: int = 405

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


DEFAULT_CHART_BOX = ChartBox()


def image_to_framebuffer(image: Image.Image) -> bytes:
    """Apply the field panel's exact 180-degree/controller-bit convention."""

    if image.size != (FRAME_WIDTH, FRAME_HEIGHT):
        raise ValueError(f"bitmap must be exactly {FRAME_WIDTH}x{FRAME_HEIGHT}")
    rotated = image.transpose(Image.Transpose.ROTATE_180)
    raw = bytearray(rotated.convert("1").tobytes("raw"))
    for index in range(len(raw)):
        raw[index] ^= 0xFF
    if len(raw) != FRAME_BYTES:
        raise RuntimeError(f"renderer produced {len(raw)} bytes instead of {FRAME_BYTES}")
    return bytes(raw)


def legacy_bitmap_to_framebuffer(path: str | Path) -> bytes:
    """Decode one checked legacy bitmap into controller-native bytes."""

    with Image.open(path) as source:
        source.load()
        return image_to_framebuffer(source)


class PillowRenderer:
    """Render directly at panel resolution and return controller-native bytes.

    The output is rotated 180 degrees and bit-inverted to preserve the checked-in
    Waveshare driver's controller convention.
    """

    def __init__(
        self,
        *,
        asset_dir: Path | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        chart: ChartBox = DEFAULT_CHART_BOX,
    ) -> None:
        self.asset_dir = asset_dir or default_asset_dir()
        self.monotonic = monotonic
        self.chart = chart
        self.regular = _font(self.asset_dir, "Ubuntu-Regular.ttf", 14)
        self.bold = _font(self.asset_dir, "Ubuntu-Bold.ttf", 16)
        self.small_bold = _font(self.asset_dir, "Ubuntu-Bold.ttf", 11)
        self.title = _font(self.asset_dir, "Ubuntu-Bold.ttf", 18)

    def _check_deadline(self, deadline: float) -> None:
        if self.monotonic() > deadline:
            raise RenderTimeoutError("render deadline exceeded")

    def render(self, model: RenderModel, *, timeout_seconds: float) -> bytes:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        deadline = self.monotonic() + timeout_seconds
        if model.offline and model.previous_framebuffer is not None:
            if len(model.previous_framebuffer) != FRAME_BYTES:
                raise ValueError("previous framebuffer has an invalid size")
            image = framebuffer_to_image(model.previous_framebuffer).copy()
            self._draw_offline(image, ImageDraw.Draw(image))
            return self._pack(image, deadline)

        predictions = tuple(sorted(model.predictions, key=lambda item: item.timestamp))
        if len(predictions) < 2:
            raise ValueError("at least two tide predictions are required")
        self._check_deadline(deadline)

        image = Image.new("1", (FRAME_WIDTH, FRAME_HEIGHT), 1)
        draw = ImageDraw.Draw(image)
        self._draw_header(draw, model)
        visible, points, start, end, low, high = self._scale_predictions(predictions, model)
        self._draw_chart(draw, model, visible, points, start, end, low, high)
        self._check_deadline(deadline)

        if model.offline:
            self._draw_offline(image, draw)
        return self._pack(image, deadline)

    def _pack(self, image: Image.Image, deadline: float) -> bytes:
        raw = image_to_framebuffer(image)
        self._check_deadline(deadline)
        return raw

    def _draw_header(self, draw: ImageDraw.ImageDraw, model: RenderModel) -> None:
        generated = model.generated_at.astimezone(ZoneInfo(model.config.station.timezone))
        refresh = remove_leading_hour_zero(generated.strftime("%I:%M %p"))
        draw.text((18, 7), "Last Refresh:", font=self.regular, fill=0)
        draw.multiline_text((110, 7), f"{refresh}\n{generated:%m/%d/%Y}", font=self.regular, fill=0)

        sunrise = (
            remove_leading_hour_zero(model.solar.sunrise.strftime("%I:%M %p"))
            if model.solar.sunrise
            else "Unavailable"
        )
        sunset = (
            remove_leading_hour_zero(model.solar.sunset.strftime("%I:%M %p"))
            if model.solar.sunset
            else "Unavailable"
        )
        icon_path = self.asset_dir / "sun_rise.png"
        if icon_path.exists():
            with Image.open(icon_path) as icon:
                icon = icon.convert("1").resize((40, 40))
                draw._image.paste(icon, (585, 5))
        draw.multiline_text((630, 4), f"Rise:   {sunrise}\nSet:     {sunset}", font=self.bold, fill=0)

    def _scale_predictions(
        self, predictions: tuple[TidePrediction, ...], model: RenderModel
    ) -> tuple[
        tuple[TidePrediction, ...],
        list[tuple[int, int]],
        datetime,
        datetime,
        float,
        float,
    ]:
        window = build_tide_window(
            model.generated_at,
            model.config.station.timezone,
            chart_hours=model.config.chart_hours,
            minimum_range_hours=model.config.prediction_range_hours,
        )
        predictions = validate_tide_coverage(
            predictions,
            window,
            interval_minutes=model.config.prediction_interval_minutes,
        )
        visible = tuple(
            item
            for item in predictions
            if window.utc_start <= item.timestamp.astimezone(UTC) <= window.utc_end
        )
        if len(visible) < 2:
            raise ValueError("tide predictions do not cover the chart window")
        start = window.utc_start
        end = window.utc_end
        values = [item.value for item in visible]
        low = min(values)
        high = max(values)
        padding = max((high - low) * 0.12, 0.15)
        low -= padding
        high += padding
        span_seconds = max((end - start).total_seconds(), 1.0)
        span_value = max(high - low, 0.01)
        points = [
            (
                round(
                    self.chart.left
                    + (item.timestamp.astimezone(UTC) - start).total_seconds()
                    / span_seconds
                    * self.chart.width
                ),
                round(self.chart.bottom - (item.value - low) / span_value * self.chart.height),
            )
            for item in visible
        ]
        return visible, points, start, end, low, high

    def _draw_chart(
        self,
        draw: ImageDraw.ImageDraw,
        model: RenderModel,
        visible_predictions: tuple[TidePrediction, ...],
        points: list[tuple[int, int]],
        start: datetime,
        end: datetime,
        low: float,
        high: float,
    ) -> None:
        chart = self.chart
        draw.text(
            ((chart.left + chart.right) // 2, 48),
            f"Tide Predictions for\n{model.config.station.name}",
            font=self.title,
            fill=0,
            anchor="ma",
        )
        draw.rectangle((chart.left, chart.top, chart.right, chart.bottom), outline=0, width=2)

        self._draw_night_shading(draw, model, start, end)

        for index in range(5):
            y = round(chart.top + index * chart.height / 4)
            value = high - index * (high - low) / 4
            draw.line((chart.left, y, chart.right, y), fill=0, width=1)
            draw.text((chart.left - 7, y), f"{value:.1f} ft.", font=self.small_bold, fill=0, anchor="rm")

        total_seconds = max((end - start).total_seconds(), 1.0)
        for tick, local_tick in hourly_chart_ticks(
            start,
            end,
            model.config.station.timezone,
        ):
            elapsed = (tick - start).total_seconds()
            x = round(chart.left + elapsed / total_seconds * chart.width)
            if local_tick.hour % 6 == 0:
                draw.line((x, chart.top, x, chart.bottom), fill=0, width=1)
                label = remove_leading_hour_zero(local_tick.strftime("%I:%M %p"))
                if local_tick.hour in {0, 12}:
                    label = f"{local_tick:%b. %d}\n{label}"
                draw.multiline_text(
                    (x, chart.bottom + 5),
                    label,
                    font=self.small_bold,
                    fill=0,
                    anchor="ma",
                    align="center",
                )
            elif local_tick.hour % 2 == 0:
                draw.line((x, chart.bottom, x, chart.bottom + 5), fill=0, width=1)

        zero_y = round(chart.bottom - (0.0 - low) / max(high - low, 0.01) * chart.height)
        if chart.top <= zero_y <= chart.bottom:
            draw.line((chart.left, zero_y, chart.right, zero_y), fill=0, width=2)

        if len(points) > 1:
            draw.line(points, fill=0, width=2, joint="curve")
        generated_x = round(
            chart.left
            + (model.generated_at.astimezone(UTC) - start).total_seconds()
            / total_seconds
            * chart.width
        )
        two_hour_x = round(
            chart.left
            + (
                model.generated_at.astimezone(UTC) + timedelta(hours=2) - start
            ).total_seconds()
            / total_seconds
            * chart.width
        )
        highlighted = [point for point in points if generated_x <= point[0] <= two_hour_x]
        if len(highlighted) > 1:
            draw.line(highlighted, fill=0, width=9, joint="curve")

        point_values = [chart.bottom - y for _, y in points]
        peaks, valleys = local_extrema(point_values)
        for index in peaks:
            x, y = points[index]
            label = remove_leading_hour_zero(
                visible_predictions[index]
                .timestamp.astimezone(ZoneInfo(model.config.station.timezone))
                .strftime("%I:%M %p")
            )
            draw.text((x, y - 8), label, font=self.small_bold, fill=0, anchor="ms")
        for index in valleys:
            x, y = points[index]
            label = remove_leading_hour_zero(
                visible_predictions[index]
                .timestamp.astimezone(ZoneInfo(model.config.station.timezone))
                .strftime("%I:%M %p")
            )
            draw.text((x, y + 8), label, font=self.small_bold, fill=0, anchor="ma")

        for event in (model.solar.sunrise, model.solar.sunset):
            if event is not None and start <= event.astimezone(UTC) <= end:
                event_utc = event.astimezone(UTC)
                event_x = round(
                    chart.left
                    + (event_utc - start).total_seconds()
                    / total_seconds
                    * chart.width
                )
                label = remove_leading_hour_zero(
                    event.astimezone(ZoneInfo(model.config.station.timezone)).strftime(
                        "%I:%M %p"
                    )
                )
                draw.text(
                    (event_x, chart.top + 4),
                    label,
                    font=self.small_bold,
                    fill=0,
                    anchor="ma",
                )

        label_image = Image.new("1", (210, 38), 1)
        label_draw = ImageDraw.Draw(label_image)
        label_draw.multiline_text(
            (105, 19),
            "Tide Height (ft)\nAbove Chart Depth",
            font=self.small_bold,
            fill=0,
            anchor="mm",
            align="center",
        )
        vertical_label = label_image.transpose(Image.Transpose.ROTATE_90)
        label_y = (chart.top + chart.bottom - vertical_label.height) // 2
        draw._image.paste(vertical_label, (8, label_y))

    def _draw_night_shading(
        self,
        draw: ImageDraw.ImageDraw,
        model: RenderModel,
        start: datetime,
        end: datetime,
    ) -> None:
        days = model.solar_days or (model.solar,)
        rises = [item.sunrise for item in days if item.sunrise is not None]
        sets = [item.sunset for item in days if item.sunset is not None]
        intervals: list[tuple[datetime, datetime]] = []
        for sunset in sets:
            next_sunrise = next((sunrise for sunrise in rises if sunrise > sunset), None)
            if next_sunrise is not None:
                intervals.append((sunset, next_sunrise))
        total_seconds = max((end - start).total_seconds(), 1.0)
        for night_start, night_end in intervals:
            clipped_start = max(start, night_start)
            clipped_end = min(end, night_end)
            if clipped_start >= clipped_end:
                continue
            left = round(
                self.chart.left
                + (clipped_start - start).total_seconds() / total_seconds * self.chart.width
            )
            right = round(
                self.chart.left
                + (clipped_end - start).total_seconds() / total_seconds * self.chart.width
            )
            for x in range(left, right + 1, 4):
                draw.line((x, self.chart.top + 1, x, self.chart.bottom - 1), fill=0, width=1)

    def _draw_offline(self, image: Image.Image, draw: ImageDraw.ImageDraw) -> None:
        legacy = self.asset_dir / "no_wifi.bmp"
        if legacy.exists():
            with Image.open(legacy) as overlay:
                overlay = overlay.convert("1")
                x = (FRAME_WIDTH - overlay.width) // 2
                y = (FRAME_HEIGHT - overlay.height) // 2
                image.paste(overlay, (x, y))
            return
        box = (220, 185, 580, 295)
        draw.rectangle(box, fill=1, outline=0, width=4)
        draw.text((400, 220), "NO WI-FI", font=self.title, fill=0, anchor="mm")
        draw.text((400, 255), "Showing last available prediction", font=self.bold, fill=0, anchor="mm")


def framebuffer_to_image(framebuffer: bytes) -> Image.Image:
    """Convert controller-native bytes back to the user-visible panel orientation."""

    if len(framebuffer) != FRAME_BYTES:
        raise ValueError(f"expected {FRAME_BYTES} bytes")
    raw = bytes(value ^ 0xFF for value in framebuffer)
    controller = Image.frombytes("1", (FRAME_WIDTH, FRAME_HEIGHT), raw, "raw")
    return controller.transpose(Image.Transpose.ROTATE_180)
