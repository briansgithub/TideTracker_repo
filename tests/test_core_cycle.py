from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta

import pytest

from tidetracker.config import default_config
from tidetracker.cycle import CycleRunner
from tidetracker.display import FRAME_BYTES
from tidetracker.models import (
    CycleMode,
    CyclePhase,
    NetworkStatus,
    RenderModel,
    SolarTimes,
    StationConfig,
    StoredCycle,
    TidePrediction,
)
from tidetracker.noaa import NoaaTransportError
from tidetracker.storage import MemoryStateStore
from tidetracker.telemetry import MemoryTelemetry

DEFAULT_FRAME = bytes(FRAME_BYTES)


@dataclass
class FixedClock:
    current: datetime = datetime(2026, 8, 25, 12, tzinfo=UTC)
    ticks: float = 100.0

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        self.ticks += 0.01
        return self.ticks


@dataclass
class Mode:
    value: CycleMode = CycleMode.RUN

    def read_mode(self) -> CycleMode:
        return self.value


@dataclass
class Network:
    connected: bool = True
    error: Exception | None = None
    calls: list[tuple[CycleMode, float]] = field(default_factory=list)
    release_error: Exception | None = None
    release_calls: list[float] = field(default_factory=list)

    def prepare(self, mode: CycleMode, *, timeout_seconds: float) -> NetworkStatus:
        self.calls.append((mode, timeout_seconds))
        if self.error:
            raise self.error
        return NetworkStatus(self.connected, "wlan0" if self.connected else None)

    def release(self, *, timeout_seconds: float) -> None:
        self.release_calls.append(timeout_seconds)
        if self.release_error is not None:
            raise self.release_error


@dataclass
class TimeSync:
    synchronized: bool = True
    persisted: list[datetime] = field(default_factory=list)

    def restore_before_network(self, *, timeout_seconds: float) -> bool:
        assert timeout_seconds > 0
        return False

    def wait_until_synchronized(self, *, timeout_seconds: float) -> datetime:
        assert timeout_seconds > 0
        if not self.synchronized:
            raise TimeoutError("NTP unavailable")
        return datetime(2026, 8, 25, 12, tzinfo=UTC)

    def persist_trusted(self, value: datetime) -> int:
        self.persisted.append(value)
        return int(value.timestamp())


@dataclass
class Route:
    interface: str = "wlan0"
    error: Exception | None = None
    calls: list[float] = field(default_factory=list)

    def interface_for_endpoint(self, *, timeout_seconds: float) -> str:
        self.calls.append(timeout_seconds)
        if self.error is not None:
            raise self.error
        return self.interface


def predictions() -> tuple[TidePrediction, ...]:
    start = datetime(2026, 8, 24, tzinfo=UTC)
    return tuple(
        TidePrediction(start + timedelta(minutes=5 * index), float(index % 12) / 10)
        for index in range(865)
    )


@dataclass
class PredictionSource:
    values: tuple[TidePrediction, ...] = field(default_factory=predictions)
    error: Exception | None = None
    fault_kind: str | None = None
    calls: int = 0

    def fetch(
        self,
        station: StationConfig,
        *,
        begin: date,
        range_hours: int,
        interval_minutes: int,
        timeout_seconds: float,
    ) -> tuple[TidePrediction, ...]:
        del station, begin, range_hours, interval_minutes, timeout_seconds
        self.calls += 1
        if self.error:
            raise self.error
        return self.values


@dataclass
class Renderer:
    framebuffer: bytes = DEFAULT_FRAME
    error: Exception | None = None
    models: list[RenderModel] = field(default_factory=list)

    def render(self, model: RenderModel, *, timeout_seconds: float) -> bytes:
        assert timeout_seconds > 0
        self.models.append(model)
        if self.error:
            raise self.error
        return self.framebuffer


@dataclass
class Display:
    error: Exception | None = None
    frames: list[bytes] = field(default_factory=list)

    def display(self, framebuffer: bytes, *, timeout_seconds: float) -> None:
        assert timeout_seconds > 0
        self.frames.append(framebuffer)
        if self.error:
            raise self.error


@dataclass
class Done:
    calls: int = 0
    error: Exception | None = None

    def signal_done(self) -> None:
        self.calls += 1
        if self.error:
            raise self.error


def runner(
    *,
    mode: Mode | None = None,
    network: Network | None = None,
    source: PredictionSource | None = None,
    renderer: Renderer | None = None,
    display: Display | None = None,
    done: Done | None = None,
    store: MemoryStateStore | None = None,
) -> tuple[CycleRunner, Renderer, Display, Done, MemoryStateStore]:
    renderer = renderer or Renderer()
    display = display or Display()
    done = done or Done()
    store = store or MemoryStateStore()
    return (
        CycleRunner(
            default_config(),
            clock=FixedClock(),
            mode_port=mode or Mode(),
            network=network or Network(),
            predictions=source or PredictionSource(),
            renderer=renderer,
            display=display,
            done=done,
            store=store,
            telemetry=MemoryTelemetry(),
            run_id_factory=lambda: "deterministic-run",
        ),
        renderer,
        display,
        done,
        store,
    )


