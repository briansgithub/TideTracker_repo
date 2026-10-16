#!/bin/sh
set -eu

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
archive=
output=
apply=false
execute=false
confirmation=

usage() {
    cat <<'EOF'
usage: sudo sh deploy/prepare-package-bundle.sh --archive BASE.img.xz \
  --output PACKAGE-BUNDLE \
  [--apply --execute --confirmation PREPARE-PACKAGE-BUNDLE]

Default operation verifies the pinned archive and prints a non-mutating plan.
Apply+execute uses a private image copy and the image's signed APT sources to
download exact ARMHF controller and Zero closures into a new output directory.
Existing output and partial directories are never overwritten.
EOF
}

while test "$#" -gt 0; do
    case "$1" in
        --archive) archive=${2:?}; shift 2 ;;
        --output) output=${2:?}; shift 2 ;;
        --apply) apply=true; shift ;;
        --execute) execute=true; shift ;;
        --confirmation) confirmation=${2:?}; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 64 ;;
    esac
done

test -n "$archive" || { echo '--archive is required' >&2; exit 64; }
test -n "$output" || { echo '--output is required' >&2; exit 64; }
test -f "$archive" && test ! -L "$archive" && test -s "$archive" || {
    echo 'archive must be a non-empty regular non-symlink file' >&2; exit 1;
}
for command_name in awk basename dirname python3 readlink sha256sum stat; do
    command -v "$command_name" >/dev/null 2>&1 || {
        echo "missing required command: $command_name" >&2; exit 1;
    }
done
for package_file in "$script_dir/packages/common.txt" \
    "$script_dir/packages/controller.txt" "$script_dir/packages/zero.txt"; do
    test -f "$package_file" && test ! -L "$package_file" || {
        echo "package list is missing or symlinked: $package_file" >&2; exit 1;
    }
done
test -f "$script_dir/create-package-lock.py" \
    && test -f "$script_dir/verify-package-bundle.py" || {
    echo 'package lock generator or verifier is missing' >&2; exit 1;
}

archive=$(readlink -f "$archive")
output_parent=$(dirname -- "$output")
test -d "$output_parent" && test ! -L "$output_parent" || {
    echo 'output parent must be an existing non-symlink directory' >&2; exit 1;
}
output_parent=$(readlink -f "$output_parent")
output_name=$(basename -- "$output")
case "$output_name" in ''|.|..) echo 'output directory name is invalid' >&2; exit 64 ;; esac
output="$output_parent/$output_name"
test ! -e "$output" && test ! -L "$output" || {
    echo "refusing to overwrite output: $output" >&2; exit 1;
}
partial="$output.partial"
test ! -e "$partial" && test ! -L "$partial" || {
    echo "remove stale partial directory first: $partial" >&2; exit 1;
}

expected_hash=$(python3 -c \
    'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["archive"]["sha256"])' \
    "$script_dir/image-lock.json")
expected_size=$(python3 -c \
    'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["archive"]["size_bytes"])' \
    "$script_dir/image-lock.json")
expected_codename=$(python3 -c \
    'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["codename"])' \
    "$script_dir/image-lock.json")
expected_architecture=$(python3 -c \
    'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["architecture"])' \
    "$script_dir/image-lock.json")
actual_size=$(stat -c %s "$archive")
test "$actual_size" = "$expected_size" || {
    echo "archive size mismatch: expected $expected_size, found $actual_size" >&2; exit 1;
}
actual_hash=$(sha256sum "$archive" | awk '{print $1}')
test "$actual_hash" = "$expected_hash" || {
    echo 'archive SHA-256 does not match deploy/image-lock.json' >&2; exit 1;
}
test "$expected_architecture" = armhf || {
    echo 'deploy/image-lock.json does not select ARMHF' >&2; exit 1;
}

cat <<EOF
TideTracker offline package-bundle preparation plan
  pinned archive: $archive
  archive SHA-256: $actual_hash
  target baseline: $expected_codename/$expected_architecture
  package lists: common + controller; common + zero
  output: $output
  APT policy: signed image sources, no insecure repositories, no recommends
  final validation: schema 2, exact bytes/control metadata, both role closures
