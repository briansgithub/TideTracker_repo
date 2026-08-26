from __future__ import annotations

import pytest

from tidetracker.benchmark import CycleMetrics, evaluate_promotion


def metrics(
    wake: float,
    wifi: float,
    *,
    refresh: int = 1,
    swap_kib: int = 0,
) -> list[CycleMetrics]:
    return [
        CycleMetrics(
            wake_to_done_ms=wake + (index % 3),
            wifi_active_ms=wifi + (index % 3),
            cpu_ms=2_000,
            max_rss_kib=64 * 1024,
            bytes_written=1024,
            refresh_count=refresh,
            swap_used_kib=swap_kib,
        )
        for index in range(20)
    ]


def test_accepts_significant_practical_improvement() -> None:
    decision = evaluate_promotion(metrics(20_000, 8_000), metrics(18_000, 7_000))

    assert decision.accepted
    assert decision.wake_to_done.improvement > 1_000


def test_rejects_extra_refresh() -> None:
    decision = evaluate_promotion(metrics(20_000, 8_000), metrics(18_000, 7_000, refresh=2))

    assert not decision.accepted
    assert "candidate added a physical display refresh" in decision.reasons


def test_requires_twenty_cycles() -> None:
    with pytest.raises(ValueError, match="at least 20"):
        evaluate_promotion(metrics(20_000, 8_000)[:2], metrics(18_000, 7_000)[:2])


def test_rejects_any_candidate_swap_use() -> None:
    decision = evaluate_promotion(
        metrics(20_000, 8_000),
        metrics(18_000, 7_000, swap_kib=1),
    )

    assert not decision.accepted
    assert "candidate used swap" in decision.reasons
