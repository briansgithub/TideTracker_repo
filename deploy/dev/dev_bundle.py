#!/usr/bin/env python3
"""Build, validate, activate, and inspect unsigned TideTracker dev bundles.

This is intentionally separate from the signed field-release path.  It is for a
single developer-controlled Pi Zero W reached over a private USB link.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

MANIFEST_NAME = "DEV_RELEASE.json"
SCHEMA_VERSION = 1
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_EXTRACTED_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 1024
MAX_MANIFEST_BYTES = 1024 * 1024
RELEASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SHA256 = re.compile(r"[0-9a-f]{64}")
FIXED_ASSETS = (
    "Ubuntu-Bold.ttf",
    "Ubuntu-Medium.ttf",
    "Ubuntu-Regular.ttf",
    "error_image.bmp",
    "no_wifi.bmp",
    "stations.csv",
    "sun_rise.png",
    "sun_set.png",
)


class DevBundleError(ValueError):
    """A development bundle or activation request is invalid."""


def _reject_duplicate_json(items: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in items:
        if key in value:
            raise DevBundleError(f"duplicate JSON field: {key}")
        value[key] = child
    return value


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_stream(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _validate_release_id(value: str) -> str:
    if RELEASE_ID.fullmatch(value) is None:
        raise DevBundleError("release ID must contain only letters, digits, dot, underscore, or dash")
    return value


def _validate_member_name(value: str) -> str:
    if not value or "\\" in value or "\x00" in value:
        raise DevBundleError(f"unsafe bundle path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise DevBundleError(f"unsafe bundle path: {value!r}")
    normalized = path.as_posix()
    if normalized != value:
        raise DevBundleError(f"non-canonical bundle path: {value!r}")
    return normalized


def _is_allowed_member(name: str) -> bool:
    if name == MANIFEST_NAME:
        return True
    if name.startswith("src/tidetracker/") and name.endswith(".py"):
        return True
    if name.startswith("e-ink_lib/waveshare_epd/") and name.endswith(".py"):
        return True
    return name.startswith("assets/") and name.removeprefix("assets/") in FIXED_ASSETS


def _stable_file_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise DevBundleError(f"source must be a regular non-symlink file: {path}")
    before = path.stat(follow_symlinks=False)
    payload = path.read_bytes()
    after = path.stat(follow_symlinks=False)
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns") if os.name != "nt" else (
        "st_size",
        "st_mtime_ns",
    )
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        raise DevBundleError(f"source changed while it was read: {path}")
    return payload


def collect_source_files(source_root: Path) -> dict[str, bytes]:
    """Return the exact minimal runtime payload keyed by archive path."""

    source_root = source_root.resolve(strict=True)
    package_root = source_root / "src" / "tidetracker"
    driver_root = source_root / "e-ink_lib" / "waveshare_epd"
    if not package_root.is_dir() or not driver_root.is_dir():
        raise DevBundleError("source root lacks src/tidetracker or e-ink_lib/waveshare_epd")

    selected: list[tuple[Path, str]] = []
    for path in sorted(package_root.rglob("*.py")):
        selected.append((path, path.relative_to(source_root).as_posix()))
    for path in sorted(driver_root.rglob("*.py")):
        selected.append((path, path.relative_to(source_root).as_posix()))
    for filename in FIXED_ASSETS:
        selected.append((source_root / filename, f"assets/{filename}"))

    payloads: dict[str, bytes] = {}
    for path, archive_name in selected:
        name = _validate_member_name(archive_name)
        if not _is_allowed_member(name) or name in payloads:
            raise DevBundleError(f"invalid or duplicate selected path: {name}")
        payloads[name] = _stable_file_bytes(path)
    if not payloads:
        raise DevBundleError("development bundle would be empty")
    return dict(sorted(payloads.items()))


def _file_records(payloads: dict[str, bytes]) -> list[dict[str, object]]:
    return [
        {"path": name, "size": len(payload), "sha256": _sha256_bytes(payload)}
        for name, payload in sorted(payloads.items())
    ]


def build_bundle(source_root: Path, output: Path, release_id: str | None = None) -> dict[str, object]:
    """Create a normalized, uncompressed tar archive without overwriting output."""

    payloads = collect_source_files(source_root)
    records = _file_records(payloads)
    content_sha256 = _sha256_bytes(_canonical_json(records))
    selected_release_id = _validate_release_id(release_id or f"dev-{content_sha256[:16]}")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "release_id": selected_release_id,
        "content_sha256": content_sha256,
        "files": records,
    }
    manifest_payload = _canonical_json(manifest) + b"\n"
    output = output.resolve(strict=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(output, flags, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as stream:
            with tarfile.open(fileobj=stream, mode="w", format=tarfile.GNU_FORMAT) as archive:
                for name, payload in [(MANIFEST_NAME, manifest_payload), *payloads.items()]:
                    member = tarfile.TarInfo(name)
                    member.size = len(payload)
                    member.mode = 0o644
                    member.uid = 0
                    member.gid = 0
                    member.uname = ""
                    member.gname = ""
                    member.mtime = 0
                    archive.addfile(member, io.BytesIO(payload))
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise
    archive_sha256 = _sha256_bytes(output.read_bytes())
    return {
        "schema_version": SCHEMA_VERSION,
        "release_id": selected_release_id,
        "content_sha256": content_sha256,
        "archive_sha256": archive_sha256,
        "archive": str(output),
        "size": output.stat().st_size,
    }


def _parse_manifest(payload: bytes) -> dict[str, Any]:
    if len(payload) > MAX_MANIFEST_BYTES:
        raise DevBundleError("development manifest is too large")
    try:
        manifest = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_json)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DevBundleError("development manifest is invalid JSON") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "release_id",
        "content_sha256",
        "files",
    }:
        raise DevBundleError("development manifest fields are invalid")
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise DevBundleError("unsupported development manifest schema")
    _validate_release_id(manifest["release_id"] if isinstance(manifest["release_id"], str) else "")
    if not isinstance(manifest["content_sha256"], str) or SHA256.fullmatch(
        manifest["content_sha256"]
    ) is None:
        raise DevBundleError("manifest content hash is invalid")
    files = manifest["files"]
    if not isinstance(files, list) or not files or len(files) > MAX_MEMBERS - 1:
        raise DevBundleError("manifest file list is invalid")
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for record in files:
        if not isinstance(record, dict) or set(record) != {"path", "size", "sha256"}:
            raise DevBundleError("manifest contains an invalid file record")
        name = _validate_member_name(record["path"] if isinstance(record["path"], str) else "")
        size = record["size"]
        digest = record["sha256"]
        if not _is_allowed_member(name) or name == MANIFEST_NAME or name in seen:
            raise DevBundleError(f"manifest contains an invalid or duplicate path: {name}")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= MAX_EXTRACTED_BYTES:
            raise DevBundleError(f"manifest size is invalid: {name}")
        if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
            raise DevBundleError(f"manifest SHA-256 is invalid: {name}")
        seen.add(name)
        normalized.append({"path": name, "size": size, "sha256": digest})
    if normalized != sorted(normalized, key=lambda item: str(item["path"])):
        raise DevBundleError("manifest file records must be sorted")
    if sum(int(item["size"]) for item in normalized) > MAX_EXTRACTED_BYTES:
        raise DevBundleError("development bundle expands beyond its size limit")
    if _sha256_bytes(_canonical_json(normalized)) != manifest["content_sha256"]:
        raise DevBundleError("manifest content hash does not match its file list")
    manifest["files"] = normalized
    return manifest


def _open_archive(archive_path: Path, incoming_root: Path | None) -> BinaryIO:
    if incoming_root is not None:
        incoming = incoming_root.resolve(strict=True)
        if incoming.is_symlink() or not incoming.is_dir():
            raise DevBundleError("incoming root must be a non-symlink directory")
        if archive_path.parent.resolve(strict=True) != incoming:
            raise DevBundleError("archive must be a direct child of the incoming directory")
    if archive_path.is_symlink():
        raise DevBundleError("archive must not be a symbolic link")
    stream = archive_path.open("rb")
    metadata = os.fstat(stream.fileno())
    if not stat.S_ISREG(metadata.st_mode) or not 1 <= metadata.st_size <= MAX_ARCHIVE_BYTES:
        stream.close()
        raise DevBundleError("archive must be a bounded regular file")
    return stream


def _exclusive_file(path: Path) -> BinaryIO:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.fdopen(os.open(path, flags, 0o644), "wb")


def extract_bundle(
    archive_path: Path,
    destination: Path,
    *,
    expected_sha256: str | None = None,
    expected_release_id: str | None = None,
    incoming_root: Path | None = None,
) -> dict[str, Any]:
    """Validate an archive and manually extract only its allow-listed files."""

    if expected_sha256 is not None and SHA256.fullmatch(expected_sha256) is None:
        raise DevBundleError("expected archive SHA-256 is invalid")
    if expected_release_id is not None:
        _validate_release_id(expected_release_id)
    if destination.exists() or destination.is_symlink():
        raise DevBundleError("extraction destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)

    stream = _open_archive(archive_path, incoming_root)
    before = os.fstat(stream.fileno())
    try:
        actual_sha256 = _sha256_stream(stream)
        if expected_sha256 is not None and actual_sha256 != expected_sha256:
            raise DevBundleError("archive SHA-256 mismatch")
        stream.seek(0)
        try:
            with tarfile.open(fileobj=stream, mode="r:") as archive:
                members = archive.getmembers()
                if not members or len(members) > MAX_MEMBERS:
                    raise DevBundleError("archive member count is invalid")
                by_name: dict[str, tarfile.TarInfo] = {}
                for member in members:
                    name = _validate_member_name(member.name)
                    if name in by_name:
                        raise DevBundleError(f"archive contains a duplicate path: {name}")
                    if not member.isreg() or not _is_allowed_member(name):
                        raise DevBundleError(
                            f"archive contains a non-regular or disallowed member: {name}"
                        )
                    if member.size < 0 or member.size > MAX_EXTRACTED_BYTES:
                        raise DevBundleError(f"archive member size is invalid: {name}")
                    by_name[name] = member
                manifest_member = by_name.get(MANIFEST_NAME)
                if manifest_member is None:
                    raise DevBundleError("archive lacks its development manifest")
                manifest_stream = archive.extractfile(manifest_member)
                if manifest_stream is None:
                    raise DevBundleError("development manifest could not be read")
                manifest_payload = manifest_stream.read(MAX_MANIFEST_BYTES + 1)
                manifest = _parse_manifest(manifest_payload)
                if (
                    expected_release_id is not None
                    and manifest["release_id"] != expected_release_id
                ):
                    raise DevBundleError("archive release ID does not match the requested release")
                declared = {str(record["path"]) for record in manifest["files"]}
                if set(by_name) != declared | {MANIFEST_NAME}:
                    raise DevBundleError("archive members do not exactly match the manifest")

                destination.mkdir(mode=0o755)
                for record in manifest["files"]:
                    name = str(record["path"])
                    target = destination.joinpath(*PurePosixPath(name).parts)
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                    source = archive.extractfile(by_name[name])
                    if source is None:
                        raise DevBundleError(f"archive member could not be read: {name}")
                    digest = hashlib.sha256()
                    written = 0
                    with _exclusive_file(target) as output:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > int(record["size"]):
                                raise DevBundleError(
                                    f"archive member exceeds its declared size: {name}"
                                )
                            digest.update(chunk)
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    if written != record["size"] or digest.hexdigest() != record["sha256"]:
                        raise DevBundleError(f"archive member does not match its manifest: {name}")
                with _exclusive_file(destination / MANIFEST_NAME) as output:
                    output.write(manifest_payload)
                    output.flush()
                    os.fsync(output.fileno())
        except tarfile.TarError as exc:
            raise DevBundleError("archive is not a valid uncompressed tar file") from exc
    except BaseException:
        if destination.exists() and not destination.is_symlink():
            shutil.rmtree(destination)
        raise
    finally:
        after = os.fstat(stream.fileno())
        stream.close()
        stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if os.name == "nt":
            stable = ("st_size", "st_mtime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable):
            if destination.exists() and not destination.is_symlink():
                shutil.rmtree(destination)
            raise DevBundleError("archive changed while it was validated")
    return manifest


def verify_release_directory(path: Path, manifest: dict[str, Any]) -> None:
    if path.is_symlink() or not path.is_dir():
        raise DevBundleError("existing release is not a regular directory")
    expected = {str(record["path"]): record for record in manifest["files"]}
    actual: set[str] = set()
    for child in path.rglob("*"):
        if child.is_symlink():
            raise DevBundleError("existing release contains a symbolic link")
        if child.is_file():
            actual.add(child.relative_to(path).as_posix())
    if actual != set(expected) | {MANIFEST_NAME}:
        raise DevBundleError("existing release contents do not match the bundle")
    for name, record in expected.items():
        payload = _stable_file_bytes(path.joinpath(*PurePosixPath(name).parts))
        if len(payload) != record["size"] or _sha256_bytes(payload) != record["sha256"]:
            raise DevBundleError(f"existing release file does not match the bundle: {name}")


def _run_candidate(release: Path, state_root: Path, timeout_seconds: float) -> None:
    candidate_root = state_root / f".candidate-{secrets.token_hex(8)}"
    candidate_root.mkdir(parents=True, mode=0o700)
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": str(release / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "TIDETRACKER_ASSET_DIR": str(release / "assets"),
            "TIDETRACKER_STATIONS": str(release / "assets" / "stations.csv"),
            "TIDETRACKER_EINK_LIB": str(release / "e-ink_lib"),
        }
    )
    command = [
        sys.executable,
        "-c",
        "import sys; from tidetracker.cli import main; raise SystemExit(main(sys.argv[1:]))",
        "cycle",
        "--simulate",
        "--builtin",
        "--mode",
        "run",
        "--data-dir",
        str(candidate_root),
        "--asset-dir",
        str(release / "assets"),
        "--display-output",
        str(candidate_root / "simulated-framebuffer.bin"),
    ]
    try:
        result = subprocess.run(
            command,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
        evidence = candidate_root / "state" / "cycle" / "last_result.json"
        framebuffer = candidate_root / "simulated-framebuffer.bin"
        if result.returncode != 0 or not evidence.is_file() or not framebuffer.is_file():
            detail = " ".join((result.stderr or result.stdout).split())[:800]
            raise DevBundleError(f"candidate simulated cycle failed: {detail}")
        if framebuffer.stat().st_size != 48_000:
            raise DevBundleError("candidate simulated cycle produced the wrong framebuffer size")
        candidate_result = json.loads(evidence.read_text(encoding="utf-8"))
        if not isinstance(candidate_result, dict) or candidate_result.get("success") is not True:
            raise DevBundleError("candidate simulated cycle did not produce success evidence")
    except subprocess.TimeoutExpired as exc:
        raise DevBundleError("candidate simulated cycle timed out") from exc
    finally:
        shutil.rmtree(candidate_root, ignore_errors=True)


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    payload = _canonical_json(value) + b"\n"
    try:
        with _exclusive_file(temporary) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _current_release(current: Path, releases_root: Path) -> str | None:
    if not current.exists() and not current.is_symlink():
        return None
    if not current.is_symlink():
        raise DevBundleError("current release pointer is not a symbolic link")
    target = os.readlink(current)
    candidate = (current.parent / target).resolve(strict=True)
    root = releases_root.resolve(strict=True)
    if candidate.parent != root or RELEASE_ID.fullmatch(candidate.name) is None:
        raise DevBundleError("current release pointer escapes the release root")
    return candidate.name


def _set_current(current: Path, releases_root: Path, release_id: str | None) -> None:
    current.parent.mkdir(parents=True, exist_ok=True)
    temporary = current.parent / f".current-{secrets.token_hex(8)}"
    if release_id is None:
        current.unlink(missing_ok=True)
    else:
        target = releases_root / _validate_release_id(release_id)
        if target.is_symlink() or not target.is_dir():
            raise DevBundleError("release target is missing or unsafe")
        os.symlink(Path("releases") / release_id, temporary)
        os.replace(temporary, current)
    temporary.unlink(missing_ok=True)
    _sync_directory(current.parent)


def _restart_service(service: str) -> None:
    if not service or any(char.isspace() for char in service) or "/" in service:
        raise DevBundleError("service name is invalid")
    restart = subprocess.run(
        ["/usr/bin/systemctl", "restart", service],
        text=True,
        capture_output=True,
        check=False,
        timeout=240,
    )
    active = subprocess.run(
        ["/usr/bin/systemctl", "is-active", "--quiet", service],
        check=False,
        timeout=30,
    )
    if restart.returncode != 0 or active.returncode != 0:
        detail = " ".join((restart.stderr or restart.stdout).split())[:800]
        raise DevBundleError(f"service failed after activation: {detail}")


def activate_bundle(
    archive: Path,
    expected_sha256: str,
    expected_release_id: str,
    *,
    install_root: Path,
    state_root: Path,
    incoming_root: Path,
    service: str = "tidetracker-dev.service",
    timeout_seconds: float = 180.0,
    restart: bool = True,
    remove_archive: bool = False,
) -> dict[str, object]:
    """Validate, smoke-test, atomically activate, and roll back on boot failure."""

    expected_release_id = _validate_release_id(expected_release_id)
    if SHA256.fullmatch(expected_sha256) is None:
        raise DevBundleError("expected archive SHA-256 is invalid")
    install_root.mkdir(parents=True, exist_ok=True, mode=0o755)
    releases_root = install_root / "releases"
    releases_root.mkdir(parents=True, exist_ok=True, mode=0o755)
    state_root.mkdir(parents=True, exist_ok=True, mode=0o755)
    current = install_root / "current"
    previous = _current_release(current, releases_root)

    partial = releases_root / f".partial-{expected_release_id}-{secrets.token_hex(8)}"
    final = releases_root / expected_release_id
    manifest: dict[str, Any]
    created_release = False
    try:
        if final.exists() or final.is_symlink():
            if final.is_symlink() or not final.is_dir():
                raise DevBundleError("release destination already exists and is unsafe")
            probe = releases_root / f".probe-{expected_release_id}-{secrets.token_hex(8)}"
            try:
                manifest = extract_bundle(
                    archive,
                    probe,
                    expected_sha256=expected_sha256,
                    expected_release_id=expected_release_id,
                    incoming_root=incoming_root,
                )
                verify_release_directory(final, manifest)
            finally:
                if probe.exists() and not probe.is_symlink():
                    shutil.rmtree(probe)
        else:
            manifest = extract_bundle(
                archive,
                partial,
                expected_sha256=expected_sha256,
                expected_release_id=expected_release_id,
                incoming_root=incoming_root,
            )
            _run_candidate(partial, state_root, timeout_seconds)
            os.rename(partial, final)
            _sync_directory(releases_root)
            created_release = True
        if not created_release:
            _run_candidate(final, state_root, timeout_seconds)

        _set_current(current, releases_root, expected_release_id)
        try:
            if restart:
                _restart_service(service)
            record = {
                "schema_version": SCHEMA_VERSION,
                "release_id": expected_release_id,
                "previous_release_id": previous,
                "archive_sha256": expected_sha256,
                "content_sha256": manifest["content_sha256"],
                "service_restarted": restart,
            }
            _atomic_json(state_root / "active-release.json", record)
        except BaseException:
            _set_current(current, releases_root, previous)
            if restart and previous is not None:
                _restart_service(service)
            raise
        if remove_archive:
            archive.unlink()
            _sync_directory(incoming_root)
        return record
    finally:
        if partial.exists() and not partial.is_symlink():
            shutil.rmtree(partial)


def status_record(install_root: Path, state_root: Path) -> dict[str, object]:
    releases_root = install_root / "releases"
    current: str | None = None
    if releases_root.is_dir():
        current = _current_release(install_root / "current", releases_root)

    def read_json(path: Path) -> object | None:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None

    return {
        "schema_version": SCHEMA_VERSION,
        "current_release_id": current,
        "active_release": read_json(state_root / "active-release.json"),
        "last_cycle": read_json(state_root / "state" / "cycle" / "last_result.json"),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    pack = commands.add_parser("pack", help="create a deterministic development bundle")
    pack.add_argument("--source", type=Path, required=True)
    pack.add_argument("--output", type=Path, required=True)
    pack.add_argument("--release-id")

    activate = commands.add_parser("activate", help="validate and activate an uploaded bundle")
    activate.add_argument("archive", type=Path)
    activate.add_argument("sha256")
    activate.add_argument("release_id")
    activate.add_argument("--install-root", type=Path, default=Path("/opt/tidetracker-dev"))
    activate.add_argument("--state-root", type=Path, default=Path("/var/lib/tidetracker-dev"))
    activate.add_argument(
        "--incoming-root", type=Path, default=Path("/var/lib/tidetracker-dev/incoming")
    )
    activate.add_argument("--service", default="tidetracker-dev.service")
    activate.add_argument("--timeout", type=float, default=180.0)
    activate.add_argument("--no-restart", action="store_true")
    activate.add_argument("--remove-archive", action="store_true")

    status = commands.add_parser("status", help="print current release and last-cycle state")
    status.add_argument("--install-root", type=Path, default=Path("/opt/tidetracker-dev"))
    status.add_argument("--state-root", type=Path, default=Path("/var/lib/tidetracker-dev"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "pack":
            result = build_bundle(args.source, args.output, args.release_id)
        elif args.command == "activate":
            if hasattr(os, "geteuid") and os.geteuid() != 0:
                raise DevBundleError("activation must run as root")
            if not 1 <= args.timeout <= 600:
                raise DevBundleError("candidate timeout must be between 1 and 600 seconds")
            result = activate_bundle(
                args.archive,
                args.sha256,
                args.release_id,
                install_root=args.install_root,
                state_root=args.state_root,
                incoming_root=args.incoming_root,
                service=args.service,
                timeout_seconds=args.timeout,
                restart=not args.no_restart,
                remove_archive=args.remove_archive,
            )
        else:
            result = status_record(args.install_root, args.state_root)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except (DevBundleError, OSError, subprocess.SubprocessError, tarfile.TarError) as exc:
        print(
            json.dumps(
                {"error": type(exc).__name__, "message": " ".join(str(exc).split())[:1000]},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
