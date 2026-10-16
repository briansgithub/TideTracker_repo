# Reproducible TideTracker cards

`deploy/image-lock.json` pins Raspberry Pi OS Legacy Lite 32-bit Bookworm and
archive SHA-256
`8a044f4c55feb9b0626ab2060a2eef15c3f57327dd610a0a4cac02cdb959166e`.
Changing it is an OS upgrade. The role-image builder never refreshes APT or
downloads a package. Package retrieval is a separate, explicitly approved
preparation phase; release image builds consume only its frozen output.

## Inputs and image build

Prepare these outside Git:

- two distinct OpenSSH Ed25519 keypairs: workstation-to-controller management
  and controller-to-Zero automation. Pass both public keys to every role build;
  the role chooses its authorized key while the manifest binds both identities;
- separate Ed25519 PEM public keys for application releases, lab approvals, and
  role-image releases. All five canonical public keys must differ;
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

### Create the guarded Windows keystore

`deploy/prepare-keystore.ps1` creates all five identities before either role
image is built. Its destination is fixed at
`%LOCALAPPDATA%\TideTracker\keystore\v1`; it rejects drive `D:`, the repository,
reparse points, and any existing `v1`. The default invocation is a write-free
plan:

```powershell
$Repo = 'B:\Desktop\W\Programming_write\Python_workspaces\TideTracker_repo'
& "$Repo\deploy\prepare-keystore.ps1"
```

Separately inspect and record the exact Windows OpenSSL and `ssh-keygen`
executables and their SHA-256 digests. Creation requires all three authorization
switches plus those previously accepted identities; do not calculate and accept
the digests inline in the creation command:

```powershell
$OpenSsl = 'C:\Program Files\Git\usr\bin\openssl.exe'
$OpenSslSha256 = '<separately-reviewed-64-hex>'
$SshKeygen = 'C:\Windows\System32\OpenSSH\ssh-keygen.exe'
$SshKeygenSha256 = '<separately-reviewed-64-hex>'

& "$Repo\deploy\prepare-keystore.ps1" `
  -OpenSslPath $OpenSsl -ExpectedOpenSslSha256 $OpenSslSha256 `
  -SshKeygenPath $SshKeygen -ExpectedSshKeygenSha256 $SshKeygenSha256 `
  -Apply -Execute -Confirmation CREATE-TIDETRACKER-KEYSTORE-v1
```

The script locks both executable files against changes, creates a restricted
temporary sibling before generating secrets, and grants only the current
Windows SID and `SYSTEM` full control. It validates every private/public pair,
requires five distinct canonical Ed25519 identities, and atomically renames the
completed tree to `v1` without replacement. A failed run cleans only its exact
validated temporary sibling.

The three signing keys and both SSH keys are intentionally unencrypted for the
closed-loop automation workflow; the Windows account ACL is therefore the
private-key trust boundary. The output uses these public filenames:

```text
public/
  workstation-to-controller-ssh-ed25519.pub
  controller-to-zero-ssh-ed25519.pub
  application-release-ed25519.public.pem
  lab-safety-approval-ed25519.public.pem
  image-release-ed25519.public.pem
records/
  key-policy.json
  public-keystore-record.json
```

`public-keystore-record.json` contains only public fingerprints/digests and
tool provenance; it contains no private bytes, paths, or hashes. Record the
printed key-policy digest separately. Never copy or mount the `private/` tree
into Git, a container, a build-input directory, or either role image. Installing
the controller-to-Zero private half on an already booted controller is a later,
separately approved controller mutation.

### Prepare the offline package bundle

Run package preparation on a privileged Linux/ARM host, or on a Linux host with
working ARMHF binfmt. The command verifies the compressed archive against
`deploy/image-lock.json` before doing anything. Its default mode is read-only
and makes no network request:

```text
sudo sh deploy/prepare-package-bundle.sh \
  --archive <2026-06-18-raspios-bookworm-armhf-lite.img.xz> \
  --output <new-package-bundle>
```

After reviewing that plan, enable the one networked preparation phase:

```text
sudo sh deploy/prepare-package-bundle.sh \
  --archive <2026-06-18-raspios-bookworm-armhf-lite.img.xz> \
  --output <new-package-bundle> \
  --apply --execute --confirmation PREPARE-PACKAGE-BUNDLE
