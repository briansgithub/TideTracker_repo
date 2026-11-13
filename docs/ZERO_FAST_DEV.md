# Pi Zero W fast-development loop

This path is intentionally smaller than the field-release workflow. It uses one
writable Raspberry Pi OS Bookworm card, direct USB Ethernet, one development SSH
key, and an automatic simulated cycle at boot. It does not use the Pi 3, signed
releases, a read-only root, TIDEDATA, fixtures, or statistical promotion gates.

## One-time image preparation

1. Keep the SSH private key outside the repository. The default deployment path
   is `%LOCALAPPDATA%\TideTracker\dev\ssh\zero-dev-ed25519`; create one Ed25519
   key there and never put it on the SD image.
2. Run `deploy/build-zero-dev-image.sh` in the repository's privileged image-
   builder container. First omit the apply flags to inspect the plan. Then add
   `--apply --execute --confirmation BUILD-ZERO-DEV` to create a new `.img` on
   workstation storage. The script cannot address a physical disk.
3. Stop and obtain explicit approval before writing that image to a microSD card.
   Image creation and SD-card flashing are deliberately separate operations.

The image exposes the Zero as USB Ethernet at `10.77.0.2/30`; configure the
workstation side as `10.77.0.1/30` with no gateway or DNS. Use the Zero's USB/OTG
connector, not `PWR IN`. The first physical connection is also an approval point.

## Normal edit/deploy loop

From PowerShell on the workstation:

```powershell
& .\deploy\dev\deploy-zero-dev.ps1
```

That command packages only the Python runtime, required display assets, and
Waveshare Python drivers; uploads it over USB; runs a fresh simulated cycle on
the ARMv6 Zero; switches the `current` link atomically; restarts the boot service;
and prints release plus last-cycle JSON. If the restarted service fails, it puts
the previous release back automatically.

Useful direct checks are:

```powershell
ssh -i $env:LOCALAPPDATA\TideTracker\dev\ssh\zero-dev-ed25519 tidetracker@10.77.0.2 /usr/local/bin/tidetracker-dev-status
ssh -i $env:LOCALAPPDATA\TideTracker\dev\ssh\zero-dev-ed25519 tidetracker@10.77.0.2 journalctl --no-pager -u tidetracker-dev.service -n 80
```

The boot service is deliberately simulation-only: it does not touch GPIO, Wi-Fi,
DONE, or the e-ink display. Once this loop is stable, physical execution can be
enabled as a separate, small change and tested with explicit hardware approval.
