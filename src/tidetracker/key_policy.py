"""Canonical five-key policy used by role-image build and release verification."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import pathlib
import stat
from typing import Any

POLICY_SCHEMA_VERSION = 1
ROLE_MANIFEST_SCHEMA_VERSION = 2
ED25519_DER_PREFIX = bytes.fromhex("302a300506032b6570032100")
KEY_SPECS = (
    ("workstation_to_controller_ssh", "ssh"),
    ("controller_to_zero_ssh", "ssh"),
    ("application_release", "pem"),
    ("lab_safety", "pem"),
    ("image_release", "pem"),
)
RAW_FIELDS = tuple(f"{name}_public_key_sha256" for name, _kind in KEY_SPECS)
MATERIAL_FIELDS = tuple(f"{name}_key_material_sha256" for name, _kind in KEY_SPECS)
POLICY_FIELDS = ("schema_version", *RAW_FIELDS, *MATERIAL_FIELDS, "key_policy_sha256")


class KeyPolicyError(ValueError):
    pass


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def read_public_key(path: pathlib.Path, label: str, *, maximum: int = 64 * 1024) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise KeyPolicyError(f"{label} is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_size < 1
        or metadata.st_size > maximum
    ):
        raise KeyPolicyError(f"{label} must be a bounded regular, non-symlink file")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise KeyPolicyError(f"{label} is unreadable") from exc
    if len(payload) != metadata.st_size:
        raise KeyPolicyError(f"{label} changed while it was read")
    return payload


def openssh_ed25519_der(payload: bytes, label: str = "OpenSSH public key") -> bytes:
    try:
        line = payload.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise KeyPolicyError(f"{label} is not strict UTF-8") from exc
    if "\n" in line or "\r" in line:
        raise KeyPolicyError(f"{label} must contain exactly one record")
    fields = line.split()
    if len(fields) < 2 or fields[0] != "ssh-ed25519":
        raise KeyPolicyError(f"{label} must be an OpenSSH Ed25519 public key")
    try:
        wire = base64.b64decode(fields[1], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise KeyPolicyError(f"{label} has invalid base64 material") from exc
    if len(wire) != 51:
        raise KeyPolicyError(f"{label} has invalid Ed25519 wire length")
    type_length = int.from_bytes(wire[0:4], "big")
    key_length = int.from_bytes(wire[15:19], "big")
    if type_length != 11 or wire[4:15] != b"ssh-ed25519" or key_length != 32:
        raise KeyPolicyError(f"{label} has invalid Ed25519 wire fields")
    return ED25519_DER_PREFIX + wire[19:51]


def pem_ed25519_der(payload: bytes, label: str = "PEM public key") -> bytes:
    try:
        lines = payload.decode("ascii", errors="strict").strip().splitlines()
    except UnicodeDecodeError as exc:
        raise KeyPolicyError(f"{label} is not strict ASCII PEM") from exc
    if (
        len(lines) < 3
        or lines[0] != "-----BEGIN PUBLIC KEY-----"
        or lines[-1] != "-----END PUBLIC KEY-----"
        or any(not line or line.strip() != line for line in lines[1:-1])
    ):
        raise KeyPolicyError(f"{label} is not a strict public-key PEM")
    try:
        der = base64.b64decode("".join(lines[1:-1]), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise KeyPolicyError(f"{label} has invalid PEM base64 material") from exc
    if len(der) != 44 or not der.startswith(ED25519_DER_PREFIX):
        raise KeyPolicyError(f"{label} must contain an Ed25519 public key")
    return der


def _policy_digest(policy_without_digest: dict[str, Any]) -> str:
    payload = json.dumps(
        policy_without_digest, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    return _sha256(payload)


def build_key_policy(
    *,
    workstation_to_controller_ssh: bytes,
    controller_to_zero_ssh: bytes,
    application_release: bytes,
    lab_safety: bytes,
    image_release: bytes,
) -> dict[str, Any]:
    inputs = {
        "workstation_to_controller_ssh": workstation_to_controller_ssh,
        "controller_to_zero_ssh": controller_to_zero_ssh,
        "application_release": application_release,
        "lab_safety": lab_safety,
        "image_release": image_release,
    }
    policy: dict[str, Any] = {"schema_version": POLICY_SCHEMA_VERSION}
    material_hashes: list[str] = []
    for name, kind in KEY_SPECS:
        raw = inputs[name]
        der = (
            openssh_ed25519_der(raw, name)
            if kind == "ssh"
            else pem_ed25519_der(raw, name)
        )
        policy[f"{name}_public_key_sha256"] = _sha256(raw)
        material_hash = _sha256(der)
        policy[f"{name}_key_material_sha256"] = material_hash
        material_hashes.append(material_hash)
    if len(set(material_hashes)) != len(material_hashes):
        raise KeyPolicyError("all five SSH/application/lab/image keys must be distinct")
    policy["key_policy_sha256"] = _policy_digest(policy)
    return policy


def validate_key_policy(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(POLICY_FIELDS):
        raise KeyPolicyError("key policy does not have the exact schema-1 field set")
    if (
        type(value.get("schema_version")) is not int
        or value.get("schema_version") != POLICY_SCHEMA_VERSION
    ):
        raise KeyPolicyError("key policy schema_version must be 1")
    for field in (*RAW_FIELDS, *MATERIAL_FIELDS, "key_policy_sha256"):
        item = value.get(field)
        if (
            not isinstance(item, str)
            or len(item) != 64
            or any(character not in "0123456789abcdef" for character in item)
        ):
            raise KeyPolicyError(f"key policy field {field} is not a lowercase SHA-256")
    if len({str(value[field]) for field in MATERIAL_FIELDS}) != len(MATERIAL_FIELDS):
        raise KeyPolicyError("key policy reuses canonical key material")
    without_digest = {
        field: value[field] for field in POLICY_FIELDS if field != "key_policy_sha256"
    }
    if value["key_policy_sha256"] != _policy_digest(without_digest):
        raise KeyPolicyError("key policy digest is inconsistent with its canonical record")
    return dict(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name, _kind in KEY_SPECS:
        parser.add_argument(
            f"--{name.replace('_', '-')}-public-key",
            type=pathlib.Path,
            required=True,
        )
    arguments = parser.parse_args(argv)
    values = {
        name: read_public_key(getattr(arguments, f"{name}_public_key"), name)
        for name, _kind in KEY_SPECS
    }
    policy = build_key_policy(**values)
    print(json.dumps(policy, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
