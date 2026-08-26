"""Deterministic benchmark summaries and promotion policy."""

from __future__ import annotations

import json
import math
import random
import statistics
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CycleMetrics:
    wake_to_done_ms: float
    wifi_active_ms: float
    cpu_ms: float
    max_rss_kib: int
    bytes_written: int
    refresh_count: int
    swap_used_kib: int = 0


@dataclass(frozen=True, slots=True)
class MetricComparison:
    baseline_median: float
    candidate_median: float
    improvement: float
    improvement_percent: float
    confidence_low: float
    confidence_high: float


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    accepted: bool
    reasons: tuple[str, ...]
    wake_to_done: MetricComparison
    wifi_active: MetricComparison

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


def _bootstrap_improvement(
    baseline: tuple[float, ...],
    candidate: tuple[float, ...],
    *,
    samples: int = 10_000,
    seed: int = 0x715E,
) -> MetricComparison:
    if len(baseline) < 2 or len(candidate) < 2:
        raise ValueError("at least two observations are required per variant")
    generator = random.Random(seed)
    differences: list[float] = []
    for _ in range(samples):
        base_sample = [generator.choice(baseline) for _ in baseline]
        candidate_sample = [generator.choice(candidate) for _ in candidate]
        differences.append(statistics.median(base_sample) - statistics.median(candidate_sample))
    differences.sort()
    lower = differences[math.floor(samples * 0.025)]
    upper = differences[min(math.ceil(samples * 0.975), samples - 1)]
    base_median = statistics.median(baseline)
    candidate_median = statistics.median(candidate)
    improvement = base_median - candidate_median
    return MetricComparison(
        baseline_median=base_median,
        candidate_median=candidate_median,
        improvement=improvement,
        improvement_percent=(improvement / base_median * 100.0) if base_median else 0.0,
        confidence_low=lower,
        confidence_high=upper,
    )


def evaluate_promotion(
    baseline: Iterable[CycleMetrics],
    candidate: Iterable[CycleMetrics],
    *,
    minimum_cycles: int = 20,
) -> PromotionDecision:
    base = tuple(baseline)
    trial = tuple(candidate)
    reasons: list[str] = []
    if len(base) < minimum_cycles or len(trial) < minimum_cycles:
        raise ValueError(f"promotion requires at least {minimum_cycles} cycles per variant")
    wake = _bootstrap_improvement(
        tuple(item.wake_to_done_ms for item in base),
        tuple(item.wake_to_done_ms for item in trial),
    )
    wifi = _bootstrap_improvement(
        tuple(item.wifi_active_ms for item in base),
        tuple(item.wifi_active_ms for item in trial),
        seed=0x715F,
    )
    practical_wake = max(wake.baseline_median * 0.02, 250.0)
    practical_wifi = max(wifi.baseline_median * 0.02, 250.0)
    significant = (wake.confidence_low > 0 and wake.improvement >= practical_wake) or (
        wifi.confidence_low > 0 and wifi.improvement >= practical_wifi
    )
    if not significant:
        reasons.append("no primary metric has a statistically significant practical improvement")

    def p95(items: tuple[float, ...]) -> float:
        return statistics.quantiles(items, n=100, method="inclusive")[94]

    for name, base_values, trial_values in (
        ("wake_to_done", tuple(x.wake_to_done_ms for x in base), tuple(x.wake_to_done_ms for x in trial)),
        ("wifi_active", tuple(x.wifi_active_ms for x in base), tuple(x.wifi_active_ms for x in trial)),
    ):
        if p95(trial_values) > p95(base_values) * 1.05:
            reasons.append(f"{name} p95 regressed by more than 5%")
    if max(item.max_rss_kib for item in trial) >= 128 * 1024:
        reasons.append("candidate maximum RSS is not below 128 MiB")
    if any(item.swap_used_kib > 0 for item in trial):
        reasons.append("candidate used swap")
    if max(item.refresh_count for item in trial) > max(item.refresh_count for item in base):
        reasons.append("candidate added a physical display refresh")
    return PromotionDecision(not reasons, tuple(reasons), wake, wifi)


def load_metrics(path: Path) -> tuple[CycleMetrics, ...]:
    values: list[CycleMetrics] = []
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            if line.strip():
                values.append(CycleMetrics(**json.loads(line)))
    return tuple(values)
