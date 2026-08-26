from __future__ import annotations

import pytest

from tidetracker.hardware import GpioModeDone, GpioModeReader
from tidetracker.models import CycleMode


class Input:
    def __init__(self, value: int) -> None:
        self.value = value
        self.closed = False

    def close(self) -> None:
        self.closed = True


class Output:
    def __init__(self) -> None:
        self.events: list[str] = []

    def on(self) -> None:
        self.events.append("on")

    def off(self) -> None:
        self.events.append("off")

    def close(self) -> None:
        self.events.append("close")


def test_reads_modes_and_signals_done_once(tmp_path) -> None:
    mode = Input(0)
    done = Output()
    sleeps: list[float] = []
    syncs: list[bool] = []
    marker = tmp_path / "run" / "done-signalled"
    adapter = GpioModeDone(
        mode_pin=mode,
        done_pin=done,
        done_marker=marker,
        sleep=sleeps.append,
        sync=lambda: syncs.append(True),
    )

    assert adapter.read_mode() is CycleMode.RUN
    mode.value = 1
    assert adapter.read_mode() is CycleMode.SETUP

    adapter.signal_done()

    assert syncs == [True]
    assert done.events == ["off", "on", "off"]
    assert sleeps == [0.5, 0.5]
    assert marker.read_text(encoding="ascii") == "signalled\n"
    with pytest.raises(RuntimeError, match="only be signalled once"):
        adapter.signal_done()


def test_rejects_unsafe_done_pulse() -> None:
    with pytest.raises(ValueError, match="at least 10 ms"):
        GpioModeDone(mode_pin=Input(0), done_pin=Output(), pulse_seconds=0.001)


def test_short_lived_mode_reader_does_not_claim_done_pin() -> None:
    mode = Input(1)

    with GpioModeReader(mode_pin=mode) as reader:
        assert reader.read_mode() is CycleMode.SETUP

    assert mode.closed


def test_failed_pre_pulse_sync_hook_cannot_suppress_done_edge() -> None:
    done = Output()

    def failed_sync() -> None:
        raise OSError("injected")

    GpioModeDone(
        mode_pin=Input(0),
        done_pin=done,
        sleep=lambda _: None,
        sync=failed_sync,
    ).signal_done()

    assert done.events == ["off", "on", "off"]
