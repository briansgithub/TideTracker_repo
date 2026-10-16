from __future__ import annotations

import base64
import importlib.util
import json
import pathlib
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "src"))
MODULE_PATH = pathlib.Path(__file__).with_name("evaluate_promotion.py")
SPEC = importlib.util.spec_from_file_location("evaluate_promotion", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)
BUNDLE_SHA = "c" * 64
IMAGE_SHA = "1" * 64
IMAGE_MANIFEST_SHA = "2" * 64
IMAGE_BUILD_ID = "e" * 64
KEY_POLICY_SHA = "d" * 64
RELEASE_KEY_SHA = "9" * 64
OPENSSL_BYTES = b"test-openssl-executable"
OPENSSL_SHA = promotion.hashlib.sha256(OPENSSL_BYTES).hexdigest()


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


def hardware_inventory() -> dict[str, dict[str, str]]:
    return {
        name: {"model": f"test-{name}-model", "serial_or_asset_id": f"asset-{name}"}
        for name in sorted(promotion.HARDWARE_INVENTORY_COMPONENTS)
    }


def tool_identities() -> dict[str, dict[str, object]]:
    identities: dict[str, dict[str, object]] = {}
    for index, name in enumerate(sorted(promotion.TOOL_IDENTITY_NAMES), start=1):
        executable = f"/opt/tidetracker/lab-tools/{name}"
        identities[name] = {
            "canonical_argv": [executable, "--evidence", "{output}"],
            "executable_path": executable,
            "executable_sha256": str(index) * 64,
            "executable_size_bytes": 1024 + index,
            "support_files": [
                {
                    "path": f"/etc/tidetracker/{name}.json",
                    "sha256": chr(ord("a") + index) * 64,
                    "size_bytes": 100 + index,
                }
            ],
        }
    return identities


def expected_environment_identity() -> dict[str, object]:
    return {
        "hardware_id": "fixture-1",
        "fixture_firmware_sha256": "4" * 64,
        "target_known_hosts_sha256": "5" * 64,
        "controller_machine_id_sha256": "6" * 64,
        "fixture_identity": {
            "hardware_id": "fixture-1",
            "firmware_sha256": "4" * 64,
        },
        "controller_artifact_policy": {
            **promotion.EXPECTED_CONTROLLER_ARTIFACT_POLICY
        },
        "hardware_inventory": hardware_inventory(),
        "tool_identities": tool_identities(),
        "image_identity": {
            "image_build_id": IMAGE_BUILD_ID,
            "image_sha256": IMAGE_SHA,
            "image_manifest_sha256": IMAGE_MANIFEST_SHA,
            "key_policy_sha256": KEY_POLICY_SHA,
        },
    }


@pytest.fixture(autouse=True)
def signed_report_verification(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = tmp_path / "approval.pub.pem"
    key.write_bytes(b"test-public-key")
    openssl = tmp_path / "openssl"
    openssl.write_bytes(OPENSSL_BYTES)
    monkeypatch.setattr(promotion, "DEFAULT_APPROVAL_PUBLIC_KEY", key)
    monkeypatch.setattr(promotion, "OPENSSL_PATH", openssl)
    monkeypatch.setattr(
        promotion.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
    )


def write_campaign(
    path: pathlib.Path,
    *,
    release_id: str,
    source: str,
    starts: list[datetime],
    cold_seconds: float,
    wifi_seconds: float = 2.0,
    fallback: bool = False,
) -> None:
    path.mkdir()
    cycles = []
    for index, started in enumerate(starts, start=1):
        run_id = f"{release_id}-{index}"
        image_bytes = f"display-{release_id}-{index}".encode()
        cycle_directory = path / f"cycle-{index:03d}"
        cycle_directory.mkdir()
        (cycle_directory / "display.jpg").write_bytes(image_bytes)
        metrics = {
            "run_id": run_id,
            "success": True,
            "release_id": release_id,
            "execution_source": source,
            "wake_to_done_seconds": 0.001,
            "wifi_active_seconds": 99.0,
            "wifi_active_observed_seconds": wifi_seconds,
            "cpu_user_seconds": 0.1,
            "cpu_system_seconds": 0.1,
            "max_rss_kib": 32_768,
            "process_write_bytes": 1_024,
            "display_refresh_count": 1,
            "swap_used_kib": 0,
        }
        execution = {
            "expected_release_id": release_id,
            "expected_execution_source": source,
            "attempts": [
                {
                    "release_id": release_id,
                    "execution_source": source,
                    "returncode": 0,
                }
            ],
            "trial_confirmed": False,
            "trial_rolled_back": False,
            "fallback_used": fallback,
            "selected_run_id": run_id,
            "selected_release_id": release_id,
            "selected_execution_source": source,
            "selected_success": True,
            "expected_release_executed_successfully": True,
        }
        cycles.append(
            {
                "number": index,
                "started": started.isoformat(),
                "outcome": "success",
                "cold_wake_to_done_seconds": cold_seconds,
                "wifi_power_budget_seconds": cold_seconds,
                "wifi_active_controller_seconds": wifi_seconds,
                "wifi_activity": {
                    "source": "controller_ap_station_poll",
                    "clock": "controller_monotonic_mapped_fixture_done",
                    "association_observed": True,
                    "observed_seconds": wifi_seconds,
                },
                "done": {
                    "event": "done",
                    "edge_us": index * 1_000_000,
                    "pulse_us": 500_000,
                },
                "camera": {
                    "capture": "complete",
                    "comparison": "accepted",
                    "image_sha256": promotion.hashlib.sha256(image_bytes).hexdigest(),
                    "image_size_bytes": len(image_bytes),
                    "capture_tool_identity": tool_identities()["camera_capture"],
                    "validation_tool_identity": tool_identities()["camera_validator"],
                },
                "rails_after_done": {"zero_5v_v": 0.0, "zero_3v3_v": 0.0},
                "target": {
                    "last_result": {
                        "run_id": run_id,
                        "success": True,
                        "release_id": release_id,
                        "execution_source": source,
                    },
                    "metrics": metrics,
                    "supervisor": {
                        "schema_version": 1,
                        "event": "release_supervision",
                        "execution": execution,
                    },
                    "swap_policy": {
                        "active_entries": 0,
                        "dphys_swapfile_masked": True,
                        "swapfile_absent": True,
                    },
                    "storage_policy": {
                        "root_overlay": True,
                        "boot_read_only": True,
                        "data_uuid": "54540000-0000-4000-8000-000000000002",
                        "data_read_write": True,
                        "networkmanager_bind_from_data": True,
                        "overlay_recurse": False,
                    },
                },
            }
        )
    session = path / "session.json"
    gate = {
        "signature": "verified",
        "gate_sha256": "f" * 64,
        "signature_sha256": "a" * 64,
        "approval_public_key_sha256": promotion.hashlib.sha256(
            promotion.DEFAULT_APPROVAL_PUBLIC_KEY.read_bytes()
        ).hexdigest(),
        "hardware_id": "fixture-1",
        "fixture_firmware_sha256": "4" * 64,
        "target_known_hosts_sha256": "5" * 64,
        "controller_machine_id_sha256": "6" * 64,
        "target_image_sha256": IMAGE_SHA,
        "target_image_manifest_sha256": IMAGE_MANIFEST_SHA,
        "target_image_build_id": IMAGE_BUILD_ID,
        "target_key_policy_sha256": KEY_POLICY_SHA,
        "hardware_inventory": hardware_inventory(),
        "tool_identities": tool_identities(),
        "approval_nonce": "approval_nonce_1234",
        "valid_until": "2030-01-01T00:00:00+00:00",
        "approved_operations": ["cycles"],
        "approved_update_power_cut_phases": [],
        "done_pulse": {
            "minimum_us": 400_000,
            "maximum_us": 600_000,
            "shortened_timing_hardware_validated": False,
        },
        "measurements": {
            "zero_5v_off_usb_connected_v": 0.0,
            "zero_3v3_off_all_sense_connected_v": 0.0,
            "zero_5v_on_under_load_v": 5.0,
        },
    }
    session.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "outcome": "success",
                "simulated": False,
                "started": starts[0].isoformat(),
                "finished": (starts[-1] + timedelta(seconds=1)).isoformat(),
                "safety_gate": gate,
                "fixture_identity": {
                    "hardware_id": "fixture-1",
                    "firmware_sha256": "4" * 64,
                },
                "controller_machine_id_sha256": "6" * 64,
                "controller_artifact_policy": {
                    **promotion.EXPECTED_CONTROLLER_ARTIFACT_POLICY
                },
                "hardware_inventory": hardware_inventory(),
                "tool_identities": tool_identities(),
                "initial_rail_isolation": {"zero_5v_v": 0.0, "zero_3v3_v": 0.0},
                "target_identity": {
                    "release_id": release_id,
                    "execution_source": source,
                    "image_build_id": IMAGE_BUILD_ID,
                    "image_sha256": IMAGE_SHA,
                    "image_manifest_sha256": IMAGE_MANIFEST_SHA,
                    "key_policy_sha256": KEY_POLICY_SHA,
                    "attestation": "verified_signed_safety_gate",
                },
                "expected_release_id": release_id,
                "expected_execution_source": source,
                **({"bundle": {"sha256": BUNDLE_SHA}} if source == "verified-release" else {}),
                "cycles": cycles,
            }
        ),
        encoding="utf-8",
    )
    session.with_name("session.json.sig").write_bytes(b"test-signature")


