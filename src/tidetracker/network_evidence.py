"""Fail-closed, secret-free NetworkManager evidence for Pi Zero HIL tests.

The lab orchestrator invokes this module through the root-owned target helper.
Every operation is read-only: keyfiles and NetworkManager state are inspected,
never changed.  Successful output deliberately contains hashes and booleans only.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

PROFILE_DIRECTORY = Path("/etc/NetworkManager/system-connections")
RUNTIME_PROFILE_DIRECTORY = Path("/run/tidetracker/network-profiles")
ROLE_PATH = Path("/etc/tidetracker/role")
NMCLI = "/usr/bin/nmcli"
ARTIFACT_ROOTS = (Path("/var/lib/tidetracker"), Path("/run/tidetracker"))
EXCLUDED_SOURCE_ROOTS = (
    Path("/var/lib/tidetracker/networkmanager"),
    Path("/run/tidetracker/network-profiles"),
)

SETUP_SSID = "TideTracker-Setup"
USB_PROFILE_NAME = "tidetracker-usb-management.nmconnection"
USB_CONNECTION_ID = "tidetracker-usb-management"
USB_CONNECTION_UUID = "eea12d5b-22cc-4fac-9002-545477000002"

CASE_SPEC: dict[str, tuple[str, bool, str]] = {
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

PROFILE_NAME_PATTERN = re.compile(
    r"tidetracker-([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})"
    r"\.nmconnection"
)
PROFILE_ID_PATTERN = re.compile(r"tidetracker-[0-9a-f]{12}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
SAFE_NM_TYPE_PATTERN = re.compile(r"[A-Za-z0-9-]{1,32}")
SAFE_DEVICE_PATTERN = re.compile(r"(?:--|[A-Za-z0-9_.:-]{1,32})")

MAX_PROFILE_FILES = 64
MAX_PROFILE_BYTES = 32 * 1024
MAX_NMCLI_OUTPUT_BYTES = 64 * 1024
MAX_SECRET_INPUT_BYTES = 64 * 1024
MAX_SUPPLIED_SECRETS = 32
MAX_SUPPLIED_SECRET_BYTES = 512
MAX_ARTIFACT_DIRECTORIES = 512
MAX_ARTIFACT_FILES = 4096
MAX_ARTIFACT_FILE_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT_TOTAL_BYTES = 128 * 1024 * 1024
NMCLI_WAIT_SECONDS = 10
NMCLI_PROCESS_TIMEOUT_SECONDS = 12.0
PROFILE_EVIDENCE_WAIT_SECONDS = 90.0
PROFILE_EVIDENCE_POLL_SECONDS = 0.5

ProfileSource = Literal["persistent", "runtime"]
ProfileKind = Literal["management", "client", "hotspot"]


class NetworkEvidenceError(RuntimeError):
    """Network evidence cannot be established without exposing private data."""


class NetworkEvidencePending(NetworkEvidenceError):
    """A valid asynchronous portal transaction has not reached its evidence state."""


class NmcliRunner(Protocol):
    def __call__(
        self, arguments: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]: ...


def _subprocess_nmcli(
    arguments: Sequence[str], *, timeout: float
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["LC_ALL"] = "C"
    return subprocess.run(
        list(arguments),
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
        env=environment,
    )


@dataclass(frozen=True, slots=True)
class ParsedProfile:
    source: ProfileSource
    kind: ProfileKind
    connection_id: str
    profile_uuid: str
    ssid: str | None
    security: str | None
    hidden: bool | None
    secrets: tuple[bytes, ...]


@dataclass(frozen=True, slots=True)
class NmConnection:
    name: str
    profile_uuid: str
    connection_type: str
    device: str


def _stable_fields() -> tuple[str, ...]:
    if os.name == "nt":
        return ("st_size", "st_mtime_ns")
    return ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")


def _snapshot_regular(
    path: Path,
    *,
    maximum_bytes: int,
    require_nonempty: bool,
    require_root_owner: bool = False,
    required_mode: int | None = None,
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise NetworkEvidenceError("a required evidence file is unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise NetworkEvidenceError("an evidence path is not a regular file")
        if require_root_owner and (before.st_uid != 0 or before.st_gid != 0):
            raise NetworkEvidenceError("an evidence file is not owned by root")
        if required_mode is not None and stat.S_IMODE(before.st_mode) != required_mode:
            raise NetworkEvidenceError("an evidence file has unsafe permissions")
        if before.st_size > maximum_bytes or (require_nonempty and before.st_size <= 0):
            raise NetworkEvidenceError("an evidence file exceeds its size policy")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        current = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise NetworkEvidenceError("an evidence file changed during inspection") from exc
    fields = _stable_fields()
    if (
        len(payload) != before.st_size
        or len(payload) > maximum_bytes
        or any(getattr(before, field) != getattr(after, field) for field in fields)
        or any(getattr(before, field) != getattr(current, field) for field in fields)
    ):
        raise NetworkEvidenceError("an evidence file changed during inspection")
    return payload


def _same_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return all(getattr(left, field) == getattr(right, field) for field in _stable_fields())


def _snapshot_regular_at(
    directory_fd: int,
    name: str,
    *,
    maximum_bytes: int,
) -> bytes:
    """Read a stable regular file without resolving it through a mutable pathname."""

    try:
        initial = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise NetworkEvidenceError("an artifact changed during inspection") from exc
    if stat.S_ISLNK(initial.st_mode):
        raise NetworkEvidenceError("an artifact symbolic link is not allowed")
    if not stat.S_ISREG(initial.st_mode):
        raise NetworkEvidenceError("an artifact path is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise NetworkEvidenceError("an artifact changed during inspection") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not _same_snapshot(initial, before):
            raise NetworkEvidenceError("an artifact changed during inspection")
        if before.st_size > maximum_bytes:
            raise NetworkEvidenceError("an evidence file exceeds its size policy")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise NetworkEvidenceError("an artifact changed during inspection") from exc
    if (
        len(payload) != before.st_size
        or len(payload) > maximum_bytes
        or not _same_snapshot(before, after)
        or not _same_snapshot(before, current)
    ):
        raise NetworkEvidenceError("an artifact changed during inspection")
    return payload


def _require_safe_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise NetworkEvidenceError("an evidence directory is unavailable") from exc
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise NetworkEvidenceError("an evidence directory is unsafe")


def _canonical_uuid(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise NetworkEvidenceError("a NetworkManager profile has an invalid UUID") from exc
    canonical = str(parsed)
    if canonical != value or parsed.version != 4:
        raise NetworkEvidenceError("a NetworkManager profile has an invalid UUID")
    return canonical


def _decode_profile(payload: bytes) -> configparser.RawConfigParser:
    try:
        text = payload.decode("utf-8")
        parser = configparser.RawConfigParser(interpolation=None, strict=True)
        parser.read_string(text)
    except (UnicodeDecodeError, configparser.Error) as exc:
        raise NetworkEvidenceError("a NetworkManager profile is invalid") from exc
    return parser


def _require_sections(
    parser: configparser.RawConfigParser, expected: dict[str, frozenset[str]]
) -> None:
    if set(parser.sections()) != set(expected):
        raise NetworkEvidenceError("a NetworkManager profile contains unexpected sections")
    for section, keys in expected.items():
        if set(parser[section]) != set(keys):
            raise NetworkEvidenceError("a NetworkManager profile contains unexpected fields")


def _value(parser: configparser.RawConfigParser, section: str, key: str) -> str:
    try:
        return parser[section][key]
    except KeyError as exc:  # pragma: no cover - guarded by _require_sections
        raise NetworkEvidenceError("a NetworkManager profile is incomplete") from exc


def _validate_ssid(value: str) -> str:
    encoded = value.encode("utf-8")
    if not 1 <= len(encoded) <= 32 or any(character in value for character in "\x00\r\n"):
        raise NetworkEvidenceError("a NetworkManager profile has an invalid SSID")
    return value


def _validate_secret(value: str, *, maximum: int) -> bytes:
    encoded = value.encode("utf-8")
    if not 1 <= len(encoded) <= maximum or any(character in value for character in "\x00\r\n"):
        raise NetworkEvidenceError("a NetworkManager profile has invalid credential fields")
    return encoded


def _parse_management_profile(path: Path, parser: configparser.RawConfigParser) -> ParsedProfile:
    if path.name != USB_PROFILE_NAME:
        raise NetworkEvidenceError("a persistent NetworkManager profile name is not allowed")
    _require_sections(
        parser,
        {
            "connection": frozenset(
                {
                    "id",
                    "uuid",
                    "type",
                    "interface-name",
                    "autoconnect",
                    "autoconnect-priority",
                }
            ),
            "ethernet": frozenset({"cloned-mac-address"}),
            "ipv4": frozenset(
                {
                    "method",
                    "address1",
                    "never-default",
                    "ignore-auto-dns",
                    "route-metric",
                    "may-fail",
                }
            ),
            "ipv6": frozenset({"method"}),
            "proxy": frozenset(),
        },
    )
    expected = {
        ("connection", "id"): USB_CONNECTION_ID,
        ("connection", "uuid"): USB_CONNECTION_UUID,
        ("connection", "type"): "ethernet",
        ("connection", "interface-name"): "usb0",
        ("connection", "autoconnect"): "true",
        ("connection", "autoconnect-priority"): "100",
        ("ethernet", "cloned-mac-address"): "02:54:54:77:00:02",
        ("ipv4", "method"): "manual",
        ("ipv4", "address1"): "10.77.0.2/30",
        ("ipv4", "never-default"): "true",
        ("ipv4", "ignore-auto-dns"): "true",
        ("ipv4", "route-metric"): "32767",
        ("ipv4", "may-fail"): "false",
        ("ipv6", "method"): "disabled",
    }
    if any(_value(parser, section, key) != value for (section, key), value in expected.items()):
        raise NetworkEvidenceError("the USB NetworkManager profile does not match policy")
    return ParsedProfile(
        "persistent",
        "management",
        USB_CONNECTION_ID,
        _canonical_uuid(USB_CONNECTION_UUID),
        None,
        None,
        None,
        (),
    )


def _client_sections(security: str) -> dict[str, frozenset[str]]:
    sections = {
        "connection": frozenset(
            {"id", "uuid", "type", "interface-name", "autoconnect", "autoconnect-retries"}
        ),
        "wifi": frozenset({"mode", "ssid", "hidden"}),
        "ipv4": frozenset({"method"}),
        "ipv6": frozenset({"method"}),
    }
    if security == "wpa-personal":
        sections["wifi-security"] = frozenset({"key-mgmt", "psk"})
    elif security == "wpa-enterprise":
        sections["wifi-security"] = frozenset({"key-mgmt"})
        sections["802-1x"] = frozenset({"eap", "identity", "password", "phase2-auth"})
    return sections


def _parse_client_profile(
    path: Path, parser: configparser.RawConfigParser, source: ProfileSource, profile_uuid: str
) -> ParsedProfile:
    if parser.has_section("802-1x"):
        security = "wpa-enterprise"
    elif parser.has_section("wifi-security"):
        security = "wpa-personal"
    else:
        security = "open"
    _require_sections(parser, _client_sections(security))
    autoconnect = "true" if source == "persistent" else "false"
    expected = {
        ("connection", "uuid"): profile_uuid,
        ("connection", "type"): "wifi",
        ("connection", "interface-name"): "wlan0",
        ("connection", "autoconnect"): autoconnect,
        ("connection", "autoconnect-retries"): "1",
        ("wifi", "mode"): "infrastructure",
        ("ipv4", "method"): "auto",
        ("ipv6", "method"): "auto",
    }
    if any(_value(parser, section, key) != value for (section, key), value in expected.items()):
        raise NetworkEvidenceError("a Wi-Fi NetworkManager profile does not match policy")
    hidden_text = _value(parser, "wifi", "hidden")
    if hidden_text not in {"true", "false"}:
        raise NetworkEvidenceError("a Wi-Fi NetworkManager profile has an invalid hidden flag")
    hidden = hidden_text == "true"
    ssid = _validate_ssid(_value(parser, "wifi", "ssid"))
    profile_id = _value(parser, "connection", "id")
    fingerprint = hashlib.sha256(
        f"{ssid}\0{security}\0{int(hidden)}".encode()
    ).hexdigest()[:12]
    if not PROFILE_ID_PATTERN.fullmatch(profile_id) or profile_id != f"tidetracker-{fingerprint}":
        raise NetworkEvidenceError("a Wi-Fi NetworkManager profile has an invalid connection ID")

    secrets: tuple[bytes, ...] = ()
    if security == "wpa-personal":
        if _value(parser, "wifi-security", "key-mgmt") != "wpa-psk":
            raise NetworkEvidenceError("a Wi-Fi NetworkManager profile has invalid security")
        raw_psk = _value(parser, "wifi-security", "psk")
        valid_hex = len(raw_psk) == 64 and re.fullmatch(r"[0-9A-Fa-f]{64}", raw_psk)
        if not valid_hex and not 8 <= len(raw_psk) <= 63:
            raise NetworkEvidenceError("a Wi-Fi NetworkManager profile has invalid credentials")
        secrets = (_validate_secret(raw_psk, maximum=256),)
    elif security == "wpa-enterprise":
        if (
            _value(parser, "wifi-security", "key-mgmt") != "wpa-eap"
            or _value(parser, "802-1x", "eap") != "peap;"
            or _value(parser, "802-1x", "phase2-auth") != "mschapv2"
        ):
            raise NetworkEvidenceError("a Wi-Fi NetworkManager profile has invalid security")
        secrets = (
            _validate_secret(_value(parser, "802-1x", "identity"), maximum=1012),
            _validate_secret(_value(parser, "802-1x", "password"), maximum=2048),
        )
    return ParsedProfile(
        source,
        "client",
        profile_id,
        profile_uuid,
        ssid,
        security,
        hidden,
        secrets,
    )


def _parse_hotspot_profile(
    path: Path, parser: configparser.RawConfigParser, profile_uuid: str
) -> ParsedProfile:
    del path
    _require_sections(
        parser,
        {
            "connection": frozenset({"id", "uuid", "type", "interface-name", "autoconnect"}),
            "wifi": frozenset({"mode", "band", "ssid"}),
            "ipv4": frozenset({"method", "address1"}),
            "ipv6": frozenset({"method"}),
        },
    )
    connection_id = f"tidetracker-hotspot-{profile_uuid[:8]}"
    expected = {
        ("connection", "id"): connection_id,
        ("connection", "uuid"): profile_uuid,
        ("connection", "type"): "wifi",
        ("connection", "interface-name"): "wlan0",
        ("connection", "autoconnect"): "false",
        ("wifi", "mode"): "ap",
        ("wifi", "band"): "bg",
        ("wifi", "ssid"): SETUP_SSID,
        ("ipv4", "method"): "shared",
        ("ipv4", "address1"): "192.168.42.1/24",
        ("ipv6", "method"): "disabled",
    }
    if any(_value(parser, section, key) != value for (section, key), value in expected.items()):
        raise NetworkEvidenceError("the setup hotspot profile does not match policy")
    return ParsedProfile(
        "runtime",
        "hotspot",
        connection_id,
        profile_uuid,
        SETUP_SSID,
        "open",
        False,
        (),
    )


def _parse_profile(
    path: Path, source: ProfileSource, *, required_mode: int
) -> ParsedProfile:
    dynamic_match = PROFILE_NAME_PATTERN.fullmatch(path.name)
    if not (source == "persistent" and path.name == USB_PROFILE_NAME) and dynamic_match is None:
        raise NetworkEvidenceError("a NetworkManager profile filename is not allowed")
    payload = _snapshot_regular(
        path,
        maximum_bytes=MAX_PROFILE_BYTES,
        require_nonempty=True,
        require_root_owner=True,
        required_mode=required_mode,
    )
    parser = _decode_profile(payload)
    if source == "persistent" and path.name == USB_PROFILE_NAME:
        return _parse_management_profile(path, parser)
    assert dynamic_match is not None
    profile_uuid = _canonical_uuid(dynamic_match.group(1))
    connection_id = parser.get("connection", "id", fallback="")
    if source == "runtime" and connection_id.startswith("tidetracker-hotspot-"):
        return _parse_hotspot_profile(path, parser, profile_uuid)
    return _parse_client_profile(path, parser, source, profile_uuid)


class NetworkEvidenceService:
    """Read-only proof collector with injectable filesystem roots and nmcli runner."""

    def __init__(
        self,
        *,
        profile_directory: Path = PROFILE_DIRECTORY,
        runtime_profile_directory: Path = RUNTIME_PROFILE_DIRECTORY,
        artifact_roots: Sequence[Path] = ARTIFACT_ROOTS,
        excluded_source_roots: Sequence[Path] = EXCLUDED_SOURCE_ROOTS,
        nmcli: str = NMCLI,
        runner: NmcliRunner = _subprocess_nmcli,
        required_profile_mode: int = 0o600,
        profile_wait_seconds: float = PROFILE_EVIDENCE_WAIT_SECONDS,
        profile_poll_seconds: float = PROFILE_EVIDENCE_POLL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        max_artifact_directories: int = MAX_ARTIFACT_DIRECTORIES,
        max_artifact_files: int = MAX_ARTIFACT_FILES,
        max_artifact_file_bytes: int = MAX_ARTIFACT_FILE_BYTES,
        max_artifact_total_bytes: int = MAX_ARTIFACT_TOTAL_BYTES,
    ) -> None:
        self.profile_directory = Path(profile_directory)
        self.runtime_profile_directory = Path(runtime_profile_directory)
        self.artifact_roots = tuple(Path(path) for path in artifact_roots)
        self.excluded_source_roots = frozenset(Path(path) for path in excluded_source_roots)
        self.nmcli = nmcli
        self.runner = runner
        self.required_profile_mode = required_profile_mode
        self.profile_wait_seconds = profile_wait_seconds
        self.profile_poll_seconds = profile_poll_seconds
        self.monotonic = monotonic
        self.sleeper = sleeper
        self._query_deadline: float | None = None
        self.max_artifact_directories = max_artifact_directories
        self.max_artifact_files = max_artifact_files
        self.max_artifact_file_bytes = max_artifact_file_bytes
        self.max_artifact_total_bytes = max_artifact_total_bytes
        if (
            not self.artifact_roots
            or self.nmcli != NMCLI
            or len(set(self.artifact_roots)) != len(self.artifact_roots)
            or any(not path.is_absolute() for path in self.artifact_roots)
            or any(not path.is_absolute() for path in self.excluded_source_roots)
            or any(
                not any(excluded.is_relative_to(root) and excluded != root for root in self.artifact_roots)
                for excluded in self.excluded_source_roots
            )
            or min(
                self.max_artifact_directories,
                self.max_artifact_files,
                self.max_artifact_file_bytes,
                self.max_artifact_total_bytes,
            )
            <= 0
            or (
                self.required_profile_mode != 0o600
                and not (os.name == "nt" and self.required_profile_mode == 0o666)
            )
            or not 0 <= self.profile_wait_seconds <= 120
            or not 0.05 <= self.profile_poll_seconds <= 5
        ):
            raise ValueError("network evidence filesystem policy is invalid")

    def _profiles(self, source: ProfileSource) -> tuple[ParsedProfile, ...]:
        directory = (
            self.profile_directory if source == "persistent" else self.runtime_profile_directory
        )
        _require_safe_directory(directory)
        try:
            entries = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise NetworkEvidenceError("NetworkManager profiles cannot be enumerated") from exc
        if len(entries) > MAX_PROFILE_FILES:
            raise NetworkEvidenceError("NetworkManager profile count exceeds policy")
        profiles: list[ParsedProfile] = []
        for path in entries:
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise NetworkEvidenceError("a NetworkManager profile changed during inspection") from exc
            if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                raise NetworkEvidenceError("a NetworkManager profile path is unsafe")
            profiles.append(
                _parse_profile(path, source, required_mode=self.required_profile_mode)
            )
        if source == "persistent":
            management = [profile for profile in profiles if profile.kind == "management"]
            if len(management) != 1:
                raise NetworkEvidenceError("the USB management profile inventory is invalid")
        return tuple(profiles)

    def _nmcli(self, arguments: Sequence[str]) -> str:
        process_timeout = NMCLI_PROCESS_TIMEOUT_SECONDS
        wait_seconds = NMCLI_WAIT_SECONDS
        if self._query_deadline is not None:
            remaining = self._query_deadline - self.monotonic()
            if remaining <= 0:
                raise NetworkEvidencePending(
                    "the NetworkManager transaction did not converge before its deadline"
                )
            process_timeout = min(process_timeout, remaining)
            wait_seconds = min(wait_seconds, max(0, int(remaining)))
        command = [
            self.nmcli,
            "--wait",
            str(wait_seconds),
            *arguments,
        ]
        try:
            result = self.runner(command, timeout=process_timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            if (
                self._query_deadline is not None
                and self.monotonic() >= self._query_deadline
            ):
                raise NetworkEvidencePending(
                    "the NetworkManager transaction did not converge before its deadline"
                ) from exc
            raise NetworkEvidenceError("NetworkManager evidence query could not run") from exc
        if result.returncode != 0:
            raise NetworkEvidenceError("NetworkManager evidence query failed")
        if not isinstance(result.stdout, str):
            raise NetworkEvidenceError("NetworkManager evidence query returned invalid data")
        if len(result.stdout.encode("utf-8")) > MAX_NMCLI_OUTPUT_BYTES:
            raise NetworkEvidenceError("NetworkManager evidence query exceeded its output bound")
        return result.stdout

    def _nm_connections(self, *, active: bool) -> tuple[NmConnection, ...]:
        arguments = [
            "--terse",
            "--escape",
            "no",
            "--separator",
            "\t",
            "--fields",
            "NAME,UUID,TYPE,DEVICE",
            "connection",
            "show",
        ]
        if active:
            arguments.append("--active")
        output = self._nmcli(arguments)
        connections: list[NmConnection] = []
        seen: set[str] = set()
        for line in output.splitlines():
            fields = line.split("\t")
            if len(fields) != 4:
                raise NetworkEvidenceError("NetworkManager connection inventory is invalid")
            name, raw_uuid, connection_type, device = fields
            profile_uuid = _canonical_uuid(raw_uuid)
            if (
                not name
                or len(name.encode("utf-8")) > 256
                or any(character in name for character in "\x00\r\n\t")
                or SAFE_NM_TYPE_PATTERN.fullmatch(connection_type) is None
                or (device and SAFE_DEVICE_PATTERN.fullmatch(device) is None)
                or profile_uuid in seen
            ):
                raise NetworkEvidenceError("NetworkManager connection inventory is invalid")
            seen.add(profile_uuid)
            connections.append(NmConnection(name, profile_uuid, connection_type, device))
        return tuple(connections)

    @staticmethod
    def _is_wifi(connection: NmConnection) -> bool:
        return connection.connection_type in {"wifi", "802-11-wireless"}

    def _nm_ssid(self, profile_uuid: str) -> str:
        output = self._nmcli(
            [
                "--escape",
                "no",
                "--get-values",
                "802-11-wireless.ssid",
                "connection",
                "show",
                "uuid",
                profile_uuid,
            ]
        )
        if "\x00" in output or "\r" in output or output.count("\n") > 1:
            raise NetworkEvidenceError("NetworkManager returned an invalid SSID")
        return _validate_ssid(output.removesuffix("\n"))

    def _validated_network_state(
        self,
        profiles: Sequence[ParsedProfile],
        *,
        pending_loaded_uuids: frozenset[str] = frozenset(),
    ) -> tuple[tuple[NmConnection, ...], tuple[NmConnection, ...]]:
        loaded = self._nm_connections(active=False)
        active = self._nm_connections(active=True)
        loaded_by_uuid = {connection.profile_uuid: connection for connection in loaded}
        known = {profile.profile_uuid: profile for profile in profiles}
        if len(known) != len(profiles):
            raise NetworkEvidenceError("NetworkManager profile UUIDs are not unique")
        for profile in profiles:
            connection = loaded_by_uuid.get(profile.profile_uuid)
            expected_type = "ethernet" if profile.kind == "management" else "wifi"
            if connection is None and profile.profile_uuid in pending_loaded_uuids:
                raise NetworkEvidencePending(
                    "the requested NetworkManager profile is still being loaded"
                )
            if (
                connection is None
                or connection.name != profile.connection_id
                or connection.connection_type not in {expected_type, "802-11-wireless"}
                or (profile.kind == "management" and connection.connection_type != "ethernet")
            ):
                raise NetworkEvidenceError("a NetworkManager profile is not loaded as expected")
            if profile.ssid is not None and self._nm_ssid(profile.profile_uuid) != profile.ssid:
                raise NetworkEvidenceError("a loaded NetworkManager profile has the wrong SSID")
        for connection in loaded:
            if connection.name.startswith("tidetracker-"):
                known_profile = known.get(connection.profile_uuid)
                if known_profile is None or known_profile.connection_id != connection.name:
                    raise NetworkEvidenceError("NetworkManager retains a stale TideTracker profile")
        for connection in active:
            if connection.profile_uuid not in loaded_by_uuid:
                raise NetworkEvidenceError("NetworkManager active state is inconsistent")
            loaded_connection = loaded_by_uuid[connection.profile_uuid]
            if (
                connection.name != loaded_connection.name
                or connection.connection_type != loaded_connection.connection_type
            ):
                raise NetworkEvidenceError("NetworkManager active state is inconsistent")
        return loaded, active

    @staticmethod
    def _require_only_active_wifi(
        active: Sequence[NmConnection], expected_profile: ParsedProfile
    ) -> None:
        active_wifi = [
            connection
            for connection in active
            if NetworkEvidenceService._is_wifi(connection) and connection.device == "wlan0"
        ]
        if (
            len(active_wifi) != 1
            or active_wifi[0].profile_uuid != expected_profile.profile_uuid
            or active_wifi[0].name != expected_profile.connection_id
        ):
            raise NetworkEvidenceError("NetworkManager did not restore the expected Wi-Fi state")

    @staticmethod
    def _ssid_sha256(profile: ParsedProfile) -> str | None:
        if profile.ssid is None:
            return None
        return hashlib.sha256(profile.ssid.encode("utf-8")).hexdigest()

    def profile_evidence(
        self,
        case_id: str,
        ssid_sha256: str,
        security: str,
        hidden: bool,
        operation: str,
    ) -> dict[str, object]:
        specification = CASE_SPEC.get(case_id)
        if (
            specification is None
            or specification != (security, hidden, operation)
            or SHA256_PATTERN.fullmatch(ssid_sha256) is None
        ):
            raise NetworkEvidenceError("network profile evidence request is not allowed")
        deadline = self.monotonic() + self.profile_wait_seconds
        if self._query_deadline is not None:
            raise NetworkEvidenceError("a network evidence query is already active")
        self._query_deadline = deadline if self.profile_wait_seconds > 0 else None
        try:
            while True:
                try:
                    return self._profile_evidence_once(
                        case_id, ssid_sha256, security, hidden, operation
                    )
                except NetworkEvidencePending:
                    remaining = deadline - self.monotonic()
                    if remaining <= 0:
                        raise
                    self.sleeper(min(self.profile_poll_seconds, remaining))
        finally:
            self._query_deadline = None

    def _profile_evidence_once(
        self,
        case_id: str,
        ssid_sha256: str,
        security: str,
        hidden: bool,
        operation: str,
    ) -> dict[str, object]:
        persistent = self._profiles("persistent")
        runtime = self._profiles("runtime")
        matches = [
            profile
            for profile in persistent
            if profile.kind == "client" and self._ssid_sha256(profile) == ssid_sha256
        ]
        if not matches:
            raise NetworkEvidencePending("the network profile transaction is still in progress")
        if len(matches) != 1:
            raise NetworkEvidenceError("the requested NetworkManager profile is not unique")
        profile = matches[0]
        if profile.security != security or profile.hidden is not hidden:
            raise NetworkEvidenceError("the requested NetworkManager profile does not match")
        if operation == "test":
            if runtime:
                raise NetworkEvidencePending("the tested network profile is still being finalized")
            _, active = self._validated_network_state(
                persistent,
                pending_loaded_uuids=frozenset({profile.profile_uuid}),
            )
            try:
                self._require_only_active_wifi(active, profile)
            except NetworkEvidenceError as exc:
                raise NetworkEvidencePending(
                    "the tested network profile is not active yet"
                ) from exc
        else:
            if any(item.kind == "client" for item in runtime):
                raise NetworkEvidencePending("the force-save profile is still being persisted")
            if len(runtime) != 1 or runtime[0].kind != "hotspot":
                raise NetworkEvidenceError("force-save did not retain the setup hotspot")
            _, active = self._validated_network_state(
                (*persistent, *runtime),
                pending_loaded_uuids=frozenset({profile.profile_uuid}),
            )
            if any(connection.profile_uuid == profile.profile_uuid for connection in active):
                raise NetworkEvidenceError("force-save activated its untested client profile")
            self._require_setup_hotspot(runtime, active)
        return {
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

    def _require_setup_hotspot(
        self, runtime: Sequence[ParsedProfile], active: Sequence[NmConnection]
    ) -> ParsedProfile:
        if len(runtime) != 1 or runtime[0].kind != "hotspot":
            raise NetworkEvidenceError("the setup hotspot runtime profile was not restored")
        hotspot = runtime[0]
        self._require_only_active_wifi(active, hotspot)
        return hotspot

    def rollback_evidence(self, ssid_sha256: str) -> dict[str, object]:
        if SHA256_PATTERN.fullmatch(ssid_sha256) is None:
            raise NetworkEvidenceError("rollback evidence request is not allowed")
        persistent = self._profiles("persistent")
        runtime = self._profiles("runtime")
        profiles = (*persistent, *runtime)
        if any(self._ssid_sha256(profile) == ssid_sha256 for profile in profiles):
            raise NetworkEvidenceError("the failed NetworkManager profile remains on disk")
        loaded, active = self._validated_network_state(profiles)
        self._require_setup_hotspot(runtime, active)
        for connection in loaded:
            if self._is_wifi(connection):
                loaded_ssid = self._nm_ssid(connection.profile_uuid)
                if hashlib.sha256(loaded_ssid.encode("utf-8")).hexdigest() == ssid_sha256:
                    raise NetworkEvidenceError("the failed NetworkManager profile remains loaded")
        return {
            "schema_version": 1,
            "event": "network_rollback_evidence",
            "ssid_sha256": ssid_sha256,
            "profile_absent": True,
            "active_profile_restored": True,
        }

    def _scan_artifacts_portable(self, patterns: frozenset[bytes]) -> tuple[int, int]:
        """Path-based test-host fallback; field evidence always uses the fd walker."""

        files_scanned = 0
        total_bytes = 0
        directories_seen = 0
        for root in self.artifact_roots:
            _require_safe_directory(root)
            stack = [root]
            while stack:
                directory = stack.pop()
                directories_seen += 1
                if directories_seen > self.max_artifact_directories:
                    raise NetworkEvidenceError("artifact directory count exceeds policy")
                try:
                    entries = sorted(directory.iterdir(), key=lambda path: path.name)
                except OSError as exc:
                    raise NetworkEvidenceError("an artifact directory cannot be inspected") from exc
                for path in entries:
                    try:
                        metadata = path.lstat()
                    except OSError as exc:
                        raise NetworkEvidenceError("an artifact changed during inspection") from exc
                    if path.is_symlink():
                        raise NetworkEvidenceError("an artifact symbolic link is not allowed")
                    if stat.S_ISDIR(metadata.st_mode):
                        if path not in self.excluded_source_roots:
                            stack.append(path)
                        continue
                    if not stat.S_ISREG(metadata.st_mode):
                        raise NetworkEvidenceError("an artifact path is not a regular file")
                    files_scanned += 1
                    if files_scanned > self.max_artifact_files:
                        raise NetworkEvidenceError("artifact file count exceeds policy")
                    payload = _snapshot_regular(
                        path,
                        maximum_bytes=self.max_artifact_file_bytes,
                        require_nonempty=False,
                    )
                    total_bytes += len(payload)
                    if total_bytes > self.max_artifact_total_bytes:
                        raise NetworkEvidenceError("artifact byte count exceeds policy")
                    if any(pattern in payload for pattern in patterns):
                        raise NetworkEvidenceError("credential material appeared in target artifacts")
        if files_scanned < 1:
            raise NetworkEvidenceError("the target artifact scan inspected no files")
        return files_scanned, total_bytes

    def _scan_artifacts_fd(self, patterns: frozenset[bytes]) -> tuple[int, int]:
        files_scanned = 0
        total_bytes = 0
        directories_seen = 0
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )

        def scan_directory(directory_fd: int, logical_path: Path) -> None:
            nonlocal directories_seen, files_scanned, total_bytes
            before = os.fstat(directory_fd)
            if not stat.S_ISDIR(before.st_mode):
                raise NetworkEvidenceError("an artifact directory is unsafe")
            directories_seen += 1
            if directories_seen > self.max_artifact_directories:
                raise NetworkEvidenceError("artifact directory count exceeds policy")
            try:
                names = sorted(os.listdir(directory_fd))
            except OSError as exc:
                raise NetworkEvidenceError("an artifact directory cannot be inspected") from exc
            for name in names:
                child_path = logical_path / name
                try:
                    initial = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except OSError as exc:
                    raise NetworkEvidenceError("an artifact changed during inspection") from exc
                if stat.S_ISLNK(initial.st_mode):
                    raise NetworkEvidenceError("an artifact symbolic link is not allowed")
                if stat.S_ISDIR(initial.st_mode):
                    if child_path in self.excluded_source_roots:
                        continue
                    try:
                        child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
                    except OSError as exc:
                        raise NetworkEvidenceError("an artifact changed during inspection") from exc
                    try:
                        opened = os.fstat(child_fd)
                        if not stat.S_ISDIR(opened.st_mode) or not _same_snapshot(
                            initial, opened
                        ):
                            raise NetworkEvidenceError("an artifact changed during inspection")
                        scan_directory(child_fd, child_path)
                        try:
                            current = os.stat(
                                name, dir_fd=directory_fd, follow_symlinks=False
                            )
                        except OSError as exc:
                            raise NetworkEvidenceError(
                                "an artifact changed during inspection"
                            ) from exc
                        if not _same_snapshot(opened, current):
                            raise NetworkEvidenceError("an artifact changed during inspection")
                    finally:
                        os.close(child_fd)
                    continue
                if not stat.S_ISREG(initial.st_mode):
                    raise NetworkEvidenceError("an artifact path is not a regular file")
                files_scanned += 1
                if files_scanned > self.max_artifact_files:
                    raise NetworkEvidenceError("artifact file count exceeds policy")
                payload = _snapshot_regular_at(
                    directory_fd,
                    name,
                    maximum_bytes=self.max_artifact_file_bytes,
                )
                total_bytes += len(payload)
                if total_bytes > self.max_artifact_total_bytes:
                    raise NetworkEvidenceError("artifact byte count exceeds policy")
                if any(pattern in payload for pattern in patterns):
                    raise NetworkEvidenceError("credential material appeared in target artifacts")
            after = os.fstat(directory_fd)
            if not _same_snapshot(before, after):
                raise NetworkEvidenceError("an artifact directory changed during inspection")

        for root in self.artifact_roots:
            _require_safe_directory(root)
            try:
                initial = root.stat(follow_symlinks=False)
                root_fd = os.open(root, directory_flags)
            except OSError as exc:
                raise NetworkEvidenceError("an evidence directory is unavailable") from exc
            try:
                opened = os.fstat(root_fd)
                if not stat.S_ISDIR(opened.st_mode) or not _same_snapshot(initial, opened):
                    raise NetworkEvidenceError("an artifact directory changed during inspection")
                scan_directory(root_fd, root)
                try:
                    current = root.stat(follow_symlinks=False)
                except OSError as exc:
                    raise NetworkEvidenceError(
                        "an artifact directory changed during inspection"
                    ) from exc
                if not _same_snapshot(opened, current):
                    raise NetworkEvidenceError("an artifact directory changed during inspection")
            finally:
                os.close(root_fd)
        if files_scanned < 1:
            raise NetworkEvidenceError("the target artifact scan inspected no files")
        return files_scanned, total_bytes

    def _scan_artifacts(self, patterns: frozenset[bytes]) -> tuple[int, int]:
        # Raspberry Pi OS is Linux. Windows lacks the dir_fd primitives used to
        # make rename/replacement races fail closed, so it gets a test-only fallback.
        if os.name == "nt":
            return self._scan_artifacts_portable(patterns)
        return self._scan_artifacts_fd(patterns)

    def secret_artifact_scan(self, supplied_secrets: Sequence[str]) -> dict[str, object]:
        persistent = self._profiles("persistent")
        runtime = self._profiles("runtime")
        profiles = (*persistent, *runtime)
        _, active = self._validated_network_state(profiles)
        self._require_setup_hotspot(runtime, active)
        external = _validate_supplied_secrets(supplied_secrets)
        secrets = frozenset(
            (*external, *(secret for profile in profiles for secret in profile.secrets if secret))
        )
        files_scanned, _ = self._scan_artifacts(_secret_search_patterns(secrets))
        return {
            "schema_version": 1,
            "event": "network_secret_artifact_scan",
            "passed": True,
            "files_scanned": files_scanned,
            "forbidden_matches": 0,
        }


def _require_root_zero_role(
    role_path: Path,
    effective_uid: Callable[[], int],
    *,
    enforce_permissions: bool,
) -> None:
    if effective_uid() != 0:
        raise NetworkEvidenceError("network evidence requires root")
    payload = _snapshot_regular(
        role_path,
        maximum_bytes=16,
        require_nonempty=True,
        require_root_owner=True,
    )
    try:
        metadata = role_path.stat(follow_symlinks=False)
    except OSError as exc:  # pragma: no cover - snapshot already verifies this
        raise NetworkEvidenceError("target role evidence is unavailable") from exc
    if (
        (enforce_permissions and stat.S_IMODE(metadata.st_mode) & 0o022)
        or payload not in {b"zero", b"zero\n"}
    ):
        raise NetworkEvidenceError("network evidence is restricted to the Pi Zero role")


def _effective_uid() -> int:
    getter = getattr(os, "geteuid", None)
    if getter is None:  # pragma: no cover - field target is Linux
        raise NetworkEvidenceError("network evidence requires Linux root identity")
    return int(getter())


def _parse_hidden(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise NetworkEvidenceError("network profile evidence request is not allowed")


def _reject_duplicate_json_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, child in pairs:
        if key in value:
            raise NetworkEvidenceError("network secret input contains duplicate fields")
        value[key] = child
    return value


def _validate_supplied_secrets(values: Sequence[str]) -> tuple[bytes, ...]:
    if (
        not 1 <= len(values) <= MAX_SUPPLIED_SECRETS
        or len(values) != len(set(values))
        or list(values) != sorted(values)
    ):
        raise NetworkEvidenceError("network secret input is invalid")
    encoded: list[bytes] = []
    for value in values:
        if not isinstance(value, str):
            raise NetworkEvidenceError("network secret input is invalid")
        payload = value.encode("utf-8")
        if (
            not 1 <= len(payload) <= MAX_SUPPLIED_SECRET_BYTES
            or any(character in value for character in "\x00\r\n")
        ):
            raise NetworkEvidenceError("network secret input is invalid")
        encoded.append(payload)
    return tuple(encoded)


def _secret_search_patterns(secrets: frozenset[bytes]) -> frozenset[bytes]:
    """Return raw and JSON-serialized forms without formatting them in errors."""

    patterns: set[bytes] = set(secrets)
    for secret in secrets:
        try:
            value = secret.decode("utf-8")
        except UnicodeDecodeError as exc:  # pragma: no cover - callers validate UTF-8
            raise NetworkEvidenceError("network secret input is invalid") from exc
        for ensure_ascii in (False, True):
            serialized = json.dumps(value, ensure_ascii=ensure_ascii).encode("utf-8")
            patterns.add(serialized)
            patterns.add(serialized[1:-1])
    patterns.discard(b"")
    return frozenset(patterns)


def _read_secret_input(payload: bytes) -> tuple[str, ...]:
    if not 0 < len(payload) <= MAX_SECRET_INPUT_BYTES:
        raise NetworkEvidenceError("network secret input is invalid")
    try:
        value = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_fields
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NetworkEvidenceError("network secret input is invalid") from exc
    if not isinstance(value, dict) or set(value) != {"schema_version", "secrets"}:
        raise NetworkEvidenceError("network secret input is invalid")
    raw_secrets = value.get("secrets")
    if value.get("schema_version") != 1 or not isinstance(raw_secrets, list):
        raise NetworkEvidenceError("network secret input is invalid")
    if not all(isinstance(item, str) for item in raw_secrets):
        raise NetworkEvidenceError("network secret input is invalid")
    secrets = tuple(raw_secrets)
    _validate_supplied_secrets(secrets)
    return secrets


def _stdin_payload() -> bytes:
    try:
        payload = sys.stdin.buffer.read(MAX_SECRET_INPUT_BYTES + 1)
    except OSError as exc:
        raise NetworkEvidenceError("network secret input is unavailable") from exc
    return payload


def main(
    argv: Sequence[str] | None = None,
    *,
    service: NetworkEvidenceService | None = None,
    role_path: Path = ROLE_PATH,
    effective_uid: Callable[[], int] = _effective_uid,
    enforce_role_permissions: bool = True,
    secret_input: bytes | None = None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        _require_root_zero_role(
            role_path, effective_uid, enforce_permissions=enforce_role_permissions
        )
        if not arguments:
            raise NetworkEvidenceError("a network evidence command is required")
        command, values = arguments[0], arguments[1:]
        collector = service or NetworkEvidenceService()
        if command == "network-profile-evidence":
            if len(values) != 5:
                raise NetworkEvidenceError("network profile evidence arguments are invalid")
            case_id, ssid_sha256, security, hidden_text, operation = values
            result = collector.profile_evidence(
                case_id,
                ssid_sha256,
                security,
                _parse_hidden(hidden_text),
                operation,
            )
        elif command == "network-rollback-evidence":
            if len(values) != 1:
                raise NetworkEvidenceError("network rollback evidence arguments are invalid")
            result = collector.rollback_evidence(values[0])
        elif command == "network-secret-artifact-scan":
            if values:
                raise NetworkEvidenceError("network artifact scan arguments are invalid")
            result = collector.secret_artifact_scan(
                _read_secret_input(_stdin_payload() if secret_input is None else secret_input)
            )
        else:
            raise NetworkEvidenceError("network evidence command is not allowed")
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except NetworkEvidenceError as exc:
        print(
            json.dumps(
                {"error": "NetworkEvidenceError", "message": str(exc)},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2
    except Exception:
        print(
            '{"error":"NetworkEvidenceError","message":"network evidence collection failed"}',
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