def test_successful_cycle_runs_every_phase_and_signals_done_once() -> None:
    cycle, _, display, done, store = runner()

    result = cycle.run()

    assert result.success
    assert result.phases == tuple(CyclePhase)
    assert done.calls == 1
    assert display.frames == [DEFAULT_FRAME]
    assert store.sync_count == 1
    assert store.last_good is not None
    assert result.framebuffer_sha256 == hashlib.sha256(DEFAULT_FRAME).hexdigest()


def test_failed_render_shortcuts_to_persist_and_done_exactly_once() -> None:
    cycle, _, display, done, store = runner(renderer=Renderer(error=RuntimeError("boom")))

    result = cycle.run()

    assert not result.success
    assert result.error_phase is CyclePhase.RENDER
    assert result.phases == (
        CyclePhase.MODE,
        CyclePhase.NETWORK,
        CyclePhase.FETCH,
        CyclePhase.VALIDATE,
        CyclePhase.COMPUTE,
        CyclePhase.RENDER,
        CyclePhase.PERSIST,
        CyclePhase.DONE,
    )
    assert display.frames == []
    assert done.calls == 1
    assert len(store.results) == 1


def test_short_framebuffer_fails_before_display_or_persistence() -> None:
    cycle, _, display, done, store = runner(renderer=Renderer(framebuffer=b"short"))

    result = cycle.run()

    assert not result.success
    assert result.error_phase is CyclePhase.RENDER
    assert result.error_type == "InvalidFramebuffer"
    assert display.frames == []
    assert store.last_good is None
    assert done.calls == 1


def test_offline_cycle_uses_matching_last_good_data() -> None:
    framebuffer = bytes([0xAA]) * FRAME_BYTES
    old_frame = bytes([0x55]) * FRAME_BYTES
    snapshot = StoredCycle(
        station_id="8531833",
        generated_at=datetime(2026, 8, 24, tzinfo=UTC),
        predictions=predictions(),
        framebuffer=old_frame,
        framebuffer_sha256=hashlib.sha256(old_frame).hexdigest(),
    )
    store = MemoryStateStore(snapshot)
    cycle, renderer, display, done, _ = runner(
        network=Network(connected=False),
        renderer=Renderer(framebuffer=framebuffer),
        store=store,
    )

    result = cycle.run()

    assert result.success
    assert result.used_cached_data
    assert renderer.models[0].offline
    assert renderer.models[0].previous_framebuffer == old_frame
    assert display.frames == [framebuffer]
    assert done.calls == 1


def test_no_network_and_no_cache_fails_but_still_persists_and_signals_done() -> None:
    cycle, _, _, done, store = runner(network=Network(error=TimeoutError("no network")))

    result = cycle.run()

    assert not result.success
    assert result.error_phase is CyclePhase.FETCH
    assert result.error_type == "OfflineDataUnavailable"
    assert result.phases == (
        CyclePhase.MODE,
        CyclePhase.NETWORK,
        CyclePhase.FETCH,
        CyclePhase.PERSIST,
        CyclePhase.DONE,
    )
    assert done.calls == 1
    assert len(store.results) == 1


def test_unchanged_framebuffer_skips_physical_refresh() -> None:
    frame = bytes([0xA5]) * FRAME_BYTES
    snapshot = StoredCycle(
        station_id="8531833",
        generated_at=datetime(2026, 8, 24, tzinfo=UTC),
        predictions=predictions(),
        framebuffer=frame,
        framebuffer_sha256=hashlib.sha256(frame).hexdigest(),
    )
    cycle, _, display, done, _ = runner(
        renderer=Renderer(framebuffer=frame), store=MemoryStateStore(snapshot)
    )

    result = cycle.run()

    assert result.success
    assert not result.display_refreshed
    assert display.frames == []
    assert done.calls == 1


def test_equal_last_good_frame_refreshes_when_retained_display_state_is_unknown() -> None:
    frame = bytes([0xA5]) * FRAME_BYTES
    snapshot = StoredCycle(
        station_id="8531833",
        generated_at=datetime(2026, 8, 24, tzinfo=UTC),
        predictions=predictions(),
        framebuffer=frame,
        framebuffer_sha256=hashlib.sha256(frame).hexdigest(),
    )
    store = MemoryStateStore(snapshot)
    store.display_state = None
    cycle, _, display, _, _ = runner(renderer=Renderer(framebuffer=frame), store=store)

    result = cycle.run()

    assert result.success
    assert result.display_refreshed
    assert display.frames == [frame]
    assert store.display_state == snapshot.framebuffer_sha256


