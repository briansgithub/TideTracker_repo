"""Signed, atomic application release staging for TideTracker.

OS and kernel updates intentionally remain outside this module.  A release is a
directory containing ``manifest.json``, ``manifest.sig`` and only the payload files
listed by the signed manifest.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

MANIFEST_NAME = "manifest.json"
SIGNATURE_NAME = "manifest.sig"
STATE_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_SIGNATURE_BYTES = 16 * 1024
MAX_STATE_BYTES = 64 * 1024
MAX_STATE_TEMPS = 8
MAX_RELEASE_INVENTORY = 64
# Field extraction shares a 64 MiB RAM-backed /tmp with the running system.
# Keep the complete application payload below half of that budget.
MAX_RELEASE_BYTES = 24 * 1024 * 1024
RELEASE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
STATE_TEMP_PATTERN = re.compile(r"\.state\.json\.[1-9][0-9]{0,9}\.[1-9][0-9]{0,19}\.tmp\Z")


class UpdateError(RuntimeError):
    """Base class for release-management failures."""


class ReleaseStateError(UpdateError):
    pass


class ReleaseVerificationError(UpdateError):
    pass


class SignatureVerifier(Protocol):
    def verify(self, manifest: Path, signature: Path) -> bool: ...


class CommandRunner(Protocol):
    def __call__(
        self, args: Sequence[str], *, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]: ...


def _command_runner(
    args: Sequence[str], *, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        capture_output=True,
        check=False,
        shell=False,
        text=True,
        timeout=timeout,
    )


def _resolve_openssl(binary: str) -> str:
    discovered = shutil.which(binary)
    if discovered is not None:
        return discovered
    if os.name == "nt" and binary == "openssl":
        git = shutil.which("git")
        if git is not None:
            candidate = Path(git).resolve().parents[1] / "usr" / "bin" / "openssl.exe"
            if candidate.is_file():
                return str(candidate)
    return binary


class OpenSSLEd25519Verifier:
    """Verify raw Ed25519 signatures using the OS-provided OpenSSL binary."""

    def __init__(
        self,
        public_key: Path,
        *,
        binary: str = "openssl",
        runner: CommandRunner = _command_runner,
        timeout: float = 10.0,
    ) -> None:
        self.public_key = Path(public_key)
        self.binary = _resolve_openssl(binary)
        self._runner = runner
        self.timeout = timeout

    def verify(self, manifest: Path, signature: Path) -> bool:
        if not self.public_key.is_file():
            raise ReleaseVerificationError("release public key is unavailable")
        try:
            result = self._runner(
                [
                    self.binary,
                    "pkeyutl",
                    "-verify",
                    "-pubin",
                    "-inkey",
                    str(self.public_key),
                    "-rawin",
                    "-in",
                    str(manifest),
                    "-sigfile",
                    str(signature),
                ],
                timeout=self.timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReleaseVerificationError("release signature could not be verified") from exc
        return result.returncode == 0


def validate_release_id(value: object) -> str:
    if not isinstance(value, str) or value in {".", ".."} or not RELEASE_ID_PATTERN.fullmatch(value):
        raise ValueError("invalid release ID")
    return value


@dataclass(frozen=True, slots=True)
class ManifestFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    release_id: str
    files: tuple[ManifestFile, ...]
    entrypoint: str | None = None

    @classmethod
    def load(cls, path: Path) -> ReleaseManifest:
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ReleaseVerificationError("release manifest is unavailable") from exc
        if not 0 < size <= MAX_MANIFEST_BYTES:
            raise ReleaseVerificationError("release manifest has an invalid size")

        def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ReleaseVerificationError("release manifest contains duplicate fields")
                result[key] = value
            return result

        try:
            value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseVerificationError("release manifest is invalid") from exc
        if not isinstance(value, dict) or value.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            raise ReleaseVerificationError("release manifest schema is unsupported")
        try:
            release_id = validate_release_id(value.get("release_id"))
        except ValueError as exc:
            raise ReleaseVerificationError("release manifest has an invalid release ID") from exc
        raw_files = value.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            raise ReleaseVerificationError("release manifest must list payload files")

        files: list[ManifestFile] = []
        seen: set[str] = set()
        total = 0
        for item in raw_files:
            if not isinstance(item, dict):
                raise ReleaseVerificationError("release manifest contains an invalid file record")
            raw_path = item.get("path")
            size_value = item.get("size")
            digest = item.get("sha256")
            if not isinstance(raw_path, str) or "\\" in raw_path:
                raise ReleaseVerificationError("release manifest contains an unsafe path")
            relative = PurePosixPath(raw_path)
            if (
                relative.is_absolute()
                or not relative.parts
                or any(part in {"", ".", ".."} for part in relative.parts)
                or raw_path in {MANIFEST_NAME, SIGNATURE_NAME}
            ):
                raise ReleaseVerificationError("release manifest contains an unsafe path")
            normalized = relative.as_posix()
            if normalized in seen:
                raise ReleaseVerificationError("release manifest contains duplicate paths")
            if not isinstance(size_value, int) or isinstance(size_value, bool) or size_value < 0:
                raise ReleaseVerificationError("release manifest contains an invalid file size")
            if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
                raise ReleaseVerificationError("release manifest contains an invalid hash")
            total += size_value
            if total > MAX_RELEASE_BYTES:
                raise ReleaseVerificationError("release exceeds the size limit")
            seen.add(normalized)
            files.append(ManifestFile(normalized, size_value, digest))

        entrypoint_value = value.get("entrypoint")
        entrypoint: str | None
        if entrypoint_value is None:
            entrypoint = None
        elif not isinstance(entrypoint_value, str) or entrypoint_value not in seen:
            raise ReleaseVerificationError("release manifest entrypoint is not a payload file")
        else:
            entrypoint = entrypoint_value
        return cls(release_id, tuple(files), entrypoint)


def _sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        # Windows does not expose Linux's directory-fsync contract. Field
        # deployment is Linux; workstation tests still exercise atomic replace.
        return
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        raise ReleaseStateError("release directory could not be opened for fsync") from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise ReleaseStateError("release directory fsync failed") from exc
    finally:
        os.close(descriptor)


def _durable_unlink(path: Path) -> bool:
    """Unlink one non-directory entry and durably prove its name is absent."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ReleaseStateError("release cleanup could not inspect a path") from exc
    if stat.S_ISDIR(metadata.st_mode):
        raise ReleaseStateError("release cleanup encountered a directory")
    try:
        path.unlink()
        _fsync_directory(path.parent)
    except OSError as exc:
        raise ReleaseStateError("release cleanup was not durable") from exc
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise ReleaseStateError("release cleanup could not be proved") from exc
    raise ReleaseStateError("release cleanup did not remove its target")


