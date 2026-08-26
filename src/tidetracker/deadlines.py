"""Wall-clock deadlines that do not strand production work in daemon threads."""

from __future__ import annotations

import math
import os
import queue
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import FrameType
from typing import Any, Generic, TypeVar, cast

T = TypeVar("T")


class DeadlineUnavailableError(RuntimeError):
    """Raised before work starts when a production deadline cannot be enforced."""


class _DeadlineSignal(BaseException):
    """Carry a deadline through nested application exception handlers."""

    def __init__(self, owner: object, error: BaseException) -> None:
        super().__init__()
        self.owner = owner
        self.error = error


@dataclass(slots=True)
class _Outcome(Generic[T]):
    value: T | None = None
    error: BaseException | None = None


_SIGALRM = getattr(signal, "SIGALRM", None)
_ITIMER_REAL = getattr(signal, "ITIMER_REAL", None)
_GETITIMER = getattr(signal, "getitimer", None)
_SETITIMER = getattr(signal, "setitimer", None)


def _has_posix_alarm() -> bool:
    return (
        os.name == "posix"
        and _SIGALRM is not None
        and _ITIMER_REAL is not None
        and callable(_GETITIMER)
        and callable(_SETITIMER)
    )


class _PosixDeadline:
    """Temporarily install one nestable ITIMER_REAL deadline on the main thread."""

    _MINIMUM_DELAY_SECONDS = 0.000_001

    def __init__(self, timeout_seconds: float, timeout_error: BaseException) -> None:
        self._timeout_seconds = timeout_seconds
        self._timeout_error = timeout_error
        self._started = 0.0
        self._deadline = 0.0
        self._previous_handler: object = signal.SIG_DFL
        self._previous_deadline: float | None = None
        self._previous_interval = 0.0
        self._armed_for_previous = False
        self._entered = False

    def __enter__(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            raise DeadlineUnavailableError(
                "POSIX wall-clock deadlines require the process main thread"
            )
        assert _SIGALRM is not None
        assert _ITIMER_REAL is not None
        assert callable(_GETITIMER)
        assert callable(_SETITIMER)

        self._started = time.monotonic()
        self._deadline = self._started + self._timeout_seconds
        self._previous_handler = signal.getsignal(_SIGALRM)
        previous_delay, self._previous_interval = _GETITIMER(_ITIMER_REAL)
        if previous_delay > 0:
            if self._previous_handler == signal.SIG_DFL:
                raise DeadlineUnavailableError(
                    "cannot safely nest beneath a default-handler POSIX alarm"
                )
            self._previous_deadline = self._started + previous_delay
        signal.signal(_SIGALRM, self._handle_alarm)
        self._entered = True
        try:
            self._arm_next()
        except BaseException:
            self._restore_previous()
            raise

    def _arm_next(self) -> None:
        assert _ITIMER_REAL is not None
        assert callable(_SETITIMER)
        now = time.monotonic()
        self._armed_for_previous = (
            self._previous_deadline is not None
            and self._previous_deadline <= self._deadline
        )
        next_deadline = (
            self._previous_deadline if self._armed_for_previous else self._deadline
        )
        assert next_deadline is not None
        delay = max(next_deadline - now, self._MINIMUM_DELAY_SECONDS)
        _SETITIMER(_ITIMER_REAL, delay, 0.0)

    def _handle_alarm(self, signum: int, frame: FrameType | None) -> None:
        if not self._armed_for_previous:
            raise _DeadlineSignal(self, self._timeout_error)

        previous_handler = self._previous_handler
        fired_at = self._previous_deadline
        if self._previous_interval > 0 and fired_at is not None:
            now = time.monotonic()
            missed = max(0, math.floor((now - fired_at) / self._previous_interval))
            self._previous_deadline = fired_at + (missed + 1) * self._previous_interval
        else:
            self._previous_deadline = None

        if callable(previous_handler):
            previous_handler(signum, frame)
        elif previous_handler != signal.SIG_IGN:
            raise DeadlineUnavailableError("saved POSIX alarm handler is not callable")

        # A prior handler is allowed to return (including SIG_IGN). Continue to
        # enforce this deadline, and preserve any repeating prior timer cadence.
        self._arm_next()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        del exc_type, exc, traceback
        if not self._entered:
            return

        self._restore_previous()

    def _restore_previous(self) -> None:
        assert _SIGALRM is not None
        assert _ITIMER_REAL is not None
        assert callable(_SETITIMER)

        _SETITIMER(_ITIMER_REAL, 0.0, 0.0)
        signal.signal(_SIGALRM, cast(Any, self._previous_handler))
        self._entered = False
        if self._previous_deadline is None:
            return
        remaining = max(
            self._previous_deadline - time.monotonic(),
            self._MINIMUM_DELAY_SECONDS,
        )
        _SETITIMER(_ITIMER_REAL, remaining, self._previous_interval)


def _run_in_development_thread(
    operation: Callable[[], T],
    *,
    timeout_seconds: float,
    timeout_error: BaseException,
) -> T:
    """Non-POSIX development fallback; production images never use this path."""

    outcomes: queue.Queue[_Outcome[T]] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            outcome = _Outcome(value=operation())
        except BaseException as exc:
            outcome = _Outcome[T](error=exc)
        try:
            outcomes.put_nowait(outcome)
        except queue.Full:
            return

    threading.Thread(
        target=worker,
        name="tidetracker-development-deadline",
        daemon=True,
    ).start()
    try:
        outcome = outcomes.get(timeout=timeout_seconds)
    except queue.Empty as exc:
        raise timeout_error from exc
    if outcome.error is not None:
        raise outcome.error
    return cast(T, outcome.value)


def run_with_wall_clock_deadline(
    operation: Callable[[], T],
    *,
    timeout_seconds: float,
    timeout_error: BaseException,
) -> T:
    """Run one operation under a hard caller-visible wall-clock deadline.

    Raspberry Pi/Linux execution is synchronous in the process main thread and
    uses ``ITIMER_REAL``. Therefore a timeout unwinds the operation before this
    function returns; no worker can continue toward a later DONE/power cut.
    Windows lacks this timer and uses a development-only daemon fallback so host
    tests still retain a bounded caller deadline.
    """

    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
    ):
        raise ValueError("wall-clock timeout must be finite and positive")
    if not isinstance(timeout_error, BaseException):
        raise TypeError("timeout_error must be an exception instance")
    if _has_posix_alarm():
        deadline = _PosixDeadline(float(timeout_seconds), timeout_error)
        try:
            with deadline:
                return operation()
        except _DeadlineSignal as exc:
            if exc.owner is deadline:
                raise exc.error from None
            raise
    return _run_in_development_thread(
        operation,
        timeout_seconds=float(timeout_seconds),
        timeout_error=timeout_error,
    )
