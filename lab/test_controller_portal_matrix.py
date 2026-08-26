from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import pathlib
import subprocess
import sys
from collections.abc import Sequence

import pytest

MODULE_PATH = (
    pathlib.Path(__file__).parents[1]
    / "deploy"
    / "libexec"
    / "tidetracker-controller-portal-matrix.py"
)
LOADER = importlib.machinery.SourceFileLoader("controller_portal_matrix", str(MODULE_PATH))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None
portal = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = portal
LOADER.exec_module(portal)


def profile_case(case_id: str) -> dict[str, object]:
    security, hidden, operation = portal.CASE_SPEC[case_id]
    profile: dict[str, object] = {
        "ssid": f"tt-{case_id}"[:32],
        "security": security,
        "hidden": hidden,
    }
    if security == "wpa-personal":
        profile["password"] = "private-personal-password"
    elif security == "wpa-enterprise":
        profile.update(
            {"identity": "private.enterprise", "password": "PrivateEnterprisePassword"}
        )
    return {"case_id": case_id, "operation": operation, "profile": profile}


def private_bytes(case_id: str) -> bytes:
    return (json.dumps(profile_case(case_id), separators=(",", ":")) + "\n").encode()


def make_tools(root: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for name in portal.TOOLS:
        path = root / name
        path.write_bytes(b"test executable\n")
        path.chmod(0o700)
        values[name] = str(path.resolve())
    return values


class FakeRunner:
    def __init__(self, runtime: pathlib.Path) -> None:
        self.runtime = runtime
        self.active_service = False
        self.test_address_active = False
        self.active_connection = "tidetracker-lab-ap"
        self.calls: list[tuple[list[str], str | None, float]] = []

    def __call__(
        self,
        arguments: Sequence[str],
        *,
        input: str | None = None,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        command = list(arguments)
        self.calls.append((command, input, timeout))
        if "is-active" in command:
            return subprocess.CompletedProcess(command, 0 if self.active_service else 3, "", "")
        if "start" in command:
            self.active_service = True
            self.test_address_active = True
            (self.runtime / "ready.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "case_id": json.loads(
                            (self.runtime / "state.json").read_text(encoding="utf-8")
                        )["case_id"],
                        "ready": True,
                    }
                ),
                encoding="utf-8",
            )
            (self.runtime / "ready.json").chmod(0o600)
            return subprocess.CompletedProcess(command, 0, "", "")
        if "stop" in command:
            self.active_service = False
            self.test_address_active = False
            (self.runtime / "ready.json").unlink(missing_ok=True)
            return subprocess.CompletedProcess(command, 0, "", "")
        if "--get-values" in command:
            property_name = command[command.index("--get-values") + 1]
            if property_name == "GENERAL.NM-MANAGED":
                return subprocess.CompletedProcess(command, 0, "yes\n", "")
            connection = command[-1]
            expected = (
                portal.NORMAL_AP_PROFILE
                if connection == "tidetracker-lab-ap"
                else portal.PORTAL_CLIENT_PROFILE
            )
            values = {
                "connection.id": connection,
                **expected,
            }
            output = "".join(f"{values[name]}\n" for name in property_name.split(","))
            return subprocess.CompletedProcess(command, 0, output, "")
        if command[-3:] == ["connection", "show", "--active"]:
            return subprocess.CompletedProcess(command, 0, self.active_connection + "\n", "")
        if "connection" in command and "up" in command:
            self.active_connection = command[-1]
            return subprocess.CompletedProcess(command, 0, "activated\n", "")
        if "connection" in command and "down" in command:
            if self.active_connection == command[-1]:
                self.active_connection = ""
            return subprocess.CompletedProcess(command, 0, "", "")
        if "route" in command:
            return subprocess.CompletedProcess(
                command, 0, "default via 192.0.2.1 dev eth0\n", ""
            )
        if "nft" in pathlib.Path(command[0]).name and "list" in command:
            return subprocess.CompletedProcess(command, 1, "", "table absent")
        if "iw" in pathlib.Path(command[0]).name and command[-1] == "link":
            return subprocess.CompletedProcess(
                command,
                0,
                "Connected to 02:00:00:00:00:01\n\tSSID: TideTracker-Setup\n",
                "",
            )
        if "iw" in pathlib.Path(command[0]).name:
            hostapd = self.runtime / "hostapd.conf"
            ssid = "TideTracker-Lab"
            if hostapd.exists():
                encoded = next(
                    line.removeprefix("ssid2=")
                    for line in hostapd.read_text(encoding="utf-8").splitlines()
                    if line.startswith("ssid2=")
                )
                ssid = bytes.fromhex(encoded).decode("utf-8")
            return subprocess.CompletedProcess(
                command,
                0,
                f"Interface wlan0\n\tssid {ssid}\n\ttype AP\n\tchannel 6 (2437 MHz)\n",
                "",
            )
        if "address" in command and "show" in command:
            value = "inet 192.168.50.1/24\n" if self.test_address_active else ""
            return subprocess.CompletedProcess(command, 0, value, "")
        if "openssl" in pathlib.Path(command[0]).name:
            key = pathlib.Path(command[command.index("-keyout") + 1])
            certificate = pathlib.Path(command[command.index("-out") + 1])
            key.write_text("private test key\n", encoding="ascii")
            certificate.write_text("test certificate\n", encoding="ascii")
        return subprocess.CompletedProcess(command, 0, "present\n", "")


