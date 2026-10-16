from __future__ import annotations

import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).parents[1]


def test_installed_hil_wrapper_escalates_only_real_execution() -> None:
    wrapper = (ROOT / "deploy" / "bin" / "tidetracker-hil").read_text(encoding="utf-8")
    provision = (ROOT / "deploy" / "provision-profile.sh").read_text(encoding="utf-8")

    assert 'if test "$argument" = --execute' in wrapper
    assert "exec /usr/bin/sudo -n -- /usr/local/sbin/tidetracker-hil-root" in wrapper
    assert "NOPASSWD: /usr/local/sbin/tidetracker-hil-root *" in provision
    assert "install -m 0755" in provision


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX uid isolation is controller-only")
def test_root_owned_private_material_is_actually_unreadable_to_unprivileged_uid(
    tmp_path: pathlib.Path,
) -> None:
    if os.geteuid() != 0:
        pytest.skip("actual root ownership requires a root test runner")
    setpriv = shutil.which("setpriv")
    if setpriv is None:
        pytest.skip("setpriv is unavailable")
    root = tmp_path / "root-only"
    root.mkdir(mode=0o700)
    private_key = root / "controller-to-zero-ed25519"
    private_key.write_bytes(b"non-secret-test-placeholder")
    private_key.chmod(0o600)

    assert private_key.read_bytes() == b"non-secret-test-placeholder"
    probe = subprocess.run(
        [
            setpriv,
            "--reuid=65534",
            "--regid=65534",
            "--clear-groups",
            sys.executable,
            "-c",
            "import pathlib,sys; pathlib.Path(sys.argv[1]).read_bytes()",
            str(private_key),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert probe.returncode != 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sudo identity is controller-only")
def test_root_launcher_rejects_wrong_actual_sudo_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_orchestrator = types.ModuleType("hil_orchestrator")
    fake_orchestrator.HilError = RuntimeError
    fake_orchestrator.main = lambda argv: 0
    monkeypatch.setitem(sys.modules, "hil_orchestrator", fake_orchestrator)
    path = ROOT / "lab" / "hil_root_launcher.py"
    spec = importlib.util.spec_from_file_location("hil_root_launcher_test", path)
    assert spec is not None and spec.loader is not None
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    monkeypatch.setattr(launcher.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", "tidetracker")
    monkeypatch.setenv("SUDO_UID", "1234")
    monkeypatch.setattr(
        launcher.pwd,
        "getpwnam",
        lambda _name: types.SimpleNamespace(pw_uid=4321),
    )

    with pytest.raises(launcher.LauncherError, match="UID does not match"):
        launcher.validate_invocation(["--execute"])
