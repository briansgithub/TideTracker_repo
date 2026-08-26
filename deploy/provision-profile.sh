#!/bin/sh
set -eu

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
role=
root_mount=
boot_mount=
data_mount=
ssh_key=
release_key=
lab_safety_key=
image_build_id=
package_bundle=
application_source=$(CDPATH='' cd -- "$script_dir/.." && pwd)
loop_device=
apply=false
execute=false
confirmation=

usage() {
    cat <<'EOF'
usage: provision-profile.sh --role controller|zero --root-mount DIR \
  --boot-mount DIR --data-mount DIR --ssh-public-key FILE \
  --release-public-key FILE [--application-source DIR] \
  --lab-safety-public-key FILE --image-build-id SHA256 --package-bundle DIR \
  --loop-device /dev/loopN \
  [--apply --execute --confirmation PROVISION-controller|PROVISION-zero]

Without --apply this command only validates arguments and prints its plan.
The three mount paths must be distinct mounted filesystems; live '/' is refused.
EOF
}

while test "$#" -gt 0; do
    case "$1" in
        --role) role=${2:?}; shift 2 ;;
        --root-mount) root_mount=${2:?}; shift 2 ;;
        --boot-mount) boot_mount=${2:?}; shift 2 ;;
        --data-mount) data_mount=${2:?}; shift 2 ;;
        --ssh-public-key) ssh_key=${2:?}; shift 2 ;;
        --release-public-key) release_key=${2:?}; shift 2 ;;
        --lab-safety-public-key) lab_safety_key=${2:?}; shift 2 ;;
        --image-build-id) image_build_id=${2:?}; shift 2 ;;
        --package-bundle) package_bundle=${2:?}; shift 2 ;;
        --application-source) application_source=${2:?}; shift 2 ;;
        --loop-device) loop_device=${2:?}; shift 2 ;;
        --apply) apply=true; shift ;;
        --execute) execute=true; shift ;;
        --confirmation) confirmation=${2:?}; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 64 ;;
    esac
done

case "$role" in controller|zero) ;; *) echo '--role must be controller or zero' >&2; exit 64 ;; esac
for path in "$root_mount" "$boot_mount" "$data_mount" "$ssh_key" "$release_key" "$lab_safety_key" "$image_build_id" "$application_source" "$package_bundle" "$loop_device"; do
    test -n "$path" || { echo 'all mount, key, and application arguments are required' >&2; exit 64; }
done
case "$image_build_id" in
    *[!0-9a-f]*|'') echo '--image-build-id must be a lowercase SHA-256 digest' >&2; exit 64 ;;
esac
test "${#image_build_id}" -eq 64 || {
    echo '--image-build-id must contain exactly 64 hexadecimal characters' >&2; exit 64;
}
test -d "$root_mount" && test -d "$boot_mount" && test -d "$data_mount"
test -f "$ssh_key" && test -s "$ssh_key"
test -f "$release_key" && test -s "$release_key"
test -f "$lab_safety_key" && test -s "$lab_safety_key"
test -d "$application_source/src/tidetracker" && test -f "$application_source/pyproject.toml"
test -d "$package_bundle" || { echo 'offline package bundle is missing' >&2; exit 1; }

root_mount=$(readlink -f "$root_mount")
boot_mount=$(readlink -f "$boot_mount")
data_mount=$(readlink -f "$data_mount")
ssh_key=$(readlink -f "$ssh_key")
release_key=$(readlink -f "$release_key")
lab_safety_key=$(readlink -f "$lab_safety_key")
application_source=$(readlink -f "$application_source")
package_bundle=$(readlink -f "$package_bundle")
loop_device=$(readlink -f "$loop_device")
case "$loop_device" in /dev/loop*) loop_number=${loop_device#/dev/loop} ;; *)
    echo '--loop-device must resolve exactly to /dev/loopN' >&2; exit 1 ;;
esac
case "$loop_number" in ''|*[!0-9]*)
    echo '--loop-device must resolve exactly to /dev/loopN' >&2; exit 1 ;;
esac

for command_name in dpkg-deb openssl python3 sha256sum ssh-keygen; do
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

