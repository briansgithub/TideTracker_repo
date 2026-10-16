from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tidetracker.noaa import (
    FaultInjectingTransport,
    HttpResponse,
    NoaaFaultConfigurationError,
    NoaaTransportError,
    stage_noaa_fault_request,
)
from tidetracker.route import ProductionRouteError, SystemProductionRoute
from tidetracker.telemetry import CompositeTelemetry, UartPhaseTelemetry
from tidetracker.time_sync import (
    RootDateClockSetter,
    TrustedEpochStore,
    TrustedTimeAuthority,
)


@dataclass
class MutableClock:
    current: datetime
    tick: float = 10.0

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        self.tick += 0.01
        return self.tick


class UpdatingSetter:
    def __init__(self, clock: MutableClock) -> None:
        self.clock = clock
        self.calls: list[tuple[int, float]] = []

    def set_epoch(self, epoch: int, *, timeout_seconds: float) -> None:
        self.calls.append((epoch, timeout_seconds))
        self.clock.current = datetime.fromtimestamp(epoch, UTC)


def test_trusted_epoch_store_never_moves_backward(tmp_path: Path) -> None:
    store = TrustedEpochStore(tmp_path / "trusted-time.json")
    newer = datetime(2026, 8, 25, 12, tzinfo=UTC)

    assert store.persist(newer) == int(newer.timestamp())
    assert store.persist(newer - timedelta(days=2)) == int(newer.timestamp())
    assert store.load() == int(newer.timestamp())
    assert json.loads(store.path.read_text(encoding="utf-8"))["schema_version"] == 1


def test_restore_sets_only_an_implausibly_behind_clock(tmp_path: Path) -> None:
    trusted = datetime(2026, 8, 25, 12, tzinfo=UTC)
    store = TrustedEpochStore(tmp_path / "trusted-time.json")
    store.persist(trusted)
    clock = MutableClock(datetime(1970, 1, 1, tzinfo=UTC))
    setter = UpdatingSetter(clock)
    authority = TrustedTimeAuthority(store, clock, clock_setter=setter)

    assert authority.restore_before_network(timeout_seconds=2)
    assert setter.calls == [(int(trusted.timestamp()), 2)]
    assert not authority.restore_before_network(timeout_seconds=2)
    assert len(setter.calls) == 1


