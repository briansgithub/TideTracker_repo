from __future__ import annotations

from pathlib import Path

import pytest

from tidetracker.firstboot_overlay import (
    DISABLED_OVERLAY_CONFIG,
    OVERLAY_ARGUMENT,
    OverlayConfigurationError,
    configure_overlay_files,
)


class SimulatedPowerCut(RuntimeError):
    pass


def _files(root: Path) -> tuple[Path, Path, Path]:
    fstab = root / "fstab"
    overlay = root / "overlayroot.conf"
    cmdline = root / "cmdline.txt"
    fstab.write_text(
        "proc /proc proc defaults 0 0\n"
        "/dev/mmcblk0p1 /boot/firmware vfat defaults,rw 0 2\n"
        "/dev/mmcblk0p2 / ext4 defaults 0 1\n",
        encoding="utf-8",
    )
    overlay.write_text(DISABLED_OVERLAY_CONFIG, encoding="utf-8")
    cmdline.write_text("console=serial0,115200 rootwait quiet\n", encoding="utf-8")
    for path in (fstab, overlay, cmdline):
        path.chmod(0o644)
    return fstab, overlay, cmdline


@pytest.mark.parametrize("cut_after", ["fstab", "overlay_config", "cmdline"])
def test_every_overlay_activation_write_recovers_idempotently(
    tmp_path: Path,
    cut_after: str,
) -> None:
    fstab, overlay, cmdline = _files(tmp_path)

    def cut(label: str) -> None:
        if label == cut_after:
            raise SimulatedPowerCut(label)

    with pytest.raises(SimulatedPowerCut, match=cut_after):
        configure_overlay_files(fstab, overlay, cmdline, after_write=cut)

    if cut_after != "cmdline":
        assert OVERLAY_ARGUMENT not in cmdline.read_text(encoding="utf-8")
        assert overlay.read_text(encoding="utf-8") == DISABLED_OVERLAY_CONFIG

    configure_overlay_files(fstab, overlay, cmdline)

    assert overlay.read_text(encoding="utf-8") == DISABLED_OVERLAY_CONFIG
    assert cmdline.read_text(encoding="utf-8").split().count(OVERLAY_ARGUMENT) == 1
    boot_row = next(
        line for line in fstab.read_text(encoding="utf-8").splitlines() if "/boot/firmware" in line
    )
    options = boot_row.split()[3].split(",")
    assert options.count("ro") == 1
    assert "rw" not in options
    assert not tuple(tmp_path.glob(".*.tidetracker.tmp"))


def test_overlay_configuration_rejects_an_unexpected_activation_source(tmp_path: Path) -> None:
    fstab, overlay, cmdline = _files(tmp_path)
    cmdline.write_text("overlayroot=local rootwait\n", encoding="utf-8")

    with pytest.raises(OverlayConfigurationError, match="unexpected overlayroot"):
        configure_overlay_files(fstab, overlay, cmdline)

    assert overlay.read_text(encoding="utf-8") == DISABLED_OVERLAY_CONFIG


def test_firstboot_unit_skips_only_after_marker_cleanup_and_has_a_short_kill_bound() -> None:
    repository = Path(__file__).parents[1]
    unit = (repository / "systemd" / "tidetracker-firstboot.service").read_text(
        encoding="utf-8"
    )
    script = (repository / "deploy" / "libexec" / "tidetracker-firstboot").read_text(
        encoding="utf-8"
    )

    assert (
        "test ! -e /var/lib/tidetracker/provisioned || "
        "test -e /var/lib/tidetracker/provisioning-pending"
    ) in unit
    assert "TimeoutStopSec=10" in unit
    assert "KillMode=control-group" in unit
    assert "SendSIGKILL=yes" in unit
    assert "firstboot_overlay.py" in script
    assert "systemctl disable tidetracker-firstboot.service" not in script
    assert "grep -Fx 'overlayroot=\"\"'" in script