for required_file in stations.csv Ubuntu-Regular.ttf Ubuntu-Medium.ttf Ubuntu-Bold.ttf \
    sun_rise.png sun_set.png no_wifi.bmp plot_image.bmp e-ink_lib/waveshare_epd/__init__.py \
    e-ink_lib/waveshare_epd/epdconfig.py e-ink_lib/waveshare_epd/epd7in5_V2.py \
    src/tidetracker/cli.py lab/hil_orchestrator.py lab/hil_config.example.json \
    lab/evaluate_promotion.py; do
    test -f "$application_source/$required_file" || {
        echo "application source is missing $required_file" >&2; exit 1;
    }
done

packages=
for package_file in "$script_dir/packages/common.txt" "$script_dir/packages/$role.txt"; do
    test -f "$package_file" || { echo "package list is missing: $package_file" >&2; exit 1; }
    while IFS= read -r package_name; do
        case "$package_name" in ''|'#'*) continue ;; *[!a-z0-9.+-]*)
            echo "invalid package name in $package_file: $package_name" >&2; exit 1 ;;
        esac
        packages="$packages $package_name"
    done < "$package_file"
done
package_bundle_hash=$(
    python3 "$script_dir/verify-package-bundle.py" --bundle "$package_bundle" --role "$role" \
        --package-list "$script_dir/packages/common.txt" \
        --package-list "$script_dir/packages/$role.txt"
)
application_hash=$(python3 "$script_dir/hash-application.py" --root "$application_source")

test "$root_mount" != / || { echo "refusing to provision the live root filesystem" >&2; exit 1; }
test "$root_mount" != "$boot_mount" && test "$root_mount" != "$data_mount" && test "$boot_mount" != "$data_mount" || {
    echo 'root, boot, and TIDEDATA mounts must be distinct' >&2; exit 1;
}

cat <<EOF
TideTracker offline provisioning plan
  role:       $role
  root:       $root_mount
  boot:       $boot_mount
  TIDEDATA:   $data_mount
  SSH key:    $ssh_key
  release key: $release_key
  lab safety key: $lab_safety_key
  image build ID: $image_build_id
  application: $application_source
  loop device: $loop_device (boot p1, root p2, TIDEDATA p3)
  packages:   $packages
  package bundle: $package_bundle ($package_bundle_hash)
  USB subnet: 10.77.0.0/30 (never-default, no DNS)
  root/boot:  read-only overlay and read-only boot enabled on first boot
EOF

if test "$apply" != true || test "$execute" != true; then
    echo "DRY RUN: no files were changed. Re-run with --apply --execute --confirmation PROVISION-$role."
    exit 0
fi

test "$(id -u)" -eq 0 || { echo '--apply requires root' >&2; exit 1; }
test "$confirmation" = "PROVISION-$role" || { echo "confirmation must be PROVISION-$role" >&2; exit 1; }
for command_name in chroot cp findmnt install mktemp mountpoint sha256sum unshare; do
    command -v "$command_name" >/dev/null 2>&1 || {
        echo "missing required command: $command_name" >&2; exit 1;
    }
done
for path in "$root_mount" "$boot_mount" "$data_mount"; do
    mountpoint -q "$path" || { echo "not a mount point: $path" >&2; exit 1; }
done
test -b "$loop_device" || { echo "loop device is not a block device: $loop_device" >&2; exit 1; }
root_source=$(readlink -f "$(findmnt -n -o SOURCE --target "$root_mount")")
boot_source=$(readlink -f "$(findmnt -n -o SOURCE --target "$boot_mount")")
data_source=$(readlink -f "$(findmnt -n -o SOURCE --target "$data_mount")")
test "$root_source" = "${loop_device}p2" \
    && test "$boot_source" = "${loop_device}p1" \
    && test "$data_source" = "${loop_device}p3" || {
    echo 'mount sources must be p2/p1/p3 of the exact supplied loop device' >&2
    echo "found root=$root_source boot=$boot_source data=$data_source" >&2
    exit 1
}
test -f "$root_mount/etc/os-release" || { echo 'target lacks /etc/os-release' >&2; exit 1; }
grep -Eq '^VERSION_CODENAME=bookworm$' "$root_mount/etc/os-release" || {
    echo 'target is not the pinned Bookworm baseline' >&2; exit 1;
}
test -f "$boot_mount/config.txt" && test -f "$boot_mount/cmdline.txt" || {
    echo 'boot mount lacks config.txt or cmdline.txt' >&2; exit 1;
}
data_label=$(findmnt -n -o LABEL --target "$data_mount" || true)
test "$data_label" = TIDEDATA || { echo "data filesystem label must be TIDEDATA, found '$data_label'" >&2; exit 1; }
if test "$role" = zero; then
    data_uuid=54540000-0000-4000-8000-000000000002