def write_controller_config(path: pathlib.Path) -> None:
    path.write_text(
        "TIDETRACKER_NORMAL_AP_CONNECTION=tidetracker-lab-ap\n"
        "TIDETRACKER_PORTAL_CLIENT_CONNECTION=tidetracker-zero-portal\n"
        "TIDETRACKER_ETHERNET_INTERFACE=eth0\n",
        encoding="ascii",
    )
    path.chmod(0o600)


def test_all_twelve_profiles_render_real_security_without_public_secrets() -> None:
    assert set(portal.CASE_SPEC) == {
        "visible_open_test",
        "visible_open_force_save",
        "hidden_open_test",
        "hidden_open_force_save",
        "visible_wpa_personal_test",
        "visible_wpa_personal_force_save",
        "hidden_wpa_personal_test",
        "hidden_wpa_personal_force_save",
        "visible_wpa_enterprise_test",
        "visible_wpa_enterprise_force_save",
        "hidden_wpa_enterprise_test",
        "hidden_wpa_enterprise_force_save",
    }
    for case_id in portal.CASE_SPEC:
        case = portal.parse_private_profile(private_bytes(case_id), case_id)
        profile = case["profile"]
        hostapd, eap_users = portal.render_hostapd(profile)
        assert f"ssid2={profile['ssid'].encode('utf-8').hex()}" in hostapd
        assert str(profile["ssid"]) not in hostapd
        assert f"ignore_broadcast_ssid={1 if profile['hidden'] else 0}" in hostapd
        if profile["security"] == "open":
            assert "wpa=0" in hostapd
            assert eap_users is None
        elif profile["security"] == "wpa-personal":
            assert "wpa_key_mgmt=WPA-PSK" in hostapd
            assert str(profile["password"]) not in hostapd
            assert "wpa_psk=" in hostapd
        else:
            assert "wpa_key_mgmt=WPA-EAP" in hostapd
            assert "eap_server=1" in hostapd
            assert eap_users is not None
            assert str(profile["identity"]) in eap_users
            assert str(profile["password"]) in eap_users
        public = json.dumps(portal.public_result(case_id, "validate"))
        for field in ("ssid", "identity", "password"):
            if field in profile:
                assert str(profile[field]) not in public


def test_private_profile_rejects_duplicate_keys_and_bounds() -> None:
    duplicate = (
        b'{"case_id":"visible_open_test","case_id":"visible_open_test",'
        b'"operation":"test","profile":{"ssid":"x","security":"open",'
        b'"hidden":false}}'
    )
    with pytest.raises(portal.PortalMatrixError, match="repeats"):
        portal.parse_private_profile(duplicate, "visible_open_test")
    with pytest.raises(portal.PortalMatrixError, match="input bound"):
        portal.parse_private_profile(b"x" * (portal.MAX_PRIVATE_INPUT_BYTES + 1), "visible_open_test")


