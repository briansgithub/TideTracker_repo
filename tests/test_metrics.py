from __future__ import annotations

from datetime import UTC, datetime

from tidetracker.metrics import ResourceSnapshot, cycle_metric_fields
from tidetracker.models import CycleMode, CyclePhase, CycleResult


def test_cycle_metrics_are_nonnegative_and_include_phase_proxy() -> None:
    now = datetime.now(UTC)
    result = CycleResult(
        run_id="run-1",
        success=True,
        mode=CycleMode.RUN,
        started_at=now,
        ended_at=now,
        phases=(CyclePhase.NETWORK, CyclePhase.FETCH, CyclePhase.DONE),
        phase_durations_seconds={"NETWORK": 2.0, "FETCH": 3.0},
    )
    before = ResourceSnapshot(10, 1, 2, 100, 10, 20, 30, 40)
    after = ResourceSnapshot(19, 3, 3, 110, 15, 28, 42, 48, 7)

    fields = cycle_metric_fields(
        result, before, after, display_busy_seconds=4.0, refresh_count=1
    )

    assert fields["wake_to_done_seconds"] == 9
    assert fields["wifi_active_seconds"] == 5
    assert fields["network_rx_bytes"] == 12
    assert fields["process_write_bytes"] == 8
    assert fields["display_refresh_count"] == 1
    assert fields["swap_used_kib"] == 7
