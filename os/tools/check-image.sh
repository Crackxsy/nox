#!/usr/bin/env bash
# Check a built Nox-OS image before anything else happens with it, and take out what the boot test
# needs (os/README.md). Mounts the image read-only; changes nothing in it.
#
#   sudo os/tools/check-image.sh <image.img> <out folder>
#
# Fails when the image carries anything that must be made per device or never exist at all: a
# password, a key, SSH switched on, Nox data (certificates, pairings), cloud-init or Wi-Fi seeds.
# Writes to <out>: kernel8.img and the Pi 3 device tree (for os/tools/qemu-smoke.sh) and
# <image name>.licenses.json (os/tools/source_manifest.py).
set -euo pipefail

image="${1:?usage: check-image.sh <image.img> <out folder>}"
out="${2:?usage: check-image.sh <image.img> <out folder>}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
name="$(basename "$image" .img)"
[ "$(id -u)" -eq 0 ] || { echo "check-image.sh: needs root (loop devices)" >&2; exit 1; }

mnt="$(mktemp -d)"
loop=""
cleanup() {
  umount "$mnt/boot" "$mnt/root" 2>/dev/null || true
  [ -z "$loop" ] || losetup -d "$loop" 2>/dev/null || true
  rmdir "$mnt/boot" "$mnt/root" "$mnt" 2>/dev/null || true
}
trap cleanup EXIT
loop="$(losetup --read-only --find --show --partscan "$image")"
udevadm settle 2>/dev/null || true
mkdir -p "$mnt/boot" "$mnt/root"
mount -o ro "${loop}p1" "$mnt/boot"
mount -o ro,noload "${loop}p2" "$mnt/root"
root="$mnt/root" boot="$mnt/boot"

failures=0
check() {  # check "what" command...
  local what="$1"; shift
  if "$@"; then printf 'ok    %s\n' "$what"; else printf 'FAIL  %s\n' "$what"; failures=$((failures + 1)); fi
}
none() { [ -z "$(cat)" ]; }  # stdin is empty
files() { local f; for f in "$@"; do [ -f "$f" ] || return 1; done; }

# Nox is there, laid out as an install lays it out, enabled but never started.
current="$(readlink "$root/opt/nox/current" || true)"
check "Nox installed (/opt/nox/current -> $current)" files "$root/opt/nox/current/.complete" "$root/opt/nox/current/.venv/bin/nox"
check "nox command and unit installed" files "$root/usr/local/bin/nox" "$root/etc/systemd/system/nox.service"
check "nox.service enabled" test -L "$root/etc/systemd/system/multi-user.target.wants/nox.service"
check "dashboard bundled" test -f "$root/opt/nox/current/ui/dashboard/dist/index.html"
check "no download caches" test ! -e "$root/opt/nox/.cache"
check "/etc/nox-os-release" grep -q '^NOX_OS_VERSION=' "$root/etc/nox-os-release"
check "hostname is nox" grep -qx 'nox' "$root/etc/hostname"

# Nothing of Nox's own: the Pi makes its certificates, keys and pairings on its first start.
check "Nox data is only the settings file" none < <(find "$root/var/lib/nox" -mindepth 1 ! -path "$root/var/lib/nox/user.yaml")

# No account can log in, nobody has a key, SSH is off.
check "no password in /etc/shadow" none < <(awk -F: '$2 != "*" && $2 != "!" && $2 != "!*" && $2 != "!!" {print $1}' "$root/etc/shadow")
check "no authorized_keys" none < <(find "$root/root" "$root/home" -name 'authorized_keys*' 2>/dev/null)
check "no SSH host keys (made per device on first boot)" none < <(find "$root/etc/ssh" -name 'ssh_host_*' 2>/dev/null)
check "ssh.service masked" test "$(readlink "$root/etc/systemd/system/ssh.service" || true)" = /dev/null
check "no shared private keys" none < <(grep -rlI -e '-----BEGIN [A-Z ]*PRIVATE KEY-----' \
  "$root/etc" "$root/root" "$root/home" "$root/var/lib" "$root/opt/nox/state" 2>/dev/null)
check "machine-id made on first boot" none < <(grep -vx 'uninitialized' "$root/etc/machine-id")

# Nothing on the boot partition that configures users, SSH or networks.
check "no provisioning seeds on the boot partition" none < <(find "$boot" -maxdepth 1 \
  \( -iname 'user-data' -o -iname 'meta-data' -o -iname 'network-config' -o -iname 'ssh' \
     -o -iname 'ssh.txt' -o -iname 'userconf*' -o -iname 'wpa_supplicant.conf' -o -iname 'firstrun.sh' \))
check "no saved Wi-Fi or network secrets" none < <(find "$root/etc/NetworkManager/system-connections" -type f 2>/dev/null)
check "cloud-init not installed" test ! -e "$root/usr/bin/cloud-init"

mkdir -p "$out"
cp "$boot/kernel8.img" "$boot/bcm2710-rpi-3-b-plus.dtb" "$out/"
python3 -I "$here/source_manifest.py" --root "$root" --image "$name" --out "$out/$name.licenses.json"

printf '\nroot file system: %s used of %s\n' \
  "$(df -h --output=used "$mnt/root" | tail -n1 | tr -d ' ')" "$(df -h --output=size "$mnt/root" | tail -n1 | tr -d ' ')"
if [ "$failures" -gt 0 ]; then
  printf '%s check(s) failed: this image must not be published.\n' "$failures" >&2
  exit 1
fi
echo "All image checks passed."
