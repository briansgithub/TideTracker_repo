from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from tidetracker.display import (
    FRAME_BYTES,
    FileDisplay,
    WaveshareDisplay,
    framebuffer_sha256,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 10.0

    def __call__(self) -> float:
        return self.now


class FakeDriver:
    def __init__(self, clock: FakeClock, busy_timeout_seconds: float) -> None:
        self.clock = clock
        self.busy_timeout_seconds = busy_timeout_seconds
        self.last_busy_seconds = 0.0
        self.total_busy_seconds = 1.25
        self.budgets: list[float] = []

    def init(self) -> int:
        self.budgets.append(self.busy_timeout_seconds)
        self.clock.now += 2.0
        return 0

    def display(self, framebuffer: bytes) -> None:
        del framebuffer
        self.budgets.append(self.busy_timeout_seconds)
        self.clock.now += 2.0

    def sleep(self) -> None:
        self.budgets.append(self.busy_timeout_seconds)
        self.clock.now += 0.5


def test_file_display_writes_exact_controller_frame(tmp_path: Path) -> None:
    frame = bytes([0xA5]) * FRAME_BYTES
    output = tmp_path / "frame.bin"

    display = FileDisplay(output)
    display.display(frame, timeout_seconds=1)

    assert output.read_bytes() == frame
    assert display.refresh_count == 1
    assert len(framebuffer_sha256(frame)) == 64


def test_frame_size_is_enforced() -> None:
    with pytest.raises(ValueError, match="expected 48000"):
        framebuffer_sha256(b"short")


def test_waveshare_display_rebudgets_one_aggregate_deadline() -> None:
    clock = FakeClock()
    created: list[FakeDriver] = []

    def factory(*, busy_timeout_seconds: float) -> FakeDriver:
        driver = FakeDriver(clock, busy_timeout_seconds)
        created.append(driver)
        return driver

    display = WaveshareDisplay(driver_factory=factory, monotonic=clock)
    display.display(bytes(FRAME_BYTES), timeout_seconds=5.0)

    assert created[0].budgets == pytest.approx([5.0, 3.0, 1.0])
    assert display.last_busy_seconds == pytest.approx(1.25)
    assert display.refresh_count == 1


def test_waveshare_display_rejects_elapsed_aggregate_deadline() -> None:
    clock = FakeClock()

    class SlowDriver(FakeDriver):
        def init(self) -> int:
            self.clock.now += 6.0
            return 0

    display = WaveshareDisplay(
        driver_factory=lambda **kwargs: SlowDriver(clock, **kwargs),
        monotonic=clock,
    )

    with pytest.raises(TimeoutError, match="aggregate deadline"):
        display.display(bytes(FRAME_BYTES), timeout_seconds=5.0)


def test_waveshare_display_bounds_blocked_spi_write_by_wall_clock() -> None:
    unwound = False

    class BlockedDriver:
        busy_timeout_seconds = 1.0
        last_busy_seconds = 0.0
        total_busy_seconds = 0.0

        def init(self) -> int:
            return 0

        def display(self, framebuffer: bytes) -> None:
            nonlocal unwound
            del framebuffer
            try:
                time.sleep(0.2)
            finally:
                unwound = True

        def sleep(self) -> None:
            return

    display = WaveshareDisplay(driver_factory=lambda **kwargs: BlockedDriver())
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="wall-clock deadline"):
        display.display(bytes(FRAME_BYTES), timeout_seconds=0.03)

    assert time.monotonic() - started < 0.15
    if os.name == "posix":
        assert unwound
