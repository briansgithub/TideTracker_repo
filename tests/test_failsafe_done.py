from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
FAILSAFE = PROJECT_ROOT / "deploy" / "libexec" / "tidetracker-failsafe"


def _production_shell() -> Path | None:
    if os.name == "posix":
        return Path("/bin/sh")
    git = shutil.which("git")
    if git is None:
        return None
    candidate = Path(git).resolve().parents[1] / "usr" / "bin" / "sh.exe"
    return candidate if candidate.is_file() else None


PRODUCTION_SHELL = _production_shell()


def _shell_path(path: Path) -> str:
    if os.name != "nt":
        return str(path)
    drive = path.drive.rstrip(":").lower()
    suffix = path.as_posix().partition(":")[2]
    return f"/{drive}{suffix}"


def _install_command(directory: Path, name: str, body: str) -> None:
    command = directory / name
    command.write_text("#!/bin/sh\n" + body, encoding="utf-8", newline="\n")
    command.chmod(0o755)


def _failsafe_environment(tmp_path: Path, *, fail_on_high: bool = False) -> dict[str, str]:
    assert PRODUCTION_SHELL is not None
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _install_command(fake_bin, "sleep", "exit 0\n")
    _install_command(fake_bin, "sync", "exit 0\n")
    _install_command(fake_bin, "logger", "exit 0\n")
    _install_command(
        fake_bin,
        "timeout",
        'test "${1:-}" = --kill-after=1 && shift\ntest "${1:-}" = 3 && shift\nexec "$@"\n',
    )
    _install_command(
        fake_bin,
        "pinctrl",
        'printf \'%s\\n\' "$*" >> "$DONE_GPIO_LOG"\n'
        'if test "${FAIL_ON_HIGH:-0}" = 1 && test "$*" = \'set 26 dh\'; then\n'
        "    exit 42\n"
        "fi\n",
    )
    _install_command(
        fake_bin,
        "raspi-gpio",
        'printf \'raspi-gpio %s\\n\' "$*" >> "$DONE_GPIO_LOG"\n',
    )
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": os.pathsep.join(
                (
                    str(fake_bin),
                    str(PRODUCTION_SHELL.parent),
                    environment.get("PATH", ""),
                )
            ),
            "DONE_GPIO_LOG": _shell_path(tmp_path / "gpio.log"),
            "FAIL_ON_HIGH": "1" if fail_on_high else "0",
            "TIDETRACKER_FAILSAFE_RUNTIME_DIR": _shell_path(tmp_path / "run"),
            "TIDETRACKER_FAILSAFE_STATE_DIR": _shell_path(tmp_path / "results"),
        }
    )
    return environment


def _run_failsafe(environment: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
    assert PRODUCTION_SHELL is not None
    return subprocess.run(
        [str(PRODUCTION_SHELL), str(FAILSAFE), *arguments],
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )


def test_failsafe_contract_is_single_backend_and_intent_before_gpio() -> None:
    script = FAILSAFE.read_text(encoding="utf-8")
    intent_claim = script.index('claim_runtime_marker "$intent_marker" attempted')
    gpio_invocation = script.index("timeout --kill-after=1 3 /bin/sh")

    assert intent_claim < gpio_invocation
    assert "trying fallback" not in script
    assert "--pulse-pinctrl" not in script
    assert "--pulse-raspi-gpio" not in script
    assert "elif command -v raspi-gpio" in script

    cycle_unit = (PROJECT_ROOT / "systemd" / "tidetracker-cycle.service").read_text(
        encoding="utf-8"
    )
    failsafe_unit = (PROJECT_ROOT / "systemd" / "tidetracker-failsafe.service").read_text(
        encoding="utf-8"
    )
    assert "rm -f /run/tidetracker/done" not in cycle_unit
    assert "RuntimeDirectoryMode=0750" in failsafe_unit
    assert "RuntimeDirectoryPreserve=yes" in failsafe_unit


@pytest.mark.skipif(PRODUCTION_SHELL is None, reason="a POSIX shell is unavailable")
def test_successful_done_is_completed_and_never_repeated(tmp_path: Path) -> None:
    environment = _failsafe_environment(tmp_path)

    first = _run_failsafe(environment, "--done-only")
    assert first.returncode == 0
    assert (tmp_path / "run" / "done-attempted").read_text(encoding="utf-8") == "attempted\n"
    assert (tmp_path / "run" / "done-signalled").read_text(encoding="utf-8") == "complete\n"
    first_log = (tmp_path / "gpio.log").read_text(encoding="utf-8")
    assert first_log.splitlines() == ["set 26 op dl", "set 26 dh", "set 26 dl"]

    second = _run_failsafe(environment, "--done-only")
    assert second.returncode == 0
    assert (tmp_path / "gpio.log").read_text(encoding="utf-8") == first_log


@pytest.mark.skipif(PRODUCTION_SHELL is None, reason="a POSIX shell is unavailable")
def test_ambiguous_high_failure_is_not_retried_and_onfailure_persists(
    tmp_path: Path,
) -> None:
    environment = _failsafe_environment(tmp_path, fail_on_high=True)

    first = _run_failsafe(environment, "--done-only")
    assert first.returncode != 0
    assert (tmp_path / "run" / "done-attempted").is_file()
    assert not (tmp_path / "run" / "done-signalled").exists()
    first_log = (tmp_path / "gpio.log").read_text(encoding="utf-8")
    assert first_log.splitlines() == ["set 26 op dl", "set 26 dh"]
    assert "raspi-gpio" not in first_log

    on_failure = _run_failsafe(environment)
    assert on_failure.returncode == 0
    assert (tmp_path / "gpio.log").read_text(encoding="utf-8") == first_log
    assert (tmp_path / "results" / "failsafe-result.json").is_file()
