#!/bin/sh
set -eu

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
archive=
output=
role=
workstation_controller_ssh_key=
controller_zero_ssh_key=
release_key=
lab_safety_key=
image_release_key=
package_bundle=
application_source=$(CDPATH='' cd -- "$script_dir/.." && pwd)
data_mib=1024
apply=false
execute=false
confirmation=

usage() {
    cat <<'EOF'
usage: sudo build-role-image.sh --archive BASE.img.xz --output ROLE.img \
  --role controller|zero --workstation-to-controller-ssh-public-key FILE \
  --controller-to-zero-ssh-public-key FILE --application-release-public-key FILE \
  --lab-safety-public-key FILE --image-release-public-key FILE \
  --package-bundle DIR \
  [--application-source DIR] [--data-mib 1024] \
  [--apply --execute --confirmation BUILD-controller|BUILD-zero]

Default operation is a non-mutating plan. Apply+execute creates a new image only;
existing output files are never overwritten.
EOF
}

while test "$#" -gt 0; do
    case "$1" in
        --archive) archive=${2:?}; shift 2 ;;
        --output) output=${2:?}; shift 2 ;;
        --role) role=${2:?}; shift 2 ;;
        --workstation-to-controller-ssh-public-key) workstation_controller_ssh_key=${2:?}; shift 2 ;;
        --controller-to-zero-ssh-public-key) controller_zero_ssh_key=${2:?}; shift 2 ;;
        --application-release-public-key) release_key=${2:?}; shift 2 ;;
        --lab-safety-public-key) lab_safety_key=${2:?}; shift 2 ;;
        --image-release-public-key) image_release_key=${2:?}; shift 2 ;;
        --package-bundle) package_bundle=${2:?}; shift 2 ;;
        --application-source) application_source=${2:?}; shift 2 ;;
        --data-mib) data_mib=${2:?}; shift 2 ;;
        --apply) apply=true; shift ;;
        --execute) execute=true; shift ;;
        --confirmation) confirmation=${2:?}; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 64 ;;
    esac
done

case "$role" in controller|zero) ;; *) echo '--role must be controller or zero' >&2; exit 64 ;; esac
case "$data_mib" in ''|*[!0-9]*) echo '--data-mib must be an integer' >&2; exit 64 ;; esac
test "$data_mib" -ge 512 && test "$data_mib" -le 8192 || { echo '--data-mib must be 512..8192' >&2; exit 64; }
test -f "$archive" && test -s "$archive" || { echo 'archive is missing or empty' >&2; exit 1; }
test -f "$workstation_controller_ssh_key" && test -s "$workstation_controller_ssh_key" || {
    echo 'workstation-to-controller SSH public key is missing or empty' >&2; exit 1;
}
test -f "$controller_zero_ssh_key" && test -s "$controller_zero_ssh_key" || {
    echo 'controller-to-Zero SSH public key is missing or empty' >&2; exit 1;
}
test -f "$release_key" && test -s "$release_key" || { echo 'release public key is missing or empty' >&2; exit 1; }
test -f "$lab_safety_key" && test -s "$lab_safety_key" || { echo 'lab safety public key is missing or empty' >&2; exit 1; }
test -f "$image_release_key" && test -s "$image_release_key" || { echo 'image release public key is missing or empty' >&2; exit 1; }
test -d "$package_bundle" || { echo 'offline package bundle is missing' >&2; exit 1; }
test -d "$application_source/src/tidetracker" && test -f "$application_source/pyproject.toml" || {
    echo 'application source must contain pyproject.toml and src/tidetracker' >&2; exit 1;
}
test -n "$output" || { echo '--output is required' >&2; exit 64; }

for command_name in dpkg-deb find python3 readlink sha256sum; do
    command -v "$command_name" >/dev/null 2>&1 || {
        echo "missing required command: $command_name" >&2; exit 1;
    }
done

archive=$(readlink -f "$archive")
workstation_controller_ssh_key=$(readlink -f "$workstation_controller_ssh_key")
controller_zero_ssh_key=$(readlink -f "$controller_zero_ssh_key")
release_key=$(readlink -f "$release_key")
lab_safety_key=$(readlink -f "$lab_safety_key")
image_release_key=$(readlink -f "$image_release_key")
package_bundle=$(readlink -f "$package_bundle")
application_source=$(readlink -f "$application_source")
output_dir=$(readlink -f "$(dirname -- "$output")")
output="$output_dir/$(basename -- "$output")"
for final_output in "$output" "$output.sha256" "$output.manifest.json" \
    "$output.partial" "$output.sha256.partial" "$output.manifest.json.partial"; do
    test ! -e "$final_output" || { echo "refusing to overwrite $final_output" >&2; exit 1; }
