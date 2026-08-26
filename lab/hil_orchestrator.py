#!/usr/bin/env python3
"""Closed-loop TideTracker Pi Zero W HIL orchestration.

The default invocation is a non-mutating plan. Real hardware actions require
--execute. --simulate exercises the orchestration and artifact paths without
network, serial, GPIO, or camera access.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import pathlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol


class HilError(RuntimeError):
    """A release-gating HIL failure."""


class AuthorizationGuard:
    """Fail closed when a signed real-HIL authorization window ends."""

    def __init__(
        self,
        valid_until: datetime | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if valid_until is not None and valid_until.tzinfo is None:
            raise HilError("signed safety authorization expiry must include a timezone")
        self.valid_until = valid_until
        self._clock = clock or (lambda: datetime.now(UTC))

    @classmethod
    def from_safety_gate(
        cls,
        gate: dict[str, Any],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> AuthorizationGuard:
        try:
            valid_until = datetime.fromisoformat(str(gate["valid_until"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError) as exc:
            raise HilError("signed safety authorization expiry is invalid") from exc
        return cls(valid_until, clock=clock)

    def require(self, action: str) -> None:
        if self.valid_until is None:
            return
        now = self._clock()
        if now.tzinfo is None:
            raise HilError("authorization clock returned a timestamp without a timezone")
        if now >= self.valid_until:
            raise HilError(f"signed safety authorization expired before {action}")

    def remaining_seconds(self, action: str) -> float | None:
        self.require(action)
        if self.valid_until is None:
            return None
        now = self._clock()
        if now.tzinfo is None:
            raise HilError("authorization clock returned a timestamp without a timezone")
        remaining = (self.valid_until - now).total_seconds()
        if remaining <= 0:
            raise HilError(f"signed safety authorization expired before {action}")
        return remaining

    def bounded_timeout(self, requested_seconds: float, action: str) -> float:
        remaining = self.remaining_seconds(action)
        if remaining is None:
            return requested_seconds
        return min(requested_seconds, remaining)

    def require_bounded(self, owner: str) -> None:
        if self.valid_until is None:
            raise HilError(f"real {owner} requires a bounded signed safety authorization")
        self.require(owner)


DEFAULT_SAFETY_PUBLIC_KEY = pathlib.Path("/etc/tidetracker/lab-safety-approver.pub.pem")
DEFAULT_EXECUTION_LOCK = pathlib.Path("/var/lib/tidetracker/controller/hil.lock")
DEFAULT_ARTIFACT_ROOT = pathlib.Path("/var/lib/tidetracker/controller/hil-results")
CONTROLLER_MACHINE_ID_PATH = pathlib.Path("/etc/machine-id")
CONTROLLER_DATA_UUID = "54540000-0000-4000-8000-000000000001"
FIXED_OPENSSL = pathlib.Path("/usr/bin/openssl")
FIXED_FINDMNT = pathlib.Path("/usr/bin/findmnt")
MAX_UART_LOG_BYTES = 4 * 1024 * 1024
MAX_SECRET_SCAN_INPUT_BYTES = 64 * 1024
MAX_SECRET_SCAN_VALUES = 32
MAX_TOOL_FILE_BYTES = 128 * 1024 * 1024
MAX_SUPPORT_FILE_BYTES = 16 * 1024 * 1024
MAX_SUPPORT_FILES = 16

HARDWARE_INVENTORY_COMPONENTS = frozenset(
    {
        "usb_data_isolator",
        "switched_power_fixture",
        "uart_adapter",
        "camera",
        "display",
        "tpl5110_power_timer",
        "target_pi_zero",
        "target_sd_card",
    }
)
TOOL_IDENTITY_NAMES = frozenset(
    {"camera_capture", "camera_validator", "usb_disconnected_probe"}
)

APPROVED_OPERATIONS = frozenset(
    {
        "cycles",
        "release_stage",
        "wifi_matrix",
        "lifecycle_power_cuts",
        "update_power_cuts",
        "field_cycle",
    }
)
UPDATE_PHASES = ("begin", "stage", "verify", "activate")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _validate_secret_scan_values(secrets: tuple[str, ...]) -> tuple[str, ...]:
    if (
        not 1 <= len(secrets) <= MAX_SECRET_SCAN_VALUES
        or len(secrets) != len(set(secrets))
        or secrets != tuple(sorted(secrets))
    ):
        raise HilError("target secret scan input is invalid")
    for value in secrets:
        if (
            not isinstance(value, str)
            or any(character in value for character in "\x00\r\n")
            or not 1 <= len(value.encode("utf-8")) <= 512
        ):
            raise HilError("target secret scan input is invalid")
    return secrets


def _private_value_patterns(values: Iterable[str]) -> frozenset[bytes]:
    patterns: set[bytes] = set()
    for value in values:
        if not value:
            continue
        patterns.add(value.encode("utf-8"))
        for ensure_ascii in (False, True):
            serialized = json.dumps(value, ensure_ascii=ensure_ascii)
            patterns.add(serialized.encode("utf-8"))
            patterns.add(serialized[1:-1].encode("utf-8"))
    return frozenset(patterns)


def write_json(path: pathlib.Path, value: Any) -> None:
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = pathlib.Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        if os.name != "nt":
            parent = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_regular_file_identity(
    path: pathlib.Path,
    *,
    label: str,
    maximum_bytes: int,
) -> dict[str, Any]:
    """Hash one stable non-symlink file snapshot and retain its canonical identity."""

    if not path.is_absolute():
        raise HilError(f"{label} path must be absolute")
    try:
        before_path = path.lstat()
    except OSError as exc:
        raise HilError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(before_path.st_mode) or not stat.S_ISREG(before_path.st_mode):
        raise HilError(f"{label} must be a regular, non-symlink file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HilError(f"cannot open {label}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > maximum_bytes
            or (before.st_dev, before.st_ino) != (before_path.st_dev, before_path.st_ino)
        ):
            raise HilError(f"{label} is empty, changed, or exceeds its size limit")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise HilError(f"{label} exceeds its size limit")
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        after_path = path.lstat()
    except OSError as exc:
        raise HilError(f"{label} disappeared while it was hashed") from exc
    stable_fields = (
        ("st_size", "st_mtime_ns")
        if os.name == "nt"
        else ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    )
    if (
        total != before.st_size
        or stat.S_ISLNK(after_path.st_mode)
        or any(getattr(before, name) != getattr(after, name) for name in stable_fields)
        or any(getattr(before, name) != getattr(after_path, name) for name in stable_fields)
    ):
        raise HilError(f"{label} changed while it was hashed")
    return {
        "path": str(path),
        "sha256": digest.hexdigest(),
        "size_bytes": total,
    }


def _resolve_executable(value: str, label: str) -> pathlib.Path:
    located = shutil.which(value)
    candidate = pathlib.Path(located if located is not None else value)
    if not candidate.is_absolute():
        raise HilError(f"{label} executable cannot be resolved to an absolute path")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise HilError(f"{label} executable is unavailable") from exc
    if not resolved.is_absolute() or resolved.is_symlink() or not os.access(resolved, os.X_OK):
        raise HilError(f"{label} executable is not an executable regular file")
    return resolved


def validate_hardware_inventory(value: Any, *, label: str) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict) or set(value) != HARDWARE_INVENTORY_COMPONENTS:
        raise HilError(f"{label} must identify every required lab hardware component")
    normalized: dict[str, dict[str, str]] = {}
    for component in sorted(HARDWARE_INVENTORY_COMPONENTS):
        identity = value.get(component)
        if not isinstance(identity, dict) or set(identity) != {"model", "serial_or_asset_id"}:
            raise HilError(f"{label}.{component} must contain exact model and serial_or_asset_id")
        model = identity.get("model")
        serial = identity.get("serial_or_asset_id")
        if not isinstance(model, str) or not isinstance(serial, str):
            raise HilError(f"{label}.{component} contains an invalid model or asset identity")
        if any(
            item != item.strip()
            or not 1 <= len(item) <= 128
            or any(ord(character) < 0x20 for character in item)
            for item in (model, serial)
        ):
            raise HilError(f"{label}.{component} contains an invalid model or asset identity")
        normalized[component] = {"model": model, "serial_or_asset_id": serial}
    return normalized


@dataclass(frozen=True)
class BoundCommand:
    """A command whose executable, argv, and declared dependencies are content-bound."""

    canonical_argv: tuple[str, ...]
    support_paths: tuple[pathlib.Path, ...]
    identity: dict[str, Any]
    label: str

    @classmethod
    def create(
        cls,
        command: Any,
        support_files: Any,
        *,
        label: str,
        require_output: bool,
    ) -> BoundCommand:
        if (
            not isinstance(command, list)
            or not command
            or not all(
                isinstance(part, str) and part and "\x00" not in part for part in command
            )
            or (require_output and all("{output}" not in part for part in command))
        ):
            suffix = " containing {output}" if require_output else ""
            raise HilError(f"{label} must be a non-empty argv array{suffix}")
        if (
            not isinstance(support_files, list)
            or len(support_files) > MAX_SUPPORT_FILES
            or not all(isinstance(item, str) and item and "\x00" not in item for item in support_files)
        ):
            raise HilError(f"{label}_support_files must be an explicit bounded path array")
        executable = _resolve_executable(command[0], label)
        canonical_argv = (str(executable), *command[1:])
        support_paths: list[pathlib.Path] = []
        support_identities: list[dict[str, Any]] = []
        for index, raw_path in enumerate(support_files):
            path = pathlib.Path(raw_path)
            if not path.is_absolute():
                raise HilError(f"{label} support file paths must be absolute")
            try:
                resolved = path.resolve(strict=True)
            except OSError as exc:
                raise HilError(f"{label} support file {index} is unavailable") from exc
            if resolved != path or path.is_symlink():
                raise HilError(f"{label} support files must use canonical non-symlink paths")
            support_paths.append(path)
            support_identities.append(
                _stable_regular_file_identity(
                    path,
                    label=f"{label} support file {index}",
                    maximum_bytes=MAX_SUPPORT_FILE_BYTES,
                )
            )
        if len(set(support_paths)) != len(support_paths):
            raise HilError(f"{label} repeats a support file")
        declared_support = set(support_paths)
        argv_file_dependencies: set[pathlib.Path] = set()
        for part in command[1:]:
            if "{output}" in part:
                continue
            raw_candidate = part
            if part.startswith("-") and "=" in part:
                raw_candidate = part.split("=", 1)[1]
            elif part.startswith("@"):
                raw_candidate = part[1:]
            elif part.startswith("-"):
                continue
            if "://" in raw_candidate:
                continue
            candidate = pathlib.Path(raw_candidate)
            if not candidate.is_absolute():
                relative_target = pathlib.Path.cwd() / candidate
                existing_regular = False
                with suppress(OSError):
                    existing_regular = stat.S_ISREG(relative_target.lstat().st_mode)
                path_like = (
                    existing_regular
                    or candidate.suffix != ""
                    or "/" in raw_candidate
                    or "\\" in raw_candidate
                    or raw_candidate.startswith(".")
                )
                if path_like:
                    raise HilError(
                        f"{label} file arguments must be canonical absolute support files"
                    )
                continue
            try:
                candidate_metadata = candidate.lstat()
            except OSError:
                continue
            if stat.S_ISREG(candidate_metadata.st_mode):
                argv_file_dependencies.add(candidate)
                if candidate not in declared_support:
                    raise HilError(
                        f"{label} argv file dependency is missing from its support_files"
                    )
        executable_name = executable.name.lower()
        interpreter = (
            executable_name.startswith(("python", "pypy"))
            or executable_name
            in {"sh", "bash", "dash", "zsh", "perl", "ruby", "node", "busybox"}
        )
        if interpreter and (
            not argv_file_dependencies
            or any(part in {"-c", "-m", "-e"} for part in command[1:])
        ):
            raise HilError(
                f"{label} interpreter commands require a direct absolute script in support_files"
            )
        executable_identity = _stable_regular_file_identity(
            executable,
            label=f"{label} executable",
            maximum_bytes=MAX_TOOL_FILE_BYTES,
        )
        identity = {
            "canonical_argv": list(canonical_argv),
            "executable_path": executable_identity["path"],
            "executable_sha256": executable_identity["sha256"],
            "executable_size_bytes": executable_identity["size_bytes"],
            "support_files": support_identities,
        }
        return cls(canonical_argv, tuple(support_paths), identity, label)

    def assert_current(self) -> None:
        current_executable = _stable_regular_file_identity(
            pathlib.Path(self.canonical_argv[0]),
            label=f"{self.label} executable",
            maximum_bytes=MAX_TOOL_FILE_BYTES,
        )
        current = {
            "canonical_argv": list(self.canonical_argv),
            "executable_path": current_executable["path"],
            "executable_sha256": current_executable["sha256"],
            "executable_size_bytes": current_executable["size_bytes"],
            "support_files": [
                _stable_regular_file_identity(
                    path,
                    label=f"{self.label} support file {index}",
                    maximum_bytes=MAX_SUPPORT_FILE_BYTES,
                )
                for index, path in enumerate(self.support_paths)
            ],
        }
        if current != self.identity:
            raise HilError(f"{self.label} identity changed after safety-gate verification")


def snapshot_controller_machine_id(
    path: pathlib.Path = CONTROLLER_MACHINE_ID_PATH,
) -> str:
    """Hash one bounded snapshot of the live controller's root-owned machine ID."""

    if path.is_symlink():
        raise HilError("controller /etc/machine-id must not be a symbolic link")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HilError("controller /etc/machine-id is unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size < 32
            or before.st_size > 64
            or (hasattr(os, "geteuid") and before.st_uid != 0)
        ):
            raise HilError("controller /etc/machine-id must be a bounded root-owned regular file")
        chunks: list[bytes] = []
        remaining = 65
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(payload) != before.st_size
            or after.st_size != before.st_size
            or getattr(after, "st_mtime_ns", None) != getattr(before, "st_mtime_ns", None)
        ):
            raise HilError("controller /etc/machine-id changed while it was snapshotted")
    finally:
        os.close(descriptor)
    if re.fullmatch(rb"[0-9a-f]{32}\n?", payload) is None:
        raise HilError("controller /etc/machine-id has an invalid format")
    return hashlib.sha256(payload).hexdigest()


class HilExecutionLock:
    """Controller-wide advisory lock held across every possible hardware action."""

    def __init__(self, path: pathlib.Path, diagnostics: dict[str, Any]) -> None:
        self.path = path
        self.diagnostics = diagnostics
        self._stream: Any = None

    def acquire(self) -> None:
        if self._stream is not None:
            raise HilError("HIL execution lock is already held by this process")
        try:
            stream = self.path.open("r+b", buffering=0)
        except OSError as exc:
            raise HilError(f"cannot open the pinned HIL execution lock: {self.path}") from exc
        try:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or self.path.is_symlink():
                raise HilError("pinned HIL execution lock is not a regular file")
            if os.name != "nt" and metadata.st_mode & 0o007:
                raise HilError("pinned HIL execution lock is accessible to other users")
            try:
                if os.name == "nt":
                    msvcrt: Any = importlib.import_module("msvcrt")

                    if metadata.st_size == 0:
                        stream.write(b"\0")
                        stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BlockingIOError) as exc:
                raise HilError(
                    "another TideTracker HIL executor already holds the rig lock"
                ) from exc
            stream.seek(0)
            stream.truncate()
            stream.write(
                (json.dumps(self.diagnostics, sort_keys=True, separators=(",", ":")) + "\n").encode(
                    "utf-8"
                )
            )
            stream.flush()
            os.fsync(stream.fileno())
        except Exception:
            stream.close()
            raise
        self._stream = stream

    def release(self) -> None:
        if self._stream is None:
            return
        stream = self._stream
        self._stream = None
        try:
            if os.name == "nt":
                msvcrt: Any = importlib.import_module("msvcrt")

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


class Fixture(Protocol):
    events: list[dict[str, Any]]

    def set_mode(self, mode: str) -> None: ...
    def set_power(self, enabled: bool) -> dict[str, Any]: ...
    def wait_done(self, timeout_seconds: float) -> dict[str, Any]: ...
    def wait_phase_cut(self, phase: str, timeout_seconds: float) -> dict[str, Any]: ...
    def wait_update_cut(self, phase: str, timeout_seconds: float) -> dict[str, Any]: ...
    def measure_rails(self) -> dict[str, Any]: ...
    def identify(self) -> dict[str, Any]: ...
    def close(self) -> None: ...


class SimulationFixture:
    def __init__(self, done_delay_seconds: float = 0.01) -> None:
        self.done_delay_seconds = done_delay_seconds
        self.mode = "setup"
        self.power = False
        self.events: list[dict[str, Any]] = []

    def _record(self, event: str, **details: Any) -> None:
        self.events.append({"time": utc_now(), "event": event, **details})

    def set_mode(self, mode: str) -> None:
        if mode not in {"run", "setup"}:
            raise HilError(f"invalid fixture mode: {mode}")
        self.mode = mode
        self._record("mode", value=mode)

    def set_power(self, enabled: bool) -> dict[str, Any]:
        edge_us = int(time.monotonic() * 1_000_000)
        self.power = enabled
        event = {
            "time": utc_now(),
            "event": "power",
            "value": enabled,
            "edge_us": edge_us,
            "_host_received_monotonic_us": edge_us,
        }
        self.events.append(event)
        return event

    def wait_done(self, timeout_seconds: float) -> dict[str, Any]:
        if not self.power or self.mode != "run":
            raise HilError("DONE cannot occur unless simulated power is on in RUN mode")
        if self.done_delay_seconds > timeout_seconds:
            raise HilError("simulated DONE timeout")
        time.sleep(self.done_delay_seconds)
        event = {
            "time": utc_now(),
            "event": "done",
            "edge_us": int(time.monotonic() * 1_000_000),
            "pulse_us": 500_000,
            "_host_received_at": utc_now(),
            "_host_received_monotonic_us": int(time.monotonic() * 1_000_000),
        }
        self.events.append(event)
        self.power = False
        self._record("power_removed_by_timer", value=True)
        return event

    def wait_phase_cut(self, phase: str, timeout_seconds: float) -> dict[str, Any]:
        if not self.power or self.mode != "run":
            raise HilError("phase cut requires simulated power on in RUN mode")
        if timeout_seconds <= 0:
            raise HilError("phase-cut timeout must be positive")
        event = {
            "time": utc_now(),
            "event": "phase_power_cut",
            "phase": phase,
            "edge_us": int(time.monotonic() * 1_000_000),
        }
        self.events.append(event)
        self.power = False
        return event

    def wait_update_cut(self, phase: str, timeout_seconds: float) -> dict[str, Any]:
        if phase not in UPDATE_PHASES:
            raise HilError("invalid simulated update-cut phase")
        if not self.power or self.mode != "setup" or timeout_seconds <= 0:
            raise HilError("update cut requires simulated power on in SETUP mode")
        event = {
            "time": utc_now(),
            "event": "update_power_cut",
            "phase": phase,
            "edge_us": int(time.monotonic() * 1_000_000),
        }
        self.events.append(event)
        self.power = False
        return event

    def measure_rails(self) -> dict[str, Any]:
        if self.power:
            raise HilError("simulated off-rail measurement requires fixture power off")
        event = {
            "time": utc_now(),
            "event": "rail_measurement",
            "zero_5v_v": 0.0,
            "zero_3v3_v": 0.0,
        }
        self.events.append(event)
        return event

    def identify(self) -> dict[str, Any]:
        return {
            "hardware_id": "simulated-rig",
            "firmware_sha256": "0" * 64,
        }

    def close(self) -> None:
        self._record("fixture_closed")


