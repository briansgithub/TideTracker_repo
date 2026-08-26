from __future__ import annotations

import json
import time
from datetime import date
from urllib.parse import parse_qs, urlsplit

import pytest

from tidetracker.config import default_config
from tidetracker.noaa import (
    HttpResponse,
    NoaaClient,
    NoaaResponseError,
    NoaaTransportError,
    PredictionValidationError,
    UrlLibTransport,
    parse_noaa_predictions,
)
from tidetracker.solar import calculate_sun_times


class FakeTransport:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.calls: list[tuple[str, float, float, int]] = []

    def get(
        self,
        url: str,
        *,
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
        max_response_bytes: int,
    ) -> HttpResponse:
        self.calls.append((url, connect_timeout_seconds, read_timeout_seconds, max_response_bytes))
        return HttpResponse(self.status, self.body, {"content-type": "application/json"})


def response(*items: tuple[str, str]) -> bytes:
    return json.dumps({"predictions": [{"t": time, "v": value} for time, value in items]}).encode()


def test_noaa_client_uses_gmt_and_passes_strict_timeout_bounds() -> None:
    transport = FakeTransport(response(("2026-08-24 00:00", "1.0"), ("2026-08-24 00:05", "1.1")))
    monotonic_values = iter((10.0, 10.5))
    client = NoaaClient(transport, monotonic=lambda: next(monotonic_values))

    predictions = client.fetch(
        default_config().station,
        begin=date(2026, 8, 24),
        range_hours=60,
        interval_minutes=5,
        timeout_seconds=7.0,
    )

    assert len(predictions) == 2
    prediction_offset = predictions[0].timestamp.utcoffset()
    assert prediction_offset is not None
    assert prediction_offset.total_seconds() == 0
    url, connect_timeout, read_timeout, response_limit = transport.calls[0]
    query = parse_qs(urlsplit(url).query)
    assert query["time_zone"] == ["gmt"]
    assert query["station"] == ["8531833"]
    assert query["datum"] == ["MLLW"]
    assert connect_timeout == 5.0
    assert read_timeout == 7.0
    assert response_limit == 1_048_576


def test_noaa_client_rejects_elapsed_total_timeout() -> None:
    transport = FakeTransport(response(("2026-08-24 00:00", "1.0")))
    monotonic_values = iter((0.0, 8.0))
    client = NoaaClient(transport, monotonic=lambda: next(monotonic_values))

    with pytest.raises(NoaaTransportError, match="total timeout"):
        client.fetch(
            default_config().station,
            begin=date(2026, 8, 24),
            range_hours=1,
            interval_minutes=5,
            timeout_seconds=7.0,
        )


def test_urllib_transport_enforces_wall_clock_deadline_during_slow_trickle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowResponse:
        status = 200
        headers: dict[str, str] = {}

        def __init__(self) -> None:
            self.remaining = 4

        def __enter__(self) -> SlowResponse:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def read(self, amount: int) -> bytes:
            del amount
            time.sleep(0.05)
            if self.remaining == 0:
                return b""
            self.remaining -= 1
            return b"x"

    monkeypatch.setattr(
        "tidetracker.noaa.urlopen",
        lambda request, timeout: SlowResponse(),
    )
    started = time.monotonic()

    with pytest.raises(NoaaTransportError, match="wall-clock timeout"):
        UrlLibTransport().get(
            "https://example.invalid/noaa",
            connect_timeout_seconds=0.05,
            read_timeout_seconds=0.08,
            max_response_bytes=1024,
        )

    assert time.monotonic() - started < 0.3


@pytest.mark.parametrize(
    "body,error",
    [
        (b"not-json", NoaaResponseError),
        (json.dumps({"error": {"message": "bad station"}}).encode(), NoaaResponseError),
        (response(("2026-08-24 00:00", "nan")), NoaaResponseError),
        (
            response(("2026-08-24 00:05", "1"), ("2026-08-24 00:00", "2")),
            PredictionValidationError,
        ),
    ],
)
def test_noaa_validation_rejects_malformed_or_unsafe_data(
    body: bytes, error: type[Exception]
) -> None:
    with pytest.raises(error):
        parse_noaa_predictions(body)


def test_solar_times_use_requested_local_date_and_dst_offset() -> None:
    summer = calculate_sun_times(date(2026, 6, 21), 40.355, -74.065, "America/New_York")
    winter = calculate_sun_times(date(2026, 12, 21), 40.355, -74.065, "America/New_York")

    assert summer.sunrise is not None and summer.sunset is not None
    assert winter.sunrise is not None and winter.sunset is not None
    assert summer.sunrise.date() == summer.sunset.date() == date(2026, 6, 21)
    assert winter.sunrise.date() == winter.sunset.date() == date(2026, 12, 21)
    summer_offset = summer.sunrise.utcoffset()
    winter_offset = winter.sunrise.utcoffset()
    assert summer_offset is not None
    assert winter_offset is not None
    assert summer_offset.total_seconds() == -4 * 3600
    assert winter_offset.total_seconds() == -5 * 3600
    assert 4 <= summer.sunrise.hour <= 6
    assert 19 <= summer.sunset.hour <= 21
    assert 6 <= winter.sunrise.hour <= 8
    assert 15 <= winter.sunset.hour <= 18


def test_solar_times_represent_polar_day_without_fake_events() -> None:
    result = calculate_sun_times(date(2026, 6, 21), 89.0, 0.0, "UTC")

    assert result.sunrise is None
    assert result.sunset is None