else
    data_uuid=54540000-0000-4000-8000-000000000001
fi
actual_data_uuid=$(findmnt -n -o UUID --target "$data_mount" || true)
test "$actual_data_uuid" = "$data_uuid" || {
    echo "TIDEDATA UUID must be $data_uuid, found '$actual_data_uuid'" >&2; exit 1;
}
test ! -f "$root_mount/etc/fstab" || ! grep -q '# BEGIN TIDETRACKER' "$root_mount/etc/fstab" || {
    echo 'target fstab already contains TideTracker entries; refusing an ambiguous reprovision' >&2; exit 1;
}

package_snapshot=$(mktemp -d)
package_stage="$root_mount/tmp/tidetracker-offline-debs"
application_file_list="$root_mount/tmp/tidetracker-application-files"
policy_path="$root_mount/usr/sbin/policy-rc.d"
cleanup_chroot_build() {
    rm -f -- "$policy_path" "$application_file_list"
    rm -rf -- "$package_stage"
    rm -rf -- "$package_snapshot"
}
trap cleanup_chroot_build 0 1 2 15
chmod 0700 "$package_snapshot"
cp -a -- "$package_bundle/." "$package_snapshot/"
snapshot_package_hash=$(
    python3 "$script_dir/verify-package-bundle.py" --bundle "$package_snapshot" --role "$role" \
        --package-list "$script_dir/packages/common.txt" \
        --package-list "$script_dir/packages/$role.txt"
)
test "$snapshot_package_hash" = "$package_bundle_hash" || {
    echo 'offline package bundle changed after validation' >&2; exit 1;
}
package_bundle=$package_snapshot
python3 "$script_dir/hash-application.py" --root "$application_source" --print-files \
    > "$application_file_list"
test "$(python3 "$script_dir/hash-application.py" --root "$application_source")" = "$application_hash" || {
    echo 'application inputs changed after validation' >&2; exit 1;
}

install -d -m 0755 "$root_mount/etc/tidetracker" "$root_mount/etc/systemd/system" \
    "$root_mount/usr/local/bin" "$root_mount/usr/local/libexec" "$root_mount/usr/local/sbin" \
    "$root_mount/usr/local/lib/tidetracker/app/tidetracker" \
    "$root_mount/usr/local/lib/tidetracker/lab" "$root_mount/usr/lib/tidetracker/e-ink_lib/waveshare_epd" \
    "$root_mount/usr/share/tidetracker" \
    "$root_mount/etc/NetworkManager/system-connections" \
    "$root_mount/etc/NetworkManager/conf.d" "$root_mount/var/lib/tidetracker"
install -d -m 0750 "$data_mount/config" "$data_mount/incoming" "$data_mount/releases" \
    "$data_mount/results" "$data_mount/state" "$data_mount/telemetry" "$data_mount/uploads"
install -d -m 0700 "$data_mount/networkmanager"

test -x "$root_mount/usr/bin/dpkg" && test -x "$root_mount/bin/true" || {
    echo 'target lacks dpkg or /bin/true' >&2; exit 1;
}
if ! unshare --net -- chroot "$root_mount" /bin/true; then
    echo 'cannot execute the armhf target; build on ARM or register qemu-arm binfmt first' >&2
    exit 1
fi

test ! -e "$policy_path" || {
    echo 'target already has policy-rc.d; refusing to replace build policy' >&2; exit 1;
}
cat > "$policy_path" <<'EOF'
#!/bin/sh
exit 101
EOF
chmod 0755 "$policy_path"

install -d -m 0700 "$package_stage"
package_arguments=
package_filename_list="$package_snapshot/verified-filenames"
python3 "$script_dir/verify-package-bundle.py" --bundle "$package_bundle" --role "$role" \
    --package-list "$script_dir/packages/common.txt" \
    --package-list "$script_dir/packages/$role.txt" --print-filenames > "$package_filename_list"
