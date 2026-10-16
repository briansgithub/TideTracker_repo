"""Command-line entrypoints for the bounded runtime and signed updater."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import default_config, load_config, save_config
from .cycle import CycleRunner, SystemClock
from .diagnostics import collect_diagnostics
from .display import FileDisplay, WaveshareDisplay
from .hardware import GpioModeDone, GpioModeReader
from .metrics import ResourceSnapshot, cycle_metric_fields
from .models import AppConfig, CycleMode, NetworkStatus
from .network import NetworkManagerAdapter, NetworkProfile
from .noaa import (
    FaultInjectingTransport,
    NoaaClient,
    UrlLibTransport,
    stage_noaa_fault_request,
)
from .portal import AtomicJSONConfigStore, PortalApplication, PortalNetworkPort, PortalServer
from .release import (
    PROJECT_ENTRYPOINT,
    DeterministicBundleBuilder,
    OpenSSLEd25519Signer,
    assemble_project_payload,
    extract_bundle,
)
from .render import PillowRenderer, legacy_bitmap_to_framebuffer
from .route import StaticProductionRoute, SystemProductionRoute
from .simulated import (
    FixedClock,
    FixturePredictionSource,
    RecordingDone,
    SimulatedNetwork,
    StaticMode,
)
from .storage import (
    AtomicCycleStore,
    atomic_write_json,
    bounded_system_sync,
    prune_regular_files,
)
from .supervisor import (
    AttemptEvidence,
    HardwareOwnershipError,
    ReleaseAttempt,
    ReleaseSupervisor,
    SubprocessReleaseExecutor,
    SupervisorResult,
    run_isolated_command,
)
from .telemetry import (
    CompositeTelemetry,
    JsonlTelemetry,
    SerialLineWriter,
    UartPhaseTelemetry,
    redact_text,
)
from .time_sync import AssumedSynchronizedTime, TrustedEpochStore, TrustedTimeAuthority
from .updater import OpenSSLEd25519Verifier, ReleaseCommandService, ReleaseUpdater

DEFAULT_DATA_DIR = Path("/var/lib/tidetracker")
DEFAULT_PUBLIC_KEY = Path("/etc/tidetracker/release-ed25519.pub.pem")
DEFAULT_DONE_MARKER = Path("/run/tidetracker/done-signalled")
DEFAULT_UART_DEVICE = Path("/dev/serial0")
DEFAULT_IMAGE_BUILD_ID = Path("/etc/tidetracker/image-build-id")
DEFAULT_KEY_POLICY_SHA256 = Path("/etc/tidetracker/key-policy.sha256")
DEFAULT_NOAA_FAULT_REQUEST = Path("/var/lib/tidetracker/state/hil/noaa-fault.request")
DEFAULT_NOAA_FAULT_RUNTIME = Path("/run/tidetracker-hil/noaa-fault")
DEFAULT_FAILSAFE = Path("/usr/local/libexec/tidetracker-failsafe")
SIMULATION_UTC = datetime(2026, 8, 25, 12, tzinfo=UTC)
EXECUTION_SOURCES = frozenset({"built-in", "verified-release"})
RELEASE_TRIAL_GRACE_SECONDS = 30.0


def _environment_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else default


def _environment_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _execution_identity() -> tuple[str, str]:
    release_id = os.environ.get("TIDETRACKER_RELEASE_ID", "").strip()
    source = os.environ.get("TIDETRACKER_EXECUTION_SOURCE", "").strip()
    if release_id:
        source = source or "verified-release"
    else:
        release_id = "built-in"
        source = source or "built-in"
    if source not in EXECUTION_SOURCES:
        raise ValueError("TIDETRACKER_EXECUTION_SOURCE is invalid")
    if (source == "built-in") != (release_id == "built-in"):
        raise ValueError("release ID and execution source do not match")
    return release_id, source


def _image_build_identity() -> str:
    path = _environment_path("TIDETRACKER_IMAGE_BUILD_ID_FILE", DEFAULT_IMAGE_BUILD_ID)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 128:
        raise ValueError("image build identity file is invalid")
    value = path.read_text(encoding="ascii").strip()
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("image build identity is not a lowercase SHA-256 digest")
    return value


def _key_policy_identity() -> str:
    path = _environment_path("TIDETRACKER_KEY_POLICY_SHA256_FILE", DEFAULT_KEY_POLICY_SHA256)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 128:
        raise ValueError("key policy identity file is invalid")
    value = path.read_text(encoding="ascii").strip()
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("key policy identity is not a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    data: Path
    config: Path
    assets: Path | None
    simulated_display: Path


def _runtime_paths(args: argparse.Namespace) -> RuntimePaths:
    data = Path(args.data_dir or _environment_path("TIDETRACKER_DATA_DIR", DEFAULT_DATA_DIR))
    config = Path(
        args.config
        or os.environ.get("TIDETRACKER_CONFIG", "")
        or data / "config" / "config.json"
    )
    raw_assets = args.asset_dir or os.environ.get("TIDETRACKER_ASSET_DIR")
    assets = Path(raw_assets) if raw_assets else None
    display = Path(
        args.display_output
        or os.environ.get("TIDETRACKER_DISPLAY_OUTPUT", "")
        or data / "results" / "simulated-framebuffer.bin"
    )
    return RuntimePaths(data, config, assets, display)


def _load_or_create_config(path: Path) -> AppConfig:
    if path.exists():
        return load_config(path)
    config = default_config()
    save_config(path, config)
    return config


class _DeferredDone:
    def signal_done(self) -> None:
        return


class _SimulatedPortalNetwork:
    def scan(self) -> tuple[Any, ...]:
        return ()

    def status(self) -> Any:
        from .network import NetworkStatus as PortalNetworkStatus

        return PortalNetworkStatus(True, "100 (connected)", "simulated-wifi")

    def test_and_save(self, profile: NetworkProfile) -> dict[str, object]:
        return profile.public_dict()

    def force_save(self, profile: NetworkProfile) -> dict[str, object]:
        return profile.public_dict()


def _write_cycle_metrics(
    data_dir: Path,
    result: Any,
    before: ResourceSnapshot,
    after: ResourceSnapshot,
    display: Any,
) -> None:
    fields = cycle_metric_fields(
        result,
        before,
        after,
        display_busy_seconds=float(getattr(display, "last_busy_seconds", 0.0)),
        refresh_count=int(getattr(display, "refresh_count", 0)),
    )
    atomic_write_json(
        data_dir / "telemetry" / f"{result.run_id}.json",
        {"schema_version": 1, "event": "cycle_metrics", **fields},
    )
    prune_regular_files(data_dir / "telemetry", "*.json", keep=128)


def _run_builtin_cycle(args: argparse.Namespace, *, defer_done: bool) -> int:
    paths = _runtime_paths(args)
    config = _load_or_create_config(paths.config)
    file_telemetry = JsonlTelemetry(
        paths.data / "telemetry" / "events.jsonl", fsync_each_event=False
    )
    store = AtomicCycleStore(paths.data / "state" / "cycle")
    renderer = PillowRenderer(asset_dir=paths.assets)
    before = ResourceSnapshot.capture("wlan0")
    release_id, execution_source = _execution_identity()

    if args.simulate:
        simulation_clock = FixedClock(SIMULATION_UTC)
        mode = StaticMode(CycleMode(args.mode))
        runtime_network: Any = SimulatedNetwork(connected=not args.simulate_offline)
        predictions = FixturePredictionSource()
        display: Any = FileDisplay(paths.simulated_display)
        physical_done: Any = RecordingDone()
        runner = CycleRunner(
            config,
            clock=simulation_clock,
            mode_port=mode,
            network=runtime_network,
            predictions=predictions,
            renderer=renderer,
            display=display,
            done=_DeferredDone() if defer_done else physical_done,
            store=store,
            telemetry=file_telemetry,
            time_sync=AssumedSynchronizedTime(simulation_clock),
            production_route=StaticProductionRoute(),
            release_id=release_id,
            execution_source=execution_source,
        )
        result = runner.run()
    else:
        uart_setting = os.environ.get("TIDETRACKER_UART_TELEMETRY", "1").strip().lower()
        uart_enabled = uart_setting not in {"0", "false", "no", "off", "disabled"}
        uart = None
        telemetry: Any = file_telemetry
        if uart_enabled:
            uart_device = _environment_path("TIDETRACKER_UART_DEVICE", DEFAULT_UART_DEVICE)
            uart = UartPhaseTelemetry(SerialLineWriter(uart_device))
            uart.emit(
                "target_identity",
                release_id=release_id,
                execution_source=execution_source,
                image_build_id=_image_build_identity(),
                key_policy_sha256=_key_policy_identity(),
            )
            telemetry = CompositeTelemetry(file_telemetry, uart)
        system_clock = SystemClock()
        gpio = GpioModeDone(done_marker=None if defer_done else DEFAULT_DONE_MARKER)
        network_adapter = NetworkManagerAdapter()
        application = PortalApplication(
            network_adapter,
            AtomicJSONConfigStore(paths.config, config),
        )
        runtime_network = PortalNetworkPort(network_adapter, application)
        display = WaveshareDisplay()
        try:
            request_path = Path(
                os.environ.get("TIDETRACKER_NOAA_FAULT_REQUEST", "")
                or paths.data / "state" / "hil" / "noaa-fault.request"
            )
            runtime_fault_path = _environment_path(
                "TIDETRACKER_NOAA_FAULT_RUNTIME", DEFAULT_NOAA_FAULT_RUNTIME
            )
            staged_fault = stage_noaa_fault_request(request_path, runtime_fault_path)
            if staged_fault is not None:
                telemetry.emit("noaa_fault_staged", fault_kind=staged_fault)
            transport = FaultInjectingTransport(
                UrlLibTransport(), runtime_fault_path, telemetry=telemetry
            )
            time_sync = TrustedTimeAuthority(
                TrustedEpochStore(paths.data / "state" / "trusted-time.json"),
                system_clock,
            )
            runner = CycleRunner(
                config,
                clock=system_clock,
                mode_port=gpio,
                network=runtime_network,
                predictions=NoaaClient(
                    transport, datum=config.datum, units=config.units
                ),
                renderer=renderer,
                display=display,
                done=_DeferredDone() if defer_done else gpio,
                store=store,
                telemetry=telemetry,
                time_sync=time_sync,
                production_route=SystemProductionRoute(),
                release_id=release_id,
                execution_source=execution_source,
                wifi_observation_source="runtime",
            )
            result = runner.run()
        finally:
            application.close()
            gpio.close()
            if uart is not None:
                uart.close()

    after = ResourceSnapshot.capture("wlan0")
    _write_cycle_metrics(paths.data, result, before, after, display)
    return 0 if result.success else 1


def _run_supervised_builtin(
    args: argparse.Namespace,
    *,
    environment: Mapping[str, str],
    timeout_seconds: float,
) -> int:
    command = [
        sys.executable,
        "-c",
        (
            "import sys;"
            "from tidetracker.cli import main;"
            "raise SystemExit(main(sys.argv[1:]))"
        ),
        "cycle",
        "--builtin",
        "--defer-done",
    ]
    if args.simulate:
        command.extend(("--simulate", "--mode", args.mode))
    if args.simulate_offline:
        command.append("--simulate-offline")
    child_environment = dict(environment)
    child_environment.update(
        {
            "TIDETRACKER_RELEASE_ID": "built-in",
            "TIDETRACKER_EXECUTION_SOURCE": "built-in",
            "TIDETRACKER_SUPERVISED": "1",
        }
    )
    return run_isolated_command(
        command,
        cwd=Path.cwd(),
        env=child_environment,
        timeout=timeout_seconds,
    )


def _signal_outer_done(simulate: bool) -> None:
    if simulate:
        RecordingDone().signal_done()
        return
    failsafe = _environment_path("TIDETRACKER_FAILSAFE", DEFAULT_FAILSAFE)
    if not failsafe.is_absolute() or not failsafe.is_file():
        raise HardwareOwnershipError("bounded DONE failsafe is unavailable")
    code = run_isolated_command(
        [str(failsafe), "--done-only"],
        cwd=Path("/"),
        env=os.environ,
        timeout=12.0,
    )
    if code != 0:
        raise HardwareOwnershipError("bounded DONE failsafe did not complete")


def _read_last_cycle_result(data_dir: Path) -> Mapping[str, Any]:
    path = data_dir / "state" / "cycle" / "last_result.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


class _FreshCycleEvidenceValidator:
    """Reject exit-zero releases that did not create matching cycle evidence."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        prior = _read_last_cycle_result(data_dir)
        prior_run_id = prior.get("run_id")
        self.previous_run_id = prior_run_id if isinstance(prior_run_id, str) else None

    def __call__(self, attempt: Any) -> AttemptEvidence:
        result = _read_last_cycle_result(self.data_dir)
        run_id = result.get("run_id")
        if not isinstance(run_id, str) or not run_id or run_id == self.previous_run_id:
            return AttemptEvidence(False)
        self.previous_run_id = run_id
        expected_source = (
            "built-in" if attempt.source == "built-in" else "verified-release"
        )
        release_id = result.get("release_id")
        execution_source = result.get("execution_source")
        success = result.get("success")
        fault = result.get("injected_noaa_fault")
        used_cached = result.get("used_cached_data")
        valid = bool(
            result.get("success") is True
            and release_id == attempt.source
            and execution_source == expected_source
        )
        return AttemptEvidence(
            valid=valid,
            run_id=run_id,
            result_success=success if isinstance(success, bool) else None,
            release_id=release_id if isinstance(release_id, str) else None,
            execution_source=(
                execution_source if isinstance(execution_source, str) else None
            ),
            injected_noaa_fault=fault if isinstance(fault, str) else None,
            used_cached_data=used_cached if isinstance(used_cached, bool) else None,
        )


