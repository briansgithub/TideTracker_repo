"""Bounded, injected TideTracker cycle state machine."""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from zoneinfo import ZoneInfo

from .deadlines import run_with_wall_clock_deadline
from .models import (
    AppConfig,
    CycleMode,
    CyclePhase,
    CycleResult,
    JSONValue,
    NetworkStatus,
    RenderModel,
    SolarTimes,
    StoredCycle,
    TidePrediction,
)
from .noaa import PredictionValidationError, validate_predictions
from .ports import (
    Clock,
    DisplayPort,
    DonePort,
    ModePort,
    NetworkPort,
    PredictionSource,
    Renderer,
    SolarCalculator,
    StateStore,
    TelemetrySink,
)
from .route import ProductionRoutePort, StaticProductionRoute
from .solar import calculate_sun_times
from .telemetry import NullTelemetry, redact_text
from .tide_window import build_tide_window, validate_tide_coverage
from .time_sync import AssumedSynchronizedTime, TimeSyncPort

EXPECTED_FRAMEBUFFER_BYTES = 48_000


class CycleError(RuntimeError):
    pass


class CycleTimeoutError(CycleError):
    pass


class OfflineDataUnavailable(CycleError):
    pass


class InvalidFramebuffer(CycleError):
    pass


def _run_with_wall_clock_timeout(
    operation: Callable[[], None],
    *,
    timeout_seconds: float,
    label: str,
) -> None:
    if timeout_seconds <= 0:
        raise CycleTimeoutError(f"{label} deadline exhausted")
    run_with_wall_clock_deadline(
        operation,
        timeout_seconds=timeout_seconds,
        timeout_error=CycleTimeoutError(f"{label} exceeded its wall-clock deadline"),
    )


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class _DoneOnce:
    def __init__(self, port: DonePort) -> None:
        self.port = port
        self.called = False

    def signal(self) -> None:
        if self.called:
            return
        self.called = True
        self.port.signal_done()


def _bounded_message(error: BaseException) -> str:
    message = redact_text(" ".join(str(error).split()))
    return message[:500] or error.__class__.__name__