EOF
if test "$apply" != true || test "$execute" != true; then
    echo 'DRY RUN: no output was created and no network request was made.'
    echo 'Re-run as root with --apply --execute --confirmation PREPARE-PACKAGE-BUNDLE.'
    exit 0
fi

test "$(id -u)" -eq 0 || { echo '--apply requires root' >&2; exit 1; }
test "$confirmation" = PREPARE-PACKAGE-BUNDLE || {
    echo 'confirmation must be PREPARE-PACKAGE-BUNDLE' >&2; exit 1;
}
for command_name in apt-get awk cat chmod chroot cp dpkg-deb find findmnt grep id losetup \
    mkdir mktemp mknod mount mountpoint mv python3 readlink rm sfdisk sort sync udevadm \
    umount xz; do
    command -v "$command_name" >/dev/null 2>&1 || {
        echo "missing required command: $command_name" >&2; exit 1;
    }
done

work=$(mktemp -d "$output_parent/.tidetracker-package-build.XXXXXX")
chmod 0700 "$work"
partial_created=false
root_mounted=false
proc_mounted=false
lists_mounted=false
cache_mounted=false
loop_device=
created_partition_nodes=
root_mount="$work/root"
lists_dir="$work/apt-lists"
controller_cache="$work/controller-cache"
zero_cache="$work/zero-cache"

cleanup() {
    set +e
    cleanup_safe=true
    if test "$cache_mounted" = true || mountpoint -q "$root_mount/var/cache/apt/archives"; then
        umount "$root_mount/var/cache/apt/archives"
    fi
    if test "$lists_mounted" = true || mountpoint -q "$root_mount/var/lib/apt/lists"; then
        umount "$root_mount/var/lib/apt/lists"
    fi
    if test "$proc_mounted" = true || mountpoint -q "$root_mount/proc"; then
        umount "$root_mount/proc"
    fi
    if test "$root_mounted" = true || mountpoint -q "$root_mount"; then
        umount "$root_mount"
    fi
    for mount_path in "$root_mount/var/cache/apt/archives" \
        "$root_mount/var/lib/apt/lists" "$root_mount/proc" "$root_mount"; do
        if mountpoint -q "$mount_path"; then
            cleanup_safe=false
        fi
    done
    for partition_node in $created_partition_nodes; do
        rm -f -- "$partition_node" || cleanup_safe=false
    done
    if test "$cleanup_safe" = true && test -n "$loop_device"; then
        losetup -d "$loop_device" || cleanup_safe=false
    fi
    if test "$partial_created" = true; then
        rm -rf -- "$partial"
    fi
    if test "$cleanup_safe" = true; then
        rm -rf -- "$work"
    else
        echo "cleanup left private workspace mounted for manual recovery: $work" >&2
    fi
}
trap cleanup EXIT INT TERM HUP

mkdir -m 0700 "$partial"
partial_created=true
mkdir -m 0755 "$root_mount" "$lists_dir" "$controller_cache" "$zero_cache"
archive_snapshot="$work/base.img.xz"
image="$work/base.img"
cp --reflink=auto --preserve=mode,timestamps -- "$archive" "$archive_snapshot"
test "$(stat -c %s "$archive_snapshot")" = "$expected_size" \
    && test "$(sha256sum "$archive_snapshot" | awk '{print $1}')" = "$expected_hash" || {
    echo 'pinned archive changed while it was snapshotted' >&2; exit 1;
}
xz --test -- "$archive_snapshot"
xz --decompress --stdout -- "$archive_snapshot" > "$image"

layout="$work/base-layout.json"
sfdisk --json "$image" > "$layout"
python3 - "$image" "$layout" <<'PY'
import json
import os
import sys

image, layout_path = sys.argv[1:]
with open(layout_path, encoding="utf-8") as stream:
    table = json.load(stream).get("partitiontable", {})
parts = table.get("partitions", [])
if table.get("label") != "dos" or table.get("sectorsize", 512) != 512:
    raise SystemExit("pinned image must have a 512-byte-sector DOS partition table")