def _persist_supervisor_evidence(
    data_dir: Path,
    supervisor_result: Any,
    *,
    expected_release_id: str,
    expected_execution_source: str,
) -> Mapping[str, Any]:
    attempts: list[dict[str, Any]] = []
    for attempt in supervisor_result.attempts:
        attempt_evidence = attempt.evidence
        attempts.append(
            {
                "release_id": attempt.source,
                "execution_source": (
                    "built-in" if attempt.source == "built-in" else "verified-release"
                ),
                "returncode": attempt.returncode,
                "evidence_valid": (
                    attempt_evidence.valid if attempt_evidence is not None else None
                ),
                "run_id": (
                    attempt_evidence.run_id if attempt_evidence is not None else None
                ),
                "result_success": (
                    attempt_evidence.result_success
                    if attempt_evidence is not None
                    else None
                ),
                "injected_noaa_fault": (
                    attempt_evidence.injected_noaa_fault
                    if attempt_evidence is not None
                    else None
                ),
                "used_cached_data": (
                    attempt_evidence.used_cached_data
                    if attempt_evidence is not None
                    else None
                ),
            }
        )
    selected_attempt = attempts[-1]
    last_result = _read_last_cycle_result(data_dir)
    selected_run_id_value = last_result.get("run_id")
    selected_run_id = (
        selected_run_id_value if isinstance(selected_run_id_value, str) else None
    )
    selected_release_id = str(selected_attempt["release_id"])
    selected_execution_source = str(selected_attempt["execution_source"])
    identity_matches = (
        last_result.get("release_id") == selected_release_id
        and last_result.get("execution_source") == selected_execution_source
    )
    selected_success = bool(
        selected_attempt["returncode"] == 0
        and selected_attempt["evidence_valid"] is not False
        and last_result.get("success") is True
        and identity_matches
    )
    fallback_used = len(attempts) > 1
    expected_succeeded = bool(
        not fallback_used
        and attempts[0]["release_id"] == expected_release_id
        and attempts[0]["execution_source"] == expected_execution_source
        and attempts[0]["returncode"] == 0
        and attempts[0]["evidence_valid"] is not False
        and selected_success
    )
    execution = {
        "expected_release_id": expected_release_id,
        "expected_execution_source": expected_execution_source,
        "attempts": attempts,
        "trial_confirmed": bool(supervisor_result.trial_confirmed),
        "trial_rolled_back": bool(supervisor_result.trial_rolled_back),
        "hardware_ownership_released": bool(
            supervisor_result.hardware_ownership_released
        ),
        "fallback_used": fallback_used,
        "selected_run_id": selected_run_id,
        "selected_release_id": selected_release_id,
        "selected_execution_source": selected_execution_source,
        "selected_success": selected_success,
        "expected_release_executed_successfully": expected_succeeded,
        "noaa_fault_kind": next(
            (
                item["injected_noaa_fault"]
                for item in attempts
                if item["release_id"] == expected_release_id
                and item["injected_noaa_fault"] is not None
            ),
            last_result.get("noaa_fault_kind"),
        ),
        "injected_noaa_fault": next(
            (
                item["injected_noaa_fault"]
                for item in attempts
                if item["release_id"] == expected_release_id
                and item["injected_noaa_fault"] is not None
            ),
            last_result.get("injected_noaa_fault"),
        ),
        "used_cached_data": next(
            (
                item["used_cached_data"]
                for item in attempts
                if item["release_id"] == expected_release_id
                and item["used_cached_data"] is not None
            ),
            last_result.get("used_cached_data"),
        ),
        "production_route_interface": last_result.get("production_route_interface"),
        "wifi_active_observed_seconds": last_result.get(
            "wifi_active_observed_seconds"
        ),
        "wifi_active_observation_source": last_result.get(
            "wifi_active_observation_source"
        ),
    }
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "event": "release_supervision",
        "execution": execution,
    }
    atomic_write_json(data_dir / "state" / "supervisor" / "last_execution.json", evidence)
    safe_run_id = (
        selected_run_id
        if selected_run_id is not None
        and 1 <= len(selected_run_id) <= 64
        and all(char.isascii() and (char.isalnum() or char in "-_") for char in selected_run_id)
        else "unattributed"
    )
    atomic_write_json(
        data_dir / "telemetry" / "supervisor" / f"{safe_run_id}.json",
        evidence,
    )
    prune_regular_files(data_dir / "telemetry" / "supervisor", "*.json", keep=64)
    return evidence