def write_legacy_report(path: pathlib.Path, key: pathlib.Path) -> None:
    digest = "a" * 64
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "event": "legacy_framebuffer_verification",
                "manifest_sha256": "b" * 64,
                "capture_manifest_signature_sha256": "d" * 64,
                "capture_approval_public_key_sha256": promotion.hashlib.sha256(
                    key.read_bytes()
                ).hexdigest(),
                "candidate_release_id": "candidate-v2",
                "candidate_bundle_sha256": BUNDLE_SHA,
                "candidate_release_public_key_sha256": RELEASE_KEY_SHA,
                "candidate_manifest_sha256": "e" * 64,
                "candidate_verification": "signed_bundle_subprocess",
                "fixture_count": 1,
                "matched_count": 1,
                "passed": True,
                "status": "passed",
                "results": [
                    {
                        "fixture_id": "approved-legacy-capture-1",
                        "matched": True,
                        "expected_sha256": digest,
                        "actual_sha256": digest,
                        "actual_bytes": 48_000,
                    }
                ],
                "failures": [],
            }
        ),
        encoding="utf-8",
    )
    path.with_name(f"{path.name}.sig").write_bytes(b"test-signature")


def configure_report_verification(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> pathlib.Path:
    key = tmp_path / "approval.pub.pem"
    key.write_bytes(b"test-public-key")
    monkeypatch.setattr(promotion, "DEFAULT_APPROVAL_PUBLIC_KEY", key)
    monkeypatch.setattr(
        promotion.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
    )
    monkeypatch.setattr(
        promotion,
        "load_image_manifest",
        lambda *args, **kwargs: {
            "verification": "test",
            "application_release_public_key_sha256": RELEASE_KEY_SHA,
            "key_policy_sha256": KEY_POLICY_SHA,
        },
    )
    monkeypatch.setattr(
        promotion,
        "load_release_evidence",
        lambda *args, **kwargs: {
            "verification": "test",
            "environment_identity": expected_environment_identity(),
        },
    )
    return key


def write_camera(path: pathlib.Path, payload: bytes) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return {
        "capture": "complete",
        "comparison": "accepted",
        "image_sha256": promotion.hashlib.sha256(payload).hexdigest(),
        "image_size_bytes": len(payload),
        "capture_tool_identity": tool_identities()["camera_capture"],
        "validation_tool_identity": tool_identities()["camera_validator"],
    }


def usb_disconnected_evidence() -> dict[str, object]:
    return {
        "usb_management": "disconnected",
        "probe_exit_code": 0,
        "probe_tool_identity": tool_identities()["usb_disconnected_probe"],
    }


def write_uart(
    path: pathlib.Path,
    *,
    required_event: str | None = None,
    required_phase: str | None = None,
    required_release_id: str | None = None,
    required_bundle_sha256: str | None = None,
) -> dict[str, object]:
    event = required_event or "phase_started"
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "event": event,
                **({"phase": required_phase} if required_phase else {}),
                **({"release_id": required_release_id} if required_release_id else {}),
                **(
                    {"bundle_sha256": required_bundle_sha256}
                    if required_bundle_sha256
                    else {}
                ),
            }
        )
        + "\n"
    ).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return {
        "size_bytes": len(payload),
        "sha256": promotion.hashlib.sha256(payload).hexdigest(),
        "schema1_event_count": 1,
        "event_names": [event],
        **({"required_event": required_event} if required_event else {}),
        **({"required_phase": required_phase} if required_phase else {}),
        **({"required_release_id": required_release_id} if required_release_id else {}),
        **(
            {"required_bundle_sha256": required_bundle_sha256}
            if required_bundle_sha256
            else {}
        ),
        **(
            {"runtime_phase_matched": True}
            if required_phase is not None and required_event is None
            else {}
        ),
    }


