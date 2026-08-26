#!/bin/sh
set -eu

apply=false
execute=false
confirmation=
secret_file=
connection_file=/var/lib/tidetracker/networkmanager/tidetracker-lab-ap.nmconnection

usage() {
    cat <<'EOF'
usage: sudo configure-controller-network.sh --secret-file FILE \
  [--apply --execute --confirmation CONFIGURE-CONTROLLER-AP]

FILE must be root-owned, mode 0600, and contain one line:
TIDETRACKER_AP_PSK=<12-to-63-character letters/digits/._- secret>

Without --apply the script performs inspection only.
EOF
}

while test "$#" -gt 0; do
    case "$1" in
        --secret-file) secret_file=${2:?}; shift 2 ;;
        --apply) apply=true; shift ;;
        --execute) execute=true; shift ;;
        --confirmation) confirmation=${2:?}; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 64 ;;
    esac
done

test -n "$secret_file" && test -f "$secret_file" || { echo '--secret-file must identify a file' >&2; exit 64; }
secret_file=$(readlink -f "$secret_file")
test "$(cat /etc/tidetracker/role 2>/dev/null || true)" = controller || {
    echo 'this command is restricted to a provisioned controller role' >&2; exit 1;
}

cat <<EOF
Controller network plan
  Ethernet: existing upstream management; never modified here
  wlan0:    TideTracker-Lab, 2.4 GHz channel 6, 10.78.0.1/24
  USB peer: 10.77.0.1/30, never-default and no DNS (pre-provisioned)
  secret:   $secret_file (value will not be displayed)
EOF
if test "$apply" != true || test "$execute" != true; then
    echo 'DRY RUN: NetworkManager and files were not changed.'
    exit 0
fi

test "$(id -u)" -eq 0 || { echo '--apply requires root' >&2; exit 1; }
test "$confirmation" = CONFIGURE-CONTROLLER-AP || { echo 'confirmation must be CONFIGURE-CONTROLLER-AP' >&2; exit 1; }
owner=$(stat -c %u "$secret_file")
mode=$(stat -c %a "$secret_file")
test "$owner" = 0 && test "$mode" = 600 || { echo 'secret file must be root-owned with mode 0600' >&2; exit 1; }
test "$(awk 'END { print NR }' "$secret_file")" -eq 1 || { echo 'secret file must contain exactly one line' >&2; exit 1; }
psk=$(sed -n 's/^TIDETRACKER_AP_PSK=//p' "$secret_file")
case "$psk" in
    *[!A-Za-z0-9._-]*|'') echo 'AP secret contains unsupported characters' >&2; exit 1 ;;
esac
test "${#psk}" -ge 12 && test "${#psk}" -le 63 || { echo 'AP secret length must be 12..63 characters' >&2; exit 1; }

install -d -m 0700 -o root -g root "$(dirname -- "$connection_file")"
temporary="$connection_file.tmp.$$"
umask 077
trap 'rm -f "$temporary"' EXIT INT TERM HUP
{
    cat <<'EOF'
[connection]
id=tidetracker-lab-ap
uuid=eea12d5b-22cc-4fac-9002-545477000078
type=wifi
interface-name=wlan0
autoconnect=true
autoconnect-priority=50

[wifi]
mode=ap
band=bg
channel=6
ssid=TideTracker-Lab

[wifi-security]
key-mgmt=wpa-psk
EOF
    printf 'psk=%s\n' "$psk"
    cat <<'EOF'

[ipv4]
method=shared
address1=10.78.0.1/24
never-default=true
ignore-auto-dns=true

[ipv6]
method=disabled

[proxy]
EOF
} > "$temporary"
chmod 0600 "$temporary"
mv -f "$temporary" "$connection_file"
trap - EXIT INT TERM HUP
sync -f "$connection_file" 2>/dev/null || true
nmcli connection reload
nmcli connection up tidetracker-lab-ap
echo 'TideTracker-Lab is active. Ethernet remains the controller management path.'
