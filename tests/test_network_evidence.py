from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import tidetracker.network_evidence as network_evidence
from tidetracker.network_evidence import (
    CASE_SPEC,
    MAX_SECRET_INPUT_BYTES,
    NMCLI_PROCESS_TIMEOUT_SECONDS,
    USB_CONNECTION_ID,
    USB_CONNECTION_UUID,
    NetworkEvidenceError,
    NetworkEvidenceService,
    main,
)

MANAGEMENT_PROFILE = """[connection]
id=tidetracker-usb-management
uuid=eea12d5b-22cc-4fac-9002-545477000002
type=ethernet
interface-name=usb0
autoconnect=true
autoconnect-priority=100

[ethernet]
cloned-mac-address=02:54:54:77:00:02

[ipv4]
method=manual
address1=10.77.0.2/30
never-default=true
ignore-auto-dns=true
route-metric=32767
may-fail=false

[ipv6]
method=disabled

[proxy]
"""

PROFILE_UUIDS = (
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
)


@dataclass(frozen=True, slots=True)
class FakeConnection:
    name: str
    profile_uuid: str
    connection_type: str
    device: str
    ssid: str | None
    active: bool


class FakeNmcli:
    def __init__(self) -> None:
        self.connections: list[FakeConnection] = [
            FakeConnection(
                USB_CONNECTION_ID,
                USB_CONNECTION_UUID,
                "ethernet",
                "usb0",
                None,
                True,
            )
        ]
        self.calls: list[tuple[tuple[str, ...], float]] = []
        self.failure: BaseException | subprocess.CompletedProcess[str] | None = None

    def __call__(
        self, arguments: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(arguments)
        self.calls.append((command, timeout))
        if isinstance(self.failure, BaseException):
            raise self.failure
        if isinstance(self.failure, subprocess.CompletedProcess):
            return self.failure
        if "--fields" in command:
            active_only = command[-1] == "--active"
            selected = [
                connection
                for connection in self.connections
                if not active_only or connection.active
            ]
            stdout = "".join(
                "\t".join(
                    (
                        connection.name,
                        connection.profile_uuid,
                        connection.connection_type,
                        connection.device if connection.active else "",
                    )
                )
                + "\n"
                for connection in selected
            )
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")
        if "--get-values" in command:
            profile_uuid = command[-1]
            connection = next(
                item for item in self.connections if item.profile_uuid == profile_uuid
            )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f"{connection.ssid or ''}\n",
                stderr="",
            )
        raise AssertionError(f"unexpected fake nmcli command shape: {command!r}")

    def add(self, connection: FakeConnection) -> None:
        if connection.active and connection.connection_type == "wifi":
            self.connections = [
                replace(item, active=False)
                if item.connection_type == "wifi" and item.device == "wlan0"
                else item
                for item in self.connections
            ]
        self.connections.append(connection)


@dataclass(slots=True)
class EvidenceRig:
    service: NetworkEvidenceService
    nmcli: FakeNmcli
    persistent: Path
    runtime: Path
    artifacts: Path
    exclusions: tuple[Path, ...]


def write_private(path: Path, text: str) -> Path:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def client_profile(
    profile_uuid: str,
    ssid: str,
    security: str,
    hidden: bool,
    *,
    autoconnect: bool = True,
    identity: str = "private-enterprise-user",
    password: str = "private-enterprise-password",
) -> str:
    fingerprint = hashlib.sha256(
        f"{ssid}\0{security}\0{int(hidden)}".encode()
    ).hexdigest()[:12]
    sections = [
        "[connection]",
        f"id=tidetracker-{fingerprint}",
        f"uuid={profile_uuid}",
        "type=wifi",
        "interface-name=wlan0",
        f"autoconnect={'true' if autoconnect else 'false'}",
        "autoconnect-retries=1",
        "",
        "[wifi]",
        "mode=infrastructure",
        f"ssid={ssid}",
        f"hidden={'true' if hidden else 'false'}",
    ]
    if security == "wpa-personal":
        sections.extend(("", "[wifi-security]", "key-mgmt=wpa-psk", f"psk={password}"))
    elif security == "wpa-enterprise":
        sections.extend(
            (
                "",
                "[wifi-security]",
                "key-mgmt=wpa-eap",
                "",
                "[802-1x]",
                "eap=peap;",
                f"identity={identity}",
                f"password={password}",
                "phase2-auth=mschapv2",
            )
        )
    sections.extend(("", "[ipv4]", "method=auto", "", "[ipv6]", "method=auto", ""))
    return "\n".join(sections)