def test_timedatectl_wait_is_bounded_and_validates_wall_clock(tmp_path: Path) -> None:
    clock = MutableClock(datetime(2026, 8, 25, 12, tzinfo=UTC))
    answers = iter(["no\n", "yes\n"])
    calls: list[tuple[list[str], float]] = []

    def runner(
        args: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        argv = list(args)
        calls.append((argv, timeout))
        return subprocess.CompletedProcess(argv, 0, next(answers), "")

    authority = TrustedTimeAuthority(
        TrustedEpochStore(tmp_path / "trusted-time.json"),
        clock,
        runner=runner,
        sleep=lambda _seconds: None,
    )

    assert authority.wait_until_synchronized(timeout_seconds=3) == clock.current
    assert len(calls) == 2
    assert all(timeout <= 2 for _, timeout in calls)
    assert calls[0][0][-2:] == ["--property=NTPSynchronized", "--value"]


def test_root_clock_setter_uses_exact_non_shell_argv() -> None:
    captured: list[tuple[list[str], float]] = []

    def runner(
        args: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        argv = list(args)
        captured.append((argv, timeout))
        return subprocess.CompletedProcess(argv, 0, "", "")

    RootDateClockSetter(runner=runner).set_epoch(1_787_660_000, timeout_seconds=1.5)

    assert captured == [
        (["/usr/bin/date", "--utc", "--set=@1787660000"], 1.5)
    ]


def test_system_route_binds_dns_to_wlan_and_checks_every_address() -> None:
    calls: list[tuple[list[str], float]] = []

    def runner(
        args: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        argv = list(args)
        calls.append((argv, timeout))
        if "resolvectl" in argv[0]:
            output = (
                "api.tidesandcurrents.noaa.gov: 192.0.2.10 -- link: wlan0\n"
                "api.tidesandcurrents.noaa.gov: 2001:db8::10 -- link: wlan0\n"
            )
        else:
            output = '[{"dst":"example","dev":"wlan0"}]\n'
        return subprocess.CompletedProcess(argv, 0, output, "")

    route = SystemProductionRoute(runner=runner)

    assert route.interface_for_endpoint(timeout_seconds=8) == "wlan0"
    assert "--interface=wlan0" in calls[0][0]
    assert "--protocol=dns" in calls[0][0]
    route_calls = [args for args, _ in calls if "route" in args]
    assert [args[-1] for args in route_calls] == ["192.0.2.10", "2001:db8::10"]


def test_system_route_fails_closed_on_usb_route() -> None:
    def runner(
        args: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        argv = list(args)
        output = "192.0.2.10\n" if "resolvectl" in argv[0] else '[{"dev":"usb0"}]'
        return subprocess.CompletedProcess(argv, 0, output, "")

    with pytest.raises(ProductionRouteError, match="must use wlan0"):
        SystemProductionRoute(runner=runner).interface_for_endpoint(timeout_seconds=8)


class MemoryTransport:
    def __init__(self) -> None:
        self.calls = 0

    def get(
        self,
        url: str,
        *,
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
        max_response_bytes: int,
    ) -> HttpResponse:
        del url, connect_timeout_seconds, read_timeout_seconds, max_response_bytes
        self.calls += 1
        return HttpResponse(200, b"{}", {})


class MemoryEvents:
    def __init__(self) -> None:
        self.values: list[tuple[str, dict[str, object]]] = []

    def emit(self, event: str, **fields: object) -> None:
        self.values.append((event, fields))


def _fixture_uid(path: Path) -> int:
    return path.parent.stat().st_uid


@pytest.mark.parametrize("kind", ["dns", "tls", "http_503"])
def test_cold_fault_request_is_staged_then_consumed_once(
    tmp_path: Path, kind: str
) -> None:
    request = tmp_path / "state" / "hil" / "noaa-fault.request"
    request.parent.mkdir(parents=True)
    request.write_bytes(f"{kind}\n".encode("ascii"))
    request.chmod(0o600)
    runtime = tmp_path / "run" / "noaa-fault"
    uid = _fixture_uid(request)
    test_mode = request.stat().st_mode & 0o777

    assert (
        stage_noaa_fault_request(
            request, runtime, required_uid=uid, required_mode=test_mode
        )
        == kind
    )
    assert not request.exists()
    assert runtime.read_bytes() == f"{kind}\n".encode("ascii")

    events = MemoryEvents()
    underlying = MemoryTransport()
    transport = FaultInjectingTransport(
        underlying,
        runtime,
        telemetry=events,
        required_uid=uid,
        required_mode=runtime.stat().st_mode & 0o777,
    )
    if kind == "http_503":
        assert transport.get(
            "https://example.test",
            connect_timeout_seconds=1,
            read_timeout_seconds=1,
            max_response_bytes=10,
        ).status == 503
    else:
        with pytest.raises(NoaaTransportError, match=kind.upper()):
            transport.get(
                "https://example.test",
                connect_timeout_seconds=1,
                read_timeout_seconds=1,
                max_response_bytes=10,
            )
    assert not runtime.exists()
    assert transport.fault_kind == kind
    assert underlying.calls == 0
    assert events.values == [("noaa_fault_injected", {"fault_kind": kind})]

    assert transport.get(
        "https://example.test",
        connect_timeout_seconds=1,
        read_timeout_seconds=1,
        max_response_bytes=10,
    ).status == 200
    assert underlying.calls == 1


def test_fault_request_rejects_permissions_and_arbitrary_values(tmp_path: Path) -> None:
    request = tmp_path / "noaa-fault.request"
    runtime = tmp_path / "run" / "noaa-fault"
    request.write_bytes(b"shell-command\n")
    request.chmod(0o600)
    test_mode = request.stat().st_mode & 0o777
    with pytest.raises(NoaaFaultConfigurationError, match="not allowed"):
        stage_noaa_fault_request(
            request,
            runtime,
            required_uid=_fixture_uid(request),
            required_mode=test_mode,
        )

    if os.name == "nt":
        return
    request.write_bytes(b"tls\n")
    request.chmod(0o644)
    with pytest.raises(NoaaFaultConfigurationError, match="0600"):
        stage_noaa_fault_request(request, runtime, required_uid=_fixture_uid(request))


class MemoryWriter:
    def __init__(self) -> None:
        self.lines: list[bytes] = []
        self.closed = False

    def write_line(self, payload: bytes) -> None:
        self.lines.append(payload)

    def close(self) -> None:
        self.closed = True


def test_uart_phase_telemetry_is_bounded_ascii_and_secret_free() -> None:
    writer = MemoryWriter()
    uart = UartPhaseTelemetry(writer, monotonic_ns=lambda: 1234)

    uart.emit("cycle_started", run_id="ignored")
    uart.emit(
        "phase_started",
        run_id="run-password=hunter2",
        phase="FETCH",
        password="never-serialized",
    )
    uart.close()

    assert len(writer.lines) == 1
    assert len(writer.lines[0]) <= UartPhaseTelemetry.MAX_LINE_BYTES
    assert writer.lines[0].decode("ascii").endswith("\n")
    record = json.loads(writer.lines[0])
    assert record == {
        "event": "phase_started",
        "monotonic_ns": 1234,
        "phase": "FETCH",
        "run_id": "run-password=[REDACTED]",
        "schema_version": 1,
    }
    assert b"never-serialized" not in writer.lines[0]
    assert writer.closed


def test_uart_fixture_identity_and_update_boundary_are_strict() -> None:
    writer = MemoryWriter()
    uart = UartPhaseTelemetry(writer)
    digest = "a" * 64

    uart.emit(
        "target_identity",
        release_id="release-1",
        execution_source="verified-release",
        image_build_id=digest,
        key_policy_sha256=digest,
    )
    uart.emit(
        "update_phase_boundary",
        phase="verify",
        release_id="release-1",
        bundle_sha256=digest,
    )

    assert [json.loads(line)["event"] for line in writer.lines] == [
        "target_identity",
        "update_phase_boundary",
    ]
    assert all(len(line) <= UartPhaseTelemetry.MAX_LINE_BYTES for line in writer.lines)
    with pytest.raises(ValueError, match="build ID"):
        uart.emit(
            "target_identity",
            release_id="release-1",
            execution_source="verified-release",
            image_build_id="not-a-hash",
            key_policy_sha256=digest,
        )
    with pytest.raises(ValueError, match="key policy"):
        uart.emit(
            "target_identity",
            release_id="release-1",
            execution_source="verified-release",
            image_build_id=digest,
            key_policy_sha256="not-a-hash",
        )


def test_composite_telemetry_keeps_other_sink_alive_after_failure() -> None:
    class Broken:
        def emit(self, event: str, **fields: object) -> None:
            del event, fields
            raise OSError("UART absent")

    events = MemoryEvents()
    CompositeTelemetry(Broken(), events).emit("phase_started", run_id="run", phase="MODE")

    assert events.values == [("phase_started", {"run_id": "run", "phase": "MODE"})]
