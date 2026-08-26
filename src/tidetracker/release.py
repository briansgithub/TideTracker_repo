"""Deterministic signed release bundles and traversal-safe extraction."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from .updater import (
    MANIFEST_NAME,
    MANIFEST_SCHEMA_VERSION,
    MAX_MANIFEST_BYTES,
    MAX_RELEASE_BYTES,
    MAX_SIGNATURE_BYTES,
    SIGNATURE_NAME,
    ReleaseManifest,
    ReleaseVerificationError,
    validate_release_id,
)

MAX_ARCHIVE_BYTES = 28 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 2_048
_NATIVE_SUFFIXES = frozenset({".dll", ".dylib", ".exe", ".o", ".obj", ".pyd", ".pyc", ".so"})
_NATIVE_MAGICS = (
    b"\x7fELF",
    b"MZ",
    b"\xca\xfe\xba\xbe",
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
)


class BundleError(RuntimeError):
    """A bundle could not be built or safely unpacked."""


def resolve_openssl(binary: str = "openssl") -> str:
    """Resolve OpenSSL, including the copy bundled with Git for Windows."""

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


class OpenSSLEd25519Signer:
    """Create a raw Ed25519 signature without loading private key bytes in Python."""

    def __init__(
        self,
        private_key: Path,
        *,
        binary: str = "openssl",
        runner: CommandRunner = _command_runner,
        timeout: float = 10.0,
    ) -> None:
        self.private_key = Path(private_key)
        self.binary = resolve_openssl(binary)
        self._runner = runner
        self.timeout = timeout

    def sign(self, manifest: Path, signature: Path) -> None:
        if not self.private_key.is_file():
            raise BundleError("release private key is unavailable")
        try:
            result = self._runner(
                [
                    self.binary,
                    "pkeyutl",
                    "-sign",
                    "-inkey",
                    str(self.private_key),
                    "-rawin",
                    "-in",
                    str(manifest),
                    "-out",
                    str(signature),
                ],
                timeout=self.timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BundleError("release manifest could not be signed") from exc
        if result.returncode != 0 or not signature.is_file():
            raise BundleError("release manifest could not be signed")
        try:
            signature_size = signature.stat().st_size
        except OSError as exc:
            raise BundleError("release signature is unavailable") from exc
        if not 0 < signature_size <= MAX_SIGNATURE_BYTES:
            raise BundleError("release signature has an invalid size")


class ManifestSigner(Protocol):
    def sign(self, manifest: Path, signature: Path) -> None: ...


@dataclass(frozen=True, slots=True)
class BuiltBundle:
    path: Path
    release_id: str
    sha256: str
    size: int


PROJECT_ENTRYPOINT = "tidetracker-release.py"
PROJECT_ASSETS = (
    "Ubuntu-Bold.ttf",
    "Ubuntu-Medium.ttf",
    "Ubuntu-Regular.ttf",
    "error_image.bmp",
    "no_wifi.bmp",
    "stations.csv",
    "sun_rise.png",
    "sun_set.png",
)


@dataclass(frozen=True, slots=True)
class _Payload:
    relative: str
    contents: bytes
    size: int
    sha256: str
    executable: bool


def _sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _safe_name(value: str) -> str:
    if not value or "\\" in value or "\x00" in value:
        raise BundleError("bundle contains an unsafe path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BundleError("bundle contains an unsafe path")
    return path.as_posix()


def _payloads(source: Path) -> tuple[_Payload, ...]:
    source_root = source.resolve()
    if not source_root.is_dir():
        raise BundleError("release source must be a directory")
    items: list[_Payload] = []
    total = 0
    for raw_root, directories, files in os.walk(source_root, followlinks=False):
        root = Path(raw_root)
        for name in directories:
            if (root / name).is_symlink():
                raise BundleError("release source contains a symbolic link")
        for name in files:
            path = root / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise BundleError("release source contains a non-regular file")
            relative = _safe_name(path.relative_to(source_root).as_posix())
            if relative in {MANIFEST_NAME, SIGNATURE_NAME}:
                raise BundleError("release source uses a reserved metadata path")
            if path.suffix.lower() in _NATIVE_SUFFIXES:
                raise BundleError("release payload must be pure Python and platform-independent")
            try:
                with path.open("rb") as source_stream:
                    opened = os.fstat(source_stream.fileno())
                    if not stat.S_ISREG(opened.st_mode):
                        raise BundleError("release source contains a non-regular file")
                    contents = source_stream.read(MAX_RELEASE_BYTES + 1)
                    after = os.fstat(source_stream.fileno())
                current = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise BundleError("release payload could not be snapshotted") from exc
            identity_fields = (
                ("st_size", "st_mtime_ns")
                if os.name == "nt"
                else ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            )
            if any(getattr(opened, field) != getattr(after, field) for field in identity_fields) or any(
                getattr(opened, field) != getattr(current, field) for field in identity_fields
            ):
                raise BundleError("release payload changed while it was snapshotted")
            if len(contents) > MAX_RELEASE_BYTES:
                raise BundleError("release exceeds the size limit")
            if any(contents[:4].startswith(magic) for magic in _NATIVE_MAGICS):
                raise BundleError("release payload must not contain native executables")
            size = len(contents)
            digest = hashlib.sha256(contents).hexdigest()
            total += size
            if total > MAX_RELEASE_BYTES:
                raise BundleError("release exceeds the size limit")
            items.append(_Payload(relative, contents, size, digest, bool(mode & 0o111)))
    if not items:
        raise BundleError("release source is empty")
    return tuple(sorted(items, key=lambda item: item.relative))


def _tar_info(name: str, size: int, *, executable: bool = False) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = 0o755 if executable else 0o644
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def assemble_project_payload(project_root: Path, destination: Path) -> Path:
    """Assemble only the ARMv6-portable application source and display assets."""

    project_root = Path(project_root).resolve()
    package_root = project_root / "src" / "tidetracker"
    if not package_root.is_dir() or package_root.is_symlink():
        raise BundleError("project does not contain src/tidetracker")
    destination = Path(destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    copied = 0
    for source in sorted(package_root.rglob("*.py")):
        mode = source.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise BundleError("project package contains an unsafe source file")
        relative = source.relative_to(project_root)
        target = destination / relative
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        os.chmod(target, 0o644)
        copied += 1
    if copied == 0:
        raise BundleError("project package contains no Python source")
    for name in PROJECT_ASSETS:
        source = project_root / name
        if not source.exists():
            continue
        mode = source.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise BundleError("project contains an unsafe display asset")
        shutil.copyfile(source, destination / name)
        os.chmod(destination / name, 0o644)
    if not (destination / "stations.csv").is_file():
        raise BundleError("project station catalogue is unavailable")
    launcher = destination / PROJECT_ENTRYPOINT
    launcher.write_text(
        "#!/usr/bin/env python3\n"
        "from tidetracker.cli import main\n"
        "raise SystemExit(main())\n",
        encoding="utf-8",
        newline="\n",
    )
    os.chmod(launcher, 0o755)
    return launcher


class DeterministicBundleBuilder:
    """Build a canonical uncompressed tar whose Ed25519 signature is reproducible."""

    def __init__(self, signer: ManifestSigner) -> None:
        self.signer = signer

    def build(
        self,
        source: Path,
        output: Path,
        *,
        release_id: str,
        entrypoint: str,
    ) -> BuiltBundle:
        release_id = validate_release_id(release_id)
        source = Path(source).resolve()
        output = Path(output).resolve()
        if output == source or output.is_relative_to(source):
            raise BundleError("bundle output must be outside the release source")
        payloads = _payloads(source)
        normalized_entrypoint = _safe_name(entrypoint)
        if normalized_entrypoint not in {item.relative for item in payloads}:
            raise BundleError("release entrypoint is not a payload file")

        manifest_value = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "release_id": release_id,
            "entrypoint": normalized_entrypoint,
            "files": [
                {"path": item.relative, "size": item.size, "sha256": item.sha256}
                for item in payloads
            ],
        }
        manifest_bytes = (
            json.dumps(manifest_value, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise BundleError("release manifest exceeds the size limit")

        output.parent.mkdir(parents=True, exist_ok=True)
        temporary_output = output.with_name(
            f".{output.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        if temporary_output.exists():
            raise BundleError("temporary bundle output already exists")
        try:
            with tempfile.TemporaryDirectory(prefix="tidetracker-manifest-") as raw_temp:
                temp = Path(raw_temp)
                manifest = temp / MANIFEST_NAME
                signature = temp / SIGNATURE_NAME
                manifest.write_bytes(manifest_bytes)
                self.signer.sign(manifest, signature)
                metadata = (
                    (MANIFEST_NAME, manifest, False),
                    (SIGNATURE_NAME, signature, False),
                )
                with temporary_output.open("xb") as raw_archive:
                    with tarfile.open(
                        fileobj=raw_archive, mode="w", format=tarfile.PAX_FORMAT
                    ) as archive:
                        for name, path, executable in sorted(metadata, key=lambda item: item[0]):
                            with path.open("rb") as contents:
                                archive.addfile(
                                    _tar_info(name, path.stat().st_size, executable=executable),
                                    contents,
                                )
                        for item in payloads:
                            archive.addfile(
                                _tar_info(
                                    item.relative,
                                    item.size,
                                    executable=(
                                        item.executable
                                        or item.relative == normalized_entrypoint
                                    ),
                                ),
                                io.BytesIO(item.contents),
                            )
                    raw_archive.flush()
                    os.fsync(raw_archive.fileno())
            os.replace(temporary_output, output)
            _fsync_directory(output.parent)
        except (OSError, tarfile.TarError) as exc:
            raise BundleError("release bundle could not be written") from exc
        finally:
            with suppress(FileNotFoundError):
                temporary_output.unlink()
        size, digest = _sha256(output)
        if size > MAX_ARCHIVE_BYTES:
            with suppress(FileNotFoundError):
                output.unlink()
            raise BundleError("release archive exceeds the size limit")
        return BuiltBundle(output, release_id, digest, size)


def extract_bundle(bundle: Path, destination: Path) -> ReleaseManifest:
    """Extract regular files from a bundle without trusting tar path handling."""

    bundle = Path(bundle)
    destination = Path(destination)
    try:
        initial = bundle.lstat()
    except OSError as exc:
        raise BundleError("release bundle is unavailable") from exc
    if (
        stat.S_ISLNK(initial.st_mode)
        or not stat.S_ISREG(initial.st_mode)
        or not 0 < initial.st_size <= MAX_ARCHIVE_BYTES
    ):
        raise BundleError("release bundle must be a bounded regular file")
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    destination_root = destination.resolve()
    seen: set[str] = set()
    total = 0
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(bundle, flags)
        with os.fdopen(descriptor, "rb") as bundle_stream:
            before = os.fstat(bundle_stream.fileno())
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_ARCHIVE_BYTES:
                raise BundleError("release bundle must be a bounded regular file")
            with tarfile.open(fileobj=bundle_stream, mode="r:") as archive:
                member_count = 0
                for member in archive:
                    member_count += 1
                    if member_count > MAX_ARCHIVE_MEMBERS:
                        raise BundleError("release bundle has too many members")
                    normalized = _safe_name(
                        member.name.rstrip("/") if member.isdir() else member.name
                    )
                    if normalized in seen:
                        raise BundleError("release bundle contains duplicate paths")
                    seen.add(normalized)
                    target = (destination / PurePosixPath(normalized)).resolve()
                    if not target.is_relative_to(destination_root):
                        raise BundleError("release bundle path escapes its destination")
                    if member.isdir():
                        target.mkdir(mode=0o755, parents=True, exist_ok=False)
                        continue
                    if not member.isreg():
                        raise BundleError("release bundle contains a non-regular file")
                    total += member.size
                    if total > MAX_RELEASE_BYTES + MAX_MANIFEST_BYTES + MAX_SIGNATURE_BYTES:
                        raise BundleError("release bundle exceeds the extraction size limit")
                    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise BundleError("release bundle member is unavailable")
                    with source, target.open("xb") as output:
                        remaining = member.size
                        while remaining:
                            chunk = source.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise BundleError("release bundle member is truncated")
                            output.write(chunk)
                            remaining -= len(chunk)
                        if source.read(1):
                            raise BundleError("release bundle member exceeds its declared size")
                        output.flush()
                        os.fsync(output.fileno())
                    os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)
                if member_count == 0:
                    raise BundleError("release bundle is empty")
            after = os.fstat(bundle_stream.fileno())
        current = bundle.stat(follow_symlinks=False)
        identity_fields = (
            ("st_mode", "st_size", "st_mtime_ns")
            if os.name == "nt"
            else ("st_mode", "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        )
        if (
            any(getattr(initial, field) != getattr(before, field) for field in identity_fields)
            or any(getattr(before, field) != getattr(after, field) for field in identity_fields)
            or any(getattr(before, field) != getattr(current, field) for field in identity_fields)
        ):
            raise BundleError("release bundle changed during extraction")
        _fsync_directory(destination)
        return ReleaseManifest.load(destination / MANIFEST_NAME)
    except (OSError, tarfile.TarError, ReleaseVerificationError, BundleError) as exc:
        with suppress(OSError):
            shutil.rmtree(destination)
        if isinstance(exc, BundleError):
            raise
        raise BundleError("release bundle is invalid") from exc


__all__ = [
    "BuiltBundle",
    "BundleError",
    "DeterministicBundleBuilder",
    "MAX_ARCHIVE_BYTES",
    "ManifestSigner",
    "OpenSSLEd25519Signer",
    "PROJECT_ENTRYPOINT",
    "assemble_project_payload",
    "extract_bundle",
    "resolve_openssl",
]