if len(parts) != 2:
    raise SystemExit("pinned base image must contain exactly boot p1 and root p2")
types = [str(part.get("type", "")).lower().removeprefix("0x") for part in parts]
if types[0] not in {"c", "0c"} or types[1] != "83":
    raise SystemExit("pinned base image partition types are not FAT32-LBA/root-Linux")
last_end = 0
for part in parts:
    start = part.get("start")
    size = part.get("size")
    if not isinstance(start, int) or not isinstance(size, int) or start < last_end or size < 1:
        raise SystemExit("pinned base image partitions overlap or have invalid geometry")
    last_end = start + size
if last_end * 512 > os.path.getsize(image):
    raise SystemExit("pinned base image partition exceeds image bounds")
PY

loop_device=$(losetup --find --show --partscan "$image")
udevadm settle
loop_number=${loop_device#/dev/loop}
case "$loop_number" in ''|*[!0-9]*) echo 'unexpected loop path' >&2; exit 1 ;; esac
for partition_number in 1 2; do
    partition_node="${loop_device}p${partition_number}"
    if ! test -b "$partition_node"; then
        partition_sysfs="/sys/class/block/loop${loop_number}p${partition_number}"
        test -e "$partition_sysfs" &&
            test "$(cat "$partition_sysfs/partition")" = "$partition_number" &&
            test "$(readlink -f "$partition_sysfs/..")" = \
                "$(readlink -f "/sys/class/block/loop${loop_number}")" || {
            echo "kernel partition mapping did not appear: $partition_sysfs" >&2
            exit 1
        }
        device_numbers=$(cat "$partition_sysfs/dev")
        case "$device_numbers" in
            *:*) device_major=${device_numbers%:*}; device_minor=${device_numbers#*:} ;;
            *) echo "invalid partition device numbers: $device_numbers" >&2; exit 1 ;;
        esac
        case "$device_major:$device_minor" in
            *[!0-9:]*|:*|*:) echo "invalid partition device numbers: $device_numbers" >&2; exit 1 ;;
        esac
        mknod "$partition_node" b "$device_major" "$device_minor"
        created_partition_nodes="$created_partition_nodes $partition_node"
    fi
done
test -b "${loop_device}p1" && test -b "${loop_device}p2" \
    && test ! -e "/sys/class/block/loop${loop_number}p3" || {
    echo 'private base image did not expose exactly p1 and p2' >&2; exit 1;
}
mount -o rw "${loop_device}p2" "$root_mount"
root_mounted=true
root_source=$(readlink -f "$(findmnt -n -o SOURCE --target "$root_mount")")
test "$root_source" = "${loop_device}p2" || {
    echo "private root mount source mismatch: $root_source" >&2; exit 1;
}
test -f "$root_mount/etc/os-release" || { echo 'base root lacks os-release' >&2; exit 1; }
test -x "$root_mount/usr/bin/apt-get" && test -x "$root_mount/usr/bin/dpkg" || {
    echo 'base root lacks apt-get or dpkg' >&2; exit 1;
}
if ! chroot "$root_mount" /bin/true; then
    echo 'cannot execute ARMHF base; use native ARMHF or registered qemu-arm binfmt' >&2
    exit 1
fi
chroot "$root_mount" grep -Eq "^VERSION_CODENAME=\"?$expected_codename\"?$" /etc/os-release || {
    echo "base root is not $expected_codename" >&2; exit 1;
}
test "$(chroot "$root_mount" dpkg --print-architecture)" = armhf || {
    echo 'base dpkg architecture is not armhf' >&2; exit 1;
}
if grep -ERisq \
    'trusted[[:space:]]*=[[:space:]]*yes|Trusted:[[:space:]]*yes|allow-insecure[[:space:]]*=[[:space:]]*yes|AllowUnauthenticated[^;]*true|AllowInsecureRepositories[^;]*true|check-valid-until[[:space:]]*=[[:space:]]*no|Check-Valid-Until:[[:space:]]*no|Check-Valid-Until[^;]*false' \
    "$root_mount/etc/apt/sources.list" "$root_mount/etc/apt/sources.list.d" \
    "$root_mount/etc/apt/apt.conf" "$root_mount/etc/apt/apt.conf.d" 2>/dev/null; then
    echo 'base APT configuration contains an insecure trust override' >&2
    exit 1