done

key_policy_json=$(PYTHONPATH="$application_source/src" python3 -m tidetracker.key_policy \
    --workstation-to-controller-ssh-public-key "$workstation_controller_ssh_key" \
    --controller-to-zero-ssh-public-key "$controller_zero_ssh_key" \
    --application-release-public-key "$release_key" \
    --lab-safety-public-key "$lab_safety_key" \
    --image-release-public-key "$image_release_key")
key_policy_hash=$(printf '%s' "$key_policy_json" | python3 -c \
    'import json,sys; print(json.load(sys.stdin)["key_policy_sha256"])')

application_hash=$(python3 "$script_dir/hash-application.py" --root "$application_source")
package_bundle_hash=$(
    python3 "$script_dir/verify-package-bundle.py" --bundle "$package_bundle" --role "$role" \
        --package-list "$script_dir/packages/common.txt" \
        --package-list "$script_dir/packages/$role.txt"
)

expected_hash=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["archive"]["sha256"])' "$script_dir/image-lock.json")
actual_hash=$(sha256sum "$archive" | awk '{print $1}')
test "$actual_hash" = "$expected_hash" || { echo "archive SHA-256 mismatch" >&2; exit 1; }
image_build_id=$(python3 - "$role" "$actual_hash" "$application_hash" \
    "$package_bundle_hash" "$data_mib" "$key_policy_hash" <<'PY'
import hashlib
import json
import sys

keys = (
    "role",
    "base_archive_sha256",
    "application_sha256",
    "package_bundle_sha256",
    "data_mib",
    "key_policy_sha256",
)
values = list(sys.argv[1:])
values[-2] = int(values[-2])
record = {"schema_version": 2, **dict(zip(keys, values, strict=True))}
payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("ascii")
print(hashlib.sha256(payload).hexdigest())
PY
)

cat <<EOF
TideTracker role-image build plan
  role:       $role
  archive:    $archive
  archive ID: $actual_hash
  output:     $output
  TIDEDATA:   ${data_mib} MiB ext4 partition
  workstation-to-controller SSH key: $workstation_controller_ssh_key
  controller-to-Zero SSH key: $controller_zero_ssh_key
  application release key: $release_key
  lab safety key: $lab_safety_key
  image release key: $image_release_key
  canonical key policy: $key_policy_hash
  application: $application_source ($application_hash)
  offline packages: $package_bundle ($package_bundle_hash)
  image build ID: $image_build_id
EOF
if test "$apply" != true || test "$execute" != true; then
    echo "DRY RUN: no output was created. Re-run as root with --apply --execute --confirmation BUILD-$role."
    exit 0
fi

test "$(id -u)" -eq 0 || { echo '--apply requires root' >&2; exit 1; }
test "$confirmation" = "BUILD-$role" || { echo "confirmation must be BUILD-$role" >&2; exit 1; }
for command_name in chroot cp findmnt losetup mknod mkfs.ext4 mount mountpoint sfdisk truncate \
    udevadm umount xz; do
    command -v "$command_name" >/dev/null 2>&1 || { echo "missing required command: $command_name" >&2; exit 1; }
done

working="$output.partial"
test ! -e "$working" || { echo "remove stale partial file first: $working" >&2; exit 1; }
mount_root=$(mktemp -d)
mount_boot=$(mktemp -d)
mount_data=$(mktemp -d)
loop_device=
created_partition_nodes=
package_snapshot=
input_snapshot=

cleanup() {
    set +e
    cleanup_safe=true
    for mount_path in "$mount_data" "$mount_boot" "$mount_root"; do
        if mountpoint -q "$mount_path"; then
            umount "$mount_path" || cleanup_safe=false
        fi
    done
    for mount_path in "$mount_data" "$mount_boot" "$mount_root"; do
        mountpoint -q "$mount_path" && cleanup_safe=false
    done
    if test "$cleanup_safe" = true; then
        for created_node in $created_partition_nodes; do
            rm -f -- "$created_node" || cleanup_safe=false
        done
    fi
    if test "$cleanup_safe" = true && test -n "$loop_device"; then
        losetup -d "$loop_device" || cleanup_safe=false
    fi
    if test "$cleanup_safe" = true; then
        test ! -e "$working" || rm -f -- "$working"
        test -z "$input_snapshot" || rm -rf -- "$input_snapshot"
        rmdir "$mount_data" "$mount_boot" "$mount_root" 2>/dev/null || true
    else
        echo "cleanup left the role-image workspace intact for manual recovery: $input_snapshot" >&2
    fi
}
trap cleanup EXIT INT TERM HUP