def hotspot_profile(profile_uuid: str) -> str:
    return f"""[connection]
id=tidetracker-hotspot-{profile_uuid[:8]}
uuid={profile_uuid}
type=wifi
interface-name=wlan0
autoconnect=false

[wifi]
mode=ap
band=bg
ssid=TideTracker-Setup

[ipv4]
method=shared
address1=192.168.42.1/24

[ipv6]
method=disabled
"""


@pytest.fixture
def rig(tmp_path: Path) -> EvidenceRig:
    persistent = tmp_path / "profiles"
    runtime = tmp_path / "runtime"
    artifacts = tmp_path / "artifacts"
    for directory in (persistent, runtime, artifacts):
        directory.mkdir(mode=0o700)
    write_private(persistent / "tidetracker-usb-management.nmconnection", MANAGEMENT_PROFILE)
    state = artifacts / "state"
    state.mkdir(mode=0o700)
    (state / "result.json").write_text('{"status":"safe"}\n', encoding="utf-8")
    exclusions = tuple(
        artifacts / name for name in ("networkmanager",)
    )
    nmcli = FakeNmcli()
    service = NetworkEvidenceService(
        profile_directory=persistent,
        runtime_profile_directory=runtime,
        artifact_roots=(artifacts,),
        excluded_source_roots=exclusions,
        runner=nmcli,
        required_profile_mode=0o666 if os.name == "nt" else 0o600,
        profile_wait_seconds=0,
    )
    return EvidenceRig(service, nmcli, persistent, runtime, artifacts, exclusions)


def add_client(
    rig: EvidenceRig,
    *,
    profile_uuid: str = PROFILE_UUIDS[0],
    ssid: str = "portal-lab-network",
    security: str = "open",
    hidden: bool = False,
    identity: str = "private-enterprise-user",
    password: str = "private-enterprise-password",
    active: bool = True,
    persistent: bool = True,
) -> Path:
    directory = rig.persistent if persistent else rig.runtime
    profile = client_profile(
        profile_uuid,
        ssid,
        security,
        hidden,
        autoconnect=persistent,
        identity=identity,
        password=password,
    )
    path = write_private(directory / f"tidetracker-{profile_uuid}.nmconnection", profile)
    fingerprint = hashlib.sha256(
        f"{ssid}\0{security}\0{int(hidden)}".encode()
    ).hexdigest()[:12]
    rig.nmcli.add(
        FakeConnection(
            f"tidetracker-{fingerprint}",
            profile_uuid,
            "wifi",
            "wlan0",
            ssid,
            active,
        )
    )
    return path


def add_hotspot(rig: EvidenceRig, profile_uuid: str = PROFILE_UUIDS[2]) -> Path:
    path = write_private(
        rig.runtime / f"tidetracker-{profile_uuid}.nmconnection",
        hotspot_profile(profile_uuid),
    )
    rig.nmcli.add(
        FakeConnection(
            f"tidetracker-hotspot-{profile_uuid[:8]}",
            profile_uuid,
            "wifi",
            "wlan0",
            "TideTracker-Setup",
            True,
        )
    )
    return path


@pytest.mark.parametrize(
    ("case_id", "security", "hidden", "operation"),
    [(case_id, *specification) for case_id, specification in CASE_SPEC.items()],
)
def test_profile_evidence_matches_exact_orchestrator_schema_for_full_matrix(
    rig: EvidenceRig,
    case_id: str,
    security: str,
    hidden: bool,
    operation: str,
) -> None:
    ssid = f"lab-{case_id}"[:32]
    add_client(
        rig,
        ssid=ssid,
        security=security,
        hidden=hidden,
        password="private-personal-password",
        active=operation == "test",
    )
    if operation == "force-save":
        add_hotspot(rig)
    ssid_sha256 = hashlib.sha256(ssid.encode()).hexdigest()

    assert rig.service.profile_evidence(
        case_id, ssid_sha256, security, hidden, operation
    ) == {
        "schema_version": 1,
        "event": "network_profile_evidence",
        "case_id": case_id,
        "ssid_sha256": ssid_sha256,
        "security": security,
        "hidden": hidden,
        "operation": operation,
        "operation_state": "succeeded",
        "profile_mode": "0600",
        "profile_owner": "root",
        "profile_group": "root",
        "networkmanager_loaded": True,
    }
    assert rig.nmcli.calls
    assert all(call[0][0] == "/usr/bin/nmcli" for call in rig.nmcli.calls)
    assert all(call[1] == NMCLI_PROCESS_TIMEOUT_SECONDS for call in rig.nmcli.calls)


