"""Guarded target-side update interruption and recovery evidence.

This module is reachable only through the root-owned lab helper.  It deliberately
stops at a durable phase boundary and waits for fixture power removal; ordinary
completion is an error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .release import MAX_ARCHIVE_BYTES, extract_bundle
from .storage import atomic_write_json, bounded_system_sync
from .telemetry import SerialLineWriter, UartPhaseTelemetry
from .updater import (
    OpenSSLEd25519Verifier,
    ReleaseStateError,
    ReleaseUpdater,
    validate_release_id,
)

PHASES = ("begin", "stage", "verify", "activate")
OBSERVED_STATES = {
    "begin": "uploading",
    "stage": "staged",
    "verify": "verified",
    "activate": "trial",
}
RECOVERY_STAGES = (
    "cleanup_pending",
    "updater_clean",
    "upload_clean",
    "history_written",
)
SHA256_CHARS = frozenset("0123456789abcdef")


class HilUpdateError(RuntimeError):
    """The destructive update-interruption evidence is invalid or unsafe."""


def _system_sync() -> None:
    bounded_system_sync()


def _sha256_regular(path: Path) -> str:
    if path.is_symlink():
        raise HilUpdateError("update bundle must not be a symbolic link")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_ARCHIVE_BYTES:
            raise HilUpdateError("update bundle is not a bounded regular file")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    current = path.stat(follow_symlinks=False)
    fields = (
        ("st_size", "st_mtime_ns")
        if os.name == "nt"
        else ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    )
    if any(getattr(before, key) != getattr(after, key) for key in fields) or any(
        getattr(before, key) != getattr(current, key) for key in fields
    ):
        raise HilUpdateError("update bundle changed while it was hashed")
    return digest.hexdigest()


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in pairs:
        if key in value:
            raise HilUpdateError(f"duplicate update evidence field: {key}")
        value[key] = child
    return value


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 64 * 1024:
        raise HilUpdateError("update interruption context is invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicates)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HilUpdateError("update interruption context is unreadable") from exc
    if not isinstance(value, dict):
        raise HilUpdateError("update interruption context must be an object")
    return value


def _validate_digest(value: str, *, label: str) -> str:
    if len(value) != 64 or any(char not in SHA256_CHARS for char in value):
        raise HilUpdateError(f"{label} must be a lowercase SHA-256 digest")
    return value


class UpdateCutController:
    def __init__(
        self,
        root: Path,
        public_key: Path,
        telemetry: UartPhaseTelemetry,
        *,
        sync: Callable[[], None] | None = None,
        updater: ReleaseUpdater | None = None,
        recovery_checkpoint: Callable[[str], None] | None = None,
    ) -> None:
        self.root = Path(root)
        self.public_key = Path(public_key)
        self.telemetry = telemetry
        self.sync: Callable[[], None] = sync if sync is not None else _system_sync
        self.hil_state = self.root / "state" / "hil"
        self.context = self.hil_state / "update-cut.json"
        self.updater = updater or ReleaseUpdater(
            self.root, OpenSSLEd25519Verifier(self.public_key, binary="/usr/bin/openssl")
        )
        self.recovery_checkpoint = recovery_checkpoint or (lambda _stage: None)

    def _history_path(self, phase: str, release_id: str, bundle_sha256: str) -> Path:
        return self.hil_state / f"update-cut-recovered-{phase}-{release_id}-{bundle_sha256}.json"

    @staticmethod
    def _candidate_references(status: Mapping[str, object], release_id: str) -> bool:
        verified = status.get("verified")
        return any(status.get(key) == release_id for key in ("current", "previous", "trial", "in_progress")) or (
            isinstance(verified, list) and release_id in verified
        )

    def prepare(
        self,
        bundle: Path,
        phase: str,
        expected_release_id: str,
        expected_bundle_sha256: str,
        *,
        wait_for_power_cut: Callable[[], None],
    ) -> None:
        if phase not in PHASES:
            raise HilUpdateError("invalid update interruption phase")
        release_id = validate_release_id(expected_release_id)
        bundle_sha256 = _validate_digest(expected_bundle_sha256, label="bundle SHA-256")
        uploads = (self.root / "uploads").resolve()
        if (
            bundle.parent.resolve() != uploads
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", bundle.name) is None
        ):
            raise HilUpdateError("update bundle must be a safe file in the uploads directory")
        if _sha256_regular(bundle) != bundle_sha256:
            raise HilUpdateError("uploaded update bundle SHA-256 does not match approval")
        if self.context.exists():
            raise HilUpdateError("an update interruption still requires recovery")
        status = self.updater.status()
        if self._candidate_references(status, release_id):
            raise HilUpdateError("candidate release is already present in update state")

        with tempfile.TemporaryDirectory(prefix="tidetracker-hil-update-") as raw_temp:
            extracted = Path(raw_temp) / "release"
            manifest = extract_bundle(bundle, extracted)
            if manifest.release_id != release_id:
                raise HilUpdateError("bundle release ID does not match approval")
            self.updater.validate_directory(release_id, extracted)
            self.updater.begin(release_id)
            if PHASES.index(phase) >= PHASES.index("stage"):
                self.updater.stage(release_id, extracted)
            if PHASES.index(phase) >= PHASES.index("verify"):
                self.updater.verify(release_id)
            if PHASES.index(phase) >= PHASES.index("activate"):
                self.updater.activate(release_id)

        observed = self.inspect(phase, release_id)
        record = {
            "schema_version": 1,
            "event": "update_phase_boundary",
            "phase": phase,
            "release_id": release_id,
            "bundle_sha256": bundle_sha256,
            "bundle_name": bundle.name,
            "observed_state": observed,
        }
        atomic_write_json(self.context, record)
        self.sync()
        self.telemetry.emit(
            "update_phase_boundary",
            phase=phase,
            release_id=release_id,
            bundle_sha256=bundle_sha256,
        )
        wait_for_power_cut()
        raise HilUpdateError("update interruption helper returned without fixture power removal")

    def inspect(self, phase: str, release_id: str) -> str:
        status = self.updater.status()
        incoming = self.updater.incoming / release_id
        release = self.updater.releases / release_id
        valid = False
        if phase == "begin":
            valid = status.get("in_progress") == release_id and incoming.is_dir() and not any(
                incoming.iterdir()
            )
        elif phase == "stage":
            valid = status.get("in_progress") == release_id and incoming.is_dir()
            if valid:
                self.updater.validate_directory(release_id, incoming)
        elif phase == "verify":
            verified = status.get("verified")
            valid = (
                isinstance(verified, list)
                and release_id in verified
                and status.get("in_progress") is None
                and status.get("current") != release_id
                and status.get("trial") != release_id
                and release.is_dir()
            )
            if valid:
                self.updater.validate_directory(release_id, release)
        elif phase == "activate":
            valid = status.get("current") == release_id and status.get("trial") == release_id
            if valid:
                active_path, manifest = self.updater.active_release()
                valid = active_path == release and manifest.release_id == release_id
        if not valid:
            raise HilUpdateError(f"update state is invalid at the {phase} boundary")
        return OBSERVED_STATES[phase]

    @staticmethod
    def _validated_status(value: Mapping[str, object]) -> dict[str, object]:
        if set(value) != {
            "schema_version",
            "current",
            "previous",
            "trial",
            "in_progress",
            "verified",
        } or value.get("schema_version") != 1:
            raise HilUpdateError("update recovery encountered an invalid updater status")
        for key in ("current", "previous", "trial", "in_progress"):
            item = value.get(key)
            if item is not None and not isinstance(item, str):
                raise HilUpdateError("update recovery encountered an invalid updater reference")
        verified = value.get("verified")
        if (
            not isinstance(verified, list)
            or not all(isinstance(item, str) for item in verified)
            or len(verified) != len(set(verified))
        ):
            raise HilUpdateError("update recovery encountered invalid verified releases")
        return {
            "schema_version": 1,
            "current": value.get("current"),
            "previous": value.get("previous"),
            "trial": value.get("trial"),
            "in_progress": value.get("in_progress"),
            "verified": list(verified),
        }

    @classmethod
    def _expected_clean_status(
        cls, phase: str, release_id: str, boundary_status: Mapping[str, object]
    ) -> dict[str, object]:
        status = cls._validated_status(boundary_status)
        verified = status["verified"]
        assert isinstance(verified, list)
        clean = {
            **status,
            "in_progress": None,
            "verified": [item for item in verified if item != release_id],
        }
        if phase == "activate":
            clean.update(
                {
                    "current": status["previous"],
                    "previous": None,
                    "trial": None,
                }
            )
        if cls._candidate_references(clean, release_id):
            raise HilUpdateError("candidate remains referenced by expected recovery state")
        return clean

    @staticmethod
    def _validate_expected_clean_status(
        value: object, release_id: str
    ) -> dict[str, object]:
        if not isinstance(value, dict):
            raise HilUpdateError("update recovery journal lacks its expected clean state")
        status = UpdateCutController._validated_status(value)
        if UpdateCutController._candidate_references(status, release_id):
            raise HilUpdateError("update recovery journal retains the candidate release")
        if status.get("in_progress") is not None or status.get("trial") is not None:
            raise HilUpdateError("update recovery journal describes an active transaction")
        return status

    def _persist_recovery_stage(
        self,
        boundary: Mapping[str, object],
        expected_clean_status: Mapping[str, object],
        stage: str,
    ) -> None:
        if stage not in RECOVERY_STAGES:
            raise HilUpdateError("invalid update recovery journal stage")
        atomic_write_json(
            self.context,
            {
                **boundary,
                "recovery": {
                    "schema_version": 1,
                    "stage": stage,
                    "expected_clean_status": dict(expected_clean_status),
                },
            },
        )
        self.sync()
        self.recovery_checkpoint(stage)

    def _candidate_paths_clean(self, release_id: str) -> bool:
        incoming = self.updater.incoming / release_id
        release = self.updater.releases / release_id
        return not (incoming.exists() or incoming.is_symlink()) and not (
            release.exists() or release.is_symlink()
        )

    def _cleanup_updater(
        self,
        phase: str,
        release_id: str,
        expected_clean_status: Mapping[str, object],
    ) -> None:
        status = self._validated_status(self.updater.status())
        if status == expected_clean_status and self._candidate_paths_clean(release_id):
            return

        expected_verified = expected_clean_status["verified"]
        assert isinstance(expected_verified, list)
        status_verified = status["verified"]
        assert isinstance(status_verified, list)
        common_state_matches = all(
            status.get(key) == expected_clean_status.get(key)
            for key in ("current", "previous", "trial", "in_progress")
        )

        if phase in {"begin", "stage"}:
            boundary_matches = (
                status.get("current") == expected_clean_status.get("current")
                and status.get("previous") == expected_clean_status.get("previous")
                and status.get("trial") == expected_clean_status.get("trial")
                and status.get("in_progress") == release_id
                and status_verified == expected_verified
            )
            if not boundary_matches:
                raise HilUpdateError("incoming cleanup state does not match its recovery journal")
            self.updater.abort(release_id)
        elif phase == "verify":
            verified_matches = [item for item in status_verified if item != release_id] == (
                expected_verified
            )
            if not common_state_matches or not verified_matches:
                raise HilUpdateError("verified cleanup state does not match its recovery journal")
            self.updater.discard_verified(release_id)
        else:
            boundary_matches = (
                status.get("current") == release_id
                and status.get("trial") == release_id
                and status.get("previous") == expected_clean_status.get("current")
                and status.get("in_progress") is None
                and [item for item in status_verified if item != release_id]
                == expected_verified
            )
            if boundary_matches:
                self.updater.rollback()
                status = self._validated_status(self.updater.status())
                status_verified = status["verified"]
                assert isinstance(status_verified, list)
                common_state_matches = all(
                    status.get(key) == expected_clean_status.get(key)
                    for key in ("current", "previous", "trial", "in_progress")
                )
            verified_matches = [item for item in status_verified if item != release_id] == (
                expected_verified
            )
            if not common_state_matches or not verified_matches:
                raise HilUpdateError("rollback cleanup state does not match its recovery journal")
            self.updater.discard_verified(release_id)

        final_status = self._validated_status(self.updater.status())
        if final_status != expected_clean_status or not self._candidate_paths_clean(release_id):
            raise HilUpdateError("candidate update state was not cleaned after recovery")

    @staticmethod
    def _recovery_result(
        phase: str, release_id: str, bundle_sha256: str
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "event": "update_power_cut_recovery",
            "phase": phase,
            "release_id": release_id,
            "bundle_sha256": bundle_sha256,
            "recovered": True,
            "state_valid": True,
            "observed_state": OBSERVED_STATES[phase],
            "cleanup_complete": True,
            "post_cleanup_clean": True,
            "fallback_used": False,
        }

    @staticmethod
    def _fsync_directory(path: Path, *, label: str) -> None:
        if os.name == "nt":
            return
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise HilUpdateError(f"cannot durably synchronize {label}") from exc

    def _remove_uploaded_bundle(self, bundle_name: str, bundle_sha256: str) -> None:
        uploaded_bundle = self.root / "uploads" / bundle_name
        if uploaded_bundle.exists() or uploaded_bundle.is_symlink():
            if _sha256_regular(uploaded_bundle) != bundle_sha256:
                raise HilUpdateError("retained uploaded bundle no longer matches recovery context")
            uploaded_bundle.unlink()
            self.recovery_checkpoint("upload_unlinked")
            self._fsync_directory(uploaded_bundle.parent, label="recovered update uploads")
            self.recovery_checkpoint("upload_removed")
        if uploaded_bundle.exists() or uploaded_bundle.is_symlink():
            raise HilUpdateError("uploaded update bundle remains after recovery")

    def recover(self, phase: str, release_id: str, bundle_sha256: str) -> dict[str, object]:
        if phase not in PHASES:
            raise HilUpdateError("invalid update recovery phase")
        release_id = validate_release_id(release_id)
        bundle_sha256 = _validate_digest(bundle_sha256, label="bundle SHA-256")
        history = self._history_path(phase, release_id, bundle_sha256)
        result = self._recovery_result(phase, release_id, bundle_sha256)
        if not self.context.exists() and history.exists():
            recorded = _read_json(history)
            if recorded != result:
                raise HilUpdateError("completed update recovery history does not match the request")
            return result
        context = _read_json(self.context)
        expected_boundary = {
            "schema_version": 1,
            "event": "update_phase_boundary",
            "phase": phase,
            "release_id": release_id,
            "bundle_sha256": bundle_sha256,
            "observed_state": OBSERVED_STATES[phase],
        }
        bundle_name = context.get("bundle_name")
        recovery = context.get("recovery")
        boundary = {key: value for key, value in context.items() if key != "recovery"}
        if (
            not isinstance(bundle_name, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", bundle_name) is None
            or {key: value for key, value in boundary.items() if key != "bundle_name"}
            != expected_boundary
        ):
            raise HilUpdateError("update recovery request does not match durable context")
        if recovery is None:
            self.inspect(phase, release_id)
            expected_clean_status = self._expected_clean_status(
                phase, release_id, self.updater.status()
            )
            stage = "cleanup_pending"
            self._persist_recovery_stage(boundary, expected_clean_status, stage)
        else:
            if not isinstance(recovery, dict) or set(recovery) != {
                "schema_version",
                "stage",
                "expected_clean_status",
            }:
                raise HilUpdateError("update recovery journal is invalid")
            stage_value = recovery.get("stage")
            if recovery.get("schema_version") != 1 or stage_value not in RECOVERY_STAGES:
                raise HilUpdateError("update recovery journal stage is invalid")
            assert isinstance(stage_value, str)
            stage = stage_value
            expected_clean_status = self._validate_expected_clean_status(
                recovery.get("expected_clean_status"), release_id
            )

        if RECOVERY_STAGES.index(stage) <= RECOVERY_STAGES.index("cleanup_pending"):
            self._cleanup_updater(phase, release_id, expected_clean_status)
            stage = "updater_clean"
            self._persist_recovery_stage(boundary, expected_clean_status, stage)
        else:
            if (
                self._validated_status(self.updater.status()) != expected_clean_status
                or not self._candidate_paths_clean(release_id)
            ):
                raise HilUpdateError("journaled updater cleanup is no longer clean")

        if RECOVERY_STAGES.index(stage) <= RECOVERY_STAGES.index("updater_clean"):
            self._remove_uploaded_bundle(bundle_name, bundle_sha256)
            stage = "upload_clean"
            self._persist_recovery_stage(boundary, expected_clean_status, stage)

        if history.exists():
            if _read_json(history) != result:
                raise HilUpdateError("update recovery history conflicts with durable context")
        else:
            atomic_write_json(history, result)
        if stage != "history_written":
            self.recovery_checkpoint("history_file_written")
            self._persist_recovery_stage(boundary, expected_clean_status, "history_written")
        self.context.unlink()
        self.recovery_checkpoint("context_unlinked")
        self._fsync_directory(self.hil_state, label="update recovery journal")
        self.sync()
        return result


def _wait_for_power_cut() -> None:
    deadline = time.monotonic() + 180.0
    while time.monotonic() < deadline:
        time.sleep(1.0)
    raise HilUpdateError("fixture did not remove power within 180 seconds")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tidetracker-hil-update")
    parser.add_argument("--root", type=Path, default=Path("/var/lib/tidetracker"))
    parser.add_argument(
        "--public-key", type=Path, default=Path("/etc/tidetracker/release-ed25519.pub.pem")
    )
    parser.add_argument("--uart", type=Path, default=Path("/dev/serial0"))
    commands = parser.add_subparsers(dest="command", required=True)
    cut = commands.add_parser("cut")
    cut.add_argument("bundle", type=Path)
    cut.add_argument("phase", choices=PHASES)
    cut.add_argument("release_id")
    cut.add_argument("bundle_sha256")
    recover = commands.add_parser("recover")
    recover.add_argument("phase", choices=PHASES)
    recover.add_argument("release_id")
    recover.add_argument("bundle_sha256")
    args = parser.parse_args(argv)
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise HilUpdateError("update interruption control requires root")
    telemetry = UartPhaseTelemetry(SerialLineWriter(args.uart))
    controller = UpdateCutController(args.root, args.public_key, telemetry)
    try:
        if args.command == "cut":
            controller.prepare(
                args.bundle,
                args.phase,
                args.release_id,
                args.bundle_sha256,
                wait_for_power_cut=_wait_for_power_cut,
            )
            raise AssertionError("unreachable")
        result = controller.recover(args.phase, args.release_id, args.bundle_sha256)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    finally:
        telemetry.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (HilUpdateError, ReleaseStateError, OSError, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        raise SystemExit(2) from None
