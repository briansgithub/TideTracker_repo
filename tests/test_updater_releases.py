from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from tidetracker.updater import (
    OpenSSLEd25519Verifier,
    ReleaseStateError,
    ReleaseUpdater,
    ReleaseVerificationError,
)


class DigestVerifier:
    def verify(self, manifest: Path, signature: Path) -> bool:
        expected = b"test-signature:" + hashlib.sha256(manifest.read_bytes()).digest()
        return signature.read_bytes() == expected


class RejectingVerifier:
    def verify(self, manifest: Path, signature: Path) -> bool:
        del manifest, signature
        return False


def make_bundle(root: Path, release_id: str, payload: bytes = b"print('tide')\n") -> Path:
    bundle = root / f"bundle-{release_id}"
    application = bundle / "app"
    application.mkdir(parents=True)
    payload_path = application / "main.py"
    payload_path.write_bytes(payload)
    manifest = {
        "schema_version": 1,
        "release_id": release_id,
        "entrypoint": "app/main.py",
        "files": [
            {
                "path": "app/main.py",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }
    manifest_path = bundle / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    (bundle / "manifest.sig").write_bytes(
        b"test-signature:" + hashlib.sha256(manifest_path.read_bytes()).digest()
    )
    return bundle


def upload(updater: ReleaseUpdater, release_id: str, bundle: Path) -> None:
    updater.begin(release_id)
    updater.stage(release_id, bundle)
    updater.verify(release_id)


def test_verified_release_activation_confirmation_and_rollback(tmp_path: Path) -> None:
    updater = ReleaseUpdater(tmp_path / "state", DigestVerifier(), clock=lambda: 1234.0)
    upload(updater, "v1", make_bundle(tmp_path, "v1"))
    updater.activate("v1")
    updater.confirm("v1")
    upload(updater, "v2", make_bundle(tmp_path, "v2", b"print('v2')\n"))

    updater.activate("v2")

    assert updater.status() == {
        "schema_version": 1,
        "current": "v2",
        "previous": "v1",
        "trial": "v2",
        "in_progress": None,
        "verified": ["v1", "v2"],
    }
    assert updater.current_path().name == "v2"

    restored = updater.rollback()
    assert restored is not None
    assert restored.name == "v1"
    assert updater.status()["current"] == "v1"
    assert updater.status()["trial"] is None


def test_releases_are_reverified_before_activation(tmp_path: Path) -> None:
    updater = ReleaseUpdater(tmp_path / "state", DigestVerifier())
    upload(updater, "v1", make_bundle(tmp_path, "v1"))
    payload = tmp_path / "state" / "releases" / "v1" / "app" / "main.py"
    payload.write_bytes(b"tampered")

    with pytest.raises(ReleaseVerificationError, match="hash or size"):
        updater.activate("v1")


def test_bad_signature_and_bad_hash_never_become_verified(tmp_path: Path) -> None:
    bad_signature = ReleaseUpdater(tmp_path / "bad-signature", RejectingVerifier())
    bundle = make_bundle(tmp_path, "unsigned")
    bad_signature.begin("unsigned")
    bad_signature.stage("unsigned", bundle)
    with pytest.raises(ReleaseVerificationError, match="signature is invalid"):
        bad_signature.verify("unsigned")
    assert bad_signature.status()["verified"] == []

    bad_hash = ReleaseUpdater(tmp_path / "bad-hash", DigestVerifier())
    bundle = make_bundle(tmp_path, "bad-hash")
    bad_hash.begin("bad-hash")
    bad_hash.stage("bad-hash", bundle)
    (tmp_path / "bad-hash" / "incoming" / "bad-hash" / "app" / "main.py").write_text(
        "changed", encoding="utf-8"
    )
    with pytest.raises(ReleaseVerificationError, match="hash or size"):
        bad_hash.verify("bad-hash")
    assert bad_hash.status()["in_progress"] == "bad-hash"


def test_unlisted_files_are_rejected(tmp_path: Path) -> None:
    updater = ReleaseUpdater(tmp_path / "state", DigestVerifier())
    bundle = make_bundle(tmp_path, "v1")
    (bundle / "not-signed.txt").write_text("surprise", encoding="utf-8")
    updater.begin("v1")
    updater.stage("v1", bundle)

    with pytest.raises(ReleaseVerificationError, match="unlisted or missing"):
        updater.verify("v1")


def test_only_two_latest_releases_are_retained(tmp_path: Path) -> None:
    updater = ReleaseUpdater(tmp_path / "state", DigestVerifier())
    for index in range(1, 4):
        release_id = f"v{index}"
        upload(updater, release_id, make_bundle(tmp_path, release_id, str(index).encode()))
        updater.activate(release_id)
        updater.confirm(release_id)

    assert updater.status()["verified"] == ["v2", "v3"]
    assert {path.name for path in (tmp_path / "state" / "releases").iterdir()} == {"v2", "v3"}
    assert updater.status()["current"] == "v3"
    assert updater.status()["previous"] == "v2"


def test_trial_blocks_concurrent_release_upload(tmp_path: Path) -> None:
    updater = ReleaseUpdater(tmp_path / "state", DigestVerifier())
    upload(updater, "v1", make_bundle(tmp_path, "v1"))
    updater.activate("v1")

    with pytest.raises(ReleaseStateError, match="trial release"):
        updater.begin("v2")


def test_openssl_verifier_uses_argument_array_without_a_shell(tmp_path: Path) -> None:
    public_key = tmp_path / "release.pub"
    manifest = tmp_path / "manifest.json"
    signature = tmp_path / "manifest.sig"
    public_key.write_text("public", encoding="utf-8")
    manifest.write_text("{}", encoding="utf-8")
    signature.write_bytes(b"signature")
    calls: list[list[str]] = []

    def runner(
        args: Sequence[str], *, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "Signature Verified Successfully", "")

    verifier = OpenSSLEd25519Verifier(public_key, binary="openssl-stub", runner=runner)

    assert verifier.verify(manifest, signature) is True
    assert calls == [
        [
            "openssl-stub",
            "pkeyutl",
            "-verify",
            "-pubin",
            "-inkey",
            str(public_key),
            "-rawin",
            "-in",
            str(manifest),
            "-sigfile",
            str(signature),
        ]
    ]


def test_interrupted_directory_move_is_recovered_on_verify(tmp_path: Path) -> None:
    updater = ReleaseUpdater(tmp_path / "state", DigestVerifier())
    bundle = make_bundle(tmp_path, "v1")
    updater.begin("v1")
    updater.stage("v1", bundle)
    (updater.incoming / "v1").replace(updater.releases / "v1")

    manifest = updater.verify("v1")

    assert manifest.release_id == "v1"
    assert updater.status()["in_progress"] is None
    assert updater.status()["verified"] == ["v1"]


def test_abort_discards_only_active_unverified_upload(tmp_path: Path) -> None:
    updater = ReleaseUpdater(tmp_path / "state", DigestVerifier())
    target = updater.begin("v1")
    (target / "partial").write_text("partial", encoding="utf-8")

    updater.abort("v1")

    assert not target.exists()
    assert updater.status()["in_progress"] is None
    assert updater.status()["verified"] == []


def test_startup_durably_recovers_exact_private_state_temps(tmp_path: Path) -> None:
    root = tmp_path / "state"
    updater = ReleaseUpdater(root, DigestVerifier())
    updater.begin("candidate")
    interrupted = (
        root / ".state.json.123.456.tmp",
        root / ".state.json.124.457.tmp",
    )
    for path in interrupted:
        path.write_bytes(b"partial")
        if os.name != "nt":
            path.chmod(0o600)

    recovered = ReleaseUpdater(root, DigestVerifier())

    assert recovered.status()["in_progress"] == "candidate"
    assert all(not path.exists() for path in interrupted)


@pytest.mark.parametrize(
    "name",
    [
        ".state.json.bad.456.tmp",
        ".state.json.123.bad.tmp",
    ],
)
def test_startup_rejects_malformed_owned_state_temp_names(tmp_path: Path, name: str) -> None:
    root = tmp_path / "state"
    root.mkdir()
    (root / name).write_bytes(b"partial")

    with pytest.raises(ReleaseStateError, match="temporary path is malformed"):
        ReleaseUpdater(root, DigestVerifier())


def test_startup_rejects_too_many_interrupted_state_writes(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    for index in range(9):
        path = root / f".state.json.{100 + index}.456.tmp"
        path.write_bytes(b"partial")
        if os.name != "nt":
            path.chmod(0o600)

    with pytest.raises(ReleaseStateError, match="entry bound"):
        ReleaseUpdater(root, DigestVerifier())


def test_startup_rejects_unsafe_state_temp(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    (root / ".state.json.123.456.tmp").mkdir()

    with pytest.raises(ReleaseStateError, match="temporary path is unsafe"):
        ReleaseUpdater(root, DigestVerifier())


def test_state_read_is_bounded_and_rejects_nonregular_paths(tmp_path: Path) -> None:
    oversized_root = tmp_path / "oversized"
    oversized_root.mkdir()
    oversized = oversized_root / "state.json"
    oversized.write_bytes(b"x" * (64 * 1024 + 1))
    if os.name != "nt":
        oversized.chmod(0o600)

    with pytest.raises(ReleaseStateError, match="malformed or unsafe"):
        ReleaseUpdater(oversized_root, DigestVerifier())

    directory_root = tmp_path / "directory"
    directory_root.mkdir()
    (directory_root / "state.json").mkdir()
    with pytest.raises(ReleaseStateError, match="path is unsafe"):
        ReleaseUpdater(directory_root, DigestVerifier())


def test_startup_removes_unreferenced_partial_release_idempotently(tmp_path: Path) -> None:
    root = tmp_path / "state"
    updater = ReleaseUpdater(root, DigestVerifier())
    upload(updater, "current", make_bundle(tmp_path, "current"))
    updater.activate("current")
    updater.confirm("current")
    orphan = updater.releases / "retired"
    orphan.mkdir(mode=0o700)
    (orphan / "partially-removed").write_bytes(b"partial")

    recovered = ReleaseUpdater(root, DigestVerifier())
    recovered_again = ReleaseUpdater(root, DigestVerifier())

    assert not orphan.exists()
    assert recovered.status()["current"] == "current"
    assert recovered_again.current_path().name == "current"


def test_startup_restores_two_verified_release_bound_state_first(tmp_path: Path) -> None:
    root = tmp_path / "state"
    updater = ReleaseUpdater(root, DigestVerifier())
    upload(updater, "v1", make_bundle(tmp_path, "v1"))
    upload(updater, "v2", make_bundle(tmp_path, "v2"))
    state = updater._read_state()
    make_bundle(tmp_path, "v3").replace(updater.releases / "v3")
    state["verified_order"] = ["v1", "v2", "v3"]
    updater._write_state(state)

    recovered = ReleaseUpdater(root, DigestVerifier())

    assert recovered.status()["verified"] == ["v2", "v3"]
    assert {path.name for path in recovered.releases.iterdir()} == {"v2", "v3"}


def test_startup_preserves_release_moved_during_in_progress_verify(tmp_path: Path) -> None:
    root = tmp_path / "state"
    updater = ReleaseUpdater(root, DigestVerifier())
    bundle = make_bundle(tmp_path, "candidate")
    updater.begin("candidate")
    updater.stage("candidate", bundle)
    (updater.incoming / "candidate").replace(updater.releases / "candidate")

    recovered = ReleaseUpdater(root, DigestVerifier())

    assert (recovered.releases / "candidate").is_dir()
    assert recovered.status()["in_progress"] == "candidate"
    assert recovered.verify("candidate").release_id == "candidate"


def test_startup_fails_closed_on_unsafe_release_inventory_entry(tmp_path: Path) -> None:
    root = tmp_path / "state"
    updater = ReleaseUpdater(root, DigestVerifier())
    (updater.releases / "unexpected-file").write_bytes(b"unsafe")

    with pytest.raises(ReleaseStateError, match="unsafe entry"):
        ReleaseUpdater(root, DigestVerifier())