@pytest.mark.parametrize(
    ("case_id", "security", "hidden", "operation"),
    [
        ("unknown", "open", False, "test"),
        ("visible_open_test", "wpa-personal", False, "test"),
        ("visible_open_test", "open", True, "test"),
        ("visible_open_test", "open", False, "force-save"),
    ],
)
def test_profile_evidence_rejects_values_outside_fixed_allowlist(
    rig: EvidenceRig,
    case_id: str,
    security: str,
    hidden: bool,
    operation: str,
) -> None:
    with pytest.raises(NetworkEvidenceError, match="not allowed"):
        rig.service.profile_evidence(case_id, "a" * 64, security, hidden, operation)


def test_profile_evidence_rejects_hash_permissions_autoconnect_and_nm_state(
    rig: EvidenceRig,
) -> None:
    ssid = "visible-open"
    path = add_client(rig, ssid=ssid)
    digest = hashlib.sha256(ssid.encode()).hexdigest()
    with pytest.raises(NetworkEvidenceError, match="still in progress"):
        rig.service.profile_evidence("visible_open_test", "b" * 64, "open", False, "test")

    path.chmod(0o444 if os.name == "nt" else 0o644)
    with pytest.raises(NetworkEvidenceError, match="unsafe permissions"):
        rig.service.profile_evidence("visible_open_test", digest, "open", False, "test")
    path.chmod(0o666 if os.name == "nt" else 0o600)

    path.write_text(
        client_profile(PROFILE_UUIDS[0], ssid, "open", False, autoconnect=False),
        encoding="utf-8",
    )
    path.chmod(0o666 if os.name == "nt" else 0o600)
    with pytest.raises(NetworkEvidenceError, match="does not match policy"):
        rig.service.profile_evidence("visible_open_test", digest, "open", False, "test")

    path.write_text(client_profile(PROFILE_UUIDS[0], ssid, "open", False), encoding="utf-8")
    path.chmod(0o666 if os.name == "nt" else 0o600)
    rig.nmcli.connections = [rig.nmcli.connections[0]]
    with pytest.raises(NetworkEvidenceError, match="still being loaded"):
        rig.service.profile_evidence("visible_open_test", digest, "open", False, "test")


