#!/bin/sh
set -eu

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
archive=
output=
role=
ssh_key=
release_key=
lab_safety_key=
package_bundle=
application_source=$(CDPATH='' cd -- "$script_dir/.." && pwd)
data_mib=1024
apply=false
execute=false
confirmation=

usage() {
    cat <<'EOF'
usage: sudo build-role-image.sh --archive BASE.img.xz --output ROLE.img \
  --role controller|zero --ssh-public-key FILE --release-public-key FILE \
  --lab-safety-public-key FILE --package-bundle DIR \
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
        --ssh-public-key) ssh_key=${2:?}; shift 2 ;;
        --release-public-key) release_key=${2:?}; shift 2 ;;
        --lab-safety-public-key) lab_safety_key=${2:?}; shift 2 ;;
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
test -f "$ssh_key" && test -s "$ssh_key" || { echo 'SSH public key is missing or empty' >&2; exit 1; }
test -f "$release_key" && test -s "$release_key" || { echo 'release public key is missing or empty' >&2; exit 1; }
test -f "$lab_safety_key" && test -s "$lab_safety_key" || { echo 'lab safety public key is missing or empty' >&2; exit 1; }
test -d "$package_bundle" || { echo 'offline package bundle is missing' >&2; exit 1; }
test -d "$application_source/src/tidetracker" && test -f "$application_source/pyproject.toml" || {
    echo 'application source must contain pyproject.toml and src/tidetracker' >&2; exit 1;
}
test -n "$output" || { echo '--output is required' >&2; exit 64; }

for command_name in dpkg-deb find openssl python3 readlink sha256sum ssh-keygen; do
    command -v "$command_name" >/dev/null 2>&1 || {
        echo "missing required command: $command_name" >&2; exit 1;
    }
done
ssh-keygen -l -f "$ssh_key" >/dev/null 2>&1 || {
    echo 'SSH public key is invalid' >&2; exit 1;
}
test "$(awk 'NR == 1 { print $1 }' "$ssh_key")" = ssh-ed25519 || {
    echo 'SSH public key must be Ed25519' >&2; exit 1;
}

archive=$(readlink -f "$archive")
ssh_key=$(readlink -f "$ssh_key")
release_key=$(readlink -f "$release_key")
lab_safety_key=$(readlink -f "$lab_safety_key")
package_bundle=$(readlink -f "$package_bundle")
application_source=$(readlink -f "$application_source")
output_dir=$(readlink -f "$(dirname -- "$output")")
output="$output_dir/$(basename -- "$output")"
test ! -e "$output" || { echo "refusing to overwrite $output" >&2; exit 1; }

command -v openssl >/dev/null 2>&1 || { echo 'openssl is required to validate the release public key' >&2; exit 1; }
openssl pkey -pubin -in "$release_key" -text -noout 2>/dev/null | grep -q ED25519 || {
    echo 'release public key must be an Ed25519 public PEM key' >&2; exit 1;
}
openssl pkey -pubin -in "$lab_safety_key" -text -noout 2>/dev/null | grep -q ED25519 || {
    echo 'lab safety public key must be an Ed25519 public PEM key' >&2; exit 1;
}
ssh_key_hash=$(
    ssh-keygen -e -m PKCS8 -f "$ssh_key" \
        | openssl pkey -pubin -outform DER 2>/dev/null | sha256sum | awk '{print $1}'
)
ssh_key_file_hash=$(sha256sum "$ssh_key" | awk '{print $1}')
release_canonical_hash=$(
    openssl pkey -pubin -in "$release_key" -outform DER 2>/dev/null | sha256sum | awk '{print $1}'
)
lab_safety_canonical_hash=$(
    openssl pkey -pubin -in "$lab_safety_key" -outform DER 2>/dev/null | sha256sum | awk '{print $1}'
)
if test "$ssh_key_hash" = "$release_canonical_hash" \
    || test "$ssh_key_hash" = "$lab_safety_canonical_hash" \
    || test "$release_canonical_hash" = "$lab_safety_canonical_hash"; then
    echo 'SSH, release, and lab-safety trust keys must all be separate' >&2
    exit 1
fi

application_hash=$(python3 "$script_dir/hash-application.py" --root "$application_source")
release_key_hash=$(sha256sum "$release_key" | awk '{print $1}')
lab_safety_key_hash=$(sha256sum "$lab_safety_key" | awk '{print $1}')
package_bundle_hash=$(
    python3 "$script_dir/verify-package-bundle.py" --bundle "$package_bundle" --role "$role" \
        --package-list "$script_dir/packages/common.txt" \
        --package-list "$script_dir/packages/$role.txt"
)