def clone_cycle(
    template: dict[str, object],
    *,
    camera_path: pathlib.Path,
    name: str,
) -> dict[str, object]:
    cycle = json.loads(json.dumps(template))
    cycle["camera"] = write_camera(camera_path, name.encode())
    return cycle


def write_complete_release_evidence(
    root: pathlib.Path,
) -> tuple[pathlib.Path, pathlib.Path]:
    origin = datetime(2026, 8, 26, tzinfo=UTC)
    combined = root / "combined-evidence"
    write_campaign(
        combined,
        release_id="candidate-v2",
        source="verified-release",
        starts=[origin + timedelta(seconds=index) for index in range(12)],
        cold_seconds=9.0,
    )
    session_path = combined / "session.json"
    session = json.loads(session_path.read_text(encoding="utf-8"))
    template = session["cycles"][0]
    session.update(
        {
            "cycles_requested": 12,
            "accelerated_cycle_campaign": True,
            "accelerated_cycles_required": 12,
            "wifi_matrix_requested": True,
            "power_cut_phases": sorted(promotion.LIFECYCLE_PHASES),
            "update_power_cut_phases": sorted(promotion.UPDATE_POWER_CUT_PHASES),
            "fixture_identity": {
                "hardware_id": "fixture-1",
                "firmware_sha256": "4" * 64,
            },
            "initial_rail_isolation": {"zero_5v_v": 0.0, "zero_3v3_v": 0.0},
            "bundle": {
                "sha256": BUNDLE_SHA,
                "transfer": "complete",
                "stage": {"release_id": "candidate-v2", "state": "trial"},
            },
        }
    )
    session["safety_gate"] = {
        **session["safety_gate"],
        "approved_operations": [
            "cycles",
            "wifi_matrix",
            "lifecycle_power_cuts",
            "update_power_cuts",
        ],
        "approved_update_power_cut_phases": sorted(promotion.UPDATE_POWER_CUT_PHASES),
    }
    lifecycle = []
    for phase in sorted(promotion.LIFECYCLE_PHASES):
        directory = combined / f"power-cut-{phase}"
        recovery = clone_cycle(
            template,
            camera_path=directory / "recovery-display.jpg",
            name=f"lifecycle-{phase}",
        )
        lifecycle.append(
            {
                "phase": phase,
                "outcome": "success",
                "cut": {"event": "phase_power_cut", "phase": phase},
                "rails_after_cut": {"zero_5v_v": 0.0, "zero_3v3_v": 0.0},
                "cut_uart": write_uart(directory / "cut-uart.log", required_phase=phase),
                "recovery_done": recovery["done"],
                "recovery_uart": write_uart(directory / "recovery-uart.log"),
                "rails_after_recovery": {"zero_5v_v": 0.0, "zero_3v3_v": 0.0},
                "camera": recovery["camera"],
                "target": recovery["target"],
            }
        )
    session["power_cut_recoveries"] = lifecycle
    expected_states = {
        "begin": "uploading",
        "stage": "staged",
        "verify": "verified",
        "activate": "trial",
    }
    updates = []
    for phase in sorted(promotion.UPDATE_POWER_CUT_PHASES):
        directory = combined / f"update-power-cut-{phase}"
        updates.append(
            {
                "phase": phase,
                "release_id": "candidate-v2",
                "bundle_sha256": BUNDLE_SHA,
                "outcome": "success",
                "cut": {"event": "update_power_cut", "phase": phase},
                "rails_after_cut": {"zero_5v_v": 0.0, "zero_3v3_v": 0.0},
                "rails_after_recovery": {"zero_5v_v": 0.0, "zero_3v3_v": 0.0},
                "camera": write_camera(
                    directory / "recovery-display.jpg", f"update-{phase}".encode()
                ),
                "transport": {
                    "ssh_interrupted_by_power_cut": True,
                    "ssh_returncode": 255,
                    "stdout_sha256": "c" * 64,
                    "stderr_sha256": "d" * 64,
                },
                "cut_uart": write_uart(
                    directory / "cut-uart.log",
                    required_event="update_phase_boundary",
                    required_phase=phase,
                    required_release_id="candidate-v2",
                    required_bundle_sha256=BUNDLE_SHA,
                ),
                "target_update_status": {
                    "schema_version": 1,
                    "event": "update_power_cut_recovery",
                    "phase": phase,
                    "release_id": "candidate-v2",
                    "bundle_sha256": BUNDLE_SHA,
                    "recovered": True,
                    "state_valid": True,
                    "observed_state": expected_states[phase],
                    "cleanup_complete": True,
                    "post_cleanup_clean": True,
                    "fallback_used": False,
                },
                "cold_setup_recovery": "reachable-over-usb",
            }
        )
    session["update_power_cut_recoveries"] = updates
    portal = []
    for case_id, (security, hidden, operation) in promotion.PORTAL_CASE_SPEC.items():
        ssid_hash = promotion.hashlib.sha256(case_id.encode()).hexdigest()
        request = {
            "case_id": case_id,
            "operation": operation,
            "http_status": 202,
            "ack_before_radio_change": True,
            "operation_id_sha256": "5" * 64,
            "ssid_sha256": ssid_hash,
            "security": security,
            "hidden": hidden,
        }
        portal.append(
            {
                "case_id": case_id,
                "outcome": "success",
                "usb_ssh_during_portal": "reachable",
                "usb_ssh_after_radio_change": "reachable",
                "portal_reachable": {"status": "reachable", "http_status": 200},
                "controller_portal_apply": {
                    "schema_version": 1,
                    "operation": "portal-matrix",
                    "case_id": case_id,
                    "scenario": "client_ap_transition",
                    "phase": "apply",
                    "state": "portal-client-active",
                    "duration_seconds": 1.0,
                },
                "controller_test_plane": {
                    "schema_version": 1,
                    "operation": "portal-matrix",
                    "case_id": case_id,
                    "scenario": "client_ap_transition",
                    "phase": "test-plane",
                    "state": "test-plane-active",
                    "duration_seconds": 1.0,
                },
                "controller_portal_reset": {
                    "schema_version": 1,
                    "operation": "portal-matrix",
                    "case_id": case_id,
                    "scenario": "client_ap_transition",
                    "phase": "reset",
                    "state": "normal-ap-restored",
                    "duration_seconds": 1.0,
                },
                "transaction_coverage": {
                    "test_proven": operation == "test",
                    "save_proven": True,
                    "force_save_proven": operation == "force-save",
                },
                "request": request,
                "networkmanager_profile": {
                    "schema_version": 1,
                    "event": "network_profile_evidence",
                    "case_id": case_id,
                    "ssid_sha256": ssid_hash,
                    "security": security,
                    "hidden": hidden,
                    "operation": operation,
                    "operation_state": "succeeded",
                    "profile_mode": "0600",
                    "profile_owner": "root",
                    "profile_group": "root",
                    "networkmanager_loaded": True,
                },
            }
        )
    portal.append(
        {
            "case_id": "failed_credentials_rollback",
            "outcome": "success",
            "usb_ssh_before_transaction": "reachable",
            "usb_ssh_after_rollback": "reachable",
            "portal": {
                "http_status": 202,
                "ack_before_radio_change": True,
                "operation_id_sha256": "e" * 64,
                "operation": "failed-and-rolled-back",
                "secret_recorded": False,
            },
            "controller_portal_apply": {
                "scenario": "client_ap_transition",
                "phase": "apply",
            },
            "controller_portal_reset": {
                "scenario": "client_ap_transition",
                "phase": "reset",
            },
            "networkmanager_rollback": {
                "schema_version": 1,
                "event": "network_rollback_evidence",
                "ssid_sha256": "6" * 64,
                "profile_absent": True,
                "active_profile_restored": True,
            },
        }
    )
    session["portal_matrix"] = portal
    session["portal_secret_artifact_scan"] = {
        "target": {
            "schema_version": 1,
            "event": "network_secret_artifact_scan",
            "passed": True,
            "files_scanned": 10,
            "forbidden_matches": 0,
        },
        "controller_artifacts": {
            "passed": True,
            "files_scanned": 10,
            "forbidden_matches": 0,
            "method": "exact_private_config_credential_match",
        },
    }
    wifi: list[dict[str, object]] = [
        {
            "scenario": "client_ap_transition",
            "outcome": "success",
            "profile_cases_completed": len(promotion.PORTAL_CASE_SPEC),
            "usb_management_reachable": True,
        },
        {
            "scenario": "bad_credentials",
            "outcome": "success",
            "rollback_case_id": "failed_credentials_rollback",
            "rollback_proven": True,
        },
    ]
    faults = {
        "wifi_disconnect": None,
        "dns_failure": "dns",
        "tls_failure": "tls",
        "http_failure": "http_503",
    }
    for scenario, fault in faults.items():
        fault_cycle = clone_cycle(
            template,
            camera_path=combined / f"wifi-matrix-{scenario}-fault" / "display.jpg",
            name=f"{scenario}-fault",
        )
        recovery_cycle = clone_cycle(
            template,
            camera_path=combined / f"wifi-matrix-{scenario}-recovery" / "display.jpg",
            name=f"{scenario}-recovery",
        )
        fault_cycle["usb_ssh_while_faulted_run"] = "reachable"
        recovery_cycle["usb_ssh_while_faulted_run"] = "reachable"
        fault_cycle["uart"] = write_uart(
            combined / f"wifi-matrix-{scenario}-fault" / "uart.log"
        )
        recovery_cycle["uart"] = write_uart(
            combined / f"wifi-matrix-{scenario}-recovery" / "uart.log"
        )
        fault_target = cast(dict[str, object], fault_cycle["target"])
        fault_result = cast(dict[str, object], fault_target["last_result"])
        fault_result["used_cached_data"] = True
        wifi_controls: dict[str, dict[str, object]]
        if fault is None:
            fault_result["production_route_interface"] = None
            wifi_controls = {
                phase: {"scenario": scenario, "phase": phase}
                for phase in ("apply", "probe", "reset")
            }
        else:
            fault_result["production_route_interface"] = "wlan0"
            fault_result["injected_noaa_fault"] = fault
            wifi_controls = {
                "apply": {"pending": True, "kind": fault},
                "probe": {"pending": True, "kind": fault},
                "reset": {"pending": False, "kind": None},
            }
        recovery_target = cast(dict[str, object], recovery_cycle["target"])
        recovery_result = cast(dict[str, object], recovery_target["last_result"])
        recovery_result["network_interface"] = "wlan0"
        recovery_result["production_route_interface"] = "wlan0"
        wifi.append(
            {
                "scenario": scenario,
                "outcome": "success",
                "fault_cycle": fault_cycle,
                "recovery_cycle": recovery_cycle,
                **wifi_controls,
            }
        )
    session["wifi_matrix"] = wifi
    session_path.write_text(json.dumps(session), encoding="utf-8")
    session_path.with_name("session.json.sig").write_bytes(b"signature")

    field = root / "field-evidence"
    field.mkdir()
    field_camera = write_camera(field / "field-cycle" / "display.jpg", b"field-display")
    field_uart = write_uart(field / "field-cycle" / "uart.log")
    field_uart["target_identity"] = {
        "release_id": "candidate-v2",
        "execution_source": "verified-release",
        "image_build_id": IMAGE_BUILD_ID,
        "key_policy_sha256": KEY_POLICY_SHA,
        "matched_signed_target_identity": True,
    }
    field_session = {
        "schema_version": 1,
        "outcome": "success",
        "simulated": False,
        "started": origin.isoformat(),
        "finished": (origin + timedelta(seconds=1)).isoformat(),
        "expected_release_id": "candidate-v2",
        "expected_execution_source": "verified-release",
        "target_identity": session["target_identity"],
        "fixture_identity": session["fixture_identity"],
        "controller_machine_id_sha256": session["controller_machine_id_sha256"],
        "controller_artifact_policy": session["controller_artifact_policy"],
        "hardware_inventory": session["hardware_inventory"],
        "tool_identities": session["tool_identities"],
        "initial_rail_isolation": session["initial_rail_isolation"],
        "safety_gate": {
            **session["safety_gate"],
            "approved_operations": ["field_cycle"],
            "approved_update_power_cut_phases": [],
        },
        "field_cycle_requested": True,
        "field_cycle": {
            "outcome": "success",
            "usb_before_power": usb_disconnected_evidence(),
            "usb_while_powered": usb_disconnected_evidence(),
            "usb_after_power": usb_disconnected_evidence(),
            "done": {"event": "done", "edge_us": 1, "pulse_us": 500_000},
            "rails_after_done": {"zero_5v_v": 0.0, "zero_3v3_v": 0.0},
            "camera": field_camera,
            "uart": field_uart,
        },
    }
    (field / "session.json").write_text(json.dumps(field_session), encoding="utf-8")
    (field / "session.json.sig").write_bytes(b"signature")
    return combined, field


