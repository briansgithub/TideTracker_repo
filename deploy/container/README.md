# Disposable Linux image builder

This directory defines the privileged Linux tool environment used to prepare
an offline ARMHF package bundle and to build TideTracker role images. It does
not flash cards. The commands below bind only locations on `B:`; do not add a
bind for `D:`, a physical-drive path, or a Docker `--device` option.

The base is the architecture-specific `linux/amd64` manifest of Debian 12.11
slim, pinned in `Dockerfile` by dated tag and digest. Native tools are installed
from Debian snapshots dated 2025-08-11. A change to the base digest, snapshot,
or installed tool set is a reviewed builder upgrade.

## Host folders

Use PowerShell to define four distinct host locations. The input tree contains
only the pinned compressed base image and public keys. Private signing keys do
not enter this container workflow.

```powershell
$Repo = 'B:\Desktop\W\Programming_write\Python_workspaces\TideTracker_repo'
$Inputs = 'B:\Desktop\W\Programming_write\Python_workspaces\TideTracker_build\inputs'
$PackageOutput = 'B:\Desktop\W\Programming_write\Python_workspaces\TideTracker_build\packages'
$ImageOutput = 'B:\Desktop\W\Programming_write\Python_workspaces\TideTracker_build\images'
$KeystorePublic = Join-Path $env:LOCALAPPDATA 'TideTracker\keystore\v1\public'

@($Repo, $Inputs, $PackageOutput, $ImageOutput) | ForEach-Object {
    if ([IO.Path]::GetPathRoot($_) -ne 'B:\') { throw "Builder path is not on B: $_" }
}
if ([IO.Path]::GetPathRoot($KeystorePublic) -eq 'D:\' -or `
    -not (Test-Path -LiteralPath $KeystorePublic -PathType Container)) {
    throw 'The accepted guarded-keystore public directory is unavailable.'
}
```

Create new `$Inputs`, `$PackageOutput`, and `$ImageOutput` directories before
running a container. Copy only the five public files out of the guarded
keystore, refusing any existing destination:

```powershell
$PublicInputs = @{
  'workstation-to-controller-ssh-ed25519.pub' = 'workstation-to-controller-ssh-ed25519.pub'
  'controller-to-zero-ssh-ed25519.pub' = 'controller-to-zero-ssh-ed25519.pub'
  'application-release-ed25519.public.pem' = 'application-release-ed25519.public.pem'
  'lab-safety-approval-ed25519.public.pem' = 'lab-safety-approval-ed25519.public.pem'
  'image-release-ed25519.public.pem' = 'image-release-ed25519.public.pem'
}
foreach ($name in $PublicInputs.Keys) {
  $source = Join-Path $KeystorePublic $PublicInputs[$name]
  $destination = Join-Path $Inputs $name
  if (-not (Test-Path -LiteralPath $source -PathType Leaf) -or `
      (Test-Path -LiteralPath $destination)) {
    throw "Missing source or existing destination: $name"
  }
  Copy-Item -LiteralPath $source -Destination $destination
}
```

Do not copy or mount the sibling `private/` directory. Expected read-only input
names in the examples are:

```text
inputs/
  2026-06-18-raspios-bookworm-armhf-lite.img.xz
  workstation-to-controller-ssh-ed25519.pub
  controller-to-zero-ssh-ed25519.pub
  application-release-ed25519.public.pem
  lab-safety-approval-ed25519.public.pem
  image-release-ed25519.public.pem
```

Every role build receives both public SSH keys. The controller selects the
workstation-to-controller key for login, while the Zero selects the distinct
controller-to-Zero automation key. Both identities remain bound into the same
five-key policy; never reuse either SSH key as a signing key.

## Build the pinned tool container

This one-time environment bootstrap needs network access to retrieve the exact
pinned Debian base and snapshot packages. No role image or package bundle is
created by this step, and it is not a release build.

```powershell
$BuilderImageTag = 'tidetracker-image-builder:bookworm-20250811'

docker build --platform linux/amd64 --network default --pull `
  --file "$Repo\deploy\container\Dockerfile" `
  --tag $BuilderImageTag `
  "$Repo"

$BuilderImageId = docker image inspect $BuilderImageTag --format '{{.Id}}'
if ($LASTEXITCODE -ne 0 -or $BuilderImageId -notmatch '^sha256:[0-9a-f]{64}$') {
    throw 'Could not capture one immutable builder image ID.'
}
$BuilderImageId
```

Record and review `$BuilderImageId`. Every command below runs that immutable
image ID, never the mutable tag. Do not silently rebuild the tool image during
a release; start a new PowerShell session only after recapturing and accepting
the ID.

## Disposable loop and mount preflight

Run this before package preparation or a role build. It has no network and
binds only the writable image-output directory. The script creates a sparse
regular file under `/output`, partitions and formats that file through its own
loop device, mounts all three filesystems, and verifies complete cleanup. It
never accepts a disk or device path.

```powershell
docker run --rm --privileged --platform linux/amd64 --network none `
  --mount "type=bind,source=$ImageOutput,target=/output" `
  $BuilderImageId `
  tidetracker-builder-preflight --work-dir /output
```

Stop if the final `PASS` line is absent or if a
`tidetracker-loop-preflight.*` directory remains in `$ImageOutput`.

## Prepare the offline package bundle

The plan command is offline. The repository and base-image/key inputs are
read-only; only the package-output directory is writable.

```powershell
docker run --rm --privileged --platform linux/amd64 --network none `
  --mount "type=bind,source=$Repo,target=/workspace,readonly" `
  --mount "type=bind,source=$Inputs,target=/inputs,readonly" `
  --mount "type=bind,source=$PackageOutput,target=/output" `
  $BuilderImageId `
  sh /workspace/deploy/prepare-package-bundle.sh `
    --archive /inputs/2026-06-18-raspios-bookworm-armhf-lite.img.xz `
    --output /output/package-bundle
```

After accepting that plan, package preparation is the only network-enabled
release-workflow command. It enables ARMHF binfmt inside the disposable
privileged container, then uses the pinned image's signed APT configuration.

```powershell
docker run --rm --privileged --platform linux/amd64 --network bridge `
  --mount "type=bind,source=$Repo,target=/workspace,readonly" `
  --mount "type=bind,source=$Inputs,target=/inputs,readonly" `
  --mount "type=bind,source=$PackageOutput,target=/output" `
  $BuilderImageId `
  sh -ceu 'mountpoint -q /proc/sys/fs/binfmt_misc || mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc; update-binfmts --enable qemu-arm; exec sh /workspace/deploy/prepare-package-bundle.sh --archive /inputs/2026-06-18-raspios-bookworm-armhf-lite.img.xz --output /output/package-bundle --apply --execute --confirmation PREPARE-PACKAGE-BUNDLE'
```

Review the generated `packages.lock.json`, retain every `.deb`, and record the
printed package-bundle digest. Subsequent role builds consume this directory
read-only and have no network.

## Build a role image offline

First inspect a role plan. This command uses the same privileged capabilities
as the accepted build so that environment differences cannot hide a preflight
failure, but it has no network and does not create an image.

```powershell
$Role = 'controller' # or 'zero'

docker run --rm --privileged --platform linux/amd64 --network none `
  --mount "type=bind,source=$Repo,target=/workspace,readonly" `
  --mount "type=bind,source=$Inputs,target=/inputs,readonly" `
  --mount "type=bind,source=$PackageOutput\package-bundle,target=/package-bundle,readonly" `
  --mount "type=bind,source=$ImageOutput,target=/output" `
  $BuilderImageId `
  sh /workspace/deploy/build-role-image.sh `
    --archive /inputs/2026-06-18-raspios-bookworm-armhf-lite.img.xz `
    --output "/output/$Role.img" --role $Role `
    --workstation-to-controller-ssh-public-key /inputs/workstation-to-controller-ssh-ed25519.pub `
    --controller-to-zero-ssh-public-key /inputs/controller-to-zero-ssh-ed25519.pub `
    --application-release-public-key /inputs/application-release-ed25519.public.pem `
    --lab-safety-public-key /inputs/lab-safety-approval-ed25519.public.pem `
    --image-release-public-key /inputs/image-release-ed25519.public.pem `
    --package-bundle /package-bundle `
    --application-source /workspace
```

After reviewing the plan, run the actual build. ARMHF execution support is
enabled in this same disposable container before the builder starts. The
container remains completely offline.

```powershell
docker run --rm --privileged --platform linux/amd64 --network none `
  --env "TIDETRACKER_ROLE=$Role" `
  --mount "type=bind,source=$Repo,target=/workspace,readonly" `
  --mount "type=bind,source=$Inputs,target=/inputs,readonly" `
  --mount "type=bind,source=$PackageOutput\package-bundle,target=/package-bundle,readonly" `
  --mount "type=bind,source=$ImageOutput,target=/output" `
  $BuilderImageId `
  sh -ceu 'mountpoint -q /proc/sys/fs/binfmt_misc || mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc; update-binfmts --enable qemu-arm; exec sh /workspace/deploy/build-role-image.sh --archive /inputs/2026-06-18-raspios-bookworm-armhf-lite.img.xz --output "/output/$TIDETRACKER_ROLE.img" --role "$TIDETRACKER_ROLE" --workstation-to-controller-ssh-public-key /inputs/workstation-to-controller-ssh-ed25519.pub --controller-to-zero-ssh-public-key /inputs/controller-to-zero-ssh-ed25519.pub --application-release-public-key /inputs/application-release-ed25519.public.pem --lab-safety-public-key /inputs/lab-safety-approval-ed25519.public.pem --image-release-public-key /inputs/image-release-ed25519.public.pem --package-bundle /package-bundle --application-source /workspace --apply --execute --confirmation "BUILD-$TIDETRACKER_ROLE"'
```

The only writable host bind in a role build is `$ImageOutput`. Do not point it
at a card, reader, mounted removable volume, or existing release directory. The
builder refuses to overwrite an output image, either sidecar, or any stale
partial. It publishes the image, checksum, and schema-2 manifest exclusively
and durably.
