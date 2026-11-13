from __future__ import annotations

import importlib.util
import io
import os
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest


def load_dev_bundle() -> ModuleType:
    path = Path(__file__).parents[1] / "deploy" / "dev" / "dev_bundle.py"
    spec = importlib.util.spec_from_file_location("tidetracker_dev_bundle", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_source(root: Path, marker: str = "one") -> Path:
    (root / "src" / "tidetracker").mkdir(parents=True)
    (root / "src" / "tidetracker" / "__init__.py").write_text(
        f"MARKER = {marker!r}\n", encoding="utf-8"
    )
    (root / "e-ink_lib" / "waveshare_epd").mkdir(parents=True)
    (root / "e-ink_lib" / "waveshare_epd" / "__init__.py").write_text("", encoding="utf-8")
    module = load_dev_bundle()
    for name in module.FIXED_ASSETS:
        (root / name).write_bytes(f"asset:{name}".encode())
    return root


def rewrite_archive(source: Path, output: Path, mutate: dict[str, bytes]) -> None:
    with tarfile.open(source, "r:") as original, tarfile.open(
        output, "w", format=tarfile.GNU_FORMAT
    ) as rewritten:
        for old_member in original.getmembers():
            stream = original.extractfile(old_member)
            assert stream is not None
            payload = mutate.get(old_member.name, stream.read())
            member = tarfile.TarInfo(old_member.name)
            member.size = len(payload)
            member.mode = old_member.mode
            member.uid = 0
            member.gid = 0
            member.mtime = 0
            rewritten.addfile(member, io.BytesIO(payload))


def test_pack_is_deterministic_minimal_and_extractable(tmp_path: Path) -> None:
    module = load_dev_bundle()
    source = make_source(tmp_path / "source")
    first = tmp_path / "first.tar"
    second = tmp_path / "second.tar"

    first_record = module.build_bundle(source, first)
    second_record = module.build_bundle(source, second)

    assert first.read_bytes() == second.read_bytes()
    assert first_record["archive_sha256"] == second_record["archive_sha256"]
    assert first_record["release_id"] == second_record["release_id"]
    with tarfile.open(first, "r:") as archive:
        names = {member.name for member in archive.getmembers()}
    assert "assets/error_image.bmp" in names
    assert "assets/plot_image.bmp" not in names
    assert names == {
        module.MANIFEST_NAME,
        "src/tidetracker/__init__.py",
        "e-ink_lib/waveshare_epd/__init__.py",
        *(f"assets/{name}" for name in module.FIXED_ASSETS),
    }

    extracted = tmp_path / "extracted"
    manifest = module.extract_bundle(
        first,
        extracted,
        expected_sha256=str(first_record["archive_sha256"]),
        expected_release_id=str(first_record["release_id"]),
        incoming_root=tmp_path,
    )
    assert manifest["release_id"] == first_record["release_id"]
    module.verify_release_directory(extracted, manifest)


def test_pack_refuses_to_overwrite(tmp_path: Path) -> None:
    module = load_dev_bundle()
    source = make_source(tmp_path / "source")
    output = tmp_path / "bundle.tar"
    output.write_bytes(b"keep-me")

    with pytest.raises(FileExistsError):
        module.build_bundle(source, output)

    assert output.read_bytes() == b"keep-me"


@pytest.mark.parametrize("name", ["../escape", "/absolute", "safe/../../escape", "bad\\name"])
def test_extract_rejects_unsafe_member_paths(tmp_path: Path, name: str) -> None:
    module = load_dev_bundle()
    archive_path = tmp_path / "unsafe.tar"
    with tarfile.open(archive_path, "w") as archive:
        payload = b"escape"
        member = tarfile.TarInfo(name)
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))

    with pytest.raises(module.DevBundleError, match="unsafe"):
        module.extract_bundle(archive_path, tmp_path / "destination", incoming_root=tmp_path)
    assert not (tmp_path / "escape").exists()


