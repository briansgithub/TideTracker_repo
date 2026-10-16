#!/usr/bin/python3
"""Constrained sudo entry point for real controller HIL execution."""

from __future__ import annotations

import os
import pwd
import sys
from collections.abc import Callable, Sequence

sys.path.insert(0, "/usr/local/lib/tidetracker/app")
sys.path.insert(0, "/usr/local/lib/tidetracker/lab")

import hil_orchestrator  # noqa: E402


class LauncherError(RuntimeError):
    pass


def validate_invocation(argv: Sequence[str]) -> None:
    if os.geteuid() != 0:
        raise LauncherError("real HIL launcher requires effective root")
    sudo_user = os.environ.get("SUDO_USER")
    sudo_uid = os.environ.get("SUDO_UID")
    if sudo_user != "tidetracker" or sudo_uid is None or not sudo_uid.isdecimal():
        raise LauncherError("real HIL launcher accepts only the tidetracker sudo caller")
    try:
        account = pwd.getpwnam("tidetracker")
    except KeyError as exc:
        raise LauncherError("tidetracker account is unavailable") from exc
    if int(sudo_uid) != account.pw_uid or account.pw_uid == 0:
        raise LauncherError("sudo caller UID does not match the tidetracker account")
    if argv.count("--execute") != 1 or "--simulate" in argv:
        raise LauncherError("root launcher accepts exactly one real --execute request")


def main(
    argv: list[str] | None = None,
    *,
    runner: Callable[[list[str]], int] = hil_orchestrator.main,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    validate_invocation(arguments)
    os.environ.clear()
    os.environ.update(
        {
            "HOME": "/root",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        }
    )
    return runner(arguments)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (LauncherError, hil_orchestrator.HilError) as exc:
        print(f"tidetracker-hil-root: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