class CycleRunner:
    """Execute one run or setup cycle and always attempt exactly one DONE.

    Side-effecting adapters must honor the timeout passed to them. The runner
    additionally maintains a total monotonic deadline and refuses to begin a
    phase with an exhausted budget.
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        clock: Clock,
        mode_port: ModePort,
        network: NetworkPort,
        predictions: PredictionSource,
        renderer: Renderer,
        display: DisplayPort,
        done: DonePort,
        store: StateStore,
        telemetry: TelemetrySink | None = None,
        solar_calculator: SolarCalculator = calculate_sun_times,
        run_id_factory: Callable[[], str] | None = None,
        time_sync: TimeSyncPort | None = None,
        production_route: ProductionRoutePort | None = None,
        release_id: str = "built-in",
        execution_source: str = "built-in",
        wifi_observation_source: str = "simulated",
    ) -> None:
        if not release_id.strip() or not execution_source.strip():
            raise ValueError("release identity must not be empty")
        if wifi_observation_source not in {"runtime", "simulated"}:
            raise ValueError("Wi-Fi observation source is invalid")
        self.config = config
        self.clock = clock
        self.mode_port = mode_port
        self.network = network
        self.predictions = predictions
        self.renderer = renderer
        self.display = display
        self.done = done
        self.store = store
        self.telemetry = telemetry or NullTelemetry()
        self.solar_calculator = solar_calculator
        self.run_id_factory = run_id_factory or (lambda: uuid.uuid4().hex)
        self.time_sync = time_sync or AssumedSynchronizedTime(clock)
        self.production_route = production_route or StaticProductionRoute()
        self.release_id = release_id
        self.execution_source = execution_source
        self.wifi_observation_source = wifi_observation_source

    def _emit(self, event: str, **fields: JSONValue) -> None:
        # Telemetry must never prevent the power controller from receiving DONE.
        with suppress(Exception):
            self.telemetry.emit(event, **fields)

    def run(self) -> CycleResult:
        """Run one lifecycle, with an outer safety net for early failures."""

        done_once = _DoneOnce(self.done)
        try:
            return self._run(done_once)
        finally:
            # Invalid clocks/run-id factories are outside the ordinary phase
            # result contract, but must not strand TPL5110 power.
            with suppress(Exception):
                done_once.signal()

    def _run(self, done_once: _DoneOnce) -> CycleResult:
        run_id = self.run_id_factory()
        started_at = self.clock.now()
        if started_at.tzinfo is None or started_at.utcoffset() is None:
            raise ValueError("Clock.now() must return a timezone-aware datetime")
        started_monotonic = self.clock.monotonic()
        deadline = started_monotonic + self.config.timeouts.normal_cycle_seconds
        mode = CycleMode.RUN
        phases: list[CyclePhase] = []
        durations: dict[str, float] = {}
        warnings: list[str] = []
        current_phase: CyclePhase | None = None
        success = False
        error_phase: CyclePhase | None = None
        error_type: str | None = None
        error_message: str | None = None
        framebuffer: bytes | None = None
        framebuffer_sha256: str | None = None
        display_refreshed = False
        used_cached_data = False
        network_status = NetworkStatus(connected=False)
        production_route_interface: str | None = None
        time_synchronized = False
        wifi_client_prepared = False
        wifi_release_attempted = False
        wifi_active_started_monotonic: float | None = None
        wifi_active_observed_seconds: float | None = None
        selected_predictions: tuple[TidePrediction, ...] | None = None
        last_good: StoredCycle | None = None

        @contextmanager
        def phase(value: CyclePhase) -> Iterator[None]:
            nonlocal current_phase
            current_phase = value
            phases.append(value)
            phase_started = self.clock.monotonic()
            self._emit("phase_started", run_id=run_id, phase=value.value)
            try:
                yield
            finally:
                elapsed = max(0.0, self.clock.monotonic() - phase_started)
                durations[value.value] = elapsed
                self._emit(
                    "phase_finished",
                    run_id=run_id,
                    phase=value.value,
                    duration_seconds=elapsed,
                )

        def remaining(requested: float) -> float:
            available = deadline - self.clock.monotonic()
            if available <= 0:
                raise CycleTimeoutError("cycle deadline exhausted")
            return min(requested, available)

        def release_wifi_client() -> None:
            nonlocal wifi_active_observed_seconds, wifi_release_attempted
            if wifi_release_attempted or mode is not CycleMode.RUN or not wifi_client_prepared:
                return
            wifi_release_attempted = True
            available = deadline - self.clock.monotonic()
            if available <= 0:
                warnings.append("Wi-Fi release skipped: cycle deadline exhausted")
                return
            try:
                self.network.release(timeout_seconds=min(5.0, available))
            except Exception as exc:
                warnings.append(f"Wi-Fi release unavailable: {_bounded_message(exc)}")
                return
            if wifi_active_started_monotonic is not None:
                observed = max(
                    0.0,
                    self.clock.monotonic() - wifi_active_started_monotonic,
                )
                wifi_active_observed_seconds = (
                    observed if self.wifi_observation_source == "runtime" else 0.0
                )
            self._emit("wifi_client_released", run_id=run_id, interface="wlan0")

        def make_result(*, ended_at: datetime | None = None) -> CycleResult:
            raw_fault_kind = getattr(self.predictions, "fault_kind", None)
            noaa_fault_kind = raw_fault_kind if isinstance(raw_fault_kind, str) else None
            return CycleResult(
                run_id=run_id,
                success=success,
                mode=mode,
                started_at=started_at,
                ended_at=ended_at or self.clock.now(),
                phases=tuple(phases),
                phase_durations_seconds=MappingProxyType(dict(durations)),
                framebuffer_sha256=framebuffer_sha256,
                display_refreshed=display_refreshed,
                used_cached_data=used_cached_data,
                network_interface=network_status.interface,
                production_route_interface=production_route_interface,
                release_id=self.release_id,
                execution_source=self.execution_source,
                noaa_fault_kind=noaa_fault_kind,
                injected_noaa_fault=noaa_fault_kind,
                wifi_active_observed_seconds=wifi_active_observed_seconds,
                wifi_active_observation_source=(
                    self.wifi_observation_source
                    if wifi_active_observed_seconds is not None
                    else None
                ),
                error_phase=error_phase,
                error_type=error_type,
                error_message=error_message,
            )

        self._emit(
            "cycle_started",
            run_id=run_id,
            station_id=self.config.station.station_id,
            config_schema=self.config.schema_version,
            release_id=self.release_id,
            execution_source=self.execution_source,
        )

        try:
            with phase(CyclePhase.MODE):
                try:
                    restored = self.time_sync.restore_before_network(
                        timeout_seconds=remaining(min(5.0, self.config.timeouts.network_seconds))
                    )
                    if restored:
                        self._emit("trusted_time_restored", run_id=run_id)
                except Exception as exc:
                    warnings.append(f"trusted-time restore unavailable: {_bounded_message(exc)}")
                # A restored estimate is useful for logs and setup, but is not
                # trusted for TLS or dated calculations until NTP confirms it.
                started_at = self.clock.now()
                if started_at.tzinfo is None or started_at.utcoffset() is None:
                    raise ValueError("Clock.now() must return a timezone-aware datetime")
                mode = self.mode_port.read_mode()
                if not isinstance(mode, CycleMode):
                    raise CycleError("mode adapter returned an invalid mode")
                if mode is CycleMode.SETUP:
                    deadline = started_monotonic + self.config.timeouts.setup_session_seconds

            with phase(CyclePhase.NETWORK):
                network_timeout = (
                    self.config.timeouts.setup_session_seconds
                    if mode is CycleMode.SETUP
                    else self.config.timeouts.network_seconds
                )
                try:
                    network_status = self.network.prepare(
                        mode, timeout_seconds=remaining(network_timeout)
                    )
                    if not isinstance(network_status, NetworkStatus):
                        raise CycleError("network adapter returned an invalid status")
                except Exception as exc:
                    if mode is CycleMode.SETUP:
                        raise
                    warnings.append(f"network unavailable: {_bounded_message(exc)}")
                    network_status = NetworkStatus(connected=False)

                if (
                    mode is CycleMode.RUN
                    and network_status.connected
                    and network_status.interface == "wlan0"
                ):
                    wifi_client_prepared = True
                    wifi_active_started_monotonic = self.clock.monotonic()

                if mode is CycleMode.RUN and network_status.connected:
                    try:
                        synchronized_at = self.time_sync.wait_until_synchronized(
                            timeout_seconds=remaining(self.config.timeouts.network_seconds)
                        )
                        time_synchronized = True
                        self._emit(
                            "trusted_time_synchronized",
                            run_id=run_id,
                            synchronized_at=synchronized_at.isoformat(),
                        )
                    except Exception as exc:
                        warnings.append(
                            f"trusted time unavailable: {_bounded_message(exc)}"
                        )
                    if time_synchronized:
                        try:
                            production_route_interface = (
                                self.production_route.interface_for_endpoint(
                                    timeout_seconds=remaining(
                                        min(10.0, self.config.timeouts.network_seconds)
                                    )
                                )
                            )
                        except Exception as exc:
                            # DNS and route inspection can fail during ordinary
                            # outages. Treat that as offline, but never attempt a
                            # fetch unless wlan0 has been positively proved.
                            warnings.append(
                                f"production route unavailable: {_bounded_message(exc)}"
                            )
                            network_status = NetworkStatus(connected=False)
                        else:
                            if production_route_interface != "wlan0":
                                raise CycleError(
                                    "production route must use wlan0; "
                                    f"got {production_route_interface}"
                                )
                            self._emit(
                                "production_route_verified",
                                run_id=run_id,
                                production_route_interface=production_route_interface,
                            )

            if mode is CycleMode.RUN:
                with phase(CyclePhase.FETCH):
                    try:
                        candidate = self.store.load_last_good()
                        if (
                            candidate is not None
                            and candidate.station_id == self.config.station.station_id
                        ):
                            last_good = candidate
                    except Exception as exc:
                        warnings.append(f"last-good state unavailable: {_bounded_message(exc)}")

                    fresh_predictions: tuple[TidePrediction, ...] | None = None
                    if network_status.connected and time_synchronized:
                        local_now = self.clock.now().astimezone(
                            ZoneInfo(self.config.station.timezone)
                        )
                        try:
                            tide_window = build_tide_window(
                                local_now,
                                self.config.station.timezone,
                                chart_hours=self.config.chart_hours,
                                minimum_range_hours=self.config.prediction_range_hours,
                            )
                            fetched_predictions = self.predictions.fetch(
                                self.config.station,
                                begin=tide_window.request_begin,
                                range_hours=tide_window.request_range_hours,
                                interval_minutes=self.config.prediction_interval_minutes,
                                timeout_seconds=remaining(self.config.timeouts.fetch_seconds),
                            )
                            fresh_predictions = validate_tide_coverage(
                                fetched_predictions,
                                tide_window,
                                interval_minutes=self.config.prediction_interval_minutes,
                            )
                        except Exception as exc:
                            warnings.append(f"prediction fetch failed: {_bounded_message(exc)}")
                    if fresh_predictions is not None:
                        selected_predictions = fresh_predictions
                    elif last_good is not None:
                        selected_predictions = last_good.predictions
                        used_cached_data = True
                    else:
                        raise OfflineDataUnavailable("no fresh or last-good tide data is available")

                # NOAA, route, and NTP activity is complete. Release the client
                # before CPU-heavy rendering and the slow physical refresh.
                release_wifi_client()

                with phase(CyclePhase.VALIDATE):
                    assert selected_predictions is not None
                    try:
                        selected_predictions = validate_predictions(selected_predictions)
                    except PredictionValidationError:
                        if not used_cached_data and last_good is not None:
                            selected_predictions = validate_predictions(last_good.predictions)
                            used_cached_data = True
                            warnings.append("fresh predictions were invalid; using last-good data")
                        else:
                            raise

                with phase(CyclePhase.COMPUTE):
                    station_zone = ZoneInfo(self.config.station.timezone)
                    if not time_synchronized and last_good is not None:
                        # PillowRenderer reuses the retained framebuffer and
                        # adds only the offline overlay. Avoid computing a new
                        # dated chart from the restored-but-untrusted estimate.
                        local_now = last_good.generated_at.astimezone(station_zone)
                    else:
                        local_now = self.clock.now().astimezone(station_zone)
                    local_date = local_now.date()
                    if not time_synchronized and last_good is not None:
                        solar_days = tuple(
                            SolarTimes(
                                local_date=local_date + timedelta(days=offset),
                                timezone=self.config.station.timezone,
                                sunrise=None,
                                sunset=None,
                            )
                            for offset in (-1, 0, 1)
                        )
                    else:
                        solar_days = tuple(
                            self.solar_calculator(
                                local_date + timedelta(days=offset),
                                self.config.station.latitude,
                                self.config.station.longitude,
                                self.config.station.timezone,
                            )
                            for offset in (-1, 0, 1)
                        )
                    render_model = RenderModel(
                        generated_at=local_now,
                        config=self.config,
                        predictions=selected_predictions,
                        solar=solar_days[1],
                        solar_days=solar_days,
                        previous_framebuffer=last_good.framebuffer if last_good else None,
                        offline=(
                            used_cached_data
                            or not network_status.connected
                            or not time_synchronized
                        ),
                        warnings=tuple(warnings),
                    )

                with phase(CyclePhase.RENDER):
                    rendered = self.renderer.render(
                        render_model,
                        timeout_seconds=remaining(self.config.timeouts.render_seconds),
                    )
                    if (
                        not isinstance(rendered, bytes)
                        or len(rendered) != EXPECTED_FRAMEBUFFER_BYTES
                    ):
                        raise InvalidFramebuffer(
                            "renderer must return exactly "
                            f"{EXPECTED_FRAMEBUFFER_BYTES} controller bytes"
                        )
                    framebuffer = rendered
                    framebuffer_sha256 = hashlib.sha256(framebuffer).hexdigest()

                with phase(CyclePhase.DISPLAY):
                    known_display_hash: str | None = None

                    def load_display_state() -> None:
                        nonlocal known_display_hash
                        known_display_hash = self.store.load_display_state()

                    try:
                        _run_with_wall_clock_timeout(
                            load_display_state,
                            timeout_seconds=remaining(
                                min(2.0, self.config.timeouts.persist_seconds)
                            ),
                            label="display state read",
                        )
                    except Exception as exc:
                        warnings.append(
                            f"retained display state unavailable: {_bounded_message(exc)}"
                        )
                    if (
                        last_good is not None
                        and last_good.framebuffer_sha256 == framebuffer_sha256
                        and last_good.framebuffer == framebuffer
                        and known_display_hash == framebuffer_sha256
                    ):
                        display_refreshed = False
                    else:
                        _run_with_wall_clock_timeout(
                            self.store.invalidate_display_state,
                            timeout_seconds=remaining(self.config.timeouts.persist_seconds),
                            label="display state invalidation",
                        )
                        self.display.display(
                            framebuffer,
                            timeout_seconds=remaining(self.config.timeouts.display_seconds),
                        )
                        assert framebuffer_sha256 is not None
                        _run_with_wall_clock_timeout(
                            lambda: self.store.mark_display_state(framebuffer_sha256),
                            timeout_seconds=remaining(self.config.timeouts.persist_seconds),
                            label="display state confirmation",
                        )
                        display_refreshed = True
                success = True
            else:
                # NetworkPort.prepare owns the bounded captive-portal session.
                success = True
        except Exception as exc:
            # FETCH can fail before reaching the normal early-release point.
            # No later failure path requires production network access.
            release_wifi_client()
            success = False
            error_phase = current_phase
            error_type = exc.__class__.__name__
            error_message = _bounded_message(exc)
            self._emit(
                "cycle_error",
                run_id=run_id,
                phase=current_phase.value if current_phase else None,
                error_type=error_type,
                error_message=error_message,
            )

        try:
            with phase(CyclePhase.PERSIST):
                def persist_transaction() -> None:
                    nonlocal wifi_active_observed_seconds
                    if time_synchronized:
                        trusted_epoch = self.time_sync.persist_trusted(self.clock.now())
                        self._emit(
                            "trusted_time_persisted",
                            run_id=run_id,
                            trusted_utc_epoch=trusted_epoch,
                        )
                    if success and mode is CycleMode.RUN:
                        assert framebuffer is not None
                        assert framebuffer_sha256 is not None
                        assert selected_predictions is not None
                        self.store.persist(
                            StoredCycle(
                                station_id=self.config.station.station_id,
                                generated_at=self.clock.now(),
                                predictions=selected_predictions,
                                framebuffer=framebuffer,
                                framebuffer_sha256=framebuffer_sha256,
                            )
                        )
                    self.store.sync()
                    if (
                        wifi_active_observed_seconds is None
                        and wifi_active_started_monotonic is not None
                    ):
                        observed = max(
                            0.0,
                            self.clock.monotonic() - wifi_active_started_monotonic,
                        )
                        wifi_active_observed_seconds = (
                            observed if self.wifi_observation_source == "runtime" else 0.0
                        )
                    # Capture the observation after the bulk durability barrier;
                    # the following result write is one atomic file before DONE.
                    self.store.persist_result(make_result())

                _run_with_wall_clock_timeout(
                    persist_transaction,
                    timeout_seconds=remaining(self.config.timeouts.persist_seconds),
                    label="persistent transaction",
                )
        except Exception as exc:
            self._emit(
                "persistence_error",
                run_id=run_id,
                error_type=exc.__class__.__name__,
                error_message=_bounded_message(exc),
            )
            if success:
                success = False
                error_phase = CyclePhase.PERSIST
                error_type = exc.__class__.__name__
                error_message = _bounded_message(exc)

        try:
            with phase(CyclePhase.DONE):
                done_once.signal()
        except Exception as exc:
            self._emit(
                "done_error",
                run_id=run_id,
                error_type=exc.__class__.__name__,
                error_message=_bounded_message(exc),
            )
            if success:
                success = False
                error_phase = CyclePhase.DONE
                error_type = exc.__class__.__name__
                error_message = _bounded_message(exc)

        ended_at = self.clock.now()
        result = make_result(ended_at=ended_at)
        self._emit(
            "cycle_finished",
            run_id=run_id,
            success=success,
            mode=mode.value,
            total_duration_seconds=max(0.0, self.clock.monotonic() - started_monotonic),
            framebuffer_sha256=framebuffer_sha256,
            display_refreshed=display_refreshed,
            used_cached_data=used_cached_data,
            network_interface=network_status.interface,
            production_route_interface=production_route_interface,
            release_id=self.release_id,
            execution_source=self.execution_source,
            noaa_fault_kind=result.noaa_fault_kind,
            injected_noaa_fault=result.injected_noaa_fault,
            wifi_active_observed_seconds=result.wifi_active_observed_seconds,
            wifi_active_observation_source=result.wifi_active_observation_source,
            error_phase=error_phase.value if error_phase else None,
            error_type=error_type,
        )
        return result
