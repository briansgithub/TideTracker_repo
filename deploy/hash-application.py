#!/usr/bin/env python3
"""Validate and content-hash the immutable TideTracker application inputs."""

from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import stat
import sys

FIXED_FILES = (
    "pyproject.toml",
    "stations.csv",
    "Ubuntu-Regular.ttf",
    "Ubuntu-Medium.ttf",
    "Ubuntu-Bold.ttf",
    "sun_rise.png",
    "sun_set.png",
    "no_wifi.bmp",
    "plot_image.bmp",
    "e-ink_lib/waveshare_epd/__init__.py",
    "e-ink_lib/waveshare_epd/epdconfig.py",
    "e-ink_lib/waveshare_epd/epd7in5_V2.py",
    "lab/hil_orchestrator.py",
    "lab/hil_config.example.json",
    "lab/evaluate_promotion.py",
    "deploy/bin/tidetracker",
    "deploy/bin/tidetracker-hil",
    "deploy/bin/tidetracker-promote",
    "deploy/bin/tidetracker-update",
    "deploy/build-role-image.sh",
    "deploy/configure-controller-network.sh",
    "deploy/flash-sd.ps1",
    "deploy/hash-application.py",
    "deploy/image-lock.json",
    "deploy/libexec/tidetracker-failsafe",
    "deploy/libexec/tidetracker-firstboot",
    "deploy/libexec/tidetracker-controller-portal-matrix.py",
    "deploy/network/controller-usb-management.nmconnection",
    "deploy/network/controller-zero-portal.nmconnection",
    "deploy/network/zero-usb-firewall.nft",
    "deploy/network/zero-usb-management.nmconnection",
    "deploy/packages/common.txt",
    "deploy/packages/controller.txt",
    "deploy/packages/PACKAGE_BUNDLE.example.json",
    "deploy/packages/zero.txt",
    "deploy/prepare-base-image.ps1",
    "deploy/profiles/controller.json",
    "deploy/profiles/zero.json",
    "deploy/provision-profile.sh",
    "deploy/sbin/tidetracker-controller-wifi-control",
    "deploy/sbin/tidetracker-lab-control",
    "deploy/verify-package-bundle.py",
    "systemd/tidetracker-cycle.service",
    "systemd/tidetracker-controller-test-plane.service",
    "systemd/tidetracker-failsafe.service",
    "systemd/tidetracker-firstboot.service",
    "systemd/tidetracker-portal.service",
    "systemd/tidetracker-ssh-provisioned.conf",
    "systemd/tidetracker-usb-firewall.service",
)


class ApplicationError(ValueError):
    pass


def application_files(root: pathlib.Path) -> tuple[pathlib.Path, ...]:
    package = root / "src" / "tidetracker"
    if root.is_symlink() or not root.is_dir() or package.is_symlink() or not package.is_dir():
        raise ApplicationError("application root/package is missing or symlinked")
    relative = {path.relative_to(root) for path in package.rglob("*.py")}
    relative.update(pathlib.Path(value) for value in FIXED_FILES)
    values: list[pathlib.Path] = []
    for item in sorted(relative, key=lambda path: path.as_posix()):
        path = root / item
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ApplicationError(f"application input is missing: {item.as_posix()}") from exc
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            raise ApplicationError(f"application input is not a regular file: {item.as_posix()}")
        values.append(item)
    return tuple(values)


def application_hash(root: pathlib.Path, files: tuple[pathlib.Path, ...]) -> str:
    aggregate = hashlib.sha256()
    for item in files:
        path = root / item
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        current = path.stat(follow_symlinks=False)
        stable_fields = (
            ("st_size", "st_mtime_ns")
            if os.name == "nt"
            else ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        )
        if (
            any(getattr(before, field) != getattr(after, field) for field in stable_fields)
            or any(getattr(before, field) != getattr(current, field) for field in stable_fields)
            or size != before.st_size
        ):
            raise ApplicationError(f"application input changed while hashing: {item.as_posix()}")
        aggregate.update(item.as_posix().encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(str(size).encode("ascii"))
        aggregate.update(b"\0")
        aggregate.update(digest.hexdigest().encode("ascii"))
        aggregate.update(b"\n")
    return aggregate.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=pathlib.Path, required=True)
    parser.add_argument("--print-files", action="store_true")
    arguments = parser.parse_args()
    root = arguments.root.resolve()
    files = application_files(root)
    if arguments.print_files:
        for path in files:
            print(path.as_posix())
    else:
        print(application_hash(root, files))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ApplicationError, OSError, UnicodeError) as error:
        print(f"APPLICATION INPUT ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from None
