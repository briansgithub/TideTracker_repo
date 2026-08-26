from __future__ import annotations

import json
from zoneinfo import ZoneInfo

from tidetracker.noaa import parse_noaa_predictions


def test_gmt_predictions_preserve_both_fall_back_hours_without_ambiguity() -> None:
    body = json.dumps(
        {
            "predictions": [
                {"t": "2026-11-01 05:30", "v": "1.0"},
                {"t": "2026-11-01 06:30", "v": "1.2"},
            ]
        }
    ).encode()

    predictions = parse_noaa_predictions(body)
    first = predictions[0].timestamp.astimezone(ZoneInfo("America/New_York"))
    second = predictions[1].timestamp.astimezone(ZoneInfo("America/New_York"))

    assert (first.hour, first.minute, first.fold) == (1, 30, 0)
    assert (second.hour, second.minute, second.fold) == (1, 30, 1)
    assert first.utcoffset() != second.utcoffset()
