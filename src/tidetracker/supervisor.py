"""Application-release trial execution with same-boot fallback."""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .updater import ReleaseManifest, ReleaseUpdater, UpdateError

PROCESS_TIMEOUT_RETURN_CODE = 124
HARDWARE_OWNERSHIP_UNPROVEN_RETURN_CODE = 125
EXECUTION_FAILURE_RETURN_CODE = 126
_TERMINATE_GRACE_SECONDS = 1.0
_KILL_GRACE_SECONDS = 2.0
_GROUP_POLL_SECONDS = 0.02


class HardwareOwnershipError(RuntimeError):
    """A candidate process tree could not be proven stopped before fallback."""


class ProcessRunner(Protocol):
    def __call__(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]: ...


def _process_runner(
    args: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    deadline = time.monotonic() + timeout
    try:
        if os.name == "posix":
            process = subprocess.Popen(
                list(args),
                cwd=cwd,
                env=dict(env),
                shell=False,
                text=True,
                start_new_session=True,
            )
        elif os.name == "nt":
            process = subprocess.Popen(
                list(args),
                cwd=cwd,
                env=dict(env),
                shell=False,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        else:
            process = subprocess.Popen(
                list(args),
                cwd=cwd,
                env=dict(env),
                shell=False,
                text=True,
            )
    except OSError:
        return subprocess.CompletedProcess(list(args), EXECUTION_FAILURE_RETURN_CODE)
    timed_out = False
    try:
        remaining = max(0.0, deadline - time.monotonic())
        if remaining == 0:
            raise subprocess.TimeoutExpired(args, timeout)
        returncode = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        timed_out = True
        returncode = PROCESS_TIMEOUT_RETURN_CODE
    except OSError as exc:
        if os.name == "posix" and _terminate_posix_process_group(process):
            return subprocess.CompletedProcess(list(args), EXECUTION_FAILURE_RETURN_CODE)
        if os.name != "posix":
            _terminate_direct_process_bounded(process)
        raise HardwareOwnershipError(
            "candidate wait failed without process-tree release proof"
        ) from exc

    if os.name == "posix":
        residual_group = _posix_process_group_exists(process.pid)
        if timed_out or residual_group:
            if not _terminate_posix_process_group(process):
                raise HardwareOwnershipError(
                    "candidate process group could not be proven released after exit or timeout"
                )
            if residual_group and not timed_out:
                returncode = EXECUTION_FAILURE_RETURN_CODE
    elif timed_out:
        _terminate_direct_process_bounded(process)
        raise HardwareOwnershipError(
            "candidate timed out on a platform without process-tree release proof"
        )
    return subprocess.CompletedProcess(list(args), returncode)


def run_isolated_command(
    args: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
) -> int:
    """Run a command in the same bounded process-group contract as a release."""

    return int(_process_runner(args, cwd=cwd, env=env, timeout=timeout).returncode)


def _process_wait_bounded(process: subprocess.Popen[str], deadline: float) -> bool:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return process.poll() is not None
    try:
        process.wait(timeout=remaining)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return True


def _posix_process_group_exists(process_group: int) -> bool:
    kill_process_group = getattr(os, "killpg", None)
    if not callable(kill_process_group):
        return True
    try:
        kill_process_group(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _wait_for_posix_process_group_exit(process_group: int, deadline: float) -> bool:
    while time.monotonic() < deadline:
        if not _posix_process_group_exists(process_group):
            return True
        time.sleep(min(_GROUP_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
    return not _posix_process_group_exists(process_group)


def _signal_posix_process_group(process_group: int, requested_signal: int) -> bool:
    kill_process_group = getattr(os, "killpg", None)
    if not callable(kill_process_group):
        return False
    try:
        kill_process_group(process_group, requested_signal)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return True


def _terminate_posix_process_group(process: subprocess.Popen[str]) -> bool:
    process_group = process.pid
    terminate_deadline = time.monotonic() + _TERMINATE_GRACE_SECONDS
    if not _signal_posix_process_group(process_group, signal.SIGTERM):
        return False
    _process_wait_bounded(process, terminate_deadline)
    if _wait_for_posix_process_group_exit(process_group, terminate_deadline):
        return process.poll() is not None

    kill_deadline = time.monotonic() + _KILL_GRACE_SECONDS
    if not _signal_posix_process_group(
        process_group, int(getattr(signal, "SIGKILL", signal.SIGTERM))
    ):
        return False
    process_reaped = _process_wait_bounded(process, kill_deadline)
    group_gone = _wait_for_posix_process_group_exit(process_group, kill_deadline)
    return process_reaped and group_gone


def _terminate_direct_process_bounded(process: subprocess.Popen[str]) -> None:
    terminate_deadline = time.monotonic() + _TERMINATE_GRACE_SECONDS
    with suppress(OSError):
        process.terminate()
    if _process_wait_bounded(process, terminate_deadline):
        return
    kill_deadline = time.monotonic() + _KILL_GRACE_SECONDS
    with suppress(OSError):
        process.kill()
    _process_wait_bounded(process, kill_deadline)


class ReleaseExecutor(Protocol):
    def execute(self, directory: Path, manifest: ReleaseManifest) -> int: ...


class SubprocessReleaseExecutor:
    """Run a verified release while deferring physical DONE to the parent."""

    def __init__(
        self,
        *,
        runner: ProcessRunner = _process_runner,
        timeout_seconds: float = 1_020.0,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("release timeout must be positive")
        self._runner = runner
        self.timeout_seconds = timeout_seconds
        self.environment = dict(environment or os.environ)

    def execute(self, directory: Path, manifest: ReleaseManifest) -> int:
        if manifest.entrypoint is None:
            return 126
        entrypoint = directory / manifest.entrypoint
        try:
            mode = entrypoint.lstat().st_mode
        except OSError as exc:
            raise HardwareOwnershipError(
                "candidate runner failed without process-tree release proof"
            ) from exc
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            return 126
        command = [str(entrypoint), "cycle", "--builtin", "--defer-done"]
        if entrypoint.suffix.lower() == ".py":
            command.insert(0, sys.executable)
        environment = dict(self.environment)
        release_imports = [str(directory / "src"), str(directory)]
        inherited_pythonpath = environment.get("PYTHONPATH")
        if inherited_pythonpath:
            release_imports.append(inherited_pythonpath)
        environment.update(
            {
                "PYTHONPATH": os.pathsep.join(release_imports),
                "TIDETRACKER_RELEASE_ID": manifest.release_id,
                "TIDETRACKER_EXECUTION_SOURCE": "verified-release",
                "TIDETRACKER_RELEASE_DIR": str(directory),
                "TIDETRACKER_SUPERVISED": "1",
            }
        )
        if (directory / "Ubuntu-Regular.ttf").is_file():
            environment["TIDETRACKER_ASSET_DIR"] = str(directory)
        if (directory / "stations.csv").is_file():
            environment["TIDETRACKER_STATIONS"] = str(directory / "stations.csv")
        try:
            completed = self._runner(
                command,
                cwd=directory,
                env=environment,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise HardwareOwnershipError(
                "candidate runner timed out without process-tree release proof"
            ) from exc
        except OSError:
            return 126
        return int(completed.returncode)


@dataclass(frozen=True, slots=True)
class AttemptEvidence:
    valid: bool
    run_id: str | None = None
    result_success: bool | None = None
    release_id: str | None = None
    execution_source: str | None = None
    injected_noaa_fault: str | None = None
    used_cached_data: bool | None = None


@dataclass(frozen=True, slots=True)
class ReleaseAttempt:
    source: str
    returncode: int
    evidence: AttemptEvidence | None = None


@dataclass(frozen=True, slots=True)
class SupervisorResult:
    returncode: int
    attempts: tuple[ReleaseAttempt, ...]
    trial_confirmed: bool = False
    trial_rolled_back: bool = False
    hardware_ownership_released: bool = True


class ReleaseSupervisor:
    """Trial the active bundle, confirm it, or restore prior/base in one boot."""

    def __init__(
        self,
        updater: ReleaseUpdater,
        executor: ReleaseExecutor,
        *,
        evidence_validator: Callable[[ReleaseAttempt], AttemptEvidence | bool] | None = None,
    ) -> None:
        self.updater = updater
        self.executor = executor
        self.evidence_validator = evidence_validator

    def _validate(self, attempt: ReleaseAttempt) -> ReleaseAttempt:
        if self.evidence_validator is None:
            return attempt
        try:
            raw_evidence = self.evidence_validator(attempt)
            evidence = (
                raw_evidence
                if isinstance(raw_evidence, AttemptEvidence)
                else AttemptEvidence(bool(raw_evidence))
            )
        except Exception:
            evidence = AttemptEvidence(False)
        return ReleaseAttempt(attempt.source, attempt.returncode, evidence)

    @staticmethod
    def _accepted(attempt: ReleaseAttempt) -> bool:
        return attempt.returncode == 0 and (
            attempt.evidence is None or attempt.evidence.valid
        )

    def _execute_active(self) -> ReleaseAttempt:
        directory, manifest = self.updater.active_release()
        return ReleaseAttempt(manifest.release_id, self.executor.execute(directory, manifest))

    def run(self, built_in: Callable[[], int]) -> SupervisorResult:
        status = self.updater.status()
        current = status.get("current")
        trial = status.get("trial")
        attempts: list[ReleaseAttempt] = []
        if not isinstance(current, str):
            try:
                attempt = self._validate(ReleaseAttempt("built-in", built_in()))
            except HardwareOwnershipError:
                attempt = ReleaseAttempt(
                    "built-in", HARDWARE_OWNERSHIP_UNPROVEN_RETURN_CODE
                )
                return SupervisorResult(
                    HARDWARE_OWNERSHIP_UNPROVEN_RETURN_CODE,
                    (attempt,),
                    hardware_ownership_released=False,
                )
            return SupervisorResult(attempt.returncode, (attempt,))

        try:
            candidate = self._execute_active()
        except HardwareOwnershipError:
            candidate = ReleaseAttempt(current, HARDWARE_OWNERSHIP_UNPROVEN_RETURN_CODE)
            attempts.append(candidate)
            return SupervisorResult(
                HARDWARE_OWNERSHIP_UNPROVEN_RETURN_CODE,
                tuple(attempts),
                hardware_ownership_released=False,
            )
        except UpdateError:
            candidate = ReleaseAttempt(current, 126)
        candidate = self._validate(candidate)
        attempts.append(candidate)
        if self._accepted(candidate):
            if trial == current:
                self.updater.confirm(current)
                return SupervisorResult(0, tuple(attempts), trial_confirmed=True)
            return SupervisorResult(0, tuple(attempts))

        if trial == current:
            # A durable state replacement may have succeeded before later
            # release-retention cleanup failed.  The only trustworthy answer
            # is the state read back from disk, never the exception alone.
            with suppress(UpdateError):
                self.updater.rollback()
            try:
                rollback_status = self.updater.status()
            except UpdateError:
                return SupervisorResult(126, tuple(attempts))
            restored_current = rollback_status.get("current")
            rollback_proven = (
                rollback_status.get("trial") is None
                and restored_current != current
                and (restored_current is None or isinstance(restored_current, str))
            )
            if not rollback_proven:
                # Do not run another hardware-owning implementation while the
                # failed trial may still be selected for the next boot, and do
                # not claim a rollback that cannot be proven from durable state.
                return SupervisorResult(126, tuple(attempts))
            if isinstance(restored_current, str):
                try:
                    previous = self._execute_active()
                except HardwareOwnershipError:
                    previous = ReleaseAttempt(
                        restored_current, HARDWARE_OWNERSHIP_UNPROVEN_RETURN_CODE
                    )
                    attempts.append(previous)
                    return SupervisorResult(
                        HARDWARE_OWNERSHIP_UNPROVEN_RETURN_CODE,
                        tuple(attempts),
                        trial_rolled_back=True,
                        hardware_ownership_released=False,
                    )
                except UpdateError:
                    previous = ReleaseAttempt(restored_current, 126)
                previous = self._validate(previous)
                attempts.append(previous)
                if self._accepted(previous):
                    return SupervisorResult(
                        0,
                        tuple(attempts),
                        trial_rolled_back=True,
                    )
            try:
                fallback = self._validate(ReleaseAttempt("built-in", built_in()))
            except HardwareOwnershipError:
                fallback = ReleaseAttempt(
                    "built-in", HARDWARE_OWNERSHIP_UNPROVEN_RETURN_CODE
                )
                attempts.append(fallback)
                return SupervisorResult(
                    HARDWARE_OWNERSHIP_UNPROVEN_RETURN_CODE,
                    tuple(attempts),
                    trial_rolled_back=True,
                    hardware_ownership_released=False,
                )
            attempts.append(fallback)
            return SupervisorResult(
                fallback.returncode,
                tuple(attempts),
                trial_rolled_back=True,
            )

        # A confirmed release is retained for forensic inspection, but a bad
        # executable must not prevent the immutable image runtime from cycling.
        try:
            fallback = self._validate(ReleaseAttempt("built-in", built_in()))
        except HardwareOwnershipError:
            fallback = ReleaseAttempt(
                "built-in", HARDWARE_OWNERSHIP_UNPROVEN_RETURN_CODE
            )
            attempts.append(fallback)
            return SupervisorResult(
                HARDWARE_OWNERSHIP_UNPROVEN_RETURN_CODE,
                tuple(attempts),
                hardware_ownership_released=False,
            )
        attempts.append(fallback)
        return SupervisorResult(fallback.returncode, tuple(attempts))


__all__ = [
    "AttemptEvidence",
    "HARDWARE_OWNERSHIP_UNPROVEN_RETURN_CODE",
    "HardwareOwnershipError",
    "ReleaseAttempt",
    "ReleaseExecutor",
    "ReleaseSupervisor",
    "SubprocessReleaseExecutor",
    "SupervisorResult",
    "run_isolated_command",
]
