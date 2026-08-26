"""Display adapters for the Waveshare 7.5-inch V2 panel and host simulations."""

from __future__ import annotations

import hashlib
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .deadlines import run_with_wall_clock_deadline

FRAME_WIDTH = 800
FRAME_HEIGHT = 480
FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT // 8


class EpaperDriver(Protocol):
    busy_timeout_seconds: float
    last_busy_seconds: float
    total_busy_seconds: float

    def init(self) -> int: ...

    def display(self, framebuffer: bytes) -> None: ...

    def sleep(self) -> None: ...


def framebuffer_sha256(framebuffer: bytes) -> str:
    if len(framebuffer) != FRAME_BYTES:
        raise ValueError(f"expected {FRAME_BYTES} framebuffer bytes, got {len(framebuffer)}")
    return hashlib.sha256(framebuffer).hexdigest()


class FileDisplay:
    """Host display adapter that atomically records controller-native bytes."""

    def __init__(self, output: Path) -> None:
        self.output = output
        self.refresh_count = 0
        self.last_busy_seconds = 0.0

    def display(self, framebuffer: bytes, *, timeout_seconds: float) -> None:
        del timeout_seconds
        framebuffer_sha256(framebuffer)
        self.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.output.with_name(f".{self.output.name}.{os.getpid()}.tmp")
        with temporary.open("wb") as target:
            target.write(framebuffer)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, self.output)
        self.refresh_count += 1


class WaveshareDisplay:
    """Bounded adapter around the checked-in Waveshare controller driver."""

    def __init__(
        self,
        *,
        library_path: Path | None = None,
        driver_factory: Callable[..., EpaperDriver] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if driver_factory is None:
            path = library_path or Path(
                os.environ.get("TIDETRACKER_EINK_LIB", "/usr/lib/tidetracker/e-ink_lib")
            )
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))
            try:
                from waveshare_epd.epd7in5_V2 import EPD
            except ImportError as exc:  # pragma: no cover - Raspberry Pi only
                raise RuntimeError(f"Waveshare driver is unavailable at {path}") from exc
            driver_factory = EPD
        self._driver_factory = driver_factory
        self._monotonic = monotonic
        self.refresh_count = 0
        self.last_busy_seconds = 0.0

    def display(self, framebuffer: bytes, *, timeout_seconds: float) -> None:
        framebuffer_sha256(framebuffer)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        deadline = self._monotonic() + timeout_seconds
        run_with_wall_clock_deadline(
            lambda: self._display_blocking(framebuffer, deadline),
            timeout_seconds=timeout_seconds,
            timeout_error=TimeoutError(
                "e-paper operation exceeded its wall-clock deadline"
            ),
        )

    def _display_blocking(self, framebuffer: bytes, deadline: float) -> None:

        def remaining() -> float:
            available = deadline - self._monotonic()
            if available <= 0:
                raise TimeoutError("e-paper operation exceeded its aggregate deadline")
            return available

        driver = self._driver_factory(busy_timeout_seconds=remaining())
        initialized = False
        deadline_interrupted = False
        try:
            driver.busy_timeout_seconds = remaining()
            if driver.init() != 0:
                raise RuntimeError("e-paper initialization failed")
            initialized = True
            driver.busy_timeout_seconds = remaining()
            driver.display(framebuffer)
            self.last_busy_seconds = float(
                getattr(
                    driver,
                    "total_busy_seconds",
                    getattr(driver, "last_busy_seconds", 0.0),
                )
            )
            self.refresh_count += 1
            driver.busy_timeout_seconds = remaining()
            driver.sleep()
            remaining()
            initialized = False
        except BaseException as exc:
            # Deadline control-flow derives directly from BaseException so an
            # earlier outer deadline cannot be swallowed by driver code. Never
            # start a fresh hardware cleanup after that deadline has fired.
            deadline_interrupted = not isinstance(exc, Exception) or isinstance(
                exc, TimeoutError
            )
            raise
        finally:
            if initialized and not deadline_interrupted:
                try:
                    from waveshare_epd import epdconfig

                    epdconfig.module_exit()
                except Exception:
                    pass