def _stable_private_file_bytes(path: Path, *, maximum_bytes: int) -> bytes:
    """Read one stable, owner-private regular file without following links."""

    try:
        initial = path.lstat()
    except OSError as exc:
        raise ReleaseStateError("release state is unreadable") from exc
    if stat.S_ISLNK(initial.st_mode) or not stat.S_ISREG(initial.st_mode):
        raise ReleaseStateError("release state path is unsafe")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            chunks: list[bytes] = []
            remaining = maximum_bytes + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        current = path.lstat()
    except OSError as exc:
        raise ReleaseStateError("release state cannot be read safely") from exc

    payload = b"".join(chunks)
    stable_fields: tuple[str, ...] = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_uid",
        "st_gid",
        "st_size",
        "st_mtime_ns",
    )
    if os.name != "nt":
        stable_fields += ("st_ctime_ns",)
    if (
        any(getattr(initial, field) != getattr(before, field) for field in stable_fields)
        or any(getattr(before, field) != getattr(after, field) for field in stable_fields)
        or any(getattr(before, field) != getattr(current, field) for field in stable_fields)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size != len(payload)
        or not 0 < len(payload) <= maximum_bytes
        or (os.name != "nt" and stat.S_IMODE(before.st_mode) != 0o600)
        or (hasattr(os, "geteuid") and before.st_uid != os.geteuid())
    ):
        raise ReleaseStateError("release state is malformed or unsafe")
    return payload


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")
    if not 0 < len(payload) <= MAX_STATE_BYTES:
        raise ReleaseStateError("release state exceeds its size bound")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        _durable_unlink(temporary)


