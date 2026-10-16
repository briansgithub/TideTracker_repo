from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


def load_generator() -> ModuleType:
    path = Path(__file__).parents[1] / "deploy" / "create-package-lock.py"
    spec = importlib.util.spec_from_file_location("tidetracker_package_lock", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def record(module: ModuleType, name: str, *, version: str = "1.0") -> object:
    return module.PackageRecord(
        name=name,
        version=version,
        architecture="armhf",
        filename=f"{name}_{version}_armhf.deb",
        size=10,
        sha256="0" * 64,
    )


def test_merge_records_builds_exact_sorted_role_union() -> None:
    generator = load_generator()
    common = record(generator, "common")
    controller = record(generator, "controller-only")
    zero = record(generator, "zero-only")

    packages, roles = generator.merge_records(
        {
            "controller": {"controller-only": controller, "common": common},
            "zero": {"zero-only": zero, "common": common},
        },
        {
            "controller": ["controller-only", "common"],
            "zero": ["zero-only", "common"],
        },
    )

    assert list(sorted(packages)) == ["common", "controller-only", "zero-only"]
    assert roles["controller"]["packages"] == ["common", "controller-only"]
    assert roles["zero"]["packages"] == ["common", "zero-only"]


def test_merge_records_rejects_requested_package_missing_from_download() -> None:
    generator = load_generator()
    common = record(generator, "common")

    with pytest.raises(generator.LockError, match="omits requested packages: controller-only"):
        generator.merge_records(
            {"controller": {"common": common}, "zero": {"common": common}},
            {
                "controller": ["common", "controller-only"],
                "zero": ["common"],
            },
        )


def test_merge_records_rejects_cross_role_version_drift() -> None:
    generator = load_generator()

    with pytest.raises(generator.LockError, match="different artifacts"):
        generator.merge_records(
            {
                "controller": {"common": record(generator, "common", version="1.0")},
                "zero": {"common": record(generator, "common", version="1.1")},
            },
            {"controller": ["common"], "zero": ["common"]},
        )


@pytest.mark.skipif(shutil.which("dpkg-deb") is None, reason="dpkg-deb is Linux-only")
def test_create_bundle_emits_schema_verified_for_both_roles(tmp_path: Path) -> None:
    generator = load_generator()
    controller_cache = tmp_path / "controller-cache"
    zero_cache = tmp_path / "zero-cache"
    controller_cache.mkdir()
    zero_cache.mkdir()

    def build_deb(cache: Path, name: str, version: str = "1.0") -> None:
        package_root = tmp_path / f"build-{cache.name}-{name}"
        control = package_root / "DEBIAN" / "control"
        control.parent.mkdir(parents=True)
        control.write_text(
            f"Package: {name}\n"
            f"Version: {version}\n"
            "Architecture: armhf\n"
            "Maintainer: TideTracker Tests <nobody@example.invalid>\n"
            "Description: package bundle test fixture\n",
            encoding="utf-8",
        )
        payload = package_root / "usr" / "share" / name / "fixture"
        payload.parent.mkdir(parents=True)
        payload.write_text(name, encoding="utf-8")
        filename = f"{name}_{version}_armhf.deb"
        subprocess.run(
            ["dpkg-deb", "--build", str(package_root), str(cache / filename)],
            check=True,
            capture_output=True,
            text=True,
        )

    for cache in (controller_cache, zero_cache):
        build_deb(cache, "common", "1.0~deb12u1")
    build_deb(controller_cache, "controller-only")
    build_deb(zero_cache, "zero-only")
    common = tmp_path / "common.txt"
    controller = tmp_path / "controller.txt"
    zero = tmp_path / "zero.txt"
    common.write_text("common\n", encoding="utf-8")
    controller.write_text("controller-only\n", encoding="utf-8")
    zero.write_text("zero-only\n", encoding="utf-8")
    output = tmp_path / "bundle"
    output.mkdir()

    generator.create_bundle(
        output,
        controller_cache,
        zero_cache,
        common,
        controller,
        zero,
    )

    verifier = Path(__file__).parents[1] / "deploy" / "verify-package-bundle.py"
    digests = []
    for role, role_list in (("controller", controller), ("zero", zero)):
        result = subprocess.run(
            [
                sys.executable,
                str(verifier),
                "--bundle",
                str(output),
                "--role",
                role,
                "--package-list",
                str(common),
                "--package-list",
                str(role_list),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        digests.append(result.stdout.strip())

    assert digests[0] == digests[1]
    assert len(digests[0]) == 64


def test_prepare_script_materializes_only_kernel_published_loop_nodes() -> None:
    root = Path(__file__).parents[1]
    script = (root / "deploy" / "prepare-package-bundle.sh").read_text(encoding="utf-8")
    assert 'partition_sysfs="/sys/class/block/loop${loop_number}p${partition_number}"' in script
    assert 'cat "$partition_sysfs/partition"' in script
    assert 'cat "$partition_sysfs/dev"' in script
    assert 'created_partition_nodes="$created_partition_nodes $partition_node"' in script
    assert 'rm -f -- "$partition_node"' in script
    assert 'test ! -e "/sys/class/block/loop${loop_number}p3"' in script