while IFS= read -r package_filename; do
    install -m 0600 "$package_bundle/debs/$package_filename" "$package_stage/$package_filename"
    test "$(sha256sum "$package_bundle/debs/$package_filename" | awk '{print $1}')" = \
        "$(sha256sum "$package_stage/$package_filename" | awk '{print $1}')" || {
        echo "staged package hash mismatch: $package_filename" >&2; exit 1;
    }
done < "$package_filename_list"
python3 "$script_dir/verify-package-bundle.py" --bundle "$package_bundle" --role "$role" \
    --package-list "$script_dir/packages/common.txt" \
    --package-list "$script_dir/packages/$role.txt" >/dev/null
for package_path in "$package_stage"/*.deb; do
    package_arguments="$package_arguments /tmp/tidetracker-offline-debs/$(basename -- "$package_path")"
done
# All package bytes and control metadata were verified before image mutation.
# dpkg runs in a network namespace with no interfaces and cannot consult APT.
# shellcheck disable=SC2086
unshare --net -- chroot "$root_mount" /usr/bin/env DEBIAN_FRONTEND=noninteractive LC_ALL=C \
    dpkg --unpack $package_arguments
unshare --net -- chroot "$root_mount" /usr/bin/env DEBIAN_FRONTEND=noninteractive LC_ALL=C \
    dpkg --configure -a
unshare --net -- chroot "$root_mount" systemctl enable systemd-resolved.service >/dev/null
python3 "$script_dir/verify-package-bundle.py" --bundle "$package_bundle" --role "$role" \
    --package-list "$script_dir/packages/common.txt" \
    --package-list "$script_dir/packages/$role.txt" \
    --verify-status "$root_mount/var/lib/dpkg/status" >/dev/null
install -m 0644 "$package_bundle/packages.lock.json" \
    "$root_mount/etc/tidetracker/bookworm-packages.lock.json"
printf '%s\n' "$package_bundle_hash" > "$root_mount/etc/tidetracker/package-bundle.sha256"
rm -rf -- "$root_mount/var/lib/apt/lists/"* "$root_mount/var/cache/apt/archives/"*
rm -f -- "$policy_path"

cat > "$root_mount/etc/NetworkManager/conf.d/10-tidetracker-resolved.conf" <<'EOF'
[main]
dns=systemd-resolved
rc-manager=symlink
EOF
rm -f -- "$root_mount/etc/resolv.conf"
ln -s /run/systemd/resolve/stub-resolv.conf "$root_mount/etc/resolv.conf"
install -d -m 0755 "$root_mount/etc/ssh/sshd_config.d"
cat > "$root_mount/etc/ssh/sshd_config.d/90-tidetracker.conf" <<'EOF'
PubkeyAuthentication yes
AuthenticationMethods publickey
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
AllowUsers tidetracker
EOF
install -d -m 0755 "$root_mount/etc/systemd/system/ssh.service.d"
install -m 0644 "$script_dir/../systemd/tidetracker-ssh-provisioned.conf" \
    "$root_mount/etc/systemd/system/ssh.service.d/90-tidetracker-provisioned.conf"

# Every card must create its own machine and SSH identities on first boot.
rm -f -- "$root_mount/etc/ssh/ssh_host_"*
: > "$root_mount/etc/machine-id"
rm -f -- "$root_mount/var/lib/dbus/machine-id"
install -d -m 0755 "$root_mount/var/lib/dbus"
ln -s /etc/machine-id "$root_mount/var/lib/dbus/machine-id"
rm -f -- "$root_mount/var/lib/systemd/random-seed"
rm -f -- "$root_mount/var/swap"
ln -sfn /dev/null "$root_mount/etc/systemd/system/dphys-swapfile.service"

printf '%s\n' "$role" > "$root_mount/etc/tidetracker/role"
cat > "$root_mount/etc/tidetracker/runtime.env" <<EOF
TIDETRACKER_ROLE=$role
TIDETRACKER_DATA_DIR=/var/lib/tidetracker
TIDETRACKER_CONFIG=/var/lib/tidetracker/config/config.json
TIDETRACKER_UPDATE_ROOT=/var/lib/tidetracker
TIDETRACKER_RELEASE_PUBLIC_KEY=/etc/tidetracker/release-ed25519.pub.pem
TIDETRACKER_IMAGE_BUILD_ID_FILE=/etc/tidetracker/image-build-id
TIDETRACKER_ASSET_DIR=/usr/share/tidetracker
TIDETRACKER_EINK_LIB=/usr/lib/tidetracker/e-ink_lib
TIDETRACKER_STATIONS=/usr/share/tidetracker/stations.csv
EOF
chmod 0644 "$root_mount/etc/tidetracker/runtime.env"
install -m 0600 "$ssh_key" "$root_mount/etc/tidetracker/provision-authorized-key"
install -m 0644 "$release_key" "$root_mount/etc/tidetracker/release-ed25519.pub.pem"
install -m 0644 "$lab_safety_key" "$root_mount/etc/tidetracker/lab-safety-approver.pub.pem"
printf '%s\n' "$image_build_id" > "$root_mount/etc/tidetracker/image-build-id"
chmod 0644 "$root_mount/etc/tidetracker/image-build-id"

while IFS= read -r application_item; do
    source_file="$application_source/$application_item"
    case "$application_item" in
        src/tidetracker/*.py)
            relative_path=${application_item#src/tidetracker/}
            destination="$root_mount/usr/local/lib/tidetracker/app/tidetracker/$relative_path"
            ;;
        stations.csv|Ubuntu-Regular.ttf|Ubuntu-Medium.ttf|Ubuntu-Bold.ttf|sun_rise.png|sun_set.png|no_wifi.bmp|plot_image.bmp)
            destination="$root_mount/usr/share/tidetracker/$application_item"
            ;;
        e-ink_lib/waveshare_epd/*.py)
            driver_file=${application_item#e-ink_lib/waveshare_epd/}
            destination="$root_mount/usr/lib/tidetracker/e-ink_lib/waveshare_epd/$driver_file"
            ;;
        lab/hil_orchestrator.py|lab/hil_config.example.json|lab/evaluate_promotion.py)
            test "$role" = controller || continue
            destination="$root_mount/usr/local/lib/tidetracker/lab/${application_item#lab/}"
            ;;
        deploy/*|systemd/*) continue ;;
        pyproject.toml) continue ;;
        *) echo "unmapped validated application input: $application_item" >&2; exit 1 ;;
    esac
    install -d -m 0755 "$(dirname -- "$destination")"
    install -m 0644 "$source_file" "$destination"
    test "$(sha256sum "$source_file" | awk '{print $1}')" = \
        "$(sha256sum "$destination" | awk '{print $1}')" || {
        echo "installed application file hash mismatch: $application_item" >&2; exit 1;
    }
done < "$application_file_list"
test "$(python3 "$script_dir/hash-application.py" --root "$application_source")" = "$application_hash" || {
    echo 'application inputs changed while they were installed' >&2; exit 1;
}
for command_file in tidetracker tidetracker-update; do
    install -m 0755 "$script_dir/bin/$command_file" "$root_mount/usr/local/bin/$command_file"
done
if test "$role" = controller; then
    install -m 0755 "$script_dir/bin/tidetracker-hil" "$root_mount/usr/local/bin/tidetracker-hil"
    install -m 0755 "$script_dir/bin/tidetracker-promote" \
        "$root_mount/usr/local/bin/tidetracker-promote"
    install -m 0755 "$script_dir/sbin/tidetracker-controller-wifi-control" \
        "$root_mount/usr/local/sbin/tidetracker-controller-wifi-control"
    install -m 0750 "$script_dir/libexec/tidetracker-controller-portal-matrix.py" \
        "$root_mount/usr/local/libexec/tidetracker-controller-portal-matrix"
    install -m 0644 "$script_dir/../systemd/tidetracker-controller-test-plane.service" \
        "$root_mount/etc/systemd/system/tidetracker-controller-test-plane.service"
fi

release_key_hash=$(sha256sum "$release_key" | awk '{print $1}')
lab_safety_key_hash=$(sha256sum "$lab_safety_key" | awk '{print $1}')
ssh_key_hash=$(sha256sum "$ssh_key" | awk '{print $1}')
printf '{"schema_version":1,"image_build_id":"%s","application_sha256":"%s","ssh_authorized_key_sha256":"%s","release_public_key_sha256":"%s","lab_safety_public_key_sha256":"%s","package_bundle_sha256":"%s"}\n' \
    "$image_build_id" "$application_hash" "$ssh_key_hash" "$release_key_hash" "$lab_safety_key_hash" "$package_bundle_hash" \
    > "$root_mount/etc/tidetracker/application-build.json"
cleanup_chroot_build
trap - 0 1 2 15

test ! -e "$data_mount/config/config.json" || {
    echo 'TIDEDATA already contains an application config; refusing to overwrite it' >&2; exit 1;
}
umask 077
cat > "$data_mount/config/config.json" <<'EOF'
{
  "schema_version": 1,
  "station": {
    "station_id": "8531833",
    "name": "Navesink - Red Bank, NJ",
    "latitude": 40.355,
    "longitude": -74.065,
    "timezone": "America/New_York"
  },
  "datum": "MLLW",
  "units": "english",
  "prediction_interval_minutes": 5,
  "prediction_range_hours": 60,
  "chart_hours": 48,
  "timeouts": {
    "normal_cycle_seconds": 150.0,
    "setup_session_seconds": 900.0,
    "network_seconds": 30.0,
    "fetch_seconds": 30.0,
    "render_seconds": 15.0,
    "display_seconds": 60.0
  }
}
EOF
chmod 0600 "$data_mount/config/config.json"

for unit in tidetracker-cycle.service tidetracker-failsafe.service tidetracker-portal.service \
    tidetracker-firstboot.service tidetracker-usb-firewall.service; do
    install -m 0644 "$script_dir/../systemd/$unit" "$root_mount/etc/systemd/system/$unit"
done
install -m 0755 "$script_dir/libexec/tidetracker-failsafe" "$root_mount/usr/local/libexec/tidetracker-failsafe"
install -m 0755 "$script_dir/libexec/tidetracker-firstboot" "$root_mount/usr/local/libexec/tidetracker-firstboot"
install -m 0755 "$script_dir/sbin/tidetracker-lab-control" "$root_mount/usr/local/sbin/tidetracker-lab-control"

install -d -m 0755 "$root_mount/etc/sudoers.d"
printf 'tidetracker ALL=(root) NOPASSWD: /usr/local/sbin/tidetracker-lab-control *\n' > "$root_mount/etc/sudoers.d/90-tidetracker-lab"
if test "$role" = controller; then
    printf 'tidetracker ALL=(root) NOPASSWD: /usr/local/sbin/tidetracker-controller-wifi-control *\n' \
        >> "$root_mount/etc/sudoers.d/90-tidetracker-lab"
    install -d -m 0700 "$data_mount/controller"
    cat > "$data_mount/controller/controller-lab.env" <<'EOF'
TIDETRACKER_NORMAL_AP_CONNECTION=tidetracker-lab-ap
TIDETRACKER_PORTAL_CLIENT_CONNECTION=tidetracker-zero-portal
TIDETRACKER_ETHERNET_INTERFACE=eth0
EOF
    chmod 0600 "$data_mount/controller/controller-lab.env"
fi
chmod 0440 "$root_mount/etc/sudoers.d/90-tidetracker-lab"

if test "$role" = zero; then
    install -m 0600 "$script_dir/network/zero-usb-management.nmconnection" \
        "$data_mount/networkmanager/tidetracker-usb-management.nmconnection"
    install -m 0644 "$script_dir/network/zero-usb-firewall.nft" \
        "$root_mount/etc/tidetracker/zero-usb-firewall.nft"
else
    install -m 0600 "$script_dir/network/controller-usb-management.nmconnection" \
        "$data_mount/networkmanager/tidetracker-zero-usb.nmconnection"
    install -m 0600 "$script_dir/network/controller-zero-portal.nmconnection" \
        "$data_mount/networkmanager/tidetracker-zero-portal.nmconnection"
fi

cat >> "$root_mount/etc/fstab" <<'EOF'

# BEGIN TIDETRACKER
UUID=REPLACE_TIDEDATA_UUID /var/lib/tidetracker ext4 defaults,noatime,nodev,nosuid,x-systemd.device-timeout=15 0 2
/var/lib/tidetracker/networkmanager /etc/NetworkManager/system-connections none bind,x-systemd.requires-mounts-for=/var/lib/tidetracker 0 0
tmpfs /tmp tmpfs nosuid,nodev,noatime,mode=1777,size=64M 0 0
tmpfs /var/tmp tmpfs nosuid,nodev,noatime,mode=1777,size=32M 0 0
# END TIDETRACKER
EOF
sed -i "s/REPLACE_TIDEDATA_UUID/$data_uuid/" "$root_mount/etc/fstab"

install -d -m 0755 "$root_mount/etc/systemd/journald.conf.d"
cat > "$root_mount/etc/systemd/journald.conf.d/tidetracker-volatile.conf" <<'EOF'
[Journal]
Storage=volatile
RuntimeMaxUse=16M
ForwardToSyslog=no
EOF

hostname="tidetracker-$role"
printf '%s\n' "$hostname" > "$root_mount/etc/hostname"
if grep -Eq '^127\.0\.1\.1[[:space:]]+' "$root_mount/etc/hosts"; then
    sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$hostname/" "$root_mount/etc/hosts"
else
    printf '127.0.1.1\t%s\n' "$hostname" >> "$root_mount/etc/hosts"
fi

if test "$role" = zero; then
    if ! grep -q '^# BEGIN TIDETRACKER ZERO$' "$boot_mount/config.txt"; then
        cat >> "$boot_mount/config.txt" <<'EOF'

# BEGIN TIDETRACKER ZERO
[all]
dtoverlay=dwc2,dr_mode=peripheral
dtoverlay=disable-bt
dtparam=spi=on
enable_uart=1
# END TIDETRACKER ZERO
EOF
    fi
    cmdline=$(tr -d '\n' < "$boot_mount/cmdline.txt")
    case " $cmdline " in *' console=serial0,115200 '*) ;; *)
        cmdline="console=serial0,115200 $cmdline"
    esac
    case "$cmdline" in *g_ether*) ;; *)
        case "$cmdline" in *modules-load=*)
            echo 'cmdline already contains modules-load without g_ether; merge it explicitly before provisioning' >&2
            exit 1
        esac
        printf '%s modules-load=dwc2,g_ether g_ether.host_addr=02:54:54:77:00:01 g_ether.dev_addr=02:54:54:77:00:02\n' \
            "$cmdline" > "$boot_mount/cmdline.txt"
    esac
fi
touch "$boot_mount/ssh"

# A third TIDEDATA partition immediately follows p2. Disable every Raspberry Pi
# first-boot root-resize path before it can expand p2 across p3.
cmdline=$(tr -d '\n' < "$boot_mount/cmdline.txt")
filtered_cmdline=
for token in $cmdline; do
    case "$token" in
        resize|init=/usr/lib/raspi-config/init_resize.sh|init=/usr/lib/raspberrypi-sys-mods/firstboot)
            continue
            ;;
    esac
    filtered_cmdline="$filtered_cmdline $token"
done
filtered_cmdline=${filtered_cmdline# }
test -n "$filtered_cmdline" || { echo 'boot cmdline became empty while disabling root resize' >&2; exit 1; }
printf '%s\n' "$filtered_cmdline" > "$boot_mount/cmdline.txt"
grep -Eq '(^|[[:space:]])(resize|init=/(usr/lib/raspi-config/init_resize\.sh|usr/lib/raspberrypi-sys-mods/firstboot))([[:space:]]|$)' \
    "$boot_mount/cmdline.txt" && {
    echo 'automatic root-resize init remains enabled' >&2; exit 1;
}
ln -sfn /dev/null "$root_mount/etc/systemd/system/resize2fs_once.service"
rm -f -- "$root_mount/etc/init.d/resize2fs_once"

install -d -m 0755 "$root_mount/etc/systemd/system/multi-user.target.wants"
if test "$role" = zero; then
    ln -sfn /dev/null "$root_mount/etc/systemd/system/serial-getty@serial0.service"
    ln -sfn /dev/null "$root_mount/etc/systemd/system/hciuart.service"
fi
ln -sfn ../tidetracker-firstboot.service "$root_mount/etc/systemd/system/multi-user.target.wants/tidetracker-firstboot.service"
if test "$role" = zero; then
    ln -sfn ../tidetracker-cycle.service "$root_mount/etc/systemd/system/multi-user.target.wants/tidetracker-cycle.service"
    ln -sfn ../tidetracker-usb-firewall.service \
        "$root_mount/etc/systemd/system/multi-user.target.wants/tidetracker-usb-firewall.service"
fi

sync
echo "Provisioned $role profile. First boot will create the locked SSH-key-only account and enable overlayfs."
