#!/usr/bin/env bash
# Check a built Nox-OS image before anything else happens with it (os/README.md).
#
#   sudo os/tools/check-image.sh <image.img> <out folder>
#       mounts the image read-only (changes nothing in it), checks it, and writes to <out>:
#       kernel8.img and the Pi 3 device tree (for os/tools/qemu-smoke.sh) and
#       <image name>.licenses.json (os/tools/source_manifest.py)
#   os/tools/check-image.sh --root <dir> --boot <dir>
#       the same checks on an unpacked tree (tests/unit/nox_os/test_check_image.py)
#
# Fails when the image carries anything that must be made per device or never exist at all: a
# password, a key or token, SSH switched on, Nox data (certificates, pairings), a seed or secret
# of the system, cloud-init / Wi-Fi / first-run seeds on the boot partition, build leftovers.
set -euo pipefail

REPO_URL="https://github.com/Crackxsy/nox"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

failures=0
check() {  # check "what" command...
  local what="$1"; shift
  if "$@"; then printf 'ok    %s\n' "$what"; else printf 'FAIL  %s\n' "$what"; failures=$((failures + 1)); fi
}
none() { [ -z "$(cat)" ]; }  # stdin is empty
files() { local f; for f in "$@"; do [ -f "$f" ] || return 1; done; }
absent() { local f; for f in "$@"; do { [ ! -e "$f" ] && [ ! -L "$f" ]; } || return 1; done; }
masked() {
  local unit
  for unit in "$@"; do [ "$(readlink "$root/etc/systemd/system/$unit" || true)" = /dev/null ] || return 1; done
}
# Exactly user.yaml in /var/lib/nox - and the folder must exist.
nox_data_is_settings_only() {
  [ -d "$root/var/lib/nox" ] && [ "$(find "$root/var/lib/nox" -mindepth 1 -printf '%P\n')" = user.yaml ]
}
# /opt/nox/src fetches from the project and carries no credentials.
source_is_clean() {
  local config="$root/opt/nox/src/.git/config"
  [ -f "$config" ] || return 1
  grep -qE "^[[:space:]]*url = $REPO_URL(\.git)?\$" "$config" || return 1
  if grep -E '^[[:space:]]*url = ' "$config" | grep -qvE "url = $REPO_URL(\.git)?\$"; then return 1; fi
  ! grep -qiE '://[^/[:space:]]*@|extraheader|password|token|credential' "$config"
}
cmdline_is_plain() {
  [ ! -f "$boot/cmdline.txt" ] || ! grep -qE '(^|[[:space:]])(systemd\.run|init)=' "$boot/cmdline.txt"
}