def _initial_state(now: float) -> dict[str, object]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "current": None,
        "previous": None,
        "trial": None,
        "in_progress": None,
        "verified_order": [],
        "updated_at": now,
    }


class ReleaseUpdater:
    """Command-layer service for staging, activating and rolling back releases."""

    def __init__(
        self,
        root: Path,
        verifier: SignatureVerifier,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root)
        self.verifier = verifier
        self.clock = clock
        self.incoming = self.root / "incoming"
        self.releases = self.root / "releases"
        self.state_path = self.root / "state.json"
        self.lock_path = self.root / ".update.lock"
        self._thread_lock = threading.RLock()
        for directory in (self.root, self.incoming, self.releases):
            if directory.exists():
                if not directory.is_dir() or directory.is_symlink():
                    raise ReleaseStateError("release storage path is unsafe")
                continue
            directory.mkdir(mode=0o700, parents=True, exist_ok=False)
            _fsync_directory(directory.parent)
        with self._locked():
            self._read_state()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            self.lock_path.touch(mode=0o600, exist_ok=True)
            with self.lock_path.open("a+b") as lock:
                if os.name == "nt":
                    import msvcrt

                    lock.seek(0, os.SEEK_END)
                    if lock.tell() == 0:
                        lock.write(b"\0")
                        lock.flush()
                    lock.seek(0)
                    msvcrt.locking(  # type: ignore[attr-defined]
                        lock.fileno(), msvcrt.LK_LOCK, 1  # type: ignore[attr-defined]
                    )
                    try:
                        yield
                    finally:
                        lock.seek(0)
                        msvcrt.locking(  # type: ignore[attr-defined]
                            lock.fileno(), msvcrt.LK_UNLCK, 1  # type: ignore[attr-defined]
                        )
                else:
                    fcntl = importlib.import_module("fcntl")
                    flock = fcntl.flock
                    flock(lock.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        flock(lock.fileno(), fcntl.LOCK_UN)

    def _cleanup_state_temps(self) -> None:
        """Bound and durably remove only updater-owned interrupted state writes."""

        candidates: list[Path] = []
        try:
            entries = os.scandir(self.root)
            with entries:
                for entry in entries:
                    if not (
                        entry.name.startswith(".state.json.")
                        and entry.name.endswith(".tmp")
                    ):
                        continue
                    candidates.append(Path(entry.path))
                    if len(candidates) > MAX_STATE_TEMPS:
                        raise ReleaseStateError(
                            "release state temp recovery exceeded its entry bound"
                        )
        except ReleaseStateError:
            raise
        except OSError as exc:
            raise ReleaseStateError("release state directory cannot be inventoried") from exc
        for path in candidates:
            if STATE_TEMP_PATTERN.fullmatch(path.name) is None:
                raise ReleaseStateError("release state temporary path is malformed")
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise ReleaseStateError("release state temporary path is unavailable") from exc
            if (
                not stat.S_ISREG(metadata.st_mode)
                or path.is_symlink()
                or metadata.st_nlink != 1
                or metadata.st_size > MAX_STATE_BYTES
                or (os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600)
                or (hasattr(os, "geteuid") and metadata.st_uid != os.geteuid())
            ):
                raise ReleaseStateError("release state temporary path is unsafe")
            _durable_unlink(path)

    def _read_state_file(self) -> dict[str, object]:
        self._cleanup_state_temps()
        try:
            self.state_path.lstat()
        except FileNotFoundError:
            return _initial_state(self.clock())
        except OSError as exc:
            raise ReleaseStateError("release state is unreadable") from exc
        try:
            value = json.loads(
                _stable_private_file_bytes(
                    self.state_path, maximum_bytes=MAX_STATE_BYTES
                ).decode("utf-8")
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseStateError("release state is unreadable") from exc
        if not isinstance(value, dict) or value.get("schema_version") != STATE_SCHEMA_VERSION:
            raise ReleaseStateError("release state schema is unsupported")
        for key in ("current", "previous", "trial", "in_progress"):
            item = value.get(key)
            if item is not None:
                try:
                    validate_release_id(item)
                except ValueError as exc:
                    raise ReleaseStateError("release state contains an invalid release ID") from exc
        order = value.get("verified_order")
        if not isinstance(order, list) or len(order) != len(set(order)):
            raise ReleaseStateError("release state has an invalid verified history")
        try:
            for item in order:
                validate_release_id(item)
        except ValueError as exc:
            raise ReleaseStateError("release state has an invalid verified history") from exc
        verified = set(order)
        for key in ("current", "previous", "trial"):
            item = value.get(key)
            if item is not None and item not in verified:
                raise ReleaseStateError("release state references an unverified release")
        trial = value.get("trial")
        if trial is not None and trial != value.get("current"):
            raise ReleaseStateError("release trial state is inconsistent")
        return value

    def _inventory_releases(self) -> dict[str, Path]:
        inventory: dict[str, Path] = {}
        try:
            entries = os.scandir(self.releases)
            with entries:
                for entry in entries:
                    if len(inventory) >= MAX_RELEASE_INVENTORY:
                        raise ReleaseStateError("release inventory exceeded its entry bound")
                    try:
                        release_id = validate_release_id(entry.name)
                        metadata = entry.stat(follow_symlinks=False)
                    except (OSError, ValueError) as exc:
                        raise ReleaseStateError(
                            "release inventory contains an unsafe entry"
                        ) from exc
                    if (
                        stat.S_ISLNK(metadata.st_mode)
                        or not stat.S_ISDIR(metadata.st_mode)
                        or (os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o022)
                        or (hasattr(os, "geteuid") and metadata.st_uid != os.geteuid())
                    ):
                        raise ReleaseStateError("release inventory contains an unsafe entry")
                    inventory[release_id] = Path(entry.path)
        except ReleaseStateError:
            raise
        except OSError as exc:
            raise ReleaseStateError("release inventory is unavailable") from exc
        return inventory

    def _reconcile_releases(self, state: dict[str, object]) -> dict[str, object]:
        """Restore the two-release bound after any interrupted retirement."""

        inventory = self._inventory_releases()
        order_before = self._verified_order(state)
        missing = [release_id for release_id in order_before if release_id not in inventory]
        if missing:
            raise ReleaseStateError("verified release directory is unavailable")

        removed = self._select_retained(state)
        if removed:
            # State ceases to reference retired releases before destructive cleanup.
            self._write_state(state)

        preserve = set(self._verified_order(state))
        for key in ("current", "previous", "trial", "in_progress"):
            item = state.get(key)
            if isinstance(item, str):
                preserve.add(item)
        orphans = sorted(release_id for release_id in inventory if release_id not in preserve)
        self._remove_releases(orphans)
        return state

    def _read_state(self) -> dict[str, object]:
        return self._reconcile_releases(self._read_state_file())

    def _write_state(self, state: dict[str, object]) -> None:
        self._cleanup_state_temps()
        state["schema_version"] = STATE_SCHEMA_VERSION
        state["updated_at"] = self.clock()
        _atomic_json(self.state_path, state)

    @staticmethod
    def _verified_order(state: Mapping[str, object]) -> list[str]:
        value = state.get("verified_order")
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ReleaseStateError("release state has an invalid verified history")
        return [str(item) for item in value]

    def status(self) -> dict[str, object]:
        with self._locked():
            state = self._read_state()
            return {
                "schema_version": state["schema_version"],
                "current": state.get("current"),
                "previous": state.get("previous"),
                "trial": state.get("trial"),
                "in_progress": state.get("in_progress"),
                "verified": self._verified_order(state),
            }

    def begin(self, release_id: str) -> Path:
        release_id = validate_release_id(release_id)
        with self._locked():
            state = self._read_state()
            if state.get("trial") is not None:
                raise ReleaseStateError("a trial release must be confirmed or rolled back first")
            active = state.get("in_progress")
            target = self.incoming / release_id
            if active == release_id and target.is_dir():
                return target
            if active is not None:
                raise ReleaseStateError("another release upload is in progress")
            if target.exists():
                # A power cut can leave an unreferenced incoming directory
                # between mkdir and the atomic state update. It has never been
                # verified, so it is safe to discard before restarting begin.
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                    _fsync_directory(self.incoming)
                else:
                    raise ReleaseStateError("release ID already exists")
            if (self.releases / release_id).exists():
                raise ReleaseStateError("release ID already exists")
            target.mkdir(mode=0o700)
            _fsync_directory(self.incoming)
            state["in_progress"] = release_id
            self._write_state(state)
            return target

    def stage(self, release_id: str, source: Path | None = None) -> Path:
        """Copy a bundle into the active incoming directory.

        Passing ``None`` finalizes an SFTP upload that was written directly to the
        directory returned by :meth:`begin`.
        """

        release_id = validate_release_id(release_id)
        with self._locked():
            state = self._read_state()
            target = self.incoming / release_id
            if state.get("in_progress") != release_id or not target.is_dir():
                raise ReleaseStateError("release upload has not been started")
            if source is None:
                if not any(target.iterdir()):
                    raise ReleaseStateError("release upload is empty")
                self._fsync_tree(target)
                return target
            source = Path(source)
            if not source.is_dir() or source.resolve() == target.resolve():
                raise ReleaseStateError("release source is invalid")
            if any(target.iterdir()):
                raise ReleaseStateError("incoming release is not empty")
            self._copy_tree(source, target)
            _fsync_directory(target)
            return target

    def _copy_tree(self, source: Path, target: Path) -> None:
        for root, directories, files in os.walk(source, followlinks=False):
            source_root = Path(root)
            relative_root = source_root.relative_to(source)
            target_root = target / relative_root
            for directory_name in directories:
                source_directory = source_root / directory_name
                if source_directory.is_symlink():
                    raise ReleaseVerificationError("release source contains a symbolic link")
                (target_root / directory_name).mkdir(mode=0o755, exist_ok=False)
            for file_name in files:
                source_file = source_root / file_name
                mode = source_file.lstat().st_mode
                if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                    raise ReleaseVerificationError("release source contains a non-regular file")
                destination = target_root / file_name
                destination_mode = 0o755 if mode & 0o111 else 0o644
                with source_file.open("rb") as reader, destination.open("xb") as writer:
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
                    if os.name != "nt":
                        fchmod = getattr(os, "fchmod", None)
                        if fchmod is None:  # pragma: no cover - required by field Linux
                            raise ReleaseStateError("file-descriptor chmod is unavailable")
                        fchmod(writer.fileno(), destination_mode)
                    writer.flush()
                    os.fsync(writer.fileno())
                if os.name == "nt":
                    os.chmod(destination, destination_mode)
        self._fsync_tree(target)

    @staticmethod
    def _fsync_tree(root: Path) -> None:
        directories: list[Path] = []
        for raw_root, child_directories, files in os.walk(root, followlinks=False):
            directory = Path(raw_root)
            directories.append(directory)
            for name in child_directories:
                child = directory / name
                if child.is_symlink() or not child.is_dir():
                    raise ReleaseVerificationError("release contains an unsafe directory")
            for name in files:
                path = directory / name
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                    raise ReleaseVerificationError("release contains a non-regular file")
                if os.name != "nt":
                    with path.open("rb") as stream:
                        os.fsync(stream.fileno())
        for directory in reversed(directories):
            _fsync_directory(directory)

    def remove_uploaded_archive(self, bundle: Path) -> bool:
        """Durably remove only an archive from an allowed upload root."""

        bundle = Path(bundle)
        with self._locked():
            allowed_parents = {
                self.incoming.resolve(),
                (self.root / "uploads").resolve(),
            }
            try:
                parent = bundle.parent.resolve()
            except OSError as exc:
                raise ReleaseStateError("uploaded archive parent is unavailable") from exc
            if parent not in allowed_parents:
                return False
            try:
                metadata = bundle.lstat()
            except FileNotFoundError:
                return False
            except OSError as exc:
                raise ReleaseStateError("uploaded archive is unavailable") from exc
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise ReleaseStateError("uploaded archive path is unsafe")
            bundle.unlink()
            _fsync_directory(parent)
            return True

    def _verify_directory(self, release_id: str, directory: Path) -> ReleaseManifest:
        manifest_path = directory / MANIFEST_NAME
        signature_path = directory / SIGNATURE_NAME
        for path, maximum in (
            (manifest_path, MAX_MANIFEST_BYTES),
            (signature_path, MAX_SIGNATURE_BYTES),
        ):
            try:
                mode = path.lstat().st_mode
                size = path.stat().st_size
            except OSError as exc:
                raise ReleaseVerificationError("release signature metadata is incomplete") from exc
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode) or not 0 < size <= maximum:
                raise ReleaseVerificationError("release signature metadata is invalid")
        if not self.verifier.verify(manifest_path, signature_path):
            raise ReleaseVerificationError("release signature is invalid")
        manifest = ReleaseManifest.load(manifest_path)
        if manifest.release_id != release_id:
            raise ReleaseVerificationError("release ID does not match its manifest")

        expected = {MANIFEST_NAME, SIGNATURE_NAME, *(item.path for item in manifest.files)}
        actual: set[str] = set()
        for root, directories, files in os.walk(directory, followlinks=False):
            root_path = Path(root)
            for name in directories:
                if (root_path / name).is_symlink():
                    raise ReleaseVerificationError("release contains a symbolic link")
            for name in files:
                path = root_path / name
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                    raise ReleaseVerificationError("release contains a non-regular file")
                actual.add(path.relative_to(directory).as_posix())
        if actual != expected:
            raise ReleaseVerificationError("release contains unlisted or missing files")

        directory_root = directory.resolve()
        for item in manifest.files:
            path = directory / PurePosixPath(item.path)
            try:
                resolved = path.resolve(strict=True)
            except OSError as exc:
                raise ReleaseVerificationError("release payload file is unavailable") from exc
            if not resolved.is_relative_to(directory_root):
                raise ReleaseVerificationError("release payload escapes its directory")
            size, digest = _sha256(path)
            if size != item.size or digest != item.sha256:
                raise ReleaseVerificationError("release payload hash or size is invalid")
        return manifest

    def verify(self, release_id: str) -> ReleaseManifest:
        release_id = validate_release_id(release_id)
        with self._locked():
            state = self._read_state()
            if state.get("in_progress") != release_id:
                raise ReleaseStateError("release is not the active upload")
            incoming = self.incoming / release_id
            destination = self.releases / release_id
            if destination.is_dir() and not incoming.exists():
                # Recovery for power loss after the atomic directory move but
                # before the verified state record was atomically replaced.
                manifest = self._verify_directory(release_id, destination)
                order = [item for item in self._verified_order(state) if item != release_id]
                order.append(release_id)
                state["verified_order"] = order
                state["in_progress"] = None
                removed = self._select_retained(state)
                self._write_state(state)
                self._remove_releases(removed)
                return manifest
            manifest = self._verify_directory(release_id, incoming)
            if destination.exists():
                raise ReleaseStateError("release ID already exists")
            os.replace(incoming, destination)
            _fsync_directory(self.incoming)
            _fsync_directory(self.releases)
            order = [item for item in self._verified_order(state) if item != release_id]
            order.append(release_id)
            state["verified_order"] = order
            state["in_progress"] = None
            removed = self._select_retained(state)
            self._write_state(state)
            self._remove_releases(removed)
            return manifest

    def validate_directory(self, release_id: str, directory: Path) -> ReleaseManifest:
        """Verify an unpacked source without changing updater state."""

        release_id = validate_release_id(release_id)
        with self._locked():
            return self._verify_directory(release_id, Path(directory))

    def abort(self, release_id: str | None = None) -> str:
        """Discard only the active, never-verified incoming upload."""

        with self._locked():
            state = self._read_state()
            active = state.get("in_progress")
            if not isinstance(active, str):
                raise ReleaseStateError("no release upload is in progress")
            if release_id is not None and validate_release_id(release_id) != active:
                raise ReleaseStateError("release is not the active upload")
            target = self.incoming / active
            if target.exists():
                if not target.is_dir() or target.is_symlink():
                    raise ReleaseStateError("incoming release path is unsafe")
                shutil.rmtree(target)
                _fsync_directory(self.incoming)
            state["in_progress"] = None
            self._write_state(state)
            return active

    def discard_verified(self, release_id: str) -> str:
        """Remove a verified release that is not part of the active rollback chain.

        This is intentionally narrower than rollback and is used by destructive
        update-interruption tests to restore their pre-test state. State stops
        referencing the release before its directory is removed, so a cut during
        cleanup leaves only an unreferenced directory that a retry can delete.
        """

        release_id = validate_release_id(release_id)
        with self._locked():
            state = self._read_state()
            if release_id in {
                state.get("current"),
                state.get("previous"),
                state.get("trial"),
                state.get("in_progress"),
            }:
                raise ReleaseStateError("active release state cannot be discarded")
            order = self._verified_order(state)
            target = self.releases / release_id
            if release_id in order:
                self._verify_directory(release_id, target)
                state["verified_order"] = [item for item in order if item != release_id]
                self._write_state(state)
            elif not target.exists():
                return release_id
            if target.exists():
                if not target.is_dir() or target.is_symlink():
                    raise ReleaseStateError("unreferenced release path is unsafe")
                shutil.rmtree(target)
                _fsync_directory(self.releases)
            return release_id

    def _select_retained(self, state: dict[str, object]) -> list[str]:
        order = self._verified_order(state)
        newest = order[-1] if order else None
        priority = [state.get("trial"), state.get("current"), newest, state.get("previous")]
        keep: list[str] = []
        for item in priority:
            if isinstance(item, str) and item in order and item not in keep:
                keep.append(item)
            if len(keep) == 2:
                break
        if len(keep) < 2:
            for item in reversed(order):
                if item not in keep:
                    keep.append(item)
                if len(keep) == 2:
                    break
        removed = [item for item in order if item not in keep]
        state["verified_order"] = [item for item in order if item in keep]
        for key in ("previous", "trial"):
            if state.get(key) in removed:
                state[key] = None
        return removed

    def _remove_releases(self, release_ids: Sequence[str]) -> None:
        changed = False
        for release_id in release_ids:
            path = self.releases / validate_release_id(release_id)
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ReleaseStateError("retired release cannot be inspected") from exc
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ReleaseStateError("retired release path is unsafe")
            try:
                shutil.rmtree(path)
            except OSError as exc:
                raise ReleaseStateError("retired release could not be removed") from exc
            if path.exists() or path.is_symlink():
                raise ReleaseStateError("retired release removal could not be proved")
            changed = True
        if changed:
            _fsync_directory(self.releases)

    def activate(self, release_id: str) -> Path:
        release_id = validate_release_id(release_id)
        with self._locked():
            state = self._read_state()
            if state.get("trial") is not None:
                raise ReleaseStateError("a trial release is already active")
            if release_id not in self._verified_order(state):
                raise ReleaseStateError("release has not been verified")
            directory = self.releases / release_id
            self._verify_directory(release_id, directory)
            if state.get("current") == release_id:
                raise ReleaseStateError("release is already current")
            state["previous"] = state.get("current")
            state["current"] = release_id
            state["trial"] = release_id
            removed = self._select_retained(state)
            self._write_state(state)
            self._remove_releases(removed)
            return directory

    def confirm(self, release_id: str | None = None) -> Path:
        with self._locked():
            state = self._read_state()
            trial = state.get("trial")
            if trial is None:
                raise ReleaseStateError("no trial release is active")
            if release_id is not None and validate_release_id(release_id) != trial:
                raise ReleaseStateError("release is not the active trial")
            state["trial"] = None
            removed = self._select_retained(state)
            self._write_state(state)
            self._remove_releases(removed)
            return self.releases / str(trial)

    def rollback(self) -> Path | None:
        with self._locked():
            state = self._read_state()
            previous = state.get("previous")
            trial = state.get("trial")
            if not isinstance(previous, str):
                # The first application release has the immutable image runtime as
                # its rollback target.  Only an active trial may roll back to that
                # built-in baseline; an ordinary rollback without history remains
                # an error.
                if not isinstance(trial, str):
                    raise ReleaseStateError("no previous release is available")
                state["current"] = None
                state["previous"] = None
                state["trial"] = None
                removed = self._select_retained(state)
                self._write_state(state)
                self._remove_releases(removed)
                return None
            if previous not in self._verified_order(state):
                raise ReleaseStateError("previous release is unavailable")
            self._verify_directory(previous, self.releases / previous)
            state["current"] = previous
            state["previous"] = None
            state["trial"] = None
            removed = self._select_retained(state)
            self._write_state(state)
            self._remove_releases(removed)
            return self.releases / previous

    def active_release(self) -> tuple[Path, ReleaseManifest]:
        """Return the active release only after re-verifying its signed contents."""

        with self._locked():
            state = self._read_state()
            current = state.get("current")
            if not isinstance(current, str):
                raise ReleaseStateError("no release is active")
            path = self.releases / current
            manifest = self._verify_directory(current, path)
            return path, manifest

    def current_path(self) -> Path:
        with self._locked():
            state = self._read_state()
            current = state.get("current")
            if not isinstance(current, str):
                raise ReleaseStateError("no release is active")
            path = self.releases / current
            if not path.is_dir():
                raise ReleaseStateError("active release directory is unavailable")
            return path


class ReleaseCommandService:
    """Thin command dispatcher so a CLI never edits updater state directly."""

    def __init__(self, updater: ReleaseUpdater) -> None:
        self.updater = updater

    def execute(
        self,
        command: str,
        *,
        release_id: str | None = None,
        source: Path | None = None,
    ) -> dict[str, object]:
        if command == "status":
            return self.updater.status()
        if command == "rollback":
            path = self.updater.rollback()
            return {"state": "rolled_back", "path": str(path) if path is not None else None}
        if command == "abort":
            aborted = self.updater.abort(release_id)
            return {"state": "aborted", "release_id": aborted}
        if command == "begin" and release_id is not None:
            path = self.updater.begin(release_id)
            return {"state": "uploading", "release_id": release_id, "path": str(path)}
        if command == "stage" and release_id is not None:
            path = self.updater.stage(release_id, source)
            return {"state": "staged", "release_id": release_id, "path": str(path)}
        if command == "verify" and release_id is not None:
            manifest = self.updater.verify(release_id)
            return {"state": "verified", "release_id": manifest.release_id}
        if command == "activate" and release_id is not None:
            path = self.updater.activate(release_id)
            return {"state": "trial", "release_id": release_id, "path": str(path)}
        if command == "confirm":
            path = self.updater.confirm(release_id)
            return {"state": "confirmed", "path": str(path)}
        raise ValueError("unsupported update command or missing release ID")


__all__ = [
    "MANIFEST_NAME",
    "SIGNATURE_NAME",
    "ManifestFile",
    "OpenSSLEd25519Verifier",
    "ReleaseCommandService",
    "ReleaseManifest",
    "ReleaseStateError",
    "ReleaseUpdater",
    "ReleaseVerificationError",
    "SignatureVerifier",
    "UpdateError",
    "validate_release_id",
]