def test_extract_rejects_links_and_unlisted_files(tmp_path: Path) -> None:
    module = load_dev_bundle()
    link_archive = tmp_path / "link.tar"
    with tarfile.open(link_archive, "w") as archive:
        member = tarfile.TarInfo("src/tidetracker/link.py")
        member.type = tarfile.SYMTYPE
        member.linkname = "/etc/passwd"
        archive.addfile(member)
    with pytest.raises(module.DevBundleError, match="non-regular"):
        module.extract_bundle(link_archive, tmp_path / "link-output", incoming_root=tmp_path)

    extra_archive = tmp_path / "extra.tar"
    with tarfile.open(extra_archive, "w") as archive:
        payload = b"extra"
        member = tarfile.TarInfo("notes.txt")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    with pytest.raises(module.DevBundleError, match="disallowed"):
        module.extract_bundle(extra_archive, tmp_path / "extra-output", incoming_root=tmp_path)


def test_extract_rejects_payload_manifest_mismatch(tmp_path: Path) -> None:
    module = load_dev_bundle()
    source = make_source(tmp_path / "source")
    valid = tmp_path / "valid.tar"
    record = module.build_bundle(source, valid)
    tampered = tmp_path / "tampered.tar"
    rewrite_archive(valid, tampered, {"src/tidetracker/__init__.py": b"changed\n"})

    with pytest.raises(module.DevBundleError, match="does not match its manifest"):
        module.extract_bundle(
            tampered,
            tmp_path / "output",
            expected_release_id=str(record["release_id"]),
            incoming_root=tmp_path,
        )


def test_archive_must_be_direct_child_of_incoming(tmp_path: Path) -> None:
    module = load_dev_bundle()
    source = make_source(tmp_path / "source")
    nested = tmp_path / "nested"
    nested.mkdir()
    archive = nested / "bundle.tar"
    module.build_bundle(source, archive)

    with pytest.raises(module.DevBundleError, match="direct child"):
        module.extract_bundle(archive, tmp_path / "output", incoming_root=tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="Windows developer mode may prohibit symlinks")
def test_activation_rolls_back_current_pointer_on_service_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_dev_bundle()
    source = make_source(tmp_path / "source")
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    install = tmp_path / "install"
    state = tmp_path / "state"
    first = incoming / "first.tar"
    second = incoming / "second.tar"
    first_record = module.build_bundle(source, first, "dev-first")
    (source / "src" / "tidetracker" / "__init__.py").write_text("MARKER = 'two'\n")
    second_record = module.build_bundle(source, second, "dev-second")
    monkeypatch.setattr(module, "_run_candidate", lambda *args, **kwargs: None)

    module.activate_bundle(
        first,
        str(first_record["archive_sha256"]),
        "dev-first",
        install_root=install,
        state_root=state,
        incoming_root=incoming,
        restart=False,
    )

    def fail_restart(service: str) -> None:
        raise module.DevBundleError(f"failed: {service}")

    monkeypatch.setattr(module, "_restart_service", fail_restart)
    with pytest.raises(module.DevBundleError, match="failed"):
        module.activate_bundle(
            second,
            str(second_record["archive_sha256"]),
            "dev-second",
            install_root=install,
            state_root=state,
            incoming_root=incoming,
            restart=True,
        )
    assert os.readlink(install / "current") == "releases/dev-first"


def test_real_bundle_candidate_cycle_produces_exact_framebuffer(tmp_path: Path) -> None:
    module = load_dev_bundle()
    repository = Path(__file__).parents[1]
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    archive = incoming / "real.tar"
    record = module.build_bundle(repository, archive)
    release = tmp_path / "release"
    module.extract_bundle(
        archive,
        release,
        expected_sha256=str(record["archive_sha256"]),
        incoming_root=incoming,
    )

    module._run_candidate(release, tmp_path / "state", 60.0)


def test_boot_unit_and_builder_are_guarded_and_simulation_only() -> None:
    repository = Path(__file__).parents[1]
    unit = (repository / "deploy" / "dev" / "tidetracker-dev.service").read_text()
    builder = (repository / "deploy" / "build-zero-dev-image.sh").read_text()

    assert "--simulate --builtin --mode run" in unit
    assert "--service" not in unit
    assert "GPIO" not in unit
    assert "BUILD-ZERO-DEV" in builder
    assert "DRY RUN: no image was created" in builder
    assert "modules-load=dwc2,g_ether" in builder
    assert "10.77.0.2/30" in builder
    assert " of=\"/dev/" not in builder
    assert "PhysicalDrive" not in builder
