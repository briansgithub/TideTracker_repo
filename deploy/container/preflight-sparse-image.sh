#!/bin/sh
set -eu

work_dir=

usage() {
    cat <<'EOF'
usage: tidetracker-builder-preflight --work-dir DIRECTORY

Exercise loop allocation, an MBR partition table, FAT/ext4 formatting, and
three mounts using only a disposable sparse regular file created below the
given directory. The temporary image and mounts are removed before success.
EOF
}

while test "$#" -gt 0; do
    case "$1" in
        --work-dir) work_dir=${2:?}; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 64 ;;
    esac
done

test "$(id -u)" -eq 0 || {
    echo 'preflight must run as root in a disposable --privileged container' >&2
    exit 1
}
test -n "$work_dir" || { echo '--work-dir is required' >&2; exit 64; }
test ! -L "$work_dir" && test -d "$work_dir" && test -w "$work_dir" || {
    echo 'work directory must be an existing writable non-symlink directory' >&2
    exit 1
}
work_dir=$(readlink -f "$work_dir")
case "$work_dir" in
    /|/bin|/boot|/dev|/etc|/home|/proc|/root|/run|/sbin|/sys|/usr|/var)
        echo "refusing broad or system work directory: $work_dir" >&2
        exit 1
        ;;
esac

for command_name in cat findmnt id losetup mkdir mkfs.ext4 mkfs.vfat mktemp mknod mount \
    mountpoint readlink rm rmdir sed sfdisk truncate udevadm umount; do
    command -v "$command_name" >/dev/null 2>&1 || {
        echo "missing required command: $command_name" >&2
        exit 1
    }
done

# Fail before creating anything when the container has no usable loop device.
losetup --find >/dev/null 2>&1 || {
    echo 'no loop device is available; run this image with Docker --privileged' >&2
    exit 1
}

scratch=$(mktemp -d "$work_dir/tidetracker-loop-preflight.XXXXXX")
case "$scratch" in
    "$work_dir"/tidetracker-loop-preflight.*) ;;
    *) echo 'mktemp returned a path outside the approved work directory' >&2; exit 1 ;;
esac
image=$scratch/disposable.img
mount_boot=$scratch/boot
mount_root=$scratch/root
mount_data=$scratch/data
loop_device=
created_partition_nodes=

cleanup() {
    cleanup_status=0
    for mount_path in "$mount_data" "$mount_root" "$mount_boot"; do
        if mountpoint -q "$mount_path"; then
            umount "$mount_path" || cleanup_status=1
        fi
    done
    for partition_node in $created_partition_nodes; do
        rm -f -- "$partition_node" || cleanup_status=1
    done
    if test -n "$loop_device" && losetup "$loop_device" >/dev/null 2>&1; then
        losetup --detach "$loop_device" || cleanup_status=1
    fi
    if test -n "$loop_device" && losetup "$loop_device" >/dev/null 2>&1; then
        cleanup_status=1
    else
        rm -f -- "$image" || cleanup_status=1
        for mount_path in "$mount_data" "$mount_root" "$mount_boot"; do
            if test -d "$mount_path"; then
                rmdir "$mount_path" 2>/dev/null || cleanup_status=1
            fi
        done
        if test -d "$scratch"; then
            rmdir "$scratch" 2>/dev/null || cleanup_status=1
        fi
    fi
    return "$cleanup_status"
}
trap 'cleanup' 0
trap 'exit 129' 1
trap 'exit 130' 2
trap 'exit 143' 15
mkdir "$mount_boot" "$mount_root" "$mount_data"

# 256 MiB sparse regular file, with three exact non-overlapping partitions.
truncate -s 268435456 "$image"
test -f "$image" && test ! -b "$image" || {
    echo 'refusing to partition anything except the created regular file' >&2
    exit 1
}
sfdisk --wipe always "$image" >/dev/null <<'EOF'
label: dos
unit: sectors

start=2048, size=131072, type=c, bootable
start=133120, size=196608, type=83
start=329728, size=194560, type=83
EOF

loop_device=$(losetup --find --show --partscan -- "$image")
case "$loop_device" in /dev/loop*) ;; *) echo 'unexpected loop path' >&2; exit 1 ;; esac
loop_number=${loop_device#/dev/loop}
case "$loop_number" in ''|*[!0-9]*) echo 'unexpected loop path' >&2; exit 1 ;; esac
udevadm settle --timeout=10

backing_file=$(losetup --noheadings --output BACK-FILE "$loop_device" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
test "$(readlink -f "$backing_file")" = "$(readlink -f "$image")" || {
    echo 'allocated loop device is not backed by the disposable image' >&2
    exit 1
}

# Docker Desktop exposes the kernel's loop partition mappings but does not run
# udev in this disposable container, so create only the missing /dev nodes from
# the kernel-published major/minor values. Native Linux builders normally take
# the existing-node path and do not create anything here.
for partition_number in 1 2 3; do
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
    test -b "$partition_node" || {
        echo "partition device did not appear: $partition_node" >&2
        exit 1
    }
done

mkfs.vfat -F 32 -n PREBOOT "${loop_device}p1" >/dev/null
mkfs.ext4 -q -F -L PREROOT -E lazy_itable_init=0,lazy_journal_init=0 "${loop_device}p2"
mkfs.ext4 -q -F -L TIDEDATA -E lazy_itable_init=0,lazy_journal_init=0 "${loop_device}p3"
mount -o nodev,nosuid "${loop_device}p1" "$mount_boot"
mount -o nodev,nosuid "${loop_device}p2" "$mount_root"
mount -o nodev,nosuid "${loop_device}p3" "$mount_data"

test "$(readlink -f "$(findmnt -n -o SOURCE --target "$mount_boot")")" = \
    "$(readlink -f "${loop_device}p1")"
test "$(readlink -f "$(findmnt -n -o SOURCE --target "$mount_root")")" = \
    "$(readlink -f "${loop_device}p2")"
test "$(readlink -f "$(findmnt -n -o SOURCE --target "$mount_data")")" = \
    "$(readlink -f "${loop_device}p3")"

printf '%s\n' preflight-boot > "$mount_boot/preflight-marker"
printf '%s\n' preflight-root > "$mount_root/preflight-marker"
printf '%s\n' preflight-data > "$mount_data/preflight-marker"
test "$(cat "$mount_boot/preflight-marker")" = preflight-boot
test "$(cat "$mount_root/preflight-marker")" = preflight-root
test "$(cat "$mount_data/preflight-marker")" = preflight-data

cleanup
trap - 0 1 2 15
test ! -e "$scratch" || { echo 'preflight cleanup left a temporary path' >&2; exit 1; }
echo 'PASS: disposable loop, partition, filesystem, mount, and cleanup preflight'