class SerialFixture:
    """JSON-lines protocol implemented by the isolated fixture controller."""

    def __init__(
        self,
        port: str,
        baudrate: int,
        response_timeout: float = 5.0,
        *,
        authorization_guard: AuthorizationGuard,
    ) -> None:
        self.authorization_guard = authorization_guard
        self.authorization_guard.require_bounded("fixture serial open")
        try:
            import serial
        except ImportError as exc:
            raise HilError("real fixture control requires the pyserial package") from exc
        self._serial = serial.Serial(port, baudrate=baudrate, timeout=response_timeout)
        self.events: list[dict[str, Any]] = []

    def _command(
        self,
        command: str,
        *,
        response_timeout: float | None = None,
        **arguments: Any,
    ) -> dict[str, Any]:
        cleanup_command = command == "set_power" and arguments.get("value") is False
        safety_observation = command == "measure_rails"
        if not cleanup_command and not safety_observation:
            self.authorization_guard.require(f"fixture {command}")
        request = {"command": command, **arguments}
        host_request_time = utc_now()
        original_timeout = self._serial.timeout
        effective_timeout = response_timeout if response_timeout is not None else original_timeout
        if not cleanup_command and not safety_observation:
            if not isinstance(effective_timeout, (int, float)) or effective_timeout <= 0:
                raise HilError("fixture command timeout must be a positive bounded value")
            effective_timeout = self.authorization_guard.bounded_timeout(
                float(effective_timeout), f"fixture {command}"
            )
        if effective_timeout is not None:
            self._serial.timeout = effective_timeout
        try:
            self._serial.write((json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8"))
            self._serial.flush()
            response_line = self._serial.readline()
        finally:
            self._serial.timeout = original_timeout
        if not response_line:
            raise HilError(f"fixture timed out handling {command}")
        try:
            response = json.loads(response_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HilError(f"invalid fixture response to {command}: {response_line!r}") from exc
        host_received_at = utc_now()
        host_received_monotonic_us = int(time.monotonic() * 1_000_000)
        self.events.append(
            {
                "host_request_time": host_request_time,
                "host_received_at": host_received_at,
                "request": request,
                "response": response,
            }
        )
        if response.get("ok") is not True:
            raise HilError(f"fixture rejected {command}: {response}")
        return {
            **response,
            "_host_received_at": host_received_at,
            "_host_received_monotonic_us": host_received_monotonic_us,
        }

    def set_mode(self, mode: str) -> None:
        if mode not in {"run", "setup"}:
            raise HilError(f"invalid fixture mode: {mode}")
        self._command("set_mode", value=mode)

    def set_power(self, enabled: bool) -> dict[str, Any]:
        response = self._command("set_power", value=enabled)
        if enabled:
            edge_us = response.get("edge_us")
            if not isinstance(edge_us, (int, float)) or isinstance(edge_us, bool) or edge_us < 0:
                raise HilError("fixture power-on response lacks a valid edge_us")
        return response

    def wait_done(self, timeout_seconds: float) -> dict[str, Any]:
        timeout_seconds = self.authorization_guard.bounded_timeout(
            timeout_seconds, "fixture DONE wait"
        )
        response = self._command(
            "wait_done",
            timeout_ms=int(timeout_seconds * 1000),
            response_timeout=timeout_seconds + 5,
        )
        edge_us = response.get("edge_us")
        pulse_us = response.get("pulse_us")
        if (
            not isinstance(edge_us, (int, float))
            or isinstance(edge_us, bool)
            or edge_us < 0
            or not isinstance(pulse_us, (int, float))
            or isinstance(pulse_us, bool)
            or not 10_000 <= pulse_us <= 1_000_000
        ):
            raise HilError("fixture DONE response lacks valid edge_us or pulse_us")
        if response.get("event") not in {None, "done"}:
            raise HilError("fixture DONE response carries the wrong event identity")
        return {**response, "event": "done"}

    def wait_phase_cut(self, phase: str, timeout_seconds: float) -> dict[str, Any]:
        timeout_seconds = self.authorization_guard.bounded_timeout(
            timeout_seconds, f"fixture {phase} lifecycle cut wait"
        )
        response = self._command(
            "wait_phase_cut",
            phase=phase,
            timeout_ms=int(timeout_seconds * 1000),
            response_timeout=timeout_seconds + 5,
        )
        edge_us = response.get("edge_us")
        if (
            response.get("phase") != phase
            or not isinstance(edge_us, (int, float))
            or isinstance(edge_us, bool)
            or edge_us < 0
        ):
            raise HilError("fixture phase-cut response lacks the requested phase/edge_us")
        if response.get("event") not in {None, "phase_power_cut"}:
            raise HilError("fixture phase-cut response carries the wrong event identity")
        return {**response, "event": "phase_power_cut"}

    def wait_update_cut(self, phase: str, timeout_seconds: float) -> dict[str, Any]:
        if phase not in UPDATE_PHASES:
            raise HilError("invalid update-cut phase")
        timeout_seconds = self.authorization_guard.bounded_timeout(
            timeout_seconds, f"fixture {phase} update cut wait"
        )
        response = self._command(
            "wait_update_cut",
            phase=phase,
            timeout_ms=int(timeout_seconds * 1000),
            response_timeout=timeout_seconds + 5,
        )
        edge_us = response.get("edge_us")
        if (
            response.get("phase") != phase
            or not isinstance(edge_us, (int, float))
            or isinstance(edge_us, bool)
            or edge_us < 0
        ):
            raise HilError("fixture update-cut response lacks the requested phase/edge_us")
        if response.get("event") not in {None, "update_power_cut"}:
            raise HilError("fixture update-cut response carries the wrong event identity")
        return {**response, "event": "update_power_cut"}

    def measure_rails(self) -> dict[str, Any]:
        response = self._command("measure_rails")
        for field in ("zero_5v_v", "zero_3v3_v"):
            value = response.get(field)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not 0 <= value < 0.1
            ):
                raise HilError(f"fixture reports unsafe powered-off rail: {field}")
        return response

    def identify(self) -> dict[str, Any]:
        response = self._command("identify")
        hardware_id = response.get("hardware_id")
        firmware_hash = response.get("firmware_sha256")
        if (
            not isinstance(hardware_id, str)
            or not hardware_id
            or not isinstance(firmware_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", firmware_hash) is None
        ):
            raise HilError("fixture identity response is invalid")
        return response

    def close(self) -> None:
        self._serial.close()


class UartCapture:
    def __init__(
        self,
        port: str,
        baudrate: int,
        output: pathlib.Path,
        *,
        authorization_guard: AuthorizationGuard,
    ) -> None:
        self.authorization_guard = authorization_guard
        self.authorization_guard.require_bounded("UART capture open")
        try:
            import serial
        except ImportError as exc:
            raise HilError("UART capture requires the pyserial package") from exc
        self._serial = serial.Serial(port, baudrate=baudrate, timeout=0.2)
        self._output = output
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._capture, daemon=True)
        self._error: str | None = None
        self._closed = False

    def _capture(self) -> None:
        try:
            written = 0
            with self._output.open("xb") as stream:
                while not self._stop.is_set():
                    self.authorization_guard.require("UART capture read")
                    data = self._serial.read(4096)
                    if data:
                        written += len(data)
                        if written > MAX_UART_LOG_BYTES:
                            raise HilError("UART evidence exceeded its fixed size bound")
                        stream.write(data)
                        stream.flush()
                os.fsync(stream.fileno())
        except BaseException as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            self._stop.set()

    def start(self) -> None:
        self._thread.start()

    def close(self) -> dict[str, Any]:
        if self._closed:
            raise HilError("UART capture was already closed")
        self._closed = True
        self._stop.set()
        self._thread.join(timeout=2)
        self._serial.close()
        if self._thread.is_alive():
            raise HilError("UART capture thread did not stop")
        if self._error is not None:
            raise HilError(f"UART capture failed: {self._error}")
        try:
            payload = self._output.read_bytes()
        except OSError as exc:
            raise HilError("UART evidence could not be read") from exc
        if not payload:
            raise HilError("UART capture produced empty evidence")
        events: list[dict[str, Any]] = []
        for raw_line in payload.splitlines():
            if not raw_line.startswith(b"{") or len(raw_line) > 1024:
                continue
            try:
                value = json.loads(raw_line.decode("ascii"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict) and value.get("schema_version") == 1:
                events.append(value)
        return {
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "schema1_event_count": len(events),
            "event_names": sorted(
                {str(item.get("event")) for item in events if isinstance(item.get("event"), str)}
            ),
            "_events": events,
        }


@dataclass
class UpdateCutHandle:
    phase: str
    release_id: str
    bundle_sha256: str
    remote_name: str
    process: subprocess.Popen[str] | None = None


class Target(Protocol):
    def wait_available(self, timeout_seconds: float) -> None: ...
    def wait_unavailable(self, timeout_seconds: float) -> None: ...
    def stage_bundle(self, bundle: pathlib.Path) -> dict[str, Any]: ...
    def collect(self) -> dict[str, Any]: ...
    def set_noaa_fault(self, kind: str) -> dict[str, Any]: ...
    def noaa_fault_status(self) -> dict[str, Any]: ...
    def clear_noaa_fault(self) -> dict[str, Any]: ...
    def begin_update_cut(
        self, bundle: pathlib.Path, phase: str, release_id: str
    ) -> UpdateCutHandle: ...
    def finish_update_cut(self, handle: UpdateCutHandle) -> dict[str, Any]: ...
    def recover_update_cut(
        self, phase: str, release_id: str, bundle_sha256: str
    ) -> dict[str, Any]: ...
    def network_profile_evidence(
        self,
        case_id: str,
        ssid_sha256: str,
        security: str,
        hidden: bool,
        operation: str,
    ) -> dict[str, Any]: ...
    def network_rollback_evidence(self, ssid_sha256: str) -> dict[str, Any]: ...
    def network_secret_artifact_scan(self, secrets: tuple[str, ...]) -> dict[str, Any]: ...


class SimulationTarget:
    def __init__(
        self,
        expected_release_id: str = "built-in",
        expected_execution_source: str = "built-in",
    ) -> None:
        self._collect_count = 0
        self.expected_release_id = expected_release_id
        self.expected_execution_source = expected_execution_source
        self._pending_fault: str | None = None
        self._last_fault: str | None = None
        self._update_cut: UpdateCutHandle | None = None

    def wait_available(self, timeout_seconds: float) -> None:
        del timeout_seconds

    def wait_unavailable(self, timeout_seconds: float) -> None:
        del timeout_seconds

    def stage_bundle(self, bundle: pathlib.Path) -> dict[str, Any]:
        digest = sha256_file(bundle)
        return {
            "simulated": True,
            "bundle": str(bundle),
            "sha256": digest,
            "status": "trial-activated",
            "stage": {
                "release_id": self.expected_release_id,
                "state": "trial",
            },
        }

    def set_noaa_fault(self, kind: str) -> dict[str, Any]:
        if kind not in {"dns", "tls", "http_503"} or self._pending_fault is not None:
            raise HilError("invalid or duplicate simulated NOAA fault")
        self._pending_fault = kind
        return {"pending": True, "kind": kind}

    def noaa_fault_status(self) -> dict[str, Any]:
        return {"pending": self._pending_fault is not None, "kind": self._pending_fault}

    def clear_noaa_fault(self) -> dict[str, Any]:
        self._pending_fault = None
        return {"pending": False, "kind": None}

    def begin_update_cut(
        self, bundle: pathlib.Path, phase: str, release_id: str
    ) -> UpdateCutHandle:
        if phase not in UPDATE_PHASES or self._update_cut is not None:
            raise HilError("invalid or overlapping simulated update cut")
        handle = UpdateCutHandle(
            phase=phase,
            release_id=release_id,
            bundle_sha256=sha256_file(bundle),
            remote_name=f"simulated-{phase}-{bundle.name}",
        )
        self._update_cut = handle
        return handle

    def finish_update_cut(self, handle: UpdateCutHandle) -> dict[str, Any]:
        if self._update_cut is not handle:
            raise HilError("simulated update-cut handle does not match")
        return {"ssh_interrupted_by_power_cut": True, "simulated": True}

    def recover_update_cut(
        self, phase: str, release_id: str, bundle_sha256: str
    ) -> dict[str, Any]:
        handle = self._update_cut
        if (
            handle is None
            or handle.phase != phase
            or handle.release_id != release_id
            or handle.bundle_sha256 != bundle_sha256
        ):
            raise HilError("simulated update recovery does not match its cut")
        self._update_cut = None
        return {
            "schema_version": 1,
            "event": "update_power_cut_recovery",
            "phase": phase,
            "release_id": release_id,
            "bundle_sha256": bundle_sha256,
            "recovered": True,
            "state_valid": True,
            "observed_state": {
                "begin": "uploading",
                "stage": "staged",
                "verify": "verified",
                "activate": "trial",
            }[phase],
            "cleanup_complete": True,
            "post_cleanup_clean": True,
            "fallback_used": False,
        }

    def network_profile_evidence(
        self,
        case_id: str,
        ssid_sha256: str,
        security: str,
        hidden: bool,
        operation: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "event": "network_profile_evidence",
            "case_id": case_id,
            "ssid_sha256": ssid_sha256,
            "security": security,
            "hidden": hidden,
            "operation": operation,
            "operation_state": "succeeded",
            "profile_mode": "0600",
            "profile_owner": "root",
            "profile_group": "root",
            "networkmanager_loaded": True,
            "simulated": True,
        }

    def network_rollback_evidence(self, ssid_sha256: str) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "event": "network_rollback_evidence",
            "ssid_sha256": ssid_sha256,
            "profile_absent": True,
            "active_profile_restored": True,
            "simulated": True,
        }

    def network_secret_artifact_scan(self, secrets: tuple[str, ...]) -> dict[str, Any]:
        _validate_secret_scan_values(secrets)
        return {
            "schema_version": 1,
            "event": "network_secret_artifact_scan",
            "passed": True,
            "files_scanned": 1,
            "forbidden_matches": 0,
        }

    def collect(self) -> dict[str, Any]:
        self._collect_count += 1
        self._last_fault = self._pending_fault
        self._pending_fault = None
        run_id = f"simulated-run-{self._collect_count}"
        return {
            "diagnose": {"simulated": True, "board": "Raspberry Pi Zero W"},
            "journal": "simulated cycle completed",
            "route": "setup mode has no production default route",
            "last_result": {
                "run_id": run_id,
                "success": True,
                "network_interface": "wlan0",
                "production_route_interface": "wlan0",
                "framebuffer_sha256": "0" * 64,
                "release_id": self.expected_release_id,
                "execution_source": self.expected_execution_source,
                "injected_noaa_fault": self._last_fault,
                "used_cached_data": self._last_fault is not None,
                "wifi_active_observation_source": "runtime",
            },
            "metrics": {
                "run_id": run_id,
                "success": True,
                "wake_to_done_seconds": 1.0,
                "wifi_active_seconds": 0.2,
                "wifi_active_observed_seconds": 0.2,
                "cpu_user_seconds": 0.1,
                "cpu_system_seconds": 0.05,
                "max_rss_kib": 32 * 1024,
                "process_write_bytes": 1024,
                "display_refresh_count": 1,
                "swap_used_kib": 0,
                "release_id": self.expected_release_id,
                "execution_source": self.expected_execution_source,
            },
            "supervisor": {
                "schema_version": 1,
                "event": "release_supervision",
                "execution": {
                    "expected_release_id": self.expected_release_id,
                    "expected_execution_source": self.expected_execution_source,
                    "attempts": [
                        {
                            "release_id": self.expected_release_id,
                            "execution_source": self.expected_execution_source,
                            "returncode": 0,
                        }
                    ],
                    "trial_confirmed": False,
                    "trial_rolled_back": False,
                    "fallback_used": False,
                    "selected_run_id": run_id,
                    "selected_release_id": self.expected_release_id,
                    "selected_execution_source": self.expected_execution_source,
                    "selected_success": True,
                    "expected_release_executed_successfully": True,
                },
            },
            "usb_policy": {
                "never_default": True,
                "ignore_auto_dns": True,
                "gateway": None,
                "dns": [],
                "firewall_enforced": True,
            },
            "resolver_policy": {
                "service_active": True,
                "stub_resolver": True,
                "networkmanager_backend": "systemd-resolved",
                "wlan0_link_visible": True,
            },
            "swap_policy": {
                "active_entries": 0,
                "dphys_swapfile_masked": True,
                "swapfile_absent": True,
            },
            "storage_policy": {
                "root_overlay": True,
                "boot_read_only": True,
                "data_uuid": "54540000-0000-4000-8000-000000000002",
                "data_read_write": True,
                "networkmanager_bind_from_data": True,
                "overlay_recurse": False,
            },
        }


@dataclass(frozen=True)
class SshSettings:
    host: str
    user: str
    identity_file: pathlib.Path
    known_hosts_file: pathlib.Path
    connect_timeout_seconds: int = 5


class SshTarget:
    def __init__(
        self,
        settings: SshSettings,
        *,
        authorization_guard: AuthorizationGuard,
    ) -> None:
        self.settings = settings
        self.authorization_guard = authorization_guard
        self.authorization_guard.require_bounded("target SSH control")

    def _common(self) -> list[str]:
        return [
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self.settings.known_hosts_file}",
            "-o",
            f"ConnectTimeout={self.settings.connect_timeout_seconds}",
            "-i",
            str(self.settings.identity_file),
        ]

    def _run(
        self,
        remote_command: str,
        *,
        check: bool = True,
        stdin_text: str | None = None,
        authorize: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        if authorize:
            self.authorization_guard.require("target SSH command")
        command = self._ssh_command(remote_command)
        timeout = (
            self.authorization_guard.bounded_timeout(240, "target SSH command")
            if authorize
            else 240
        )
        return subprocess.run(
            command,
            input=stdin_text,
            text=True,
            capture_output=True,
            check=check,
            timeout=timeout,
        )

    def _ssh_command(self, remote_command: str) -> list[str]:
        return [
            "ssh",
            *self._common(),
            f"{self.settings.user}@{self.settings.host}",
            remote_command,
        ]

    def _reachable(self, *, authorize: bool = True) -> bool:
        try:
            result = self._run("true", check=False, authorize=authorize)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def wait_available(self, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self._reachable():
                return
            time.sleep(1)
        raise HilError(f"SSH did not become available at {self.settings.host}")

    def wait_unavailable(self, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not self._reachable(authorize=False):
                return
            time.sleep(0.5)
        raise HilError(
            "SSH remained reachable after switched power was removed; check USB back-power isolation"
        )

    def _upload_bundle(self, bundle: pathlib.Path, *, remote_name: str) -> dict[str, str]:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", bundle.name) is None:
            raise HilError(
                "bundle filename must use only letters, digits, dot, underscore, and dash"
            )
        digest = sha256_file(bundle)
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", remote_name) is None:
            raise HilError("remote bundle name is unsafe")
        remote_path = f"/var/lib/tidetracker/uploads/{remote_name}"
        partial_path = f"/var/lib/tidetracker/uploads/.{remote_name}.{digest[:12]}.partial"
        local_path = str(bundle).replace("\\", "/")
        if any(character in local_path for character in {'"', "\n", "\r"}):
            raise HilError("bundle path contains characters unsafe for SFTP batch mode")
        batch = f'put "{local_path}" "{partial_path}"\n'
        command = ["sftp", "-b", "-", *self._common(), f"{self.settings.user}@{self.settings.host}"]
        self.authorization_guard.require("target SFTP upload")
        timeout = self.authorization_guard.bounded_timeout(240, "target SFTP upload")
        subprocess.run(
            command, input=batch, text=True, capture_output=True, check=True, timeout=timeout
        )
        quoted_partial = shlex.quote(partial_path)
        quoted_path = shlex.quote(remote_path)
        finalize = self._run(
            f"test ! -e {quoted_path} "
            f"&& test \"$(sha256sum {quoted_partial} | cut -d' ' -f1)\" = {digest} "
            f"&& mv {quoted_partial} {quoted_path}"
        )
        return {
            "sha256": digest,
            "remote_name": remote_name,
            "remote_path": remote_path,
            "finalize": str(finalize.returncode),
        }

    def stage_bundle(self, bundle: pathlib.Path) -> dict[str, Any]:
        upload = self._upload_bundle(bundle, remote_name=bundle.name)
        quoted_name = shlex.quote(upload["remote_name"])
        stage = self._run(
            f"sudo -n /usr/local/sbin/tidetracker-lab-control --execute update-bundle {quoted_name}"
        )
        try:
            stage_value: Any = json.loads(stage.stdout)
        except json.JSONDecodeError as exc:
            raise HilError("tidetracker-update returned invalid JSON") from exc
        if not isinstance(stage_value, dict):
            raise HilError("tidetracker-update returned a non-object result")
        return {
            "sha256": upload["sha256"],
            "transfer": "complete",
            "finalize": int(upload["finalize"]),
            "stage": stage_value,
            "upload_cleanup": "performed-by-updater",
        }

    def _control_json(
        self,
        command: str,
        *,
        execute: bool = False,
        stdin_text: str | None = None,
        authorize: bool = True,
    ) -> dict[str, Any]:
        prefix = "sudo -n /usr/local/sbin/tidetracker-lab-control"
        if execute:
            prefix += " --execute"
        result = self._run(
            f"{prefix} {command}",
            check=False,
            stdin_text=stdin_text,
            authorize=authorize,
        )
        text = self._require_success(command, result)
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise HilError(f"target {command} output is invalid JSON") from exc
        if not isinstance(value, dict):
            raise HilError(f"target {command} output must be a JSON object")
        return value

    def set_noaa_fault(self, kind: str) -> dict[str, Any]:
        if kind not in {"dns", "tls", "http_503"}:
            raise HilError("invalid NOAA fault kind")
        return self._control_json(f"fault-set {kind}", execute=True)

    def noaa_fault_status(self) -> dict[str, Any]:
        return self._control_json("fault-status")

    def clear_noaa_fault(self) -> dict[str, Any]:
        return self._control_json("fault-clear", execute=True, authorize=False)

    def begin_update_cut(
        self, bundle: pathlib.Path, phase: str, release_id: str
    ) -> UpdateCutHandle:
        if phase not in UPDATE_PHASES:
            raise HilError("invalid update-cut phase")
        digest = sha256_file(bundle)
        suffix = f"update-cut-{phase}-{digest[:12]}-{bundle.name}"
        if len(suffix) > 128:
            suffix = f"update-cut-{phase}-{digest}.tar"
        self._upload_bundle(bundle, remote_name=suffix)
        command = (
            "sudo -n /usr/local/sbin/tidetracker-lab-control --execute update-cut "
            f"{shlex.quote(suffix)} {phase} {shlex.quote(release_id)} {digest}"
        )
        self.authorization_guard.require("target update power-cut command")
        process = subprocess.Popen(
            self._ssh_command(command),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.1)
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=5)
            del stdout, stderr
            raise HilError("target update-cut helper exited before the fixture cut boundary")
        return UpdateCutHandle(phase, release_id, digest, suffix, process)

    def finish_update_cut(self, handle: UpdateCutHandle) -> dict[str, Any]:
        process = handle.process
        if process is None:
            raise HilError("real update-cut handle lacks its SSH process")
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.communicate()
            raise HilError("update-cut SSH process remained alive after target power removal") from exc
        if process.returncode == 0:
            raise HilError("update-cut helper completed normally instead of being power-interrupted")
        return {
            "ssh_interrupted_by_power_cut": True,
            "ssh_returncode": process.returncode,
            "stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
            "stderr_sha256": hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
        }

    def recover_update_cut(
        self, phase: str, release_id: str, bundle_sha256: str
    ) -> dict[str, Any]:
        return self._control_json(
            "update-cut-recover "
            f"{phase} {shlex.quote(release_id)} {bundle_sha256}",
            execute=True,
        )

    def network_profile_evidence(
        self,
        case_id: str,
        ssid_sha256: str,
        security: str,
        hidden: bool,
        operation: str,
    ) -> dict[str, Any]:
        arguments = (case_id, ssid_sha256, security, "true" if hidden else "false", operation)
        if any(re.fullmatch(r"[A-Za-z0-9_.-]+", value) is None for value in arguments):
            raise HilError("network profile evidence request contains an unsafe value")
        return self._control_json("network-profile-evidence " + " ".join(arguments))

    def network_rollback_evidence(self, ssid_sha256: str) -> dict[str, Any]:
        if re.fullmatch(r"[0-9a-f]{64}", ssid_sha256) is None:
            raise HilError("rollback evidence SSID hash is invalid")
        return self._control_json(f"network-rollback-evidence {ssid_sha256}")

    def network_secret_artifact_scan(self, secrets: tuple[str, ...]) -> dict[str, Any]:
        validated = _validate_secret_scan_values(secrets)
        payload = json.dumps(
            {"schema_version": 1, "secrets": list(validated)},
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(payload.encode("utf-8")) > MAX_SECRET_SCAN_INPUT_BYTES:
            raise HilError("target secret scan input is invalid")
        return self._control_json(
            "network-secret-artifact-scan",
            stdin_text=f"{payload}\n",
        )

    @staticmethod
    def _require_success(label: str, result: subprocess.CompletedProcess[str]) -> str:
        if result.returncode != 0:
            raise HilError(f"target {label} command failed with exit code {result.returncode}")
        if not result.stdout.strip():
            raise HilError(f"target {label} command returned no data")
        return result.stdout

    def collect(self) -> dict[str, Any]:
        diagnose = self._run("/usr/local/bin/tidetracker diagnose --json", check=False)
        journal = self._run("sudo -n /usr/local/sbin/tidetracker-lab-control journal", check=False)
        route = self._run("ip route get 8.8.8.8", check=False)
        metrics = self._run(
            "sudo -n /usr/local/sbin/tidetracker-lab-control metrics-latest",
            check=False,
        )
        last_result = self._run(
            "sudo -n /usr/local/sbin/tidetracker-lab-control last-result",
            check=False,
        )
        usb_policy = self._run(
            "sudo -n /usr/local/sbin/tidetracker-lab-control usb-policy",
            check=False,
        )
        supervisor = self._run(
            "sudo -n /usr/local/sbin/tidetracker-lab-control supervisor-last",
            check=False,
        )
        resolver_policy = self._run(
            "sudo -n /usr/local/sbin/tidetracker-lab-control resolver-policy",
            check=False,
        )
        swap_policy = self._run(
            "sudo -n /usr/local/sbin/tidetracker-lab-control swap-policy",
            check=False,
        )
        storage_policy = self._run(
            "sudo -n /usr/local/sbin/tidetracker-lab-control storage-policy",
            check=False,
        )
        diagnose_text = self._require_success("diagnose", diagnose)
        journal_text = self._require_success("journal", journal)
        metrics_text = self._require_success("metrics", metrics)
        last_result_text = self._require_success("last result", last_result)
        usb_policy_text = self._require_success("USB policy", usb_policy)
        supervisor_text = self._require_success("supervisor evidence", supervisor)
        resolver_policy_text = self._require_success("resolver policy", resolver_policy)
        swap_policy_text = self._require_success("swap policy", swap_policy)
        storage_policy_text = self._require_success("storage policy", storage_policy)
        try:
            diagnose_value: Any = json.loads(diagnose_text)
        except json.JSONDecodeError as exc:
            raise HilError("target diagnose output is invalid JSON") from exc
        try:
            metrics_value: Any = json.loads(metrics_text)
        except json.JSONDecodeError as exc:
            raise HilError("target metrics output is invalid JSON") from exc
        try:
            last_result_value: Any = json.loads(last_result_text)
        except json.JSONDecodeError as exc:
            raise HilError("target last-result output is invalid JSON") from exc
        try:
            usb_policy_value: Any = json.loads(usb_policy_text)
        except json.JSONDecodeError as exc:
            raise HilError("target USB policy output is invalid JSON") from exc
        try:
            supervisor_value: Any = json.loads(supervisor_text)
        except json.JSONDecodeError as exc:
            raise HilError("target supervisor output is invalid JSON") from exc
        try:
            resolver_policy_value: Any = json.loads(resolver_policy_text)
        except json.JSONDecodeError as exc:
            raise HilError("target resolver policy output is invalid JSON") from exc
        try:
            swap_policy_value: Any = json.loads(swap_policy_text)
        except json.JSONDecodeError as exc:
            raise HilError("target swap policy output is invalid JSON") from exc
        try:
            storage_policy_value: Any = json.loads(storage_policy_text)
        except json.JSONDecodeError as exc:
            raise HilError("target storage policy output is invalid JSON") from exc
        if not all(
            isinstance(value, dict)
            for value in (
                diagnose_value,
                metrics_value,
                last_result_value,
                usb_policy_value,
                supervisor_value,
                resolver_policy_value,
                swap_policy_value,
                storage_policy_value,
            )
        ):
            raise HilError("target diagnose, metrics, and last-result values must be JSON objects")
        run_id = last_result_value.get("run_id")
        if not isinstance(run_id, str) or not run_id or metrics_value.get("run_id") != run_id:
            raise HilError("target metrics do not match the durable cycle result")
        if usb_policy_value != {
            "never_default": True,
            "ignore_auto_dns": True,
            "gateway": None,
            "dns": [],
            "firewall_enforced": True,
        }:
            raise HilError("USB management profile is not route/DNS isolated")
        if resolver_policy_value != {
            "service_active": True,
            "stub_resolver": True,
            "networkmanager_backend": "systemd-resolved",
            "wlan0_link_visible": True,
        }:
            raise HilError("target resolver is not bound to NetworkManager/systemd-resolved")
        if swap_policy_value != {
            "active_entries": 0,
            "dphys_swapfile_masked": True,
            "swapfile_absent": True,
        }:
            raise HilError("target system swap is not disabled")
        if storage_policy_value != {
            "root_overlay": True,
            "boot_read_only": True,
            "data_uuid": "54540000-0000-4000-8000-000000000002",
            "data_read_write": True,
            "networkmanager_bind_from_data": True,
            "overlay_recurse": False,
        }:
            raise HilError("target read-only root/TIDEDATA storage policy is not active")
        framebuffer_hash = last_result_value.get("framebuffer_sha256")
        if (
            not isinstance(framebuffer_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", framebuffer_hash) is None
        ):
            raise HilError("durable cycle result lacks a valid framebuffer hash")
        for metric_name in (
            "wake_to_done_seconds",
            "wifi_active_seconds",
            "cpu_user_seconds",
            "cpu_system_seconds",
            "max_rss_kib",
            "process_write_bytes",
            "display_refresh_count",
            "swap_used_kib",
        ):
            metric = metrics_value.get(metric_name)
            if not isinstance(metric, (int, float)) or isinstance(metric, bool) or metric < 0:
                raise HilError(f"target metrics contain invalid {metric_name}")
        if metrics_value.get("swap_used_kib") != 0:
            raise HilError("target used swap during the cycle")
        observed_wifi = metrics_value.get("wifi_active_observed_seconds")
        if observed_wifi is not None and (
            not isinstance(observed_wifi, (int, float))
            or isinstance(observed_wifi, bool)
            or observed_wifi < 0
        ):
            raise HilError("target metrics contain invalid wifi_active_observed_seconds")
        return {
            "diagnose": diagnose_value,
            "journal": journal_text,
            "route": route.stdout if route.returncode == 0 else None,
            "last_result": last_result_value,
            "metrics": metrics_value,
            "usb_policy": usb_policy_value,
            "supervisor": supervisor_value,
            "resolver_policy": resolver_policy_value,
            "swap_policy": swap_policy_value,
            "storage_policy": storage_policy_value,
        }


class Camera:
    def __init__(
        self,
        command: list[str] | None,
        simulated: bool,
        validation_command: list[str] | None = None,
        *,
        capture_support_files: list[str] | None = None,
        validation_support_files: list[str] | None = None,
        authorization_guard: AuthorizationGuard | None = None,
    ) -> None:
        self.command = command
        self.simulated = simulated
        self.validation_command = validation_command
        self.authorization_guard = authorization_guard or AuthorizationGuard()
        self.capture_binding: BoundCommand | None = None
        self.validation_binding: BoundCommand | None = None
        if not simulated:
            self.capture_binding = BoundCommand.create(
                command,
                capture_support_files,
                label="camera.command",
                require_output=True,
            )
            self.validation_binding = BoundCommand.create(
                validation_command,
                validation_support_files,
                label="camera.validation_command",
                require_output=True,
            )

    @property
    def tool_identities(self) -> dict[str, dict[str, Any]]:
        if self.capture_binding is None or self.validation_binding is None:
            return {}
        return {
            "camera_capture": self.capture_binding.identity,
            "camera_validator": self.validation_binding.identity,
        }

    def bind_authorization(self, authorization_guard: AuthorizationGuard) -> None:
        self.authorization_guard = authorization_guard

    def capture(self, output: pathlib.Path) -> dict[str, Any]:
        if self.simulated:
            output.write_bytes(b"SIMULATED CAMERA FRAME\n")
            return {
                "simulated": True,
                "capture": "complete",
                "comparison": "accepted",
                "image_size_bytes": output.stat().st_size,
                "image_sha256": sha256_file(output),
            }
        self.authorization_guard.require_bounded("camera control")
        if self.capture_binding is None:
            raise HilError("real HIL requires a camera command containing an {output} argument")
        self.authorization_guard.require("camera capture")
        self.capture_binding.assert_current()
        command = [
            part.replace("{output}", str(output))
            for part in self.capture_binding.canonical_argv
        ]
        capture_timeout = self.authorization_guard.bounded_timeout(30, "camera capture")
        subprocess.run(command, check=True, capture_output=True, timeout=capture_timeout)
        self.capture_binding.assert_current()
        if not output.is_file() or output.stat().st_size == 0:
            raise HilError("camera command did not produce a non-empty frame")
        if self.validation_binding is None:
            raise HilError("real HIL requires camera.validation_command for automated comparison")
        self.authorization_guard.require("camera validation")
        self.validation_binding.assert_current()
        validation = [
            part.replace("{output}", str(output))
            for part in self.validation_binding.canonical_argv
        ]
        started = time.monotonic()
        result = subprocess.run(
            validation,
            text=True,
            capture_output=True,
            check=False,
            timeout=self.authorization_guard.bounded_timeout(60, "camera validation"),
        )
        self.validation_binding.assert_current()
        if result.returncode != 0:
            raise HilError(f"camera comparison rejected the display (exit {result.returncode})")
        return {
            "capture": "complete",
            "comparison": "accepted",
            "comparison_seconds": round(time.monotonic() - started, 3),
            "image_size_bytes": output.stat().st_size,
            "image_sha256": sha256_file(output),
            "capture_tool_identity": self.capture_binding.identity,
            "validation_tool_identity": self.validation_binding.identity,
        }


WIFI_SCENARIOS = (
    "wifi_disconnect",
    "bad_credentials",
    "client_ap_transition",
    "dns_failure",
    "tls_failure",
    "http_failure",
)
PORTAL_CASE_SPEC: dict[str, tuple[str, bool, str]] = {
    "visible_open_test": ("open", False, "test"),
    "visible_open_force_save": ("open", False, "force-save"),
    "hidden_open_test": ("open", True, "test"),
    "hidden_open_force_save": ("open", True, "force-save"),
    "visible_wpa_personal_test": ("wpa-personal", False, "test"),
    "visible_wpa_personal_force_save": ("wpa-personal", False, "force-save"),
    "hidden_wpa_personal_test": ("wpa-personal", True, "test"),
    "hidden_wpa_personal_force_save": ("wpa-personal", True, "force-save"),
    "visible_wpa_enterprise_test": ("wpa-enterprise", False, "test"),
    "visible_wpa_enterprise_force_save": ("wpa-enterprise", False, "force-save"),
    "hidden_wpa_enterprise_test": ("wpa-enterprise", True, "test"),
    "hidden_wpa_enterprise_force_save": ("wpa-enterprise", True, "force-save"),
}
LIFECYCLE_PHASES = (
    "mode",
    "network",
    "fetch",
    "validate",
    "compute",
    "render",
    "display",
    "persist",
    "done",
)


class ScenarioController(Protocol):
    def run(self, scenario: str, phase: str) -> dict[str, Any]: ...
    def run_portal_case(self, case_id: str, phase: str) -> dict[str, Any]: ...


class SimulationScenarioController:
    def run(self, scenario: str, phase: str) -> dict[str, Any]:
        return {"simulated": True, "scenario": scenario, "phase": phase, "status": "complete"}

    def run_portal_case(self, case_id: str, phase: str) -> dict[str, Any]:
        return {
            "simulated": True,
            "scenario": "client_ap_transition",
            "case_id": case_id,
            "phase": phase,
            "status": "complete",
        }


class CommandScenarioController:
    """Invoke only the immutable, whitelist-validating controller Wi-Fi helper."""

    HELPER = "/usr/local/sbin/tidetracker-controller-wifi-control"

    PORTAL_STATES = {
        "validate": "validated",
        "apply": "portal-client-active",
        "test-plane": "test-plane-active",
        "reset": "normal-ap-restored",
    }

    def __init__(
        self,
        portal_config: dict[str, Any],
        *,
        authorization_guard: AuthorizationGuard,
    ) -> None:
        self.authorization_guard = authorization_guard
        self.authorization_guard.require_bounded("controller Wi-Fi control")
        if shutil.which("sudo") is None:
            raise HilError("controller Wi-Fi matrix requires sudo")
        helper = pathlib.Path(self.HELPER)
        if not helper.is_file() or helper.is_symlink() or not os.access(helper, os.X_OK):
            raise HilError("immutable controller Wi-Fi helper is unavailable")
        try:
            self.authorization_guard.require("controller Wi-Fi preflight")
            check = subprocess.run(
                ["sudo", "-n", self.HELPER, "--execute", "normal-disconnect", "validate"],
                text=True,
                capture_output=True,
                check=False,
                timeout=self.authorization_guard.bounded_timeout(
                    30, "controller Wi-Fi preflight"
                ),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HilError("controller AP and portal-client preflight failed or timed out") from exc
        if check.returncode != 0:
            raise HilError("controller AP and portal-client profiles failed preflight")
        self.portal_cases = {
            str(case["case_id"]): case
            for case in PortalClient._validate_cases(portal_config.get("profile_cases"))
        }
        for case_id in PORTAL_CASE_SPEC:
            self._invoke_portal_case(case_id, "validate", timeout=90)

    @staticmethod
    def _strict_json_object(payload: str) -> dict[str, Any]:
        def reject_duplicates(items: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, child in items:
                if key in value:
                    raise HilError("controller portal test plane repeated a JSON field")
                value[key] = child
            return value

        try:
            value = json.loads(payload, object_pairs_hook=reject_duplicates)
        except json.JSONDecodeError as exc:
            raise HilError("controller portal test plane returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise HilError("controller portal test plane returned invalid JSON")
        return value

    def _invoke_portal_case(
        self, case_id: str, phase: str, *, timeout: float = 180
    ) -> dict[str, Any]:
        case = self.portal_cases.get(case_id)
        if case is None or phase not in self.PORTAL_STATES:
            raise HilError("portal matrix controller transition is invalid")
        if phase != "reset":
            self.authorization_guard.require(f"controller portal {case_id}/{phase}")
        private_input = json.dumps(case, sort_keys=True, separators=(",", ":")) + "\n"
        if len(private_input.encode("utf-8")) > 8 * 1024:
            raise HilError("controller portal profile exceeds its private input bound")
        command = [
            "sudo",
            "-n",
            self.HELPER,
            "--execute",
            "portal-matrix",
            case_id,
            phase,
        ]
        try:
            effective_timeout = (
                timeout
                if phase == "reset"
                else self.authorization_guard.bounded_timeout(
                    timeout, f"controller portal {case_id}/{phase}"
                )
            )
            result = subprocess.run(
                command,
                input=private_input,
                text=True,
                capture_output=True,
                check=False,
                timeout=effective_timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HilError("controller portal test plane command failed or timed out") from exc
        if result.returncode != 0 or len(result.stdout.encode("utf-8")) > 4096:
            raise HilError(f"controller portal test plane failed: {case_id}/{phase}")
        value = self._strict_json_object(result.stdout)
        expected = {
            "schema_version": 1,
            "operation": "portal-matrix",
            "case_id": case_id,
            "phase": phase,
            "state": self.PORTAL_STATES[phase],
        }
        if value != expected:
            raise HilError("controller portal test plane did not prove the requested state")
        return value

    def run(self, scenario: str, phase: str) -> dict[str, Any]:
        operation = {
            "wifi_disconnect": "normal-disconnect",
            "bad_credentials": "portal-client",
            "client_ap_transition": "portal-client",
        }.get(scenario)
        if operation is None or phase not in {"apply", "probe", "reset"}:
            raise HilError("scenario is not a controller Wi-Fi transition")
        if phase != "reset":
            self.authorization_guard.require(f"controller Wi-Fi {scenario}/{phase}")
        command = ["sudo", "-n", self.HELPER, "--execute", operation, phase]
        started = time.monotonic()
        timeout = (
            180
            if phase == "reset"
            else self.authorization_guard.bounded_timeout(
                180, f"controller Wi-Fi {scenario}/{phase}"
            )
        )
        result = subprocess.run(
            command, text=True, capture_output=True, check=False, timeout=timeout
        )
        duration = time.monotonic() - started
        if result.returncode != 0:
            raise HilError(
                f"wifi scenario {scenario}/{phase} failed with exit code {result.returncode}"
            )
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise HilError("controller Wi-Fi helper returned invalid JSON") from exc
        if not isinstance(value, dict) or value.get("phase") != phase:
            raise HilError("controller Wi-Fi helper did not prove the requested state")
        return {**value, "scenario": scenario, "duration_seconds": round(duration, 3)}

    def run_portal_case(self, case_id: str, phase: str) -> dict[str, Any]:
        if case_id not in PORTAL_CASE_SPEC or phase not in {"apply", "test-plane", "reset"}:
            raise HilError("portal matrix controller transition is invalid")
        started = time.monotonic()
        timeout = {"apply": 240.0, "test-plane": 180.0, "reset": 360.0}[phase]
        value = self._invoke_portal_case(case_id, phase, timeout=timeout)
        return {
            **value,
            "scenario": "client_ap_transition",
            "duration_seconds": round(time.monotonic() - started, 3),
        }


class WifiActivityObserver(Protocol):
    def begin_cycle(self) -> None: ...
    def finish_cycle(
        self,
        power_on: dict[str, Any],
        done: dict[str, Any],
        *,
        require_association: bool,
    ) -> dict[str, Any]: ...
    def abort_cycle(self) -> None: ...


class SimulationWifiActivityObserver:
    def __init__(self) -> None:
        self._started_us: int | None = None

    def begin_cycle(self) -> None:
        if self._started_us is not None:
            raise HilError("simulated Wi-Fi observation is already active")
        self._started_us = int(time.monotonic() * 1_000_000)

    def finish_cycle(
        self,
        power_on: dict[str, Any],
        done: dict[str, Any],
        *,
        require_association: bool,
    ) -> dict[str, Any]:
        if self._started_us is None:
            raise HilError("simulated Wi-Fi observation was not started")
        self._started_us = None
        power_edge = float(power_on["edge_us"])
        done_edge = float(done["edge_us"])
        duration = max(0.0, (done_edge - power_edge) / 1_000_000)
        return {
            "source": "controller_ap_station_poll",
            "clock": "controller_monotonic_mapped_fixture_done",
            "target_mac": "02:00:00:00:00:01",
            "poll_interval_seconds": 0.05,
            "association_observed": require_association,
            "observed_seconds": duration if require_association else 0.0,
            "transitions": [],
            "simulated": True,
        }

    def abort_cycle(self) -> None:
        self._started_us = None


class CommandWifiActivityObserver:
    """Measure target association on the Pi 3 AP using the controller clock."""

    HELPER = CommandScenarioController.HELPER

    def __init__(
        self,
        config: dict[str, Any],
        *,
        authorization_guard: AuthorizationGuard,
    ) -> None:
        self.authorization_guard = authorization_guard
        self.authorization_guard.require_bounded("controller Wi-Fi observation")
        target_mac = str(config.get("target_mac", "")).lower()
        if re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", target_mac) is None:
            raise HilError("wifi_observation.target_mac must be a concrete MAC address")
        poll_interval = config.get("poll_interval_seconds", 0.05)
        disassociation_timeout = config.get("disassociation_timeout_seconds", 10.0)
        if (
            not isinstance(poll_interval, (int, float))
            or isinstance(poll_interval, bool)
            or not 0.02 <= float(poll_interval) <= 0.5
        ):
            raise HilError("wifi_observation.poll_interval_seconds must be 0.02..0.5")
        if (
            not isinstance(disassociation_timeout, (int, float))
            or isinstance(disassociation_timeout, bool)
            or not 0 <= float(disassociation_timeout) <= 30
        ):
            raise HilError("wifi_observation.disassociation_timeout_seconds must be 0..30")
        if shutil.which("sudo") is None:
            raise HilError("controller Wi-Fi observation requires sudo")
        helper = pathlib.Path(self.HELPER)
        if not helper.is_file() or helper.is_symlink() or not os.access(helper, os.X_OK):
            raise HilError("immutable controller Wi-Fi helper is unavailable")
        self.target_mac = target_mac
        self.poll_interval = float(poll_interval)
        self.disassociation_timeout = float(disassociation_timeout)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._guard = threading.Lock()
        self._transitions: list[dict[str, Any]] = []
        self._error: str | None = None
        self._started_us: int | None = None

    def _probe(self) -> bool:
        self.authorization_guard.require("controller Wi-Fi station probe")
        timeout = self.authorization_guard.bounded_timeout(
            5, "controller Wi-Fi station probe"
        )
        result = subprocess.run(
            [
                "sudo",
                "-n",
                self.HELPER,
                "--execute",
                "station-status",
                self.target_mac,
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
        if result.returncode != 0:
            raise HilError("controller AP station probe failed")
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise HilError("controller AP station probe returned invalid JSON") from exc
        if (
            not isinstance(value, dict)
            or value.get("operation") != "station-status"
            or value.get("interface") != "wlan0"
            or value.get("target_mac") != self.target_mac
            or not isinstance(value.get("associated"), bool)
        ):
            raise HilError("controller AP station probe returned invalid evidence")
        return bool(value["associated"])

    def _observe(self) -> None:
        previous = False
        try:
            while not self._stop.is_set():
                associated = self._probe()
                observed_us = int(time.monotonic() * 1_000_000)
                if associated != previous:
                    with self._guard:
                        self._transitions.append(
                            {
                                "host_monotonic_us": observed_us,
                                "time": utc_now(),
                                "associated": associated,
                            }
                        )
                    previous = associated
                self._stop.wait(self.poll_interval)
        except Exception as exc:
            with self._guard:
                self._error = f"{type(exc).__name__}: {exc}"
            self._stop.set()

    def begin_cycle(self) -> None:
        if self._thread is not None:
            raise HilError("controller Wi-Fi observation is already active")
        deadline = time.monotonic() + self.disassociation_timeout
        while self._probe():
            if time.monotonic() >= deadline:
                raise HilError("target station is still associated before switched power-on")
            time.sleep(self.poll_interval)
        self._stop.clear()
        self._transitions = []
        self._error = None
        self._started_us = int(time.monotonic() * 1_000_000)
        self._thread = threading.Thread(target=self._observe, daemon=True)
        self._thread.start()

    @staticmethod
    def _mapped_done_us(power_on: dict[str, Any], done: dict[str, Any]) -> int:
        fields = (
            power_on.get("edge_us"),
            power_on.get("_host_received_monotonic_us"),
            done.get("edge_us"),
            done.get("_host_received_monotonic_us"),
        )
        if any(
            not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0
            for value in fields
        ):
            raise HilError("fixture/controller timestamps cannot map the DONE edge")
        numeric_fields = [float(value) for value in fields if value is not None]
        power_edge, host_power_received, done_edge, host_done_received = numeric_fields
        if done_edge < power_edge:
            raise HilError("fixture DONE edge precedes power-on")
        mapped = int(host_power_received + done_edge - power_edge)
        if mapped > host_done_received + 100_000:
            raise HilError("mapped fixture DONE edge exceeds its host receipt timestamp")
        return mapped

    def finish_cycle(
        self,
        power_on: dict[str, Any],
        done: dict[str, Any],
        *,
        require_association: bool,
    ) -> dict[str, Any]:
        thread = self._thread
        started_us = self._started_us
        if thread is None or started_us is None:
            raise HilError("controller Wi-Fi observation was not started")
        mapped_done_us = self._mapped_done_us(power_on, done)
        self._stop.set()
        thread.join(timeout=6)
        self._thread = None
        self._started_us = None
        if thread.is_alive():
            raise HilError("controller Wi-Fi observation did not stop")
        with self._guard:
            error = self._error
            transitions = list(self._transitions)
        if error is not None:
            raise HilError(f"controller Wi-Fi observation failed: {error}")
        if mapped_done_us < started_us:
            raise HilError("mapped DONE edge precedes controller Wi-Fi observation")
        associated = False
        cursor_us = started_us
        active_us = 0
        association_observed = False
        for transition in transitions:
            transition_us = int(transition["host_monotonic_us"])
            if transition_us > mapped_done_us:
                break
            if transition_us < cursor_us:
                raise HilError("controller Wi-Fi transitions are not monotonic")
            if associated:
                active_us += transition_us - cursor_us
            associated = bool(transition["associated"])
            association_observed = association_observed or associated
            cursor_us = transition_us
        if associated:
            active_us += mapped_done_us - cursor_us
        if require_association and (not association_observed or active_us <= 0):
            raise HilError("controller AP did not observe the target Wi-Fi association")
        return {
            "source": "controller_ap_station_poll",
            "clock": "controller_monotonic_mapped_fixture_done",
            "target_mac": self.target_mac,
            "poll_interval_seconds": self.poll_interval,
            "association_observed": association_observed,
            "observed_seconds": active_us / 1_000_000,
            "observation_started_monotonic_us": started_us,
            "mapped_done_monotonic_us": mapped_done_us,
            "transitions": transitions,
        }

    def abort_cycle(self) -> None:
        thread = self._thread
        self._stop.set()
        if thread is not None:
            thread.join(timeout=6)
        self._thread = None
        self._started_us = None


class PortalClient:
    def __init__(
        self,
        config: dict[str, Any],
        simulated: bool,
        *,
        authorization_guard: AuthorizationGuard | None = None,
    ) -> None:
        self.simulated = simulated
        self.authorization_guard = authorization_guard or AuthorizationGuard()
        if not simulated:
            self.authorization_guard.require_bounded("portal client")
        self.base_url = str(config.get("base_url", "http://192.168.42.1")).rstrip("/")
        self.bad_profile = config.get("bad_credentials_profile")
        raw_cases = config.get("profile_cases")
        self.cases: list[dict[str, Any]] = []
        if simulated and raw_cases is None:
            raw_cases = [
                {
                    "case_id": case_id,
                    "operation": operation,
                    "profile": {
                        "ssid": f"sim-{hashlib.sha256(case_id.encode()).hexdigest()[:12]}",
                        "security": security,
                        "hidden": hidden,
                        **(
                            {"password": "simulated-password"}
                            if security == "wpa-personal"
                            else {}
                        ),
                        **(
                            {"identity": "simulated-user", "password": "simulated-password"}
                            if security == "wpa-enterprise"
                            else {}
                        ),
                    },
                }
                for case_id, (security, hidden, operation) in PORTAL_CASE_SPEC.items()
            ]
        if raw_cases is not None:
            self.cases = self._validate_cases(raw_cases)
        if not simulated:
            if not self.base_url.startswith("http://"):
                raise HilError("portal_tests.base_url must be an HTTP URL on the isolated setup AP")
            if not isinstance(self.bad_profile, dict):
                raise HilError("portal_tests.bad_credentials_profile is required")
            if len(self.cases) != len(PORTAL_CASE_SPEC):
                raise HilError("portal_tests.profile_cases must contain the complete fixed matrix")
            bad_ssid = self.bad_profile.get("ssid")
            bad_security = self.bad_profile.get("security")
            bad_password = self.bad_profile.get("password")
            if (
                not isinstance(bad_ssid, str)
                or not bad_ssid
                or len(bad_ssid.encode("utf-8")) > 32
                or not isinstance(self.bad_profile.get("hidden"), bool)
                or bad_security not in {"wpa-personal", "wpa-enterprise"}
                or not isinstance(bad_password, str)
                or not bad_password
                or (
                    bad_security == "wpa-enterprise"
                    and not isinstance(self.bad_profile.get("identity"), str)
                )
            ):
                raise HilError("portal_tests.bad_credentials_profile is invalid")

    @staticmethod
    def _validate_cases(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            raise HilError("portal_tests.profile_cases must be an array")
        validated: dict[str, dict[str, Any]] = {}
        for item in value:
            if not isinstance(item, dict):
                raise HilError("portal profile case must be an object")
            case_id = item.get("case_id")
            operation = item.get("operation")
            profile = item.get("profile")
            if (
                not isinstance(case_id, str)
                or case_id not in PORTAL_CASE_SPEC
                or case_id in validated
                or not isinstance(profile, dict)
            ):
                raise HilError("portal profile case ID/profile is invalid or duplicated")
            expected_security, expected_hidden, expected_operation = PORTAL_CASE_SPEC[case_id]
            expected_profile_fields = {"ssid", "security", "hidden"}
            if expected_security == "wpa-personal":
                expected_profile_fields.add("password")
            elif expected_security == "wpa-enterprise":
                expected_profile_fields.update({"identity", "password"})
            if (
                operation != expected_operation
                or set(profile) != expected_profile_fields
                or profile.get("security") != expected_security
                or profile.get("hidden") is not expected_hidden
            ):
                raise HilError(f"portal profile case {case_id} does not match its fixed coverage")
            ssid = profile.get("ssid")
            identity = profile.get("identity")
            password = profile.get("password")
            if (
                not isinstance(ssid, str)
                or not ssid
                or len(ssid.encode("utf-8")) > 32
                or any(character in ssid for character in ("\x00", "\r", "\n"))
            ):
                raise HilError(f"portal profile case {case_id} has an invalid SSID")
            if expected_security == "open":
                if identity is not None or password is not None:
                    raise HilError("open portal profile must not include credentials")
            elif expected_security == "wpa-personal":
                if (
                    identity is not None
                    or not isinstance(password, str)
                    or any(character in password for character in ("\x00", "\r", "\n"))
                    or not (
                        8 <= len(password) <= 63
                        or (
                            len(password) == 64
                            and re.fullmatch(r"[0-9A-Fa-f]{64}", password) is not None
                        )
                    )
                ):
                    raise HilError("WPA Personal portal profile credentials are invalid")
            elif (
                not isinstance(identity, str)
                or re.fullmatch(r"[A-Za-z0-9_.@+-]{1,128}", identity) is None
                or not isinstance(password, str)
                or not 8 <= len(password) <= 128
                or any(ord(character) < 0x21 or ord(character) > 0x7E for character in password)
                or any(character in password for character in ('"', "\\"))
            ):
                raise HilError("WPA Enterprise portal profile credentials are invalid")
            validated[case_id] = {
                "case_id": case_id,
                "operation": operation,
                "profile": dict(profile),
            }
        if set(validated) != set(PORTAL_CASE_SPEC):
            raise HilError("portal profile cases do not provide exact required coverage")
        return [validated[case_id] for case_id in PORTAL_CASE_SPEC]

    def _request(self, path: str, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
        if self.simulated:
            return 200, {"operation": {"state": "failed", "error": "network_test_failed"}}
        self.authorization_guard.require(f"portal request {path}")
        data = None
        headers: dict[str, str] = {}
        method = "GET"
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
            method = "POST"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            timeout = self.authorization_guard.bounded_timeout(10, f"portal request {path}")
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed isolated lab URL
                body = response.read(64 * 1024)
                return response.status, json.loads(body)
        except (OSError, json.JSONDecodeError) as exc:
            raise HilError(f"portal request failed: {path}") from exc

    def prove_client_ap_transition(self) -> dict[str, Any]:
        if self.simulated:
            return {"simulated": True, "status": "reachable"}
        status, value = self._request("/api/status")
        if status != 200 or not isinstance(value, dict):
            raise HilError("setup portal did not become reachable over its Wi-Fi AP")
        return {"status": "reachable", "http_status": status}

    def begin_profile_case(self, case: dict[str, Any]) -> dict[str, Any]:
        case_id = str(case["case_id"])
        operation = str(case["operation"])
        profile = case["profile"]
        assert isinstance(profile, dict)
        endpoint = "/api/network/test" if operation == "test" else "/api/network/force-save"
        if self.simulated:
            operation_id = f"simulated-{case_id}"
            status = 202
        else:
            status, accepted = self._request(endpoint, profile)
            if status != 202 or not isinstance(accepted, dict):
                raise HilError(f"portal did not acknowledge matrix case {case_id}")
            accepted_operation_id = accepted.get("operation_id")
            if not isinstance(accepted_operation_id, str) or not accepted_operation_id:
                raise HilError(f"portal matrix case {case_id} lacks an operation ID")
            operation_id = accepted_operation_id
        ssid = str(profile["ssid"])
        return {
            "case_id": case_id,
            "operation": operation,
            "http_status": status,
            "ack_before_radio_change": True,
            "operation_id_sha256": hashlib.sha256(operation_id.encode("utf-8")).hexdigest(),
            "ssid_sha256": hashlib.sha256(ssid.encode("utf-8")).hexdigest(),
            "security": profile["security"],
            "hidden": profile["hidden"],
        }

    def secret_values(self) -> tuple[str, ...]:
        values: set[str] = set()
        for case in self.cases:
            profile = case["profile"]
            assert isinstance(profile, dict)
            for key in ("identity", "password"):
                value = profile.get(key)
                if isinstance(value, str) and value:
                    values.add(value)
        if isinstance(self.bad_profile, dict):
            for key in ("identity", "password"):
                value = self.bad_profile.get(key)
                if isinstance(value, str) and value:
                    values.add(value)
        return tuple(sorted(values))

    def prove_bad_credentials_rollback(self) -> dict[str, Any]:
        if self.simulated:
            return {
                "simulated": True,
                "http_status": 202,
                "ack_before_radio_change": True,
                "operation_id_sha256": hashlib.sha256(b"simulated-bad-operation").hexdigest(),
                "operation": "failed-and-rolled-back",
                "secret_recorded": False,
            }
        assert isinstance(self.bad_profile, dict)
        status, accepted = self._request("/api/network/test", self.bad_profile)
        if status != 202 or not isinstance(accepted, dict):
            raise HilError("portal did not acknowledge the bad-credential test before radio change")
        operation_id = accepted.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            raise HilError("portal bad-credential operation lacks an ID")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                status, value = self._request("/api/status")
            except HilError:
                time.sleep(1)
                continue
            operation = value.get("operation") if isinstance(value, dict) else None
            if (
                status == 200
                and isinstance(operation, dict)
                and operation.get("operation_id") == operation_id
                and operation.get("state") == "failed"
                and operation.get("error") == "network_test_failed"
            ):
                return {
                    "http_status": 202,
                    "ack_before_radio_change": True,
                    "operation_id_sha256": hashlib.sha256(
                        operation_id.encode("utf-8")
                    ).hexdigest(),
                    "operation": "failed-and-rolled-back",
                    "secret_recorded": False,
                }
            time.sleep(1)
        raise HilError("portal did not roll back the bad-credential transaction")


class FieldUsbGuard:
    """A controller-side probe that succeeds only when the USB data link is absent."""

    def __init__(
        self,
        command: list[str] | None,
        simulated: bool,
        *,
        support_files: list[str] | None = None,
        authorization_guard: AuthorizationGuard | None = None,
    ) -> None:
        self.command = command
        self.simulated = simulated
        self.authorization_guard = authorization_guard or AuthorizationGuard()
        self.binding: BoundCommand | None = None
        if not simulated:
            self.binding = BoundCommand.create(
                command,
                support_files,
                label="field_cycle.usb_disconnected_probe",
                require_output=False,
            )

    @property
    def tool_identity(self) -> dict[str, Any] | None:
        return self.binding.identity if self.binding is not None else None

    def bind_authorization(self, authorization_guard: AuthorizationGuard) -> None:
        self.authorization_guard = authorization_guard

    def validate_configuration(self) -> None:
        if self.simulated:
            return
        if self.binding is None:
            raise HilError("field_cycle.usb_disconnected_probe is not content-bound")
        self.binding.assert_current()

    def assert_disconnected(self) -> dict[str, Any]:
        if self.simulated:
            return {"simulated": True, "usb_management": "disconnected"}
        self.authorization_guard.require_bounded("USB-disconnected field probe")
        self.validate_configuration()
        assert self.binding is not None
        self.authorization_guard.require("USB-disconnected field probe")
        timeout = self.authorization_guard.bounded_timeout(
            30, "USB-disconnected field probe"
        )
        result = subprocess.run(
            list(self.binding.canonical_argv),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
        self.binding.assert_current()
        if result.returncode != 0:
            raise HilError("field-cycle probe detected a connected USB management path")
        return {
            "usb_management": "disconnected",
            "probe_exit_code": result.returncode,
            "probe_tool_identity": self.binding.identity,
        }


class Orchestrator:
    def __init__(
        self,
        *,
        fixture: Fixture,
        target: Target,
        camera: Camera,
        artifact_directory: pathlib.Path,
        config: dict[str, Any],
        simulated: bool,
        scenario_controller: ScenarioController | None = None,
        wifi_observer: WifiActivityObserver | None = None,
        portal_client: PortalClient | None = None,
        field_usb_guard: FieldUsbGuard | None = None,
        safety_gate: dict[str, Any] | None = None,
        expected_release_id: str = "built-in",
        expected_execution_source: str = "built-in",
        target_identity: dict[str, Any] | None = None,
        controller_artifact_policy: dict[str, Any] | None = None,
        controller_machine_id_sha256: str | None = None,
        hardware_inventory: dict[str, dict[str, str]] | None = None,
        tool_identities: dict[str, dict[str, Any]] | None = None,
        accelerated_cycle_campaign: bool = False,
        authorization_guard: AuthorizationGuard | None = None,
    ) -> None:
        self.fixture = fixture
        self.target = target
        self.camera = camera
        self.artifact_directory = artifact_directory
        self.config = config
        self.simulated = simulated
        self.scenario_controller = scenario_controller or SimulationScenarioController()
        self.wifi_observer = wifi_observer or SimulationWifiActivityObserver()
        self.portal_client = portal_client or PortalClient({}, simulated=True)
        self.field_usb_guard = field_usb_guard or FieldUsbGuard(None, simulated=True)
        self.safety_gate = safety_gate
        self.expected_release_id = expected_release_id
        self.expected_execution_source = expected_execution_source
        self.target_identity = target_identity or {
            "release_id": expected_release_id,
            "execution_source": expected_execution_source,
            "image_build_id": "simulated-image-build",
            "image_sha256": "0" * 64,
            "image_manifest_sha256": "0" * 64,
            "attestation": "simulation",
        }
        self.controller_artifact_policy = controller_artifact_policy
        self.controller_machine_id_sha256 = controller_machine_id_sha256 or "0" * 64
        self.hardware_inventory = hardware_inventory
        self.tool_identities = tool_identities
        self.accelerated_cycle_campaign = accelerated_cycle_campaign
        self.authorization_guard = authorization_guard or AuthorizationGuard()
        if not simulated:
            self.authorization_guard.require_bounded("HIL orchestration")
        self._last_run_id: str | None = None

    def _authorize(self, action: str) -> None:
        self.authorization_guard.require(action)

    @property
    def boot_timeout(self) -> float:
        return float(self.config.get("timing", {}).get("boot_timeout_seconds", 90))

    @property
    def done_timeout(self) -> float:
        return float(self.config.get("timing", {}).get("done_timeout_seconds", 180))

    def _maintenance_boot(self) -> None:
        self._authorize("maintenance boot")
        self.fixture.set_mode("setup")
        self.fixture.set_power(True)
        self.target.wait_available(self.boot_timeout)

    def _power_off_and_verify(self) -> dict[str, Any]:
        self.fixture.set_power(False)
        self.target.wait_unavailable(
            float(self.config.get("timing", {}).get("power_off_timeout_seconds", 15))
        )
        return self.fixture.measure_rails()

    def _start_uart(self, output: pathlib.Path) -> UartCapture | None:
        if self.simulated:
            return None
        uart_config = self.config.get("uart", {})
        uart = UartCapture(
            str(uart_config["port"]),
            int(uart_config.get("baudrate", 115200)),
            output,
            authorization_guard=self.authorization_guard,
        )
        uart.start()
        return uart

    def _finish_uart(
        self,
        uart: UartCapture | None,
        *,
        required_event: str | None = None,
        required_phase: str | None = None,
        required_release_id: str | None = None,
        required_bundle_sha256: str | None = None,
        require_runtime_phase: bool = False,
        require_target_identity: bool = False,
    ) -> dict[str, Any]:
        if uart is None:
            return {"simulated": True, "evidence": "not-opened"}
        evidence = uart.close()
        events = evidence.pop("_events")
        assert isinstance(events, list)
        if require_runtime_phase and not any(
            item.get("event") in {"phase_started", "phase_finished"} for item in events
        ):
            raise HilError("UART evidence lacks a schema-1 runtime phase event")
        if required_event is None and required_phase is not None and not any(
            item.get("event") in {"phase_started", "phase_finished"}
            and str(item.get("phase", "")).casefold() == required_phase.casefold()
            for item in events
        ):
            raise HilError(f"UART evidence lacks the requested lifecycle phase {required_phase}")
        if required_event is None and required_phase is not None:
            evidence["required_phase"] = required_phase
            evidence["runtime_phase_matched"] = True
        if required_event is not None:
            matches = [
                item
                for item in events
                if item.get("event") == required_event
                and (
                    required_phase is None
                    or str(item.get("phase", "")).casefold() == required_phase.casefold()
                )
                and (
                    required_release_id is None
                    or item.get("release_id") == required_release_id
                )
                and (
                    required_bundle_sha256 is None
                    or item.get("bundle_sha256") == required_bundle_sha256
                )
            ]
            if len(matches) != 1:
                raise HilError(
                    f"UART evidence must contain exactly one {required_event}/{required_phase} event"
                )
            evidence["required_event"] = required_event
            if required_phase is not None:
                evidence["required_phase"] = required_phase
            if required_release_id is not None:
                evidence["required_release_id"] = required_release_id
            if required_bundle_sha256 is not None:
                evidence["required_bundle_sha256"] = required_bundle_sha256
        if require_target_identity:
            matches = [item for item in events if item.get("event") == "target_identity"]
            if len(matches) != 1:
                raise HilError("field UART evidence must contain exactly one target_identity event")
            identity = matches[0]
            expected = self.target_identity
            if (
                identity.get("release_id") != expected.get("release_id")
                or identity.get("execution_source") != expected.get("execution_source")
                or identity.get("image_build_id") != expected.get("image_build_id")
            ):
                raise HilError("field UART target identity differs from signed image/release identity")
            evidence["target_identity"] = {
                "release_id": identity["release_id"],
                "execution_source": identity["execution_source"],
                "image_build_id": identity["image_build_id"],
                "matched_signed_target_identity": True,
            }
        return evidence

    def _finish_wifi_observation(
        self,
        power_on: dict[str, Any],
        done: dict[str, Any],
        *,
        require_association: bool,
    ) -> dict[str, Any]:
        evidence = self.wifi_observer.finish_cycle(
            power_on,
            done,
            require_association=require_association,
        )
        if (
            evidence.get("source") != "controller_ap_station_poll"
            or evidence.get("clock") != "controller_monotonic_mapped_fixture_done"
            or not isinstance(evidence.get("observed_seconds"), (int, float))
            or isinstance(evidence.get("observed_seconds"), bool)
            or evidence["observed_seconds"] < 0
            or (require_association and evidence.get("association_observed") is not True)
        ):
            raise HilError("controller Wi-Fi activity evidence is incomplete")
        return evidence

    def _matrix_cold_cycle(
        self,
        directory: pathlib.Path,
        *,
        require_online: bool,
    ) -> dict[str, Any]:
        self._authorize("Wi-Fi matrix cold cycle")
        directory.mkdir()
        item: dict[str, Any] = {"started": utc_now(), "outcome": "running"}
        uart = self._start_uart(directory / "uart.log")
        wifi_observation_finished = False
        try:
            self.fixture.set_mode("run")
            self.wifi_observer.begin_cycle()
            item["power_on"] = self.fixture.set_power(True)
            self.target.wait_available(
                float(self.config.get("timing", {}).get("management_probe_seconds", 15))
            )
            item["usb_ssh_while_faulted_run"] = "reachable"
            item["done"] = self.fixture.wait_done(self.done_timeout)
            item["wifi_activity"] = self._finish_wifi_observation(
                item["power_on"],
                item["done"],
                require_association=require_online,
            )
            item["wifi_active_controller_seconds"] = item["wifi_activity"]["observed_seconds"]
            wifi_observation_finished = True
            item["cold_wake_to_done_seconds"] = self._cold_wake_to_done_seconds(
                item["power_on"], item["done"]
            )
            item["wifi_power_budget_seconds"] = item["cold_wake_to_done_seconds"]
            self.target.wait_unavailable(
                float(self.config.get("timing", {}).get("power_off_timeout_seconds", 15))
            )
            item["rails_after_done"] = self.fixture.measure_rails()
        finally:
            if not wifi_observation_finished:
                self.wifi_observer.abort_cycle()
            if uart is not None:
                item["uart"] = self._finish_uart(uart, require_runtime_phase=True)
        item["camera"] = self.camera.capture(directory / "display.jpg")
        self._maintenance_boot()
        item["target"] = self.target.collect()
        self._validate_cycle_evidence(
            item["done"],
            item["target"],
            item["started"],
            require_online=require_online,
        )
        item["outcome"] = "success"
        item["finished"] = utc_now()
        write_json(directory / "result.json", item)
        return item

    @staticmethod
    def _validate_profile_evidence(
        expected: dict[str, Any], evidence: dict[str, Any]
    ) -> None:
        if evidence != {
            **{
                "schema_version": 1,
                "event": "network_profile_evidence",
                "case_id": expected["case_id"],
                "ssid_sha256": expected["ssid_sha256"],
                "security": expected["security"],
                "hidden": expected["hidden"],
                "operation": expected["operation"],
                "operation_state": "succeeded",
                "profile_mode": "0600",
                "profile_owner": "root",
                "profile_group": "root",
                "networkmanager_loaded": True,
            },
            **({"simulated": True} if evidence.get("simulated") is True else {}),
        }:
            raise HilError(f"NetworkManager evidence is incomplete for {expected['case_id']}")

    def _local_secret_artifact_scan(self) -> dict[str, Any]:
        secrets = _private_value_patterns(self.portal_client.secret_values())
        files_scanned = 0
        matches = 0
        for path in self.artifact_directory.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            files_scanned += 1
            try:
                payload = path.read_bytes()
            except OSError as exc:
                raise HilError(f"cannot scan HIL artifact for portal secrets: {path.name}") from exc
            matches += sum(payload.count(secret) for secret in secrets if secret)
        if matches:
            raise HilError("credential material appeared in HIL artifacts")
        result = {
            "passed": True,
            "files_scanned": files_scanned,
            "forbidden_matches": 0,
            "method": "exact_private_config_credential_match",
        }
        if (
            set(result) != {"passed", "files_scanned", "forbidden_matches", "method"}
            or type(result["files_scanned"]) is not int
            or result["files_scanned"] < 1
            or type(result["forbidden_matches"]) is not int
            or result["forbidden_matches"] != 0
        ):
            raise HilError("controller secret-artifact scan did not inspect any evidence")
        return result

    def _assert_secret_free_value(self, value: Any) -> None:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        private_values = set(self.portal_client.secret_values())
        for case in self.portal_client.cases:
            profile = case["profile"]
            assert isinstance(profile, dict)
            ssid = profile.get("ssid")
            if isinstance(ssid, str) and ssid:
                private_values.add(ssid)
        if any(pattern in payload for pattern in _private_value_patterns(private_values)):
            raise HilError("private portal material would be serialized into HIL evidence")

    def _run_portal_matrix(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        timeout = float(self.config.get("timing", {}).get("management_probe_seconds", 15))
        results: list[dict[str, Any]] = []
        for case in self.portal_client.cases:
            self._authorize(f"portal matrix case {case['case_id']}")
            item: dict[str, Any] = {
                "case_id": case["case_id"],
                "started": utc_now(),
                "outcome": "running",
            }
            self._maintenance_boot()
            try:
                self.target.wait_available(timeout)
                item["controller_portal_apply"] = self.scenario_controller.run_portal_case(
                    str(case["case_id"]), "apply"
                )
                self.target.wait_available(timeout)
                item["usb_ssh_during_portal"] = "reachable"
                item["portal_reachable"] = self.portal_client.prove_client_ap_transition()
                request = self.portal_client.begin_profile_case(case)
                item["request"] = request
                # The HTTP 202 response is complete before this transition
                # makes the case-specific deterministic test AP available.
                item["controller_test_plane"] = self.scenario_controller.run_portal_case(
                    str(case["case_id"]), "test-plane"
                )
                self.target.wait_available(self.boot_timeout)
                item["usb_ssh_after_radio_change"] = "reachable"
                evidence = self.target.network_profile_evidence(
                    request["case_id"],
                    request["ssid_sha256"],
                    request["security"],
                    request["hidden"],
                    request["operation"],
                )
                self._validate_profile_evidence(request, evidence)
                item["networkmanager_profile"] = evidence
                item["transaction_coverage"] = {
                    "test_proven": request["operation"] == "test",
                    "save_proven": True,
                    "force_save_proven": request["operation"] == "force-save",
                }
                item["outcome"] = "success"
                item["finished"] = utc_now()
            finally:
                item["controller_portal_reset"] = self.scenario_controller.run_portal_case(
                    str(case["case_id"]), "reset"
                )
                self._power_off_and_verify()
            results.append(item)

        bad: dict[str, Any] = {
            "case_id": "failed_credentials_rollback",
            "started": utc_now(),
            "outcome": "running",
        }
        self._authorize("failed-credential rollback case")
        self._maintenance_boot()
        reset_complete = False
        try:
            self.target.wait_available(timeout)
            bad["controller_portal_apply"] = self.scenario_controller.run(
                "client_ap_transition", "apply"
            )
            bad["usb_ssh_before_transaction"] = "reachable"
            bad["portal"] = self.portal_client.prove_bad_credentials_rollback()
            assert isinstance(self.portal_client.bad_profile, dict)
            bad_ssid = str(self.portal_client.bad_profile.get("ssid", ""))
            bad_hash = hashlib.sha256(bad_ssid.encode("utf-8")).hexdigest()
            rollback = self.target.network_rollback_evidence(bad_hash)
            expected_rollback = {
                "schema_version": 1,
                "event": "network_rollback_evidence",
                "ssid_sha256": bad_hash,
                "profile_absent": True,
                "active_profile_restored": True,
            }
            if rollback not in (
                expected_rollback,
                {**expected_rollback, "simulated": True},
            ):
                raise HilError("NetworkManager did not prove failed-profile rollback")
            bad["networkmanager_rollback"] = rollback
            self.target.wait_available(timeout)
            bad["usb_ssh_after_rollback"] = "reachable"
            bad["controller_portal_reset"] = self.scenario_controller.run(
                "client_ap_transition", "reset"
            )
            reset_complete = True
            bad["outcome"] = "success"
            bad["finished"] = utc_now()
        finally:
            if not reset_complete:
                self.scenario_controller.run("client_ap_transition", "reset")
            self._power_off_and_verify()
        results.append(bad)

        self._maintenance_boot()
        try:
            target_scan = self.target.network_secret_artifact_scan(
                self.portal_client.secret_values()
            )
            expected_target_keys = {
                "schema_version",
                "event",
                "passed",
                "files_scanned",
                "forbidden_matches",
            }
            if (
                not isinstance(target_scan, dict)
                or set(target_scan) != expected_target_keys
                or type(target_scan.get("schema_version")) is not int
                or target_scan.get("schema_version") != 1
                or target_scan.get("event") != "network_secret_artifact_scan"
                or target_scan.get("passed") is not True
                or type(target_scan.get("files_scanned")) is not int
                or target_scan["files_scanned"] < 1
                or type(target_scan.get("forbidden_matches")) is not int
                or target_scan["forbidden_matches"] != 0
            ):
                raise HilError("target secret-artifact scan did not pass")
        finally:
            self._power_off_and_verify()
        return results, {
            "target": target_scan,
            "controller_artifacts": self._local_secret_artifact_scan(),
        }

    def _run_wifi_matrix(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        precondition = self._matrix_cold_cycle(
            self.artifact_directory / "wifi-matrix-precondition",
            require_online=True,
        )
        self._power_off_and_verify()
        write_json(self.artifact_directory / "wifi-matrix-precondition.json", precondition)
        portal_matrix, secret_scan = self._run_portal_matrix()
        results["client_ap_transition"] = {
            "scenario": "client_ap_transition",
            "outcome": "success",
            "profile_cases_completed": len(PORTAL_CASE_SPEC),
            "usb_management_reachable": True,
        }
        results["bad_credentials"] = {
            "scenario": "bad_credentials",
            "outcome": "success",
            "rollback_case_id": "failed_credentials_rollback",
            "rollback_proven": True,
        }

        fault_kinds = {
            "dns_failure": "dns",
            "tls_failure": "tls",
            "http_failure": "http_503",
        }
        for scenario in ("wifi_disconnect", *fault_kinds):
            self._authorize(f"Wi-Fi fault scenario {scenario}")
            item: dict[str, Any] = {
                "scenario": scenario,
                "started": utc_now(),
                "outcome": "running",
            }
            self._maintenance_boot()
            try:
                if scenario == "wifi_disconnect":
                    item["apply"] = self.scenario_controller.run(scenario, "apply")
                    item["probe"] = self.scenario_controller.run(scenario, "probe")
                else:
                    kind = fault_kinds[scenario]
                    item["apply"] = self.target.set_noaa_fault(kind)
                    if item["apply"] != {"pending": True, "kind": kind}:
                        raise HilError("target did not persist the requested one-shot NOAA fault")
                    item["probe"] = self.target.noaa_fault_status()
                    if item["probe"] != {"pending": True, "kind": kind}:
                        raise HilError("target NOAA fault request is not active")
            finally:
                item["rails_after_fault_staging"] = self._power_off_and_verify()

            fault_cycle = self._matrix_cold_cycle(
                self.artifact_directory / f"wifi-matrix-{scenario}-fault",
                require_online=False,
            )
            item["fault_cycle"] = fault_cycle
            result = fault_cycle["target"]["last_result"]
            if result.get("success") is not True or (
                not self.simulated and result.get("used_cached_data") is not True
            ):
                raise HilError(f"{scenario} did not render a successful cached/offline cycle")
            if scenario == "wifi_disconnect":
                if not self.simulated and result.get("production_route_interface") is not None:
                    raise HilError("Wi-Fi disconnect scenario retained a production route")
                item["reset"] = self.scenario_controller.run(scenario, "reset")
            else:
                expected_fault = fault_kinds[scenario]
                if result.get("injected_noaa_fault") != expected_fault:
                    raise HilError(f"{scenario} did not execute its injected transport fault")
                status = self.target.noaa_fault_status()
                if status != {"pending": False, "kind": None}:
                    raise HilError("one-shot NOAA fault was not consumed")
                item["reset"] = self.target.clear_noaa_fault()
            self.target.wait_available(
                float(self.config.get("timing", {}).get("management_probe_seconds", 15))
            )
            self._power_off_and_verify()

            item["recovery_cycle"] = self._matrix_cold_cycle(
                self.artifact_directory / f"wifi-matrix-{scenario}-recovery",
                require_online=True,
            )
            self._power_off_and_verify()
            item["outcome"] = "success"
            item["finished"] = utc_now()
            results[scenario] = item
        return [results[scenario] for scenario in WIFI_SCENARIOS], portal_matrix, secret_scan

    def _run_power_cut_recoveries(self, phases: tuple[str, ...]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        power_timeout = float(self.config.get("timing", {}).get("power_off_timeout_seconds", 15))
        for phase in phases:
            self._authorize(f"lifecycle power-cut phase {phase}")
            phase_dir = self.artifact_directory / f"power-cut-{phase}"
            phase_dir.mkdir()
            item: dict[str, Any] = {"phase": phase, "started": utc_now(), "outcome": "running"}
            cut_uart = self._start_uart(phase_dir / "cut-uart.log")
            try:
                self.fixture.set_mode("run")
                self.fixture.set_power(True)
                item["cut"] = self.fixture.wait_phase_cut(phase, self.done_timeout)
                self.target.wait_unavailable(power_timeout)
                item["rails_after_cut"] = self.fixture.measure_rails()
            finally:
                if cut_uart is not None:
                    item["cut_uart"] = self._finish_uart(
                        cut_uart,
                        required_phase=phase,
                        require_runtime_phase=True,
                    )

            recovery_uart = self._start_uart(phase_dir / "recovery-uart.log")
            wifi_observation_finished = False
            try:
                self.fixture.set_mode("run")
                self.wifi_observer.begin_cycle()
                item["recovery_power_on"] = self.fixture.set_power(True)
                item["recovery_done"] = self.fixture.wait_done(self.done_timeout)
                item["recovery_wifi_activity"] = self._finish_wifi_observation(
                    item["recovery_power_on"],
                    item["recovery_done"],
                    require_association=True,
                )
                wifi_observation_finished = True
                item["recovery_cold_wake_to_done_seconds"] = self._cold_wake_to_done_seconds(
                    item["recovery_power_on"], item["recovery_done"]
                )
                item["recovery_wifi_power_budget_seconds"] = item[
                    "recovery_cold_wake_to_done_seconds"
                ]
                self.target.wait_unavailable(power_timeout)
                item["rails_after_recovery"] = self.fixture.measure_rails()
            finally:
                if not wifi_observation_finished:
                    self.wifi_observer.abort_cycle()
                if recovery_uart is not None:
                    item["recovery_uart"] = self._finish_uart(
                        recovery_uart, require_runtime_phase=True
                    )
            item["camera"] = self.camera.capture(phase_dir / "recovery-display.jpg")
            self._maintenance_boot()
            item["target"] = self.target.collect()
            self._validate_cycle_evidence(item["recovery_done"], item["target"], item["started"])
            self._power_off_and_verify()
            item["finished"] = utc_now()
            item["outcome"] = "success"
            write_json(phase_dir / "result.json", item)
            results.append(item)
        return results

    def _run_update_power_cut_recoveries(
        self, bundle: pathlib.Path, phases: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        power_timeout = float(self.config.get("timing", {}).get("power_off_timeout_seconds", 15))
        expected_states = {
            "begin": "uploading",
            "stage": "staged",
            "verify": "verified",
            "activate": "trial",
        }
        bundle_digest = sha256_file(bundle)
        for phase in phases:
            self._authorize(f"update power-cut phase {phase}")
            phase_dir = self.artifact_directory / f"update-power-cut-{phase}"
            phase_dir.mkdir()
            item: dict[str, Any] = {
                "phase": phase,
                "release_id": self.expected_release_id,
                "bundle_sha256": bundle_digest,
                "started": utc_now(),
                "outcome": "running",
            }
            self._maintenance_boot()
            cut_uart = self._start_uart(phase_dir / "cut-uart.log")
            handle: UpdateCutHandle | None = None
            try:
                handle = self.target.begin_update_cut(
                    bundle, phase, self.expected_release_id
                )
                item["cut"] = self.fixture.wait_update_cut(phase, self.done_timeout)
                self.target.wait_unavailable(power_timeout)
                item["rails_after_cut"] = self.fixture.measure_rails()
                item["transport"] = self.target.finish_update_cut(handle)
            except Exception:
                try:
                    self.fixture.set_power(False)
                finally:
                    if handle is not None and handle.process is not None:
                        with suppress(Exception):
                            self.target.finish_update_cut(handle)
                raise
            finally:
                if cut_uart is not None:
                    item["cut_uart"] = self._finish_uart(
                        cut_uart,
                        required_event="update_phase_boundary",
                        required_phase=phase,
                        required_release_id=self.expected_release_id,
                        required_bundle_sha256=bundle_digest,
                    )

            self._maintenance_boot()
            try:
                recovery = self.target.recover_update_cut(
                    phase, self.expected_release_id, bundle_digest
                )
                expected_recovery = {
                    "schema_version": 1,
                    "event": "update_power_cut_recovery",
                    "phase": phase,
                    "release_id": self.expected_release_id,
                    "bundle_sha256": bundle_digest,
                    "recovered": True,
                    "state_valid": True,
                    "observed_state": expected_states[phase],
                    "cleanup_complete": True,
                    "post_cleanup_clean": True,
                    "fallback_used": False,
                }
                if recovery not in (
                    expected_recovery,
                    {**expected_recovery, "simulated": True},
                ):
                    raise HilError(f"update {phase} power-cut recovery evidence is incomplete")
                item["target_update_status"] = recovery
                item["cold_setup_recovery"] = "reachable-over-usb"
                item["camera"] = self.camera.capture(
                    phase_dir / "recovery-display.jpg"
                )
            finally:
                item["rails_after_recovery"] = self._power_off_and_verify()
            item["finished"] = utc_now()
            item["outcome"] = "success"
            write_json(phase_dir / "result.json", item)
            results.append(item)
        return results

    def _validate_cycle_evidence(
        self,
        done: dict[str, Any],
        target: dict[str, Any],
        orchestrator_started: str,
        *,
        require_online: bool = True,
    ) -> None:
        result = target.get("last_result", {})
        run_id = result.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise HilError("collected cycle result lacks a run ID")
        if run_id == self._last_run_id:
            raise HilError("collected cycle result reused the prior run ID")
        metrics = target.get("metrics")
        supervisor = target.get("supervisor")
        execution = supervisor.get("execution") if isinstance(supervisor, dict) else None
        if (
            not isinstance(metrics, dict)
            or not isinstance(supervisor, dict)
            or not isinstance(execution, dict)
        ):
            raise HilError("cycle lacks metrics or release-supervisor evidence")
        expected_id = self.expected_release_id
        expected_source = self.expected_execution_source
        if (
            supervisor.get("schema_version") != 1
            or supervisor.get("event") != "release_supervision"
            or execution.get("expected_release_id") != expected_id
            or execution.get("expected_execution_source") != expected_source
            or execution.get("expected_release_executed_successfully") is not True
            or execution.get("fallback_used") is not False
            or execution.get("trial_rolled_back") is not False
            or execution.get("selected_run_id") != run_id
            or execution.get("selected_release_id") != expected_id
            or execution.get("selected_execution_source") != expected_source
            or execution.get("selected_success") is not True
            or result.get("release_id") != expected_id
            or result.get("execution_source") != expected_source
            or metrics.get("release_id") != expected_id
            or metrics.get("execution_source") != expected_source
        ):
            raise HilError("cycle did not execute the expected release source without fallback")
        if require_online and (
            result.get("success") is not True
            or metrics.get("success") is not True
            or result.get("network_interface") != "wlan0"
            or result.get("production_route_interface") != "wlan0"
            or not isinstance(metrics.get("wifi_active_observed_seconds"), (int, float))
            or isinstance(metrics.get("wifi_active_observed_seconds"), bool)
            or result.get("wifi_active_observation_source") != "runtime"
        ):
            raise HilError("normal cycle did not complete successfully through wlan0")
        if self.simulated or not require_online:
            self._last_run_id = run_id
            return
        try:
            orchestration_time = datetime.fromisoformat(orchestrator_started.replace("Z", "+00:00"))
            result_started = datetime.fromisoformat(
                str(result["started_at"]).replace("Z", "+00:00")
            )
            result_ended = datetime.fromisoformat(str(result["ended_at"]).replace("Z", "+00:00"))
            done_received = datetime.fromisoformat(
                str(done["_host_received_at"]).replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HilError(
                "cycle evidence lacks valid orchestration/result/DONE timestamps"
            ) from exc
        skew_seconds = float(self.config.get("timing", {}).get("clock_skew_seconds", 5))
        if skew_seconds < 0 or skew_seconds > 30:
            raise HilError("timing.clock_skew_seconds must be 0..30")
        skew = timedelta(seconds=skew_seconds)
        if result_started < orchestration_time - skew or result_ended < result_started:
            raise HilError("durable cycle timestamps precede this orchestrated RUN")
        if result_ended > done_received + skew:
            raise HilError("durable cycle result ends after the captured DONE response")
        self._last_run_id = run_id

    def _cold_wake_to_done_seconds(self, power_on: dict[str, Any], done: dict[str, Any]) -> float:
        self._validate_done_timing(done)
        power_edge = power_on.get("edge_us")
        done_edge = done.get("edge_us")
        if (
            not isinstance(power_edge, (int, float))
            or isinstance(power_edge, bool)
            or not isinstance(done_edge, (int, float))
            or isinstance(done_edge, bool)
            or power_edge < 0
            or done_edge < power_edge
        ):
            raise HilError("fixture power/DONE timestamps cannot form a cold-cycle duration")
        duration = (float(done_edge) - float(power_edge)) / 1_000_000
        if duration < 0 or duration > self.done_timeout:
            raise HilError("fixture cold wake-to-DONE duration exceeds the cycle bound")
        return duration

    def _validate_done_timing(self, done: dict[str, Any]) -> None:
        timing = self.safety_gate.get("done_pulse") if isinstance(self.safety_gate, dict) else None
        minimum = 400_000
        maximum = 600_000
        if isinstance(timing, dict):
            minimum = int(timing["minimum_us"])
            maximum = int(timing["maximum_us"])
        pulse = done.get("pulse_us")
        if (
            not isinstance(pulse, (int, float))
            or isinstance(pulse, bool)
            or not minimum <= pulse <= maximum
        ):
            raise HilError(f"fixture DONE pulse is outside the signed {minimum}..{maximum} us gate")

    def _run_cycle(self, cycle_number: int, session: dict[str, Any]) -> None:
        self._authorize(f"cycle {cycle_number}")
        cycle_dir = self.artifact_directory / f"cycle-{cycle_number:03d}"
        cycle_dir.mkdir()
        cycle: dict[str, Any] = {
            "number": cycle_number,
            "started": utc_now(),
            "outcome": "running",
        }
        session["cycles"].append(cycle)
        write_json(self.artifact_directory / "session.json", session)

        uart = self._start_uart(cycle_dir / "uart.log")
        wifi_observation_finished = False
        try:
            self.fixture.set_mode("run")
            self.wifi_observer.begin_cycle()
            cycle["power_on"] = self.fixture.set_power(True)
            cycle["done"] = self.fixture.wait_done(self.done_timeout)
            cycle["wifi_activity"] = self._finish_wifi_observation(
                cycle["power_on"], cycle["done"], require_association=True
            )
            cycle["wifi_active_controller_seconds"] = cycle["wifi_activity"]["observed_seconds"]
            wifi_observation_finished = True
            cycle["cold_wake_to_done_seconds"] = self._cold_wake_to_done_seconds(
                cycle["power_on"], cycle["done"]
            )
            cycle["wifi_power_budget_seconds"] = cycle["cold_wake_to_done_seconds"]
            self.target.wait_unavailable(
                float(self.config.get("timing", {}).get("power_off_timeout_seconds", 15))
            )
            cycle["rails_after_done"] = self.fixture.measure_rails()
        finally:
            if not wifi_observation_finished:
                self.wifi_observer.abort_cycle()
            if uart is not None:
                cycle["uart"] = self._finish_uart(uart, require_runtime_phase=True)

        cycle["camera"] = self.camera.capture(cycle_dir / "display.jpg")
        self._maintenance_boot()
        cycle["target"] = self.target.collect()
        self._validate_cycle_evidence(cycle["done"], cycle["target"], cycle["started"])
        self._power_off_and_verify()
        cycle["finished"] = utc_now()
        cycle["outcome"] = "success"
        write_json(cycle_dir / "result.json", cycle)
        write_json(self.artifact_directory / "session.json", session)

    def _wait_until(self, deadline: float) -> None:
        while True:
            authorization_remaining = self.authorization_guard.remaining_seconds(
                "scheduled campaign wait"
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._authorize("campaign action after scheduled wait")
                return
            sleep_seconds = min(remaining, 30.0)
            if authorization_remaining is not None:
                sleep_seconds = min(sleep_seconds, authorization_remaining)
            time.sleep(sleep_seconds)

    def _run_field_cycle(self) -> dict[str, Any]:
        self._authorize("field cycle")
        field_dir = self.artifact_directory / "field-cycle"
        field_dir.mkdir()
        result: dict[str, Any] = {"started": utc_now(), "outcome": "running"}
        result["usb_before_power"] = self.field_usb_guard.assert_disconnected()
        uart = self._start_uart(field_dir / "uart.log")
        try:
            self.fixture.set_mode("run")
            result["power_on"] = self.fixture.set_power(True)
            probe_delay = float(
                self.config.get("field_cycle", {}).get("usb_probe_delay_seconds", 8)
            )
            if probe_delay < 0 or probe_delay > 30:
                raise HilError("field_cycle.usb_probe_delay_seconds must be 0..30")
            self._wait_until(time.monotonic() + probe_delay)
            result["usb_while_powered"] = self.field_usb_guard.assert_disconnected()
            result["done"] = self.fixture.wait_done(self.done_timeout)
            result["cold_wake_to_done_seconds"] = self._cold_wake_to_done_seconds(
                result["power_on"], result["done"]
            )
        finally:
            if uart is not None:
                result["uart"] = self._finish_uart(
                    uart,
                    require_runtime_phase=True,
                    require_target_identity=True,
                )
        result["rails_after_done"] = self.fixture.measure_rails()
        result["usb_after_power"] = self.field_usb_guard.assert_disconnected()
        result["camera"] = self.camera.capture(field_dir / "display.jpg")
        result["finished"] = utc_now()
        result["outcome"] = "success"
        write_json(field_dir / "result.json", result)
        return result

    def run(
        self,
        cycles: int,
        bundle: pathlib.Path | None,
        *,
        wifi_matrix: bool = False,
        power_cut_phases: tuple[str, ...] = (),
        update_power_cut_phases: tuple[str, ...] = (),
        field_cycle: bool = False,
        cycle_interval_seconds: float = 0.0,
    ) -> pathlib.Path:
        self.artifact_directory.mkdir(mode=0o770, parents=True, exist_ok=False)
        if not self.simulated:
            os.chmod(self.artifact_directory, 0o770)
            metadata = self.artifact_directory.stat()
            if (
                self.artifact_directory.is_symlink()
                or not stat.S_ISDIR(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o770
            ):
                raise HilError("real HIL session artifact directory is not a private directory")
        session: dict[str, Any] = {
            "schema_version": 1,
            "started": utc_now(),
            "simulated": self.simulated,
            "cycles_requested": cycles,
            "cycles": [],
            "wifi_matrix_requested": wifi_matrix,
            "power_cut_phases": list(power_cut_phases),
            "update_power_cut_phases": list(update_power_cut_phases),
            "field_cycle_requested": field_cycle,
            "expected_release_id": self.expected_release_id,
            "expected_execution_source": self.expected_execution_source,
            "target_identity": self.target_identity,
            "controller_machine_id_sha256": self.controller_machine_id_sha256,
            "accelerated_cycle_campaign": self.accelerated_cycle_campaign,
            "accelerated_cycles_required": 12 if self.accelerated_cycle_campaign else 0,
        }
        if self.hardware_inventory is not None:
            session["hardware_inventory"] = self.hardware_inventory
        if self.tool_identities is not None:
            session["tool_identities"] = self.tool_identities
        if self.controller_artifact_policy is not None:
            session["controller_artifact_policy"] = self.controller_artifact_policy
        if self.safety_gate is not None:
            session["safety_gate"] = self.safety_gate
        write_json(self.artifact_directory / "session.json", session)
        try:
            self._authorize("HIL session start")
            fixture_identity = self.fixture.identify()
            session["fixture_identity"] = fixture_identity
            if self.safety_gate is not None and (
                fixture_identity.get("hardware_id") != self.safety_gate.get("hardware_id")
                or fixture_identity.get("firmware_sha256")
                != self.safety_gate.get("fixture_firmware_sha256")
            ):
                raise HilError("live fixture identity does not match the signed safety gate")
            session["initial_rail_isolation"] = self.fixture.measure_rails()
            write_json(self.artifact_directory / "session.json", session)
            if update_power_cut_phases:
                if bundle is None:
                    raise HilError("update power-cut campaigns require the exact candidate bundle")
                if self.expected_execution_source != "verified-release":
                    raise HilError("update power cuts require the verified-release expectation")
                session["update_power_cut_recoveries"] = (
                    self._run_update_power_cut_recoveries(bundle, update_power_cut_phases)
                )
                write_json(self.artifact_directory / "session.json", session)
            if bundle is not None:
                if self.expected_execution_source != "verified-release":
                    raise HilError("a staged bundle requires the verified-release expectation")
                self._maintenance_boot()
                session["bundle"] = self.target.stage_bundle(bundle)
                stage = session["bundle"].get("stage")
                if (
                    not isinstance(stage, dict)
                    or stage.get("release_id") != self.expected_release_id
                ):
                    raise HilError("staged bundle release ID differs from the expected release")
                self._power_off_and_verify()

            if wifi_matrix:
                wifi_results, portal_results, secret_scan = self._run_wifi_matrix()
                session["wifi_matrix"] = wifi_results
                session["portal_matrix"] = portal_results
                session["portal_secret_artifact_scan"] = secret_scan
                self._assert_secret_free_value(session)
                write_json(self.artifact_directory / "session.json", session)
                session["portal_secret_artifact_scan"]["controller_artifacts"] = (
                    self._local_secret_artifact_scan()
                )
                write_json(self.artifact_directory / "session.json", session)

            if power_cut_phases:
                session["power_cut_recoveries"] = self._run_power_cut_recoveries(power_cut_phases)
                write_json(self.artifact_directory / "session.json", session)

            if field_cycle:
                session["field_cycle"] = self._run_field_cycle()
            else:
                campaign_start = time.monotonic()
                for cycle_number in range(1, cycles + 1):
                    if cycle_number > 1 and cycle_interval_seconds > 0:
                        self._wait_until(
                            campaign_start + (cycle_number - 1) * cycle_interval_seconds
                        )
                    self._run_cycle(cycle_number, session)

            self._authorize("HIL campaign completion")
            session["outcome"] = "success"
            return self.artifact_directory
        except Exception as exc:
            session["outcome"] = "failure"
            session["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            session["finished"] = utc_now()
            session["fixture_events"] = self.fixture.events
            try:
                self.fixture.set_power(False)
            except Exception as cleanup_error:
                session["cleanup_error"] = f"{type(cleanup_error).__name__}: {cleanup_error}"
            self.fixture.close()
            write_json(self.artifact_directory / "session.json", session)


def load_config(path: pathlib.Path) -> dict[str, Any]:
    def reject_duplicates(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, child in items:
            if key in value:
                raise HilError(f"HIL configuration contains duplicate field: {key}")
            value[key] = child
        return value

    try:
        with path.open("r", encoding="utf-8") as stream:
            config = json.load(stream, object_pairs_hook=reject_duplicates)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HilError("HIL configuration is unreadable or invalid JSON") from exc
    if not isinstance(config, dict):
        raise HilError("HIL configuration must be a JSON object")
    if config.get("schema_version") != 1:
        raise HilError("unsupported or missing HIL configuration schema_version")
    for section in (
        "target",
        "fixture",
        "uart",
        "safety_identity",
        "controller_artifacts",
        "camera",
        "portal_tests",
        "wifi_observation",
        "field_cycle",
        "timing",
    ):
        if section in config and not isinstance(config[section], dict):
            raise HilError(f"HIL configuration section {section} must be an object")
    return config


def validate_private_config(path: pathlib.Path) -> pathlib.Path:
    candidate = pathlib.Path(os.path.abspath(path.expanduser()))
    if candidate.is_symlink():
        raise HilError("real HIL configuration must not be a symbolic link")
    try:
        metadata = candidate.stat()
    except OSError as exc:
        raise HilError("real HIL configuration is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise HilError("real HIL configuration must be a regular mode-0600 file")
    if hasattr(os, "geteuid") and metadata.st_uid not in {0, os.geteuid()}:
        raise HilError("real HIL configuration has an unexpected owner")
    return candidate


def verify_safety_gate(
    gate_path: pathlib.Path,
    public_key: pathlib.Path,
    *,
    expected_identity: dict[str, Any],
    approval_nonce: str,
) -> dict[str, Any]:
    signature_path = gate_path.with_name(f"{gate_path.name}.sig")

    def snapshot(path: pathlib.Path, label: str, maximum: int) -> bytes:
        if path.is_symlink():
            raise HilError(f"{label} must be a regular, non-symlink file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise HilError(f"cannot open pinned {label}") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
                raise HilError(f"{label} is not regular or exceeds its size limit")
            chunks: list[bytes] = []
            remaining = maximum + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) != metadata.st_size or len(payload) > maximum:
                raise HilError(f"{label} changed while it was snapshotted")
            return payload
        finally:
            os.close(descriptor)

    gate_bytes = snapshot(gate_path, "safety gate", 64 * 1024)
    signature_bytes = snapshot(signature_path, "safety gate signature", 1024)
    public_key_bytes = snapshot(public_key, "safety gate public key", 64 * 1024)
    openssl = FIXED_OPENSSL
    if not openssl.is_file() or openssl.is_symlink():
        raise HilError("fixed /usr/bin/openssl verifier is unavailable")
    with tempfile.TemporaryDirectory(prefix="tidetracker-safety-snapshot-") as raw_temp:
        snapshot_root = pathlib.Path(raw_temp)
        os.chmod(snapshot_root, 0o700)
        private_gate = snapshot_root / "gate.json"
        private_signature = snapshot_root / "gate.sig"
        private_key = snapshot_root / "approver.pub.pem"
        for path, payload in (
            (private_gate, gate_bytes),
            (private_signature, signature_bytes),
            (private_key, public_key_bytes),
        ):
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                remaining = memoryview(payload)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise HilError("private safety snapshot write made no progress")
                    remaining = remaining[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        verification = subprocess.run(
            [
                str(openssl),
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                str(private_key),
                "-rawin",
                "-in",
                str(private_gate),
                "-sigfile",
                str(private_signature),
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )
        if verification.returncode != 0:
            raise HilError("safety gate Ed25519 signature is invalid")

    def reject_duplicates(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, child in items:
            if key in value:
                raise HilError(f"safety gate contains duplicate field: {key}")
            value[key] = child
        return value

    try:
        value = json.loads(gate_bytes.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HilError("safety gate is invalid JSON") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("approved") is not True
    ):
        raise HilError("safety gate is not an approved schema-1 record")
    hardware_id = value.get("hardware_id")
    if not isinstance(hardware_id, str) or not hardware_id.strip():
        raise HilError("safety gate hardware_id is missing")
    if hardware_id != expected_identity.get("hardware_id"):
        raise HilError("safety gate hardware_id does not match this controller config")
    if value.get("fixture_firmware_sha256") != expected_identity.get("fixture_firmware_sha256"):
        raise HilError("safety gate fixture firmware hash does not match controller config")
    if value.get("target_known_hosts_sha256") != expected_identity.get("target_known_hosts_sha256"):
        raise HilError("safety gate target host-key record does not match known_hosts")
    controller_machine_id_sha256 = value.get("controller_machine_id_sha256")
    if (
        controller_machine_id_sha256
        != expected_identity.get("controller_machine_id_sha256")
        or not isinstance(controller_machine_id_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", controller_machine_id_sha256) is None
    ):
        raise HilError("safety gate controller machine identity does not match this Pi 3")
    for field in ("target_image_sha256", "target_image_manifest_sha256"):
        if (
            value.get(field) != expected_identity.get(field)
            or not isinstance(value.get(field), str)
            or re.fullmatch(r"[0-9a-f]{64}", value[field]) is None
        ):
            raise HilError(f"safety gate {field} does not match the accepted image")
    image_build_id = value.get("target_image_build_id")
    if (
        image_build_id != expected_identity.get("target_image_build_id")
        or not isinstance(image_build_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", image_build_id) is None
    ):
        raise HilError("safety gate target_image_build_id does not match the accepted image")
    hardware_inventory = validate_hardware_inventory(
        value.get("hardware_inventory"), label="safety gate hardware_inventory"
    )
    if hardware_inventory != expected_identity.get("hardware_inventory"):
        raise HilError("safety gate hardware inventory does not match this controller config")
    tool_identities = value.get("tool_identities")
    if (
        not isinstance(tool_identities, dict)
        or set(tool_identities) != TOOL_IDENTITY_NAMES
        or tool_identities != expected_identity.get("tool_identities")
    ):
        raise HilError("safety gate tool identities do not match the preflighted commands")
    if value.get("approval_nonce") != approval_nonce:
        raise HilError("safety gate approval nonce does not match this execution")
    try:
        approved_at = datetime.fromisoformat(str(value["approved_at"]).replace("Z", "+00:00"))
        valid_until = datetime.fromisoformat(str(value["valid_until"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise HilError("safety gate approval validity timestamps are invalid") from exc
    now = datetime.now(UTC)
    if approved_at.tzinfo is None or valid_until.tzinfo is None:
        raise HilError("safety gate approval timestamps must include a timezone")
    if approved_at > now + timedelta(minutes=5) or not approved_at < valid_until:
        raise HilError("safety gate approval time range is invalid")
    if valid_until <= now or valid_until - approved_at > timedelta(days=7):
        raise HilError("safety gate is expired or valid for longer than seven days")
    measurements = value.get("measurements")
    if not isinstance(measurements, dict):
        raise HilError("safety gate measurements are missing")

    def voltage(name: str) -> float:
        reading = measurements.get(name)
        if not isinstance(reading, (int, float)) or isinstance(reading, bool):
            raise HilError(f"safety gate measurement {name} is invalid")
        return float(reading)

    zero_5v_off = voltage("zero_5v_off_usb_connected_v")
    zero_3v3_off = voltage("zero_3v3_off_all_sense_connected_v")
    zero_5v_on = voltage("zero_5v_on_under_load_v")
    if not 0 <= zero_5v_off < 0.1 or not 0 <= zero_3v3_off < 0.1:
        raise HilError("safety gate records unsafe USB/UART/GPIO back-power voltage")
    if not 4.75 <= zero_5v_on <= 5.25:
        raise HilError("safety gate records an out-of-range switched 5 V rail")
    for field in (
        "usb_vbus_isolated",
        "uart_receive_only",
        "fixture_defaults_power_off",
        "fixture_defaults_setup",
    ):
        if value.get(field) is not True:
            raise HilError(f"safety gate requires {field}=true")
    done_pulse = value.get("done_pulse")
    if not isinstance(done_pulse, dict):
        raise HilError("safety gate done_pulse calibration is missing")
    minimum_us = done_pulse.get("minimum_us")
    maximum_us = done_pulse.get("maximum_us")
    shortened_validated = done_pulse.get("shortened_timing_hardware_validated")
    if (
        not isinstance(minimum_us, int)
        or isinstance(minimum_us, bool)
        or not isinstance(maximum_us, int)
        or isinstance(maximum_us, bool)
        or not 10_000 <= minimum_us <= maximum_us <= 1_000_000
        or not isinstance(shortened_validated, bool)
    ):
        raise HilError("safety gate DONE pulse bounds are invalid")
    if (minimum_us < 400_000 or maximum_us < 500_000) and not shortened_validated:
        raise HilError("shortened DONE timing lacks signed hardware validation")
    approved_operations = value.get("approved_operations")
    approved_update_phases = value.get("approved_update_power_cut_phases")
    if (
        not isinstance(approved_operations, list)
        or not all(isinstance(item, str) for item in approved_operations)
        or len(set(approved_operations)) != len(approved_operations)
        or not set(approved_operations) <= APPROVED_OPERATIONS
    ):
        raise HilError("safety gate approved_operations is invalid")
    if (
        not isinstance(approved_update_phases, list)
        or not all(isinstance(item, str) for item in approved_update_phases)
        or len(set(approved_update_phases)) != len(approved_update_phases)
        or not set(approved_update_phases) <= set(UPDATE_PHASES)
    ):
        raise HilError("safety gate approved_update_power_cut_phases is invalid")
    if approved_update_phases and "update_power_cuts" not in approved_operations:
        raise HilError("safety gate update phases lack update_power_cuts authorization")
    return {
        "hardware_id": hardware_id,
        "fixture_firmware_sha256": expected_identity["fixture_firmware_sha256"],
        "target_known_hosts_sha256": expected_identity["target_known_hosts_sha256"],
        "controller_machine_id_sha256": controller_machine_id_sha256,
        "target_image_sha256": value["target_image_sha256"],
        "target_image_manifest_sha256": value["target_image_manifest_sha256"],
        "target_image_build_id": image_build_id,
        "hardware_inventory": hardware_inventory,
        "tool_identities": tool_identities,
        "approval_nonce": approval_nonce,
        "valid_until": valid_until.isoformat(),
        "gate_sha256": hashlib.sha256(gate_bytes).hexdigest(),
        "signature_sha256": hashlib.sha256(signature_bytes).hexdigest(),
        "approval_public_key_sha256": hashlib.sha256(public_key_bytes).hexdigest(),
        "signature": "verified",
        "approved_operations": approved_operations,
        "approved_update_power_cut_phases": approved_update_phases,
        "done_pulse": {
            "minimum_us": minimum_us,
            "maximum_us": maximum_us,
            "shortened_timing_hardware_validated": shortened_validated,
        },
        "measurements": {
            "zero_5v_off_usb_connected_v": zero_5v_off,
            "zero_3v3_off_all_sense_connected_v": zero_3v3_off,
            "zero_5v_on_under_load_v": zero_5v_on,
        },
    }


def require_safety_scope(
    gate: dict[str, Any],
    *,
    run_cycles: bool,
    bundle: pathlib.Path | None,
    wifi_matrix: bool,
    lifecycle_phases: tuple[str, ...],
    update_phases: tuple[str, ...],
    field_cycle: bool,
) -> None:
    required: set[str] = set()
    if run_cycles:
        required.add("cycles")
    if bundle is not None:
        required.add("release_stage")
    if wifi_matrix:
        required.add("wifi_matrix")
    if lifecycle_phases:
        required.add("lifecycle_power_cuts")
    if update_phases:
        required.add("update_power_cuts")
    if field_cycle:
        required.add("field_cycle")
    approved = set(gate.get("approved_operations", []))
    if not required <= approved:
        raise HilError(
            "signed safety gate does not authorize requested operations: "
            + ", ".join(sorted(required - approved))
        )
    approved_update = set(gate.get("approved_update_power_cut_phases", []))
    if not set(update_phases) <= approved_update:
        raise HilError("signed safety gate does not authorize every requested update-cut phase")


def validate_controller_artifact_destination(
    destination: pathlib.Path, config: dict[str, Any]
) -> dict[str, Any]:
    settings = config.get("controller_artifacts")
    if not isinstance(settings, dict):
        raise HilError("real HIL requires controller_artifacts configuration")
    configured_root = settings.get("root")
    configured_uuid = settings.get("data_uuid")
    if configured_root != str(DEFAULT_ARTIFACT_ROOT) or configured_uuid != CONTROLLER_DATA_UUID:
        raise HilError("controller artifact root/UUID must match the pinned TIDEDATA policy")
    if DEFAULT_ARTIFACT_ROOT.is_symlink():
        raise HilError("controller HIL artifact root must not be a symbolic link")
    try:
        root = DEFAULT_ARTIFACT_ROOT.resolve(strict=True)
        metadata = root.stat()
    except OSError as exc:
        raise HilError("controller HIL artifact root is unavailable") from exc
    if root != DEFAULT_ARTIFACT_ROOT or not stat.S_ISDIR(metadata.st_mode):
        raise HilError("controller HIL artifact root is not the pinned directory")
    if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o770:
        raise HilError("controller HIL artifact root must be root-owned mode 0770")
    try:
        import grp

        expected_gid = grp.getgrnam("tidetracker").gr_gid
    except (ImportError, KeyError) as exc:
        raise HilError("controller tidetracker group is unavailable") from exc
    if metadata.st_gid != expected_gid:
        raise HilError("controller HIL artifact root must be group-owned by tidetracker")
    if destination.parent != root or destination.name in {"", ".", ".."}:
        raise HilError("real HIL artifact directory must be a new direct child of pinned TIDEDATA")
    if destination.exists() or destination.is_symlink():
        raise HilError(f"artifact directory already exists: {destination}")
    if not FIXED_FINDMNT.is_file() or FIXED_FINDMNT.is_symlink():
        raise HilError("fixed /usr/bin/findmnt is unavailable")
    result = subprocess.run(
        [
            str(FIXED_FINDMNT),
            "-n",
            "-o",
            "UUID,FSTYPE,OPTIONS",
            "--target",
            str(root),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    fields = result.stdout.strip().split(maxsplit=2)
    if (
        result.returncode != 0
        or len(fields) != 3
        or fields[0] != CONTROLLER_DATA_UUID
        or fields[1] != "ext4"
        or "rw" not in fields[2].split(",")
    ):
        raise HilError("controller HIL results are not on writable ext4 TIDEDATA")
    return {
        "root": str(root),
        "data_uuid": fields[0],
        "filesystem": fields[1],
        "read_write": True,
        "owner": "root",
        "group": "tidetracker",
        "mode": "0770",
        "verified": True,
    }


def planned_actions(
    cycles: int,
    bundle: pathlib.Path | None,
    *,
    wifi_matrix: bool = False,
    power_cut_phases: tuple[str, ...] = (),
    update_power_cut_phases: tuple[str, ...] = (),
    field_cycle: bool = False,
    soak_hours: int | None = None,
    accelerated_cycle_campaign: bool = False,
) -> list[str]:
    actions = [
        "validate pinned USB target, fixture, receive-only UART, camera, and host-key inputs",
    ]
    if bundle is not None:
        actions.append("boot SETUP, upload and verify the bundle, then remove switched power")
    if wifi_matrix:
        actions.append("run the six-case Wi-Fi fault matrix while proving USB SSH stays reachable")
    if power_cut_phases:
        actions.append(
            "cut power and prove cold recovery at phases: " + ", ".join(power_cut_phases)
        )
    if update_power_cut_phases:
        actions.append(
            "cut power and prove updater recovery at signed boundaries: "
            + ", ".join(update_power_cut_phases)
        )
    if soak_hours is not None:
        actions.append(f"run the optional bounded {soak_hours}-hour soak campaign")
    if field_cycle:
        actions.append(
            "run one field-card cycle with USB physically disconnected and probe-verified"
        )
    if not field_cycle:
        label = "accelerated reliability" if accelerated_cycle_campaign else "cold RUN"
        actions.append(f"repeat {cycles} {label} cycle(s), waiting for the fixture DONE edge")
    actions.extend(
        [
            "prove powered-off rails are safe after each switched-power removal",
            "capture and automatically compare the retained e-ink image",
            "write a machine-readable session result without modifying repository source",
        ]
    )
    return actions


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=pathlib.Path,
        default=pathlib.Path(__file__).with_name("hil_config.example.json"),
    )
    parser.add_argument("--cycles", type=int)
    parser.add_argument(
        "--accelerated-cycles",
        action="store_true",
        help="run the fixed 12-cycle accelerated reliability campaign",
    )
    parser.add_argument("--bundle", type=pathlib.Path)
    parser.add_argument("--expected-release-id")
    parser.add_argument(
        "--expected-execution-source",
        choices=("built-in", "verified-release"),
    )
    parser.add_argument("--artifact-dir", type=pathlib.Path)
    parser.add_argument("--wifi-matrix", action="store_true")
    parser.add_argument(
        "--power-cut-phase",
        action="append",
        choices=[*LIFECYCLE_PHASES, "all"],
        default=[],
    )
    parser.add_argument(
        "--update-power-cut-phase",
        action="append",
        choices=[*UPDATE_PHASES, "all"],
        default=[],
    )
    parser.add_argument("--soak-hours", type=int, choices=(24, 72))
    parser.add_argument("--field-cycle", action="store_true")
    parser.add_argument("--safety-gate", type=pathlib.Path)
    parser.add_argument("--approval-nonce")
    parser.add_argument("--execute-confirmation")
    parser.add_argument(
        "--execute", action="store_true", help="allow real hardware/network actions"
    )
    parser.add_argument(
        "--simulate", action="store_true", help="execute only in-memory simulated hardware"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(sys.argv[1:] if argv is None else argv)
    if arguments.execute and arguments.simulate:
        raise HilError("--execute and --simulate are mutually exclusive")
    if arguments.execute and arguments.execute_confirmation != "EXECUTE-TIDETRACKER-HIL":
        raise HilError("real HIL requires --execute-confirmation EXECUTE-TIDETRACKER-HIL")
    if arguments.accelerated_cycles and (
        arguments.cycles is not None or arguments.soak_hours is not None
    ):
        raise HilError("--accelerated-cycles cannot combine with --cycles or --soak-hours")
    cycles = arguments.cycles if arguments.cycles is not None else 1
    if arguments.accelerated_cycles:
        cycles = 12
    if cycles < 1 or cycles > 1000:
        raise HilError("--cycles must be between 1 and 1000")
    config_path = (
        validate_private_config(arguments.config)
        if arguments.execute
        else arguments.config.resolve()
    )
    config = load_config(config_path)
    requested_phases = tuple(arguments.power_cut_phase)
    power_cut_phases = (
        LIFECYCLE_PHASES if "all" in requested_phases else tuple(dict.fromkeys(requested_phases))
    )
    requested_update_phases = tuple(arguments.update_power_cut_phase)
    update_power_cut_phases = (
        UPDATE_PHASES
        if "all" in requested_update_phases
        else tuple(dict.fromkeys(requested_update_phases))
    )
    cycle_interval_seconds = 0.0
    if arguments.soak_hours is not None:
        if arguments.cycles is not None:
            raise HilError("--cycles and --soak-hours are mutually exclusive")
        cycle_interval_seconds = float(config.get("timing", {}).get("soak_interval_seconds", 900))
        if cycle_interval_seconds < 60 or cycle_interval_seconds > 3600:
            raise HilError("timing.soak_interval_seconds must be between 60 and 3600")
        cycles = int(arguments.soak_hours * 3600 // cycle_interval_seconds) + 1
    if arguments.field_cycle and (
        arguments.bundle is not None
        or arguments.wifi_matrix
        or power_cut_phases
        or update_power_cut_phases
        or arguments.soak_hours is not None
        or arguments.accelerated_cycles
        or cycles != 1
    ):
        raise HilError(
            "--field-cycle requires one isolated cycle and cannot combine with bundle, matrix, cuts, or soak"
        )
    bundle = (
        pathlib.Path(os.path.abspath(arguments.bundle.expanduser()))
        if arguments.bundle
        else None
    )
    if bundle is not None and (not bundle.is_file() or bundle.is_symlink()):
        raise HilError(f"bundle must be a regular, non-symlink file: {bundle}")
    if update_power_cut_phases and bundle is None:
        raise HilError("--update-power-cut-phase requires --bundle")
    expected_release_id = arguments.expected_release_id
    expected_execution_source = arguments.expected_execution_source
    if arguments.execute and (
        not isinstance(expected_release_id, str)
        or not expected_release_id
        or expected_execution_source is None
    ):
        raise HilError("real HIL requires --expected-release-id and --expected-execution-source")
    if expected_release_id is None:
        expected_release_id = "built-in"
    if expected_execution_source is None:
        expected_execution_source = "built-in"
    if (expected_release_id == "built-in") != (expected_execution_source == "built-in"):
        raise HilError("built-in release ID and execution source must be selected together")

    if not arguments.execute and not arguments.simulate:
        print(
            "TideTracker HIL dry run; no files, network, serial ports, camera, or power controls will be touched."
        )
        for index, action in enumerate(
            planned_actions(
                cycles,
                bundle,
                wifi_matrix=arguments.wifi_matrix,
                power_cut_phases=power_cut_phases,
                update_power_cut_phases=update_power_cut_phases,
                field_cycle=arguments.field_cycle,
                soak_hours=arguments.soak_hours,
                accelerated_cycle_campaign=arguments.accelerated_cycles,
            ),
            start=1,
        ):
            print(f"  {index}. {action}")
        print(
            "Use --simulate to exercise the loop safely. Real execution additionally requires "
            "a signed nonce-bound safety gate and --execute-confirmation "
            "EXECUTE-TIDETRACKER-HIL."
        )
        return 0

    simulated = bool(arguments.simulate)

    safety_gate: dict[str, Any] | None = None
    if arguments.execute:
        if arguments.safety_gate is None:
            raise HilError("real HIL requires --safety-gate")
        if (
            not isinstance(arguments.approval_nonce, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", arguments.approval_nonce) is None
        ):
            raise HilError("real HIL requires a 16–128 character --approval-nonce")

    if arguments.artifact_dir is None and arguments.execute:
        artifact_directory = DEFAULT_ARTIFACT_ROOT / f"tidetracker-hil-{int(time.time())}"
    elif arguments.artifact_dir is None:
        artifact_directory = (
            pathlib.Path(tempfile.gettempdir()) / f"tidetracker-hil-{int(time.time())}"
        )
    else:
        artifact_directory = arguments.artifact_dir.resolve()
    if artifact_directory.exists():
        raise HilError(f"artifact directory already exists: {artifact_directory}")

    execution_lock: HilExecutionLock | None = None
    controller_artifact_policy: dict[str, Any] | None = None
    controller_machine_id_sha256 = "0" * 64
    target_identity: dict[str, Any] | None = None
    hardware_inventory: dict[str, dict[str, str]] | None = None
    tool_identities: dict[str, dict[str, Any]] | None = None
    authorization_guard = AuthorizationGuard()
    if simulated:
        fixture: Fixture = SimulationFixture()
        target: Target = SimulationTarget(expected_release_id, expected_execution_source)
        scenario_controller: ScenarioController = SimulationScenarioController()
        wifi_observer: WifiActivityObserver = SimulationWifiActivityObserver()
        portal_client = PortalClient(config.get("portal_tests", {}), simulated=True)
        camera_config = config.get("camera", {})
        camera = Camera(
            camera_config.get("command"),
            simulated=True,
            validation_command=camera_config.get("validation_command"),
            capture_support_files=camera_config.get("capture_support_files"),
            validation_support_files=camera_config.get("validation_support_files"),
        )
        field_usb_guard = FieldUsbGuard(
            config.get("field_cycle", {}).get("usb_disconnected_probe"),
            simulated=True,
            support_files=config.get("field_cycle", {}).get("usb_probe_support_files"),
        )
    else:
        controller_artifact_policy = validate_controller_artifact_destination(
            artifact_directory, config
        )
        for executable in ("sftp", "ssh"):
            if shutil.which(executable) is None:
                raise HilError(f"required HIL executable is unavailable: {executable}")
        fixture_config = config.get("fixture")
        ssh_config = config.get("target")
        uart_config = config.get("uart")
        if not all(isinstance(item, dict) for item in (fixture_config, ssh_config, uart_config)):
            raise HilError("fixture, target, and UART configuration objects are required")
        assert isinstance(fixture_config, dict)
        assert isinstance(ssh_config, dict)
        assert isinstance(uart_config, dict)
        identity = pathlib.Path(
            os.path.abspath(pathlib.Path(ssh_config["identity_file"]).expanduser())
        )
        known_hosts = pathlib.Path(
            os.path.abspath(pathlib.Path(ssh_config["known_hosts_file"]).expanduser())
        )
        if (
            not identity.is_file()
            or identity.is_symlink()
            or not known_hosts.is_file()
            or known_hosts.is_symlink()
        ):
            raise HilError("identity_file and known_hosts must be regular, non-symlink files")
        camera_config = config.get("camera", {})
        camera = Camera(
            camera_config.get("command"),
            simulated=False,
            validation_command=camera_config.get("validation_command"),
            capture_support_files=camera_config.get("capture_support_files"),
            validation_support_files=camera_config.get("validation_support_files"),
        )
        field_usb_guard = FieldUsbGuard(
            config.get("field_cycle", {}).get("usb_disconnected_probe"),
            simulated=False,
            support_files=config.get("field_cycle", {}).get("usb_probe_support_files"),
        )
        field_usb_guard.validate_configuration()
        probe_identity = field_usb_guard.tool_identity
        if probe_identity is None:
            raise HilError("USB-disconnected probe identity is unavailable")
        tool_identities = {
            **camera.tool_identities,
            "usb_disconnected_probe": probe_identity,
        }
        safety_identity = config.get("safety_identity")
        if not isinstance(safety_identity, dict):
            raise HilError("safety_identity configuration is required")
        hardware_id = safety_identity.get("hardware_id")
        fixture_firmware_hash = safety_identity.get("fixture_firmware_sha256")
        configured_host_hash = safety_identity.get("target_known_hosts_sha256")
        configured_controller_machine_id_hash = safety_identity.get(
            "controller_machine_id_sha256"
        )
        image_sha256 = safety_identity.get("target_image_sha256")
        image_manifest_sha256 = safety_identity.get("target_image_manifest_sha256")
        image_build_id = safety_identity.get("target_image_build_id")
        hardware_inventory = validate_hardware_inventory(
            safety_identity.get("hardware_inventory"),
            label="safety_identity.hardware_inventory",
        )
        actual_host_hash = sha256_file(known_hosts)
        if not isinstance(hardware_id, str) or not hardware_id:
            raise HilError("safety_identity.hardware_id is invalid")
        if (
            not isinstance(fixture_firmware_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", fixture_firmware_hash) is None
        ):
            raise HilError("safety_identity.fixture_firmware_sha256 is invalid")
        if configured_host_hash != actual_host_hash:
            raise HilError("safety_identity target known_hosts hash does not match the file")
        controller_machine_id_sha256 = snapshot_controller_machine_id()
        if configured_controller_machine_id_hash != controller_machine_id_sha256:
            raise HilError("safety_identity controller machine ID hash does not match this Pi 3")
        if any(
            not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in (image_sha256, image_manifest_sha256)
        ):
            raise HilError("safety_identity target image hashes are invalid")
        if (
            not isinstance(image_build_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", image_build_id) is None
        ):
            raise HilError("safety_identity.target_image_build_id is invalid")
        safety_gate = verify_safety_gate(
            pathlib.Path(os.path.abspath(arguments.safety_gate.expanduser())),
            DEFAULT_SAFETY_PUBLIC_KEY,
            expected_identity={
                "hardware_id": hardware_id,
                "fixture_firmware_sha256": fixture_firmware_hash,
                "target_known_hosts_sha256": actual_host_hash,
                "controller_machine_id_sha256": controller_machine_id_sha256,
                "target_image_sha256": image_sha256,
                "target_image_manifest_sha256": image_manifest_sha256,
                "target_image_build_id": image_build_id,
                "hardware_inventory": hardware_inventory,
                "tool_identities": tool_identities,
            },
            approval_nonce=arguments.approval_nonce,
        )
        authorization_guard = AuthorizationGuard.from_safety_gate(safety_gate)
        authorization_guard.require_bounded("post-verification HIL setup")
        camera.bind_authorization(authorization_guard)
        field_usb_guard.bind_authorization(authorization_guard)
        require_safety_scope(
            safety_gate,
            run_cycles=not arguments.field_cycle,
            bundle=bundle,
            wifi_matrix=arguments.wifi_matrix,
            lifecycle_phases=power_cut_phases,
            update_phases=update_power_cut_phases,
            field_cycle=arguments.field_cycle,
        )
        portal_client = PortalClient(
            config.get("portal_tests", {}),
            simulated=not arguments.wifi_matrix,
            authorization_guard=authorization_guard,
        )
        target_identity = {
            "release_id": expected_release_id,
            "execution_source": expected_execution_source,
            "image_build_id": image_build_id,
            "image_sha256": image_sha256,
            "image_manifest_sha256": image_manifest_sha256,
            "attestation": "verified_signed_safety_gate",
        }
        fixture_port = fixture_config.get("port")
        uart_port = uart_config.get("port")
        if not isinstance(fixture_port, str) or not fixture_port:
            raise HilError("fixture.port is required")
        if not isinstance(uart_port, str) or not uart_port:
            raise HilError("uart.port is required")
        execution_lock = HilExecutionLock(
            DEFAULT_EXECUTION_LOCK,
            {
                "artifact_directory": str(artifact_directory),
                "gate_sha256": safety_gate["gate_sha256"],
                "pid": os.getpid(),
                "started": utc_now(),
            },
        )
        execution_lock.acquire()
        try:
            scenario_controller = (
                CommandScenarioController(
                    config.get("portal_tests", {}),
                    authorization_guard=authorization_guard,
                )
                if arguments.wifi_matrix
                else SimulationScenarioController()
            )
            wifi_observer = CommandWifiActivityObserver(
                config.get("wifi_observation", {}),
                authorization_guard=authorization_guard,
            )
            # Fixture serial opens only after the signed gate, scope, and controller lock.
            fixture = SerialFixture(
                fixture_port,
                int(fixture_config.get("baudrate", 115200)),
                authorization_guard=authorization_guard,
            )
            target = SshTarget(
                SshSettings(
                    host=str(ssh_config.get("host", "10.77.0.2")),
                    user=str(ssh_config.get("user", "tidetracker")),
                    identity_file=identity,
                    known_hosts_file=known_hosts,
                ),
                authorization_guard=authorization_guard,
            )
        except Exception:
            execution_lock.release()
            raise
    try:
        orchestrator = Orchestrator(
            fixture=fixture,
            target=target,
            camera=camera,
            artifact_directory=artifact_directory,
            config=config,
            simulated=simulated,
            scenario_controller=scenario_controller,
            wifi_observer=wifi_observer,
            portal_client=portal_client,
            field_usb_guard=field_usb_guard,
            safety_gate=safety_gate,
            expected_release_id=expected_release_id,
            expected_execution_source=expected_execution_source,
            target_identity=target_identity,
            controller_artifact_policy=controller_artifact_policy,
            controller_machine_id_sha256=controller_machine_id_sha256,
            hardware_inventory=hardware_inventory,
            tool_identities=tool_identities,
            accelerated_cycle_campaign=arguments.accelerated_cycles,
            authorization_guard=authorization_guard,
        )
        output = orchestrator.run(
            cycles,
            bundle,
            wifi_matrix=arguments.wifi_matrix,
            power_cut_phases=power_cut_phases,
            update_power_cut_phases=update_power_cut_phases,
            field_cycle=arguments.field_cycle,
            cycle_interval_seconds=cycle_interval_seconds,
        )
    finally:
        if execution_lock is not None:
            execution_lock.release()
    print(output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HilError as error:
        print(f"HIL ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from None
