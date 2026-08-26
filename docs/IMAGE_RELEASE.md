# Reproducible TideTracker cards

`deploy/image-lock.json` pins Raspberry Pi OS Legacy Lite 32-bit Bookworm and
archive SHA-256
`8a044f4c55feb9b0626ab2060a2eef15c3f57327dd610a0a4cac02cdb959166e`.
Changing it is an OS upgrade. The builder never refreshes APT or downloads a
package.

## Inputs and image build

Prepare these outside Git:

- one OpenSSH Ed25519 public key for key-only SSH;
- separate Ed25519 PEM public keys for signed application releases and lab
  approvals (the three canonical keys must differ);
- a reviewed offline ARMHF package closure. It contains
  `packages.lock.json` and `debs/`; the lock follows
  `deploy/packages/PACKAGE_BUNDLE.example.json` and records every `.deb` name,
  exact version, architecture, filename, size, and SHA-256. Each schema-2 role
  records both the exact repository request list and its reviewed dependency
  closure; the global package records must equal the union of those two role
  closures. Dependencies not already in the pinned base must also be included.
  The controller role request list includes `hostapd`, `dnsmasq-base`, and
  `nftables` for the isolated portal test plane; regenerate the schema-2 lock
  after any role-list change and never copy an older closure forward.

The image builder verifies all package bytes and Debian control metadata before
mutation. It stages and installs only the selected role's closure under a
network namespace with no interfaces, checks the resulting dpkg database
against that role, and hashes the entire reviewed bundle into the image
manifest. A missing, extra, or cross-role-only package is a hard stop; the
repository intentionally does not fetch one during a release build.

1. Inspect `deploy/prepare-base-image.ps1`; use `-Apply -Execute` only after the
   download/decompression action is approved.
2. On a Linux/ARM or binfmt-enabled build host, inspect the role build:

```text
sudo sh deploy/build-role-image.sh --archive <base.img.xz> --output <role.img> \
  --role controller|zero --ssh-public-key <ssh.pub> \
  --release-public-key <release.pub.pem> \
  --lab-safety-public-key <approval.pub.pem> \
  --package-bundle <reviewed-offline-bundle>
```

3. After review, add `--apply --execute --confirmation BUILD-<role>`. The
   builder creates a partial copy, proves boot/root/data are p1/p2/p3 of its
   exact loop device, creates a role-specific fixed-UUID `TIDEDATA`, installs
   the runtime/assets/Waveshare driver/services, and emits `.sha256` plus
   `.manifest.json`. The deterministic image build ID and signed manifest bind
   the exact SSH authorized-key file hash as well as every other build input.
4. Sign the exact role manifest bytes outside the image with the accepted
   image-release Ed25519 private key; store the raw signature as
   `<role.img>.manifest.json.sig`. Keep every private key off both Pis and out of
   Git.

Build one immutable Zero image artifact. Flash that exact hashed/signed artifact
independently to the development and pristine field cards; do not rebuild a
nominally equivalent field image and do not clone a used card. First boot creates
unique machine IDs, random seeds, and SSH host keys per card.

## Guarded flashing

Running `deploy/flash-sd.ps1` without `-DiskNumber` only lists candidates. A
selected-card dry run requires PowerShell 7.2 or newer and all of:

- `-ExpectedRole controller|zero`;
- the exact raw reader/card serial shown by inspection;
- `-ExpectedImageSha256` copied from the separately accepted release record
  (the adjacent manifest is never its own trust anchor);
- the accepted image-release public key and its separately recorded SHA-256.
- an explicit `-OpenSslPath` and the separately accepted executable digest in
  `-ExpectedOpenSslSha256`; release verification never searches `PATH`.

The flasher requires the exact schema-1 manifest field set, recomputes its
deterministic image build ID, and accepts only an integer `data_mib` from 512
through 8192. It verifies the signed role manifest, role, image hash,
package-bundle hash, minimum image size, MBR signature, exact p1/p2/p3 partition
types, non-overlap, an empty p4 entry, and a p3 sector count and image boundary
that exactly match signed `data_mib`. Duplicate JSON properties, including case
variants, are rejected.
The manifest, signature, and accepted key are copied into private immutable
verification snapshots. The image and the explicitly selected OpenSSL binary
are opened with write/delete sharing denied; those exact handles remain locked
while their digests and signature are checked. The image handle is retained
through the raw write and full byte-for-byte readback, so a path replacement can
never substitute different bytes between acceptance and flashing.
The source path must resolve to exactly one local volume and partition whose
backing disk differs from the selected target. Mount-point and volume-GUID paths
are resolved through the storage API; UNC sources are rejected because their
backing disk cannot be proven locally. Virtual, pooled, and unknown-backing
source disks are rejected for the same reason.

