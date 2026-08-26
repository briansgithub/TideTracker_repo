#!/usr/bin/env python3
"""Root-only ephemeral Pi 3 Wi-Fi test plane for the TideTracker portal matrix."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol

MAX_PRIVATE_INPUT_BYTES = 8 * 1024
MAX_COMMAND_OUTPUT_BYTES = 64 * 1024
RUNTIME_DIR = pathlib.Path("/run/tidetracker-portal-matrix")
CONTROLLER_CONFIG = pathlib.Path("/var/lib/tidetracker/controller/controller-lab.env")
SERVICE_NAME = "tidetracker-controller-test-plane.service"
TEST_ADDRESS = "192.168.50.1/24"
TEST_SUBNET = "192.168.50.0/24"
TEST_DHCP_RANGE = "192.168.50.20,192.168.50.80,255.255.255.0,5m"

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
PHASE_STATES = {
    "validate": "validated",
    "apply": "portal-client-active",
    "test-plane": "test-plane-active",
    "reset": "normal-ap-restored",
}
TOOLS = {
    "nmcli": "/usr/bin/nmcli",
    "systemctl": "/usr/bin/systemctl",
    "ip": "/usr/sbin/ip",
    "iw": "/usr/sbin/iw",
    "nft": "/usr/sbin/nft",
    "hostapd": "/usr/sbin/hostapd",
    "dnsmasq": "/usr/sbin/dnsmasq",
    "openssl": "/usr/bin/openssl",
}
RUNTIME_FILES = frozenset(
    {
        "state.json",
        "state.json.tmp",
        "hostapd.conf",
        "dnsmasq.conf",
        "eap-users",
        "ca-cert.pem",
        "server-cert.pem",
        "server-key.pem",
        "openssl.cnf",
        "ready.json",
        "ip-forward.previous",
        "dnsmasq.pid",
    }
)
NORMAL_AP_PROFILE = {
    "connection.uuid": "eea12d5b-22cc-4fac-9002-545477000078",
    "connection.type": "802-11-wireless",
    "connection.interface-name": "wlan0",
    "connection.autoconnect": "yes",
    "connection.autoconnect-priority": "50",
    "802-11-wireless.mode": "ap",
    "802-11-wireless.band": "bg",
    "802-11-wireless.channel": "6",
    "802-11-wireless.ssid": "TideTracker-Lab",
    "802-11-wireless-security.key-mgmt": "wpa-psk",
    "ipv4.method": "shared",
    "ipv4.addresses": "10.78.0.1/24",
    "ipv4.never-default": "yes",
    "ipv4.ignore-auto-dns": "yes",
    "ipv6.method": "disabled",
}
PORTAL_CLIENT_PROFILE = {
    "connection.uuid": "eea12d5b-22cc-4fac-9002-545477000079",
    "connection.type": "802-11-wireless",
    "connection.interface-name": "wlan0",
    "connection.autoconnect": "no",
    "802-11-wireless.mode": "infrastructure",
    "802-11-wireless.ssid": "TideTracker-Setup",
    "802-11-wireless-security.key-mgmt": "",
    "ipv4.method": "auto",
    "ipv4.never-default": "yes",
    "ipv4.ignore-auto-dns": "yes",
    "ipv6.method": "disabled",
}


class PortalMatrixError(RuntimeError):
    """Fail-closed error whose text never contains private profile material."""


class Runner(Protocol):
    def __call__(
        self,
        arguments: Sequence[str],
        *,
        input: str | None = None,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]: ...


def subprocess_runner(
    arguments: Sequence[str], *, input: str | None = None, timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(arguments),
        input=input,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"},
    )


def _reject_duplicates(items: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in items:
        if key in value:
            raise PortalMatrixError("private portal profile repeats a JSON field")
        value[key] = child
    return value


def parse_private_profile(payload: bytes, expected_case_id: str) -> dict[str, Any]:
    if not 1 <= len(payload) <= MAX_PRIVATE_INPUT_BYTES:
        raise PortalMatrixError("private portal profile exceeds its input bound")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortalMatrixError("private portal profile is invalid JSON") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"case_id", "operation", "profile"}
        or value.get("case_id") != expected_case_id
        or expected_case_id not in CASE_SPEC
        or not isinstance(value.get("profile"), dict)
    ):
        raise PortalMatrixError("private portal profile does not match the requested case")
    security, hidden, operation = CASE_SPEC[expected_case_id]
    profile = value["profile"]
    required = {"ssid", "security", "hidden"}
    if security == "wpa-personal":
        required.add("password")
    elif security == "wpa-enterprise":
        required.update({"identity", "password"})
    if (
        value.get("operation") != operation
        or set(profile) != required
        or profile.get("security") != security
        or profile.get("hidden") is not hidden
    ):
        raise PortalMatrixError("private portal profile does not provide exact fixed coverage")
    ssid = profile.get("ssid")
    if (
        not isinstance(ssid, str)
        or not ssid
        or len(ssid.encode("utf-8")) > 32
        or any(character in ssid for character in ("\x00", "\r", "\n"))
    ):
        raise PortalMatrixError("private portal profile has an invalid SSID")
    password = profile.get("password")
    identity = profile.get("identity")
    if security == "wpa-personal":
        if (
            not isinstance(password, str)
            or any(character in password for character in ("\x00", "\r", "\n"))
            or not (
                8 <= len(password) <= 63
                or (len(password) == 64 and re.fullmatch(r"[0-9A-Fa-f]{64}", password))
            )
        ):
            raise PortalMatrixError("private WPA Personal profile is invalid")
    elif security == "wpa-enterprise" and (
        not isinstance(identity, str)
        or re.fullmatch(r"[A-Za-z0-9_.@+-]{1,128}", identity) is None
        or not isinstance(password, str)
        or not 8 <= len(password) <= 128
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in password)
        or any(character in password for character in ('"', "\\"))
    ):
        raise PortalMatrixError("private WPA Enterprise profile is invalid")
    return value


def render_hostapd(profile: dict[str, Any]) -> tuple[str, str | None]:
    ssid = str(profile["ssid"])
    security = str(profile["security"])
    hidden = bool(profile["hidden"])
    lines = [
        "interface=wlan0",
        "driver=nl80211",
        f"ssid2={ssid.encode('utf-8').hex()}",
        "hw_mode=g",
        "channel=6",
        "country_code=US",
        "ieee80211d=1",
        "wmm_enabled=1",
        "auth_algs=1",
        f"ignore_broadcast_ssid={1 if hidden else 0}",
        "logger_syslog=0",
        "logger_stdout=0",
    ]
    eap_users: str | None = None
    if security == "open":
        lines.append("wpa=0")
    elif security == "wpa-personal":
        password = str(profile["password"])
        if len(password) == 64 and re.fullmatch(r"[0-9A-Fa-f]{64}", password):
            psk = password.lower()
        else:
            psk = hashlib.pbkdf2_hmac(
                "sha1", password.encode("utf-8"), ssid.encode("utf-8"), 4096, 32
            ).hex()
        lines.extend(
            [
                "wpa=2",
                "wpa_key_mgmt=WPA-PSK",
                "rsn_pairwise=CCMP",
                f"wpa_psk={psk}",
            ]
        )
    else:
        identity = str(profile["identity"])
        password = str(profile["password"])
        lines.extend(
            [
                "wpa=2",
                "wpa_key_mgmt=WPA-EAP",
                "rsn_pairwise=CCMP",
                "ieee8021x=1",
                "eap_server=1",
                "eapol_version=2",
                f"eap_user_file={RUNTIME_DIR / 'eap-users'}",
                f"ca_cert={RUNTIME_DIR / 'ca-cert.pem'}",
                f"server_cert={RUNTIME_DIR / 'server-cert.pem'}",
                f"private_key={RUNTIME_DIR / 'server-key.pem'}",
            ]
        )
        eap_users = f"* PEAP\n\"{identity}\" MSCHAPV2 \"{password}\" [2]\n"
    return "\n".join(lines) + "\n", eap_users


def render_dnsmasq() -> str:
    return "\n".join(
        [
            "interface=wlan0",
            "bind-interfaces",
            "except-interface=lo",
            f"dhcp-range={TEST_DHCP_RANGE}",
            "dhcp-option=3,192.168.50.1",
            "dhcp-option=6,192.168.50.1",
            "resolv-file=/run/systemd/resolve/resolv.conf",
            "no-hosts",
            "domain-needed",
            "bogus-priv",
            "log-facility=/dev/null",
        ]
    ) + "\n"


def public_result(case_id: str, phase: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "operation": "portal-matrix",
        "case_id": case_id,
        "phase": phase,
        "state": PHASE_STATES[phase],
    }


@dataclass
class PortalMatrixController:
    runtime_dir: pathlib.Path = RUNTIME_DIR
    controller_config: pathlib.Path = CONTROLLER_CONFIG
    tools: dict[str, str] | None = None
    runner: Runner = subprocess_runner
    require_root_files: bool = True

    def __post_init__(self) -> None:
        self.tools = dict(TOOLS if self.tools is None else self.tools)

    def _run(
        self,
        arguments: Sequence[str],
        *,
        timeout: float,
        check: bool = True,
        input: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = self.runner(arguments, input=input, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PortalMatrixError("controller network command failed or timed out") from exc
        if (
            len(result.stdout.encode("utf-8")) > MAX_COMMAND_OUTPUT_BYTES
            or len(result.stderr.encode("utf-8")) > MAX_COMMAND_OUTPUT_BYTES
        ):
            raise PortalMatrixError("controller network command exceeded its output bound")
        if check and result.returncode != 0:
            raise PortalMatrixError("controller network command failed")
        return result

    def _tool(self, name: str) -> str:
        assert self.tools is not None
        value = self.tools.get(name)
        if not isinstance(value, str) or not pathlib.Path(value).is_absolute():
            raise PortalMatrixError("controller test-plane dependency map is invalid")
        return value

    def _validate_dependencies(self) -> None:
        assert self.tools is not None
        if set(self.tools) != set(TOOLS):
            raise PortalMatrixError("controller test-plane dependency map is incomplete")
        for name in TOOLS:
            path = pathlib.Path(self._tool(name))
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise PortalMatrixError("controller test-plane dependency is unavailable") from exc
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or not os.access(path, os.X_OK)
            ):
                raise PortalMatrixError("controller test-plane dependency is unsafe")

    def _read_controller_config(self) -> dict[str, str]:
        path = self.controller_config
        try:
            metadata = path.lstat()
            payload = path.read_bytes()
            current = path.lstat()
        except OSError as exc:
            raise PortalMatrixError("controller lab configuration is unavailable") from exc
        stable = (metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or len(payload) != metadata.st_size
            or not 1 <= len(payload) <= 4096
            or stable != (current.st_size, current.st_mtime_ns, current.st_ctime_ns)
            or (self.require_root_files and (metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600))
        ):
            raise PortalMatrixError("controller lab configuration is unsafe")
        values: dict[str, str] = {}
        try:
            lines = payload.decode("ascii").splitlines()
        except UnicodeDecodeError as exc:
            raise PortalMatrixError("controller lab configuration is invalid") from exc
        for line in lines:
            if not line or line.startswith("#") or "=" not in line:
                raise PortalMatrixError("controller lab configuration is invalid")
            key, value = line.split("=", 1)
            if key in values or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) is None:
                raise PortalMatrixError("controller lab configuration is invalid")
            values[key] = value
        if set(values) != {
            "TIDETRACKER_NORMAL_AP_CONNECTION",
            "TIDETRACKER_PORTAL_CLIENT_CONNECTION",
            "TIDETRACKER_ETHERNET_INTERFACE",
        }:
            raise PortalMatrixError("controller lab configuration fields are incomplete")
        return values

    def _prove_profiles_and_ethernet(self, config: dict[str, str]) -> None:
        nmcli = self._tool("nmcli")
        profiles = (
            (
                config["TIDETRACKER_NORMAL_AP_CONNECTION"],
                NORMAL_AP_PROFILE,
            ),
            (
                config["TIDETRACKER_PORTAL_CLIENT_CONNECTION"],
                PORTAL_CLIENT_PROFILE,
            ),
        )
        for connection, expected in profiles:
            exact_profile = {
                "connection.id": connection,
                **expected,
            }
            result = self._run(
                [
                    nmcli,
                    "--terse",
                    "--get-values",
                    ",".join(exact_profile),
                    "connection",
                    "show",
                    connection,
                ],
                timeout=10,
            )
            if result.stdout.splitlines() != list(exact_profile.values()):
                raise PortalMatrixError(
                    "controller NetworkManager profile does not match its fixed definition"
                )
        route = self._run(
            [
                self._tool("ip"),
                "-4",
                "route",
                "show",
                "default",
                "dev",
                config["TIDETRACKER_ETHERNET_INTERFACE"],
            ],
            timeout=5,
        )
        if not route.stdout.strip():
            raise PortalMatrixError("controller Ethernet default route is unavailable")

    def _active(self, connection: str) -> bool:
        result = self._run(
            [self._tool("nmcli"), "-t", "-f", "NAME", "connection", "show", "--active"],
            timeout=10,
        )
        return connection in result.stdout.splitlines()

    def _service_active(self) -> bool:
        result = self._run(
            [self._tool("systemctl"), "is-active", "--quiet", SERVICE_NAME],
            timeout=10,
            check=False,
        )
        return result.returncode == 0

    def _prove_portal_client(self, connection: str) -> None:
        if not self._active(connection):
            raise PortalMatrixError("controller portal-client profile did not activate")
        link = self._run([self._tool("iw"), "dev", "wlan0", "link"], timeout=5)
        lines = {line.strip() for line in link.stdout.splitlines()}
        if (
            not any(line.startswith("Connected to ") for line in lines)
            or "SSID: TideTracker-Setup" not in lines
        ):
            raise PortalMatrixError("controller wlan0 did not join the Zero setup AP")

    def _prove_ap_interface(self, ssid: str) -> None:
        info = self._run([self._tool("iw"), "dev", "wlan0", "info"], timeout=5)
        lines = {line.strip() for line in info.stdout.splitlines()}
        if (
            "type AP" not in lines
            or f"ssid {ssid}" not in lines
            or not any(line.startswith("channel 6 (2437 MHz)") for line in lines)
        ):
            raise PortalMatrixError("controller wlan0 did not become the requested 2.4 GHz AP")

    def _prove_clean_normal_state(self, config: dict[str, str]) -> None:
        if (
            not self._active(config["TIDETRACKER_NORMAL_AP_CONNECTION"])
            or self._active(config["TIDETRACKER_PORTAL_CLIENT_CONNECTION"])
        ):
            raise PortalMatrixError("controller normal lab AP state is not exclusive")
        self._prove_ap_interface("TideTracker-Lab")
        nft = self._run(
            [self._tool("nft"), "list", "table", "ip", "tidetracker_portal_matrix"],
            timeout=5,
            check=False,
        )
        address = self._run(
            [self._tool("ip"), "address", "show", "dev", "wlan0"], timeout=5
        )
        managed = self._run(
            [
                self._tool("nmcli"),
                "--terse",
                "--get-values",
                "GENERAL.NM-MANAGED",
                "device",
                "show",
                "wlan0",
            ],
            timeout=5,
        )
        if (
            nft.returncode == 0
            or TEST_ADDRESS in address.stdout
            or managed.stdout.strip() != "yes"
        ):
            raise PortalMatrixError("controller test-plane network state is not clean")

    def validate(self, case: dict[str, Any]) -> dict[str, Any]:
        self._validate_dependencies()
        config = self._read_controller_config()
        self._prove_profiles_and_ethernet(config)
        if self.runtime_dir.exists() or self.runtime_dir.is_symlink() or self._service_active():
            raise PortalMatrixError("controller test plane is not clean before validation")
        self._prove_clean_normal_state(config)
        render_hostapd(case["profile"])
        render_dnsmasq()
        return public_result(str(case["case_id"]), "validate")

    def _write_private(self, name: str, payload: str) -> None:
        path = self.runtime_dir / name
        if name not in RUNTIME_FILES or path.exists() or path.is_symlink():
            raise PortalMatrixError("controller runtime file target is unsafe")
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            data = payload.encode("utf-8")
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise PortalMatrixError("controller runtime write made no progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _fsync_runtime_directory(self) -> None:
        if os.name != "posix":
            return
        descriptor = os.open(
            self.runtime_dir,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _write_state(self, case_id: str, phase: str) -> None:
        state = json.dumps(
            {"schema_version": 1, "case_id": case_id, "phase": phase},
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n"
        path = self.runtime_dir / "state.json"
        temporary = self.runtime_dir / "state.json.tmp"
        if path.exists() or path.is_symlink():
            metadata = path.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or (
                    self.require_root_files
                    and (metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600)
                )
            ):
                raise PortalMatrixError("controller test-plane state is unsafe")
        if temporary.exists() or temporary.is_symlink():
            raise PortalMatrixError("controller test-plane state update is incomplete")
        self._write_private("state.json.tmp", state)
        os.replace(temporary, path)
        self._fsync_runtime_directory()

    def _read_state(self) -> dict[str, Any]:
        path = self.runtime_dir / "state.json"
        try:
            metadata = path.lstat()
            payload = path.read_bytes()
        except OSError as exc:
            raise PortalMatrixError("controller test-plane state is unavailable") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or (
                self.require_root_files
                and (metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600)
            )
            or len(payload) != metadata.st_size
            or len(payload) > 1024
        ):
            raise PortalMatrixError("controller test-plane state is unsafe")
        try:
            value = json.loads(payload, object_pairs_hook=_reject_duplicates)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PortalMatrixError("controller test-plane state is invalid") from exc
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "case_id", "phase"}
            or value.get("schema_version") != 1
            or value.get("case_id") not in CASE_SPEC
            or value.get("phase") not in {"apply", "test-plane"}
        ):
            raise PortalMatrixError("controller test-plane state is invalid")
        return value

    def _verify_staged_files(self, case: dict[str, Any]) -> None:
        hostapd, eap_users = render_hostapd(case["profile"])
        expected = {"hostapd.conf": hostapd, "dnsmasq.conf": render_dnsmasq()}
        if eap_users is not None:
            expected["eap-users"] = eap_users
        for name, payload in expected.items():
            path = self.runtime_dir / name
            try:
                metadata = path.lstat()
                actual = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise PortalMatrixError("controller staged profile is unavailable") from exc
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or (
                    self.require_root_files
                    and (metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600)
                )
                or actual != payload
            ):
                raise PortalMatrixError("controller staged profile identity changed")

    def _verify_runtime_permissions(self) -> None:
        try:
            directory = self.runtime_dir.lstat()
            entries = tuple(self.runtime_dir.iterdir())
        except OSError as exc:
            raise PortalMatrixError("controller runtime permissions cannot be inspected") from exc
        if (
            self.runtime_dir.is_symlink()
            or not stat.S_ISDIR(directory.st_mode)
            or (self.require_root_files and (directory.st_uid != 0 or stat.S_IMODE(directory.st_mode) != 0o700))
        ):
            raise PortalMatrixError("controller runtime directory is unsafe")
        for path in entries:
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise PortalMatrixError(
                    "controller runtime file permissions cannot be inspected"
                ) from exc
            if (
                path.name not in RUNTIME_FILES
                or stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or (
                    self.require_root_files
                    and (metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600)
                )
            ):
                raise PortalMatrixError("controller runtime file permissions are unsafe")

    def _generate_enterprise_certificate(self) -> None:
        openssl_config = "\n".join(
            [
                "[req]",
                "distinguished_name=dn",
                "x509_extensions=extensions",
                "prompt=no",
                "[dn]",
                "CN=tidetracker-lab.invalid",
                "[extensions]",
                "basicConstraints=critical,CA:TRUE",
                "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign",
                "extendedKeyUsage=serverAuth",
                "subjectAltName=DNS:tidetracker-lab.invalid",
            ]
        ) + "\n"
        self._write_private("openssl.cnf", openssl_config)
        self._run(
            [
                self._tool("openssl"),
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "2",
                "-config",
                str(self.runtime_dir / "openssl.cnf"),
                "-keyout",
                str(self.runtime_dir / "server-key.pem"),
                "-out",
                str(self.runtime_dir / "server-cert.pem"),
            ],
            timeout=30,
        )
        for name in ("server-key.pem", "server-cert.pem"):
            path = self.runtime_dir / name
            path.chmod(0o600)
        certificate = (self.runtime_dir / "server-cert.pem").read_bytes()
        self._write_private("ca-cert.pem", certificate.decode("ascii"))

    def apply(self, case: dict[str, Any]) -> dict[str, Any]:
        self._validate_dependencies()
        config = self._read_controller_config()
        self._prove_profiles_and_ethernet(config)
        if self.runtime_dir.exists() or self.runtime_dir.is_symlink() or self._service_active():
            raise PortalMatrixError("controller test plane is not clean before apply")
        self._prove_clean_normal_state(config)
        os.mkdir(self.runtime_dir, 0o700)
        try:
            if self.require_root_files:
                metadata = self.runtime_dir.lstat()
                if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o700:
                    raise PortalMatrixError("controller runtime directory is unsafe")
            hostapd, eap_users = render_hostapd(case["profile"])
            self._write_private("hostapd.conf", hostapd)
            self._write_private("dnsmasq.conf", render_dnsmasq())
            if eap_users is not None:
                self._write_private("eap-users", eap_users)
                self._generate_enterprise_certificate()
            self._write_state(str(case["case_id"]), "apply")
            self._verify_runtime_permissions()
            nmcli = self._tool("nmcli")
            self._run(
                [nmcli, "connection", "down", config["TIDETRACKER_NORMAL_AP_CONNECTION"]],
                timeout=30,
                check=False,
            )
            self._run(
                [nmcli, "connection", "up", config["TIDETRACKER_PORTAL_CLIENT_CONNECTION"]],
                timeout=60,
            )
            self._prove_portal_client(config["TIDETRACKER_PORTAL_CLIENT_CONNECTION"])
        except Exception:
            with suppress(Exception):
                self._cleanup_runtime()
            with suppress(Exception):
                self._run(
                    [
                        self._tool("nmcli"),
                        "connection",
                        "up",
                        config["TIDETRACKER_NORMAL_AP_CONNECTION"],
                    ],
                    timeout=60,
                )
            raise
        return public_result(str(case["case_id"]), "apply")

    def test_plane(self, case: dict[str, Any]) -> dict[str, Any]:
        config = self._read_controller_config()
        state = self._read_state()
        if state != {"schema_version": 1, "case_id": case["case_id"], "phase": "apply"}:
            raise PortalMatrixError("controller test-plane phase order is invalid")
        self._verify_staged_files(case)
        self._run(
            [
                self._tool("nmcli"),
                "connection",
                "down",
                config["TIDETRACKER_PORTAL_CLIENT_CONNECTION"],
            ],
            timeout=30,
            check=False,
        )
        self._run(
            [self._tool("systemctl"), "start", SERVICE_NAME], timeout=30
        )
        deadline = time.monotonic() + 20
        ready = self.runtime_dir / "ready.json"
        while time.monotonic() < deadline:
            if self._service_active() and ready.is_file() and not ready.is_symlink():
                break
            time.sleep(0.1)
        else:
            raise PortalMatrixError("controller test-plane service did not become ready")
        try:
            ready_value = json.loads(ready.read_bytes(), object_pairs_hook=_reject_duplicates)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PortalMatrixError("controller test-plane readiness record is invalid") from exc
        if ready_value != {
            "schema_version": 1,
            "case_id": case["case_id"],
            "ready": True,
        }:
            raise PortalMatrixError("controller test-plane readiness record does not match")
        self._verify_runtime_permissions()
        address = self._run(
            [self._tool("ip"), "address", "show", "dev", "wlan0"], timeout=5
        )
        self._prove_ap_interface(str(case["profile"]["ssid"]))
        if "inet 192.168.50.1/24" not in address.stdout:
            raise PortalMatrixError("controller wlan0 did not become the requested test AP")
        self._prove_profiles_and_ethernet(config)
        self._write_state(str(case["case_id"]), "test-plane")
        return public_result(str(case["case_id"]), "test-plane")

    def _cleanup_runtime(self) -> None:
        if not self.runtime_dir.exists() and not self.runtime_dir.is_symlink():
            return
        self._verify_runtime_permissions()
        try:
            entries = tuple(self.runtime_dir.iterdir())
        except OSError as exc:
            raise PortalMatrixError("controller runtime cleanup cannot inspect state") from exc
        if len(entries) > len(RUNTIME_FILES):
            raise PortalMatrixError("controller runtime cleanup found unexpected entries")
        for path in entries:
            if path.name not in RUNTIME_FILES or path.is_symlink() or not path.is_file():
                raise PortalMatrixError("controller runtime cleanup found an unsafe entry")
        errors = 0
        ordered = sorted(
            entries,
            key=lambda item: (item.name in {"state.json", "state.json.tmp"}, item.name),
        )
        for path in ordered:
            try:
                path.unlink()
            except OSError:
                errors += 1
        try:
            self._fsync_runtime_directory()
        except OSError:
            errors += 1
        try:
            self.runtime_dir.rmdir()
        except OSError:
            errors += 1
        if errors or self.runtime_dir.exists() or self.runtime_dir.is_symlink():
            raise PortalMatrixError("controller runtime cleanup is incomplete")

    def _previous_forward_value(self) -> str | None:
        marker = self.runtime_dir / "ip-forward.previous"
        if not marker.exists() and not marker.is_symlink():
            return None
        try:
            metadata = marker.lstat()
            value = marker.read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise PortalMatrixError("controller forwarding recovery marker is invalid") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or (
                self.require_root_files
                and (metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600)
            )
            or value not in {"0", "1"}
        ):
            raise PortalMatrixError("controller forwarding recovery marker is invalid")
        return value

    def recover_service_state(self) -> None:
        """Attempt every network repair and report one aggregate, secret-free failure."""
        self._validate_dependencies()
        self._read_controller_config()
        errors = 0
        previous_forward: str | None = None
        if self.runtime_dir.exists() or self.runtime_dir.is_symlink():
            try:
                self._verify_runtime_permissions()
                previous_forward = self._previous_forward_value()
            except (OSError, PortalMatrixError):
                errors += 1

        def attempt(arguments: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[str] | None:
            nonlocal errors
            try:
                return self._run(arguments, timeout=timeout, check=False)
            except PortalMatrixError:
                errors += 1
                return None

        attempt(
            [self._tool("nft"), "delete", "table", "ip", "tidetracker_portal_matrix"],
            timeout=10,
        )
        attempt([self._tool("ip"), "address", "flush", "dev", "wlan0"], timeout=10)
        if previous_forward is not None:
            try:
                pathlib.Path("/proc/sys/net/ipv4/ip_forward").write_text(
                    previous_forward + "\n", encoding="ascii"
                )
            except OSError:
                errors += 1
        attempt(
            [self._tool("nmcli"), "device", "set", "wlan0", "managed", "yes"],
            timeout=15,
        )

        nft = attempt(
            [self._tool("nft"), "list", "table", "ip", "tidetracker_portal_matrix"],
            timeout=5,
        )
        if nft is not None and nft.returncode == 0:
            errors += 1
        address = attempt(
            [self._tool("ip"), "address", "show", "dev", "wlan0"], timeout=5
        )
        if address is not None and TEST_ADDRESS in address.stdout:
            errors += 1
        managed = attempt(
            [
                self._tool("nmcli"),
                "--terse",
                "--get-values",
                "GENERAL.NM-MANAGED",
                "device",
                "show",
                "wlan0",
            ],
            timeout=5,
        )
        if managed is not None and managed.stdout.strip() != "yes":
            errors += 1
        if previous_forward is not None:
            try:
                restored = pathlib.Path("/proc/sys/net/ipv4/ip_forward").read_text(
                    encoding="ascii"
                ).strip()
            except OSError:
                errors += 1
            else:
                if restored != previous_forward:
                    errors += 1
        if errors:
            raise PortalMatrixError("controller test-plane recovery did not restore every invariant")
        if self.runtime_dir.exists():
            cleanup_errors = 0
            for name in ("ready.json", "dnsmasq.pid", "ip-forward.previous"):
                path = self.runtime_dir / name
                if path.exists() or path.is_symlink():
                    try:
                        metadata = path.lstat()
                        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(
                            metadata.st_mode
                        ):
                            cleanup_errors += 1
                        else:
                            path.unlink()
                    except OSError:
                        cleanup_errors += 1
            try:
                self._fsync_runtime_directory()
            except OSError:
                cleanup_errors += 1
            if cleanup_errors:
                raise PortalMatrixError("controller recovery marker cleanup is incomplete")

    def reset(self, case: dict[str, Any]) -> dict[str, Any]:
        config = self._read_controller_config()
        errors = 0
        try:
            self._run(
                [self._tool("systemctl"), "stop", SERVICE_NAME], timeout=100, check=False
            )
        except PortalMatrixError:
            errors += 1
        try:
            if self._service_active():
                errors += 1
        except PortalMatrixError:
            errors += 1
        try:
            self.recover_service_state()
        except PortalMatrixError:
            errors += 1
        state_mismatch = False
        if self.runtime_dir.exists() or self.runtime_dir.is_symlink():
            try:
                self._verify_runtime_permissions()
                state_path = self.runtime_dir / "state.json"
                if state_path.exists() and not state_path.is_symlink():
                    try:
                        state = self._read_state()
                    except PortalMatrixError:
                        state = None
                    if state is not None and state.get("case_id") != case["case_id"]:
                        state_mismatch = True
            except PortalMatrixError:
                errors += 1
        nmcli = self._tool("nmcli")
        reset_commands = (
            ([nmcli, "device", "set", "wlan0", "managed", "yes"], 15.0, True),
            ([nmcli, "connection", "reload"], 15.0, True),
            (
                [
                    nmcli,
                    "connection",
                    "down",
                    config["TIDETRACKER_PORTAL_CLIENT_CONNECTION"],
                ],
                30.0,
                False,
            ),
        )
        for arguments, timeout, check in reset_commands:
            try:
                self._run(arguments, timeout=timeout, check=check)
            except PortalMatrixError:
                errors += 1
        try:
            self._cleanup_runtime()
        except PortalMatrixError:
            errors += 1
        try:
            self._run(
                [nmcli, "connection", "up", config["TIDETRACKER_NORMAL_AP_CONNECTION"]],
                timeout=60,
            )
        except PortalMatrixError:
            errors += 1
        try:
            if not self._active(config["TIDETRACKER_NORMAL_AP_CONNECTION"]):
                errors += 1
        except PortalMatrixError:
            errors += 1
        try:
            self._prove_ap_interface("TideTracker-Lab")
        except PortalMatrixError:
            errors += 1
        try:
            self._prove_profiles_and_ethernet(config)
        except PortalMatrixError:
            errors += 1
        if self.runtime_dir.exists() or self.runtime_dir.is_symlink():
            errors += 1
        if state_mismatch:
            errors += 1
        if errors:
            raise PortalMatrixError("controller reset did not restore every required invariant")
        return public_result(str(case["case_id"]), "reset")

    def execute(self, case: dict[str, Any], phase: str) -> dict[str, Any]:
        if phase == "validate":
            return self.validate(case)
        if phase == "apply":
            return self.apply(case)
        if phase == "test-plane":
            return self.test_plane(case)
        if phase == "reset":
            return self.reset(case)
        raise PortalMatrixError("invalid controller portal-matrix phase")


def _write_ready(runtime_dir: pathlib.Path, case_id: str) -> None:
    path = runtime_dir / "ready.json"
    payload = json.dumps(
        {"schema_version": 1, "case_id": case_id, "ready": True},
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        remaining = memoryview(payload.encode("ascii"))
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise PortalMatrixError("controller readiness write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def serve() -> int:
    controller = PortalMatrixController()
    config = controller._read_controller_config()
    state = controller._read_state()
    if state.get("phase") != "apply":
        raise PortalMatrixError("controller service phase is invalid")
    runtime = controller.runtime_dir
    previous_forward = pathlib.Path("/proc/sys/net/ipv4/ip_forward").read_text(
        encoding="ascii"
    ).strip()
    if previous_forward not in {"0", "1"}:
        raise PortalMatrixError("controller IPv4 forwarding state is invalid")
    controller._write_private("ip-forward.previous", previous_forward + "\n")
    nft_rules = "\n".join(
        [
            "table ip tidetracker_portal_matrix {",
            " chain postrouting {",
            "  type nat hook postrouting priority srcnat; policy accept;",
            f'  ip saddr {TEST_SUBNET} oifname "{config["TIDETRACKER_ETHERNET_INTERFACE"]}" masquerade',
            " }",
            "}",
        ]
    ) + "\n"
    children: list[subprocess.Popen[bytes]] = []
    stop = False

    def request_stop(signum: int, frame: Any) -> None:
        del signum, frame
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        controller._run(
            [controller._tool("nmcli"), "device", "set", "wlan0", "managed", "no"],
            timeout=15,
        )
        controller._run(
            [controller._tool("ip"), "address", "flush", "dev", "wlan0"], timeout=10
        )
        controller._run(
            [controller._tool("ip"), "address", "add", TEST_ADDRESS, "dev", "wlan0"],
            timeout=10,
        )
        controller._run(
            [controller._tool("ip"), "link", "set", "wlan0", "up"], timeout=10
        )
        pathlib.Path("/proc/sys/net/ipv4/ip_forward").write_text("1\n", encoding="ascii")
        controller._run([controller._tool("nft"), "-f", "-"], input=nft_rules, timeout=10)
        children.append(
            subprocess.Popen(
                [controller._tool("hostapd"), str(runtime / "hostapd.conf")],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
            )
        )
        time.sleep(1)
        if children[0].poll() is not None:
            raise PortalMatrixError("controller hostapd service exited during startup")
        children.append(
            subprocess.Popen(
                [
                    controller._tool("dnsmasq"),
                    "--keep-in-foreground",
                    f"--conf-file={runtime / 'dnsmasq.conf'}",
                    f"--pid-file={runtime / 'dnsmasq.pid'}",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
            )
        )
        time.sleep(1)
        if children[1].poll() is not None:
            raise PortalMatrixError("controller dnsmasq service exited during startup")
        _write_ready(runtime, str(state["case_id"]))
        while not stop:
            if any(child.poll() is not None for child in children):
                raise PortalMatrixError("controller test-plane child service exited")
            time.sleep(0.2)
    finally:
        cleanup_errors = 0
        for child in reversed(children):
            try:
                child.terminate()
            except (OSError, ProcessLookupError):
                cleanup_errors += 1
        deadline = time.monotonic() + 5
        for child in reversed(children):
            remaining = max(0.0, deadline - time.monotonic())
            try:
                child.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                pass
            except OSError:
                cleanup_errors += 1
            if child.poll() is None:
                try:
                    child.kill()
                    child.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    cleanup_errors += 1
        try:
            controller.recover_service_state()
        except PortalMatrixError:
            cleanup_errors += 1
        if cleanup_errors:
            raise PortalMatrixError("controller service cleanup did not complete every repair")
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if os.geteuid() != 0:
        raise PortalMatrixError("controller portal matrix requires root")
    os.umask(0o077)
    if arguments == ["serve"]:
        return serve()
    if arguments == ["recover"]:
        PortalMatrixController().recover_service_state()
        return 0
    if len(arguments) != 3 or arguments[0] != "portal-matrix":
        raise PortalMatrixError("invalid controller portal-matrix invocation")
    _, case_id, phase = arguments
    if case_id not in CASE_SPEC or phase not in PHASE_STATES:
        raise PortalMatrixError("invalid controller portal-matrix case or phase")
    payload = sys.stdin.buffer.read(MAX_PRIVATE_INPUT_BYTES + 1)
    case = parse_private_profile(payload, case_id)
    result = PortalMatrixController().execute(case, phase)
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PortalMatrixError as error:
        print(f"portal-matrix error: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print("portal-matrix error: unexpected bounded helper failure", file=sys.stderr)
        raise SystemExit(1) from None