```

The preparer works only on a private decompressed image copy. It uses that
image's signed APT sources, refuses insecure trust overrides, disables
recommended packages, keeps controller and Zero solver caches separate, and
proves APT did not alter the private dpkg status database. It then emits only
`packages.lock.json` and `debs/`, atomically publishes the new directory, and
runs `deploy/verify-package-bundle.py` for both roles. Existing output or stale
partial directories are hard stops.

APT repositories can advance after preparation. The reviewed, content-addressed
bundle is the immutable release input; rerunning live resolution at a later date
is not a substitute for retaining the accepted bundle. Review package names and
versions in the generated schema-2 lock before accepting its printed digest.

The image builder verifies all package bytes and Debian control metadata before
mutation. It stages and installs only the selected role's closure under a
network namespace with no interfaces, checks the resulting dpkg database
against that role, and hashes the entire reviewed bundle into the image
manifest. A missing, extra, or cross-role-only package is a hard stop; the
repository intentionally does not fetch one during a release build.

1. Create and accept the guarded `v1` keystore above.
2. Inspect `deploy/prepare-base-image.ps1`; use `-Apply -Execute` only after the
   download/decompression action is approved.
3. On a Linux/ARM or binfmt-enabled build host, inspect the role build:

```text
sudo sh deploy/build-role-image.sh --archive <base.img.xz> --output <role.img> \
  --role controller|zero \
  --workstation-to-controller-ssh-public-key <workstation-to-controller-ssh-ed25519.pub> \
  --controller-to-zero-ssh-public-key <controller-to-zero-ssh-ed25519.pub> \
  --application-release-public-key <application-release-ed25519.public.pem> \
  --lab-safety-public-key <lab-safety-approval-ed25519.public.pem> \
  --image-release-public-key <image-release-ed25519.public.pem> \
  --package-bundle <reviewed-offline-bundle>
```

4. After review, add `--apply --execute --confirmation BUILD-<role>`. The
   builder creates a partial copy, proves boot/root/data are p1/p2/p3 of its
   exact loop device, creates a role-specific fixed-UUID `TIDEDATA`, installs
   the runtime/assets/Waveshare driver/services, and emits `.sha256` plus
   `.manifest.json`. All three final paths and their partial paths must be new;
   the builder refuses any pre-existing one, fsyncs the bytes, and publishes
   each output without replacement. The schema-2 deterministic image build ID binds the
   canonical five-key policy digest; the manifest records both raw and
   canonical identities for both SSH keys and all three signing authorities.
5. Sign the exact role manifest bytes outside the image with the accepted
   image-release Ed25519 private key; store the raw signature as
   `<role.img>.manifest.json.sig`. The builder uses its public half only to
   reject canonical key reuse and does not install it into the image.

Signing private keys never belong on either Pi: keep the application-release,
lab-approval, and image-release private keys on the trusted workstation or
offline signer and out of Git. The one intentional private-key exception is not
a signing authority: the dedicated controller-to-Zero SSH automation key lives
only on controller `TIDEDATA` as
`/var/lib/tidetracker/controller/ssh/controller-to-zero-ed25519`, owned by root
with mode `0600`. The workstation-to-controller SSH private key remains on the
workstation. Never copy either SSH private key to the Zero, and never reuse an
SSH key as an application, lab, or image signing key.

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
- both exact SSH public keys plus the application-release, lab-safety, and
  image-release public keys whose raw and canonical hashes appear in the signed
  manifest;
- an explicit `-OpenSslPath` and the separately accepted executable digest in
  `-ExpectedOpenSslSha256`; release verification never searches `PATH`.

The flasher requires the exact schema-2 manifest field set, explicitly rejects
schema 1, recomputes its
deterministic image build ID, and accepts only an integer `data_mib` from 512
through 8192. It verifies the signed role manifest, role, image hash,
package-bundle hash, minimum image size, MBR signature, exact p1/p2/p3 partition
types, non-overlap, an empty p4 entry, and a p3 sector count and image boundary
that exactly match signed `data_mib`. It also binds all five supplied public-key
files to their signed raw and canonical hashes,
and recomputes the aggregate policy digest from all five supplied public keys.
Any canonical key reuse is rejected. Duplicate JSON properties, including case
variants, are rejected.
The manifest, signature, and accepted keys are copied into private immutable
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
On the controller it also creates an empty root-owned mode-`0700`
`TIDEDATA/controller/ssh` directory; provisioning never creates or embeds its
private automation key.

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
image hash, both SSH public-key hashes, the five-key policy digest, offline package-bundle hash, signed
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

Promotion also requires the signed Zero role-image manifest and all five
separately accepted public-key files. Supply the externally recorded manifest
digest, image digest, image build ID, and matching policy; all candidate benchmark and auxiliary
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