expected_hash=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["archive"]["sha256"])' "$script_dir/image-lock.json")
actual_hash=$(sha256sum "$archive" | awk '{print $1}')
test "$actual_hash" = "$expected_hash" || { echo "archive SHA-256 mismatch" >&2; exit 1; }
image_build_id=$(python3 - "$role" "$actual_hash" "$application_hash" "$ssh_key_file_hash" "$release_key_hash" \
    "$lab_safety_key_hash" "$package_bundle_hash" "$data_mib" <<'PY'
import hashlib
import json
import sys

keys = (
    "role",
    "base_archive_sha256",
    "application_sha256",
    "ssh_authorized_key_sha256",
    "release_public_key_sha256",
    "lab_safety_public_key_sha256",
    "package_bundle_sha256",
    "data_mib",
)
values = list(sys.argv[1:])
values[-1] = int(values[-1])
record = {"schema_version": 1, **dict(zip(keys, values, strict=True))}
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
  SSH key:    $ssh_key
  release key: $release_key ($release_key_hash)
  lab safety key: $lab_safety_key ($lab_safety_key_hash)
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
for command_name in chroot cp findmnt losetup mkfs.ext4 mount mountpoint sfdisk truncate \
    udevadm umount xz; do
    command -v "$command_name" >/dev/null 2>&1 || { echo "missing required command: $command_name" >&2; exit 1; }
done

working="$output.partial"
test ! -e "$working" || { echo "remove stale partial file first: $working" >&2; exit 1; }
mount_root=$(mktemp -d)
mount_boot=$(mktemp -d)
mount_data=$(mktemp -d)
loop_device=
package_snapshot=
input_snapshot=

cleanup() {
    set +e
    mountpoint -q "$mount_data" && umount "$mount_data"
    mountpoint -q "$mount_boot" && umount "$mount_boot"
    mountpoint -q "$mount_root" && umount "$mount_root"
    test -z "$loop_device" || losetup -d "$loop_device"
    test -z "$input_snapshot" || rm -rf -- "$input_snapshot"
    rmdir "$mount_data" "$mount_boot" "$mount_root" 2>/dev/null || true
}
trap cleanup EXIT INT TERM HUP

# Freeze every approved input before creating the output. All subsequent reads
# use these private snapshots, so a concurrent source/key/archive change cannot
# produce an image whose bytes diverge from the displayed build plan.
input_snapshot=$(mktemp -d "$output_dir/.tidetracker-inputs.XXXXXX")
chmod 0700 "$input_snapshot"
archive_snapshot="$input_snapshot/base.img.xz"
ssh_key_snapshot="$input_snapshot/provision-authorized-key.pub"
release_key_snapshot="$input_snapshot/release-approver.pub.pem"
lab_safety_key_snapshot="$input_snapshot/lab-safety-approver.pub.pem"
application_snapshot="$input_snapshot/application"
package_snapshot="$input_snapshot/package-bundle"
cp --reflink=auto --preserve=mode,timestamps -- "$archive" "$archive_snapshot"
install -m 0600 "$ssh_key" "$ssh_key_snapshot"
install -m 0600 "$release_key" "$release_key_snapshot"
install -m 0600 "$lab_safety_key" "$lab_safety_key_snapshot"
test "$(sha256sum "$archive_snapshot" | awk '{print $1}')" = "$actual_hash" || {
    echo 'base archive changed after the build plan was approved' >&2; exit 1;
}
test "$(sha256sum "$ssh_key_snapshot" | awk '{print $1}')" = "$ssh_key_file_hash" || {
    echo 'SSH key changed after the build plan was approved' >&2; exit 1;
}
test "$(sha256sum "$release_key_snapshot" | awk '{print $1}')" = "$release_key_hash" || {
    echo 'release key changed after the build plan was approved' >&2; exit 1;
}
test "$(sha256sum "$lab_safety_key_snapshot" | awk '{print $1}')" = "$lab_safety_key_hash" || {
    echo 'lab-safety key changed after the build plan was approved' >&2; exit 1;
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
ssh_key=$ssh_key_snapshot
release_key=$release_key_snapshot
lab_safety_key=$lab_safety_key_snapshot
application_source=$application_snapshot
package_bundle=$package_snapshot
script_dir="$application_source/deploy"

xz --decompress --keep --stdout -- "$archive" > "$working"
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
test -b "${loop_device}p1" && test -b "${loop_device}p2" && test -b "${loop_device}p3"

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
    --boot-mount "$mount_boot" --data-mount "$mount_data" --ssh-public-key "$ssh_key" \
    --release-public-key "$release_key" --application-source "$application_source" \
    --lab-safety-public-key "$lab_safety_key" \
    --image-build-id "$image_build_id" \
    --package-bundle "$package_bundle" \
    --loop-device "$loop_device" \
    --apply --execute --confirmation "PROVISION-$role"

sync
umount "$mount_data"
umount "$mount_boot"
umount "$mount_root"
losetup -d "$loop_device"
loop_device=
mv "$working" "$output"
image_hash=$(sha256sum "$output" | awk '{print $1}')
printf '%s  %s\n' "$image_hash" "$(basename -- "$output")" > "$output.sha256"
printf '{"schema_version":1,"role":"%s","base_archive_sha256":"%s","image_sha256":"%s","image_build_id":"%s","application_sha256":"%s","ssh_authorized_key_sha256":"%s","release_public_key_sha256":"%s","lab_safety_public_key_sha256":"%s","package_bundle_sha256":"%s","data_mib":%s}\n' \
    "$role" "$actual_hash" "$image_hash" "$image_build_id" "$application_hash" "$ssh_key_file_hash" "$release_key_hash" \
    "$lab_safety_key_hash" "$package_bundle_hash" "$data_mib" > "$output.manifest.json"
echo "Built and hashed $output"
