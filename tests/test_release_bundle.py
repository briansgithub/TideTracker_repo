from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from collections.abc import Sequence
from pathlib import Path

import pytest

import tidetracker.release as release_module
from tidetracker.release import (
    MAX_ARCHIVE_BYTES,
    BundleError,
    DeterministicBundleBuilder,
    OpenSSLEd25519Signer,
    extract_bundle,
)
from tidetracker.updater import MAX_RELEASE_BYTES


class DigestSigner:
    def sign(self, manifest: Path, signature: Path) -> None:
        signature.write_bytes(b"test-signature:" + hashlib.sha256(manifest.read_bytes()).digest())


def test_bundle_is_deterministic_and_extracts_controller_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "bin").mkdir(parents=True)
    entrypoint = source / "bin" / "tidetracker-release.py"
    entrypoint.write_text("print('release')\n", encoding="utf-8")
    entrypoint.chmod(0o755)
    (source / "data.txt").write_text("payload\n", encoding="utf-8")
    first = tmp_path / "first.tar"
    second = tmp_path / "second.tar"
    builder = DeterministicBundleBuilder(DigestSigner())

    built_first = builder.build(
        source,
        first,
        release_id="v2.0.0",
        entrypoint="bin/tidetracker-release.py",
    )
    built_second = builder.build(
        source,
        second,
        release_id="v2.0.0",
        entrypoint="bin/tidetracker-release.py",
    )

    assert built_first.sha256 == built_second.sha256
    assert first.read_bytes() == second.read_bytes()
    extracted = tmp_path / "extracted"
    manifest = extract_bundle(first, extracted)
    assert manifest.release_id == "v2.0.0"
    assert manifest.entrypoint == "bin/tidetracker-release.py"
    assert (extracted / "data.txt").read_text(encoding="utf-8") == "payload\n"
    with tarfile.open(first, "r") as archive:
        assert archive.getmember("bin/tidetracker-release.py").mode == 0o755


@pytest.mark.parametrize("name", ["../escape", "/absolute", "safe/../../escape", "bad\\name"])
def test_extraction_rejects_unsafe_member_paths(tmp_path: Path, name: str) -> None:
    archive_path = tmp_path / "unsafe.tar"
    with tarfile.open(archive_path, "w") as archive:
        payload = b"escape"
        member = tarfile.TarInfo(name)
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))

    with pytest.raises(BundleError, match="unsafe path"):
        extract_bundle(archive_path, tmp_path / "destination")
    assert not (tmp_path / "escape").exists()


def test_extraction_rejects_links(tmp_path: Path) -> None:
    archive_path = tmp_path / "link.tar"
    with tarfile.open(archive_path, "w") as archive:
        member = tarfile.TarInfo("manifest.json")
        member.type = tarfile.SYMTYPE
        member.linkname = "/etc/passwd"
        archive.addfile(member)

    with pytest.raises(BundleError, match="non-regular"):
        extract_bundle(archive_path, tmp_path / "destination")


def test_extraction_rejects_compression_and_oversized_archives(tmp_path: Path) -> None:
    compressed = tmp_path / "compressed.tar.gz"
    with tarfile.open(compressed, "w:gz") as archive:
        payload = b"small"
        member = tarfile.TarInfo("entry.py")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    with pytest.raises(BundleError, match="invalid"):
        extract_bundle(compressed, tmp_path / "compressed-output")

    oversized = tmp_path / "oversized.tar"
    with oversized.open("wb") as stream:
        stream.truncate(MAX_ARCHIVE_BYTES + 1)
    with pytest.raises(BundleError, match="bounded regular"):
        extract_bundle(oversized, tmp_path / "oversized-output")
    assert not (tmp_path / "oversized-output").exists()


def test_extraction_counts_members_while_streaming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_path = tmp_path / "members.tar"
    with tarfile.open(archive_path, "w") as archive:
        for index in range(3):
            member = tarfile.TarInfo(f"file-{index}")
            member.size = 0
            archive.addfile(member, io.BytesIO())
    monkeypatch.setattr(release_module, "MAX_ARCHIVE_MEMBERS", 2)

    with pytest.raises(BundleError, match="too many members"):
        extract_bundle(archive_path, tmp_path / "member-output")
    assert not (tmp_path / "member-output").exists()


def test_builder_uses_snapshotted_payload_after_signer_side_effect(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    entrypoint = source / "entry.py"
    entrypoint.write_text("print('original')\n", encoding="utf-8")

    class MutatingSigner:
        def sign(self, manifest: Path, signature: Path) -> None:
            entrypoint.write_text("print('mutated')\n", encoding="utf-8")
            signature.write_bytes(b"signature")

    bundle = tmp_path / "release.tar"
    DeterministicBundleBuilder(MutatingSigner()).build(
        source,
        bundle,
        release_id="snapshot-test",
        entrypoint="entry.py",
    )
    extract_bundle(bundle, tmp_path / "snapshot-output")

    assert (tmp_path / "snapshot-output" / "entry.py").read_text(encoding="utf-8") == (
        "print('original')\n"
    )
    assert MAX_RELEASE_BYTES <= 32 * 1024 * 1024


def test_builder_rejects_native_payloads(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "entry.py").write_text("print('release')\n", encoding="utf-8")
    (source / "extension.so").write_bytes(b"\x7fELFcompiled")

    with pytest.raises(BundleError, match="pure Python"):
        DeterministicBundleBuilder(DigestSigner()).build(
            source,
            tmp_path / "release.tar",
            release_id="v1",
            entrypoint="entry.py",
        )


def test_openssl_signer_uses_argument_array_without_shell(tmp_path: Path) -> None:
    private_key = tmp_path / "private.pem"
    manifest = tmp_path / "manifest.json"
    signature = tmp_path / "manifest.sig"
    private_key.write_text("private", encoding="utf-8")
    manifest.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    calls: list[list[str]] = []

    def runner(
        args: Sequence[str], *, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        calls.append(list(args))
        command = list(args)
        Path(command[command.index("-out") + 1]).write_bytes(b"signature")
        return subprocess.CompletedProcess(args, 0, "", "")

    OpenSSLEd25519Signer(private_key, binary="openssl-stub", runner=runner).sign(
        manifest, signature
    )

    assert signature.read_bytes() == b"signature"
    assert calls == [
        [
            "openssl-stub",
            "pkeyutl",
            "-sign",
            "-inkey",
            str(private_key),
            "-rawin",
            "-in",
            str(manifest),
            "-out",
            str(signature),
        ]
    ]
