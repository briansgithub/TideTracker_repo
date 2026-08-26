"""Lazy Raspberry Pi GPIO adapters.

Nothing in this module imports a GPIO implementation until an adapter is constructed,
so host-side tests can import the full application package without Raspberry Pi modules.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .models import CycleMode


@dataclass(frozen=True, slots=True)
class BoardProfile:
    mode_bcm: int = 16
    done_bcm: int = 26
    display_reset_bcm: int = 17
    display_dc_bcm: int = 25
    display_cs_bcm: int = 8
    display_busy_bcm: int = 24
    display_power_bcm: int = 18
    spi_mosi_bcm: int = 10
    spi_clock_bcm: int = 11


ZERO_W_PROFILE = BoardProfile()


def _no_additional_sync() -> None:
    return


class InputPin(Protocol):
    @property
    def value(self) -> float: ...

    def close(self) -> None: ...


class OutputPin(Protocol):
    def on(self) -> None: ...

    def off(self) -> None: ...

    def close(self) -> None: ...


class GpioModeReader:
    """Short-lived BCM16 reader used to choose a release-trial power budget."""

    def __init__(
        self,
        profile: BoardProfile = ZERO_W_PROFILE,
        *,
        mode_pin: InputPin | None = None,
    ) -> None:
        if mode_pin is None:
            try:
                from gpiozero import Button
            except ImportError as exc:  # pragma: no cover - Raspberry Pi only
                raise RuntimeError("install gpiozero and an lgpio pin factory") from exc
            mode_pin = Button(profile.mode_bcm, pull_up=False)
        self._mode_pin = mode_pin

    def read_mode(self) -> CycleMode:
        return CycleMode.SETUP if bool(self._mode_pin.value) else CycleMode.RUN

    def close(self) -> None:
        self._mode_pin.close()

    def __enter__(self) -> GpioModeReader:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class GpioModeDone:
    """Mode and TPL5110 DONE adapter with a one-shot completion invariant."""

    def __init__(
        self,
        profile: BoardProfile = ZERO_W_PROFILE,
        *,
        mode_pin: InputPin | None = None,
        done_pin: OutputPin | None = None,
        pulse_seconds: float = 0.500,
        settle_seconds: float = 0.500,
        done_marker: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
        sync: Callable[[], None] = _no_additional_sync,
    ) -> None:
        if pulse_seconds < 0.010:
            raise ValueError("DONE pulse must be at least 10 ms")
        if settle_seconds < 0:
            raise ValueError("DONE settle time cannot be negative")
        if mode_pin is None or done_pin is None:
            try:
                from gpiozero import Button, DigitalOutputDevice
            except ImportError as exc:  # pragma: no cover - exercised only on Raspberry Pi
                raise RuntimeError("install gpiozero and an lgpio pin factory on Raspberry Pi") from exc
            mode_pin = mode_pin or Button(profile.mode_bcm, pull_up=False)
            done_pin = done_pin or DigitalOutputDevice(profile.done_bcm, initial_value=False)
        self._mode_pin = mode_pin
        self._done_pin = done_pin
        self._pulse_seconds = pulse_seconds
        self._settle_seconds = settle_seconds
        self._done_marker = done_marker
        self._sleep = sleep
        self._sync = sync
        self._done_signalled = False

    def read_mode(self) -> CycleMode:
        return CycleMode.SETUP if bool(self._mode_pin.value) else CycleMode.RUN

    def signal_done(self) -> None:
        if self._done_signalled:
            raise RuntimeError("DONE may only be signalled once per boot")
        # Cycle persistence already performs a bounded durability barrier. A
        # second raw system sync here could prevent the safety-critical edge.
        with suppress(Exception):
            self._sync()
        self._done_pin.off()
        self._sleep(self._settle_seconds)
        self._done_pin.on()
        self._sleep(self._pulse_seconds)
        self._done_pin.off()
        self._done_signalled = True
        # This marker is RAM-backed and only suppresses a duplicate OnFailure
        # pulse. Write it after the physical edge so marker I/O is never on the
        # safety-critical path.
        if self._done_marker is not None:
            with suppress(OSError):
                self._done_marker.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
                marker_candidate = self._done_marker.with_name(
                    f".{self._done_marker.name}.{os.getpid()}.tmp"
                )
                descriptor = os.open(
                    marker_candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as marker:
                    marker.write(b"signalled\n")
                os.replace(marker_candidate, self._done_marker)

    def close(self) -> None:
        self._mode_pin.close()
        self._done_pin.close()

    def __enter__(self) -> GpioModeDone:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
