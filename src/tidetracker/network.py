"""NetworkManager integration with transactional Wi-Fi profile updates.

The application never persists Wi-Fi credentials in its own configuration. A test
profile is loaded from a mode-0600, non-autoconnecting keyfile under ``/run`` and is
only promoted to the persistent TIDEDATA bind mount after it succeeds. In particular,
secrets are never included in command-line arguments, application JSON, or exception
messages.
"""

from __future__ import annotations

import configparser
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from .models import NetworkStatus as RuntimeNetworkStatus

_MAX_PROFILE_BYTES = 16 * 1024
_MAX_RETIRED_PROFILES = 32
_PROFILE_ID = re.compile(r"tidetracker-[0-9a-f]{12}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        # Windows does not support opening directories with the POSIX durability
        # contract. Field deployments are Linux and must fail closed below.
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise NetworkError("NetworkManager directory durability check failed") from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise NetworkError("NetworkManager directory durability check failed") from exc
    finally:
        os.close(descriptor)


def _durable_unlink(path: Path) -> bool:
    """Unlink one non-directory entry and durably prove its name is absent."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise NetworkError("NetworkManager profile cleanup could not inspect a path") from exc
    if stat.S_ISDIR(metadata.st_mode):
        raise NetworkError("NetworkManager profile cleanup encountered a directory")
    try:
        path.unlink()
        _fsync_directory(path.parent)
    except OSError as exc:
        raise NetworkError("NetworkManager profile cleanup was not durable") from exc
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise NetworkError("NetworkManager profile cleanup could not be proved") from exc
    raise NetworkError("NetworkManager profile cleanup did not remove its target")


def _stable_private_file_bytes(path: Path, *, maximum_bytes: int) -> bytes:
    """Read one owner-private regular file through a stable, no-follow descriptor."""

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            payload = os.read(descriptor, maximum_bytes + 1)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise NetworkError("NetworkManager private state cannot be read safely") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_size != len(payload)
        or len(payload) > maximum_bytes
        or (os.name != "nt" and stat.S_IMODE(before.st_mode) != 0o600)
        or (hasattr(os, "geteuid") and before.st_uid != os.geteuid())
    ):
        raise NetworkError("NetworkManager private state is malformed or unsafe")
    return payload


class NetworkError(RuntimeError):
    """Base class for safe, user-presentable networking failures."""


class NetworkCommandError(NetworkError):
    """Raised when NetworkManager rejects an operation.

    The command and its output are intentionally omitted: both are controlled by an
    external program and could echo a submitted credential.
    """

    def __init__(self, operation: str, returncode: int) -> None:
        super().__init__(f"NetworkManager {operation} failed (exit {returncode})")
        self.operation = operation
        self.returncode = returncode


class NetworkTestFailed(NetworkError):
    """Raised when a candidate profile activates but lacks connectivity."""


class _PromotionCommitted(NetworkError):
    """A durable save committed, but idempotent retirement needs recovery."""


class SecurityMode(StrEnum):
    OPEN = "open"
    WPA_PERSONAL = "wpa-personal"
    WPA_ENTERPRISE = "wpa-enterprise"

    @classmethod
    def parse(cls, value: object) -> SecurityMode:
        normalized = str(value or "").strip().lower().replace("_", "-")
        aliases = {
            "none": cls.OPEN,
            "open": cls.OPEN,
            "wpa": cls.WPA_PERSONAL,
            "wpa2": cls.WPA_PERSONAL,
            "password": cls.WPA_PERSONAL,
            "personal": cls.WPA_PERSONAL,
            "wpa-personal": cls.WPA_PERSONAL,
            "enterprise": cls.WPA_ENTERPRISE,
            "wpa-enterprise": cls.WPA_ENTERPRISE,
            "802.1x": cls.WPA_ENTERPRISE,
        }
        try:
            return aliases[normalized]
        except KeyError as exc:
            raise ValueError("unsupported Wi-Fi security mode") from exc


def _clean_text(value: object, *, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError(f"{field_name} contains unsupported control characters")
    if not value or len(value) > maximum:
        raise ValueError(f"{field_name} has an invalid length")
    return value


@dataclass(frozen=True, slots=True)
class NetworkProfile:
    """A credential-bearing value object whose representation is always redacted."""

    ssid: str
    security: SecurityMode
    hidden: bool = False
    identity: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        ssid = _clean_text(self.ssid, field_name="ssid", maximum=32)
        if len(ssid.encode("utf-8")) > 32:
            raise ValueError("ssid must not exceed 32 UTF-8 bytes")
        object.__setattr__(self, "ssid", ssid)

        if self.security is SecurityMode.OPEN:
            if self.identity is not None or self.password is not None:
                raise ValueError("open Wi-Fi must not include credentials")
            return

        if self.security is SecurityMode.WPA_PERSONAL:
            if self.identity is not None:
                raise ValueError("WPA Personal does not use an identity")
            password = _clean_text(self.password, field_name="password", maximum=64)
            valid_hex_key = len(password) == 64 and re.fullmatch(r"[0-9a-fA-F]{64}", password)
            if not valid_hex_key and not 8 <= len(password) <= 63:
                raise ValueError("WPA Personal password must contain 8 to 63 characters")
            object.__setattr__(self, "password", password)
            return

        identity = _clean_text(self.identity, field_name="identity", maximum=253)
        password = _clean_text(self.password, field_name="password", maximum=512)
        object.__setattr__(self, "identity", identity)
        object.__setattr__(self, "password", password)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> NetworkProfile:
        return cls(
            ssid=value.get("ssid"),  # type: ignore[arg-type]
            security=SecurityMode.parse(value.get("security")),
            hidden=bool(value.get("hidden", False)),
            identity=value.get("identity") if value.get("identity") is not None else None,  # type: ignore[arg-type]
            password=value.get("password") if value.get("password") is not None else None,  # type: ignore[arg-type]
        )

    def public_dict(self) -> dict[str, object]:
        return {"ssid": self.ssid, "security": self.security.value, "hidden": self.hidden}

    def __repr__(self) -> str:
        return (
            "NetworkProfile("
            f"ssid={self.ssid!r}, security={self.security.value!r}, hidden={self.hidden!r}, "
            "identity=<redacted>, password=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class AccessPoint:
    ssid: str
    security: SecurityMode
    signal: int | None = None

    def public_dict(self) -> dict[str, object]:
        return {"ssid": self.ssid, "security": self.security.value, "signal": self.signal}


@dataclass(frozen=True, slots=True)
class NetworkStatus:
    connected: bool
    state: str
    connection: str | None

    def public_dict(self) -> dict[str, object]:
        return {
            "connected": self.connected,
            "state": self.state,
            "connection": self.connection,
        }


@dataclass(frozen=True, slots=True)
class NetworkSnapshot:
    active_wifi_uuids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HotspotSession:
    profile_uuid: str
    ssid: str
    snapshot: NetworkSnapshot


@dataclass(frozen=True, slots=True)
class _ProfileRecord:
    path: Path
    profile_uuid: str
    connection_id: str
    connection_type: str
    interface: str
    autoconnect: bool
    wifi_mode: str | None
    ssid: str | None
    sha256: str


@dataclass(frozen=True, slots=True)
class _RetiredProfile:
    profile_uuid: str
    sha256: str


@dataclass(frozen=True, slots=True)
class _PromotionJournal:
    phase: Literal["preparing", "committed"]
    new_uuid: str
    new_sha256: str
    retire: tuple[_RetiredProfile, ...]


class Runner(Protocol):
    def __call__(
        self,
        args: Sequence[str],
        *,
        input: str | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


def _subprocess_runner(
    args: Sequence[str], *, input: str | None = None, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        capture_output=True,
        check=False,
        input=input,
        shell=False,
        text=True,
        timeout=timeout,
    )


def _split_terse(value: str) -> list[str]:
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for character in value.rstrip("\r\n"):
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(character)
    if escaped:
        current.append("\\")
    fields.append("".join(current))
    return fields


class NetworkManagerAdapter:
    """Narrow ``nmcli`` adapter used by the portal and cycle runtime."""

    def __init__(
        self,
        *,
        interface: str = "wlan0",
        binary: str = "nmcli",
        profile_directory: Path = Path("/etc/NetworkManager/system-connections"),
        runtime_directory: Path = Path("/run/tidetracker/network-profiles"),
        runner: Runner = _subprocess_runner,
        timeout: float = 20.0,
    ) -> None:
        self.interface = interface
        self.binary = binary
        self.profile_directory = Path(profile_directory)
        self.runtime_directory = Path(runtime_directory)
        self._runner = runner
        self.timeout = timeout
        self._profile_paths: dict[str, set[Path]] = {}
        self._candidate_profiles: dict[str, NetworkProfile] = {}
        self._cleanup_owned_profile_temps(self.runtime_directory, include_marker_temps=False)
        self._cleanup_stale_runtime_profiles()
        self._cleanup_owned_profile_temps(self.profile_directory, include_marker_temps=True)
        self._cleanup_interrupted_persistent_profiles()

    def _cleanup_owned_profile_temps(
        self,
        directory: Path,
        *,
        include_marker_temps: bool,
    ) -> None:
        """Durably remove private credential temps left before an atomic replace."""

        try:
            entries = tuple(directory.iterdir())
        except FileNotFoundError:
            return
        except OSError as exc:
            raise NetworkError("NetworkManager temporary profiles cannot be inspected") from exc
        profile_pattern = re.compile(
            r"^\.tidetracker-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12})\.nmconnection\.[A-Za-z0-9_-]+\.tmp$"
        )
        marker_pattern = re.compile(
            r"^\.\.tidetracker-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12})\.pending\.[A-Za-z0-9_-]+\.tmp$"
        )
        for path in entries:
            match = profile_pattern.fullmatch(path.name)
            if match is None and include_marker_temps:
                match = marker_pattern.fullmatch(path.name)
            if match is None:
                continue
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise NetworkError("NetworkManager temporary profile is unreadable") from exc
            if (
                not stat.S_ISREG(metadata.st_mode)
                or path.is_symlink()
                or (os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600)
                or (hasattr(os, "geteuid") and metadata.st_uid != os.geteuid())
            ):
                raise NetworkError("NetworkManager temporary profile is malformed or unsafe")
            profile_uuid = str(uuid.UUID(match.group(1)))
            self._delete_loaded_profile(
                profile_uuid,
                operation="interrupted temporary-profile deletion",
                missing_ok=True,
            )
            _durable_unlink(path)

    @staticmethod
    def _read_profile(path: Path) -> _ProfileRecord:
        try:
            payload = _stable_private_file_bytes(path, maximum_bytes=_MAX_PROFILE_BYTES)
            text = payload.decode("utf-8")
            parser = configparser.RawConfigParser(interpolation=None)
            parser.read_string(text)
            connection_id = parser.get("connection", "id", fallback="")
            if not connection_id.startswith("tidetracker-"):
                raise ValueError("profile is outside the reserved namespace")
            profile_uuid = str(uuid.UUID(parser.get("connection", "uuid", fallback="")))
            autoconnect = parser.getboolean("connection", "autoconnect", fallback=False)
            connection_type = parser.get("connection", "type", fallback="")
            interface = parser.get("connection", "interface-name", fallback="")
            wifi_mode = parser.get("wifi", "mode", fallback=None)
            ssid = parser.get("wifi", "ssid", fallback=None)
        except (UnicodeError, ValueError, configparser.Error, NetworkError) as exc:
            raise NetworkError("NetworkManager profile is malformed or unsafe") from exc
        return _ProfileRecord(
            path=path,
            profile_uuid=profile_uuid,
            connection_id=connection_id,
            connection_type=connection_type,
            interface=interface,
            autoconnect=autoconnect,
            wifi_mode=wifi_mode,
            ssid=ssid,
            sha256=hashlib.sha256(payload).hexdigest(),
        )

    def _forget_stale_profile(self, path: Path, profile_uuid: str) -> None:
        self._delete_loaded_profile(
            profile_uuid,
            operation="stale profile deletion",
            missing_ok=True,
        )
        _durable_unlink(path)

    def _delete_loaded_profile(
        self,
        profile_uuid: str,
        *,
        operation: str,
        missing_ok: bool,
    ) -> None:
        deletion = self._run(
            ["connection", "delete", "uuid", profile_uuid],
            operation=operation,
            check=False,
        )
        if deletion.returncode != 0:
            loaded = self._run(
                ["--terse", "--fields", "UUID", "connection", "show", "uuid", profile_uuid],
                operation=f"{operation} absence proof",
                check=False,
            )
            if not missing_ok or loaded.returncode == 0:
                raise NetworkError("NetworkManager retained a credential profile")

    def _cleanup_stale_runtime_profiles(self) -> None:
        """Remove non-persistent candidates left by an interrupted portal process."""

        try:
            entries = tuple(self.runtime_directory.glob("tidetracker-*.nmconnection"))
        except OSError as exc:
            raise NetworkError("NetworkManager runtime profiles cannot be inspected") from exc
        for path in entries:
            profile = self._read_profile(path)
            self._forget_stale_profile(path, profile.profile_uuid)
        if any(self.runtime_directory.glob("tidetracker-*.nmconnection")):
            raise NetworkError("NetworkManager runtime profile cleanup is incomplete")

    @staticmethod
    def _parse_promotion_journal(marker: Path) -> _PromotionJournal:
        try:
            payload = _stable_private_file_bytes(marker, maximum_bytes=8 * 1024)

            def reject_duplicates(items: list[tuple[str, object]]) -> dict[str, object]:
                value: dict[str, object] = {}
                for key, child in items:
                    if key in value:
                        raise ValueError("duplicate journal field")
                    value[key] = child
                return value

            raw = json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicates)
            if not isinstance(raw, dict) or set(raw) != {
                "schema_version",
                "phase",
                "new_uuid",
                "new_sha256",
                "retire",
            }:
                raise ValueError("invalid journal schema")
            raw_phase = raw["phase"]
            new_uuid = str(uuid.UUID(str(raw["new_uuid"])))
            new_sha256 = raw["new_sha256"]
            retire_raw = raw["retire"]
            if (
                raw["schema_version"] != 1
                or raw_phase not in {"preparing", "committed"}
                or marker.name != f".tidetracker-{new_uuid}.pending"
                or not isinstance(new_sha256, str)
                or _SHA256.fullmatch(new_sha256) is None
                or not isinstance(retire_raw, list)
                or len(retire_raw) > _MAX_RETIRED_PROFILES
            ):
                raise ValueError("invalid journal fields")
            retire: list[_RetiredProfile] = []
            for item in retire_raw:
                if not isinstance(item, dict) or set(item) != {"uuid", "sha256"}:
                    raise ValueError("invalid retired profile")
                retired_uuid = str(uuid.UUID(str(item["uuid"])))
                digest = item["sha256"]
                if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
                    raise ValueError("invalid retired profile hash")
                retire.append(_RetiredProfile(retired_uuid, digest))
            if (
                retire != sorted(retire, key=lambda item: item.profile_uuid)
                or len({item.profile_uuid for item in retire}) != len(retire)
                or any(item.profile_uuid == new_uuid for item in retire)
            ):
                raise ValueError("non-canonical retired profile list")
            phase: Literal["preparing", "committed"] = (
                "preparing" if raw_phase == "preparing" else "committed"
            )
        except (UnicodeError, ValueError, TypeError, NetworkError) as exc:
            raise NetworkError("NetworkManager promotion marker is malformed or unsafe") from exc
        return _PromotionJournal(
            phase=phase,
            new_uuid=new_uuid,
            new_sha256=new_sha256,
            retire=tuple(retire),
        )

    def _recover_promotion(self, marker: Path, journal: _PromotionJournal) -> None:
        new_path = self.profile_directory / f"tidetracker-{journal.new_uuid}.nmconnection"
        if journal.phase == "preparing":
            self._delete_loaded_profile(
                journal.new_uuid,
                operation="interrupted promotion rollback",
                missing_ok=True,
            )
            _durable_unlink(new_path)
            _durable_unlink(marker)
            return

        new_profile = self._read_profile(new_path)
        if (
            new_profile.profile_uuid != journal.new_uuid
            or new_profile.sha256 != journal.new_sha256
            or not new_profile.autoconnect
            or _PROFILE_ID.fullmatch(new_profile.connection_id) is None
            or new_profile.connection_type not in {"wifi", "802-11-wireless"}
            or new_profile.interface != self.interface
            or new_profile.wifi_mode != "infrastructure"
            or new_profile.ssid is None
        ):
            raise NetworkError("committed NetworkManager profile cannot be proved")

        # Validate every still-present retirement target before the first delete.
        retired_paths: list[tuple[_RetiredProfile, Path]] = []
        for retired in journal.retire:
            path = self.profile_directory / f"tidetracker-{retired.profile_uuid}.nmconnection"
            try:
                profile = self._read_profile(path)
            except NetworkError:
                try:
                    path.lstat()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise NetworkError("retired profile presence cannot be proved") from exc
                else:
                    raise
            else:
                if (
                    profile.profile_uuid != retired.profile_uuid
                    or profile.sha256 != retired.sha256
                    or _PROFILE_ID.fullmatch(profile.connection_id) is None
                    or profile.connection_type not in {"wifi", "802-11-wireless"}
                    or profile.interface != self.interface
                    or profile.wifi_mode != "infrastructure"
                    or profile.ssid != new_profile.ssid
                ):
                    raise NetworkError("retired NetworkManager profile differs from journal")
            retired_paths.append((retired, path))

        self._run(
            ["connection", "load", str(new_path)],
            operation="committed profile recovery load",
        )
        loaded = self._run(
            ["--terse", "--fields", "UUID", "connection", "show", "uuid", journal.new_uuid],
            operation="committed profile recovery proof",
            check=False,
        )
        if loaded.returncode != 0:
            raise NetworkError("committed NetworkManager profile is not loaded")
        for retired, path in retired_paths:
            self._delete_loaded_profile(
                retired.profile_uuid,
                operation="superseded profile retirement",
                missing_ok=True,
            )
            _durable_unlink(path)
        _durable_unlink(marker)

    def _cleanup_interrupted_persistent_profiles(self) -> None:
        """Roll back pre-commit saves or finish committed profile replacement."""

        try:
            markers = tuple(self.profile_directory.glob(".tidetracker-*.pending"))
        except OSError as exc:
            raise NetworkError("persistent NetworkManager profiles cannot be inspected") from exc
        if len(markers) > 1:
            raise NetworkError("multiple NetworkManager promotions require manual recovery")
        for marker in markers:
            self._recover_promotion(marker, self._parse_promotion_journal(marker))
        for path in self.profile_directory.glob("tidetracker-*.nmconnection"):
            profile = self._read_profile(path)
            if not profile.autoconnect:
                self._forget_stale_profile(path, profile.profile_uuid)
        for path in self.profile_directory.glob("tidetracker-*.nmconnection"):
            profile = self._read_profile(path)
            if not profile.autoconnect:
                raise NetworkError("persistent NetworkManager cleanup is incomplete")
        if any(self.profile_directory.glob(".tidetracker-*.pending")):
            raise NetworkError("persistent NetworkManager promotion cleanup is incomplete")

    def _run(
        self,
        arguments: Sequence[str],
        *,
        operation: str,
        check: bool = True,
        timeout_seconds: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command_timeout = self.timeout if timeout_seconds is None else min(self.timeout, timeout_seconds)
        if command_timeout <= 0:
            raise NetworkError(f"NetworkManager {operation} deadline is exhausted")
        try:
            result = self._runner(
                [self.binary, "--wait", str(max(1, round(command_timeout))), *arguments],
                input=None,
                timeout=command_timeout + 2,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise NetworkError(f"NetworkManager {operation} could not be executed") from exc
        if check and result.returncode != 0:
            raise NetworkCommandError(operation, result.returncode)
        return result

    def scan(self) -> tuple[AccessPoint, ...]:
        result = self._run(
            [
                "--terse",
                "--escape",
                "yes",
                "--fields",
                "SSID,SECURITY,SIGNAL",
                "device",
                "wifi",
                "list",
                "ifname",
                self.interface,
                "--rescan",
                "yes",
            ],
            operation="scan",
        )
        strongest: dict[tuple[str, SecurityMode], AccessPoint] = {}
        for line in result.stdout.splitlines():
            fields = _split_terse(line)
            if len(fields) != 3 or not fields[0]:
                continue
            security_text = fields[1].upper()
            if "802.1X" in security_text or "EAP" in security_text:
                security = SecurityMode.WPA_ENTERPRISE
            elif security_text in {"", "--", "NONE", "OPEN"}:
                security = SecurityMode.OPEN
            else:
                security = SecurityMode.WPA_PERSONAL
            try:
                signal = int(fields[2])
            except ValueError:
                signal = None
            candidate = AccessPoint(fields[0], security, signal)
            key = (candidate.ssid, candidate.security)
            old = strongest.get(key)
            if old is None or (candidate.signal or -1) > (old.signal or -1):
                strongest[key] = candidate
        return tuple(
            sorted(strongest.values(), key=lambda item: (-(item.signal or -1), item.ssid))
        )

    def status(self) -> NetworkStatus:
        result = self._run(
            [
                "--terse",
                "--escape",
                "yes",
                "--fields",
                "GENERAL.STATE,GENERAL.CONNECTION",
                "device",
                "show",
                self.interface,
            ],
            operation="status",
        )
        values: dict[str, str] = {}
        for line in result.stdout.splitlines():
            fields = _split_terse(line)
            if len(fields) >= 2:
                values[fields[0]] = ":".join(fields[1:])
        state = values.get("GENERAL.STATE", "unknown")
        connection = values.get("GENERAL.CONNECTION") or None
        return NetworkStatus(state.startswith("100") or "connected" in state.lower(), state, connection)

    def snapshot(self) -> NetworkSnapshot:
        result = self._run(
            [
                "--terse",
                "--escape",
                "yes",
                "--fields",
                "UUID,TYPE",
                "connection",
                "show",
                "--active",
            ],
            operation="snapshot",
        )
        active: list[str] = []
        for line in result.stdout.splitlines():
            fields = _split_terse(line)
            if len(fields) == 2 and fields[1] in {"wifi", "802-11-wireless"}:
                active.append(fields[0])
        return NetworkSnapshot(tuple(active))

    def transaction(self) -> NetworkTransaction:
        # A prior acknowledged commit may have been interrupted while retiring
        # an older profile. Finish that exact journal before accepting a new save.
        self._cleanup_interrupted_persistent_profiles()
        return NetworkTransaction(self, self.snapshot())

    def _profile_name(self, profile: NetworkProfile) -> str:
        fingerprint = hashlib.sha256(
            f"{profile.ssid}\0{profile.security.value}\0{int(profile.hidden)}".encode()
        ).hexdigest()[:12]
        return f"tidetracker-{fingerprint}"

    def _render_keyfile(
        self,
        profile: NetworkProfile,
        profile_uuid: str,
        *,
        autoconnect: bool,
    ) -> str:
        parser = configparser.RawConfigParser(interpolation=None)
        parser["connection"] = {
            "id": self._profile_name(profile),
            "uuid": profile_uuid,
            "type": "wifi",
            "interface-name": self.interface,
            "autoconnect": "true" if autoconnect else "false",
            "autoconnect-retries": "1",
        }
        parser["wifi"] = {
            "mode": "infrastructure",
            "ssid": profile.ssid,
            "hidden": "true" if profile.hidden else "false",
        }
        if profile.security is SecurityMode.WPA_PERSONAL:
            parser["wifi-security"] = {"key-mgmt": "wpa-psk", "psk": profile.password or ""}
        elif profile.security is SecurityMode.WPA_ENTERPRISE:
            parser["wifi-security"] = {"key-mgmt": "wpa-eap"}
            parser["802-1x"] = {
                "eap": "peap;",
                "identity": profile.identity or "",
                "password": profile.password or "",
                "phase2-auth": "mschapv2",
            }
        parser["ipv4"] = {"method": "auto"}
        parser["ipv6"] = {"method": "auto"}
        output = io.StringIO()
        parser.write(output, space_around_delimiters=False)
        return output.getvalue()

    def _render_hotspot_keyfile(
        self,
        *,
        ssid: str,
        profile_uuid: str,
        address: str,
    ) -> str:
        _clean_text(ssid, field_name="hotspot ssid", maximum=32)
        if len(ssid.encode("utf-8")) > 32:
            raise ValueError("hotspot ssid must not exceed 32 UTF-8 bytes")
        if not re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}/(?:[1-9]|[12]\d|3[0-2])", address):
            raise ValueError("hotspot address must be an IPv4 interface in CIDR notation")
        parser = configparser.RawConfigParser(interpolation=None)
        parser["connection"] = {
            "id": f"tidetracker-hotspot-{profile_uuid[:8]}",
            "uuid": profile_uuid,
            "type": "wifi",
            "interface-name": self.interface,
            "autoconnect": "false",
        }
        parser["wifi"] = {
            "mode": "ap",
            "band": "bg",
            "ssid": ssid,
        }
        # NetworkManager's shared mode owns DHCP/DNS for the captive network;
        # USB management remains a separate, never-default interface.
        parser["ipv4"] = {"method": "shared", "address1": address}
        parser["ipv6"] = {"method": "disabled"}
        output = io.StringIO()
        parser.write(output, space_around_delimiters=False)
        return output.getvalue()

    def _load_keyfile(self, contents: str, profile_uuid: str, *, directory: Path) -> str:
        directory_existed = directory.exists()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise NetworkError("NetworkManager profile directory is unsafe")
        os.chmod(directory, 0o700)
        if not directory_existed:
            _fsync_directory(directory.parent)
        path = directory / f"tidetracker-{profile_uuid}.nmconnection"
        descriptor, raw_path = tempfile.mkstemp(
            dir=directory,
            prefix=f".{path.name}.",
            suffix=".tmp",
            text=True,
        )
        temporary = Path(raw_path)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as destination:
                destination.write(contents)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, path)
            _fsync_directory(directory)
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or path.is_symlink()
                or (os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600)
                or (hasattr(os, "geteuid") and metadata.st_uid != os.geteuid())
            ):
                raise NetworkError("NetworkManager profile permissions are unsafe")
            self._run(["connection", "load", str(path)], operation="profile load")
        except Exception:
            cleanup_error: NetworkError | None = None
            try:
                self._delete_loaded_profile(
                    profile_uuid,
                    operation="failed profile-load deletion",
                    missing_ok=True,
                )
            except NetworkError as exc:
                cleanup_error = exc
            try:
                _durable_unlink(path)
            except NetworkError as unlink_error:
                if cleanup_error is None:
                    cleanup_error = unlink_error
            if cleanup_error is not None:
                raise NetworkError(
                    "NetworkManager profile load failed and cleanup was not durable"
                ) from cleanup_error
            raise
        finally:
            _durable_unlink(temporary)
        self._profile_paths.setdefault(profile_uuid, set()).add(path)
        return profile_uuid

    def install(self, profile: NetworkProfile) -> str:
        """Load a non-autoconnecting candidate from volatile storage."""

        profile_uuid = str(uuid.uuid4())
        self._load_keyfile(
            self._render_keyfile(profile, profile_uuid, autoconnect=False),
            profile_uuid,
            directory=self.runtime_directory,
        )
        self._candidate_profiles[profile_uuid] = profile
        return profile_uuid

    def _replacement_targets(self, profile: NetworkProfile) -> tuple[_RetiredProfile, ...]:
        targets: list[_RetiredProfile] = []
        try:
            paths = tuple(self.profile_directory.glob("tidetracker-*.nmconnection"))
        except OSError as exc:
            raise NetworkError("persistent NetworkManager profiles cannot be inspected") from exc
        for path in paths:
            record = self._read_profile(path)
            expected_app_path = self.profile_directory / (
                f"tidetracker-{record.profile_uuid}.nmconnection"
            )
            if _PROFILE_ID.fullmatch(record.connection_id) is None:
                continue
            if (
                path != expected_app_path
                or record.connection_type not in {"wifi", "802-11-wireless"}
                or record.interface != self.interface
                or record.wifi_mode != "infrastructure"
                or record.ssid is None
            ):
                raise NetworkError("owned NetworkManager profile has an invalid identity")
            if record.ssid == profile.ssid:
                targets.append(_RetiredProfile(record.profile_uuid, record.sha256))
        targets.sort(key=lambda item: item.profile_uuid)
        if (
            len(targets) > _MAX_RETIRED_PROFILES
            or len({item.profile_uuid for item in targets}) != len(targets)
        ):
            raise NetworkError("too many superseded NetworkManager profiles")
        return tuple(targets)

    @staticmethod
    def _journal_bytes(journal: _PromotionJournal) -> bytes:
        value = {
            "schema_version": 1,
            "phase": journal.phase,
            "new_uuid": journal.new_uuid,
            "new_sha256": journal.new_sha256,
            "retire": [
                {"uuid": item.profile_uuid, "sha256": item.sha256}
                for item in journal.retire
            ],
        }
        payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        if len(payload) > 8 * 1024:
            raise NetworkError("NetworkManager promotion journal exceeds its bound")
        return payload

    def _write_promotion_journal(
        self,
        journal: _PromotionJournal,
        *,
        require_absent: bool,
    ) -> Path:
        marker = self.profile_directory / f".tidetracker-{journal.new_uuid}.pending"
        directory_existed = self.profile_directory.exists()
        self.profile_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.profile_directory.is_symlink() or not self.profile_directory.is_dir():
            raise NetworkError("NetworkManager profile directory is unsafe")
        os.chmod(self.profile_directory, 0o700)
        if not directory_existed:
            _fsync_directory(self.profile_directory.parent)
        try:
            marker.lstat()
        except FileNotFoundError:
            marker_exists = False
        except OSError as exc:
            raise NetworkError("NetworkManager promotion marker cannot be inspected") from exc
        else:
            marker_exists = True
        if require_absent and marker_exists:
            raise NetworkError("NetworkManager promotion marker already exists")
        if not require_absent:
            if not marker_exists:
                raise NetworkError("NetworkManager promotion marker disappeared")
            current = self._parse_promotion_journal(marker)
            if current != _PromotionJournal(
                "preparing",
                journal.new_uuid,
                journal.new_sha256,
                journal.retire,
            ):
                raise NetworkError("NetworkManager promotion marker changed unexpectedly")

        descriptor, raw_temporary = tempfile.mkstemp(
            dir=self.profile_directory,
            prefix=f".{marker.name}.",
            suffix=".tmp",
        )
        temporary = Path(raw_temporary)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                os.chmod(temporary, 0o600)
                stream.write(self._journal_bytes(journal))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, marker)
            _fsync_directory(self.profile_directory)
        finally:
            _durable_unlink(temporary)
        self._profile_paths.setdefault(journal.new_uuid, set()).add(marker)
        return marker

    def persist(self, profile_uuid: str) -> None:
        """Promote an already loaded candidate to the durable profile directory."""

        try:
            profile = self._candidate_profiles[profile_uuid]
        except KeyError as exc:
            raise NetworkError("profile is not an uncommitted candidate") from exc

        active_contents = self._render_keyfile(profile, profile_uuid, autoconnect=True)
        preparing = _PromotionJournal(
            phase="preparing",
            new_uuid=profile_uuid,
            new_sha256=hashlib.sha256(active_contents.encode("utf-8")).hexdigest(),
            retire=self._replacement_targets(profile),
        )
        marker = self._write_promotion_journal(preparing, require_absent=True)
        # The first durable version is deliberately inert. Startup removes it
        # while the journal remains in `preparing`, preserving every old profile.
        self._load_keyfile(
            self._render_keyfile(profile, profile_uuid, autoconnect=False),
            profile_uuid,
            directory=self.profile_directory,
        )
        self._load_keyfile(
            active_contents,
            profile_uuid,
            directory=self.profile_directory,
        )

        candidate = self.runtime_directory / f"tidetracker-{profile_uuid}.nmconnection"
        _durable_unlink(candidate)
        paths = self._profile_paths.get(profile_uuid)
        if paths is not None:
            paths.discard(candidate)
        self._candidate_profiles.pop(profile_uuid, None)
        committed = _PromotionJournal(
            phase="committed",
            new_uuid=preparing.new_uuid,
            new_sha256=preparing.new_sha256,
            retire=preparing.retire,
        )
        self._write_promotion_journal(committed, require_absent=False)
        try:
            self._recover_promotion(marker, committed)
        except NetworkError as exc:
            raise _PromotionCommitted(
                "Wi-Fi profile was committed but superseded-profile cleanup is pending"
            ) from exc
        if paths is not None:
            paths.discard(marker)

    def start_hotspot(
        self,
        ssid: str = "TideTracker-Setup",
        *,
        address: str = "192.168.42.1/24",
    ) -> HotspotSession:
        """Start an open, isolated setup AP and retain enough state to restore Wi-Fi."""

        snapshot = self.snapshot()
        profile_uuid = str(uuid.uuid4())
        self._load_keyfile(
            self._render_hotspot_keyfile(
                ssid=ssid,
                profile_uuid=profile_uuid,
                address=address,
            ),
            profile_uuid,
            directory=self.runtime_directory,
        )
        try:
            self.activate(profile_uuid)
        except NetworkError:
            self.delete(profile_uuid, missing_ok=True)
            self.restore(snapshot)
            raise
        return HotspotSession(profile_uuid, ssid, snapshot)

    def stop_hotspot(self, session: HotspotSession, *, restore: bool = True) -> None:
        errors: list[NetworkError] = []
        try:
            self.delete(session.profile_uuid, missing_ok=True)
        except NetworkError as exc:
            errors.append(exc)
        if restore:
            try:
                self.restore(session.snapshot)
            except NetworkError as exc:
                errors.append(exc)
        if errors:
            raise NetworkError("hotspot teardown was incomplete") from errors[0]

    def prepare_client(self, *, timeout_seconds: float) -> RuntimeNetworkStatus:
        """Ask NetworkManager to activate its best saved client profile."""

        result = self._run(
            ["device", "connect", self.interface],
            operation="client preparation",
            check=False,
            timeout_seconds=timeout_seconds,
        )
        connected = result.returncode == 0
        return RuntimeNetworkStatus(
            connected=connected,
            interface=self.interface if connected else None,
        )

    def release_client(self, *, timeout_seconds: float) -> None:
        """Disconnect wlan0 and inhibit autoactivation until the next explicit connect."""

        result = self._run(
            ["device", "disconnect", self.interface],
            operation="client release",
            check=False,
            timeout_seconds=timeout_seconds,
        )
        if result.returncode != 0:
            raise NetworkError("NetworkManager could not release the Wi-Fi client")

    def activate(self, profile_uuid: str) -> None:
        self._run(
            ["connection", "up", "uuid", profile_uuid, "ifname", self.interface],
            operation="profile activation",
        )

    def delete(self, profile_uuid: str, *, missing_ok: bool = False) -> None:
        deletion_error: NetworkError | None = None
        try:
            self._delete_loaded_profile(
                profile_uuid,
                operation="profile deletion",
                missing_ok=missing_ok,
            )
        except NetworkError as exc:
            deletion_error = exc
        finally:
            paths = self._profile_paths.pop(profile_uuid, set())
            self._candidate_profiles.pop(profile_uuid, None)
            cleanup_errors: list[NetworkError] = []
            for path in paths:
                try:
                    _durable_unlink(path)
                except NetworkError as exc:
                    cleanup_errors.append(exc)
            if cleanup_errors:
                raise NetworkError("NetworkManager profile-file cleanup was incomplete") from (
                    cleanup_errors[0]
                )
        if deletion_error is not None:
            raise deletion_error

    def restore(self, snapshot: NetworkSnapshot) -> None:
        failures = 0
        for profile_uuid in snapshot.active_wifi_uuids:
            result = self._run(
                ["connection", "up", "uuid", profile_uuid, "ifname", self.interface],
                operation="profile restoration",
                check=False,
            )
            failures += result.returncode != 0
        if failures:
            raise NetworkError("NetworkManager could not restore the previous Wi-Fi profile")

    def connectivity(self) -> bool:
        result = self._run(
            ["--terse", "networking", "connectivity", "check"],
            operation="connectivity check",
            check=False,
        )
        return result.returncode == 0 and result.stdout.strip().lower() == "full"

    def test_and_save(
        self,
        profile: NetworkProfile,
        *,
        connectivity_check: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        check = connectivity_check or self.connectivity
        with self.transaction() as transaction:
            profile_uuid = transaction.install(profile)
            transaction.activate(profile_uuid)
            if not check():
                raise NetworkTestFailed("Wi-Fi connected without confirmed internet access")
            transaction.commit()
        return profile.public_dict()

    def force_save(self, profile: NetworkProfile) -> dict[str, object]:
        with self.transaction() as transaction:
            transaction.install(profile)
            transaction.commit()
        return profile.public_dict()


class NetworkTransaction:
    """A snapshot-backed profile transaction with deterministic rollback."""

    def __init__(self, adapter: NetworkManagerAdapter, snapshot: NetworkSnapshot) -> None:
        self.adapter = adapter
        self.snapshot = snapshot
        self.created: list[str] = []
        self._committed = False
        self._closed = False

    def __enter__(self) -> NetworkTransaction:
        return self

    def install(self, profile: NetworkProfile) -> str:
        if self._closed:
            raise NetworkError("network transaction is already closed")
        if self.created:
            raise NetworkError("a Wi-Fi transaction may install only one profile")
        profile_uuid = self.adapter.install(profile)
        self.created.append(profile_uuid)
        return profile_uuid

    def activate(self, profile_uuid: str) -> None:
        if profile_uuid not in self.created:
            raise NetworkError("profile does not belong to this transaction")
        self.adapter.activate(profile_uuid)

    def commit(self) -> None:
        if self._closed:
            raise NetworkError("network transaction is already closed")
        if len(self.created) != 1:
            raise NetworkError("network transaction has no single candidate to commit")
        try:
            profile_uuid = self.created[0]
            self.adapter.persist(profile_uuid)
        except _PromotionCommitted:
            self._committed = True
            raise
        self._committed = True

    def rollback(self) -> None:
        if self._closed:
            return
        errors: list[NetworkError] = []
        for profile_uuid in reversed(self.created):
            try:
                self.adapter.delete(profile_uuid, missing_ok=True)
            except NetworkError as exc:
                errors.append(exc)
        try:
            self.adapter.restore(self.snapshot)
        except NetworkError as exc:
            errors.append(exc)
        self._closed = True
        if errors:
            raise NetworkError("Wi-Fi rollback was incomplete") from errors[0]

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> Literal[False]:
        if self._committed:
            self._closed = True
            return False
        try:
            self.rollback()
        except NetworkError as rollback_error:
            if exc is None:
                raise
            raise NetworkError(
                "Wi-Fi transaction failed and rollback could not be proved"
            ) from rollback_error
        return False


__all__ = [
    "AccessPoint",
    "HotspotSession",
    "NetworkCommandError",
    "NetworkError",
    "NetworkManagerAdapter",
    "NetworkProfile",
    "NetworkSnapshot",
    "NetworkStatus",
    "NetworkTestFailed",
    "NetworkTransaction",
    "SecurityMode",
]
