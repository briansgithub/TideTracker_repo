#!/bin/sh
set -eu

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
archive=
output=
ssh_key=
package_bundle=
application_source=$(CDPATH='' cd -- "$script_dir/.." && pwd)
apply=false
execute=false
confirmation=

usage() {
    cat <<'EOF'
usage: sudo build-zero-dev-image.sh --archive BASE.img.xz --output ZERO-DEV.img \
  --ssh-public-key FILE --package-bundle DIR [--application-source DIR] \
  [--apply --execute --confirmation BUILD-ZERO-DEV]

The default is an inspection-only plan. The confirmed operation creates a new
image file on workstation storage; it never opens or writes a physical disk.
EOF
}

while test "$#" -gt 0; do
    case "$1" in
        --archive) archive=${2:?}; shift 2 ;;
        --output) output=${2:?}; shift 2 ;;
        --ssh-public-key) ssh_key=${2:?}; shift 2 ;;
        --package-bundle) package_bundle=${2:?}; shift 2 ;;
        --application-source) application_source=${2:?}; shift 2 ;;
        --apply) apply=true; shift ;;
        --execute) execute=true; shift ;;
        --confirmation) confirmation=${2:?}; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 64 ;;
    esac
done

test -n "$output" || { echo '--output is required' >&2; exit 64; }
test -f "$archive" && test -s "$archive" || { echo 'base archive is missing or empty' >&2; exit 1; }
test -f "$ssh_key" && test -s "$ssh_key" || { echo 'SSH public key is missing or empty' >&2; exit 1; }
test -d "$package_bundle" || { echo 'offline package bundle is missing' >&2; exit 1; }
test -d "$application_source/src/tidetracker" \
    && test -f "$application_source/deploy/dev/dev_bundle.py" || {
    echo 'application source lacks the core package or dev workflow' >&2; exit 1;
}

for command_name in dpkg-deb find python3 readlink sha256sum ssh-keygen; do
    command -v "$command_name" >/dev/null 2>&1 || {
        echo "missing required command: $command_name" >&2; exit 1;
    }
done
ssh-keygen -l -f "$ssh_key" >/dev/null 2>&1 || { echo 'SSH public key is invalid' >&2; exit 1; }
test "$(awk 'NR == 1 { print $1 }' "$ssh_key")" = ssh-ed25519 || {
    echo 'SSH public key must be Ed25519' >&2; exit 1;
}
test "$(wc -l < "$ssh_key" | tr -d ' ')" -eq 1 || {
    echo 'SSH public key file must contain exactly one key' >&2; exit 1;
}

archive=$(readlink -f "$archive")
ssh_key=$(readlink -f "$ssh_key")
package_bundle=$(readlink -f "$package_bundle")
application_source=$(readlink -f "$application_source")
output_dir=$(readlink -f "$(dirname -- "$output")")
output="$output_dir/$(basename -- "$output")"
test ! -e "$output" && test ! -e "$output.sha256" || {
    echo "refusing to overwrite output or checksum: $output" >&2; exit 1;
}

input_snapshot=$(mktemp -d "$output_dir/.zero-dev-inputs.XXXXXX")
chmod 0700 "$input_snapshot"
mount_root=
mount_boot=
loop_device=
created_nodes=
working="$output.partial"
completed=false

cleanup() {
    set +e
    test -z "$mount_boot" || { mountpoint -q "$mount_boot" && umount "$mount_boot"; }
    test -z "$mount_root" || { mountpoint -q "$mount_root" && umount "$mount_root"; }
    test -z "$loop_device" || losetup -d "$loop_device"
    for node in $created_nodes; do
        case "$node" in /dev/loop[0-9]*p[0-9]*) rm -f -- "$node" ;; esac
    done
    test "$completed" = true || rm -f -- "$working"
    test -z "$mount_boot" || rmdir "$mount_boot" 2>/dev/null || true
    test -z "$mount_root" || rmdir "$mount_root" 2>/dev/null || true
    rm -rf -- "$input_snapshot"
}
trap cleanup EXIT INT TERM HUP

