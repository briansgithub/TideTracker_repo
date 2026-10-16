from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

from tidetracker.key_policy import build_key_policy

MODULE_PATH = pathlib.Path(__file__).with_name("hil_orchestrator.py")
SPEC = importlib.util.spec_from_file_location("hil_orchestrator", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
hil = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = hil
SPEC.loader.exec_module(hil)


def hardware_inventory() -> dict[str, dict[str, str]]:
    return {
        name: {"model": f"test-{name}", "serial_or_asset_id": f"asset-{name}"}
        for name in sorted(hil.HARDWARE_INVENTORY_COMPONENTS)
    }


def tool_identities() -> dict[str, dict[str, object]]:
    return {
        name: {
            "canonical_argv": [f"/opt/tidetracker/{name}"],
            "executable_path": f"/opt/tidetracker/{name}",
            "executable_sha256": "a" * 64,
            "executable_size_bytes": 1,
            "support_files": [],
        }
        for name in sorted(hil.TOOL_IDENTITY_NAMES)
    }


def authorization_guard() -> Any:
    return hil.AuthorizationGuard(datetime.now(UTC) + timedelta(hours=1))


def openssh_public_key(fill: int) -> bytes:
    key_type = b"ssh-ed25519"
    raw = bytes([fill]) * 32
    wire = (
        len(key_type).to_bytes(4, "big")
        + key_type
        + len(raw).to_bytes(4, "big")
        + raw
    )
    return b"ssh-ed25519 " + base64.b64encode(wire) + b" test\n"


def pem_public_key(fill: int) -> bytes:
    der = bytes.fromhex("302a300506032b6570032100") + bytes([fill]) * 32
    return (
        b"-----BEGIN PUBLIC KEY-----\n"
        + base64.b64encode(der)
        + b"\n-----END PUBLIC KEY-----\n"
    )


class HilOrchestratorTests(unittest.TestCase):
    def test_simulation_runs_two_closed_loop_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = pathlib.Path(temporary) / "session"
            config = {
                "schema_version": 1,
                "timing": {
                    "boot_timeout_seconds": 1,
                    "done_timeout_seconds": 1,
                    "power_off_timeout_seconds": 1,
                },
            }
            orchestrator = hil.Orchestrator(
                fixture=hil.SimulationFixture(done_delay_seconds=0),
                target=hil.SimulationTarget(),
                camera=hil.Camera(None, simulated=True),
                artifact_directory=output,
                config=config,
                simulated=True,
            )
            orchestrator.run(2, None)

            result = json.loads((output / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(result["outcome"], "success")
            self.assertEqual(result["controller_machine_id_sha256"], "0" * 64)
            self.assertEqual(len(result["cycles"]), 2)
            self.assertGreaterEqual(result["cycles"][0]["cold_wake_to_done_seconds"], 0)
            self.assertTrue((output / "cycle-001" / "display.jpg").is_file())
            self.assertTrue((output / "cycle-002" / "result.json").is_file())

    def test_authorization_expiry_mid_campaign_blocks_the_next_cycle(self) -> None:
        start = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
        expiry = start + timedelta(minutes=1)
        clock = types.SimpleNamespace(current=start)

        with tempfile.TemporaryDirectory() as temporary:
            output = pathlib.Path(temporary) / "expired-campaign"
            fixture = hil.SimulationFixture(done_delay_seconds=0)
            target = hil.SimulationTarget()
            original_collect = target.collect

            def collect_and_expire() -> dict[str, Any]:
                evidence = original_collect()
                clock.current = expiry
                return evidence

            orchestrator = hil.Orchestrator(
                fixture=fixture,
                target=target,
                camera=hil.Camera(None, simulated=True),
                artifact_directory=output,
                config={"timing": {"done_timeout_seconds": 1}},
                simulated=True,
                authorization_guard=hil.AuthorizationGuard(
                    expiry, clock=lambda: clock.current
                ),
            )

            with (
                mock.patch.object(target, "collect", side_effect=collect_and_expire),
                self.assertRaisesRegex(hil.HilError, "expired before cycle 2"),
            ):
                orchestrator.run(2, None)

            session = json.loads((output / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(session["outcome"], "failure")
            self.assertEqual(len(session["cycles"]), 1)
            self.assertFalse(fixture.power)

    def test_default_cli_is_non_mutating_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = pathlib.Path(temporary) / "must-not-exist"
            exit_code = hil.main(
                [
                    "--config",
                    str(pathlib.Path(__file__).with_name("hil_config.example.json")),
                    "--artifact-dir",
                    str(output),
                ]
            )
            self.assertEqual(exit_code, 0)
            self.assertFalse(output.exists())

    def test_real_cli_rejects_non_root_before_reading_private_config(self) -> None:
        with (
            mock.patch.object(hil.os, "geteuid", return_value=1000, create=True),
            self.assertRaisesRegex(hil.HilError, "root-only tidetracker HIL launcher"),
        ):
            hil.main(
                [
                    "--execute",
                    "--execute-confirmation",
                    "EXECUTE-TIDETRACKER-HIL",
                    "--config",
                    "must-not-be-read.json",
                ]
            )

    def test_invalid_fixture_transition_fails(self) -> None:
        fixture = hil.SimulationFixture(done_delay_seconds=0)
        fixture.set_mode("setup")
        fixture.set_power(True)
        with self.assertRaises(hil.HilError):
            fixture.wait_done(1)

    def test_simulation_dispatches_wifi_matrix_and_phase_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = pathlib.Path(temporary) / "session"
            exit_code = hil.main(
                [
                    "--simulate",
                    "--config",
                    str(pathlib.Path(__file__).with_name("hil_config.example.json")),
                    "--wifi-matrix",
                    "--power-cut-phase",
                    "persist",
                    "--artifact-dir",
                    str(output),
                ]
            )
            self.assertEqual(exit_code, 0)
            result = json.loads((output / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(len(result["wifi_matrix"]), len(hil.WIFI_SCENARIOS))
            self.assertEqual(len(result["portal_matrix"]), len(hil.PORTAL_CASE_SPEC) + 1)
            self.assertTrue(result["portal_secret_artifact_scan"]["target"]["passed"])
            self.assertEqual(
                result["portal_secret_artifact_scan"]["controller_artifacts"][
                    "forbidden_matches"
                ],
                0,
            )
            self.assertEqual(result["power_cut_recoveries"][0]["phase"], "persist")
            self.assertEqual(result["outcome"], "success")

    def test_simulation_covers_all_update_power_cut_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            bundle = root / "candidate.tar"
            bundle.write_bytes(b"signed candidate placeholder")
            output = root / "update-cuts"
            exit_code = hil.main(
                [
                    "--simulate",
                    "--config",
                    str(pathlib.Path(__file__).with_name("hil_config.example.json")),
                    "--bundle",
                    str(bundle),
                    "--expected-release-id",
                    "candidate-1",
                    "--expected-execution-source",
                    "verified-release",
                    "--update-power-cut-phase",
                    "all",
                    "--artifact-dir",
                    str(output),
                ]
            )
            self.assertEqual(exit_code, 0)
            result = json.loads((output / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(result["update_power_cut_phases"], list(hil.UPDATE_PHASES))
            recoveries = result["update_power_cut_recoveries"]
            self.assertEqual([item["phase"] for item in recoveries], list(hil.UPDATE_PHASES))
            self.assertTrue(all(item["outcome"] == "success" for item in recoveries))
            self.assertTrue(
                all(item["target_update_status"]["state_valid"] is True for item in recoveries)
            )
            self.assertTrue(all(item["camera"]["image_size_bytes"] > 0 for item in recoveries))

    def test_accelerated_campaign_is_explicitly_bound_to_twelve_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = pathlib.Path(temporary) / "accelerated"
            exit_code = hil.main(
                [
                    "--simulate",
                    "--accelerated-cycles",
                    "--config",
                    str(pathlib.Path(__file__).with_name("hil_config.example.json")),
                    "--artifact-dir",
                    str(output),
                ]
            )
            self.assertEqual(exit_code, 0)
            result = json.loads((output / "session.json").read_text(encoding="utf-8"))
            self.assertTrue(result["accelerated_cycle_campaign"])
            self.assertEqual(result["accelerated_cycles_required"], 12)
            self.assertEqual(result["cycles_requested"], 12)
            self.assertEqual(len(result["cycles"]), 12)

    def test_simulation_dispatches_isolated_field_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = pathlib.Path(temporary) / "field"
            exit_code = hil.main(
                [
                    "--simulate",
                    "--field-cycle",
                    "--config",
                    str(pathlib.Path(__file__).with_name("hil_config.example.json")),
                    "--artifact-dir",
                    str(output),
                ]
            )
            self.assertEqual(exit_code, 0)
            result = json.loads((output / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(result["cycles"], [])
            self.assertEqual(
                result["field_cycle"]["usb_while_powered"]["usb_management"], "disconnected"
            )

    def test_execute_is_rejected_before_serial_without_signed_gate(self) -> None:
        with mock.patch.object(hil, "SerialFixture") as serial_fixture:
            with self.assertRaisesRegex(hil.HilError, "execute-confirmation"):
                hil.main(
                    [
                        "--execute",
                        "--config",
                        str(pathlib.Path(__file__).with_name("hil_config.example.json")),
                    ]
                )
            serial_fixture.assert_not_called()

    def test_serial_fixture_accepts_protocol_done_without_application_run_id(self) -> None:
        class FakeSerial:
            def __init__(self) -> None:
                self.timeout = 5.0
                self.written = b""

            def write(self, value: bytes) -> None:
                self.written += value

            def flush(self) -> None:
                return

            def readline(self) -> bytes:
                return b'{"ok":true,"edge_us":123456,"pulse_us":500000}\n'

            def close(self) -> None:
                return

        fake = FakeSerial()
        serial_module = types.SimpleNamespace(Serial=lambda *args, **kwargs: fake)
        with mock.patch.dict(sys.modules, {"serial": serial_module}):
            fixture = hil.SerialFixture(
                "COM-test", 115200, authorization_guard=authorization_guard()
            )
            response = fixture.wait_done(180)
        request = json.loads(fake.written.decode("utf-8"))
        self.assertEqual(request, {"command": "wait_done", "timeout_ms": 180000})
        self.assertEqual(response["pulse_us"], 500000)
        self.assertIn("_host_received_at", response)
        self.assertEqual(fake.timeout, 5.0)

    def test_controller_execution_lock_rejects_a_second_executor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "hil.lock"
            path.write_bytes(b"\0")
            path.chmod(0o660)
            first = hil.HilExecutionLock(path, {"gate_sha256": "a" * 64})
            second = hil.HilExecutionLock(path, {"gate_sha256": "b" * 64})
            first.acquire()
            try:
                with self.assertRaisesRegex(hil.HilError, "already holds"):
                    second.acquire()
            finally:
                first.release()

    def test_uncharacterized_short_done_pulse_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            orchestrator = hil.Orchestrator(
                fixture=hil.SimulationFixture(done_delay_seconds=0),
                target=hil.SimulationTarget(),
                camera=hil.Camera(None, simulated=True),
                artifact_directory=pathlib.Path(temporary) / "unused",
                config={"timing": {"done_timeout_seconds": 1}},
                simulated=True,
                safety_gate={
                    "done_pulse": {
                        "minimum_us": 400_000,
                        "maximum_us": 600_000,
                    }
                },
            )
            with self.assertRaisesRegex(hil.HilError, "signed"):
                orchestrator._validate_done_timing({"pulse_us": 20_000})

    def test_uart_capture_fails_closed_on_empty_evidence(self) -> None:
        class FakeSerial:
            def read(self, size: int) -> bytes:
                del size
                return b""

            def close(self) -> None:
                return

        with tempfile.TemporaryDirectory() as temporary:
            serial_module = types.SimpleNamespace(Serial=lambda *args, **kwargs: FakeSerial())
            with mock.patch.dict(sys.modules, {"serial": serial_module}):
                capture = hil.UartCapture(
                    "COM-test",
                    115200,
                    pathlib.Path(temporary) / "uart.log",
                    authorization_guard=authorization_guard(),
                )
                capture.start()
                with self.assertRaisesRegex(hil.HilError, "empty evidence"):
                    capture.close()

    def test_uart_capture_fails_closed_on_reader_error(self) -> None:
        class BrokenSerial:
            def read(self, size: int) -> bytes:
                del size
                raise OSError("isolator disconnected")

            def close(self) -> None:
                return

        with tempfile.TemporaryDirectory() as temporary:
            serial_module = types.SimpleNamespace(Serial=lambda *args, **kwargs: BrokenSerial())
            with mock.patch.dict(sys.modules, {"serial": serial_module}):
                capture = hil.UartCapture(
                    "COM-test",
                    115200,
                    pathlib.Path(temporary) / "uart.log",
                    authorization_guard=authorization_guard(),
                )
                capture.start()
                capture._thread.join(timeout=1)
                with self.assertRaisesRegex(hil.HilError, "UART capture failed"):
                    capture.close()

    def test_safety_gate_uses_one_stable_snapshot_for_verify_parse_and_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            now = datetime.now(UTC)
            gate_value = {
                "schema_version": 1,
                "approved": True,
                "hardware_id": "rig-1",
                "approved_at": (now - timedelta(minutes=1)).isoformat(),
                "valid_until": (now + timedelta(hours=1)).isoformat(),
                "approval_nonce": "nonce-1234567890-abcd",
                "fixture_firmware_sha256": "1" * 64,
                "target_known_hosts_sha256": "2" * 64,
                "controller_machine_id_sha256": "6" * 64,
                "target_image_sha256": "3" * 64,
                "target_image_manifest_sha256": "4" * 64,
                "target_image_build_id": "5" * 64,
                "target_key_policy_sha256": "7" * 64,
                "hardware_inventory": hardware_inventory(),
                "tool_identities": tool_identities(),
                "approved_operations": ["cycles", "update_power_cuts"],
                "approved_update_power_cut_phases": ["begin"],
                "usb_vbus_isolated": True,
                "uart_receive_only": True,
                "fixture_defaults_power_off": True,
                "fixture_defaults_setup": True,
                "done_pulse": {
                    "minimum_us": 400000,
                    "maximum_us": 600000,
                    "shortened_timing_hardware_validated": False,
                },
                "measurements": {
                    "zero_5v_off_usb_connected_v": 0.0,
                    "zero_3v3_off_all_sense_connected_v": 0.0,
                    "zero_5v_on_under_load_v": 5.0,
                },
            }
            gate = root / "gate.json"
            original = (json.dumps(gate_value, separators=(",", ":")) + "\n").encode()
            gate.write_bytes(original)
            gate.with_name("gate.json.sig").write_bytes(b"signature")
            key = root / "approver.pub.pem"
            key.write_bytes(b"public key")
            openssl = root / "openssl"
            openssl.write_bytes(b"executable placeholder")

            def mutate_after_snapshot(*args: object, **kwargs: object) -> object:
                del args, kwargs
                gate.write_text('{"schema_version":0}\n', encoding="utf-8")
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                mock.patch.object(hil, "FIXED_OPENSSL", openssl),
                mock.patch.object(hil.subprocess, "run", side_effect=mutate_after_snapshot),
            ):
                result = hil.verify_safety_gate(
                    gate,
                    key,
                    expected_identity={
                        "hardware_id": "rig-1",
                        "fixture_firmware_sha256": "1" * 64,
                        "target_known_hosts_sha256": "2" * 64,
                        "controller_machine_id_sha256": "6" * 64,
                        "target_image_sha256": "3" * 64,
                        "target_image_manifest_sha256": "4" * 64,
                        "target_image_build_id": "5" * 64,
                        "target_key_policy_sha256": "7" * 64,
                        "hardware_inventory": hardware_inventory(),
                        "tool_identities": tool_identities(),
                    },
                    approval_nonce="nonce-1234567890-abcd",
                )
            self.assertEqual(result["gate_sha256"], hashlib.sha256(original).hexdigest())
            self.assertEqual(result["signature"], "verified")
            self.assertEqual(result["controller_machine_id_sha256"], "6" * 64)
            self.assertEqual(result["hardware_inventory"], hardware_inventory())
            self.assertEqual(result["tool_identities"], tool_identities())

    def test_controller_machine_identity_hashes_one_bounded_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            machine_id = pathlib.Path(temporary) / "machine-id"
            payload = b"0123456789abcdef0123456789abcdef\n"
            machine_id.write_bytes(payload)
            result = hil.snapshot_controller_machine_id(machine_id)
        self.assertEqual(result, hashlib.sha256(payload).hexdigest())

    def test_bound_command_requires_and_rechecks_interpreter_script(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script = (pathlib.Path(temporary) / "validator.py").resolve()
            script.write_text("print('accepted')\n", encoding="utf-8")
            command = [sys.executable, str(script), "{output}"]
            with self.assertRaisesRegex(hil.HilError, "missing from its support_files"):
                hil.BoundCommand.create(
                    command,
                    [],
                    label="test.validator",
                    require_output=True,
                )
            binding = hil.BoundCommand.create(
                command,
                [str(script)],
                label="test.validator",
                require_output=True,
            )
            script.write_text("print('substituted implementation')\n", encoding="utf-8")
            with self.assertRaisesRegex(hil.HilError, "identity changed"):
                binding.assert_current()
            with self.assertRaisesRegex(hil.HilError, "direct absolute script"):
                hil.BoundCommand.create(
                    [sys.executable, "-m", "replaceable_validator", "{output}"],
                    [],
                    label="test.validator",
                    require_output=True,
                )
            with self.assertRaisesRegex(hil.HilError, "canonical absolute support"):
                hil.BoundCommand.create(
                    [sys.executable, str(script), "--reference=reference.png", "{output}"],
                    [str(script)],
                    label="test.validator",
                    require_output=True,
                )

    def test_field_uart_identity_is_bound_to_signed_session_identity(self) -> None:
        class FakeCapture:
            def close(self) -> dict[str, object]:
                return {
                    "size_bytes": 100,
                    "sha256": "9" * 64,
                    "schema1_event_count": 2,
                    "event_names": ["phase_started", "target_identity"],
                    "_events": [
                        {"schema_version": 1, "event": "phase_started", "phase": "MODE"},
                        {
                            "schema_version": 1,
                            "event": "target_identity",
                            "release_id": "candidate-1",
                            "execution_source": "verified-release",
                            "image_build_id": "5" * 64,
                            "key_policy_sha256": "7" * 64,
                        },
                    ],
                }

        with tempfile.TemporaryDirectory() as temporary:
            orchestrator = hil.Orchestrator(
                fixture=hil.SimulationFixture(done_delay_seconds=0),
                target=hil.SimulationTarget("candidate-1", "verified-release"),
                camera=hil.Camera(None, simulated=True),
                artifact_directory=pathlib.Path(temporary) / "unused",
                config={},
                simulated=False,
                target_identity={
                    "release_id": "candidate-1",
                    "execution_source": "verified-release",
                    "image_build_id": "5" * 64,
                    "image_sha256": "3" * 64,
                    "image_manifest_sha256": "4" * 64,
                    "key_policy_sha256": "7" * 64,
                    "attestation": "verified_signed_safety_gate",
                },
                authorization_guard=authorization_guard(),
            )
            evidence = orchestrator._finish_uart(
                FakeCapture(),
                require_runtime_phase=True,
                require_target_identity=True,
            )
        self.assertTrue(evidence["target_identity"]["matched_signed_target_identity"])

    def test_portal_matrix_dispatches_every_private_case_and_phase_over_stdin(self) -> None:
        config = hil.load_config(pathlib.Path(__file__).with_name("hil_config.example.json"))
        portal_config = config["portal_tests"]
        calls: list[tuple[list[str], str | None, float]] = []

        def run(
            command: list[str],
            *,
            input: str | None = None,
            timeout: float,
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            del kwargs
            calls.append((command, input, timeout))
            if "portal-matrix" not in command:
                return subprocess.CompletedProcess(command, 0, '{"phase":"validate"}\n', "")
            case_id = command[-2]
            phase = command[-1]
            assert input is not None
            private = json.loads(input)
            assert private["case_id"] == case_id
            output = {
                "schema_version": 1,
                "operation": "portal-matrix",
                "case_id": case_id,
                "phase": phase,
                "state": hil.CommandScenarioController.PORTAL_STATES[phase],
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(output), "")

        with tempfile.TemporaryDirectory() as temporary:
            helper = pathlib.Path(temporary) / "wifi-helper"
            helper.write_text("placeholder\n", encoding="utf-8")
            helper.chmod(0o700)
            with (
                mock.patch.object(hil.CommandScenarioController, "HELPER", str(helper)),
                mock.patch.object(hil.shutil, "which", return_value="/usr/bin/sudo"),
                mock.patch.object(hil.subprocess, "run", side_effect=run),
            ):
                controller = hil.CommandScenarioController(
                    portal_config, authorization_guard=authorization_guard()
                )
                for case_id in hil.PORTAL_CASE_SPEC:
                    for phase in ("apply", "test-plane", "reset"):
                        evidence = controller.run_portal_case(case_id, phase)
                        self.assertEqual(evidence["case_id"], case_id)
                        self.assertEqual(evidence["phase"], phase)

        portal_calls = [item for item in calls if "portal-matrix" in item[0]]
        self.assertEqual(len(portal_calls), len(hil.PORTAL_CASE_SPEC) * 4)
        private_values = {
            str(value)
            for case in portal_config["profile_cases"]
            for key, value in case["profile"].items()
            if key in {"ssid", "identity", "password"} and isinstance(value, str)
        }
        for command, private_input, timeout in portal_calls:
            argv_text = " ".join(command)
            self.assertTrue(all(value not in argv_text for value in private_values))
            self.assertIsNotNone(private_input)
            assert private_input is not None
            self.assertLessEqual(len(private_input.encode("utf-8")), 8 * 1024)
            self.assertIn(timeout, {90, 180, 240, 360})

    def test_portal_matrix_rejects_extra_private_profile_fields(self) -> None:
        config = hil.load_config(pathlib.Path(__file__).with_name("hil_config.example.json"))
        cases = config["portal_tests"]["profile_cases"]
        cases[0]["profile"]["unexpected"] = "not-allowed"
        with self.assertRaisesRegex(hil.HilError, "fixed coverage"):
            hil.PortalClient._validate_cases(cases)

    def test_target_secret_scan_sends_credentials_only_over_bounded_ssh_stdin(self) -> None:
        settings = hil.SshSettings(
            host="10.77.0.2",
            user="tidetracker",
            identity_file=pathlib.Path("/private/id"),
            known_hosts_file=pathlib.Path("/private/known_hosts"),
        )
        captured: dict[str, object] = {}

        def fake_run(command: list[str], **kwargs: object) -> object:
            captured["command"] = list(command)
            captured["input"] = kwargs.get("input")
            return types.SimpleNamespace(
                returncode=0,
                stdout=(
                    '{"event":"network_secret_artifact_scan","files_scanned":1,'
                    '"forbidden_matches":0,"passed":true,"schema_version":1}\n'
                ),
                stderr="",
            )

        secrets = ("enterprise-secret", "failed-profile-secret")
        with mock.patch.object(hil.subprocess, "run", side_effect=fake_run):
            result = hil.SshTarget(
                settings, authorization_guard=authorization_guard()
            ).network_secret_artifact_scan(secrets)

        command = captured["command"]
        self.assertIsInstance(command, list)
        command_text = " ".join(command)  # type: ignore[arg-type]
        self.assertIn("network-secret-artifact-scan", command_text)
        self.assertTrue(all(secret not in command_text for secret in secrets))
        self.assertEqual(
            json.loads(str(captured["input"])),
            {"schema_version": 1, "secrets": list(secrets)},
        )
        serialized_result = json.dumps(result)
        self.assertTrue(all(secret not in serialized_result for secret in secrets))

    def test_target_secret_scan_rejects_noncanonical_values_before_ssh(self) -> None:
        target = hil.SshTarget(
            hil.SshSettings(
                host="10.77.0.2",
                user="tidetracker",
                identity_file=pathlib.Path("/private/id"),
                known_hosts_file=pathlib.Path("/private/known_hosts"),
            ),
            authorization_guard=authorization_guard(),
        )
        with (
            mock.patch.object(hil.subprocess, "run") as run,
            self.assertRaisesRegex(hil.HilError, "secret scan input"),
        ):
            target.network_secret_artifact_scan(("z-secret", "a-secret"))
        run.assert_not_called()

    def test_target_ssh_uses_only_fixed_noninteractive_identity_policy(self) -> None:
        target = hil.SshTarget(
            hil.SshSettings(
                host="10.77.0.2",
                user="tidetracker",
                identity_file=hil.CONTROLLER_TO_ZERO_IDENTITY,
                known_hosts_file=hil.CONTROLLER_TO_ZERO_KNOWN_HOSTS,
            ),
            authorization_guard=authorization_guard(),
        )

        command = target._ssh_command("true")
        self.assertEqual(hil.FIXED_SSH.as_posix(), "/usr/bin/ssh")
        self.assertEqual(command[0], str(hil.FIXED_SSH))
        self.assertEqual(command[1:3], ["-F", "/dev/null"])
        rendered = " ".join(command)
        for option in (
            "BatchMode=yes",
            "IdentitiesOnly=yes",
            "IdentityAgent=none",
            "PreferredAuthentications=publickey",
            "AuthenticationMethods=publickey",
            "PubkeyAcceptedAlgorithms=ssh-ed25519",
            "PasswordAuthentication=no",
            "KbdInteractiveAuthentication=no",
            "GSSAPIAuthentication=no",
            "HostbasedAuthentication=no",
            "NumberOfPasswordPrompts=0",
            "GlobalKnownHostsFile=/dev/null",
            f"UserKnownHostsFile={hil.CONTROLLER_TO_ZERO_KNOWN_HOSTS}",
            "StrictHostKeyChecking=yes",
            "UpdateHostKeys=no",
            "HostKeyAlgorithms=ssh-ed25519",
            "ProxyCommand=none",
            "ProxyJump=none",
        ):
            self.assertIn(option, rendered)

    def test_known_hosts_rejects_any_record_beyond_exact_zero_ed25519(self) -> None:
        record = b"10.77.0.2 " + b" ".join(openssh_public_key(2).split()[:2]) + b"\n"
        self.assertEqual(len(hil._known_host_ed25519_der(record)), 44)
        for invalid in (
            record + record,
            record.replace(b"10.77.0.2", b"zero.local"),
            record.replace(b"ssh-ed25519", b"ssh-rsa"),
        ):
            with self.assertRaises(hil.HilError):
                hil._known_host_ed25519_der(invalid)

    def test_hil_reconstructs_all_five_installed_public_identities(self) -> None:
        keys = {
            "workstation_to_controller_ssh": openssh_public_key(1),
            "controller_to_zero_ssh": openssh_public_key(2),
            "application_release": pem_public_key(3),
            "lab_safety": pem_public_key(4),
            "image_release": pem_public_key(5),
        }
        policy = build_key_policy(**keys)

        self.assertEqual(
            hil._validate_reconstructed_key_policy(
                policy, str(policy["key_policy_sha256"]), keys
            ),
            policy["key_policy_sha256"],
        )

        changed = {**keys, "image_release": pem_public_key(9)}
        with self.assertRaisesRegex(hil.HilError, "pinned five-key policy"):
            hil._validate_reconstructed_key_policy(
                policy, str(policy["key_policy_sha256"]), changed
            )

    def test_private_pattern_scan_catches_json_escaped_and_unicode_values(self) -> None:
        private = 'private-"-p\u00e4ssword'
        payload = json.dumps({"password": private}, ensure_ascii=True).encode("utf-8")
        self.assertFalse(private.encode("utf-8") in payload)
        self.assertTrue(any(pattern in payload for pattern in hil._private_value_patterns((private,))))

    @unittest.skipIf(sys.platform == "win32", "POSIX key modes are enforced on the controller")
    def test_controller_ssh_material_requires_root_only_tidetracker_data(self) -> None:
        if os.geteuid() != 0:
            self.skipTest("actual root ownership is required")
        for executable in (hil.FIXED_SSH, hil.FIXED_SFTP, hil.FIXED_SSH_KEYGEN):
            if not executable.is_file():
                self.skipTest(f"controller executable unavailable: {executable}")
        with tempfile.TemporaryDirectory() as raw_temp:
            root = pathlib.Path(raw_temp) / "ssh"
            root.mkdir(mode=0o700)
            identity = root / "controller-to-zero-ed25519"
            known_hosts = root / "zero-known-hosts"
            identity.write_text("private-key-placeholder", encoding="ascii")
            controller_public = openssh_public_key(2)
            known_hosts.write_bytes(
                b"10.77.0.2 " + b" ".join(controller_public.split()[:2]) + b"\n"
            )
            identity.chmod(0o600)
            known_hosts.chmod(0o600)
            public_key = pathlib.Path(raw_temp) / "controller-zero.pub"
            public_key.write_bytes(controller_public)
            public_key.chmod(0o644)
            workstation_public = pathlib.Path(raw_temp) / "workstation-controller.pub"
            application_public = pathlib.Path(raw_temp) / "application-release.pub.pem"
            lab_public = pathlib.Path(raw_temp) / "lab-safety.pub.pem"
            image_public = pathlib.Path(raw_temp) / "image-release.pub.pem"
            workstation_public.write_bytes(openssh_public_key(1))
            application_public.write_bytes(pem_public_key(3))
            lab_public.write_bytes(pem_public_key(4))
            image_public.write_bytes(pem_public_key(5))
            for installed_public in (
                workstation_public,
                application_public,
                lab_public,
                image_public,
            ):
                installed_public.chmod(0o644)
            policy = build_key_policy(
                workstation_to_controller_ssh=openssh_public_key(1),
                controller_to_zero_ssh=controller_public,
                application_release=pem_public_key(3),
                lab_safety=pem_public_key(4),
                image_release=pem_public_key(5),
            )
            policy_file = pathlib.Path(raw_temp) / "key-policy.json"
            policy_hash_file = pathlib.Path(raw_temp) / "key-policy.sha256"
            policy_file.write_text(json.dumps(policy), encoding="ascii")
            policy_hash_file.write_text(f"{policy['key_policy_sha256']}\n", encoding="ascii")
            policy_file.chmod(0o644)
            policy_hash_file.chmod(0o644)
            findmnt = pathlib.Path(raw_temp) / "findmnt"
            findmnt.write_text("placeholder", encoding="ascii")
            findmnt.chmod(0o700)

            def completed(command: list[str], **kwargs: object) -> object:
                del kwargs
                if pathlib.Path(command[0]) == hil.FIXED_SSH_KEYGEN:
                    return subprocess.CompletedProcess(command, 0, controller_public.decode(), "")
                return subprocess.CompletedProcess(
                    command, 0, f"{hil.CONTROLLER_DATA_UUID} ext4 rw,noatime\n", ""
                )

            with (
                mock.patch.object(hil, "CONTROLLER_SSH_ROOT", root),
                mock.patch.object(hil, "CONTROLLER_TO_ZERO_IDENTITY", identity),
                mock.patch.object(hil, "CONTROLLER_TO_ZERO_KNOWN_HOSTS", known_hosts),
                mock.patch.object(
                    hil, "WORKSTATION_TO_CONTROLLER_PUBLIC_KEY", workstation_public
                ),
                mock.patch.object(hil, "CONTROLLER_TO_ZERO_PUBLIC_KEY", public_key),
                mock.patch.object(hil, "APPLICATION_RELEASE_PUBLIC_KEY", application_public),
                mock.patch.object(hil, "DEFAULT_SAFETY_PUBLIC_KEY", lab_public),
                mock.patch.object(hil, "IMAGE_RELEASE_PUBLIC_KEY", image_public),
                mock.patch.object(hil, "CONTROLLER_KEY_POLICY", policy_file),
                mock.patch.object(hil, "CONTROLLER_KEY_POLICY_SHA256", policy_hash_file),
                mock.patch.object(hil, "FIXED_FINDMNT", findmnt),
                mock.patch.object(hil.subprocess, "run", side_effect=completed) as run,
            ):
                digest = hil.validate_controller_ssh_material(identity, known_hosts)

            self.assertEqual(digest, policy["key_policy_sha256"])
            self.assertEqual(run.call_args.args[0][-1], str(root))

            application_public.write_bytes(pem_public_key(9))
            application_public.chmod(0o644)
            with (
                mock.patch.object(hil, "CONTROLLER_SSH_ROOT", root),
                mock.patch.object(hil, "CONTROLLER_TO_ZERO_IDENTITY", identity),
                mock.patch.object(hil, "CONTROLLER_TO_ZERO_KNOWN_HOSTS", known_hosts),
                mock.patch.object(
                    hil, "WORKSTATION_TO_CONTROLLER_PUBLIC_KEY", workstation_public
                ),
                mock.patch.object(hil, "CONTROLLER_TO_ZERO_PUBLIC_KEY", public_key),
                mock.patch.object(hil, "APPLICATION_RELEASE_PUBLIC_KEY", application_public),
                mock.patch.object(hil, "DEFAULT_SAFETY_PUBLIC_KEY", lab_public),
                mock.patch.object(hil, "IMAGE_RELEASE_PUBLIC_KEY", image_public),
                mock.patch.object(hil, "CONTROLLER_KEY_POLICY", policy_file),
                mock.patch.object(hil, "CONTROLLER_KEY_POLICY_SHA256", policy_hash_file),
                mock.patch.object(hil, "FIXED_FINDMNT", findmnt),
                mock.patch.object(hil.subprocess, "run", side_effect=completed),
                self.assertRaisesRegex(hil.HilError, "pinned five-key policy"),
            ):
                hil.validate_controller_ssh_material(identity, known_hosts)

    @unittest.skipIf(sys.platform == "win32", "POSIX key modes are enforced on the controller")
    def test_controller_ssh_material_rejects_group_readable_private_key(self) -> None:
        if os.geteuid() != 0:
            self.skipTest("actual root ownership is required")
        for executable in (hil.FIXED_SSH, hil.FIXED_SFTP, hil.FIXED_SSH_KEYGEN):
            if not executable.is_file():
                self.skipTest(f"controller executable unavailable: {executable}")
        with tempfile.TemporaryDirectory() as raw_temp:
            root = pathlib.Path(raw_temp) / "ssh"
            root.mkdir(mode=0o700)
            identity = root / "controller-to-zero-ed25519"
            known_hosts = root / "zero-known-hosts"
            identity.write_text("private-key-placeholder", encoding="ascii")
            known_hosts.write_bytes(
                b"10.77.0.2 " + b" ".join(openssh_public_key(2).split()[:2]) + b"\n"
            )
            identity.chmod(0o640)
            known_hosts.chmod(0o600)
            with (
                mock.patch.object(hil, "CONTROLLER_SSH_ROOT", root),
                mock.patch.object(hil, "CONTROLLER_TO_ZERO_IDENTITY", identity),
                mock.patch.object(hil, "CONTROLLER_TO_ZERO_KNOWN_HOSTS", known_hosts),
                self.assertRaisesRegex(hil.HilError, "private key.*0600"),
            ):
                hil.validate_controller_ssh_material(identity, known_hosts)


if __name__ == "__main__":
    unittest.main()