run_checks() {
  # Nox is there, laid out as an install lays it out, enabled but never started.
  # /opt/nox/current is an absolute link: it means the image's /opt, not this machine's.
  local current release
  current="$(readlink "$root/opt/nox/current" || true)"
  release="$root${current:-/nonexistent}"
  check "Nox installed (/opt/nox/current -> $current)" files "$release/.complete" "$release/.venv/bin/nox"
  check "nox command and unit installed" files "$root/usr/local/bin/nox" "$root/etc/systemd/system/nox.service"
  check "nox.service enabled" test -L "$root/etc/systemd/system/multi-user.target.wants/nox.service"
  check "dashboard bundled" test -f "$release/ui/dashboard/dist/index.html"
  check "no download caches" absent "$root/opt/nox/.cache"
  check "/etc/nox-os-release" grep -q '^NOX_OS_VERSION=' "$root/etc/nox-os-release"
  check "hostname is nox" grep -qx 'nox' "$root/etc/hostname"
  check "Nox source fetches from $REPO_URL, no credentials" source_is_clean

  # Nothing of Nox's own: the Pi makes its certificates, keys and pairings on its first start.
  check "Nox data is only the settings file" nox_data_is_settings_only

  # No account can log in, nobody has a key, SSH is off.
  check "no password in /etc/shadow" none < <(awk -F: '$2 != "*" && $2 != "!" && $2 != "!*" && $2 != "!!" {print $1}' "$root/etc/shadow")
  check "no password in /etc/passwd" none < <(awk -F: '$2 != "x" {print $1}' "$root/etc/passwd")
  check "no NOPASSWD sudo" none < <(grep -rls 'NOPASSWD' "$root/etc/sudoers" "$root/etc/sudoers.d" 2>/dev/null)
  check "no authorized_keys" none < <(find "$root/root" "$root/home" -name 'authorized_keys*' 2>/dev/null)
  check "no SSH host keys (made per device on first boot)" none < <(find "$root/etc/ssh" -name 'ssh_host_*' 2>/dev/null)
  check "ssh.service, ssh.socket, sshswitch.service masked" masked ssh.service ssh.socket sshswitch.service
  check "no private keys" none < <(grep -rlI -E -e '-----BEGIN [A-Z0-9 ]*PRIVATE KEY' \
    "$root/etc" "$root/root" "$root/home" "$root/var/lib" "$root/opt/nox/state" "$boot" 2>/dev/null)
  check "no GitHub tokens anywhere" none < <(grep -rlI -E \
    -e '(ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{36}' -e 'github_pat_[A-Za-z0-9_]{40,}' "$root" "$boot" 2>/dev/null)

  # Seeds and secrets of the system are made per device.
  check "machine-id made on first boot" none < <(grep -vx 'uninitialized' "$root/etc/machine-id")
  check "no D-Bus machine id, random seed, credential secret, NetworkManager key" absent \
    "$root/var/lib/dbus/machine-id" "$root/var/lib/systemd/random-seed" \
    "$root/var/lib/systemd/credential.secret" "$root/var/lib/NetworkManager/secret_key"
  check "no saved Wi-Fi or network secrets" none < <(find "$root/etc/NetworkManager/system-connections" -type f 2>/dev/null)

  # Nothing left over from the build.
  check "no shell history, caches or git identity of the build" none < <(find "$root/root" "$root/home" \
    -maxdepth 2 \( -name '.bash_history' -o -name '.cache' -o -name '.gitconfig' -o -name '.git-credentials' \) 2>/dev/null)
  check "no apt credentials" none < <(find "$root/etc/apt" -maxdepth 2 \( -name 'auth.conf' -o -path '*/auth.conf.d/*' \) 2>/dev/null)
  check "no apt proxy" none < <(grep -rliE 'Acquire::(https?|ftp)::Proxy' "$root/etc/apt/apt.conf" "$root/etc/apt/apt.conf.d" 2>/dev/null)

  # Nothing on the boot partition that configures users, SSH or networks, or runs a program.
  check "no provisioning seeds on the boot partition" none < <(find "$boot" -maxdepth 1 \
    \( -iname 'user-data' -o -iname 'meta-data' -o -iname 'network-config' -o -iname 'ssh' \
       -o -iname 'ssh.txt' -o -iname 'userconf*' -o -iname 'wpa_supplicant.conf' -o -iname 'firstrun.sh' \
       -o -iname 'custom.toml' \))
  check "cmdline.txt runs nothing extra (no systemd.run=, init=)" cmdline_is_plain

  # Packages Nox-OS leaves out.
  check "cloud-init not installed" absent "$root/usr/bin/cloud-init"
  check "Raspberry Pi Connect not installed" absent "$root/usr/bin/rpi-connect"
  check "userconf-pi not installed" absent "$root/usr/lib/userconf-pi"
}

if [ "${1:-}" = "--root" ]; then
  root="${2:?--root <dir>}"
  [ "${3:-}" = "--boot" ] || { echo "usage: check-image.sh --root <dir> --boot <dir>" >&2; exit 2; }
  boot="${4:?--boot <dir>}"
  run_checks
  if [ "$failures" -gt 0 ]; then printf '%s check(s) failed.\n' "$failures" >&2; exit 1; fi
  echo "All image checks passed."
  exit 0
fi

image="${1:?usage: check-image.sh <image.img> <out folder> | --root <dir> --boot <dir>}"
out="${2:?usage: check-image.sh <image.img> <out folder>}"
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

run_checks

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