cp -a -- "$script_dir/dev" "$input_snapshot/dev"
recipe_hash=$(python3 - "$input_snapshot/dev" <<'PY'
import hashlib
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
digest = hashlib.sha256()
for path in sorted(item for item in root.rglob("*") if item.is_file()):
    name = path.relative_to(root).as_posix().encode("utf-8")
    payload = path.read_bytes()
    digest.update(name + b"\0" + str(len(payload)).encode("ascii") + b"\0")
    digest.update(hashlib.sha256(payload).digest())
print(digest.hexdigest())
PY
)
app_bundle="$input_snapshot/bootstrap.tar"
bundle_json=$(python3 "$input_snapshot/dev/dev_bundle.py" pack \
    --source "$application_source" --output "$app_bundle")
release_id=$(printf '%s' "$bundle_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["release_id"])')
application_hash=$(printf '%s' "$bundle_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["archive_sha256"])')

expected_record=$(python3 - "$script_dir/image-lock.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    archive = json.load(stream)["archive"]
print(archive["filename"], archive["size_bytes"], archive["sha256"])
PY
)
set -- $expected_record
expected_filename=$1
expected_size=$2
expected_hash=$3
test "$(basename -- "$archive")" = "$expected_filename" || {
    echo 'base archive filename does not match image-lock.json' >&2; exit 1;
}
test "$(wc -c < "$archive" | tr -d ' ')" = "$expected_size" || {
    echo 'base archive size does not match image-lock.json' >&2; exit 1;
}
actual_hash=$(sha256sum "$archive" | awk '{print $1}')
test "$actual_hash" = "$expected_hash" || { echo 'base archive SHA-256 mismatch' >&2; exit 1; }

package_bundle_hash=$(python3 "$script_dir/verify-package-bundle.py" \
    --bundle "$package_bundle" --role zero \
    --package-list "$script_dir/packages/common.txt" \
    --package-list "$script_dir/packages/zero.txt")
ssh_key_hash=$(sha256sum "$ssh_key" | awk '{print $1}')
image_build_id=$(python3 - "$actual_hash" "$application_hash" "$ssh_key_hash" \
    "$package_bundle_hash" "$recipe_hash" <<'PY'
import hashlib
import json
import sys

names = (
    "base_archive_sha256",
    "application_bundle_sha256",
    "ssh_authorized_key_sha256",
    "package_bundle_sha256",
    "recipe_sha256",
)
record = {"schema_version": 1, **dict(zip(names, sys.argv[1:], strict=True))}
payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("ascii")
print(hashlib.sha256(payload).hexdigest())
PY
)

cat <<EOF
TideTracker Pi Zero W fast-development image plan
  base archive:     $archive ($actual_hash)
  output image:     $output
  initial release:  $release_id ($application_hash)
  SSH public key:   $ssh_key ($ssh_key_hash)
  offline packages: $package_bundle ($package_bundle_hash)
  recipe:           $recipe_hash
  image build ID:   $image_build_id
  root filesystem:  writable, original two-partition Raspberry Pi OS layout
  USB management:   Zero 10.77.0.2/30, host 10.77.0.1/30
  boot test:        simulated cycle only (no GPIO, Wi-Fi, or display access)
EOF
if test "$apply" != true || test "$execute" != true; then
    echo 'DRY RUN: no image was created. Re-run as root with --apply --execute --confirmation BUILD-ZERO-DEV.'
    exit 0
fi

test "$(id -u)" -eq 0 || { echo '--apply requires root' >&2; exit 1; }
test "$confirmation" = BUILD-ZERO-DEV || {
    echo 'confirmation must be BUILD-ZERO-DEV' >&2; exit 1;
}
for command_name in chroot cp findmnt install losetup mount mountpoint \
    mknod udevadm umount unshare xz; do
    command -v "$command_name" >/dev/null 2>&1 || {
        echo "missing required command: $command_name" >&2; exit 1;
    }
done
test ! -e "$working" || { echo "remove stale partial image first: $working" >&2; exit 1; }

archive_snapshot="$input_snapshot/base.img.xz"
ssh_key_snapshot="$input_snapshot/authorized_key.pub"
package_snapshot="$input_snapshot/package-bundle"
cp --reflink=auto --preserve=mode,timestamps -- "$archive" "$archive_snapshot"
install -m 0600 "$ssh_key" "$ssh_key_snapshot"
mkdir -m 0700 "$package_snapshot"
cp -a -- "$package_bundle/." "$package_snapshot/"
test "$(sha256sum "$archive_snapshot" | awk '{print $1}')" = "$actual_hash" || {
    echo 'base archive changed after validation' >&2; exit 1;
}
test "$(sha256sum "$ssh_key_snapshot" | awk '{print $1}')" = "$ssh_key_hash" || {
    echo 'SSH key changed after validation' >&2; exit 1;
}
snapshot_package_hash=$(python3 "$script_dir/verify-package-bundle.py" \
    --bundle "$package_snapshot" --role zero \
    --package-list "$script_dir/packages/common.txt" \
    --package-list "$script_dir/packages/zero.txt")
test "$snapshot_package_hash" = "$package_bundle_hash" || {
    echo 'package bundle changed after validation' >&2; exit 1;
}

mount_root=$(mktemp -d)
mount_boot=$(mktemp -d)
xz --decompress --keep --stdout -- "$archive_snapshot" > "$working"
loop_device=$(losetup --find --show --partscan "$working")
udevadm settle || true

materialize_partition_node() {
    node=$1
    test -b "$node" && return 0
    name=$(basename -- "$node")
    sys_path="/sys/class/block/$name"
    test -f "$sys_path/partition" && test -f "$sys_path/dev" || {
        echo "kernel did not expose partition metadata for $name" >&2; exit 1;
    }
    device_numbers=$(cat "$sys_path/dev")
    major=${device_numbers%:*}
    minor=${device_numbers#*:}
    case "$major:$minor" in *[!0-9:]*) echo 'invalid loop partition device numbers' >&2; exit 1 ;; esac
    mknod "$node" b "$major" "$minor"
    chmod 0600 "$node"
    created_nodes="$created_nodes $node"
}
materialize_partition_node "${loop_device}p1"
materialize_partition_node "${loop_device}p2"
mount "${loop_device}p2" "$mount_root"
mount "${loop_device}p1" "$mount_boot"
test "$(readlink -f "$(findmnt -n -o SOURCE --target "$mount_root")")" = "$(readlink -f "${loop_device}p2")"
test "$(readlink -f "$(findmnt -n -o SOURCE --target "$mount_boot")")" = "$(readlink -f "${loop_device}p1")"
grep -Eq '^VERSION_CODENAME=bookworm$' "$mount_root/etc/os-release" || {
    echo 'target root is not Raspberry Pi OS Bookworm' >&2; exit 1;
}
test -f "$mount_boot/config.txt" && test -f "$mount_boot/cmdline.txt" || {
    echo 'target boot partition lacks config.txt or cmdline.txt' >&2; exit 1;
}
if ! unshare --net -- chroot "$mount_root" /bin/true; then
    echo 'cannot execute armhf target; register qemu-arm binfmt and retry' >&2
    exit 1
fi

package_stage="$mount_root/tmp/tidetracker-offline-debs"
policy_path="$mount_root/usr/sbin/policy-rc.d"
test ! -e "$policy_path" || { echo 'target already has policy-rc.d' >&2; exit 1; }
install -d -m 0700 "$package_stage"
cat > "$policy_path" <<'EOF'
#!/bin/sh
exit 101
EOF
chmod 0755 "$policy_path"
filenames="$input_snapshot/verified-filenames"
python3 "$script_dir/verify-package-bundle.py" --bundle "$package_snapshot" --role zero \
    --package-list "$script_dir/packages/common.txt" \
    --package-list "$script_dir/packages/zero.txt" --print-filenames > "$filenames"
package_arguments=
while IFS= read -r package_filename; do
    install -m 0600 "$package_snapshot/debs/$package_filename" "$package_stage/$package_filename"
    package_arguments="$package_arguments /tmp/tidetracker-offline-debs/$package_filename"
done < "$filenames"
# Filenames come from the strict package-bundle verifier and cannot contain shell metacharacters.
# shellcheck disable=SC2086
unshare --net -- chroot "$mount_root" /usr/bin/env DEBIAN_FRONTEND=noninteractive LC_ALL=C \
    dpkg --unpack $package_arguments
unshare --net -- chroot "$mount_root" /usr/bin/env DEBIAN_FRONTEND=noninteractive LC_ALL=C \
    dpkg --configure -a
python3 "$script_dir/verify-package-bundle.py" --bundle "$package_snapshot" --role zero \
    --package-list "$script_dir/packages/common.txt" \
    --package-list "$script_dir/packages/zero.txt" \
    --verify-status "$mount_root/var/lib/dpkg/status" >/dev/null
rm -f -- "$policy_path"
rm -rf -- "$package_stage"
rm -rf -- "$mount_root/var/lib/apt/lists/"* "$mount_root/var/cache/apt/archives/"*

if unshare --net -- chroot "$mount_root" getent passwd tidetracker >/dev/null; then
    echo 'base image unexpectedly already contains the tidetracker user' >&2
    exit 1
fi
unshare --net -- chroot "$mount_root" useradd --create-home --shell /bin/bash tidetracker
unshare --net -- chroot "$mount_root" passwd --lock tidetracker >/dev/null
supplementary=
for group in gpio spi i2c video render netdev systemd-journal; do
    if unshare --net -- chroot "$mount_root" getent group "$group" >/dev/null; then
        if test -z "$supplementary"; then supplementary=$group; else supplementary="$supplementary,$group"; fi
    fi
done
test -z "$supplementary" || unshare --net -- chroot "$mount_root" \
    usermod --append --groups "$supplementary" tidetracker
user_id=$(unshare --net -- chroot "$mount_root" id -u tidetracker)
group_id=$(unshare --net -- chroot "$mount_root" id -g tidetracker)
install -d -m 0700 -o "$user_id" -g "$group_id" "$mount_root/home/tidetracker/.ssh"
install -m 0600 -o "$user_id" -g "$group_id" "$ssh_key_snapshot" \
    "$mount_root/home/tidetracker/.ssh/authorized_keys"

install -d -m 0755 "$mount_root/usr/local/bin" "$mount_root/usr/local/sbin" \
    "$mount_root/usr/local/libexec" "$mount_root/etc/systemd/system" \
    "$mount_root/etc/ssh/sshd_config.d" "$mount_root/etc/systemd/system/ssh.service.d" \
    "$mount_root/etc/NetworkManager/system-connections" "$mount_root/etc/sudoers.d" \
    "$mount_root/opt/tidetracker-dev/releases" "$mount_root/var/lib/tidetracker-dev"
install -d -m 0750 -o "$user_id" -g "$group_id" \
    "$mount_root/var/lib/tidetracker-dev/incoming"
install -m 0755 "$input_snapshot/dev/dev_bundle.py" \
    "$mount_root/usr/local/libexec/tidetracker-dev-bundle"
install -m 0755 "$input_snapshot/dev/tidetracker-dev" "$mount_root/usr/local/bin/tidetracker-dev"
install -m 0755 "$input_snapshot/dev/tidetracker-dev-activate" \
    "$mount_root/usr/local/sbin/tidetracker-dev-activate"
install -m 0755 "$input_snapshot/dev/tidetracker-dev-status" \
    "$mount_root/usr/local/bin/tidetracker-dev-status"
install -m 0644 "$input_snapshot/dev/tidetracker-dev.service" \
    "$mount_root/etc/systemd/system/tidetracker-dev.service"

cat > "$mount_root/etc/ssh/sshd_config.d/90-tidetracker-dev.conf" <<'EOF'
PubkeyAuthentication yes
AuthenticationMethods publickey
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
AllowUsers tidetracker
EOF
cat > "$mount_root/etc/systemd/system/ssh.service.d/10-generate-host-keys.conf" <<'EOF'
[Service]
ExecStartPre=
ExecStartPre=/usr/bin/ssh-keygen -A
ExecStartPre=/usr/sbin/sshd -t
EOF
cat > "$mount_root/etc/sudoers.d/90-tidetracker-dev" <<'EOF'
tidetracker ALL=(root) NOPASSWD: /usr/local/sbin/tidetracker-dev-activate *, /usr/bin/systemctl reboot
EOF
chmod 0440 "$mount_root/etc/sudoers.d/90-tidetracker-dev"
unshare --net -- chroot "$mount_root" visudo -cf /etc/sudoers.d/90-tidetracker-dev >/dev/null

cat > "$mount_root/etc/NetworkManager/system-connections/tidetracker-usb-dev.nmconnection" <<'EOF'
[connection]
id=tidetracker-usb-dev
type=ethernet
interface-name=usb0
autoconnect=true
autoconnect-priority=100

[ethernet]

[ipv4]
method=manual
addresses=10.77.0.2/30
never-default=true
may-fail=false
dns-search=

[ipv6]
method=disabled
EOF
chmod 0600 "$mount_root/etc/NetworkManager/system-connections/tidetracker-usb-dev.nmconnection"

printf '%s\n' tidetracker-zero-dev > "$mount_root/etc/hostname"
python3 - "$mount_root/etc/hosts" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines()
found = False
for index, line in enumerate(lines):
    if line.split(maxsplit=1)[0:1] == ["127.0.1.1"]:
        lines[index] = "127.0.1.1\ttidetracker-zero-dev"
        found = True
if not found:
    lines.append("127.0.1.1\ttidetracker-zero-dev")
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
grep -qxF 'dtoverlay=dwc2' "$mount_boot/config.txt" || printf '\n%s\n' 'dtoverlay=dwc2' >> "$mount_boot/config.txt"
grep -qxF 'dtparam=spi=on' "$mount_boot/config.txt" || printf '%s\n' 'dtparam=spi=on' >> "$mount_boot/config.txt"
python3 - "$mount_boot/cmdline.txt" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
tokens = path.read_text(encoding="ascii").split()
required = (
    "modules-load=dwc2,g_ether",
    "g_ether.host_addr=02:54:54:77:00:01",
    "g_ether.dev_addr=02:54:54:77:00:02",
)
for token in required:
    key = token.split("=", 1)[0] + "="
    tokens = [existing for existing in tokens if not existing.startswith(key)]
    tokens.append(token)
path.write_text(" ".join(tokens) + "\n", encoding="ascii")
PY

rm -f -- "$mount_root/etc/ssh/ssh_host_"*
: > "$mount_root/etc/machine-id"
rm -f -- "$mount_root/var/lib/dbus/machine-id"
install -d -m 0755 "$mount_root/var/lib/dbus"
ln -s /etc/machine-id "$mount_root/var/lib/dbus/machine-id"
rm -f -- "$mount_root/var/lib/systemd/random-seed"

install -m 0600 "$app_bundle" "$mount_root/var/lib/tidetracker-dev/incoming/bootstrap.tar"
unshare --net -- chroot "$mount_root" /usr/local/sbin/tidetracker-dev-activate \
    /var/lib/tidetracker-dev/incoming/bootstrap.tar "$application_hash" "$release_id" \
    --no-restart --remove-archive
unshare --net -- chroot "$mount_root" systemctl enable NetworkManager.service ssh.service \
    tidetracker-dev.service >/dev/null

printf '%s\n' "$image_build_id" > "$mount_root/etc/tidetracker-dev-image-id"
sync
umount "$mount_boot"
umount "$mount_root"
mount_boot=
mount_root=
losetup -d "$loop_device"
loop_device=
for node in $created_nodes; do
    case "$node" in /dev/loop[0-9]*p[0-9]*) rm -f -- "$node" ;; esac
done
created_nodes=

mv "$working" "$output"
image_hash=$(sha256sum "$output" | awk '{print $1}')
printf '%s  %s\n' "$image_hash" "$(basename -- "$output")" > "$output.sha256"
completed=true
echo "Built Pi Zero W development image: $output"
echo "Image SHA-256: $image_hash"