# Freeze every approved input before creating the output. All subsequent reads
# use these private snapshots, so a concurrent source/key/archive change cannot
# produce an image whose bytes diverge from the displayed build plan.
input_snapshot=$(mktemp -d "$output_dir/.tidetracker-inputs.XXXXXX")
chmod 0700 "$input_snapshot"
archive_snapshot="$input_snapshot/base.img.xz"
workstation_controller_ssh_key_snapshot="$input_snapshot/workstation-controller-ssh.pub"
controller_zero_ssh_key_snapshot="$input_snapshot/controller-zero-ssh.pub"
release_key_snapshot="$input_snapshot/release-approver.pub.pem"
lab_safety_key_snapshot="$input_snapshot/lab-safety-approver.pub.pem"
image_release_key_snapshot="$input_snapshot/image-release-approver.pub.pem"
application_snapshot="$input_snapshot/application"
package_snapshot="$input_snapshot/package-bundle"
cp --reflink=auto --preserve=mode,timestamps -- "$archive" "$archive_snapshot"
install -m 0600 "$workstation_controller_ssh_key" "$workstation_controller_ssh_key_snapshot"
install -m 0600 "$controller_zero_ssh_key" "$controller_zero_ssh_key_snapshot"
install -m 0600 "$release_key" "$release_key_snapshot"
install -m 0600 "$lab_safety_key" "$lab_safety_key_snapshot"
install -m 0600 "$image_release_key" "$image_release_key_snapshot"
test "$(sha256sum "$archive_snapshot" | awk '{print $1}')" = "$actual_hash" || {
    echo 'base archive changed after the build plan was approved' >&2; exit 1;
}

mkdir -m 0700 "$application_snapshot"
application_file_list="$input_snapshot/application-files"
python3 "$script_dir/hash-application.py" --root "$application_source" --print-files \
    > "$application_file_list"
while IFS= read -r application_item; do
    source_file="$application_source/$application_item"
    destination_file="$application_snapshot/$application_item"
    mkdir -p "$(dirname -- "$destination_file")"
    cp --reflink=auto --preserve=mode,timestamps -- "$source_file" "$destination_file"
done < "$application_file_list"
test "$(python3 "$script_dir/hash-application.py" --root "$application_snapshot")" = \
    "$application_hash" || {
    echo 'application or image-recipe inputs changed after build approval' >&2; exit 1;
}
snapshot_key_policy_json=$(PYTHONPATH="$application_snapshot/src" python3 -m tidetracker.key_policy \
    --workstation-to-controller-ssh-public-key "$workstation_controller_ssh_key_snapshot" \
    --controller-to-zero-ssh-public-key "$controller_zero_ssh_key_snapshot" \
    --application-release-public-key "$release_key_snapshot" \
    --lab-safety-public-key "$lab_safety_key_snapshot" \
    --image-release-public-key "$image_release_key_snapshot")
test "$snapshot_key_policy_json" = "$key_policy_json" || {
    echo 'key policy inputs changed after the build plan was approved' >&2; exit 1;
}
key_policy_file="$input_snapshot/key-policy.json"
printf '%s\n' "$snapshot_key_policy_json" > "$key_policy_file"
chmod 0600 "$key_policy_file"

mkdir -m 0700 "$package_snapshot"
cp -a -- "$package_bundle/." "$package_snapshot/"
snapshot_package_hash=$(
    python3 "$script_dir/verify-package-bundle.py" --bundle "$package_snapshot" --role "$role" \
        --package-list "$script_dir/packages/common.txt" \
        --package-list "$script_dir/packages/$role.txt"
)
test "$snapshot_package_hash" = "$package_bundle_hash" || {
    echo 'offline package bundle changed after the build plan was approved' >&2; exit 1;
}
archive=$archive_snapshot
workstation_controller_ssh_key=$workstation_controller_ssh_key_snapshot
controller_zero_ssh_key=$controller_zero_ssh_key_snapshot
release_key=$release_key_snapshot
lab_safety_key=$lab_safety_key_snapshot
image_release_key=$image_release_key_snapshot
application_source=$application_snapshot
package_bundle=$package_snapshot
script_dir="$application_source/deploy"

