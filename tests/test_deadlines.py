from __future__ import annotations

import math
import os
import signal
import threading
import time

import pytest

from tidetracker.deadlines import (
    DeadlineUnavailableError,
    run_with_wall_clock_deadline,
)


class InnerDeadline(TimeoutError):
    pass


class OuterDeadline(TimeoutError):
    pass


@pytest.mark.parametrize("timeout", [0, -1, math.inf, math.nan, True])
def test_deadline_rejects_invalid_bounds(timeout: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        run_with_wall_clock_deadline(
            lambda: None,
            timeout_seconds=timeout,
            timeout_error=TimeoutError("bounded"),
        )


def test_deadline_returns_values_and_preserves_operation_errors() -> None:
    assert (
        run_with_wall_clock_deadline(
            lambda: "complete",
            timeout_seconds=1,
            timeout_error=TimeoutError("bounded"),
        )
        == "complete"
    )

    expected = RuntimeError("operation failed")

    def fail() -> None:
        raise expected

    with pytest.raises(RuntimeError) as raised:
        run_with_wall_clock_deadline(
            fail,
            timeout_seconds=1,
            timeout_error=TimeoutError("bounded"),
        )
    assert raised.value is expected


@pytest.mark.skipif(os.name != "posix", reason="POSIX ITIMER_REAL contract")
def test_posix_timeout_unwinds_inline_before_returning() -> None:
    caller_ident = threading.get_ident()
    operation_ident: int | None = None
    unwound = threading.Event()

    def blocked() -> None:
        nonlocal operation_ident
        operation_ident = threading.get_ident()
        try:
            time.sleep(1)
        finally:
            unwound.set()

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="inline deadline"):
        run_with_wall_clock_deadline(
            blocked,
            timeout_seconds=0.03,
            timeout_error=TimeoutError("inline deadline"),
        )

    assert time.monotonic() - started < 0.3
    assert operation_ident == caller_ident
    assert unwound.is_set()
    assert not any(
        thread.name == "tidetracker-development-deadline"
        for thread in threading.enumerate()
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX ITIMER_REAL contract")
def test_nested_deadline_preserves_the_earlier_outer_deadline() -> None:
    def nested() -> None:
        try:
            run_with_wall_clock_deadline(
                lambda: time.sleep(1),
                timeout_seconds=0.4,
                timeout_error=InnerDeadline("inner deadline"),
            )
        except Exception as exc:
            raise AssertionError("outer deadline was exposed to inner handlers") from exc

    started = time.monotonic()
    with pytest.raises(OuterDeadline, match="outer deadline"):
        run_with_wall_clock_deadline(
            nested,
            timeout_seconds=0.04,
            timeout_error=OuterDeadline("outer deadline"),
        )

    assert time.monotonic() - started < 0.3


@pytest.mark.skipif(os.name != "posix", reason="POSIX ITIMER_REAL contract")
def test_nested_inner_timeout_restores_outer_remaining_budget() -> None:
    def operation() -> None:
        with pytest.raises(InnerDeadline, match="inner deadline"):
            run_with_wall_clock_deadline(
                lambda: time.sleep(1),
                timeout_seconds=0.05,
                timeout_error=InnerDeadline("inner deadline"),
            )
        time.sleep(1)

    started = time.monotonic()
    with pytest.raises(OuterDeadline, match="outer deadline"):
        run_with_wall_clock_deadline(
            operation,
            timeout_seconds=0.2,
            timeout_error=OuterDeadline("outer deadline"),
        )

    elapsed = time.monotonic() - started
    assert 0.15 <= elapsed < 0.3


@pytest.mark.skipif(os.name != "posix", reason="POSIX ITIMER_REAL contract")
def test_posix_deadline_restores_preexisting_handler_and_timer() -> None:
    sigalrm = signal.SIGALRM
    timer = signal.ITIMER_REAL
    previous_handler = signal.getsignal(sigalrm)
    previous_timer = signal.getitimer(timer)

    def prior_handler(signum: int, frame: object) -> None:
        del signum, frame

    signal.signal(sigalrm, prior_handler)
    signal.setitimer(timer, 0.5, 0.0)
    try:
        run_with_wall_clock_deadline(
            lambda: time.sleep(0.03),
            timeout_seconds=0.2,
            timeout_error=TimeoutError("inner deadline"),
        )
        restored_delay, restored_interval = signal.getitimer(timer)
        assert signal.getsignal(sigalrm) is prior_handler
        assert 0.3 < restored_delay < 0.5
        assert restored_interval == 0
    finally:
        signal.setitimer(timer, 0.0, 0.0)
        signal.signal(sigalrm, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(timer, *previous_timer)


@pytest.mark.skipif(os.name != "posix", reason="POSIX ITIMER_REAL contract")
def test_posix_non_main_thread_fails_before_starting_operation() -> None:
    called = False
    errors: list[BaseException] = []

    def worker() -> None:
        nonlocal called

        def operation() -> None:
            nonlocal called
            called = True

        try:
            run_with_wall_clock_deadline(
                operation,
                timeout_seconds=1,
                timeout_error=TimeoutError("deadline"),
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert not called
    assert len(errors) == 1
    assert isinstance(errors[0], DeadlineUnavailableError)