fi

# This is a disposable copy. Supplying the host resolver makes signed APT
# repositories reachable without changing the pinned archive.
rm -f -- "$root_mount/etc/resolv.conf"
cp --dereference -- /etc/resolv.conf "$root_mount/etc/resolv.conf"
mount -t proc proc "$root_mount/proc"
proc_mounted=true
mount --bind "$lists_dir" "$root_mount/var/lib/apt/lists"
lists_mounted=true

run_apt() {
    chroot "$root_mount" /usr/bin/env DEBIAN_FRONTEND=noninteractive LC_ALL=C \
        apt-get \
        -o Acquire::AllowInsecureRepositories=false \
        -o Acquire::AllowDowngradeToInsecureRepositories=false \
        -o Acquire::Check-Date=true \
        -o Acquire::Check-Valid-Until=true \
        -o Acquire::Languages=none \
        -o Acquire::Retries=3 \
        -o APT::Get::AllowUnauthenticated=false \
        "$@"
}

status_hash=$(sha256sum "$root_mount/var/lib/dpkg/status" | awk '{print $1}')
run_apt update
run_apt check

download_role() {
    role=$1
    cache=$2
    mount --bind "$cache" "$root_mount/var/cache/apt/archives"
    cache_mounted=true
    requested_file="$work/$role-requested.txt"
    sort -u "$script_dir/packages/common.txt" "$script_dir/packages/$role.txt" \
        | awk 'NF && $1 !~ /^#/ { print $1 }' > "$requested_file"
    set --
    while IFS= read -r package_name; do
        case "$package_name" in ''|*[!a-z0-9.+-]*)
            echo "invalid requested package name: $package_name" >&2; exit 1 ;;
        esac
        set -- "$@" "$package_name"
    done < "$requested_file"
    test "$#" -gt 0 || { echo "$role request list is empty" >&2; exit 1; }
    run_apt --yes --download-only --reinstall --no-install-recommends \
        -o Binary::apt-get::APT::Keep-Downloaded-Packages=true install "$@"
    umount "$root_mount/var/cache/apt/archives"
    cache_mounted=false
    test -n "$(find "$cache" -maxdepth 1 -type f -name '*.deb' -print -quit)" || {
        echo "$role APT closure downloaded no packages" >&2; exit 1;
    }
}

download_role controller "$controller_cache"
download_role zero "$zero_cache"
test "$(sha256sum "$root_mount/var/lib/dpkg/status" | awk '{print $1}')" = "$status_hash" || {
    echo 'APT preparation unexpectedly changed the private dpkg status database' >&2; exit 1;
}

umount "$root_mount/var/lib/apt/lists"
lists_mounted=false
umount "$root_mount/proc"
proc_mounted=false
umount "$root_mount"
root_mounted=false
losetup -d "$loop_device"
loop_device=

python3 "$script_dir/create-package-lock.py" --output "$partial" \
    --controller-cache "$controller_cache" --zero-cache "$zero_cache" \
    --common-list "$script_dir/packages/common.txt" \
    --controller-list "$script_dir/packages/controller.txt" \
    --zero-list "$script_dir/packages/zero.txt"
controller_digest=$(python3 "$script_dir/verify-package-bundle.py" \
    --bundle "$partial" --role controller \
    --package-list "$script_dir/packages/common.txt" \
    --package-list "$script_dir/packages/controller.txt")
zero_digest=$(python3 "$script_dir/verify-package-bundle.py" \
    --bundle "$partial" --role zero \
    --package-list "$script_dir/packages/common.txt" \
    --package-list "$script_dir/packages/zero.txt")
test "$controller_digest" = "$zero_digest" || {
    echo 'role verification produced inconsistent bundle digests' >&2; exit 1;
}

mv -- "$partial" "$output"
partial_created=false
sync -f "$output_parent"
echo "Prepared and verified $output"
echo "Package bundle SHA-256: $controller_digest"
