"""Power-loss-resistant atomic storage primitives and cycle state store."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from .deadlines import run_with_wall_clock_deadline
from .models import CycleResult, StoredCycle, TidePrediction
from .telemetry import redact_text


class StorageError(RuntimeError):
    pass


def bounded_system_sync(
    timeout_seconds: float = 5.0,
    *,
    sync: Any | None = None,
) -> None:
    """Attempt a global sync without allowing a wedged filesystem to block DONE."""

    if timeout_seconds <= 0:
        raise ValueError("sync timeout must be positive")
    operation = sync if sync is not None else getattr(os, "sync", None)
    if operation is None:
        return
    try:
        run_with_wall_clock_deadline(
            operation,
            timeout_seconds=timeout_seconds,
            timeout_error=StorageError("system sync exceeded its wall-clock timeout"),
        )
    except StorageError:
        raise
    except Exception as exc:
        raise StorageError("system sync failed") from exc


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        # Windows cannot open directories with the POSIX durability contract.
        # The field image is Linux and must fail closed on every error below.
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise StorageError(f"cannot open directory for durability sync: {path}") from exc
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            raise StorageError(f"cannot durably sync directory: {path}") from exc
    finally:
        os.close(descriptor)


def _durable_unlink(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise StorageError(f"cannot inspect temporary state file: {path}") from exc
    if stat.S_ISDIR(metadata.st_mode):
        raise StorageError(f"temporary state path is a directory: {path}")
    try:
        path.unlink()
        _fsync_directory(path.parent)
    except OSError as exc:
        raise StorageError(f"cannot durably remove temporary state file: {path}") from exc
    if path.exists() or path.is_symlink():
        raise StorageError(f"temporary state file removal could not be proved: {path}")
    return True


def _cleanup_destination_temps(destination: Path, *, maximum: int = 8) -> None:
    """Remove only mkstemp names owned by one exact atomic-write destination."""

    pattern = f".{destination.name}.*.tmp"
    candidates = tuple(destination.parent.glob(pattern))
    if len(candidates) > maximum:
        raise StorageError(f"too many interrupted atomic writes for {destination}")
    prefix = f".{destination.name}."
    for path in candidates:
        token = path.name.removeprefix(prefix).removesuffix(".tmp")
        if (
            not path.name.startswith(prefix)
            or not path.name.endswith(".tmp")
            or not 6 <= len(token) <= 16
            or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in token)
        ):
            continue
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or (os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600)
            or (hasattr(os, "geteuid") and metadata.st_uid != os.geteuid())
        ):
            raise StorageError(f"interrupted atomic-write temp is unsafe: {path}")
        _durable_unlink(path)


def _recover_cycle_store_temps(root: Path, *, maximum: int = 256) -> None:
    """Bound and remove exact state/frame temp names after an interrupted cycle."""

    if not root.exists():
        return
    visited = 0
    for raw_directory, directories, files in os.walk(root, followlinks=False):
        directory = Path(raw_directory)
        if directory.is_symlink():
            raise StorageError("cycle state contains a symbolic-link directory")
        for name in directories:
            child = directory / name
            if child.is_symlink():
                raise StorageError("cycle state contains a symbolic-link directory")
        for name in files:
            visited += 1
            if visited > maximum:
                raise StorageError("cycle state temp recovery exceeded its file bound")
            match = re.fullmatch(
                r"\.(?:last_good\.json|last_result\.json|display_state\.json|"
                r"[0-9a-f]{64}\.bin)\.([A-Za-z0-9_-]{6,16})\.tmp",
                name,
            )
            if match is None:
                continue
            path = directory / name
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or path.is_symlink()
                or (os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600)
                or (hasattr(os, "geteuid") and metadata.st_uid != os.geteuid())
            ):
                raise StorageError("interrupted cycle-state temp is unsafe")
            _durable_unlink(path)


def _ensure_directory(path: Path) -> None:
    if path.is_dir():
        return
    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    path.mkdir(parents=True, exist_ok=True)
    for created in reversed(missing):
        _fsync_directory(created.parent)


def atomic_write_bytes(path: str | Path, data: bytes, *, mode: int = 0o600) -> None:
    """Write, fsync, replace, and fsync the containing directory."""

    destination = Path(path)
    _ensure_directory(destination.parent)
    _cleanup_destination_temps(destination)
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        temporary_path = Path(temporary_name)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            with suppress(OSError):
                os.close(descriptor)
            raise
        os.replace(temporary_path, destination)
        temporary_path = None
        with suppress(OSError):
            os.chmod(destination, mode)
        _fsync_directory(destination.parent)
    except OSError as exc:
        raise StorageError(f"atomic write failed for {destination}: {exc}") from exc
    finally:
        if temporary_path is not None:
            _durable_unlink(temporary_path)


def atomic_write_json(path: str | Path, value: Mapping[str, Any], *, mode: int = 0o600) -> None:
    payload = (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")
    atomic_write_bytes(path, payload, mode=mode)


def _recover_pruned_atomic_temps(
    root: Path, pattern: str, *, maximum: int = 256
) -> None:
    """Remove interrupted atomic writes for destinations covered by a prune pass."""

    candidates: list[tuple[Path, str]] = []
    try:
        entries = os.scandir(root)
        with entries:
            for entry in entries:
                name = entry.name
                if not name.startswith(".") or not name.endswith(".tmp"):
                    continue
                body = name[1:-4]
                if "." not in body:
                    continue
                destination_name, token = body.rsplit(".", 1)
                if not fnmatchcase(destination_name, pattern):
                    continue
                candidates.append((Path(entry.path), token))
                if len(candidates) > maximum:
                    raise StorageError("atomic prune recovery exceeded its temp bound")
    except StorageError:
        raise
    except OSError as exc:
        raise StorageError(f"cannot inventory retained files in {root}") from exc

    for path, token in candidates:
        if (
            not 6 <= len(token) <= 16
            or any(
                character
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                for character in token
            )
        ):
            raise StorageError(f"interrupted retained-file temp is malformed: {path}")
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise StorageError(f"cannot inspect interrupted retained-file temp: {path}") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_nlink != 1
            or (os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600)
            or (hasattr(os, "geteuid") and metadata.st_uid != os.geteuid())
        ):
            raise StorageError(f"interrupted retained-file temp is unsafe: {path}")
        _durable_unlink(path)


def prune_regular_files(directory: str | Path, pattern: str, *, keep: int) -> None:
    """Retain only the newest bounded set and fsync directory deletions."""

    if keep < 0:
        raise ValueError("retention count must not be negative")
    root = Path(directory)
    if not root.exists():
        return
    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise StorageError(f"cannot inspect retention directory {root}") from exc
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise StorageError(f"retention directory is unsafe: {root}")
    _recover_pruned_atomic_temps(root, pattern)
    candidates: list[tuple[int, str, Path]] = []
    for path in root.glob(pattern):
        try:
            metadata = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(metadata.st_mode) and not path.is_symlink():
            candidates.append((metadata.st_mtime_ns, path.name, path))
    candidates.sort(reverse=True)
    changed = False
    for _, _, path in candidates[keep:]:
        try:
            path.unlink()
            changed = True
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise StorageError(f"cannot prune retained file {path}: {exc}") from exc
    if changed:
        _fsync_directory(root)


class AtomicStore:
    """Atomic files constrained beneath one state root."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        _ensure_directory(self.root)
        _recover_cycle_store_temps(self.root)

    def path(self, relative: str | Path) -> Path:
        candidate = (self.root / relative).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise StorageError("state path escapes storage root") from exc
        return candidate

    def write_bytes(self, relative: str | Path, data: bytes, *, mode: int = 0o600) -> None:
        atomic_write_bytes(self.path(relative), data, mode=mode)

    def write_json(
        self, relative: str | Path, value: Mapping[str, Any], *, mode: int = 0o600
    ) -> None:
        atomic_write_json(self.path(relative), value, mode=mode)

    def read_bytes(self, relative: str | Path) -> bytes:
        try:
            return self.path(relative).read_bytes()
        except OSError as exc:
            raise StorageError(f"cannot read {relative}: {exc}") from exc

    def read_json(self, relative: str | Path) -> Mapping[str, Any]:
        try:
            value = json.loads(self.path(relative).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StorageError(f"cannot read {relative}: {exc}") from exc
        if not isinstance(value, dict):
            raise StorageError(f"{relative} must contain a JSON object")
        return value


def _result_mapping(result: CycleResult) -> dict[str, Any]:
    return {
        "version": 1,
        "run_id": result.run_id,
        "success": result.success,
        "mode": result.mode.value,
        "started_at": result.started_at.isoformat(),
        "ended_at": result.ended_at.isoformat(),
        "phases": [phase.value for phase in result.phases],
        "phase_durations_seconds": dict(result.phase_durations_seconds),
        "framebuffer_sha256": result.framebuffer_sha256,
        "display_refreshed": result.display_refreshed,
        "used_cached_data": result.used_cached_data,
        "network_interface": result.network_interface,
        "production_route_interface": result.production_route_interface,
        "release_id": result.release_id,
        "execution_source": result.execution_source,
        "noaa_fault_kind": result.noaa_fault_kind,
        "injected_noaa_fault": result.injected_noaa_fault,
        "wifi_active_observed_seconds": result.wifi_active_observed_seconds,
        "wifi_active_observation_source": result.wifi_active_observation_source,
        "execution": {
            "release_id": result.release_id,
            "execution_source": result.execution_source,
            "noaa_fault_kind": result.noaa_fault_kind,
            "injected_noaa_fault": result.injected_noaa_fault,
            "wifi_active_observed_seconds": result.wifi_active_observed_seconds,
            "wifi_active_observation_source": result.wifi_active_observation_source,
        },
        "error_phase": result.error_phase.value if result.error_phase else None,
        "error_type": result.error_type,
        "error_message": redact_text(result.error_message) if result.error_message else None,
    }


class AtomicCycleStore:
    """Content-addressed last-good framebuffer plus atomically switched metadata."""

    def __init__(self, root: str | Path) -> None:
        self.files = AtomicStore(root)

    def _prune_frames(self, preserve: set[str], *, keep_extra: int = 0) -> None:
        frames = self.files.path("frames")
        if not frames.exists():
            return
        extras: list[tuple[int, str, Path]] = []
        changed = False
        for path in frames.glob("*.bin"):
            if path.stem in preserve:
                continue
            try:
                metadata = path.lstat()
            except OSError:
                continue
            if (
                len(path.stem) == 64
                and all(char in "0123456789abcdef" for char in path.stem)
                and stat.S_ISREG(metadata.st_mode)
                and not path.is_symlink()
            ):
                extras.append((metadata.st_mtime_ns, path.name, path))
        extras.sort(reverse=True)
        for _, _, path in extras[keep_extra:]:
            try:
                path.unlink()
                changed = True
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise StorageError(f"cannot prune framebuffer {path}: {exc}") from exc
        if changed:
            _fsync_directory(frames)

    def load_last_good(self) -> StoredCycle | None:
        metadata_path = self.files.path("last_good.json")
        if not metadata_path.exists():
            return None
        metadata = self.files.read_json("last_good.json")
        try:
            if metadata.get("version") != 1:
                raise StorageError("unsupported last-good state version")
            digest = str(metadata["framebuffer_sha256"])
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise StorageError("invalid framebuffer digest")
            framebuffer = self.files.read_bytes(Path("frames") / f"{digest}.bin")
            if hashlib.sha256(framebuffer).hexdigest() != digest:
                raise StorageError("last-good framebuffer checksum mismatch")
            predictions_raw = metadata["predictions"]
            if not isinstance(predictions_raw, list):
                raise StorageError("last-good predictions must be a list")
            predictions = tuple(
                TidePrediction(
                    timestamp=datetime.fromisoformat(str(item["timestamp"])),
                    value=float(item["value"]),
                )
                for item in predictions_raw
                if isinstance(item, dict)
            )
            if len(predictions) != len(predictions_raw):
                raise StorageError("invalid last-good prediction entry")
            snapshot = StoredCycle(
                station_id=str(metadata["station_id"]),
                generated_at=datetime.fromisoformat(str(metadata["generated_at"])),
                predictions=predictions,
                framebuffer=framebuffer,
                framebuffer_sha256=digest,
            )
            # Recover from a cut after a new content-addressed frame was written
            # but before metadata switched. Keep current plus one newest recovery
            # candidate, and bound every older orphan.
            with suppress(StorageError):
                self._prune_frames({digest}, keep_extra=1)
            return snapshot
        except (KeyError, TypeError, ValueError) as exc:
            raise StorageError(f"invalid last-good state: {exc}") from exc

    def load_display_state(self) -> str | None:
        path = self.files.path("display_state.json")
        if not path.exists():
            return None
        state = self.files.read_json("display_state.json")
        if state.get("version") != 1 or not isinstance(state.get("known"), bool):
            raise StorageError("display state has an invalid schema")
        if state["known"] is False:
            return None
        digest = state.get("framebuffer_sha256")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise StorageError("display state has an invalid framebuffer digest")
        return digest

    def invalidate_display_state(self) -> None:
        self.files.write_json(
            "display_state.json",
            {"version": 1, "known": False, "framebuffer_sha256": None},
        )

    def mark_display_state(self, framebuffer_sha256: str) -> None:
        if (
            len(framebuffer_sha256) != 64
            or any(char not in "0123456789abcdef" for char in framebuffer_sha256)
        ):
            raise StorageError("display framebuffer digest is invalid")
        self.files.write_json(
            "display_state.json",
            {
                "version": 1,
                "known": True,
                "framebuffer_sha256": framebuffer_sha256,
            },
        )

    def persist(self, snapshot: StoredCycle) -> None:
        actual_digest = hashlib.sha256(snapshot.framebuffer).hexdigest()
        if snapshot.framebuffer_sha256 != actual_digest:
            raise StorageError("snapshot framebuffer digest is incorrect")
        frame_relative = Path("frames") / f"{actual_digest}.bin"
        frame_path = self.files.path(frame_relative)
        previous_digest: str | None = None
        metadata_path = self.files.path("last_good.json")
        if metadata_path.exists():
            with suppress(StorageError):
                previous = self.files.read_json("last_good.json").get(
                    "framebuffer_sha256"
                )
                if (
                    isinstance(previous, str)
                    and len(previous) == 64
                    and all(char in "0123456789abcdef" for char in previous)
                ):
                    previous_digest = previous
        if (
            not frame_path.exists()
            or hashlib.sha256(frame_path.read_bytes()).hexdigest() != actual_digest
        ):
            self.files.write_bytes(frame_relative, snapshot.framebuffer)
        self.files.write_json(
            "last_good.json",
            {
                "version": 1,
                "station_id": snapshot.station_id,
                "generated_at": snapshot.generated_at.isoformat(),
                "framebuffer_sha256": actual_digest,
                "predictions": [
                    {"timestamp": item.timestamp.isoformat(), "value": item.value}
                    for item in snapshot.predictions
                ],
            },
        )
        preserve = {actual_digest}
        if previous_digest is not None:
            preserve.add(previous_digest)
        self._prune_frames(preserve)

    def persist_result(self, result: CycleResult) -> None:
        self.files.write_json("last_result.json", _result_mapping(result))

    def sync(self) -> None:
        bounded_system_sync()


class MemoryStateStore:
    """Deterministic test/development implementation of the state port."""

    def __init__(self, last_good: StoredCycle | None = None) -> None:
        self.last_good = last_good
        self.display_state = last_good.framebuffer_sha256 if last_good is not None else None
        self.display_invalidations = 0
        self.display_marks: list[str] = []
        self.results: list[CycleResult] = []
        self.sync_count = 0

    def load_last_good(self) -> StoredCycle | None:
        return self.last_good

    def load_display_state(self) -> str | None:
        return self.display_state

    def invalidate_display_state(self) -> None:
        self.display_state = None
        self.display_invalidations += 1

    def mark_display_state(self, framebuffer_sha256: str) -> None:
        self.display_state = framebuffer_sha256
        self.display_marks.append(framebuffer_sha256)

    def persist(self, snapshot: StoredCycle) -> None:
        self.last_good = snapshot

    def persist_result(self, result: CycleResult) -> None:
        self.results.append(result)

    def sync(self) -> None:
        self.sync_count += 1
