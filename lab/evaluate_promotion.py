#!/usr/bin/env python3
"""Evaluate two completed HIL campaigns against TideTracker's 20/20 gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tidetracker.benchmark import CycleMetrics, evaluate_promotion
from tidetracker.key_policy import (
    MATERIAL_FIELDS,
    RAW_FIELDS,
    KeyPolicyError,
    build_key_policy,
    read_public_key,
)

DEFAULT_APPROVAL_PUBLIC_KEY = pathlib.Path("/etc/tidetracker/lab-safety-approver.pub.pem")
OPENSSL_PATH = pathlib.Path("/usr/bin/openssl")
LIFECYCLE_PHASES = {
    "mode",
    "network",
    "fetch",
    "validate",
    "compute",
    "render",
    "display",
    "persist",
    "done",
}
UPDATE_POWER_CUT_PHASES = {"begin", "stage", "verify", "activate"}
WIFI_SCENARIOS = {
    "wifi_disconnect",
    "bad_credentials",
    "client_ap_transition",
    "dns_failure",
    "tls_failure",
    "http_failure",
}
PORTAL_CASE_SPEC = {
    "visible_open_test": ("open", False, "test"),
    "visible_open_force_save": ("open", False, "force-save"),
    "hidden_open_test": ("open", True, "test"),
    "hidden_open_force_save": ("open", True, "force-save"),
    "visible_wpa_personal_test": ("wpa-personal", False, "test"),
    "visible_wpa_personal_force_save": ("wpa-personal", False, "force-save"),
    "hidden_wpa_personal_test": ("wpa-personal", True, "test"),
    "hidden_wpa_personal_force_save": ("wpa-personal", True, "force-save"),
    "visible_wpa_enterprise_test": ("wpa-enterprise", False, "test"),
    "visible_wpa_enterprise_force_save": ("wpa-enterprise", False, "force-save"),
    "hidden_wpa_enterprise_test": ("wpa-enterprise", True, "test"),
    "hidden_wpa_enterprise_force_save": ("wpa-enterprise", True, "force-save"),
}
APPROVED_OPERATIONS = {
    "cycles",
    "release_stage",
    "wifi_matrix",
    "lifecycle_power_cuts",
    "update_power_cuts",
    "field_cycle",
}
EXPECTED_CONTROLLER_ARTIFACT_POLICY = {
    "root": "/var/lib/tidetracker/controller/hil-results",
    "data_uuid": "54540000-0000-4000-8000-000000000001",
    "filesystem": "ext4",
    "read_write": True,
    "owner": "root",
    "group": "tidetracker",
    "mode": "0770",
    "verified": True,
}
HARDWARE_INVENTORY_COMPONENTS = {
    "usb_data_isolator",
    "switched_power_fixture",
    "uart_adapter",
    "camera",
    "display",
    "tpl5110_power_timer",
    "target_pi_zero",
    "target_sd_card",
}
TOOL_IDENTITY_NAMES = {
    "camera_capture",
    "camera_validator",
    "usb_disconnected_probe",
}


class CampaignError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SignedDocument:
    value: dict[str, Any]
    sha256: str
    signature_sha256: str
    approval_public_key_sha256: str


def _stable_file_bytes(path: pathlib.Path, label: str, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise CampaignError(f"{label} is missing or unsafe")
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        current = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise CampaignError(f"{label} is unreadable") from exc
    stable_fields = (
        ("st_size", "st_mtime_ns")
        if os.name == "nt"
        else ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    )
    if (
        not stat.S_ISREG(before.st_mode)
        or not 1 <= len(data) <= limit
        or len(data) != before.st_size
        or any(getattr(before, field) != getattr(after, field) for field in stable_fields)
        or any(getattr(before, field) != getattr(current, field) for field in stable_fields)
    ):
        raise CampaignError(f"{label} changed during validation or exceeds its size limit")
    return data


def _write_snapshot(path: pathlib.Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _reject_duplicates(label: str):
    def reject(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        normalized_fields: set[str] = set()
        for field, child in items:
            normalized = field.casefold()
            if normalized in normalized_fields:
                raise CampaignError(f"{label} repeats field: {field}")
            normalized_fields.add(normalized)
            value[field] = child
        return value

    return reject


def _load_signed_json(
    path: pathlib.Path,
    *,
    label: str,
    expected_openssl_sha256: str,
    limit: int = 8 * 1024 * 1024,
    public_key: pathlib.Path | None = None,
) -> SignedDocument:
    signature = path.with_name(f"{path.name}.sig")
    key = public_key or DEFAULT_APPROVAL_PUBLIC_KEY
    document_bytes = _stable_file_bytes(path, label, limit)
    signature_bytes = _stable_file_bytes(signature, f"{label} signature", 1024)
    key_bytes = _stable_file_bytes(key, f"pinned {label} public key", 16 * 1024)
    openssl_bytes = _stable_file_bytes(OPENSSL_PATH, "fixed OpenSSL executable", 64 * 1024 * 1024)
    if (
        not _hex_digest(expected_openssl_sha256)
        or hashlib.sha256(openssl_bytes).hexdigest() != expected_openssl_sha256
    ):
        raise CampaignError("fixed OpenSSL executable is not allowlisted")
    with tempfile.TemporaryDirectory(prefix="tidetracker-promotion-") as temporary:
        snapshot = pathlib.Path(temporary)
        snapshot.chmod(0o700)
        document_snapshot = snapshot / "document.json"
        signature_snapshot = snapshot / "document.sig"
        key_snapshot = snapshot / "approval.pub.pem"
        _write_snapshot(document_snapshot, document_bytes)
        _write_snapshot(signature_snapshot, signature_bytes)
        _write_snapshot(key_snapshot, key_bytes)
        try:
            verification = subprocess.run(
                [
                    str(OPENSSL_PATH),
                    "pkeyutl",
                    "-verify",
                    "-pubin",
                    "-inkey",
                    str(key_snapshot),
                    "-rawin",
                    "-in",
                    str(document_snapshot),
                    "-sigfile",
                    str(signature_snapshot),
                ],
                text=True,
                capture_output=True,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CampaignError(f"{label} signature verification failed closed") from exc
        if verification.returncode != 0:
            raise CampaignError(f"{label} signature is invalid")
    try:
        def reject_constant(value: str) -> None:
            raise CampaignError(f"{label} contains non-finite number: {value}")

        parsed = json.loads(
            document_bytes.decode("utf-8"),
            object_pairs_hook=_reject_duplicates(label),
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CampaignError(f"{label} is unreadable") from exc
    if not isinstance(parsed, dict):
        raise CampaignError(f"{label} must be a JSON object")
    return SignedDocument(
        value=parsed,
        sha256=hashlib.sha256(document_bytes).hexdigest(),
        signature_sha256=hashlib.sha256(signature_bytes).hexdigest(),
        approval_public_key_sha256=hashlib.sha256(key_bytes).hexdigest(),
    )


@dataclass(frozen=True, slots=True)
class CampaignCycle:
    started: datetime
    metrics: CycleMetrics


def _number(value: Any, name: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise CampaignError(f"invalid {name}")
    return float(value)


def _execution_evidence(
    target: dict[str, Any], expected_release_id: str, expected_execution_source: str
) -> None:
    result = target.get("last_result")
    metrics = target.get("metrics")
    supervisor = target.get("supervisor")
    execution = supervisor.get("execution") if isinstance(supervisor, dict) else None
    if not all(isinstance(value, dict) for value in (result, metrics, supervisor, execution)):
        raise CampaignError("cycle lacks durable release-supervisor evidence")
    assert isinstance(result, dict)
    assert isinstance(metrics, dict)
    assert isinstance(supervisor, dict)
    assert isinstance(execution, dict)
    run_id = result.get("run_id")
    attempts = execution.get("attempts")
    if (
        supervisor.get("schema_version") != 1
        or supervisor.get("event") != "release_supervision"
        or not isinstance(run_id, str)
        or not run_id
        or metrics.get("run_id") != run_id
        or execution.get("expected_release_id") != expected_release_id
        or execution.get("expected_execution_source") != expected_execution_source
        or execution.get("expected_release_executed_successfully") is not True
        or execution.get("fallback_used") is not False
        or execution.get("trial_rolled_back") is not False
        or execution.get("selected_run_id") != run_id
        or execution.get("selected_release_id") != expected_release_id
        or execution.get("selected_execution_source") != expected_execution_source
        or execution.get("selected_success") is not True
        or result.get("release_id") != expected_release_id
        or result.get("execution_source") != expected_execution_source
        or metrics.get("release_id") != expected_release_id
        or metrics.get("execution_source") != expected_execution_source
        or not isinstance(attempts, list)
        or not attempts
        or not any(
            isinstance(attempt, dict)
            and attempt.get("release_id") == expected_release_id
            and attempt.get("execution_source") == expected_execution_source
            and attempt.get("returncode") == 0
            for attempt in attempts
        )
    ):
        raise CampaignError("cycle used a fallback, rollback, or unexpected release source")


def _timestamp(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise CampaignError("cycle has an invalid started timestamp") from exc
    if parsed.tzinfo is None:
        raise CampaignError("cycle started timestamp must include a timezone")
    return parsed


def load_legacy_report(
    path: pathlib.Path,
    *,
    expected_candidate_release_id: str,
    expected_candidate_bundle_sha256: str,
    expected_candidate_release_public_key_sha256: str,
    expected_openssl_sha256: str,
) -> dict[str, Any]:
    signed = _load_signed_json(
        path,
        label="legacy framebuffer report",
        expected_openssl_sha256=expected_openssl_sha256,
        limit=1024 * 1024,
    )
    report = signed.value
    fixture_count = report.get("fixture_count")
    results = report.get("results")
    manifest_hash = report.get("manifest_sha256")
    key_hash = signed.approval_public_key_sha256
    if (
        report.get("schema_version") != 1
        or report.get("event") != "legacy_framebuffer_verification"
        or report.get("passed") is not True
        or report.get("status") != "passed"
        or not isinstance(fixture_count, int)
        or isinstance(fixture_count, bool)
        or fixture_count < 1
        or report.get("matched_count") != fixture_count
        or report.get("failures") != []
        or not isinstance(results, list)
        or len(results) != fixture_count
        or not isinstance(manifest_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", manifest_hash) is None
        or report.get("candidate_release_id") != expected_candidate_release_id
        or report.get("candidate_bundle_sha256") != expected_candidate_bundle_sha256
        or report.get("candidate_release_public_key_sha256")
        != expected_candidate_release_public_key_sha256
        or not _hex_digest(report.get("candidate_release_public_key_sha256"))
        or report.get("candidate_verification") != "signed_bundle_subprocess"
        or not isinstance(report.get("candidate_manifest_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", report["candidate_manifest_sha256"]) is None
        or report.get("capture_approval_public_key_sha256") != key_hash
        or not isinstance(report.get("capture_manifest_signature_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", report["capture_manifest_signature_sha256"]) is None
    ):
        raise CampaignError("legacy framebuffer replay has not passed")
    fixture_ids: set[str] = set()
    for result in results:
        if not isinstance(result, dict):
            raise CampaignError("legacy framebuffer result is invalid")
        fixture_id = result.get("fixture_id")
        expected_hash = result.get("expected_sha256")
        if (
            not isinstance(fixture_id, str)
            or not fixture_id
            or fixture_id in fixture_ids
            or result.get("matched") is not True
            or result.get("actual_bytes") != 48_000
            or not isinstance(expected_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
            or result.get("actual_sha256") != expected_hash
        ):
            raise CampaignError("legacy framebuffer result is not byte-equal")
        fixture_ids.add(fixture_id)
    return {
        "path": str(path),
        "sha256": signed.sha256,
        "manifest_sha256": manifest_hash,
        "fixture_count": fixture_count,
        "signature_sha256": signed.signature_sha256,
        "approval_public_key_sha256": key_hash,
        "candidate_release_id": expected_candidate_release_id,
        "candidate_bundle_sha256": expected_candidate_bundle_sha256,
        "candidate_release_public_key_sha256": expected_candidate_release_public_key_sha256,
        "candidate_manifest_sha256": report["candidate_manifest_sha256"],
        "candidate_verification": report["candidate_verification"],
    }


def _hex_digest(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validate_hardware_inventory(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != HARDWARE_INVENTORY_COMPONENTS:
        raise CampaignError("signed HIL gate lacks the exact required hardware inventory")
    for component in HARDWARE_INVENTORY_COMPONENTS:
        identity = value.get(component)
        if not isinstance(identity, dict) or set(identity) != {"model", "serial_or_asset_id"}:
            raise CampaignError(f"signed hardware inventory is invalid for {component}")
        for field in ("model", "serial_or_asset_id"):
            item = identity.get(field)
            if (
                not isinstance(item, str)
                or item != item.strip()
                or not 1 <= len(item) <= 128
                or any(ord(character) < 0x20 for character in item)
            ):
                raise CampaignError(f"signed hardware inventory is invalid for {component}")
    return value


def _validate_tool_identity(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "canonical_argv",
        "executable_path",
        "executable_sha256",
        "executable_size_bytes",
        "support_files",
    }:
        raise CampaignError(f"signed {label} tool identity is invalid")
    argv = value.get("canonical_argv")
    executable_path = value.get("executable_path")
    support_files = value.get("support_files")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(part, str) and part and "\x00" not in part for part in argv)
        or not isinstance(executable_path, str)
        or not executable_path.startswith("/")
        or argv[0] != executable_path
        or not _hex_digest(value.get("executable_sha256"))
        or not isinstance(value.get("executable_size_bytes"), int)
        or isinstance(value.get("executable_size_bytes"), bool)
        or not 1 <= value["executable_size_bytes"] <= 128 * 1024 * 1024
        or not isinstance(support_files, list)
        or len(support_files) > 16
    ):
        raise CampaignError(f"signed {label} tool identity is invalid")
    seen_paths: set[str] = set()
    for item in support_files:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "sha256", "size_bytes"}
            or not isinstance(item.get("path"), str)
            or not item["path"].startswith("/")
            or item["path"] in seen_paths
            or not _hex_digest(item.get("sha256"))
            or not isinstance(item.get("size_bytes"), int)
            or isinstance(item.get("size_bytes"), bool)
            or not 1 <= item["size_bytes"] <= 16 * 1024 * 1024
        ):
            raise CampaignError(f"signed {label} support-file identity is invalid")
        seen_paths.add(item["path"])
    return value


def _validate_tool_identities(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != TOOL_IDENTITY_NAMES:
        raise CampaignError("signed HIL gate lacks the exact required evidence-tool identities")
    for name in TOOL_IDENTITY_NAMES:
        _validate_tool_identity(value[name], name)
    return value


def load_image_manifest(
    path: pathlib.Path,
    *,
    public_key: pathlib.Path,
    expected_public_key_sha256: str,
    expected_manifest_sha256: str,
    expected_image_sha256: str,
    expected_image_build_id: str,
    expected_openssl_sha256: str,
    workstation_to_controller_ssh_public_key: pathlib.Path,
    controller_to_zero_ssh_public_key: pathlib.Path,
    application_release_public_key: pathlib.Path,
    lab_safety_public_key: pathlib.Path,
) -> dict[str, Any]:
    pinned_lab_key = _stable_file_bytes(
        DEFAULT_APPROVAL_PUBLIC_KEY,
        "pinned lab approval key",
        16 * 1024,
    )
    lab_approval_key_sha256 = hashlib.sha256(pinned_lab_key).hexdigest()
    signed = _load_signed_json(
        path,
        label="role-image manifest",
        expected_openssl_sha256=expected_openssl_sha256,
        limit=1024 * 1024,
        public_key=public_key,
    )
    manifest = signed.value
    required_digests = (
        "base_archive_sha256",
        "image_sha256",
        "image_build_id",
        "application_sha256",
        "package_bundle_sha256",
        "key_policy_sha256",
        *RAW_FIELDS,
        *MATERIAL_FIELDS,
    )
    if type(manifest.get("schema_version")) is int and manifest.get("schema_version") == 1:
        raise CampaignError("role-image manifest schema 1 is explicitly unsupported")
    if (
        not _hex_digest(expected_public_key_sha256)
        or not _hex_digest(expected_manifest_sha256)
        or not _hex_digest(expected_image_sha256)
        or signed.approval_public_key_sha256 != expected_public_key_sha256
        or signed.approval_public_key_sha256 == lab_approval_key_sha256
        or signed.sha256 != expected_manifest_sha256
        or set(manifest)
        != {
            "schema_version",
            "role",
            *required_digests,
            "data_mib",
        }
        or type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != 2
        or manifest.get("role") != "zero"
        or any(not _hex_digest(manifest.get(field)) for field in required_digests)
        or manifest.get("image_sha256") != expected_image_sha256
        or manifest.get("image_build_id") != expected_image_build_id
        or not _hex_digest(expected_image_build_id)
        or not isinstance(manifest.get("data_mib"), int)
        or isinstance(manifest.get("data_mib"), bool)
        or not 512 <= manifest["data_mib"] <= 8192
    ):
        raise CampaignError("signed role-image manifest does not match the accepted Zero image")
    try:
        policy = build_key_policy(
            workstation_to_controller_ssh=read_public_key(
                workstation_to_controller_ssh_public_key,
                "workstation-to-controller SSH public key",
            ),
            controller_to_zero_ssh=read_public_key(
                controller_to_zero_ssh_public_key,
                "controller-to-Zero SSH public key",
            ),
            application_release=read_public_key(
                application_release_public_key,
                "application release public key",
            ),
            lab_safety=read_public_key(lab_safety_public_key, "lab safety public key"),
            image_release=read_public_key(public_key, "image release public key"),
        )
    except KeyPolicyError as exc:
        raise CampaignError("five-key release policy inputs are invalid") from exc
    if (
        policy["lab_safety_public_key_sha256"] != lab_approval_key_sha256
        or hashlib.sha256(pinned_lab_key).hexdigest()
        != policy["lab_safety_public_key_sha256"]
        or signed.approval_public_key_sha256
        != policy["image_release_public_key_sha256"]
        or any(manifest[field] != policy[field] for field in (*RAW_FIELDS, *MATERIAL_FIELDS))
        or manifest["key_policy_sha256"] != policy["key_policy_sha256"]
    ):
        raise CampaignError("signed role-image manifest does not match the canonical five-key policy")
    build_record = {
        "schema_version": 2,
        "role": manifest["role"],
        "base_archive_sha256": manifest["base_archive_sha256"],
        "application_sha256": manifest["application_sha256"],
        "package_bundle_sha256": manifest["package_bundle_sha256"],
        "data_mib": manifest["data_mib"],
        "key_policy_sha256": manifest["key_policy_sha256"],
    }
    computed_build_id = hashlib.sha256(
        json.dumps(build_record, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    if computed_build_id != expected_image_build_id:
        raise CampaignError("role-image build ID is inconsistent with its signed build inputs")
    return {
        "path": str(path),
        "sha256": signed.sha256,
        "signature_sha256": signed.signature_sha256,
        "public_key_sha256": signed.approval_public_key_sha256,
        "lab_approval_public_key_sha256": lab_approval_key_sha256,
        "image_sha256": expected_image_sha256,
        "image_build_id": expected_image_build_id,
        "application_sha256": manifest["application_sha256"],
        "application_release_public_key_sha256": manifest[
            "application_release_public_key_sha256"
        ],
        "package_bundle_sha256": manifest["package_bundle_sha256"],
        "key_policy_sha256": manifest["key_policy_sha256"],
    }


def _validate_target_identity(
    session: dict[str, Any],
    *,
    release_id: str,
    execution_source: str,
    image_build_id: str,
    image_sha256: str,
    image_manifest_sha256: str,
    key_policy_sha256: str,
) -> None:
    if session.get("target_identity") != {
        "release_id": release_id,
        "execution_source": execution_source,
        "image_build_id": image_build_id,
        "image_sha256": image_sha256,
        "image_manifest_sha256": image_manifest_sha256,
        "key_policy_sha256": key_policy_sha256,
        "attestation": "verified_signed_safety_gate",
    }:
        raise CampaignError("HIL session target identity does not match the accepted image")


def _validate_session_envelope(
    session: dict[str, Any],
    signed: SignedDocument,
    *,
    expected_release_id: str,
    expected_execution_source: str,
) -> None:
    gate = session.get("safety_gate")
    identity = session.get("target_identity")
    fixture = session.get("fixture_identity")
    if (
        not isinstance(gate, dict)
        or gate.get("approval_public_key_sha256")
        != signed.approval_public_key_sha256
        or not isinstance(identity, dict)
        or identity.get("release_id") != expected_release_id
        or identity.get("execution_source") != expected_execution_source
        or identity.get("image_sha256") != gate.get("target_image_sha256")
        or identity.get("image_manifest_sha256")
        != gate.get("target_image_manifest_sha256")
        or identity.get("image_build_id") != gate.get("target_image_build_id")
        or identity.get("key_policy_sha256") != gate.get("target_key_policy_sha256")
        or identity.get("attestation") != "verified_signed_safety_gate"
        or not isinstance(fixture, dict)
        or fixture.get("hardware_id") != gate.get("hardware_id")
        or fixture.get("firmware_sha256") != gate.get("fixture_firmware_sha256")
        or session.get("hardware_inventory") != gate.get("hardware_inventory")
        or session.get("tool_identities") != gate.get("tool_identities")
    ):
        raise CampaignError("signed HIL session envelope is not bound to its gate and target")
    _validate_hardware_inventory(gate.get("hardware_inventory"))
    _validate_tool_identities(gate.get("tool_identities"))
    try:
        started = datetime.fromisoformat(str(session["started"]).replace("Z", "+00:00"))
        finished = datetime.fromisoformat(str(session["finished"]).replace("Z", "+00:00"))
        valid_until = datetime.fromisoformat(str(gate["valid_until"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise CampaignError("signed HIL session envelope timestamps are invalid") from exc
    if (
        started.tzinfo is None
        or finished.tzinfo is None
        or valid_until.tzinfo is None
        or finished < started
    ):
        raise CampaignError("signed HIL session envelope timestamps are invalid")
    if started > valid_until:
        raise CampaignError("signed HIL session was not started within its approval window")
    if finished > valid_until:
        raise CampaignError("signed HIL session did not finish within its approval window")
    _validate_powered_off_rails(session.get("initial_rail_isolation"), "initial isolation")


def _environment_identity(session: dict[str, Any]) -> dict[str, Any]:
    gate = session.get("safety_gate")
    fixture = session.get("fixture_identity")
    controller_policy = session.get("controller_artifact_policy")
    if (
        not isinstance(gate, dict)
        or not isinstance(fixture, dict)
        or not _hex_digest(gate.get("controller_machine_id_sha256"))
        or session.get("controller_machine_id_sha256")
        != gate.get("controller_machine_id_sha256")
        or controller_policy != EXPECTED_CONTROLLER_ARTIFACT_POLICY
    ):
        raise CampaignError("HIL session lacks its pinned controller identity and storage policy")
    return {
        "hardware_id": gate["hardware_id"],
        "fixture_firmware_sha256": gate["fixture_firmware_sha256"],
        "target_known_hosts_sha256": gate["target_known_hosts_sha256"],
        "controller_machine_id_sha256": session["controller_machine_id_sha256"],
        "fixture_identity": {
            "hardware_id": fixture.get("hardware_id"),
            "firmware_sha256": fixture.get("firmware_sha256"),
        },
        "controller_artifact_policy": controller_policy,
        "hardware_inventory": _validate_hardware_inventory(gate.get("hardware_inventory")),
        "tool_identities": _validate_tool_identities(gate.get("tool_identities")),
        "image_identity": {
            "image_build_id": gate["target_image_build_id"],
            "image_sha256": gate["target_image_sha256"],
            "image_manifest_sha256": gate["target_image_manifest_sha256"],
            "key_policy_sha256": gate["target_key_policy_sha256"],
        },
    }


def _require_consistent_environment(
    records: list[dict[str, Any]], label: str
) -> dict[str, Any]:
    identities = [record.get("environment_identity") for record in records]
    if not identities or any(identity != identities[0] for identity in identities[1:]):
        raise CampaignError(f"{label} was not captured on one identical accepted lab system")
    identity = identities[0]
    if not isinstance(identity, dict):
        raise CampaignError(f"{label} lacks a validated lab-system identity")
    return identity


def _validate_signed_gate(
    session: dict[str, Any],
    operation: str,
    *,
    update_phases: set[str] | None = None,
) -> dict[str, Any]:
    if session.get("simulated") is not False:
        raise CampaignError("simulated HIL evidence is not promotable")
    gate = session.get("safety_gate")
    if not isinstance(gate, dict):
        raise CampaignError("HIL session lacks its verified signed safety gate")
    operations = gate.get("approved_operations")
    done_pulse = gate.get("done_pulse")
    measurements = gate.get("measurements")
    hardware_inventory = gate.get("hardware_inventory")
    tool_identities = gate.get("tool_identities")
    minimum_us = done_pulse.get("minimum_us") if isinstance(done_pulse, dict) else None
    maximum_us = done_pulse.get("maximum_us") if isinstance(done_pulse, dict) else None
    shortened = (
        done_pulse.get("shortened_timing_hardware_validated")
        if isinstance(done_pulse, dict)
        else None
    )
    if (
        gate.get("signature") != "verified"
        or not _hex_digest(gate.get("gate_sha256"))
        or not _hex_digest(gate.get("signature_sha256"))
        or not _hex_digest(gate.get("approval_public_key_sha256"))
        or not isinstance(gate.get("hardware_id"), str)
        or not gate.get("hardware_id")
        or not _hex_digest(gate.get("fixture_firmware_sha256"))
        or not _hex_digest(gate.get("target_known_hosts_sha256"))
        or not _hex_digest(gate.get("controller_machine_id_sha256"))
        or not _hex_digest(gate.get("target_image_sha256"))
        or not _hex_digest(gate.get("target_image_manifest_sha256"))
        or not _hex_digest(gate.get("target_image_build_id"))
        or not _hex_digest(gate.get("target_key_policy_sha256"))
        or not isinstance(gate.get("approval_nonce"), str)
        or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", gate["approval_nonce"]) is None
        or not isinstance(gate.get("valid_until"), str)
        or not isinstance(operations, list)
        or not operations
        or any(not isinstance(item, str) for item in operations)
        or len(set(operations)) != len(operations)
        or not set(operations) <= APPROVED_OPERATIONS
        or operation not in operations
        or not isinstance(minimum_us, int)
        or isinstance(minimum_us, bool)
        or not isinstance(maximum_us, int)
        or isinstance(maximum_us, bool)
        or not 10_000 <= minimum_us <= maximum_us <= 1_000_000
        or not isinstance(shortened, bool)
        or ((minimum_us < 400_000 or maximum_us < 500_000) and not shortened)
        or not isinstance(measurements, dict)
        or not isinstance(measurements.get("zero_5v_off_usb_connected_v"), (int, float))
        or isinstance(measurements.get("zero_5v_off_usb_connected_v"), bool)
        or not 0 <= measurements["zero_5v_off_usb_connected_v"] < 0.1
        or not isinstance(
            measurements.get("zero_3v3_off_all_sense_connected_v"), (int, float)
        )
        or isinstance(measurements.get("zero_3v3_off_all_sense_connected_v"), bool)
        or not 0 <= measurements["zero_3v3_off_all_sense_connected_v"] < 0.1
        or not isinstance(measurements.get("zero_5v_on_under_load_v"), (int, float))
        or isinstance(measurements.get("zero_5v_on_under_load_v"), bool)
        or not 4.75 <= measurements["zero_5v_on_under_load_v"] <= 5.25
    ):
        raise CampaignError(f"signed HIL gate does not authorize {operation}")
    _validate_hardware_inventory(hardware_inventory)
    _validate_tool_identities(tool_identities)
    try:
        valid_until = datetime.fromisoformat(gate["valid_until"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise CampaignError("signed HIL gate validity timestamp is invalid") from exc
    if valid_until.tzinfo is None:
        raise CampaignError("signed HIL gate validity timestamp has no timezone")
    if update_phases is not None:
        approved = gate.get("approved_update_power_cut_phases")
        if (
            not isinstance(approved, list)
            or any(not isinstance(item, str) for item in approved)
            or len(approved) != len(set(approved))
            or set(approved) != update_phases
        ):
            raise CampaignError("signed HIL gate does not authorize every update power cut")
    return gate


def _validate_done(value: Any, label: str, expected_bounds: dict[str, Any]) -> None:
    minimum_us = expected_bounds.get("minimum_us")
    maximum_us = expected_bounds.get("maximum_us")
    if (
        not isinstance(minimum_us, int)
        or isinstance(minimum_us, bool)
        or not isinstance(maximum_us, int)
        or isinstance(maximum_us, bool)
        or not isinstance(value, dict)
        or value.get("event") != "done"
        or not isinstance(value.get("edge_us"), (int, float))
        or isinstance(value.get("edge_us"), bool)
        or value["edge_us"] < 0
        or not isinstance(value.get("pulse_us"), (int, float))
        or isinstance(value.get("pulse_us"), bool)
        or not minimum_us <= value["pulse_us"] <= maximum_us
    ):
        raise CampaignError(f"{label} lacks a captured DONE pulse within signed bounds")


def _validate_camera(
    value: Any,
    label: str,
    *,
    expected_tool_identities: dict[str, Any],
    artifact_path: pathlib.Path | None = None,
) -> None:
    if (
        not isinstance(value, dict)
        or value.get("capture") != "complete"
        or value.get("comparison") != "accepted"
        or value.get("simulated") is True
        or not _hex_digest(value.get("image_sha256"))
        or not isinstance(value.get("image_size_bytes"), int)
        or isinstance(value.get("image_size_bytes"), bool)
        or not 1 <= value["image_size_bytes"] <= 32 * 1024 * 1024
        or value.get("capture_tool_identity")
        != expected_tool_identities.get("camera_capture")
        or value.get("validation_tool_identity")
        != expected_tool_identities.get("camera_validator")
    ):
        raise CampaignError(f"{label} lacks an accepted physical display comparison")
    if artifact_path is not None:
        image_bytes = _stable_file_bytes(
            artifact_path,
            f"{label} display photograph",
            32 * 1024 * 1024,
        )
        if (
            len(image_bytes) != value["image_size_bytes"]
            or hashlib.sha256(image_bytes).hexdigest() != value["image_sha256"]
        ):
            raise CampaignError(f"{label} display photograph differs from signed evidence")


def _validate_powered_off_rails(value: Any, label: str) -> None:
    if not isinstance(value, dict):
        raise CampaignError(f"{label} lacks powered-off rail measurements")
    for field in ("zero_5v_v", "zero_3v3_v"):
        reading = value.get(field)
        if (
            not isinstance(reading, (int, float))
            or isinstance(reading, bool)
            or not 0 <= float(reading) < 0.1
        ):
            raise CampaignError(f"{label} has unsafe or invalid {field}")


def _validate_reliability_cycle(
    cycle: Any,
    *,
    expected_release_id: str,
    expected_execution_source: str,
    expected_tool_identities: dict[str, Any],
    expected_done_bounds: dict[str, Any],
    label: str,
    camera_path: pathlib.Path | None = None,
) -> None:
    if not isinstance(cycle, dict) or cycle.get("outcome") != "success":
        raise CampaignError(f"{label} did not complete successfully")
    _validate_done(cycle.get("done"), label, expected_done_bounds)
    _validate_camera(
        cycle.get("camera"),
        label,
        expected_tool_identities=expected_tool_identities,
        artifact_path=camera_path,
    )
    _validate_powered_off_rails(cycle.get("rails_after_done"), label)
    target = cycle.get("target")
    if not isinstance(target, dict):
        raise CampaignError(f"{label} lacks target evidence")
    _execution_evidence(target, expected_release_id, expected_execution_source)
    metrics = target.get("metrics")
    refreshes = metrics.get("display_refresh_count") if isinstance(metrics, dict) else None
    if (
        not isinstance(metrics, dict)
        or metrics.get("success") is not True
        or not isinstance(refreshes, int)
        or isinstance(refreshes, bool)
        or not 0 <= refreshes <= 1
    ):
        raise CampaignError(f"{label} lacks successful no-duplicate-refresh metrics")


def load_campaign(
    path: pathlib.Path,
    *,
    expected_release_id: str,
    expected_execution_source: str,
    expected_bundle_sha256: str | None = None,
    expected_image_build_id: str | None = None,
    expected_image_sha256: str | None = None,
    expected_image_manifest_sha256: str | None = None,
    expected_key_policy_sha256: str | None = None,
    expected_openssl_sha256: str,
    evidence_records: list[dict[str, Any]] | None = None,
) -> tuple[CampaignCycle, ...]:
    session_path = path / "session.json"
    signed = _load_signed_json(
        session_path,
        label=f"campaign session {session_path}",
        expected_openssl_sha256=expected_openssl_sha256,
    )
    session = signed.value
    if session.get("schema_version") != 1 or session.get("outcome") != "success":
        raise CampaignError(f"campaign is not successful: {session_path}")
    _validate_signed_gate(session, "cycles")
    _validate_session_envelope(
        session,
        signed,
        expected_release_id=expected_release_id,
        expected_execution_source=expected_execution_source,
    )
    if (
        session.get("expected_release_id") != expected_release_id
        or session.get("expected_execution_source") != expected_execution_source
    ):
        raise CampaignError("campaign declaration does not match the expected release source")
    if expected_bundle_sha256 is not None:
        bundle = session.get("bundle")
        if not isinstance(bundle, dict) or bundle.get("sha256") != expected_bundle_sha256:
            raise CampaignError("candidate campaign did not stage the expected signed bundle")
    expected_identity = (
        expected_image_build_id,
        expected_image_sha256,
        expected_image_manifest_sha256,
        expected_key_policy_sha256,
    )
    if any(value is not None for value in expected_identity):
        if not all(isinstance(value, str) for value in expected_identity):
            raise CampaignError("candidate image identity expectation is incomplete")
        assert expected_image_build_id is not None
        assert expected_image_sha256 is not None
        assert expected_image_manifest_sha256 is not None
        assert expected_key_policy_sha256 is not None
        _validate_target_identity(
            session,
            release_id=expected_release_id,
            execution_source=expected_execution_source,
            image_build_id=expected_image_build_id,
            image_sha256=expected_image_sha256,
            image_manifest_sha256=expected_image_manifest_sha256,
            key_policy_sha256=expected_key_policy_sha256,
        )
    environment_identity = _environment_identity(session)
    if evidence_records is not None:
        evidence_records.append(
            {
                "path": str(session_path),
                "sha256": signed.sha256,
                "signature_sha256": signed.signature_sha256,
                "approval_public_key_sha256": signed.approval_public_key_sha256,
                "environment_identity": environment_identity,
            }
        )
    cycles = session.get("cycles")
    if not isinstance(cycles, list):
        raise CampaignError("campaign cycles are missing")
    values: list[CampaignCycle] = []
    for index, cycle in enumerate(cycles, start=1):
        if not isinstance(cycle, dict) or cycle.get("outcome") != "success":
            raise CampaignError("campaign contains an unsuccessful cycle")
        target = cycle.get("target")
        metrics = target.get("metrics") if isinstance(target, dict) else None
        if (
            not isinstance(target, dict)
            or not isinstance(metrics, dict)
            or metrics.get("success") is not True
        ):
            raise CampaignError("cycle metrics are missing or unsuccessful")
        if target.get("swap_policy") != {
            "active_entries": 0,
            "dphys_swapfile_masked": True,
            "swapfile_absent": True,
        }:
            raise CampaignError("cycle does not prove system swap was disabled")
        if target.get("storage_policy") != {
            "root_overlay": True,
            "boot_read_only": True,
            "data_uuid": "54540000-0000-4000-8000-000000000002",
            "data_read_write": True,
            "networkmanager_bind_from_data": True,
            "overlay_recurse": False,
        }:
            raise CampaignError("cycle does not prove the TIDEDATA/overlay storage policy")
        wifi_activity = cycle.get("wifi_activity")
        if (
            not isinstance(wifi_activity, dict)
            or wifi_activity.get("source") != "controller_ap_station_poll"
            or wifi_activity.get("clock") != "controller_monotonic_mapped_fixture_done"
            or wifi_activity.get("association_observed") is not True
            or wifi_activity.get("observed_seconds") != cycle.get("wifi_active_controller_seconds")
            or cycle.get("wifi_power_budget_seconds") != cycle.get("cold_wake_to_done_seconds")
        ):
            raise CampaignError("cycle lacks conservative controller Wi-Fi power evidence")
        _validate_reliability_cycle(
            cycle,
            expected_release_id=expected_release_id,
            expected_execution_source=expected_execution_source,
            expected_tool_identities=session["tool_identities"],
            expected_done_bounds=session["safety_gate"]["done_pulse"],
            label=f"campaign cycle {index}",
            camera_path=path / f"cycle-{index:03d}" / "display.jpg",
        )
        _execution_evidence(target, expected_release_id, expected_execution_source)
        swap = _number(metrics.get("swap_used_kib"), "swap_used_kib")
        if swap != 0:
            raise CampaignError("campaign used swap")
        values.append(
            CampaignCycle(
                started=_timestamp(cycle.get("started")),
                metrics=CycleMetrics(
                    wake_to_done_ms=_number(
                        cycle.get("cold_wake_to_done_seconds"),
                        "cold_wake_to_done_seconds",
                    )
                    * 1000,
                    wifi_active_ms=_number(
                        cycle.get("wifi_active_controller_seconds"),
                        "wifi_active_controller_seconds",
                    )
                    * 1000,
                    cpu_ms=(
                        _number(metrics.get("cpu_user_seconds"), "cpu_user_seconds")
                        + _number(metrics.get("cpu_system_seconds"), "cpu_system_seconds")
                    )
                    * 1000,
                    max_rss_kib=int(_number(metrics.get("max_rss_kib"), "max_rss_kib")),
                    bytes_written=int(
                        _number(metrics.get("process_write_bytes"), "process_write_bytes")
                    ),
                    refresh_count=int(
                        _number(
                            metrics.get("display_refresh_count"),
                            "display_refresh_count",
                        )
                    ),
                ),
            )
        )
    return tuple(values)


def _validate_uart_evidence(
    value: Any,
    label: str,
    *,
    artifact_path: pathlib.Path,
    required_event: str | None = None,
    required_phase: str | None = None,
    required_release_id: str | None = None,
    required_bundle_sha256: str | None = None,
    require_runtime_phase: bool = False,
) -> None:
    if (
        not isinstance(value, dict)
        or value.get("simulated") is True
        or not isinstance(value.get("size_bytes"), int)
        or isinstance(value.get("size_bytes"), bool)
        or not 1 <= value["size_bytes"] <= 4 * 1024 * 1024
        or not _hex_digest(value.get("sha256"))
        or not isinstance(value.get("schema1_event_count"), int)
        or value["schema1_event_count"] < 1
        or not isinstance(value.get("event_names"), list)
    ):
        raise CampaignError(f"{label} lacks bounded schema-1 UART evidence")
    if required_event is not None and value.get("required_event") != required_event:
        raise CampaignError(f"{label} UART evidence lacks {required_event}")
    if required_phase is not None and value.get("required_phase") != required_phase:
        raise CampaignError(f"{label} UART evidence lacks phase {required_phase}")
    if (
        required_release_id is not None
        and value.get("required_release_id") != required_release_id
    ):
        raise CampaignError(f"{label} UART evidence names another release")
    if (
        required_bundle_sha256 is not None
        and value.get("required_bundle_sha256") != required_bundle_sha256
    ):
        raise CampaignError(f"{label} UART evidence names another bundle")
    if (
        required_phase is not None
        and required_event is None
        and value.get("runtime_phase_matched") is not True
    ):
        raise CampaignError(f"{label} UART evidence did not match the runtime phase")
    if require_runtime_phase and not {
        "phase_started",
        "phase_finished",
    }.intersection(value["event_names"]):
        raise CampaignError(f"{label} UART evidence lacks runtime phase telemetry")
    payload = _stable_file_bytes(artifact_path, f"{label} UART log", 4 * 1024 * 1024)
    if (
        len(payload) != value["size_bytes"]
        or hashlib.sha256(payload).hexdigest() != value["sha256"]
    ):
        raise CampaignError(f"{label} UART log differs from signed evidence")


def _validate_portal_matrix(session: dict[str, Any]) -> None:
    _validate_signed_gate(session, "wifi_matrix")
    values = session.get("portal_matrix")
    if not isinstance(values, list) or len(values) != len(PORTAL_CASE_SPEC) + 1:
        raise CampaignError("portal profile matrix is incomplete")
    by_case: dict[str, dict[str, Any]] = {}
    for value in values:
        if not isinstance(value, dict):
            raise CampaignError("portal profile matrix contains an invalid case")
        case_id = value.get("case_id")
        if not isinstance(case_id, str) or case_id in by_case:
            raise CampaignError("portal profile matrix contains duplicate case IDs")
        by_case[case_id] = value
    if set(by_case) != {*PORTAL_CASE_SPEC, "failed_credentials_rollback"}:
        raise CampaignError("portal profile matrix does not have exact required coverage")

    def valid_controller_receipt(
        value: Any, case_id: str, phase: str, state: str
    ) -> bool:
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "operation",
            "case_id",
            "phase",
            "state",
            "scenario",
            "duration_seconds",
        }:
            return False
        duration = value.get("duration_seconds")
        if type(duration) not in {int, float}:
            return False
        assert isinstance(duration, (int, float)) and not isinstance(duration, bool)
        return (
            type(value.get("schema_version")) is int
            and value.get("schema_version") == 1
            and value.get("operation") == "portal-matrix"
            and value.get("case_id") == case_id
            and value.get("phase") == phase
            and value.get("state") == state
            and value.get("scenario") == "client_ap_transition"
            and math.isfinite(duration)
            and duration >= 0
        )

    for case_id, (security, hidden, operation) in PORTAL_CASE_SPEC.items():
        item = by_case[case_id]
        request = item.get("request")
        profile = item.get("networkmanager_profile")
        if (
            item.get("outcome") != "success"
            or item.get("usb_ssh_during_portal") != "reachable"
            or item.get("usb_ssh_after_radio_change") != "reachable"
            or not isinstance(request, dict)
            or request.get("case_id") != case_id
            or request.get("operation") != operation
            or request.get("http_status") != 202
            or request.get("ack_before_radio_change") is not True
            or request.get("security") != security
            or request.get("hidden") is not hidden
            or not _hex_digest(request.get("operation_id_sha256"))
            or not _hex_digest(request.get("ssid_sha256"))
            or not isinstance(profile, dict)
            or profile.get("schema_version") != 1
            or profile.get("event") != "network_profile_evidence"
            or profile.get("case_id") != case_id
            or profile.get("ssid_sha256") != request.get("ssid_sha256")
            or profile.get("security") != security
            or profile.get("hidden") is not hidden
            or profile.get("operation") != operation
            or profile.get("operation_state") != "succeeded"
            or profile.get("profile_mode") != "0600"
            or profile.get("profile_owner") != "root"
            or profile.get("profile_group") != "root"
            or profile.get("networkmanager_loaded") is not True
            or profile.get("simulated") is True
        ):
            raise CampaignError(f"portal profile case failed: {case_id}")
        reachable = item.get("portal_reachable")
        apply = item.get("controller_portal_apply")
        test_plane = item.get("controller_test_plane")
        reset = item.get("controller_portal_reset")
        transaction = item.get("transaction_coverage")
        if (
            not isinstance(reachable, dict)
            or reachable.get("status") != "reachable"
            or reachable.get("http_status") != 200
            or reachable.get("simulated") is True
            or not valid_controller_receipt(apply, case_id, "apply", "portal-client-active")
            or not valid_controller_receipt(
                test_plane, case_id, "test-plane", "test-plane-active"
            )
            or not valid_controller_receipt(reset, case_id, "reset", "normal-ap-restored")
            or transaction
            != {
                "test_proven": operation == "test",
                "save_proven": True,
                "force_save_proven": operation == "force-save",
            }
        ):
            raise CampaignError(f"portal radio transition was not proven: {case_id}")
    rollback = by_case["failed_credentials_rollback"]
    portal = rollback.get("portal")
    nm_rollback = rollback.get("networkmanager_rollback")
    rollback_apply = rollback.get("controller_portal_apply")
    rollback_reset = rollback.get("controller_portal_reset")
    if (
        rollback.get("outcome") != "success"
        or rollback.get("usb_ssh_before_transaction") != "reachable"
        or rollback.get("usb_ssh_after_rollback") != "reachable"
        or not isinstance(portal, dict)
        or portal.get("operation") != "failed-and-rolled-back"
        or portal.get("secret_recorded") is not False
        or portal.get("http_status") != 202
        or portal.get("ack_before_radio_change") is not True
        or not _hex_digest(portal.get("operation_id_sha256"))
        or portal.get("simulated") is True
        or not isinstance(nm_rollback, dict)
        or nm_rollback.get("schema_version") != 1
        or nm_rollback.get("event") != "network_rollback_evidence"
        or not _hex_digest(nm_rollback.get("ssid_sha256"))
        or nm_rollback.get("profile_absent") is not True
        or nm_rollback.get("active_profile_restored") is not True
        or nm_rollback.get("simulated") is True
        or not isinstance(rollback_apply, dict)
        or rollback_apply.get("scenario") != "client_ap_transition"
        or rollback_apply.get("phase") != "apply"
        or rollback_apply.get("simulated") is True
        or not isinstance(rollback_reset, dict)
        or rollback_reset.get("scenario") != "client_ap_transition"
        or rollback_reset.get("phase") != "reset"
        or rollback_reset.get("simulated") is True
    ):
        raise CampaignError("portal failed-credential rollback is incomplete")
    scans = session.get("portal_secret_artifact_scan")
    if not isinstance(scans, dict) or set(scans) != {"target", "controller_artifacts"}:
        raise CampaignError("portal secret-artifact scan is missing")
    for label, scan in scans.items():
        expected_fields = (
            {
                "schema_version",
                "event",
                "passed",
                "files_scanned",
                "forbidden_matches",
            }
            if label == "target"
            else {"passed", "files_scanned", "forbidden_matches", "method"}
        )
        if (
            not isinstance(scan, dict)
            or set(scan) != expected_fields
            or scan.get("passed") is not True
            or type(scan.get("files_scanned")) is not int
            or scan["files_scanned"] < 1
            or type(scan.get("forbidden_matches")) is not int
            or scan["forbidden_matches"] != 0
            or (label == "target" and type(scan.get("schema_version")) is not int)
        ):
            raise CampaignError(f"portal secret scan failed on {label}")
        if label == "target" and (
            type(scan.get("schema_version")) is not int
            or scan.get("schema_version") != 1
            or scan.get("event") != "network_secret_artifact_scan"
        ):
            raise CampaignError("target portal secret scan has no schema identity")
        if label == "controller_artifacts" and scan.get("method") != (
            "exact_private_config_credential_match"
        ):
            raise CampaignError("controller portal secret scan method is untrusted")


def _validate_wifi_matrix(
    session: dict[str, Any],
    *,
    session_path: pathlib.Path,
    expected_release_id: str,
    expected_execution_source: str,
) -> None:
    _validate_signed_gate(session, "wifi_matrix")
    values = session.get("wifi_matrix")
    if session.get("wifi_matrix_requested") is not True or not isinstance(values, list):
        raise CampaignError("Wi-Fi fault matrix was not requested and completed")
    by_scenario: dict[str, dict[str, Any]] = {}
    for value in values:
        if not isinstance(value, dict):
            raise CampaignError("Wi-Fi fault matrix contains an invalid result")
        scenario = value.get("scenario")
        if not isinstance(scenario, str) or scenario in by_scenario:
            raise CampaignError("Wi-Fi fault matrix contains duplicate scenarios")
        by_scenario[scenario] = value
    if set(by_scenario) != WIFI_SCENARIOS:
        raise CampaignError("Wi-Fi fault matrix does not have exact required coverage")
    transition = by_scenario["client_ap_transition"]
    rollback = by_scenario["bad_credentials"]
    if transition != {
        "scenario": "client_ap_transition",
        "outcome": "success",
        "profile_cases_completed": len(PORTAL_CASE_SPEC),
        "usb_management_reachable": True,
    }:
        raise CampaignError("client/AP transition evidence is incomplete")
    if rollback != {
        "scenario": "bad_credentials",
        "outcome": "success",
        "rollback_case_id": "failed_credentials_rollback",
        "rollback_proven": True,
    }:
        raise CampaignError("bad-credential rollback summary is incomplete")
    expected_faults = {
        "wifi_disconnect": None,
        "dns_failure": "dns",
        "tls_failure": "tls",
        "http_failure": "http_503",
    }
    for scenario, expected_fault in expected_faults.items():
        item = by_scenario[scenario]
        if item.get("outcome") != "success":
            raise CampaignError(f"Wi-Fi scenario failed: {scenario}")
        for stage in ("fault", "recovery"):
            cycle = item.get(f"{stage}_cycle")
            _validate_reliability_cycle(
                cycle,
                expected_release_id=expected_release_id,
                expected_execution_source=expected_execution_source,
                expected_tool_identities=session["tool_identities"],
                expected_done_bounds=session["safety_gate"]["done_pulse"],
                label=f"Wi-Fi {scenario} {stage}",
                camera_path=(
                    session_path
                    / f"wifi-matrix-{scenario}-{stage}"
                    / "display.jpg"
                ),
            )
            assert isinstance(cycle, dict)
            _validate_uart_evidence(
                cycle.get("uart"),
                f"Wi-Fi {scenario} {stage}",
                artifact_path=(
                    session_path
                    / f"wifi-matrix-{scenario}-{stage}"
                    / "uart.log"
                ),
                require_runtime_phase=True,
            )
            if cycle.get("usb_ssh_while_faulted_run") != "reachable":
                raise CampaignError(f"USB management was not proven during {scenario}/{stage}")
        fault_cycle = item["fault_cycle"]
        recovery_cycle = item["recovery_cycle"]
        assert isinstance(fault_cycle, dict) and isinstance(recovery_cycle, dict)
        fault_result = fault_cycle["target"]["last_result"]
        recovery_result = recovery_cycle["target"]["last_result"]
        if fault_result.get("used_cached_data") is not True:
            raise CampaignError(f"Wi-Fi fault did not use retained data: {scenario}")
        if scenario == "wifi_disconnect":
            for phase in ("apply", "probe", "reset"):
                control_evidence = item.get(phase)
                if (
                    not isinstance(control_evidence, dict)
                    or control_evidence.get("scenario") != scenario
                    or control_evidence.get("phase") != phase
                    or control_evidence.get("simulated") is True
                ):
                    raise CampaignError(f"Wi-Fi disconnect lacks controller {phase} proof")
            if fault_result.get("production_route_interface") is not None:
                raise CampaignError("Wi-Fi disconnect retained a production route")
        else:
            if (
                item.get("apply") != {"pending": True, "kind": expected_fault}
                or item.get("probe") != {"pending": True, "kind": expected_fault}
                or item.get("reset") != {"pending": False, "kind": None}
                or fault_result.get("injected_noaa_fault") != expected_fault
                or fault_result.get("production_route_interface") != "wlan0"
            ):
                raise CampaignError(f"transport fault did not remain on wlan0: {scenario}")
        if (
            recovery_result.get("network_interface") != "wlan0"
            or recovery_result.get("production_route_interface") != "wlan0"
        ):
            raise CampaignError(f"Wi-Fi recovery did not use wlan0: {scenario}")
    _validate_portal_matrix(session)


def _validate_lifecycle_power_cuts(
    session: dict[str, Any],
    *,
    session_path: pathlib.Path,
    expected_release_id: str,
    expected_execution_source: str,
) -> set[str]:
    phases = session.get("power_cut_phases")
    recoveries = session.get("power_cut_recoveries")
    if not phases and not recoveries:
        return set()
    _validate_signed_gate(session, "lifecycle_power_cuts")
    if (
        not isinstance(phases, list)
        or any(not isinstance(phase, str) for phase in phases)
        or len(phases) != len(set(phases))
        or not set(phases) <= LIFECYCLE_PHASES
        or not isinstance(recoveries, list)
        or len(recoveries) != len(phases)
    ):
        raise CampaignError("lifecycle power-cut declaration is invalid")
    by_phase: dict[str, dict[str, Any]] = {}
    for item in recoveries:
        if not isinstance(item, dict) or not isinstance(item.get("phase"), str):
            raise CampaignError("lifecycle power-cut result is invalid")
        phase = item["phase"]
        if phase in by_phase:
            raise CampaignError("lifecycle power-cut result repeats a phase")
        by_phase[phase] = item
    if set(by_phase) != set(phases):
        raise CampaignError("lifecycle power-cut results do not match requested phases")
    for phase, item in by_phase.items():
        cut = item.get("cut")
        if (
            item.get("outcome") != "success"
            or not isinstance(cut, dict)
            or cut.get("event") != "phase_power_cut"
            or cut.get("phase") != phase
        ):
            raise CampaignError(f"lifecycle power cut did not occur at {phase}")
        _validate_powered_off_rails(item.get("rails_after_cut"), f"lifecycle {phase} cut")
        _validate_powered_off_rails(
            item.get("rails_after_recovery"), f"lifecycle {phase} recovery"
        )
        _validate_uart_evidence(
            item.get("cut_uart"),
            f"lifecycle {phase} cut",
            artifact_path=session_path / f"power-cut-{phase}" / "cut-uart.log",
            required_phase=phase,
            require_runtime_phase=True,
        )
        _validate_uart_evidence(
            item.get("recovery_uart"),
            f"lifecycle {phase} recovery",
            artifact_path=session_path / f"power-cut-{phase}" / "recovery-uart.log",
            require_runtime_phase=True,
        )
        _validate_reliability_cycle(
            {
                "outcome": item.get("outcome"),
                "done": item.get("recovery_done"),
                "camera": item.get("camera"),
                "rails_after_done": item.get("rails_after_recovery"),
                "target": item.get("target"),
            },
            expected_release_id=expected_release_id,
            expected_execution_source=expected_execution_source,
            expected_tool_identities=session["tool_identities"],
            expected_done_bounds=session["safety_gate"]["done_pulse"],
            label=f"lifecycle {phase} recovery",
            camera_path=session_path / f"power-cut-{phase}" / "recovery-display.jpg",
        )
    return set(phases)


def _validate_update_power_cuts(
    session: dict[str, Any],
    *,
    session_path: pathlib.Path,
    expected_release_id: str,
    expected_bundle_sha256: str,
) -> bool:
    phases = session.get("update_power_cut_phases")
    recoveries = session.get("update_power_cut_recoveries")
    if not phases and not recoveries:
        return False
    _validate_signed_gate(session, "update_power_cuts", update_phases=UPDATE_POWER_CUT_PHASES)
    bundle = session.get("bundle")
    stage = bundle.get("stage") if isinstance(bundle, dict) else None
    if (
        not isinstance(phases, list)
        or len(phases) != len(set(phases))
        or set(phases) != UPDATE_POWER_CUT_PHASES
        or not isinstance(recoveries, list)
        or len(recoveries) != len(UPDATE_POWER_CUT_PHASES)
        or not isinstance(bundle, dict)
        or bundle.get("sha256") != expected_bundle_sha256
        or bundle.get("transfer") != "complete"
        or not isinstance(stage, dict)
        or stage.get("release_id") != expected_release_id
    ):
        raise CampaignError("update power-cut matrix is incomplete")
    expected_states = {
        "begin": "uploading",
        "stage": "staged",
        "verify": "verified",
        "activate": "trial",
    }
    by_phase: dict[str, dict[str, Any]] = {}
    for item in recoveries:
        if not isinstance(item, dict) or not isinstance(item.get("phase"), str):
            raise CampaignError("update power-cut result is invalid")
        phase = item["phase"]
        if phase in by_phase:
            raise CampaignError("update power-cut result repeats a phase")
        by_phase[phase] = item
    if set(by_phase) != UPDATE_POWER_CUT_PHASES:
        raise CampaignError("update power-cut results do not cover every phase")
    for phase, item in by_phase.items():
        cut = item.get("cut")
        transport = item.get("transport")
        status = item.get("target_update_status")
        if (
            item.get("outcome") != "success"
            or item.get("release_id") != expected_release_id
            or item.get("bundle_sha256") != expected_bundle_sha256
            or not isinstance(cut, dict)
            or cut.get("event") != "update_power_cut"
            or cut.get("phase") != phase
            or not isinstance(transport, dict)
            or transport.get("ssh_interrupted_by_power_cut") is not True
            or transport.get("simulated") is True
            or not isinstance(transport.get("ssh_returncode"), int)
            or transport.get("ssh_returncode") == 0
            or not _hex_digest(transport.get("stdout_sha256"))
            or not _hex_digest(transport.get("stderr_sha256"))
            or not isinstance(status, dict)
            or status.get("schema_version") != 1
            or status.get("event") != "update_power_cut_recovery"
            or status.get("phase") != phase
            or status.get("release_id") != expected_release_id
            or status.get("bundle_sha256") != expected_bundle_sha256
            or status.get("recovered") is not True
            or status.get("state_valid") is not True
            or status.get("observed_state") != expected_states[phase]
            or status.get("cleanup_complete") is not True
            or status.get("post_cleanup_clean") is not True
            or status.get("fallback_used") is not False
            or status.get("simulated") is True
            or item.get("cold_setup_recovery") != "reachable-over-usb"
        ):
            raise CampaignError(f"update power-cut recovery failed at {phase}")
        _validate_powered_off_rails(item.get("rails_after_cut"), f"update {phase} cut")
        _validate_powered_off_rails(
            item.get("rails_after_recovery"), f"update {phase} recovery"
        )
        _validate_camera(
            item.get("camera"),
            f"update {phase} recovery",
            expected_tool_identities=session["tool_identities"],
            artifact_path=(
                session_path / f"update-power-cut-{phase}" / "recovery-display.jpg"
            ),
        )
        _validate_uart_evidence(
            item.get("cut_uart"),
            f"update {phase} cut",
            artifact_path=session_path / f"update-power-cut-{phase}" / "cut-uart.log",
            required_event="update_phase_boundary",
            required_phase=phase,
            required_release_id=expected_release_id,
            required_bundle_sha256=expected_bundle_sha256,
        )
    return True


def _validate_accelerated_cycles(
    session: dict[str, Any],
    *,
    session_path: pathlib.Path,
    expected_release_id: str,
    expected_execution_source: str,
) -> bool:
    if session.get("accelerated_cycle_campaign") is not True:
        return False
    _validate_signed_gate(session, "cycles")
    cycles = session.get("cycles")
    if (
        session.get("accelerated_cycles_required") != 12
        or session.get("cycles_requested") != 12
        or not isinstance(cycles, list)
        or len(cycles) != 12
    ):
        raise CampaignError("accelerated cold-cycle campaign is not exactly twelve cycles")
    for index, cycle in enumerate(cycles, start=1):
        _validate_reliability_cycle(
            cycle,
            expected_release_id=expected_release_id,
            expected_execution_source=expected_execution_source,
            expected_tool_identities=session["tool_identities"],
            expected_done_bounds=session["safety_gate"]["done_pulse"],
            label=f"accelerated cycle {index}",
            camera_path=session_path / f"cycle-{index:03d}" / "display.jpg",
        )
    return True


def _validate_field_cycle(
    session: dict[str, Any],
    *,
    session_path: pathlib.Path,
    expected_release_id: str,
    expected_execution_source: str,
    expected_image_build_id: str,
    expected_key_policy_sha256: str,
) -> bool:
    if session.get("field_cycle_requested") is not True:
        return False
    _validate_signed_gate(session, "field_cycle")
    value = session.get("field_cycle")
    if not isinstance(value, dict) or value.get("outcome") != "success":
        raise CampaignError("final field cycle did not complete")
    for stage in ("usb_before_power", "usb_while_powered", "usb_after_power"):
        evidence = value.get(stage)
        if (
            not isinstance(evidence, dict)
            or evidence.get("usb_management") != "disconnected"
            or evidence.get("simulated") is True
            or evidence.get("probe_exit_code") != 0
            or evidence.get("probe_tool_identity")
            != session["tool_identities"]["usb_disconnected_probe"]
        ):
            raise CampaignError(f"field cycle did not prove USB disconnected: {stage}")
    _validate_done(
        value.get("done"),
        "field cycle",
        session["safety_gate"]["done_pulse"],
    )
    _validate_powered_off_rails(value.get("rails_after_done"), "field cycle")
    _validate_camera(
        value.get("camera"),
        "field cycle",
        expected_tool_identities=session["tool_identities"],
        artifact_path=session_path / "field-cycle" / "display.jpg",
    )
    uart = value.get("uart")
    _validate_uart_evidence(
        uart,
        "field cycle",
        artifact_path=session_path / "field-cycle" / "uart.log",
        require_runtime_phase=True,
    )
    assert isinstance(uart, dict)
    if uart.get("target_identity") != {
        "release_id": expected_release_id,
        "execution_source": expected_execution_source,
        "image_build_id": expected_image_build_id,
        "key_policy_sha256": expected_key_policy_sha256,
        "matched_signed_target_identity": True,
    }:
        raise CampaignError("field UART identity does not match the accepted image")
    return True


def load_release_evidence(
    paths: tuple[pathlib.Path, ...],
    *,
    expected_release_id: str,
    expected_execution_source: str,
    expected_bundle_sha256: str,
    expected_image_build_id: str,
    expected_image_sha256: str,
    expected_image_manifest_sha256: str,
    expected_key_policy_sha256: str,
    expected_openssl_sha256: str,
) -> dict[str, Any]:
    if not paths:
        raise CampaignError("release promotion requires signed auxiliary HIL sessions")
    lifecycle_phases: set[str] = set()
    update_complete = False
    wifi_complete = False
    accelerated_complete = False
    field_complete = False
    records: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    for path in paths:
        session_path = path / "session.json"
        signed = _load_signed_json(
            session_path,
            label=f"release evidence {session_path}",
            expected_openssl_sha256=expected_openssl_sha256,
        )
        if signed.sha256 in seen_hashes:
            raise CampaignError("release evidence repeats the same signed HIL session")
        seen_hashes.add(signed.sha256)
        session = signed.value
        if (
            session.get("schema_version") != 1
            or session.get("outcome") != "success"
            or session.get("expected_release_id") != expected_release_id
            or session.get("expected_execution_source") != expected_execution_source
            or "error" in session
            or "cleanup_error" in session
        ):
            raise CampaignError("auxiliary HIL session is unsuccessful or targets another release")
        _validate_target_identity(
            session,
            release_id=expected_release_id,
            execution_source=expected_execution_source,
            image_build_id=expected_image_build_id,
            image_sha256=expected_image_sha256,
            image_manifest_sha256=expected_image_manifest_sha256,
            key_policy_sha256=expected_key_policy_sha256,
        )
        _validate_session_envelope(
            session,
            signed,
            expected_release_id=expected_release_id,
            expected_execution_source=expected_execution_source,
        )
        environment_identity = _environment_identity(session)
        recognized = False
        phases = _validate_lifecycle_power_cuts(
            session,
            session_path=path,
            expected_release_id=expected_release_id,
            expected_execution_source=expected_execution_source,
        )
        if phases:
            if lifecycle_phases & phases:
                raise CampaignError("lifecycle power-cut evidence repeats a phase")
            lifecycle_phases.update(phases)
            recognized = True
        if session.get("update_power_cut_phases") or session.get(
            "update_power_cut_recoveries"
        ):
            if update_complete:
                raise CampaignError("update power-cut evidence is duplicated")
            update_complete = _validate_update_power_cuts(
                session,
                session_path=path,
                expected_release_id=expected_release_id,
                expected_bundle_sha256=expected_bundle_sha256,
            )
            recognized = True
        if session.get("wifi_matrix_requested") is True:
            if wifi_complete:
                raise CampaignError("Wi-Fi matrix evidence is duplicated")
            _validate_wifi_matrix(
                session,
                session_path=path,
                expected_release_id=expected_release_id,
                expected_execution_source=expected_execution_source,
            )
            wifi_complete = True
            recognized = True
        if session.get("accelerated_cycle_campaign") is True:
            if accelerated_complete:
                raise CampaignError("accelerated-cycle evidence is duplicated")
            accelerated_complete = _validate_accelerated_cycles(
                session,
                session_path=path,
                expected_release_id=expected_release_id,
                expected_execution_source=expected_execution_source,
            )
            recognized = True
        if session.get("field_cycle_requested") is True:
            if field_complete:
                raise CampaignError("field-cycle evidence is duplicated")
            field_complete = _validate_field_cycle(
                session,
                session_path=path,
                expected_release_id=expected_release_id,
                expected_execution_source=expected_execution_source,
                expected_image_build_id=expected_image_build_id,
                expected_key_policy_sha256=expected_key_policy_sha256,
            )
            recognized = True
        if not recognized:
            raise CampaignError("auxiliary HIL session does not satisfy a release gate")
        records.append(
            {
                "path": str(session_path),
                "sha256": signed.sha256,
                "signature_sha256": signed.signature_sha256,
                "approval_public_key_sha256": signed.approval_public_key_sha256,
                "environment_identity": environment_identity,
            }
        )
    if lifecycle_phases != LIFECYCLE_PHASES:
        raise CampaignError("release lacks every lifecycle power-cut recovery")
    if not update_complete:
        raise CampaignError("release lacks the complete update power-cut matrix")
    if not wifi_complete:
        raise CampaignError("release lacks the complete Wi-Fi/portal matrix")
    if not accelerated_complete:
        raise CampaignError("release lacks twelve accelerated cold cycles")
    if not field_complete:
        raise CampaignError("release lacks a USB-disconnected field cycle")
    environment_identity = _require_consistent_environment(records, "release evidence")
    return {
        "signed_sessions": records,
        "environment_identity": environment_identity,
        "target_identity": {
            "release_id": expected_release_id,
            "execution_source": expected_execution_source,
            "image_build_id": expected_image_build_id,
            "image_sha256": expected_image_sha256,
            "image_manifest_sha256": expected_image_manifest_sha256,
            "key_policy_sha256": expected_key_policy_sha256,
        },
        "lifecycle_power_cut_phases": sorted(lifecycle_phases),
        "update_power_cut_phases": sorted(UPDATE_POWER_CUT_PHASES),
        "wifi_scenarios": sorted(WIFI_SCENARIOS),
        "portal_profile_cases": [*PORTAL_CASE_SPEC, "failed_credentials_rollback"],
        "accelerated_cold_cycles": 12,
        "field_usb_disconnected": True,
    }


def atomic_write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=pathlib.Path, action="append", required=True)
    parser.add_argument("--candidate", type=pathlib.Path, action="append", required=True)
    parser.add_argument(
        "--release-evidence",
        type=pathlib.Path,
        action="append",
        required=True,
        help="directory containing an independently signed auxiliary HIL session",
    )
    parser.add_argument("--expected-baseline-release-id", required=True)
    parser.add_argument(
        "--expected-baseline-execution-source",
        choices=("built-in", "verified-release"),
        required=True,
    )
    parser.add_argument("--expected-candidate-release-id", required=True)
    parser.add_argument("--expected-candidate-bundle-sha256", required=True)
    parser.add_argument(
        "--expected-candidate-execution-source",
        choices=("built-in", "verified-release"),
        required=True,
    )
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--legacy-report", type=pathlib.Path, required=True)
    parser.add_argument("--image-manifest", type=pathlib.Path, required=True)
    parser.add_argument("--image-release-public-key", type=pathlib.Path, required=True)
    parser.add_argument(
        "--workstation-to-controller-ssh-public-key", type=pathlib.Path, required=True
    )
    parser.add_argument(
        "--controller-to-zero-ssh-public-key", type=pathlib.Path, required=True
    )
    parser.add_argument("--application-release-public-key", type=pathlib.Path, required=True)
    parser.add_argument("--lab-safety-public-key", type=pathlib.Path, required=True)
    parser.add_argument("--expected-image-release-public-key-sha256", required=True)
    parser.add_argument("--expected-image-manifest-sha256", required=True)
    parser.add_argument("--expected-image-sha256", required=True)
    parser.add_argument("--expected-image-build-id", required=True)
    parser.add_argument(
        "--expected-openssl-sha256",
        required=True,
        help="allowlisted SHA-256 of the fixed /usr/bin/openssl executable",
    )
    arguments = parser.parse_args(argv)
    if re.fullmatch(r"[0-9a-f]{64}", arguments.expected_candidate_bundle_sha256) is None:
        raise CampaignError("expected candidate bundle SHA-256 is invalid")
    if arguments.expected_candidate_execution_source != "verified-release":
        raise CampaignError("candidate promotion requires the verified-release execution source")
    image_manifest = load_image_manifest(
        arguments.image_manifest.resolve(),
        public_key=arguments.image_release_public_key.resolve(),
        expected_public_key_sha256=arguments.expected_image_release_public_key_sha256,
        expected_manifest_sha256=arguments.expected_image_manifest_sha256,
        expected_image_sha256=arguments.expected_image_sha256,
        expected_image_build_id=arguments.expected_image_build_id,
        expected_openssl_sha256=arguments.expected_openssl_sha256,
        workstation_to_controller_ssh_public_key=(
            arguments.workstation_to_controller_ssh_public_key.resolve()
        ),
        controller_to_zero_ssh_public_key=(
            arguments.controller_to_zero_ssh_public_key.resolve()
        ),
        application_release_public_key=arguments.application_release_public_key.resolve(),
        lab_safety_public_key=arguments.lab_safety_public_key.resolve(),
    )
    legacy_report = load_legacy_report(
        arguments.legacy_report.resolve(),
        expected_candidate_release_id=arguments.expected_candidate_release_id,
        expected_candidate_bundle_sha256=arguments.expected_candidate_bundle_sha256,
        expected_candidate_release_public_key_sha256=image_manifest[
            "application_release_public_key_sha256"
        ],
        expected_openssl_sha256=arguments.expected_openssl_sha256,
    )
    baseline_records: list[dict[str, Any]] = []
    candidate_records: list[dict[str, Any]] = []
    baseline = tuple(
        cycle
        for path in arguments.baseline
        for cycle in load_campaign(
            path.resolve(),
            expected_release_id=arguments.expected_baseline_release_id,
            expected_execution_source=arguments.expected_baseline_execution_source,
            expected_image_build_id=arguments.expected_image_build_id,
            expected_image_sha256=arguments.expected_image_sha256,
            expected_image_manifest_sha256=arguments.expected_image_manifest_sha256,
            expected_key_policy_sha256=image_manifest["key_policy_sha256"],
            expected_openssl_sha256=arguments.expected_openssl_sha256,
            evidence_records=baseline_records,
        )
    )
    candidate = tuple(
        cycle
        for path in arguments.candidate
        for cycle in load_campaign(
            path.resolve(),
            expected_release_id=arguments.expected_candidate_release_id,
            expected_execution_source=arguments.expected_candidate_execution_source,
            expected_bundle_sha256=arguments.expected_candidate_bundle_sha256,
            expected_image_build_id=arguments.expected_image_build_id,
            expected_image_sha256=arguments.expected_image_sha256,
            expected_image_manifest_sha256=arguments.expected_image_manifest_sha256,
            expected_key_policy_sha256=image_manifest["key_policy_sha256"],
            expected_openssl_sha256=arguments.expected_openssl_sha256,
            evidence_records=candidate_records,
        )
    )
    release_evidence = load_release_evidence(
        tuple(path.resolve() for path in arguments.release_evidence),
        expected_release_id=arguments.expected_candidate_release_id,
        expected_execution_source=arguments.expected_candidate_execution_source,
        expected_bundle_sha256=arguments.expected_candidate_bundle_sha256,
        expected_image_build_id=arguments.expected_image_build_id,
        expected_image_sha256=arguments.expected_image_sha256,
        expected_image_manifest_sha256=arguments.expected_image_manifest_sha256,
        expected_key_policy_sha256=image_manifest["key_policy_sha256"],
        expected_openssl_sha256=arguments.expected_openssl_sha256,
    )
    campaign_environment = _require_consistent_environment(
        [*baseline_records, *candidate_records], "baseline/candidate campaigns"
    )
    if release_evidence.get("environment_identity") != campaign_environment:
        raise CampaignError(
            "release evidence and benchmark campaigns used different lab systems"
        )
    order = sorted(
        [
            *((cycle.started, "baseline") for cycle in baseline),
            *((cycle.started, "candidate") for cycle in candidate),
        ],
        key=lambda item: item[0],
    )
    if any(left[0] == right[0] for left, right in zip(order, order[1:], strict=False)):
        raise CampaignError("campaign cycle timestamps are not unique")
    if any(left[1] == right[1] for left, right in zip(order, order[1:], strict=False)):
        raise CampaignError("baseline and candidate cycles were not run in alternating order")
    decision = evaluate_promotion(
        tuple(cycle.metrics for cycle in baseline),
        tuple(cycle.metrics for cycle in candidate),
        minimum_cycles=20,
    )
    result = json.loads(decision.to_json())
    result["campaign_integrity"] = {
        "alternating": True,
        "baseline_cycles": len(baseline),
        "candidate_cycles": len(candidate),
        "expected_baseline_release_id": arguments.expected_baseline_release_id,
        "expected_baseline_execution_source": arguments.expected_baseline_execution_source,
        "expected_candidate_release_id": arguments.expected_candidate_release_id,
        "expected_candidate_execution_source": arguments.expected_candidate_execution_source,
        "expected_candidate_bundle_sha256": arguments.expected_candidate_bundle_sha256,
        "signed_baseline_sessions": baseline_records,
        "signed_candidate_sessions": candidate_records,
        "environment_identity": campaign_environment,
        "promotion_verifier": {
            "path": str(OPENSSL_PATH),
            "sha256": arguments.expected_openssl_sha256,
        },
        "primary_wake_metric": "fixture_cold_wake_to_done_seconds",
        "primary_wifi_metric": "controller_observed_wifi_association_seconds",
        "conservative_wifi_power_upper_bound": (
            "fixture_power_on_to_done_wifi_budget_seconds"
        ),
        "signed_role_image_manifest": image_manifest,
        "legacy_framebuffer_verification": legacy_report,
        "release_gate_evidence": release_evidence,
    }
    payload = json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n"
    if arguments.output is None:
        sys.stdout.write(payload)
    else:
        atomic_write(arguments.output.resolve(), payload)
    return 0 if decision.accepted else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CampaignError as error:
        print(f"CAMPAIGN ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from None
