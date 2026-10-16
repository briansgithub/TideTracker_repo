from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


def load_verifier() -> ModuleType:
    path = Path(__file__).parents[1] / "deploy" / "verify-package-bundle.py"
    spec = importlib.util.spec_from_file_location("tidetracker_package_verifier", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_bundle(tmp_path: Path) -> tuple[Path, list[dict[str, object]]]:
    bundle = tmp_path / "bundle"
    debs = bundle / "debs"
    debs.mkdir(parents=True)
    entries: list[dict[str, object]] = []
    for name in ("common-pkg", "controller-only", "zero-only"):
        filename = f"{name}_1.0_armhf.deb"
        payload = f"fake-deb:{name}".encode()
        (debs / filename).write_bytes(payload)
        entries.append(
            {
                "name": name,
                "version": "1.0",
                "architecture": "armhf",
                "filename": filename,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    lock = {
        "schema_version": 2,
        "architecture": "armhf",
        "roles": {
            "controller": {
                "requested": ["common-pkg", "controller-only"],
                "packages": ["common-pkg", "controller-only"],
            },
            "zero": {
                "requested": ["common-pkg", "zero-only"],
                "packages": ["common-pkg", "zero-only"],
            },
        },
        "packages": entries,
    }
    (bundle / "packages.lock.json").write_text(
        json.dumps(lock, separators=(",", ":")), encoding="utf-8"
    )
    return bundle, entries


def test_validation_returns_only_selected_role_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = load_verifier()
    bundle, entries = make_bundle(tmp_path)
    common = tmp_path / "common.txt"
    controller = tmp_path / "controller.txt"
    common.write_text("common-pkg\n", encoding="utf-8")
    controller.write_text("controller-only\n", encoding="utf-8")
    metadata = iter(
        f"Package: {entry['name']}\nVersion: {entry['version']}\n"
        f"Architecture: {entry['architecture']}\n"
        for entry in entries
    )
    monkeypatch.setattr(
        verifier.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, next(metadata), ""),
    )

    selected, digest, _ = verifier.validate(bundle, "controller", [common, controller])

    assert [entry["name"] for entry in selected] == ["common-pkg", "controller-only"]
    assert len(digest) == 64


def test_validation_rejects_global_package_outside_both_role_closures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = load_verifier()
    bundle, entries = make_bundle(tmp_path)
    lock_path = bundle / "packages.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    payload = b"fake-deb:irrelevant"
    filename = "irrelevant_1.0_armhf.deb"
    (bundle / "debs" / filename).write_bytes(payload)
    irrelevant = {
        "name": "irrelevant",
        "version": "1.0",
        "architecture": "armhf",
        "filename": filename,
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    lock["packages"].append(irrelevant)
    entries.append(irrelevant)
    lock_path.write_text(json.dumps(lock, separators=(",", ":")), encoding="utf-8")
    common = tmp_path / "common.txt"
    zero = tmp_path / "zero.txt"
    common.write_text("common-pkg\n", encoding="utf-8")
    zero.write_text("zero-only\n", encoding="utf-8")
    metadata = iter(
        f"Package: {entry['name']}\nVersion: {entry['version']}\n"
        f"Architecture: {entry['architecture']}\n"
        for entry in entries
    )
    monkeypatch.setattr(
        verifier.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, next(metadata), ""),
    )

    with pytest.raises(verifier.BundleError, match="exact union of role closures"):
        verifier.validate(bundle, "zero", [common, zero])


def test_validation_accepts_debian_tilde_in_package_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = load_verifier()
    bundle, entries = make_bundle(tmp_path)
    entry = entries[0]
    old_path = bundle / "debs" / str(entry["filename"])
    filename = "common-pkg_1.0~deb12u1_armhf.deb"
    old_path.rename(bundle / "debs" / filename)
    entry["filename"] = filename
    lock_path = bundle / "packages.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["packages"] = entries
    lock_path.write_text(json.dumps(lock, separators=(",", ":")), encoding="utf-8")
    common = tmp_path / "common.txt"
    controller = tmp_path / "controller.txt"
    common.write_text("common-pkg\n", encoding="utf-8")
    controller.write_text("controller-only\n", encoding="utf-8")
    metadata = iter(
        f"Package: {child['name']}\nVersion: {child['version']}\n"
        f"Architecture: {child['architecture']}\n"
        for child in entries
    )
    monkeypatch.setattr(
        verifier.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, next(metadata), ""),
    )

    selected, _, _ = verifier.validate(bundle, "controller", [common, controller])

    assert [child["name"] for child in selected] == ["common-pkg", "controller-only"]