def _supervisor_result_payload(result: SupervisorResult) -> dict[str, Any]:
    return {
        "returncode": result.returncode,
        "trial_confirmed": result.trial_confirmed,
        "trial_rolled_back": result.trial_rolled_back,
        "hardware_ownership_released": result.hardware_ownership_released,
        "attempts": [
            {
                "source": attempt.source,
                "returncode": attempt.returncode,
                "evidence": (
                    None
                    if attempt.evidence is None
                    else {
                        "valid": attempt.evidence.valid,
                        "run_id": attempt.evidence.run_id,
                        "result_success": attempt.evidence.result_success,
                        "release_id": attempt.evidence.release_id,
                        "execution_source": attempt.evidence.execution_source,
                        "injected_noaa_fault": attempt.evidence.injected_noaa_fault,
                        "used_cached_data": attempt.evidence.used_cached_data,
                    }
                ),
            }
            for attempt in result.attempts
        ],
    }


def _supervisor_result_from_payload(value: object) -> SupervisorResult:
    if not isinstance(value, dict):
        raise ValueError("supervisor result payload must be an object")
    raw_attempts = value.get("attempts")
    if not isinstance(raw_attempts, list) or not raw_attempts:
        raise ValueError("supervisor result payload has no attempts")
    attempts: list[ReleaseAttempt] = []
    for raw_attempt in raw_attempts:
        if not isinstance(raw_attempt, dict):
            raise ValueError("supervisor result attempt is invalid")
        source = raw_attempt.get("source")
        returncode = raw_attempt.get("returncode")
        if (
            not isinstance(source, str)
            or not source
            or isinstance(returncode, bool)
            or not isinstance(returncode, int)
        ):
            raise ValueError("supervisor result attempt identity is invalid")
        raw_evidence = raw_attempt.get("evidence")
        if raw_evidence is None:
            evidence = None
        elif isinstance(raw_evidence, dict) and isinstance(raw_evidence.get("valid"), bool):
            evidence = AttemptEvidence(
                valid=raw_evidence["valid"],
                run_id=raw_evidence.get("run_id"),
                result_success=raw_evidence.get("result_success"),
                release_id=raw_evidence.get("release_id"),
                execution_source=raw_evidence.get("execution_source"),
                injected_noaa_fault=raw_evidence.get("injected_noaa_fault"),
                used_cached_data=raw_evidence.get("used_cached_data"),
            )
        else:
            raise ValueError("supervisor result attempt evidence is invalid")
        attempts.append(ReleaseAttempt(source, returncode, evidence))

    returncode = value.get("returncode")
    trial_confirmed = value.get("trial_confirmed")
    trial_rolled_back = value.get("trial_rolled_back")
    hardware_ownership_released = value.get("hardware_ownership_released")
    if (
        isinstance(returncode, bool)
        or not isinstance(returncode, int)
        or not isinstance(trial_confirmed, bool)
        or not isinstance(trial_rolled_back, bool)
        or not isinstance(hardware_ownership_released, bool)
    ):
        raise ValueError("supervisor result status is invalid")
    return SupervisorResult(
        returncode,
        tuple(attempts),
        trial_confirmed=trial_confirmed,
        trial_rolled_back=trial_rolled_back,
        hardware_ownership_released=hardware_ownership_released,
    )