def test_failed_refresh_leaves_display_unknown_and_prevents_later_false_skip() -> None:
    old_frame = bytes([0xA5]) * FRAME_BYTES
    new_frame = bytes([0x5A]) * FRAME_BYTES
    snapshot = StoredCycle(
        station_id="8531833",
        generated_at=datetime(2026, 8, 24, tzinfo=UTC),
        predictions=predictions(),
        framebuffer=old_frame,
        framebuffer_sha256=hashlib.sha256(old_frame).hexdigest(),
    )
    store = MemoryStateStore(snapshot)
    failed_cycle, _, _, _, _ = runner(
        renderer=Renderer(framebuffer=new_frame),
        display=Display(error=OSError("interrupted refresh")),
        store=store,
    )

    failed = failed_cycle.run()

    assert not failed.success
    assert failed.error_phase is CyclePhase.DISPLAY
    assert store.display_state is None

    retry, _, retry_display, _, _ = runner(
        renderer=Renderer(framebuffer=old_frame),
        store=store,
    )
    recovered = retry.run()

    assert recovered.success
    assert recovered.display_refreshed
    assert retry_display.frames == [old_frame]


def test_setup_mode_runs_bounded_network_session_then_persist_and_done() -> None:
    network = Network(connected=True)
    cycle, renderer, display, done, _ = runner(mode=Mode(CycleMode.SETUP), network=network)

    result = cycle.run()

    assert result.success
    assert result.phases == (
        CyclePhase.MODE,
        CyclePhase.NETWORK,
        CyclePhase.PERSIST,
        CyclePhase.DONE,
    )
    assert network.calls[0][0] is CycleMode.SETUP
    assert network.calls[0][1] <= default_config().timeouts.setup_session_seconds
    assert network.release_calls == []
    assert renderer.models == []
    assert display.frames == []
    assert done.calls == 1


class FailingStore(MemoryStateStore):
    def persist(self, snapshot: StoredCycle) -> None:
        del snapshot
        raise OSError("disk full")


def test_persistence_failure_marks_cycle_failed_and_still_signals_done_once() -> None:
    done = Done()
    cycle, _, _, _, _ = runner(done=done, store=FailingStore())

    result = cycle.run()

    assert not result.success
    assert result.error_phase is CyclePhase.PERSIST
    assert done.calls == 1


class BlockingStore(MemoryStateStore):
    def __init__(self, release: threading.Event) -> None:
        super().__init__()
        self.release = release

    def persist(self, snapshot: StoredCycle) -> None:
        del snapshot
        self.release.wait()


def test_blocked_persistent_transaction_times_out_and_still_signals_done() -> None:
    release = threading.Event()
    done = Done()
    cycle, _, _, _, _ = runner(done=done, store=BlockingStore(release))
    cycle.config = replace(
        cycle.config,
        timeouts=replace(cycle.config.timeouts, persist_seconds=0.03),
    )
    started = time.monotonic()
    try:
        result = cycle.run()
    finally:
        release.set()

    assert time.monotonic() - started < 0.2
    assert not result.success
    assert result.error_phase is CyclePhase.PERSIST
    assert result.error_type == "CycleTimeoutError"
    assert done.calls == 1


def test_done_adapter_failure_is_called_once_and_reported() -> None:
    done = Done(error=OSError("gpio failed"))
    cycle, _, _, _, _ = runner(done=done)

    result = cycle.run()

    assert not result.success
    assert result.error_phase is CyclePhase.DONE
    assert done.calls == 1


class BadClock(FixedClock):
    def now(self) -> datetime:
        raise RuntimeError("clock unavailable")


def test_early_runtime_failure_still_attempts_done_exactly_once() -> None:
    done = Done()
    cycle, _, _, _, _ = runner(done=done)
    cycle.clock = BadClock()

    with pytest.raises(RuntimeError, match="clock unavailable"):
        cycle.run()
    assert done.calls == 1