The flasher snapshots disk number, raw serial, UniqueId, size, and bus, then
re-queries every value immediately before and after raw access. Its printed
confirmation token hashes that full disk identity, role, image digest,
signed-manifest and manifest-signature digests, signing-key digest, and accepted
OpenSSL digest. Only elevated `-Apply -Execute -Confirmation <printed-token>`
writes. Full byte-for-byte post-write verification cannot be disabled; the disk
remains offline unless `-OnlineAfterFlash` was requested and the write plus
readback both succeeded. A failed or partial flash is always left offline.

Any actual flash is destructive hardware work: stop and obtain approval for the
specific disk and printed operation before proceeding.

## First boot and immutable layout

First boot validates the role-specific `TIDEDATA` UUID, creates the
`tidetracker` account with an impossible non-locking password sentinel, installs
the authorized key, generates machine/SSH identities, and validates `sshd -t`.
The SSH policy requires public key authentication, disables password/keyboard
and root login, and allows only `tidetracker`. The Zero SSH user has no gpio/spi
group access; production hardware runs in the hardened root service. The
controller user receives only `dialout` and `video` for fixture/UART/UVC access.

NetworkManager uses enabled `systemd-resolved`, the stub `resolv.conf`, and an
explicit resolver backend. Root and boot become read-only/overlay-backed after
the first-boot reboot. Configuration, NetworkManager profiles, update state,
results, and telemetry live on `TIDEDATA`.

Overlay activation has one commit point: `/etc/overlayroot.conf` remains
explicitly disabled (`overlayroot=""`), while the final durable replacement of
the boot command line adds `overlayroot=tmpfs:recurse=0`. A cut after any prior
fstab/configuration write therefore reboots into the writable lower root and
retries. The validation boot writes the role-bound `provisioned` marker to
`TIDEDATA`. A marker-plus-pending restart performs the final durable cleanup;
later boots skip the provisioning body using that persistent state.

Configure the controller AP only from Ethernet with a root-owned mode-0600 PSK
file and the guarded `deploy/configure-controller-network.sh`. It creates NM
connection ID `tidetracker-lab-ap` (SSID `TideTracker-Lab`). The image also
contains a non-autoconnect, never-default/no-DNS open client profile for the
Zero setup AP (`tidetracker-zero-portal`, SSID `TideTracker-Setup`).

## Release record

Record the base hash, signed role-image manifest/signature/key hashes, exact
image hash, SSH authorized-key hash, offline package-bundle hash, signed
application bundle hash/release ID, Git commit, fixture firmware, card/reader
identity, legacy replay report, and HIL sessions. A field card is promotable
only after the final cold cycle with USB physically disconnected. Keep the old
Bullseye card and previous verified application release untouched as rollback
paths.

Each completed real-HIL `session.json` is immutable promotion input. After the
controller closes the session, sign its exact bytes with the separately held lab
approval key and place the raw Ed25519 signature beside it as
`session.json.sig`. Do not sign an incomplete, simulated, failed, or manually
edited session. The promotion evaluator uses the fixed `/usr/bin/openssl`, an
explicitly allowlisted executable SHA-256, the pinned
`/etc/tidetracker/lab-safety-approver.pub.pem`, and private snapshots to verify
and parse the same bytes. Duplicate JSON properties fail closed.
Record the Windows flasher OpenSSL digest and Pi 3 evaluator OpenSSL digest as
separate host-specific trust inputs. They identify different executables and
must not be substituted for one another or folded into the portable image build
ID.

Promotion also requires the signed Zero role-image manifest and its separately
accepted image-release public key/hash. Supply the externally recorded manifest
digest, image digest, and image build ID; all candidate benchmark and auxiliary
HIL sessions must name that exact identity. The signed evidence set must cover
all lifecycle and update power cuts, the full Wi-Fi and portal profile matrix,
twelve accelerated cold cycles, accepted camera comparisons, and the final
USB-disconnected field cycle. The evaluator re-hashes each referenced camera
image and UART log rather than trusting mutable summary text. Every signed
session must bind the same live fixture and firmware, target host-key record,
Pi 3 machine-ID hash, controller `TIDEDATA` policy, and accepted image identity.
Promotion evaluates controller-observed Wi-Fi association duration as its
Wi-Fi-active metric while retaining fixture power-on-to-DONE as a conservative
power upper bound.
