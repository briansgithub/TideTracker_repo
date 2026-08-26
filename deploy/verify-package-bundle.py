#!/usr/bin/env python3
"""Validate a content-addressed offline Debian package closure."""

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
from typing import Any

NAME = re.compile(r"[a-z0-9][a-z0-9+.-]*")
SHA256 = re.compile(r"[0-9a-f]{64}")
FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9+._-]*\.deb")


class BundleError(ValueError):
    pass


def reject_duplicates(items: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in items:
        if key in value:
            raise BundleError(f"duplicate JSON field: {key}")
        value[key] = child
    return value


def requested_packages(paths: list[pathlib.Path]) -> list[str]:
    values: set[str] = set()
    for path in paths:
        for raw in path.read_text(encoding="utf-8").splitlines():
            name = raw.strip()
            if not name or name.startswith("#"):
                continue
            if NAME.fullmatch(name) is None:
                raise BundleError(f"invalid requested package name in {path}: {name}")
            values.add(name)
    return sorted(values)


def read_status(path: pathlib.Path) -> dict[str, tuple[str, str]]:
    installed: dict[str, tuple[str, str]] = {}
    for paragraph in path.read_text(encoding="utf-8", errors="strict").split("\n\n"):
        fields: dict[str, str] = {}
        for line in paragraph.splitlines():
            if ": " in line and not line.startswith((" ", "\t")):
                key, value = line.split(": ", 1)
                fields[key] = value
        if fields.get("Status") == "install ok installed" and "Package" in fields:
            installed[fields["Package"]] = (
                fields.get("Version", ""),
                fields.get("Architecture", ""),
            )
    return installed


def validate(
    bundle: pathlib.Path,
    role: str,
    package_lists: list[pathlib.Path],
) -> tuple[list[dict[str, Any]], str, pathlib.Path]:
    lock_path = bundle / "packages.lock.json"
    deb_dir = bundle / "debs"
    if bundle.is_symlink() or not bundle.is_dir():
        raise BundleError("package bundle must be a non-symlink directory")
    if lock_path.is_symlink() or not lock_path.is_file() or lock_path.stat().st_size > 1024 * 1024:
        raise BundleError("package lock must be a small regular non-symlink file")
    if deb_dir.is_symlink() or not deb_dir.is_dir():
        raise BundleError("package bundle debs directory is invalid")
    try:
        lock_bytes = lock_path.read_bytes()
        lock = json.loads(lock_bytes.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError("package lock is invalid JSON") from exc
    if not isinstance(lock, dict) or lock.get("schema_version") != 2:
        raise BundleError("package lock schema must be 2")
    if lock.get("architecture") != "armhf":
        raise BundleError("package lock architecture must be armhf")
    roles = lock.get("roles")
    entries = lock.get("packages")
    if not isinstance(roles, dict) or not isinstance(entries, list) or not entries:
        raise BundleError("package lock roles/packages are missing")
    if set(roles) != {"controller", "zero"}:
        raise BundleError("package lock must define exactly controller and zero roles")
    normalized_roles: dict[str, dict[str, list[str]]] = {}
    for role_name, raw_role in roles.items():
        if not isinstance(raw_role, dict) or set(raw_role) != {"requested", "packages"}:
            raise BundleError(f"package lock role '{role_name}' is invalid")
        requested = raw_role["requested"]
        closure = raw_role["packages"]
        if (
            not isinstance(requested, list)
            or not requested
            or not all(isinstance(name, str) and NAME.fullmatch(name) for name in requested)
            or len(requested) != len(set(requested))
            or not isinstance(closure, list)
            or not closure
            or not all(isinstance(name, str) and NAME.fullmatch(name) for name in closure)
            or len(closure) != len(set(closure))
            or not set(requested) <= set(closure)
        ):
            raise BundleError(f"package lock role '{role_name}' package closure is invalid")
        normalized_roles[role_name] = {
            "requested": sorted(requested),
            "packages": sorted(closure),
        }
    expected_requested = requested_packages(package_lists)
    locked_requested = normalized_roles[role]["requested"]
    if (
        locked_requested != expected_requested
    ):
        raise BundleError(f"package lock role '{role}' does not match repository package lists")

    names: set[str] = set()
    filenames: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "name",
            "version",
            "architecture",
            "filename",
            "size",
            "sha256",
        }:
            raise BundleError("package lock contains an invalid record")
        name = entry["name"]
        version = entry["version"]
        architecture = entry["architecture"]
        filename = entry["filename"]
        size = entry["size"]
        digest = entry["sha256"]
        if not isinstance(name, str) or NAME.fullmatch(name) is None or name in names:
            raise BundleError("package lock contains an invalid or duplicate package name")
        if not isinstance(version, str) or not version or "\x00" in version or "\n" in version:
            raise BundleError(f"package {name} has an invalid version")
        if architecture not in {"armhf", "all"}:
            raise BundleError(f"package {name} has an invalid architecture")
        if (
            not isinstance(filename, str)
            or FILENAME.fullmatch(filename) is None
            or filename in filenames
        ):
            raise BundleError(f"package {name} has an invalid or duplicate filename")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or not 1 <= size <= 512 * 1024 * 1024
        ):
            raise BundleError(f"package {name} has an invalid size")
        if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
            raise BundleError(f"package {name} has an invalid SHA-256")
        path = deb_dir / filename
        if path.is_symlink() or not path.is_file():
            raise BundleError(f"package file is missing or has the wrong size: {filename}")
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != size:
                raise BundleError(f"package file is missing or has the wrong size: {filename}")
            file_digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                file_digest.update(chunk)
            if file_digest.hexdigest() != digest:
                raise BundleError(f"package file hash mismatch: {filename}")
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
        stable_fields = (
            ("st_size", "st_mtime_ns")
            if os.name == "nt"
            else ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        )
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields) or any(
            getattr(before, field) != getattr(current, field) for field in stable_fields
        ):
            raise BundleError(f"package file changed while it was validated: {filename}")
        if metadata.returncode != 0 or metadata.stdout.splitlines() != [
            name,
            version,
            architecture,
        ]:
            raise BundleError(f"package control metadata does not match the lock: {filename}")
        names.add(name)
        filenames.add(filename)
        normalized.append(entry)
    selected_names = set(normalized_roles[role]["packages"])
    if not set(expected_requested).issubset(selected_names):
        raise BundleError("package closure omits a requested role package")
    declared_names = set().union(
        *(set(value["packages"]) for value in normalized_roles.values())
    )
    if declared_names != names:
        raise BundleError("global packages must equal the exact union of role closures")
    children = list(deb_dir.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in children):
        raise BundleError("debs directory may contain only regular non-symlink files")
    actual_debs = {path.name for path in children}
    if actual_debs != filenames:
        raise BundleError("debs directory contains unlisted or missing files")

    digest = hashlib.sha256()
    digest.update(lock_bytes)
    digest.update(b"\0")
    for entry in sorted(normalized, key=lambda item: item["filename"]):
        digest.update(entry["filename"].encode())
        digest.update(b"\0")
        digest.update(str(entry["size"]).encode())
        digest.update(b"\0")
        digest.update(entry["sha256"].encode())
        digest.update(b"\n")
    selected = [entry for entry in normalized if entry["name"] in selected_names]
    return selected, digest.hexdigest(), lock_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=pathlib.Path, required=True)
    parser.add_argument("--role", choices=("controller", "zero"), required=True)
    parser.add_argument("--package-list", type=pathlib.Path, action="append", required=True)
    parser.add_argument("--print-filenames", action="store_true")
    parser.add_argument("--verify-status", type=pathlib.Path)
    args = parser.parse_args()
    entries, digest, _ = validate(args.bundle.resolve(), args.role, args.package_list)
    if args.verify_status is not None:
        installed = read_status(args.verify_status)
        for entry in entries:
            if installed.get(entry["name"]) != (entry["version"], entry["architecture"]):
                raise BundleError(f"installed package does not match lock: {entry['name']}")
    if args.print_filenames:
        for entry in sorted(entries, key=lambda item: item["filename"]):
            print(entry["filename"])
    else:
        print(digest)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BundleError, OSError, subprocess.SubprocessError) as exc:
        print(f"PACKAGE BUNDLE ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