def test_unsynchronized_time_uses_retained_offline_overlay_without_solar() -> None:
    frame = bytes([0xA5]) * FRAME_BYTES
    snapshot = StoredCycle(
        station_id="8531833",
        generated_at=datetime(2026, 8, 24, tzinfo=UTC),
        predictions=predictions(),
        framebuffer=frame,
        framebuffer_sha256=hashlib.sha256(frame).hexdigest(),
    )
    source = PredictionSource()
    cycle, renderer, _, _, _ = runner(
        source=source,
        renderer=Renderer(framebuffer=bytes([0x33]) * FRAME_BYTES),
        store=MemoryStateStore(snapshot),
    )
    cycle.time_sync = TimeSync(synchronized=False)

    def solar_must_not_run(
        local_date: date,
        latitude: float,
        longitude: float,
        timezone_name: str,
    ) -> SolarTimes:
        del local_date, latitude, longitude, timezone_name
        raise AssertionError("solar calculation must not run without trusted time")

    cycle.solar_calculator = solar_must_not_run

    result = cycle.run()

    assert result.success
    assert result.used_cached_data
    assert source.calls == 0
    assert renderer.models[0].offline
    assert renderer.models[0].previous_framebuffer == frame
    assert renderer.models[0].generated_at == snapshot.generated_at.astimezone(
        renderer.models[0].generated_at.tzinfo
    )
    assert result.production_route_interface is None


def test_usb_production_route_fails_run_before_fetch() -> None:
    source = PredictionSource()
    route = Route("usb0")
    cycle, _, display, done, store = runner(source=source)
    cycle.production_route = route

    result = cycle.run()

    assert not result.success
    assert result.error_phase is CyclePhase.NETWORK
    assert result.error_message == "production route must use wlan0; got usb0"
    assert result.production_route_interface == "usb0"
    assert source.calls == 0
    assert display.frames == []
    assert done.calls == 1
    assert store.results[0].production_route_interface == "usb0"


def test_route_inspection_outage_uses_last_good_without_attempting_fetch() -> None:
    frame = bytes([0x67]) * FRAME_BYTES
    snapshot = StoredCycle(
        station_id="8531833",
        generated_at=datetime(2026, 8, 24, tzinfo=UTC),
        predictions=predictions(),
        framebuffer=frame,
        framebuffer_sha256=hashlib.sha256(frame).hexdigest(),
    )
    source = PredictionSource()
    cycle, renderer, _, done, _ = runner(
        source=source,
        renderer=Renderer(framebuffer=bytes([0x89]) * FRAME_BYTES),
        store=MemoryStateStore(snapshot),
    )
    cycle.production_route = Route(error=TimeoutError("DNS unavailable"))

    result = cycle.run()

    assert result.success
    assert result.used_cached_data
    assert result.production_route_interface is None
    assert source.calls == 0
    assert renderer.models[0].offline
    assert done.calls == 1
    assert any(
        "production route unavailable" in warning
        for warning in renderer.models[0].warnings
    )


def test_verified_wlan_route_and_real_wifi_observation_are_persisted() -> None:
    route = Route()
    network = Network()
    cycle, _, _, _, store = runner(network=network)
    cycle.production_route = route
    cycle.wifi_observation_source = "runtime"

    result = cycle.run()

    assert result.success
    assert result.production_route_interface == "wlan0"
    assert result.wifi_active_observation_source == "runtime"
    assert result.wifi_active_observed_seconds is not None
    assert result.wifi_active_observed_seconds > 0
    assert store.results[0].wifi_active_observed_seconds is not None
    assert len(network.release_calls) == 1
    assert 0 < network.release_calls[0] <= 5


def test_wifi_release_failure_is_visible_but_does_not_discard_fetched_data() -> None:
    network = Network(release_error=TimeoutError("disconnect timed out"))
    cycle, renderer, _, _, _ = runner(network=network)

    result = cycle.run()

    assert result.success
    assert len(network.release_calls) == 1
    assert any("Wi-Fi release unavailable" in warning for warning in renderer.models[0].warnings)
    assert result.wifi_active_observed_seconds is not None


@pytest.mark.parametrize("fault_kind", ["dns", "tls", "http_503"])
def test_injected_noaa_fault_is_durable_and_uses_cached_frame(
    fault_kind: str,
) -> None:
    frame = bytes([0xC3]) * FRAME_BYTES
    snapshot = StoredCycle(
        station_id="8531833",
        generated_at=datetime(2026, 8, 24, tzinfo=UTC),
        predictions=predictions(),
        framebuffer=frame,
        framebuffer_sha256=hashlib.sha256(frame).hexdigest(),
    )
    source = PredictionSource(
        error=NoaaTransportError(f"injected {fault_kind}"), fault_kind=fault_kind
    )
    network = Network()
    cycle, renderer, _, _, store = runner(
        network=network,
        source=source,
        renderer=Renderer(framebuffer=bytes([0x4D]) * FRAME_BYTES),
        store=MemoryStateStore(snapshot),
    )

    result = cycle.run()

    assert result.success
    assert result.used_cached_data
    assert result.injected_noaa_fault == fault_kind
    assert result.noaa_fault_kind == fault_kind
    assert renderer.models[0].offline
    assert store.results[0].injected_noaa_fault == fault_kind
    assert len(network.release_calls) == 1