def test_promotion_requires_interleaved_fixture_and_radio_metrics(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = datetime(2026, 8, 25, tzinfo=UTC)
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    output = tmp_path / "decision.json"
    legacy_report = tmp_path / "legacy-report.json"
    write_legacy_report(legacy_report, configure_report_verification(tmp_path, monkeypatch))
    write_campaign(
        baseline,
        release_id="built-in",
        source="built-in",
        starts=[origin + timedelta(seconds=index * 2) for index in range(20)],
        cold_seconds=10.0,
        wifi_seconds=3.0,
    )
    write_campaign(
        candidate,
        release_id="candidate-v2",
        source="verified-release",
        starts=[origin + timedelta(seconds=index * 2 + 1) for index in range(20)],
        cold_seconds=9.0,
        wifi_seconds=2.0,
    )

    code = promotion.main(
        [
            "--baseline",
            str(baseline),
            "--candidate",
            str(candidate),
            "--release-evidence",
            str(tmp_path / "release-evidence"),
            "--expected-baseline-release-id",
            "built-in",
            "--expected-baseline-execution-source",
            "built-in",
            "--expected-candidate-release-id",
            "candidate-v2",
            "--expected-candidate-execution-source",
            "verified-release",
            "--expected-candidate-bundle-sha256",
            BUNDLE_SHA,
            "--output",
            str(output),
            "--legacy-report",
            str(legacy_report),
            "--image-manifest",
            str(tmp_path / "zero.img.manifest.json"),
            "--image-release-public-key",
            str(tmp_path / "image-release.pub.pem"),
            "--workstation-to-controller-ssh-public-key",
            str(tmp_path / "workstation-controller.pub"),
            "--controller-to-zero-ssh-public-key",
            str(tmp_path / "controller-zero.pub"),
            "--application-release-public-key",
            str(tmp_path / "application-release.pub.pem"),
            "--lab-safety-public-key",
            str(tmp_path / "lab-safety.pub.pem"),
            "--expected-image-release-public-key-sha256",
            "3" * 64,
            "--expected-image-manifest-sha256",
            IMAGE_MANIFEST_SHA,
            "--expected-image-sha256",
            IMAGE_SHA,
            "--expected-image-build-id",
            IMAGE_BUILD_ID,
            "--expected-openssl-sha256",
            OPENSSL_SHA,
        ]
    )

    assert code == 0
    decision = json.loads(output.read_text(encoding="utf-8"))
    assert decision["campaign_integrity"]["alternating"] is True
    assert decision["campaign_integrity"]["promotion_verifier"] == {
        "path": str(promotion.OPENSSL_PATH),
        "sha256": OPENSSL_SHA,
    }
    assert decision["campaign_integrity"]["primary_wake_metric"] == (
        "fixture_cold_wake_to_done_seconds"
    )
    assert decision["campaign_integrity"]["primary_wifi_metric"] == (
        "controller_observed_wifi_association_seconds"
    )
    assert decision["wifi_active"]["baseline_median"] == 3_000
    assert decision["wifi_active"]["candidate_median"] == 2_000


def test_promotion_rejects_fallback_evidence(tmp_path: pathlib.Path) -> None:
    campaign = tmp_path / "fallback"
    write_campaign(
        campaign,
        release_id="candidate-v2",
        source="verified-release",
        starts=[datetime(2026, 8, 25, tzinfo=UTC)],
        cold_seconds=9.0,
        fallback=True,
    )

    with pytest.raises(promotion.CampaignError, match="fallback"):
        promotion.load_campaign(
            campaign,
            expected_release_id="candidate-v2",
            expected_execution_source="verified-release",
            expected_openssl_sha256=OPENSSL_SHA,
        )


def test_promotion_rejects_session_finished_after_authorization_expiry(
    tmp_path: pathlib.Path,
) -> None:
    started = datetime(2026, 8, 25, tzinfo=UTC)
    campaign = tmp_path / "expired-authorization"
    write_campaign(
        campaign,
        release_id="candidate-v2",
        source="verified-release",
        starts=[started],
        cold_seconds=9.0,
    )
    session_path = campaign / "session.json"
    session = json.loads(session_path.read_text(encoding="utf-8"))
    session["safety_gate"]["valid_until"] = (started + timedelta(milliseconds=500)).isoformat()
    session_path.write_text(json.dumps(session), encoding="utf-8")

    with pytest.raises(promotion.CampaignError, match="did not finish within"):
        promotion.load_campaign(
            campaign,
            expected_release_id="candidate-v2",
            expected_execution_source="verified-release",
            expected_bundle_sha256=BUNDLE_SHA,
            expected_openssl_sha256=OPENSSL_SHA,
        )


def test_promotion_rejects_campaigns_from_different_controllers(
    tmp_path: pathlib.Path,
) -> None:
    started = datetime(2026, 8, 25, tzinfo=UTC)
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    write_campaign(
        baseline,
        release_id="built-in",
        source="built-in",
        starts=[started],
        cold_seconds=10.0,
    )
    write_campaign(
        candidate,
        release_id="candidate-v2",
        source="verified-release",
        starts=[started + timedelta(seconds=1)],
        cold_seconds=9.0,
    )
    candidate_session = candidate / "session.json"
    value = json.loads(candidate_session.read_text(encoding="utf-8"))
    value["controller_machine_id_sha256"] = "7" * 64
    value["safety_gate"]["controller_machine_id_sha256"] = "7" * 64
    candidate_session.write_text(json.dumps(value), encoding="utf-8")
    records: list[dict[str, object]] = []
    promotion.load_campaign(
        baseline,
        expected_release_id="built-in",
        expected_execution_source="built-in",
        expected_openssl_sha256=OPENSSL_SHA,
        evidence_records=records,
    )
    promotion.load_campaign(
        candidate,
        expected_release_id="candidate-v2",
        expected_execution_source="verified-release",
        expected_bundle_sha256=BUNDLE_SHA,
        expected_openssl_sha256=OPENSSL_SHA,
        evidence_records=records,
    )

    with pytest.raises(promotion.CampaignError, match="identical accepted lab system"):
        promotion._require_consistent_environment(records, "test campaigns")


def test_promotion_rejects_non_alternating_order(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = datetime(2026, 8, 25, tzinfo=UTC)
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    legacy_report = tmp_path / "legacy-report.json"
    write_legacy_report(legacy_report, configure_report_verification(tmp_path, monkeypatch))
    write_campaign(
        baseline,
        release_id="built-in",
        source="built-in",
        starts=[origin, origin + timedelta(seconds=1)],
        cold_seconds=10.0,
    )
    write_campaign(
        candidate,
        release_id="candidate-v2",
        source="verified-release",
        starts=[origin + timedelta(seconds=2), origin + timedelta(seconds=3)],
        cold_seconds=9.0,
    )

    with pytest.raises(promotion.CampaignError, match="alternating"):
        promotion.main(
            [
                "--baseline",
                str(baseline),
                "--candidate",
                str(candidate),
                "--release-evidence",
                str(tmp_path / "release-evidence"),
                "--expected-baseline-release-id",
                "built-in",
                "--expected-baseline-execution-source",
                "built-in",
                "--expected-candidate-release-id",
                "candidate-v2",
                "--expected-candidate-execution-source",
                "verified-release",
                "--expected-candidate-bundle-sha256",
                BUNDLE_SHA,
                "--legacy-report",
                str(legacy_report),
                "--image-manifest",
                str(tmp_path / "zero.img.manifest.json"),
                "--image-release-public-key",
                str(tmp_path / "image-release.pub.pem"),
                "--workstation-to-controller-ssh-public-key",
                str(tmp_path / "workstation-controller.pub"),
                "--controller-to-zero-ssh-public-key",
                str(tmp_path / "controller-zero.pub"),
                "--application-release-public-key",
                str(tmp_path / "application-release.pub.pem"),
                "--lab-safety-public-key",
                str(tmp_path / "lab-safety.pub.pem"),
                "--expected-image-release-public-key-sha256",
                "3" * 64,
                "--expected-image-manifest-sha256",
                IMAGE_MANIFEST_SHA,
                "--expected-image-sha256",
                IMAGE_SHA,
                "--expected-image-build-id",
                IMAGE_BUILD_ID,
                "--expected-openssl-sha256",
                OPENSSL_SHA,
            ]
        )


def test_signed_json_verifies_and_parses_the_same_private_snapshot(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "session.json"
    original = b'{"schema_version":1,"outcome":"success"}'
    report.write_bytes(original)
    report.with_name("session.json.sig").write_bytes(b"signature")
    observed: dict[str, bytes | str] = {}

    def verify(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args[0] == str(promotion.OPENSSL_PATH)
        signed_snapshot = pathlib.Path(args[args.index("-in") + 1])
        signature_snapshot = pathlib.Path(args[args.index("-sigfile") + 1])
        key_snapshot = pathlib.Path(args[args.index("-inkey") + 1])
        assert signed_snapshot != report
        assert signature_snapshot != report.with_name("session.json.sig")
        assert key_snapshot != promotion.DEFAULT_APPROVAL_PUBLIC_KEY
        observed["document"] = signed_snapshot.read_bytes()
        observed["signature"] = signature_snapshot.read_bytes()
        observed["key"] = key_snapshot.read_bytes()
        report.write_bytes(b'{"schema_version":999}')
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(promotion.subprocess, "run", verify)
    signed = promotion._load_signed_json(
        report,
        label="test session",
        expected_openssl_sha256=OPENSSL_SHA,
    )

    assert observed == {
        "document": original,
        "signature": b"signature",
        "key": b"test-public-key",
    }
    assert signed.value == {"schema_version": 1, "outcome": "success"}
    assert signed.sha256 == promotion.hashlib.sha256(original).hexdigest()


def test_signed_json_rejects_nested_duplicate_fields(tmp_path: pathlib.Path) -> None:
    report = tmp_path / "session.json"
    report.write_text('{"outer":{"phase":"begin","Phase":"activate"}}', encoding="utf-8")
    report.with_name("session.json.sig").write_bytes(b"signature")

    with pytest.raises(promotion.CampaignError, match="repeats field: Phase"):
        promotion._load_signed_json(
            report,
            label="test session",
            expected_openssl_sha256=OPENSSL_SHA,
        )


def test_signed_json_rejects_unallowlisted_openssl(tmp_path: pathlib.Path) -> None:
    report = tmp_path / "session.json"
    report.write_text('{"schema_version":1}', encoding="utf-8")
    report.with_name("session.json.sig").write_bytes(b"signature")

    with pytest.raises(promotion.CampaignError, match="OpenSSL executable is not allowlisted"):
        promotion._load_signed_json(
            report,
            label="test session",
            expected_openssl_sha256="0" * 64,
        )


def test_release_evidence_requires_every_signed_hardware_gate(tmp_path: pathlib.Path) -> None:
    combined, field = write_complete_release_evidence(tmp_path)

    result = promotion.load_release_evidence(
        (combined, field),
        expected_release_id="candidate-v2",
        expected_execution_source="verified-release",
        expected_bundle_sha256=BUNDLE_SHA,
        expected_image_build_id=IMAGE_BUILD_ID,
        expected_image_sha256=IMAGE_SHA,
        expected_image_manifest_sha256=IMAGE_MANIFEST_SHA,
        expected_key_policy_sha256=KEY_POLICY_SHA,
        expected_openssl_sha256=OPENSSL_SHA,
    )

    assert result["lifecycle_power_cut_phases"] == sorted(promotion.LIFECYCLE_PHASES)
    assert result["update_power_cut_phases"] == sorted(promotion.UPDATE_POWER_CUT_PHASES)
    assert result["accelerated_cold_cycles"] == 12
    assert result["field_usb_disconnected"] is True
    assert len(result["signed_sessions"]) == 2


def test_release_evidence_rejects_mutated_camera_artifact(tmp_path: pathlib.Path) -> None:
    combined, field = write_complete_release_evidence(tmp_path)
    (field / "field-cycle" / "display.jpg").write_bytes(b"substituted-photo")

    with pytest.raises(promotion.CampaignError, match="differs from signed evidence"):
        promotion.load_release_evidence(
            (combined, field),
            expected_release_id="candidate-v2",
            expected_execution_source="verified-release",
            expected_bundle_sha256=BUNDLE_SHA,
            expected_image_build_id=IMAGE_BUILD_ID,
            expected_image_sha256=IMAGE_SHA,
            expected_image_manifest_sha256=IMAGE_MANIFEST_SHA,
            expected_key_policy_sha256=KEY_POLICY_SHA,
            expected_openssl_sha256=OPENSSL_SHA,
        )


def test_release_evidence_rejects_done_pulse_outside_signed_bounds(
    tmp_path: pathlib.Path,
) -> None:
    combined, field = write_complete_release_evidence(tmp_path)
    session_path = combined / "session.json"
    session = json.loads(session_path.read_text(encoding="utf-8"))
    session["cycles"][0]["done"]["pulse_us"] = 399_999
    session_path.write_text(json.dumps(session), encoding="utf-8")

    with pytest.raises(promotion.CampaignError, match="DONE pulse within signed bounds"):
        promotion.load_release_evidence(
            (combined, field),
            expected_release_id="candidate-v2",
            expected_execution_source="verified-release",
            expected_bundle_sha256=BUNDLE_SHA,
            expected_image_build_id=IMAGE_BUILD_ID,
            expected_image_sha256=IMAGE_SHA,
            expected_image_manifest_sha256=IMAGE_MANIFEST_SHA,
            expected_key_policy_sha256=KEY_POLICY_SHA,
            expected_openssl_sha256=OPENSSL_SHA,
        )


def test_release_evidence_rejects_camera_tool_substitution(tmp_path: pathlib.Path) -> None:
    combined, field = write_complete_release_evidence(tmp_path)
    session_path = field / "session.json"
    session = json.loads(session_path.read_text(encoding="utf-8"))
    substituted = dict(session["field_cycle"]["camera"]["capture_tool_identity"])
    substituted.update(
        {
            "canonical_argv": ["/bin/true"],
            "executable_path": "/bin/true",
            "executable_sha256": "0" * 64,
        }
    )
    session["field_cycle"]["camera"]["capture_tool_identity"] = substituted
    session_path.write_text(json.dumps(session), encoding="utf-8")

    with pytest.raises(promotion.CampaignError, match="physical display comparison"):
        promotion.load_release_evidence(
            (combined, field),
            expected_release_id="candidate-v2",
            expected_execution_source="verified-release",
            expected_bundle_sha256=BUNDLE_SHA,
            expected_image_build_id=IMAGE_BUILD_ID,
            expected_image_sha256=IMAGE_SHA,
            expected_image_manifest_sha256=IMAGE_MANIFEST_SHA,
            expected_key_policy_sha256=KEY_POLICY_SHA,
            expected_openssl_sha256=OPENSSL_SHA,
        )


def test_release_evidence_rejects_usb_probe_tool_substitution(tmp_path: pathlib.Path) -> None:
    combined, field = write_complete_release_evidence(tmp_path)
    session_path = field / "session.json"
    session = json.loads(session_path.read_text(encoding="utf-8"))
    substituted = dict(
        session["field_cycle"]["usb_before_power"]["probe_tool_identity"]
    )
    substituted.update(
        {
            "canonical_argv": ["/bin/true"],
            "executable_path": "/bin/true",
            "executable_sha256": "0" * 64,
        }
    )
    session["field_cycle"]["usb_before_power"]["probe_tool_identity"] = substituted
    session_path.write_text(json.dumps(session), encoding="utf-8")

    with pytest.raises(promotion.CampaignError, match="USB disconnected"):
        promotion.load_release_evidence(
            (combined, field),
            expected_release_id="candidate-v2",
            expected_execution_source="verified-release",
            expected_bundle_sha256=BUNDLE_SHA,
            expected_image_build_id=IMAGE_BUILD_ID,
            expected_image_sha256=IMAGE_SHA,
            expected_image_manifest_sha256=IMAGE_MANIFEST_SHA,
            expected_key_policy_sha256=KEY_POLICY_SHA,
            expected_openssl_sha256=OPENSSL_SHA,
        )


def test_environment_consistency_includes_each_hardware_asset(tmp_path: pathlib.Path) -> None:
    started = datetime(2026, 8, 25, tzinfo=UTC)
    baseline = tmp_path / "baseline-hardware"
    candidate = tmp_path / "candidate-hardware"
    write_campaign(
        baseline,
        release_id="built-in",
        source="built-in",
        starts=[started],
        cold_seconds=10.0,
    )
    write_campaign(
        candidate,
        release_id="candidate-v2",
        source="verified-release",
        starts=[started + timedelta(seconds=1)],
        cold_seconds=9.0,
    )
    candidate_session = candidate / "session.json"
    value = json.loads(candidate_session.read_text(encoding="utf-8"))
    replacement = "different-sd-card"
    value["hardware_inventory"]["target_sd_card"]["serial_or_asset_id"] = replacement
    value["safety_gate"]["hardware_inventory"]["target_sd_card"][
        "serial_or_asset_id"
    ] = replacement
    candidate_session.write_text(json.dumps(value), encoding="utf-8")
    records: list[dict[str, object]] = []
    promotion.load_campaign(
        baseline,
        expected_release_id="built-in",
        expected_execution_source="built-in",
        expected_openssl_sha256=OPENSSL_SHA,
        evidence_records=records,
    )
    promotion.load_campaign(
        candidate,
        expected_release_id="candidate-v2",
        expected_execution_source="verified-release",
        expected_bundle_sha256=BUNDLE_SHA,
        expected_openssl_sha256=OPENSSL_SHA,
        evidence_records=records,
    )

    with pytest.raises(promotion.CampaignError, match="identical accepted lab system"):
        promotion._require_consistent_environment(records, "test campaigns")


@pytest.mark.parametrize(
    ("label", "field", "value"),
    [
        ("target", "files_scanned", True),
        ("target", "schema_version", True),
        ("target", "forbidden_matches", False),
        ("controller_artifacts", "files_scanned", 0),
        ("controller_artifacts", "forbidden_matches", False),
    ],
)
def test_portal_secret_scan_rejects_bool_empty_or_unscanned_evidence(
    tmp_path: pathlib.Path,
    label: str,
    field: str,
    value: object,
) -> None:
    combined, _ = write_complete_release_evidence(tmp_path)
    session = json.loads((combined / "session.json").read_text(encoding="utf-8"))
    session["portal_secret_artifact_scan"][label][field] = value

    with pytest.raises(promotion.CampaignError, match="portal secret scan failed"):
        promotion._validate_portal_matrix(session)


def test_portal_secret_scan_rejects_extra_fields(tmp_path: pathlib.Path) -> None:
    combined, _ = write_complete_release_evidence(tmp_path)
    session = json.loads((combined / "session.json").read_text(encoding="utf-8"))
    session["portal_secret_artifact_scan"]["target"]["unexpected"] = True

    with pytest.raises(promotion.CampaignError, match="portal secret scan failed"):
        promotion._validate_portal_matrix(session)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("state", "validated"),
        ("schema_version", True),
        ("duration_seconds", True),
        ("unexpected", "accepted-by-subset-check"),
    ],
)
def test_portal_controller_receipts_require_exact_state_and_schema(
    tmp_path: pathlib.Path, field: str, value: object
) -> None:
    combined, _ = write_complete_release_evidence(tmp_path)
    session = json.loads((combined / "session.json").read_text(encoding="utf-8"))
    session["portal_matrix"][0]["controller_portal_apply"][field] = value

    with pytest.raises(promotion.CampaignError, match="radio transition was not proven"):
        promotion._validate_portal_matrix(session)


def test_signed_role_image_manifest_binds_external_image_identity(
    tmp_path: pathlib.Path,
) -> None:
    manifest = tmp_path / "zero.img.manifest.json"
    keys = {
        "workstation_to_controller_ssh": openssh_public_key(1),
        "controller_to_zero_ssh": openssh_public_key(2),
        "application_release": pem_public_key(3),
        "lab_safety": pem_public_key(4),
        "image_release": pem_public_key(5),
    }
    key_paths: dict[str, pathlib.Path] = {}
    for name, payload in keys.items():
        path = tmp_path / f"{name}.pub"
        path.write_bytes(payload)
        key_paths[name] = path
    promotion.DEFAULT_APPROVAL_PUBLIC_KEY.write_bytes(keys["lab_safety"])
    policy = promotion.build_key_policy(**keys)
    build_inputs = {
        "schema_version": 2,
        "role": "zero",
        "base_archive_sha256": "7" * 64,
        "application_sha256": "8" * 64,
        "package_bundle_sha256": "b" * 64,
        "data_mib": 512,
        "key_policy_sha256": policy["key_policy_sha256"],
    }
    image_build_id = promotion.hashlib.sha256(
        json.dumps(build_inputs, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    manifest.write_text(
        json.dumps(
                {
                    **build_inputs,
                    **{
                        field: policy[field]
                        for field in (*promotion.RAW_FIELDS, *promotion.MATERIAL_FIELDS)
                    },
                    "image_sha256": IMAGE_SHA,
                "image_build_id": image_build_id,
            }
        ),
        encoding="utf-8",
    )
    manifest.with_name(f"{manifest.name}.sig").write_bytes(b"signature")
    image_key = key_paths["image_release"]
    result = promotion.load_image_manifest(
        manifest,
        public_key=image_key,
        expected_public_key_sha256=promotion.hashlib.sha256(
            image_key.read_bytes()
        ).hexdigest(),
        expected_manifest_sha256=promotion.hashlib.sha256(manifest.read_bytes()).hexdigest(),
        expected_image_sha256=IMAGE_SHA,
        expected_image_build_id=image_build_id,
        expected_openssl_sha256=OPENSSL_SHA,
        workstation_to_controller_ssh_public_key=key_paths[
            "workstation_to_controller_ssh"
        ],
        controller_to_zero_ssh_public_key=key_paths["controller_to_zero_ssh"],
        application_release_public_key=key_paths["application_release"],
        lab_safety_public_key=key_paths["lab_safety"],
    )

    assert result["image_sha256"] == IMAGE_SHA
    assert result["image_build_id"] == image_build_id


def test_role_image_manifest_schema_one_is_explicitly_rejected(
    tmp_path: pathlib.Path,
) -> None:
    manifest = tmp_path / "legacy.img.manifest.json"
    manifest.write_text('{"schema_version":1}', encoding="utf-8")
    manifest.with_name(f"{manifest.name}.sig").write_bytes(b"signature")
    key = tmp_path / "image.pub.pem"
    key.write_bytes(pem_public_key(9))

    with pytest.raises(promotion.CampaignError, match="schema 1 is explicitly unsupported"):
        promotion.load_image_manifest(
            manifest,
            public_key=key,
            expected_public_key_sha256=promotion.hashlib.sha256(key.read_bytes()).hexdigest(),
            expected_manifest_sha256=promotion.hashlib.sha256(manifest.read_bytes()).hexdigest(),
            expected_image_sha256=IMAGE_SHA,
            expected_image_build_id=IMAGE_BUILD_ID,
            expected_openssl_sha256=OPENSSL_SHA,
            workstation_to_controller_ssh_public_key=key,
            controller_to_zero_ssh_public_key=key,
            application_release_public_key=key,
            lab_safety_public_key=key,
        )