def _persist_supervisor_payload_file(
    payload_path: Path,
    data_dir: Path,
    expected_release_id: str,
    expected_execution_source: str,
) -> None:
    try:
        raw = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("supervisor evidence payload is unreadable") from exc
    result = _supervisor_result_from_payload(raw)
    _persist_supervisor_evidence(
        data_dir,
        result,
        expected_release_id=expected_release_id,
        expected_execution_source=expected_execution_source,
    )


def _persist_supervisor_evidence_bounded(
    data_dir: Path,
    result: SupervisorResult,
    *,
    expected_release_id: str,
    expected_execution_source: str,
    environment: Mapping[str, str],
    simulate: bool,
) -> None:
    runtime_root = (
        Path(tempfile.gettempdir())
        if simulate
        else _environment_path("TIDETRACKER_RUNTIME_DIR", Path("/run/tidetracker"))
    )
    runtime_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, raw_payload = tempfile.mkstemp(
        prefix="supervisor-evidence-", suffix=".json", dir=runtime_root
    )
    payload_path = Path(raw_payload)
    try:
        payload = (
            json.dumps(
                _supervisor_result_payload(result),
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("short supervisor payload write")
            written += count
        os.close(descriptor)
        descriptor = -1
        command = [
            sys.executable,
            "-c",
            (
                "import pathlib,sys;"
                "from tidetracker.cli import _persist_supervisor_payload_file;"
                "_persist_supervisor_payload_file(pathlib.Path(sys.argv[1]),"
                "pathlib.Path(sys.argv[2]),sys.argv[3],sys.argv[4])"
            ),
            str(payload_path),
            str(data_dir),
            expected_release_id,
            expected_execution_source,
        ]
        code = run_isolated_command(
            command,
            cwd=Path.cwd(),
            env=environment,
            timeout=15.0,
        )
        if code != 0:
            raise HardwareOwnershipError("supervisor evidence persistence failed")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(OSError):
            payload_path.unlink()


def _release_trial_timeout(config: AppConfig, mode: CycleMode) -> float:
    lifecycle_budget = (
        config.timeouts.setup_session_seconds
        if mode is CycleMode.SETUP
        else config.timeouts.normal_cycle_seconds
    )
    return lifecycle_budget + RELEASE_TRIAL_GRACE_SECONDS


def _cycle_command(args: argparse.Namespace) -> int:
    args.simulate = bool(args.simulate or _environment_flag("TIDETRACKER_SIMULATE"))
    args.simulate_offline = bool(
        args.simulate_offline or _environment_flag("TIDETRACKER_SIMULATE_OFFLINE")
    )
    if args.builtin:
        if args.defer_done and not _environment_flag("TIDETRACKER_SUPERVISED"):
            raise ValueError("deferred DONE is reserved for the release supervisor")
        code = _run_builtin_cycle(args, defer_done=args.defer_done)
        return 0 if args.service else code

    paths = _runtime_paths(args)
    public_key = Path(
        args.public_key
        or _environment_path("TIDETRACKER_RELEASE_PUBLIC_KEY", DEFAULT_PUBLIC_KEY)
    )
    updater = ReleaseUpdater(paths.data, OpenSSLEd25519Verifier(public_key))
    status_before = updater.status()
    expected_value = status_before.get("current")
    expected_release_id = expected_value if isinstance(expected_value, str) else "built-in"
    expected_execution_source = (
        "verified-release" if expected_release_id != "built-in" else "built-in"
    )
    config = _load_or_create_config(paths.config)
    release_environment = dict(os.environ)
    release_environment["TIDETRACKER_DATA_DIR"] = str(paths.data)
    release_environment["TIDETRACKER_CONFIG"] = str(paths.config)
    if paths.assets is not None:
        release_environment["TIDETRACKER_ASSET_DIR"] = str(paths.assets)
    if args.display_output is not None:
        release_environment["TIDETRACKER_DISPLAY_OUTPUT"] = str(paths.simulated_display)
    if args.simulate:
        release_environment["TIDETRACKER_SIMULATE"] = "1"
        release_environment["TIDETRACKER_SIMULATE_MODE"] = args.mode
    if args.simulate_offline:
        release_environment["TIDETRACKER_SIMULATE_OFFLINE"] = "1"
    if args.simulate:
        supervised_mode = CycleMode(args.mode)
    else:
        try:
            with GpioModeReader() as mode_reader:
                supervised_mode = mode_reader.read_mode()
        except Exception:
            # Failing safe here means retaining enough budget for setup mode;
            # the child still reads BCM16 independently and supplies evidence.
            supervised_mode = CycleMode.SETUP
    executor = SubprocessReleaseExecutor(
        timeout_seconds=_release_trial_timeout(config, supervised_mode),
        environment=release_environment,
    )
    supervisor = ReleaseSupervisor(
        updater,
        executor,
        evidence_validator=_FreshCycleEvidenceValidator(paths.data),
    )
    result = None
    try:
        result = supervisor.run(
            lambda: _run_supervised_builtin(
                args,
                environment=release_environment,
                timeout_seconds=_release_trial_timeout(config, supervised_mode),
            )
        )
        if result.hardware_ownership_released:
            _persist_supervisor_evidence_bounded(
                paths.data,
                result,
                expected_release_id=expected_release_id,
                expected_execution_source=expected_execution_source,
                environment=release_environment,
                simulate=args.simulate,
            )
    finally:
        _signal_outer_done(args.simulate)
    assert result is not None
    return 0 if args.service else result.returncode


def _portal_command(args: argparse.Namespace) -> int:
    paths = _runtime_paths(args)
    config = _load_or_create_config(paths.config)
    timeout = args.timeout or config.timeouts.setup_session_seconds
    if timeout <= 0:
        raise ValueError("portal timeout must be positive")
    if args.simulate or _environment_flag("TIDETRACKER_SIMULATE"):
        application = PortalApplication(
            _SimulatedPortalNetwork(),
            AtomicJSONConfigStore(paths.config, config),
        )
        server = PortalServer(application, (args.host, args.port))
        try:
            server.serve_for(timeout)
        finally:
            server.close()
        return 0

    network = NetworkManagerAdapter()
    application = PortalApplication(network, AtomicJSONConfigStore(paths.config, config))
    port = PortalNetworkPort(
        network,
        application,
        hotspot_ssid=args.hotspot_ssid,
        gateway=args.gateway,
        port=args.port,
    )
    try:
        status = port.prepare(CycleMode.SETUP, timeout_seconds=timeout)
    finally:
        application.close()
    return 0 if isinstance(status, NetworkStatus) else 1


def _diagnose_command(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir or _environment_path("TIDETRACKER_DATA_DIR", DEFAULT_DATA_DIR))
    value = collect_diagnostics(data_dir=data_dir)
    if args.json:
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
    else:
        print(json.dumps(value, sort_keys=True, indent=2))
    return 0


def _display_oracle_command(args: argparse.Namespace) -> int:
    if args.timeout <= 0 or args.timeout > 180:
        raise ValueError("display-oracle timeout must be between 0 and 180 seconds")
    expected = args.sha256.strip().lower()
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise ValueError("display-oracle SHA-256 must be 64 lowercase hex characters")
    actual = hashlib.sha256(args.bitmap.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError("display-oracle bitmap SHA-256 mismatch")
    framebuffer = legacy_bitmap_to_framebuffer(args.bitmap)
    if args.physical:
        if not _environment_flag("TIDETRACKER_HIL_DISPLAY_ORACLE"):
            raise ValueError("physical display-oracle requires the HIL guard environment")
        if getattr(os, "geteuid", lambda: -1)() != 0:
            raise PermissionError("physical display-oracle requires the root HIL helper")
        display: Any = WaveshareDisplay()
        output: str | None = None
    else:
        output_path = args.display_output or args.bitmap.with_suffix(".controller.bin")
        display = FileDisplay(output_path)
        output = str(output_path)
    display.display(framebuffer, timeout_seconds=args.timeout)
    print(
        json.dumps(
            {
                "bitmap_sha256": actual,
                "framebuffer_sha256": hashlib.sha256(framebuffer).hexdigest(),
                "output": output,
                "physical": bool(args.physical),
                "refresh_count": int(getattr(display, "refresh_count", 0)),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def _add_runtime_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--asset-dir", type=Path)
    parser.add_argument("--display-output", type=Path)


def _runtime_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tidetracker")
    commands = parser.add_subparsers(dest="command", required=True)

    cycle = commands.add_parser("cycle", help="run one bounded lifecycle")
    _add_runtime_paths(cycle)
    cycle.add_argument("--simulate", action="store_true")
    cycle.add_argument("--simulate-offline", action="store_true")
    cycle.add_argument(
        "--mode",
        choices=[item.value for item in CycleMode],
        default=os.environ.get("TIDETRACKER_SIMULATE_MODE", "run"),
    )
    cycle.add_argument("--public-key", type=Path)
    cycle.add_argument("--service", action="store_true")
    cycle.add_argument("--builtin", action="store_true", help=argparse.SUPPRESS)
    cycle.add_argument("--defer-done", action="store_true", help=argparse.SUPPRESS)
    cycle.set_defaults(handler=_cycle_command)

    portal = commands.add_parser("portal", help="run a bounded setup portal")
    _add_runtime_paths(portal)
    portal.add_argument("--simulate", action="store_true")
    portal.add_argument("--host", default="127.0.0.1")
    portal.add_argument("--gateway", default="192.168.42.1")
    portal.add_argument("--port", type=int, default=80)
    portal.add_argument("--timeout", type=float)
    portal.add_argument("--hotspot-ssid", default="TideTracker-Setup")
    portal.set_defaults(handler=_portal_command)

    diagnose = commands.add_parser("diagnose", help="collect bounded read-only diagnostics")
    diagnose.add_argument("--data-dir", type=Path)
    diagnose.add_argument("--json", action="store_true")
    diagnose.set_defaults(handler=_diagnose_command)

    oracle = commands.add_parser(
        "display-oracle", help="refresh one hash-checked 800x480 HIL bitmap"
    )
    oracle.add_argument("--bitmap", required=True, type=Path)
    oracle.add_argument("--sha256", required=True)
    oracle.add_argument("--display-output", type=Path)
    oracle.add_argument("--timeout", type=float, default=120.0)
    oracle.add_argument("--physical", action="store_true")
    oracle.set_defaults(handler=_display_oracle_command)
    return parser


def _print_error(error: BaseException) -> None:
    message = redact_text(" ".join(str(error).split()))[:500] or type(error).__name__
    print(json.dumps({"error": type(error).__name__, "message": message}), file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _runtime_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        _print_error(exc)
        return 2


def _update_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tidetracker-update")
    parser.add_argument(
        "--root",
        type=Path,
        default=_environment_path("TIDETRACKER_UPDATE_ROOT", DEFAULT_DATA_DIR),
    )
    parser.add_argument(
        "--public-key",
        type=Path,
        default=_environment_path("TIDETRACKER_RELEASE_PUBLIC_KEY", DEFAULT_PUBLIC_KEY),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("rollback")
    abort = commands.add_parser("abort")
    abort.add_argument("release_id", nargs="?")
    confirm = commands.add_parser("confirm")
    confirm.add_argument("release_id", nargs="?")
    for command in ("begin", "verify", "activate"):
        child = commands.add_parser(command)
        child.add_argument("release_id")
    stage = commands.add_parser("stage")
    stage.add_argument("release_id", nargs="?")
    source = stage.add_mutually_exclusive_group()
    source.add_argument("--source", type=Path)
    source.add_argument("--bundle", type=Path)

    build = commands.add_parser("build-bundle")
    build.add_argument("--release-id", required=True)
    build_source = build.add_mutually_exclusive_group(required=True)
    build_source.add_argument("--source", type=Path)
    build_source.add_argument("--project-root", type=Path)
    build.add_argument("--entrypoint")
    build.add_argument("--private-key", required=True, type=Path)
    build.add_argument("--output", required=True, type=Path)
    return parser


def _remove_uploaded_archive(updater: ReleaseUpdater, bundle: Path) -> None:
    updater.remove_uploaded_archive(bundle)


def _stage_archive(updater: ReleaseUpdater, bundle: Path) -> dict[str, object]:
    release_id: str | None = None
    try:
        # Extraction is scratch work. Keep it on the OS temporary filesystem
        # (RAM-backed on the field image) so a power cut cannot accumulate
        # abandoned trees on TIDEDATA.
        with tempfile.TemporaryDirectory(prefix="tidetracker-bundle-") as raw_temp:
            extracted = Path(raw_temp) / "release"
            manifest = extract_bundle(bundle, extracted)
            release_id = manifest.release_id
            updater.validate_directory(release_id, extracted)
            status = updater.status()
            verified_ids = status.get("verified")
            if not isinstance(verified_ids, list):
                raise ValueError("release state has an invalid verified history")
            if release_id in verified_ids:
                existing_manifest = updater.releases / release_id / "manifest.json"
                if existing_manifest.read_bytes() != (extracted / "manifest.json").read_bytes():
                    raise ValueError("release ID is already verified with different contents")
                if status["current"] == release_id:
                    path = updater.releases / release_id
                    state = "trial" if status["trial"] == release_id else "current"
                    return {
                        "state": state,
                        "release_id": release_id,
                        "path": str(path),
                        "bundle": str(bundle),
                    }
                path = updater.activate(release_id)
                return {
                    "state": "trial",
                    "release_id": release_id,
                    "path": str(path),
                    "bundle": str(bundle),
                }
            active_upload = status["in_progress"]
            if active_upload == release_id:
                moved = updater.releases / release_id
                incoming = updater.incoming / release_id
                if moved.is_dir() and not incoming.exists():
                    updater.verify(release_id)
                    path = updater.activate(release_id)
                    return {
                        "state": "trial",
                        "release_id": release_id,
                        "path": str(path),
                        "bundle": str(bundle),
                    }
                updater.abort(release_id)
            elif active_upload is not None:
                raise ValueError("another release upload is in progress; abort it before staging")
            updater.begin(release_id)
            updater.stage(release_id, extracted)
            verified = updater.verify(release_id)
            path = updater.activate(release_id)
        result: dict[str, object] = {
            "state": "trial",
            "release_id": verified.release_id,
            "path": str(path),
            "bundle": str(bundle),
        }
        return result
    except BaseException:
        if release_id is not None:
            with suppress(Exception):
                status = updater.status()
                if status.get("in_progress") == release_id:
                    moved = updater.releases / release_id
                    incoming = updater.incoming / release_id
                    if moved.is_dir() and not incoming.exists():
                        updater.verify(release_id)
                    else:
                        updater.abort(release_id)
        raise
    finally:
        _remove_uploaded_archive(updater, bundle)


def _update_command(args: argparse.Namespace) -> Mapping[str, object]:
    if args.command == "build-bundle":
        source_root = (args.source or args.project_root).resolve()
        private_key = args.private_key.resolve()
        if args.source is not None and private_key.is_relative_to(source_root):
            raise ValueError("release private key must be outside the payload source")
        if args.output.resolve() == private_key:
            raise ValueError("bundle output must not overwrite the release private key")
        signer = OpenSSLEd25519Signer(args.private_key)
        builder = DeterministicBundleBuilder(signer)
        if args.project_root is not None:
            if args.entrypoint is not None:
                raise ValueError("--entrypoint is generated automatically with --project-root")
            with tempfile.TemporaryDirectory(prefix="tidetracker-project-payload-") as raw_temp:
                payload = Path(raw_temp) / "payload"
                assemble_project_payload(args.project_root, payload)
                built = builder.build(
                    payload,
                    args.output,
                    release_id=args.release_id,
                    entrypoint=PROJECT_ENTRYPOINT,
                )
        else:
            if args.entrypoint is None:
                raise ValueError("--entrypoint is required with --source")
            built = builder.build(
                args.source,
                args.output,
                release_id=args.release_id,
                entrypoint=args.entrypoint,
            )
        return {
            "state": "built",
            "release_id": built.release_id,
            "path": str(built.path),
            "size": built.size,
            "sha256": built.sha256,
        }

    updater = ReleaseUpdater(args.root, OpenSSLEd25519Verifier(args.public_key))
    if args.command == "stage" and args.bundle is not None:
        if args.release_id is not None:
            raise ValueError("release ID is read from --bundle and must not be supplied")
        result = _stage_archive(updater, args.bundle)
        bounded_system_sync()
        return result
    if args.command == "stage" and args.release_id is None:
        raise ValueError("stage requires a release ID or --bundle")
    service = ReleaseCommandService(updater)
    result = service.execute(
        args.command,
        release_id=getattr(args, "release_id", None),
        source=getattr(args, "source", None),
    )
    if args.command != "status":
        bounded_system_sync()
    return result


def update_main(argv: Sequence[str] | None = None) -> int:
    parser = _update_parser()
    args = parser.parse_args(argv)
    try:
        result = _update_command(args)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        _print_error(exc)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


__all__ = ["main", "update_main"]
