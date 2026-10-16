#!/usr/bin/env python3
"""Create a schema-2 TideTracker package bundle from two APT download caches."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Any

NAME = re.compile(r"[a-z0-9][a-z0-9+.-]*")
# Debian versions commonly put '~' in archive names and APT percent-escapes
# epochs.  Deliberately exclude separators and whitespace.
FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9+._%~-]*\.deb")


class LockError(ValueError):
    pass


@dataclass(frozen=True)
class PackageRecord:
    name: str
    version: str
    architecture: str
    filename: str
    size: int
    sha256: str


def requested_packages(paths: list[pathlib.Path]) -> list[str]:
    values: set[str] = set()
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise LockError(f"requested package list is not a regular file: {path}")
        for raw in path.read_text(encoding="utf-8").splitlines():
            name = raw.strip()
            if not name or name.startswith("#"):
                continue
            if NAME.fullmatch(name) is None:
                raise LockError(f"invalid requested package name in {path}: {name}")
            values.add(name)
    if not values:
        raise LockError("requested package lists are empty")
    return sorted(values)


def inspect_deb(path: pathlib.Path) -> PackageRecord:
    if path.is_symlink() or not path.is_file() or FILENAME.fullmatch(path.name) is None:
        raise LockError(f"invalid package file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size < 1:
            raise LockError(f"invalid package file: {path}")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        metadata = subprocess.run(
            [
                "dpkg-deb",
                "-f",
                f"/proc/self/fd/{stream.fileno()}",
                "Package",
                "Version",
                "Architecture",
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
            pass_fds=(stream.fileno(),),
        )
        after = os.fstat(stream.fileno())
    current = path.stat(follow_symlinks=False)
    fields = (
        ("st_size", "st_mtime_ns")
        if os.name == "nt"
        else ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    )
    if any(getattr(before, field) != getattr(after, field) for field in fields) or any(
        getattr(before, field) != getattr(current, field) for field in fields
    ):
        raise LockError(f"package changed while it was inspected: {path.name}")
    lines = metadata.stdout.splitlines()
    prefixes = ("Package: ", "Version: ", "Architecture: ")
    if (
        metadata.returncode != 0
        or len(lines) != 3
        or any(not line.startswith(prefix) for line, prefix in zip(lines, prefixes, strict=True))
    ):
        raise LockError(f"cannot read Debian control metadata: {path.name}")
    name, version, architecture = (
        line.removeprefix(prefix) for line, prefix in zip(lines, prefixes, strict=True)
    )
    if NAME.fullmatch(name) is None:
        raise LockError(f"package has an invalid name: {path.name}")
    if not version or "\x00" in version or "\n" in version:
        raise LockError(f"package has an invalid version: {path.name}")
    if architecture not in {"armhf", "all"}:
        raise LockError(f"package is not ARMHF or architecture-independent: {path.name}")
    return PackageRecord(
        name=name,
        version=version,
        architecture=architecture,
        filename=path.name,
        size=before.st_size,
        sha256=digest.hexdigest(),
    )


def scan_cache(path: pathlib.Path) -> tuple[dict[str, PackageRecord], dict[str, pathlib.Path]]:
    if path.is_symlink() or not path.is_dir():
        raise LockError(f"APT cache is not a non-symlink directory: {path}")
    records: dict[str, PackageRecord] = {}
    sources: dict[str, pathlib.Path] = {}
    filenames: set[str] = set()
    for child in sorted(path.glob("*.deb"), key=lambda value: value.name):
        record = inspect_deb(child)
        if record.name in records:
            raise LockError(f"APT cache contains multiple versions of package {record.name}")
        if record.filename in filenames:
            raise LockError(f"APT cache contains a duplicate filename: {record.filename}")
        records[record.name] = record
        sources[record.name] = child
        filenames.add(record.filename)
    if not records:
        raise LockError(f"APT cache contains no Debian packages: {path}")
    return records, sources


def merge_records(
    records_by_role: dict[str, dict[str, PackageRecord]],
    requested_by_role: dict[str, list[str]],
) -> tuple[dict[str, PackageRecord], dict[str, dict[str, list[str]]]]:
    packages: dict[str, PackageRecord] = {}
    filenames: dict[str, str] = {}
    roles: dict[str, dict[str, list[str]]] = {}
    for role in ("controller", "zero"):
        records = records_by_role[role]
        requested = sorted(requested_by_role[role])
        missing = sorted(set(requested) - set(records))
        if missing:
            raise LockError(
                f"{role} closure omits requested packages: {', '.join(missing)}"
            )
        for name, record in records.items():
            previous = packages.get(name)
            if previous is not None and previous != record:
                raise LockError(f"role closures select different artifacts for package {name}")
            filename_owner = filenames.get(record.filename)
            if filename_owner is not None and filename_owner != name:
                raise LockError(
                    f"role closures reuse filename {record.filename} for two packages"
                )
            packages[name] = record
            filenames[record.filename] = name
        roles[role] = {"requested": requested, "packages": sorted(records)}
    return packages, roles


def copy_stable(source: pathlib.Path, destination: pathlib.Path, expected: PackageRecord) -> None:
    temporary = destination.with_name(f".{destination.name}.partial")
    if destination.exists() or destination.is_symlink() or temporary.exists():
        raise LockError(f"refusing to replace package output: {destination.name}")
    digest = hashlib.sha256()
    try:
        with source.open("rb") as source_stream, temporary.open("xb") as output_stream:
            before = os.fstat(source_stream.fileno())
            size = 0
            for chunk in iter(lambda: source_stream.read(1024 * 1024), b""):
                output_stream.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
            after = os.fstat(source_stream.fileno())
        current = source.stat(follow_symlinks=False)
        fields = (
            ("st_size", "st_mtime_ns")
            if os.name == "nt"
            else ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        )
        if any(getattr(before, field) != getattr(after, field) for field in fields) or any(
            getattr(before, field) != getattr(current, field) for field in fields
        ):
            raise LockError(f"package changed while it was copied: {source.name}")
        if size != expected.size or digest.hexdigest() != expected.sha256:
            raise LockError(f"package bytes changed after inspection: {source.name}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def create_bundle(
    output: pathlib.Path,
    controller_cache: pathlib.Path,
    zero_cache: pathlib.Path,
    common_list: pathlib.Path,
    controller_list: pathlib.Path,
    zero_list: pathlib.Path,
) -> None:
    if output.is_symlink() or not output.is_dir() or any(output.iterdir()):
        raise LockError("output must be an empty, non-symlink directory")
    controller_records, controller_sources = scan_cache(controller_cache)
    zero_records, zero_sources = scan_cache(zero_cache)
    requested_by_role = {
        "controller": requested_packages([common_list, controller_list]),
        "zero": requested_packages([common_list, zero_list]),
    }
    packages, roles = merge_records(
        {"controller": controller_records, "zero": zero_records}, requested_by_role
    )
    sources = {**zero_sources, **controller_sources}
    debs = output / "debs"
    debs.mkdir(mode=0o755)
    for name in sorted(packages):
        record = packages[name]
        copy_stable(sources[name], debs / record.filename, record)
    debs_fd = os.open(debs, os.O_RDONLY)
    try:
        os.fsync(debs_fd)
    finally:
        os.close(debs_fd)
    lock: dict[str, Any] = {
        "schema_version": 2,
        "architecture": "armhf",
        "roles": roles,
        "packages": [asdict(packages[name]) for name in sorted(packages)],
    }
    lock_path = output / "packages.lock.json"
    temporary_lock = output / ".packages.lock.json.partial"
    payload = (json.dumps(lock, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        with temporary_lock.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_lock, lock_path)
        directory_fd = os.open(output, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary_lock.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--controller-cache", required=True, type=pathlib.Path)
    parser.add_argument("--zero-cache", required=True, type=pathlib.Path)
    parser.add_argument("--common-list", required=True, type=pathlib.Path)
    parser.add_argument("--controller-list", required=True, type=pathlib.Path)
    parser.add_argument("--zero-list", required=True, type=pathlib.Path)
    args = parser.parse_args()
    create_bundle(
        args.output.resolve(),
        args.controller_cache.resolve(),
        args.zero_cache.resolve(),
        args.common_list.resolve(),
        args.controller_list.resolve(),
        args.zero_list.resolve(),
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (LockError, OSError, subprocess.SubprocessError, UnicodeError) as error:
        print(f"PACKAGE LOCK ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from None
