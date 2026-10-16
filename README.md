# TideTracker

TideTracker is a power-bounded NOAA tide display runtime for the original
Raspberry Pi Zero W and Waveshare 7.5-inch V2 e-paper panel. The repository
contains the packaged runtime, transactional Wi-Fi setup portal, signed
application updater, content-addressed offline role-card builder, and a closed-loop Pi 3 →
Pi Zero hardware-in-the-loop controller.

## Lab architecture

- The workstation edits code and runs deterministic tests.
- An Ethernet-managed Pi 3 runs the lab AP, fixture, camera, and HIL controller.
- The Pi Zero W runs production code and every integration/release cycle.
- USB Ethernet (`10.77.0.1/30` ↔ `10.77.0.2/30`) carries management only.
  Production traffic is persisted and verified as `wlan0` traffic.
- The Zero's switched field rail supplies the target side of the USB isolator;
  host VBUS must never reach the Zero.

The application uses the bounded lifecycle `MODE → NETWORK → FETCH → VALIDATE
→ COMPUTE → RENDER → DISPLAY → PERSIST → DONE`. A handled failure still stores
its result and hands DONE to one bounded owner. A systemd failsafe covers
crashes/timeouts. Private RAM-backed intent and completion markers prevent a
second software attempt when a crash makes the physical edge outcome ambiguous.

## Safe starting point

All card, network, fixture, power, camera, and HIL tools default to inspection,
dry-run, or simulation. Real execution requires an explicit `--execute` (or
PowerShell `-Execute`) in addition to the tool's apply/confirmation guard.

Do not run an execute/apply command until the named SD card or hardware action
has been reviewed and explicitly approved. No repository test requires attached
hardware.

Start with:

```text
python -m pytest
python lab/hil_orchestrator.py --simulate --cycles 2 --artifact-dir <new-directory>
```

Card creation and release signing are documented in
[`docs/IMAGE_RELEASE.md`](docs/IMAGE_RELEASE.md). Physical wiring and signed
electrical approval are in [`docs/WIRING_SAFETY.md`](docs/WIRING_SAFETY.md).
The complete lab and promotion sequence is in
[`docs/LAB_RUNBOOK.md`](docs/LAB_RUNBOOK.md).

## Installed field layout

- Runtime: `/usr/local/lib/tidetracker/app`
- Assets: `/usr/share/tidetracker`
- Waveshare driver: `/usr/lib/tidetracker/e-ink_lib`
- Public application-release key: `/etc/tidetracker/release-ed25519.pub.pem`
- Pinned lab approver key: `/etc/tidetracker/lab-safety-approver.pub.pem`
- Public image-release key: `/etc/tidetracker/image-release-ed25519.pub.pem`
- Pinned SSH public identities: `/etc/tidetracker/workstation-controller-ssh.pub`
  and `/etc/tidetracker/controller-zero-ssh.pub`
- Canonical five-key policy: `/etc/tidetracker/key-policy.json` and
  `/etc/tidetracker/key-policy.sha256`
- Controller-only SSH automation key: `/var/lib/tidetracker/controller/ssh/controller-to-zero-ed25519`
  (root-owned mode `0600`; never installed on the Zero)
- Persistent data/update root: `/var/lib/tidetracker` (`TIDEDATA`)
- Non-secret config: `/var/lib/tidetracker/config/config.json`
- Wi-Fi secrets: mode-0600 NetworkManager profiles only

The cycle and portal are hardened root services because binding port 80 and
performing NetworkManager transactions require privilege. The SSH account can
write only the upload area; a narrowly validated root helper stages signed
bundles and exposes fixed read-only result/metrics commands.