# Noclobber makes creation of the private image partial exclusive even if a
# path appears after the earlier plan-time refusal check.
set -C
xz --decompress --keep --stdout -- "$archive" > "$working"
set +C
base_layout="$input_snapshot/base-layout.json"
sfdisk --json "$working" > "$base_layout"
partition_plan=$(python3 - "$working" "$data_mib" "$base_layout" <<'PY'
import json
import os
import sys

image, data_mib, layout = sys.argv[1], int(sys.argv[2]), sys.argv[3]
with open(layout, encoding="utf-8") as stream:
    table = json.load(stream).get("partitiontable")
if not isinstance(table, dict) or table.get("label") != "dos":
    raise SystemExit("base image must use an MBR/DOS partition table")
if table.get("sectorsize") != 512:
    raise SystemExit("base image must use 512-byte logical sectors")
partitions = table.get("partitions")
if not isinstance(partitions, list) or len(partitions) != 2:
    raise SystemExit("base image must contain exactly two partitions")
ends = []
for partition in partitions:
    if not isinstance(partition, dict):
        raise SystemExit("base image partition record is invalid")
    start, size = partition.get("start"), partition.get("size")
    if not isinstance(start, int) or not isinstance(size, int) or start < 1 or size < 1:
        raise SystemExit("base image partition geometry is invalid")
    ends.append(start + size)
file_sectors = (os.path.getsize(image) + 511) // 512
first_free = max(max(ends), file_sectors)
p3_start = ((first_free + 2047) // 2048) * 2048
p3_sectors = data_mib * 2048
print(p3_start, p3_sectors, (p3_start + p3_sectors) * 512)
PY
)
# The trusted Python helper emits exactly three space-delimited decimal fields.
# shellcheck disable=SC2086
set -- $partition_plan
test "$#" -eq 3 || { echo 'could not derive exact TIDEDATA geometry' >&2; exit 1; }
p3_start=$1
p3_sectors=$2
image_bytes=$3
truncate -s "$image_bytes" "$working"
printf '%s,%s,L\n' "$p3_start" "$p3_sectors" | sfdisk --append "$working" >/dev/null
final_layout="$input_snapshot/final-layout.json"
sfdisk --json "$working" > "$final_layout"
python3 - "$working" "$p3_start" "$p3_sectors" "$final_layout" <<'PY'
import json
import os
import sys

image, expected_start, expected_size, layout = (
    sys.argv[1],
    int(sys.argv[2]),
    int(sys.argv[3]),
    sys.argv[4],
)
with open(layout, encoding="utf-8") as stream:
    table = json.load(stream)["partitiontable"]
partitions = table["partitions"]
if len(partitions) != 3:
    raise SystemExit("role image must contain exactly three partitions")
third = partitions[2]
if third.get("start") != expected_start or third.get("size") != expected_size:
    raise SystemExit("TIDEDATA partition does not match the exact signed geometry")
if os.path.getsize(image) != (expected_start + expected_size) * 512:
    raise SystemExit("TIDEDATA partition must end at the exact image EOF")
PY
loop_device=$(losetup --find --show --partscan "$working")
udevadm settle
loop_base=$(basename -- "$loop_device")
loop_number=${loop_base#loop}
case "$loop_base" in loop*) ;; *) echo 'losetup returned an invalid loop name' >&2; exit 1 ;; esac
case "$loop_number" in ''|*[!0-9]*) echo 'losetup returned an invalid loop name' >&2; exit 1 ;; esac
materialize_partition_node() {
    partition_number=$1
    partition_name="${loop_base}p${partition_number}"
    partition_node="/dev/$partition_name"
    test ! -e "$partition_node" || {
        test -b "$partition_node" || {
            echo "refusing non-block partition path: $partition_node" >&2; exit 1;
        }
        return
    }
    sysfs_partition="/sys/class/block/$partition_name"
    test -e "$sysfs_partition" || {
        echo "kernel did not expose expected partition: $partition_name" >&2; exit 1;
    }
    resolved_partition=$(readlink -f "$sysfs_partition")
    test "$(basename -- "$resolved_partition")" = "$partition_name" \
        && test "$(basename -- "$(dirname -- "$resolved_partition")")" = "$loop_base" \
        && test "$(cat "$sysfs_partition/partition")" = "$partition_number" || {
        echo "sysfs partition identity is invalid: $partition_name" >&2; exit 1;
    }
    device_numbers=$(cat "$sysfs_partition/dev")
    case "$device_numbers" in
        *:*:*) echo "invalid sysfs device numbers: $partition_name" >&2; exit 1 ;;
        *:*) ;;
        *) echo "invalid sysfs device numbers: $partition_name" >&2; exit 1 ;;
    esac
    major_number=${device_numbers%%:*}
    minor_number=${device_numbers#*:}
    case "$major_number" in ''|*[!0-9]*)
        echo "invalid sysfs device numbers: $partition_name" >&2; exit 1 ;;
    esac
    case "$minor_number" in ''|*[!0-9]*)
        echo "invalid sysfs device numbers: $partition_name" >&2; exit 1 ;;
    esac
    mknod -m 0600 "$partition_node" b "$major_number" "$minor_number"
    test -b "$partition_node" || {
        echo "could not materialize partition block node: $partition_node" >&2; exit 1;
    }
    created_partition_nodes="$created_partition_nodes $partition_node"
}
for partition_number in 1 2 3; do
    materialize_partition_node "$partition_number"
