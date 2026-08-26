from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

import tidetracker.network as network_module
from tidetracker.models import NetworkStatus as RuntimeNetworkStatus
from tidetracker.network import (
    NetworkError,
    NetworkManagerAdapter,
    NetworkProfile,
    NetworkTestFailed,
    SecurityMode,
)


class RecordingRunner:
    def __init__(self, *, active: str = "", connectivity: str = "full\n") -> None:
        self.active = active
        self.connectivity = connectivity
        self.calls: list[list[str]] = []
        self.keyfiles: list[tuple[str, int, Path]] = []

    def __call__(
        self,
        args: Sequence[str],
        *,
        input: str | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del input, timeout
        command = list(args)
        self.calls.append(command)
        if command[-3:] == ["connection", "show", "--active"]:
            return subprocess.CompletedProcess(command, 0, self.active, "")
        if "connection" in command and "load" in command:
            path = Path(command[-1])
            self.keyfiles.append(
                (path.read_text(encoding="utf-8"), stat.S_IMODE(path.stat().st_mode), path)
            )
        if "connectivity" in command:
            return subprocess.CompletedProcess(command, 0, self.connectivity, "")
        return subprocess.CompletedProcess(command, 0, "", "")


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        (NetworkProfile("Guest", SecurityMode.OPEN), ("key-mgmt", None)),
        (
            NetworkProfile("Hidden Home", SecurityMode.WPA_PERSONAL, hidden=True, password="a-safe-pass"),
            ("psk=a-safe-pass", "hidden=true"),
        ),
        (
            NetworkProfile(
                "Campus",
                SecurityMode.WPA_ENTERPRISE,
                identity="tide-user",
                password="enterprise-secret",
            ),
            ("key-mgmt=wpa-eap", "phase2-auth=mschapv2"),
        ),
    ],
)
def test_candidates_live_only_in_owner_private_non_autoconnecting_runtime_keyfiles(
    tmp_path: Path,
    profile: NetworkProfile,
    expected: tuple[str, str | None],
) -> None:
    runner = RecordingRunner()
    persistent = tmp_path / "persistent"
    runtime = tmp_path / "run"
    adapter = NetworkManagerAdapter(
        profile_directory=persistent,
        runtime_directory=runtime,
        runner=runner,
    )

    adapter.install(profile)

    contents, mode, path = runner.keyfiles[0]
    if os.name != "nt":
        assert mode & 0o077 == 0
    assert path.exists()
    assert path.parent == runtime
    assert "autoconnect=false" in contents
    assert not persistent.exists()
    if profile.security is SecurityMode.OPEN:
        assert "[wifi-security]" not in contents
    else:
        assert expected[0] in contents
    if expected[1] is not None:
        assert expected[1] in contents
    for secret in (profile.identity, profile.password):
        if secret:
            assert all(secret not in argument for call in runner.calls for argument in call)
            assert secret not in repr(profile)


def test_profile_validation_rejects_credential_mismatches() -> None:
    with pytest.raises(ValueError, match="must not include credentials"):
        NetworkProfile("Guest", SecurityMode.OPEN, password="should-not-exist")
    with pytest.raises(ValueError, match="8 to 63"):
        NetworkProfile("Home", SecurityMode.WPA_PERSONAL, password="short")
    with pytest.raises(ValueError, match="identity"):
        NetworkProfile("Campus", SecurityMode.WPA_ENTERPRISE, password="secret")