def test_profile_evidence_rejects_non_root_profile_owner(
    rig: EvidenceRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    ssid = "visible-open"
    add_client(rig, ssid=ssid)
    real_fstat = network_evidence.os.fstat

    def non_root_fstat(descriptor: int) -> Any:
        metadata = real_fstat(descriptor)
        values = {
            name: getattr(metadata, name)
            for name in (
                "st_mode",
                "st_gid",
                "st_size",
                "st_mtime_ns",
                "st_dev",
                "st_ino",
                "st_ctime_ns",
            )
        }
        return SimpleNamespace(**values, st_uid=1000)

    monkeypatch.setattr(network_evidence.os, "fstat", non_root_fstat)
    with pytest.raises(NetworkEvidenceError, match="not owned by root"):
        rig.service.profile_evidence(
            "visible_open_test",
            hashlib.sha256(ssid.encode()).hexdigest(),
            "open",
            False,
            "test",
        )


def test_profile_evidence_rejects_keyfile_uuid_and_loaded_ssid_mismatches(
    rig: EvidenceRig,
) -> None:
    ssid = "visible-open"
    path = add_client(rig, ssid=ssid)
    digest = hashlib.sha256(ssid.encode()).hexdigest()
    value = path.read_text(encoding="utf-8").replace(
        f"uuid={PROFILE_UUIDS[0]}", f"uuid={PROFILE_UUIDS[1]}"
    )
    path.write_text(value, encoding="utf-8")
    path.chmod(0o666 if os.name == "nt" else 0o600)
    with pytest.raises(NetworkEvidenceError, match="does not match policy"):
        rig.service.profile_evidence("visible_open_test", digest, "open", False, "test")

    path.write_text(client_profile(PROFILE_UUIDS[0], ssid, "open", False), encoding="utf-8")
    path.chmod(0o666 if os.name == "nt" else 0o600)
    rig.nmcli.connections[-1] = replace(rig.nmcli.connections[-1], ssid="different-ssid")
    with pytest.raises(NetworkEvidenceError, match="wrong SSID"):
        rig.service.profile_evidence("visible_open_test", digest, "open", False, "test")


def test_profile_evidence_rejects_duplicate_logical_profiles(rig: EvidenceRig) -> None:
    ssid = "repeat-matrix-network"
    digest = hashlib.sha256(ssid.encode()).hexdigest()
    add_client(rig, profile_uuid=PROFILE_UUIDS[0], ssid=ssid, active=False)
    add_client(rig, profile_uuid=PROFILE_UUIDS[1], ssid=ssid, active=True)

    with pytest.raises(NetworkEvidenceError, match="not unique"):
        rig.service.profile_evidence("visible_open_test", digest, "open", False, "test")


def test_profile_and_artifact_symlinks_fail_closed(rig: EvidenceRig) -> None:
    ssid = "visible-open"
    path = add_client(rig, ssid=ssid)
    digest = hashlib.sha256(ssid.encode()).hexdigest()
    target = rig.persistent.parent / "client-target"
    path.replace(target)
    try:
        path.symlink_to(target)
    except OSError as exc:  # pragma: no cover - host policy may forbid symlinks
        pytest.skip(f"host cannot create symlinks: {exc}")
    with pytest.raises(NetworkEvidenceError, match="unsafe"):
        rig.service.profile_evidence("visible_open_test", digest, "open", False, "test")

    path.unlink()
    target.replace(path)
    add_hotspot(rig)
    artifact_target = rig.artifacts / "state" / "target.txt"
    artifact_target.write_text("safe", encoding="utf-8")
    artifact_link = rig.artifacts / "state" / "linked.txt"
    artifact_link.symlink_to(artifact_target)
    with pytest.raises(NetworkEvidenceError, match="symbolic link"):
        rig.service.secret_artifact_scan(["private-scan-value"])


def test_profile_evidence_rejects_stale_runtime_profile(rig: EvidenceRig) -> None:
    ssid = "visible-open"
    add_client(rig, ssid=ssid)
    add_client(
        rig,
        profile_uuid=PROFILE_UUIDS[1],
        ssid="stale-runtime-client",
        active=False,
        persistent=False,
    )
    with pytest.raises(NetworkEvidenceError, match="still being finalized"):
        rig.service.profile_evidence(
            "visible_open_test",
            hashlib.sha256(ssid.encode()).hexdigest(),
            "open",
            False,
            "test",
        )


def test_profile_evidence_retries_only_transient_async_state(rig: EvidenceRig) -> None:
    case_id = "visible_open_test"
    ssid = "eventually-visible-open"
    digest = hashlib.sha256(ssid.encode()).hexdigest()
    add_hotspot(rig)
    now = 10.0
    slept: list[float] = []

    def monotonic() -> float:
        return now

    def finish_transaction(delay: float) -> None:
        nonlocal now
        slept.append(delay)
        now += delay
        for path in tuple(rig.runtime.iterdir()):
            path.unlink()
        rig.nmcli.connections = [
            connection
            for connection in rig.nmcli.connections
            if connection.connection_type != "wifi"
        ]
        add_client(rig, ssid=ssid)

    waiting = NetworkEvidenceService(
        profile_directory=rig.persistent,
        runtime_profile_directory=rig.runtime,
        artifact_roots=(rig.artifacts,),
        excluded_source_roots=rig.exclusions,
        runner=rig.nmcli,
        required_profile_mode=0o666 if os.name == "nt" else 0o600,
        profile_wait_seconds=1,
        profile_poll_seconds=0.1,
        monotonic=monotonic,
        sleeper=finish_transaction,
    )
    result = waiting.profile_evidence(case_id, digest, "open", False, "test")
    assert result["operation_state"] == "succeeded"
    assert slept == [0.1]

    client = next(
        path
        for path in rig.persistent.iterdir()
        if path.name != "tidetracker-usb-management.nmconnection"
    )
    client.chmod(0o444 if os.name == "nt" else 0o644)
    terminal_sleeps: list[float] = []
    terminal = NetworkEvidenceService(
        profile_directory=rig.persistent,
        runtime_profile_directory=rig.runtime,
        artifact_roots=(rig.artifacts,),
        excluded_source_roots=rig.exclusions,
        runner=rig.nmcli,
        required_profile_mode=0o666 if os.name == "nt" else 0o600,
        profile_wait_seconds=1,
        profile_poll_seconds=0.1,
        monotonic=monotonic,
        sleeper=terminal_sleeps.append,
    )
    with pytest.raises(NetworkEvidenceError, match="unsafe permissions"):
        terminal.profile_evidence(case_id, digest, "open", False, "test")
    assert terminal_sleeps == []


def test_profile_evidence_caps_nmcli_at_the_overall_deadline(rig: EvidenceRig) -> None:
    ssid = "deadline-network"
    digest = hashlib.sha256(ssid.encode()).hexdigest()
    add_client(rig, ssid=ssid)
    now = [20.0]
    observed: list[tuple[tuple[str, ...], float]] = []

    def slow_nmcli(
        arguments: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        observed.append((tuple(arguments), timeout))
        now[0] += timeout
        raise subprocess.TimeoutExpired(arguments, timeout)

    bounded = NetworkEvidenceService(
        profile_directory=rig.persistent,
        runtime_profile_directory=rig.runtime,
        artifact_roots=(rig.artifacts,),
        excluded_source_roots=rig.exclusions,
        runner=slow_nmcli,
        required_profile_mode=0o666 if os.name == "nt" else 0o600,
        profile_wait_seconds=1,
        profile_poll_seconds=0.1,
        monotonic=lambda: now[0],
        sleeper=lambda _: pytest.fail("deadline exhaustion must not sleep"),
    )
    with pytest.raises(NetworkEvidenceError, match="before its deadline"):
        bounded.profile_evidence("visible_open_test", digest, "open", False, "test")

    assert len(observed) == 1
    assert observed[0][1] == 1
    assert observed[0][0][1:3] == ("--wait", "1")


def test_profile_evidence_enforces_test_and_force_save_active_state(
    rig: EvidenceRig,
) -> None:
    ssid = "hidden-force-save"
    digest = hashlib.sha256(ssid.encode()).hexdigest()
    add_client(rig, ssid=ssid, hidden=True, active=False)
    with pytest.raises(NetworkEvidenceError, match="retain the setup hotspot"):
        rig.service.profile_evidence(
            "hidden_open_force_save", digest, "open", True, "force-save"
        )

    add_hotspot(rig)
    candidate_uuid = PROFILE_UUIDS[0]
    rig.nmcli.connections = [
        replace(connection, active=True)
        if connection.profile_uuid == candidate_uuid
        else connection
        for connection in rig.nmcli.connections
    ]
    with pytest.raises(NetworkEvidenceError, match="activated its untested"):
        rig.service.profile_evidence(
            "hidden_open_force_save", digest, "open", True, "force-save"
        )



def test_rollback_proves_disk_loaded_absence_and_setup_hotspot(rig: EvidenceRig) -> None:
    add_client(rig, ssid="saved-client", active=False)
    add_hotspot(rig)
    bad_digest = hashlib.sha256(b"intentionally-bad-network").hexdigest()
    assert rig.service.rollback_evidence(bad_digest) == {
        "schema_version": 1,
        "event": "network_rollback_evidence",
        "ssid_sha256": bad_digest,
        "profile_absent": True,
        "active_profile_restored": True,
    }


def test_rollback_rejects_bad_profile_on_disk_or_only_in_nm_memory(rig: EvidenceRig) -> None:
    bad_ssid = "intentionally-bad-network"
    bad_digest = hashlib.sha256(bad_ssid.encode()).hexdigest()
    add_client(rig, ssid="saved-client", active=False)
    add_hotspot(rig)
    bad_path = write_private(
        rig.runtime / f"tidetracker-{PROFILE_UUIDS[1]}.nmconnection",
        client_profile(
            PROFILE_UUIDS[1], bad_ssid, "wpa-personal", True, autoconnect=False
        ),
    )
    with pytest.raises(NetworkEvidenceError, match="remains on disk"):
        rig.service.rollback_evidence(bad_digest)

    bad_path.unlink()
    rig.nmcli.connections.append(
        FakeConnection(
            "foreign-wifi-profile",
            PROFILE_UUIDS[1],
            "wifi",
            "",
            bad_ssid,
            False,
        )
    )
    with pytest.raises(NetworkEvidenceError, match="remains loaded"):
        rig.service.rollback_evidence(bad_digest)


def test_rollback_rejects_missing_or_inactive_setup_hotspot(rig: EvidenceRig) -> None:
    bad_digest = hashlib.sha256(b"intentionally-bad-network").hexdigest()
    with pytest.raises(NetworkEvidenceError, match="not restored"):
        rig.service.rollback_evidence(bad_digest)

    add_hotspot(rig)
    rig.nmcli.connections = [replace(item, active=False) for item in rig.nmcli.connections]
    with pytest.raises(NetworkEvidenceError, match="expected Wi-Fi state"):
        rig.service.rollback_evidence(bad_digest)


def test_secret_scan_uses_only_profile_secrets_and_excludes_source_trees(
    rig: EvidenceRig,
) -> None:
    identity = "private-enterprise-identity"
    enterprise_password = "private-enterprise-password"
    personal_password = "private-personal-password"
    add_client(
        rig,
        profile_uuid=PROFILE_UUIDS[0],
        ssid="personal-network",
        security="wpa-personal",
        password=personal_password,
        active=False,
    )
    add_client(
        rig,
        profile_uuid=PROFILE_UUIDS[1],
        ssid="enterprise-network",
        security="wpa-enterprise",
        identity=identity,
        password=enterprise_password,
        active=False,
    )
    add_hotspot(rig)
    credential_source = rig.exclusions[0]
    credential_source.mkdir(mode=0o700)
    (credential_source / "credential-source.txt").write_text(
        personal_password, encoding="utf-8"
    )

    result = rig.service.secret_artifact_scan(
        sorted(
            {
                identity,
                enterprise_password,
                personal_password,
                "failed-private-password",
            }
        )
    )
    assert result == {
        "schema_version": 1,
        "event": "network_secret_artifact_scan",
        "passed": True,
        "files_scanned": 1,
        "forbidden_matches": 0,
    }
    serialized = json.dumps(result)
    assert identity not in serialized
    assert enterprise_password not in serialized
    assert personal_password not in serialized


@pytest.mark.parametrize("source_tree", ["incoming", "releases", "uploads"])
def test_secret_scan_does_not_exclude_release_or_upload_trees(
    rig: EvidenceRig, source_tree: str
) -> None:
    password = "failed-profile-password-never-emit"
    add_hotspot(rig)
    directory = rig.artifacts / source_tree
    directory.mkdir(mode=0o700)
    (directory / "payload.bin").write_bytes(password.encode())
    with pytest.raises(NetworkEvidenceError) as captured:
        rig.service.secret_artifact_scan([password])
    assert password not in str(captured.value)


def test_secret_scan_rejects_secret_without_emitting_it(rig: EvidenceRig) -> None:
    password = "do-not-emit-this-private-password"
    add_client(
        rig,
        ssid="personal-network",
        security="wpa-personal",
        password=password,
        active=False,
    )
    add_hotspot(rig)
    (rig.artifacts / "state" / "leak.json").write_text(password, encoding="utf-8")

    with pytest.raises(NetworkEvidenceError) as captured:
        rig.service.secret_artifact_scan([password])
    assert password not in str(captured.value)


@pytest.mark.parametrize(
    "password",
    [
        'private-quote"and\\slash-password',
        "private-non-ascii-pässword-秘密",
    ],
)
def test_secret_scan_detects_json_escaped_credentials_without_disclosure(
    rig: EvidenceRig, password: str
) -> None:
    add_hotspot(rig)
    serialized = json.dumps({"password": password}, ensure_ascii=True)
    (rig.artifacts / "state" / "leak.json").write_text(serialized, encoding="utf-8")

    with pytest.raises(NetworkEvidenceError) as captured:
        rig.service.secret_artifact_scan([password])
    assert password not in str(captured.value)
    assert json.dumps(password, ensure_ascii=True) not in str(captured.value)


@pytest.mark.skipif(os.name == "nt", reason="field fd-relative traversal is POSIX-only")
def test_secret_scan_fails_closed_on_directory_rename_replacement_race(
    rig: EvidenceRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_hotspot(rig)
    secret = "private-directory-race-password"
    state = rig.artifacts / "state"
    (state / "result.json").write_text(secret, encoding="utf-8")
    original_listdir = network_evidence.os.listdir
    raced = False

    def replace_after_enumeration(path: os.PathLike[str] | int) -> list[str]:
        nonlocal raced
        names = original_listdir(path)
        if isinstance(path, int) and not raced:
            raced = True
            state.rename(rig.artifacts / "state-original")
            state.mkdir(mode=0o700)
            (state / "result.json").write_text('{"status":"safe"}', encoding="utf-8")
        return names

    monkeypatch.setattr(network_evidence.os, "listdir", replace_after_enumeration)
    with pytest.raises(NetworkEvidenceError, match="directory changed"):
        rig.service.secret_artifact_scan([secret])
    assert raced is True


def test_secret_scan_rejects_stale_runtime_and_artifact_bounds(rig: EvidenceRig) -> None:
    add_hotspot(rig)
    stale = rig.runtime / ".tidetracker-stale.tmp"
    write_private(stale, "private-stale-profile")
    with pytest.raises(NetworkEvidenceError, match="filename is not allowed"):
        rig.service.secret_artifact_scan(["private-stale-profile"])
    stale.unlink()

    bounded = NetworkEvidenceService(
        profile_directory=rig.persistent,
        runtime_profile_directory=rig.runtime,
        artifact_roots=(rig.artifacts,),
        excluded_source_roots=rig.exclusions,
        runner=rig.nmcli,
        required_profile_mode=0o666 if os.name == "nt" else 0o600,
        max_artifact_file_bytes=4,
    )
    with pytest.raises(NetworkEvidenceError, match="size policy"):
        bounded.secret_artifact_scan(["private-scan-value"])

    file_bounded = NetworkEvidenceService(
        profile_directory=rig.persistent,
        runtime_profile_directory=rig.runtime,
        artifact_roots=(rig.artifacts,),
        excluded_source_roots=rig.exclusions,
        runner=rig.nmcli,
        required_profile_mode=0o666 if os.name == "nt" else 0o600,
        max_artifact_files=1,
    )
    (rig.artifacts / "state" / "second.json").write_text("{}", encoding="utf-8")
    with pytest.raises(NetworkEvidenceError, match="file count"):
        file_bounded.secret_artifact_scan(["private-scan-value"])


@pytest.mark.parametrize("failure_kind", ["exit", "timeout"])
def test_nmcli_failures_are_bounded_and_do_not_emit_external_output(
    rig: EvidenceRig, failure_kind: str
) -> None:
    password = "private-nmcli-stderr-password"
    ssid = "visible-open"
    add_client(rig, ssid=ssid)
    if failure_kind == "exit":
        rig.nmcli.failure = subprocess.CompletedProcess(
            ["/usr/bin/nmcli"], 10, stdout=password, stderr=password
        )
    else:
        rig.nmcli.failure = subprocess.TimeoutExpired(
            ["/usr/bin/nmcli"], NMCLI_PROCESS_TIMEOUT_SECONDS, output=password
        )
    with pytest.raises(NetworkEvidenceError) as captured:
        rig.service.profile_evidence(
            "visible_open_test",
            hashlib.sha256(ssid.encode()).hexdigest(),
            "open",
            False,
            "test",
        )
    assert password not in str(captured.value)


def test_cli_requires_root_owned_zero_role_and_never_echoes_arguments(
    rig: EvidenceRig, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    role = tmp_path / "role"
    role.write_bytes(b"zero\n")
    role.chmod(0o644)
    secret_argument = "private-cli-argument"

    assert main(
        [secret_argument],
        service=rig.service,
        role_path=role,
        effective_uid=lambda: 1,
        enforce_role_permissions=os.name != "nt",
    ) == 2
    captured = capsys.readouterr()
    assert secret_argument not in captured.err
    assert "requires root" in captured.err

    role.write_bytes(b"controller\n")
    assert main(
        [secret_argument],
        service=rig.service,
        role_path=role,
        effective_uid=lambda: 0,
        enforce_role_permissions=os.name != "nt",
    ) == 2
    captured = capsys.readouterr()
    assert secret_argument not in captured.err
    assert "Pi Zero role" in captured.err

    role.write_bytes(b"zero\n")
    assert main(
        [secret_argument],
        service=rig.service,
        role_path=role,
        effective_uid=lambda: 0,
        enforce_role_permissions=os.name != "nt",
    ) == 2
    captured = capsys.readouterr()
    assert secret_argument not in captured.err
    assert "not allowed" in captured.err


def test_cli_success_emits_only_exact_json(rig: EvidenceRig, tmp_path: Path, capsys: Any) -> None:
    role = tmp_path / "role"
    role.write_bytes(b"zero\n")
    role.chmod(0o644)
    ssid = "visible-open"
    add_client(rig, ssid=ssid)
    digest = hashlib.sha256(ssid.encode()).hexdigest()
    assert main(
        ["network-profile-evidence", "visible_open_test", digest, "open", "false", "test"],
        service=rig.service,
        role_path=role,
        effective_uid=lambda: 0,
        enforce_role_permissions=os.name != "nt",
    ) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["ssid_sha256"] == digest


def test_secret_scan_cli_uses_bounded_stdin_for_deleted_bad_credential(
    rig: EvidenceRig, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    role = tmp_path / "role"
    role.write_bytes(b"zero\n")
    role.chmod(0o644)
    add_hotspot(rig)
    deleted_bad_password = "deleted-bad-profile-password"
    leak = rig.artifacts / "state" / "failed-transaction.json"
    leak.write_text(deleted_bad_password, encoding="utf-8")
    payload = json.dumps(
        {"schema_version": 1, "secrets": [deleted_bad_password]},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    assert main(
        ["network-secret-artifact-scan"],
        service=rig.service,
        role_path=role,
        effective_uid=lambda: 0,
        enforce_role_permissions=os.name != "nt",
        secret_input=payload,
    ) == 2
    captured = capsys.readouterr()
    assert deleted_bad_password not in captured.err
    assert captured.out == ""

    leak.unlink()
    assert main(
        ["network-secret-artifact-scan"],
        service=rig.service,
        role_path=role,
        effective_uid=lambda: 0,
        enforce_role_permissions=os.name != "nt",
        secret_input=payload,
    ) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["passed"] is True
    assert deleted_bad_password not in captured.out


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b'{"schema_version":1,"schema_version":1,"secrets":["private-value"]}',
        b'{"schema_version":1,"secrets":[]}',
        b'{"schema_version":1,"secrets":["z-private","a-private"]}',
        b'{"schema_version":1,"secrets":["private-value","private-value"]}',
        b"x" * (MAX_SECRET_INPUT_BYTES + 1),
    ],
    ids=("missing-fields", "duplicate-key", "empty", "unsorted", "duplicate", "oversize"),
)
def test_secret_scan_cli_rejects_noncanonical_or_unbounded_stdin_without_echo(
    rig: EvidenceRig,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload: bytes,
) -> None:
    role = tmp_path / "role"
    role.write_bytes(b"zero\n")
    role.chmod(0o644)
    add_hotspot(rig)
    assert main(
        ["network-secret-artifact-scan"],
        service=rig.service,
        role_path=role,
        effective_uid=lambda: 0,
        enforce_role_permissions=os.name != "nt",
        secret_input=payload,
    ) == 2
    captured = capsys.readouterr()
    assert "private-value" not in captured.err
    assert "z-private" not in captured.err
    assert "a-private" not in captured.err


def test_lab_control_exposes_read_only_strict_dispatch() -> None:
    helper = Path("deploy/sbin/tidetracker-lab-control").read_text(encoding="utf-8")
    for command in (
        "network-profile-evidence",
        "network-rollback-evidence",
        "network-secret-artifact-scan",
    ):
        assert f"{command})" in helper
        assert f"tidetracker.network_evidence {command}" in helper
    assert "network-profile-evidence is read-only and rejects --execute" in helper
    assert "network-rollback-evidence is read-only and rejects --execute" in helper
    assert "network-secret-artifact-scan is read-only and rejects --execute" in helper