done

if test "$role" = zero; then
    data_uuid=54540000-0000-4000-8000-000000000002
else
    data_uuid=54540000-0000-4000-8000-000000000001
fi
E2FSPROGS_FAKE_TIME=1781740800 mkfs.ext4 -q -F -L TIDEDATA -U "$data_uuid" \
    -E lazy_itable_init=0,lazy_journal_init=0 "${loop_device}p3"
mount "${loop_device}p2" "$mount_root"
mount "${loop_device}p1" "$mount_boot"
mount "${loop_device}p3" "$mount_data"

sh "$script_dir/provision-profile.sh" --role "$role" --root-mount "$mount_root" \
    --boot-mount "$mount_boot" --data-mount "$mount_data" \
    --workstation-to-controller-ssh-public-key "$workstation_controller_ssh_key" \
    --controller-to-zero-ssh-public-key "$controller_zero_ssh_key" \
    --application-release-public-key "$release_key" --application-source "$application_source" \
    --lab-safety-public-key "$lab_safety_key" \
    --image-release-public-key "$image_release_key" \
    --key-policy-sha256 "$key_policy_hash" \
    --image-build-id "$image_build_id" \
    --package-bundle "$package_bundle" \
    --loop-device "$loop_device" \
    --apply --execute --confirmation "PROVISION-$role"

sync
umount "$mount_data"
umount "$mount_boot"
umount "$mount_root"
for created_node in $created_partition_nodes; do
    rm -f -- "$created_node"
done
created_partition_nodes=
losetup -d "$loop_device"
loop_device=
image_hash=$(sha256sum "$working" | awk '{print $1}')
PYTHONPATH="$application_source/src" python3 - "$working" "$output" "$image_hash" \
    "$role" "$actual_hash" "$image_build_id" "$application_hash" \
    "$package_bundle_hash" "$data_mib" "$key_policy_file" <<'PY'
import json
import os
import pathlib
import sys

from tidetracker.key_policy import POLICY_FIELDS, validate_key_policy

(
    working_text,
    output_text,
    image_hash,
    role,
    base_hash,
    build_id,
    application_hash,
    package_hash,
    data_mib_text,
    policy_text,
) = sys.argv[1:]
working = pathlib.Path(working_text)
output = pathlib.Path(output_text)
policy = validate_key_policy(json.loads(pathlib.Path(policy_text).read_text(encoding="utf-8")))
manifest = {
    "schema_version": 2,
    "role": role,
    "base_archive_sha256": base_hash,
    "image_sha256": image_hash,
    "image_build_id": build_id,
    "application_sha256": application_hash,
    "package_bundle_sha256": package_hash,
    "data_mib": int(data_mib_text),
    "key_policy_sha256": policy["key_policy_sha256"],
}
manifest.update(
    {
        field: policy[field]
        for field in POLICY_FIELDS
        if field not in {"schema_version", "key_policy_sha256"}
    }
)
payloads = {
    pathlib.Path(f"{output}.sha256"): (
        f"{image_hash}  {output.name}\n".encode("ascii")
    ),
    pathlib.Path(f"{output}.manifest.json"): (
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
    ),
}
partials = {path: pathlib.Path(f"{path}.partial") for path in payloads}
published: list[pathlib.Path] = []


def fsync_directory(path: pathlib.Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


try:
    for final in (output, *payloads):
        if os.path.lexists(final):
            raise FileExistsError(f"refusing to overwrite {final}")
    for final, payload in payloads.items():
        partial = partials[final]
        descriptor = os.open(
            partial,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("short sidecar write")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    image_descriptor = os.open(working, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(image_descriptor)
    finally:
        os.close(image_descriptor)
    os.link(working, output)
    published.append(output)
    for final, partial in partials.items():
        os.link(partial, final)
        published.append(final)
    fsync_directory(output.parent)
    os.unlink(working)
    for partial in partials.values():
        os.unlink(partial)
    fsync_directory(output.parent)
except BaseException:
    for final in reversed(published):
        try:
            os.unlink(final)
        except FileNotFoundError:
            pass
    for partial in partials.values():
        try:
            os.unlink(partial)
        except FileNotFoundError:
            pass
    fsync_directory(output.parent)
    raise
PY
echo "Built and hashed $output"