def test_scan_parses_security_escaped_ssids_and_deduplicates(tmp_path: Path) -> None:
    output = (
        "Cafe\\:West:WPA2:61\n"
        "Cafe\\:West:WPA2:82\n"
        "Campus:WPA2 802.1X:70\n"
        "Guest:--:15\n"
        ":--:99\n"
    )

    def runner(
        args: Sequence[str], *, input: str | None = None, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        del input, timeout
        return subprocess.CompletedProcess(args, 0, output, "")

    adapter = NetworkManagerAdapter(
        profile_directory=tmp_path / "persistent",
        runtime_directory=tmp_path / "run",
        runner=runner,
    )

    access_points = adapter.scan()

    assert [(item.ssid, item.security, item.signal) for item in access_points] == [
        ("Cafe:West", SecurityMode.WPA_PERSONAL, 82),
        ("Campus", SecurityMode.WPA_ENTERPRISE, 70),
        ("Guest", SecurityMode.OPEN, 15),
    ]


def test_failed_test_deletes_candidate_and_restores_snapshot(tmp_path: Path) -> None:
    runner = RecordingRunner(active="old-wifi-uuid:wifi\n", connectivity="limited\n")
    adapter = NetworkManagerAdapter(
        profile_directory=tmp_path / "persistent",
        runtime_directory=tmp_path / "run",
        runner=runner,
    )
    profile = NetworkProfile("Home", SecurityMode.WPA_PERSONAL, password="correct-horse")

    with pytest.raises(NetworkTestFailed, match="without confirmed internet"):
        adapter.test_and_save(profile)

    candidate = next(call[-1] for call in runner.calls if "load" in call)
    candidate_path = Path(candidate)
    delete_calls = [call for call in runner.calls if "delete" in call]
    restore_calls = [call for call in runner.calls if "old-wifi-uuid" in call and "up" in call]
    assert len(delete_calls) == 1
    assert len(restore_calls) == 1
    assert runner.calls.index(delete_calls[0]) < runner.calls.index(restore_calls[0])
    assert not candidate_path.exists()


def test_reconstructed_adapter_removes_interrupted_volatile_candidate(tmp_path: Path) -> None:
    runner = RecordingRunner()
    persistent = tmp_path / "persistent"
    runtime = tmp_path / "run"
    first = NetworkManagerAdapter(
        profile_directory=persistent,
        runtime_directory=runtime,
        runner=runner,
    )
    profile_uuid = first.install(
        NetworkProfile("Home", SecurityMode.WPA_PERSONAL, password="correct-horse")
    )
    assert tuple(runtime.glob("*.nmconnection"))

    NetworkManagerAdapter(
        profile_directory=persistent,
        runtime_directory=runtime,
        runner=runner,
    )

    assert not tuple(runtime.glob("*.nmconnection"))
    assert not persistent.exists()
    assert any("delete" in call and profile_uuid in call for call in runner.calls)


def test_reconstructed_adapter_removes_inert_interrupted_persistent_promotion(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()
    persistent = tmp_path / "persistent"
    runtime = tmp_path / "run"
    first = NetworkManagerAdapter(
        profile_directory=persistent,
        runtime_directory=runtime,
        runner=runner,
    )
    profile_uuid = first.install(NetworkProfile("Guest", SecurityMode.OPEN))
    candidate = next(runtime.glob("*.nmconnection"))
    persistent.mkdir()
    interrupted = persistent / candidate.name
    interrupted.write_bytes(candidate.read_bytes())
    if os.name != "nt":
        interrupted.chmod(0o600)

    NetworkManagerAdapter(
        profile_directory=persistent,
        runtime_directory=runtime,
        runner=runner,
    )

    assert not interrupted.exists()
    assert any("delete" in call and profile_uuid in call for call in runner.calls)


def test_reconstructed_adapter_removes_active_profile_with_pending_marker(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()
    persistent = tmp_path / "persistent"
    runtime = tmp_path / "run"
    first = NetworkManagerAdapter(
        profile_directory=persistent,
        runtime_directory=runtime,
        runner=runner,
    )
    profile_uuid = first.install(
        NetworkProfile("Home", SecurityMode.WPA_PERSONAL, password="correct-horse")
    )
    candidate = next(runtime.glob("*.nmconnection"))
    persistent.mkdir()
    interrupted = persistent / candidate.name
    interrupted.write_text(
        candidate.read_text(encoding="utf-8").replace(
            "autoconnect=false", "autoconnect=true"
        ),
        encoding="utf-8",
    )
    if os.name != "nt":
        interrupted.chmod(0o600)
    marker = persistent / f".tidetracker-{profile_uuid}.pending"
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "phase": "preparing",
                "new_uuid": profile_uuid,
                "new_sha256": hashlib.sha256(interrupted.read_bytes()).hexdigest(),
                "retire": [],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        marker.chmod(0o600)

    NetworkManagerAdapter(
        profile_directory=persistent,
        runtime_directory=runtime,
        runner=runner,
    )

    assert not interrupted.exists()
    assert not marker.exists()
    assert sum("delete" in call and profile_uuid in call for call in runner.calls) >= 2


def test_repeated_save_retires_only_the_previous_owned_profile_for_same_ssid(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()
    persistent = tmp_path / "persistent"
    adapter = NetworkManagerAdapter(
        profile_directory=persistent,
        runtime_directory=tmp_path / "run",
        runner=runner,
    )

    adapter.force_save(
        NetworkProfile("Home", SecurityMode.WPA_PERSONAL, password="first-secret")
    )
    old_path = next(persistent.glob("tidetracker-*.nmconnection"))
    old_uuid = old_path.stem.removeprefix("tidetracker-")
    adapter.force_save(
        NetworkProfile("Home", SecurityMode.WPA_PERSONAL, password="second-secret")
    )

    retained = tuple(persistent.glob("tidetracker-*.nmconnection"))
    assert len(retained) == 1
    assert retained[0] != old_path
    assert "second-secret" in retained[0].read_text(encoding="utf-8")
    assert any("delete" in call and old_uuid in call for call in runner.calls)
    assert not tuple(persistent.glob(".tidetracker-*.pending"))


def test_save_preserves_owned_profiles_for_other_ssids(tmp_path: Path) -> None:
    persistent = tmp_path / "persistent"
    adapter = NetworkManagerAdapter(
        profile_directory=persistent,
        runtime_directory=tmp_path / "run",
        runner=RecordingRunner(),
    )

    adapter.force_save(NetworkProfile("Guest", SecurityMode.OPEN))
    adapter.force_save(
        NetworkProfile("Home", SecurityMode.WPA_PERSONAL, password="correct-horse")
    )

    assert len(tuple(persistent.glob("tidetracker-*.nmconnection"))) == 2


def test_committed_replacement_journal_finishes_after_cleanup_failure(
    tmp_path: Path,
) -> None:
    class RetirementFailureRunner(RecordingRunner):
        failed_uuid: str | None = None

        def __call__(
            self,
            args: Sequence[str],
            *,
            input: str | None = None,
            timeout: float | None = None,
        ) -> subprocess.CompletedProcess[str]:
            result = super().__call__(args, input=input, timeout=timeout)
            if self.failed_uuid and "delete" in args and self.failed_uuid in args:
                return subprocess.CompletedProcess(args, 10, "", "")
            if self.failed_uuid and "show" in args and self.failed_uuid in args:
                return subprocess.CompletedProcess(args, 0, f"{self.failed_uuid}\n", "")
            return result

    runner = RetirementFailureRunner()
    persistent = tmp_path / "persistent"
    runtime = tmp_path / "run"
    adapter = NetworkManagerAdapter(
        profile_directory=persistent,
        runtime_directory=runtime,
        runner=runner,
    )
    adapter.force_save(
        NetworkProfile("Home", SecurityMode.WPA_PERSONAL, password="first-secret")
    )
    old_path = next(persistent.glob("tidetracker-*.nmconnection"))
    runner.failed_uuid = old_path.stem.removeprefix("tidetracker-")

    with pytest.raises(NetworkError, match="committed"):
        adapter.force_save(
            NetworkProfile("Home", SecurityMode.WPA_PERSONAL, password="second-secret")
        )

    assert len(tuple(persistent.glob("tidetracker-*.nmconnection"))) == 2
    assert len(tuple(persistent.glob(".tidetracker-*.pending"))) == 1
    runner.failed_uuid = None
    NetworkManagerAdapter(
        profile_directory=persistent,
        runtime_directory=runtime,
        runner=runner,
    )

    retained = tuple(persistent.glob("tidetracker-*.nmconnection"))
    assert len(retained) == 1
    assert "second-secret" in retained[0].read_text(encoding="utf-8")
    assert not tuple(persistent.glob(".tidetracker-*.pending"))


@pytest.mark.parametrize(("persistent", "marker_temp"), [(False, False), (True, False), (True, True)])
def test_reconstructed_adapter_removes_owned_interrupted_temporary_profiles(
    tmp_path: Path,
    persistent: bool,
    marker_temp: bool,
) -> None:
    profile_uuid = "12345678-1234-4234-8234-123456789abc"
    directory = tmp_path / ("persistent" if persistent else "run")
    directory.mkdir()
    if marker_temp:
        name = f"..tidetracker-{profile_uuid}.pending.deadbeef.tmp"
    else:
        name = f".tidetracker-{profile_uuid}.nmconnection.deadbeef.tmp"
    temporary = directory / name
    temporary.write_text("partial credential-bearing data", encoding="utf-8")
    if os.name != "nt":
        temporary.chmod(0o600)

    NetworkManagerAdapter(
        profile_directory=tmp_path / "persistent",
        runtime_directory=tmp_path / "run",
        runner=RecordingRunner(),
    )

    assert not temporary.exists()


def test_reconstructed_adapter_rejects_malformed_persistent_profile(tmp_path: Path) -> None:
    persistent = tmp_path / "persistent"
    persistent.mkdir()
    (persistent / "tidetracker-malformed.nmconnection").write_text(
        "[connection]\nid=tidetracker-malformed\n",
        encoding="utf-8",
    )

    with pytest.raises(NetworkError, match="malformed or unsafe"):
        NetworkManagerAdapter(
            profile_directory=persistent,
            runtime_directory=tmp_path / "run",
            runner=RecordingRunner(),
        )


def test_stale_profile_cleanup_fails_if_networkmanager_still_has_profile(
    tmp_path: Path,
) -> None:
    class RetainingRunner(RecordingRunner):
        def __call__(
            self,
            args: Sequence[str],
            *,
            input: str | None = None,
            timeout: float | None = None,
        ) -> subprocess.CompletedProcess[str]:
            result = super().__call__(args, input=input, timeout=timeout)
            if "delete" in args:
                return subprocess.CompletedProcess(args, 10, "", "")
            if "show" in args and "uuid" in args:
                return subprocess.CompletedProcess(args, 0, "profile-uuid\n", "")
            return result

    runtime = tmp_path / "run"
    first = NetworkManagerAdapter(
        profile_directory=tmp_path / "persistent",
        runtime_directory=runtime,
        runner=RecordingRunner(),
    )
    first.install(NetworkProfile("Guest", SecurityMode.OPEN))

    with pytest.raises(NetworkError, match="retained a credential profile"):
        NetworkManagerAdapter(
            profile_directory=tmp_path / "persistent",
            runtime_directory=runtime,
            runner=RetainingRunner(),
        )


def test_failed_transaction_surfaces_unproved_durable_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = RecordingRunner(active="old-wifi-uuid:wifi\n", connectivity="limited\n")
    runtime = tmp_path / "run"
    adapter = NetworkManagerAdapter(
        profile_directory=tmp_path / "persistent",
        runtime_directory=runtime,
        runner=runner,
    )
    real_fsync = network_module._fsync_directory
    runtime_fsyncs = 0

    def fail_runtime_fsync(path: Path) -> None:
        nonlocal runtime_fsyncs
        if path == runtime:
            runtime_fsyncs += 1
            if runtime_fsyncs >= 2:
                raise NetworkError("injected directory fsync failure")
        real_fsync(path)

    monkeypatch.setattr(network_module, "_fsync_directory", fail_runtime_fsync)

    with pytest.raises(NetworkError, match="rollback could not be proved"):
        adapter.test_and_save(
            NetworkProfile("Home", SecurityMode.WPA_PERSONAL, password="correct-horse")
        )


def test_successful_test_commits_without_rollback(tmp_path: Path) -> None:
    runner = RecordingRunner(active="old-wifi-uuid:wifi\n")
    persistent = tmp_path / "persistent"
    runtime = tmp_path / "run"
    adapter = NetworkManagerAdapter(
        profile_directory=persistent,
        runtime_directory=runtime,
        runner=runner,
    )

    public = adapter.test_and_save(
        NetworkProfile("Home", SecurityMode.WPA_PERSONAL, password="correct-horse")
    )

    assert public == {"ssid": "Home", "security": "wpa-personal", "hidden": False}
    assert not [call for call in runner.calls if "delete" in call]
    assert not [call for call in runner.calls if "old-wifi-uuid" in call and "up" in call]
    saved = tuple(persistent.glob("*.nmconnection"))
    assert len(saved) == 1
    assert "autoconnect=true" in saved[0].read_text(encoding="utf-8")
    assert not tuple(runtime.glob("*.nmconnection"))


def test_hotspot_has_static_shared_ipv4_and_restores_client(tmp_path: Path) -> None:
    runner = RecordingRunner(active="old-wifi-uuid:wifi\n")
    adapter = NetworkManagerAdapter(
        profile_directory=tmp_path / "persistent",
        runtime_directory=tmp_path / "run",
        runner=runner,
    )

    session = adapter.start_hotspot("TideTracker-Lab")
    contents = runner.keyfiles[-1][0]

    assert "mode=ap" in contents
    assert "band=bg" in contents
    assert "ssid=TideTracker-Lab" in contents
    assert "method=shared" in contents
    assert "address1=192.168.42.1/24" in contents

    adapter.stop_hotspot(session)

    assert any("delete" in call and session.profile_uuid in call for call in runner.calls)
    assert any("up" in call and "old-wifi-uuid" in call for call in runner.calls)
    assert not tuple((tmp_path / "run").glob("*.nmconnection"))


def test_prepare_client_returns_core_network_status(tmp_path: Path) -> None:
    runner = RecordingRunner()
    adapter = NetworkManagerAdapter(
        profile_directory=tmp_path / "persistent",
        runtime_directory=tmp_path / "run",
        runner=runner,
    )

    result = adapter.prepare_client(timeout_seconds=7)

    assert result == RuntimeNetworkStatus(connected=True, interface="wlan0")
    call = next(call for call in runner.calls if "connect" in call)
    assert call[1:3] == ["--wait", "7"]


def test_release_client_uses_device_disconnect_with_a_bounded_wait(tmp_path: Path) -> None:
    runner = RecordingRunner()
    adapter = NetworkManagerAdapter(
        profile_directory=tmp_path / "persistent",
        runtime_directory=tmp_path / "run",
        runner=runner,
    )

    adapter.release_client(timeout_seconds=4)

    call = next(call for call in runner.calls if "disconnect" in call)
    assert call[1:3] == ["--wait", "4"]
    assert call[-3:] == ["device", "disconnect", "wlan0"]