def test_validate_rejects_changed_controller_profile_without_mutating_state(
    tmp_path: pathlib.Path,
) -> None:
    runtime = tmp_path / "runtime"
    config = tmp_path / "controller-lab.env"
    write_controller_config(config)
    runner = FakeRunner(runtime)
    tools_root = tmp_path / "tools"
    tools_root.mkdir()
    controller = portal.PortalMatrixController(
        runtime_dir=runtime,
        controller_config=config,
        tools=make_tools(tools_root),
        runner=runner,
        require_root_files=False,
    )
    case = portal.parse_private_profile(private_bytes("visible_open_test"), "visible_open_test")
    original = runner.__call__

    def changed_profile(
        arguments: Sequence[str], *, input: str | None = None, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        command = list(arguments)
        result = original(command, input=input, timeout=timeout)
        if "--get-values" in command and any(
            "ipv4.never-default" in argument for argument in command
        ):
            lines = result.stdout.splitlines()
            lines[-3] = "no"
            return subprocess.CompletedProcess(command, 0, "\n".join(lines) + "\n", "")
        return result

    controller.runner = changed_profile
    with pytest.raises(portal.PortalMatrixError, match="fixed definition"):
        controller.validate(case)
    assert not runtime.exists()
    assert runner.active_connection == "tidetracker-lab-ap"


@pytest.mark.parametrize("case_id", tuple(portal.CASE_SPEC))
def test_every_case_lifecycle_enforces_order_and_removes_private_state(
    tmp_path: pathlib.Path, case_id: str
) -> None:
    runtime = tmp_path / "runtime"
    config = tmp_path / "controller-lab.env"
    write_controller_config(config)
    runner = FakeRunner(runtime)
    tools_root = tmp_path / "tools"
    tools_root.mkdir()
    controller = portal.PortalMatrixController(
        runtime_dir=runtime,
        controller_config=config,
        tools=make_tools(tools_root),
        runner=runner,
        require_root_files=False,
    )
    case = portal.parse_private_profile(private_bytes(case_id), case_id)
    with pytest.raises(portal.PortalMatrixError, match="state is unavailable"):
        controller.test_plane(case)
    assert controller.validate(case)["state"] == "validated"
    assert not runtime.exists()
    assert controller.apply(case)["state"] == "portal-client-active"
    if case["profile"]["security"] == "wpa-enterprise" and sys.platform != "win32":
        assert (runtime / "eap-users").stat().st_mode & 0o777 == 0o600
    assert controller.test_plane(case)["state"] == "test-plane-active"
    assert controller.reset(case)["state"] == "normal-ap-restored"
    assert not runtime.exists()
    combined_output = "\n".join(
        json.dumps(portal.public_result(str(case["case_id"]), phase))
        for phase in portal.PHASE_STATES
    )
    for field in ("ssid", "identity", "password"):
        value = case["profile"].get(field)
        if isinstance(value, str):
            assert value not in combined_output
            assert all(value not in " ".join(call[0]) for call in runner.calls)


def test_command_failures_and_timeouts_are_bounded_and_redacted(
    tmp_path: pathlib.Path,
) -> None:
    def timeout_runner(
        arguments: Sequence[str], *, input: str | None = None, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        del input
        raise subprocess.TimeoutExpired(list(arguments), timeout)

    controller = portal.PortalMatrixController(
        runtime_dir=tmp_path / "runtime",
        controller_config=tmp_path / "config",
        tools={name: str(tmp_path / name) for name in portal.TOOLS},
        runner=timeout_runner,
        require_root_files=False,
    )
    with pytest.raises(portal.PortalMatrixError, match="failed or timed out") as captured:
        controller._run(["/fixed/tool", "private-value"], timeout=1)
    assert "private-value" not in str(captured.value)


def test_reset_recovers_atomic_state_interruption_without_leaving_secrets(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    config = tmp_path / "controller-lab.env"
    write_controller_config(config)
    runner = FakeRunner(runtime)
    tools_root = tmp_path / "tools"
    tools_root.mkdir()
    controller = portal.PortalMatrixController(
        runtime_dir=runtime,
        controller_config=config,
        tools=make_tools(tools_root),
        runner=runner,
        require_root_files=False,
    )
    case_id = "visible_wpa_personal_test"
    case = portal.parse_private_profile(private_bytes(case_id), case_id)

    def interrupted_replace(source: pathlib.Path, target: pathlib.Path) -> None:
        del source, target
        raise OSError("simulated atomic replace interruption")

    with (
        monkeypatch.context() as scoped,
        pytest.raises(OSError, match="simulated atomic replace interruption"),
    ):
        scoped.setattr(portal.os, "replace", interrupted_replace)
        controller._write_state(case_id, "apply")
    assert (runtime / "state.json.tmp").is_file()
    assert not (runtime / "state.json").exists()
    assert controller.reset(case)["state"] == "normal-ap-restored"
    assert not runtime.exists()


def test_service_recovery_attempts_every_repair_after_an_early_failure(
    tmp_path: pathlib.Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    config = tmp_path / "controller-lab.env"
    write_controller_config(config)
    runner = FakeRunner(runtime)
    tools_root = tmp_path / "tools"
    tools_root.mkdir()
    controller = portal.PortalMatrixController(
        runtime_dir=runtime,
        controller_config=config,
        tools=make_tools(tools_root),
        runner=runner,
        require_root_files=False,
    )
    original = runner.__call__

    def one_failed_repair(
        arguments: Sequence[str], *, input: str | None = None, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        command = list(arguments)
        if "nft" in pathlib.Path(command[0]).name and "delete" in command:
            runner.calls.append((command, input, timeout))
            raise subprocess.TimeoutExpired(command, timeout)
        return original(command, input=input, timeout=timeout)

    controller.runner = one_failed_repair
    with pytest.raises(portal.PortalMatrixError, match="every invariant"):
        controller.recover_service_state()
    flattened = [" ".join(call[0]) for call in runner.calls]
    assert any("address flush dev wlan0" in call for call in flattened)
    assert any("managed yes" in call for call in flattened)
    assert any("list table ip tidetracker_portal_matrix" in call for call in flattened)
    assert any("GENERAL.NM-MANAGED" in call for call in flattened)

    def failed_runner(
        arguments: Sequence[str], *, input: str | None = None, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        del input, timeout
        return subprocess.CompletedProcess(list(arguments), 9, "", "private-value")

    controller.runner = failed_runner
    with pytest.raises(portal.PortalMatrixError, match="network command failed") as captured:
        controller._run(["/fixed/tool"], timeout=1)
    assert "private-value" not in str(captured.value)


def test_controller_image_installs_fixed_private_portal_service_contract() -> None:
    root = pathlib.Path(__file__).parents[1]
    wrapper = (root / "deploy/sbin/tidetracker-controller-wifi-control").read_text(
        encoding="utf-8"
    )
    unit = (root / "systemd/tidetracker-controller-test-plane.service").read_text(
        encoding="utf-8"
    )
    provision = (root / "deploy/provision-profile.sh").read_text(encoding="utf-8")
    target_helper = (root / "deploy/sbin/tidetracker-lab-control").read_text(
        encoding="utf-8"
    )
    packages = set(
        (root / "deploy/packages/controller.txt").read_text(encoding="utf-8").splitlines()
    )
    assert "portal-matrix \"$case_id\" \"$phase\"" in wrapper
    assert all(case_id in wrapper for case_id in portal.CASE_SPEC)
    assert all(case_id in target_helper for case_id in portal.CASE_SPEC)
    assert "StandardOutput=null" in unit
    assert "StandardError=null" in unit
    assert "tidetracker-controller-portal-matrix serve" in unit
    assert "tidetracker-controller-portal-matrix recover" in unit
    assert "TimeoutStopSec=90" in unit
    assert "install -m 0750" in provision
    assert "tidetracker-controller-test-plane.service" in provision
    assert {"hostapd", "dnsmasq-base", "nftables"} <= packages
