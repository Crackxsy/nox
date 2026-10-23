#!/usr/bin/env bash
# Boot a Nox-OS image headless in QEMU (an emulated Raspberry Pi 3) until Nox answers (os/README.md).
#
#   sudo os/tools/qemu-smoke.sh <image.img> <boot files folder> [timeout seconds per boot, default 2700]
#
# Two boots, each from a fresh copy of the image (O3): "provisioned" - with a provisioning file
# on the boot partition (fictitious Wi-Fi, the card's claim code and hotspot password): imported
# and deleted, setup window, hotspot profile, the portal as a phone in a network namespace sees
# it, the claim, then the O2 checks on the claimed device; and "fresh" - no provisioning file and
# a test-only setup window of 600 s: claim code and hotspot password made on the device, the
# console shows them, the window closes by itself and takes them along, a claim after it is
# refused. NOX_SMOKE_ONLY=provisioned|fresh runs one of them.
#
# <boot files folder>: kernel8.img and bcm2710-rpi-3-b-plus.dtb, as os/tools/check-image.sh takes
# them out. The image itself is never changed: a copy boots, with one CI-only unit added that waits
# for nox.service and its /health answer, checks what the first boot made (host keys, machine id)
# and what it must not have (SSH running), and writes the result and the journal of the boot to
# the copy's disk. The result is read from there - the emulated Pi 3's serial console falls silent
# early in the boot - while the machine runs, so the test ends as soon as it is known.
# Exit 0 only for NOX-SMOKE-OK. NOX_SMOKE_LOG=<file> keeps the serial log.
set -euo pipefail

image="${1:?usage: qemu-smoke.sh <image.img> <boot files folder> [timeout seconds]}"
bootfiles="${2:?usage: qemu-smoke.sh <image.img> <boot files folder> [timeout seconds]}"
timeout="${3:-2700}"
[ "$(id -u)" -eq 0 ] || { echo "qemu-smoke.sh: needs root (loop devices)" >&2; exit 1; }
for tool in qemu-system-aarch64 qemu-img fdtget fdtput debugfs partx; do
  command -v "$tool" >/dev/null || { echo "qemu-smoke.sh: $tool is missing (qemu-system-arm, qemu-utils, device-tree-compiler)" >&2; exit 1; }
done

work="$(mktemp -d "${TMPDIR:-/tmp}/nox-smoke.XXXXXX")"
qemu_pid=""
loop=""
probe=""
cleanup() {
  [ -z "$qemu_pid" ] || kill "$qemu_pid" 2>/dev/null || true
  umount "$work/boot" 2>/dev/null || true
  umount "$work/root" 2>/dev/null || true
  [ -z "$loop" ] || losetup -d "$loop" 2>/dev/null || true
  [ -z "$probe" ] || losetup -d "$probe" 2>/dev/null || true
  rm -rf -- "$work"
}
trap cleanup EXIT
mkdir "$work/root" "$work/boot"
cat > "$work/nox-smoke" <<'SMOKE'
#!/bin/sh
# CI only (os/tools/qemu-smoke.sh) - never part of a published image.
out=/var/log/nox-smoke.log
say() { echo "$*"; echo "$*" >> "$out"; }
finish() {
  {
    echo "---- nox.service ----"
    systemctl status nox.service --no-pager -l 2>&1 | head -n 15
    journalctl -u nox.service -b --no-pager -n 60 2>&1
    echo "---- failed units ----"
    systemctl --failed --no-pager 2>&1
    echo "---- warnings and errors of this boot ----"
    journalctl -b -p warning --no-pager -n 60 2>&1
  } > /var/log/nox-smoke-journal.txt
  say "$1"
  sync
  exit 0
}
# One request to the root helper, as root (the helper also answers nox.service's main process).
ask() {
  python3 -I -S -c 'import socket, sys
s = socket.socket(socket.AF_UNIX)
s.settimeout(120)
s.connect("/run/nox-os/helper/helper.sock")
s.sendall(sys.argv[1].encode())
s.shutdown(socket.SHUT_WR)
print(s.makefile().readline().strip())' "$1" 2>&1
}
listening22() { ss -Htln 'sport = :22' 2>/dev/null | grep -q .; }
# O2: firewall, SSH off and its switch, SD-card wear, kernel settings, the console status.
o2_checks() {
  rules="$(nft list table inet nox_os 2>&1)"
  echo "$rules" | grep -q 'hook input priority filter; policy drop;' \
    || finish "NOX-SMOKE-FAIL: firewall not loaded or not default deny: $(echo "$rules" | head -n 3)"
  # The image's settings switch network access on: the generator must have opened the dashboard.
  echo "$rules" | grep -q 'tcp dport 47843 ct state new accept' \
    || finish "NOX-SMOKE-FAIL: the firewall did not open the dashboard port from Nox's settings"
  if echo "$rules" | grep -q 'dport 22 .*accept'; then finish "NOX-SMOKE-FAIL: port 22 open with SSH off"; fi
  echo "$rules" | grep -qF 'iif "lo" tcp dport 22 drop' || finish "NOX-SMOKE-FAIL: SSH from the device itself not dropped"
  if systemctl is-active --quiet nox-ssh.service || listening22; then finish "NOX-SMOKE-FAIL: SSH is listening"; fi
  say "NOX-SMOKE: firewall default deny, dashboard open, SSH off"
  [ ! -d /var/log/journal ] || finish "NOX-SMOKE-FAIL: the journal is on the card"
  findmnt -no OPTIONS / | grep -q noatime || finish "NOX-SMOKE-FAIL: / without noatime"
  [ "$(findmnt -no FSTYPE /tmp)" = tmpfs ] && [ "$(findmnt -no FSTYPE /var/tmp)" = tmpfs ] \
    || finish "NOX-SMOKE-FAIL: /tmp or /var/tmp not in memory"
  if swapon --show=NAME --noheadings 2>/dev/null | grep -qv '^/dev/zram'; then finish "NOX-SMOKE-FAIL: swap on the card"; fi
  [ "$(sysctl -n kernel.kptr_restrict)" = 2 ] || finish "NOX-SMOKE-FAIL: sysctl hardening not applied"
  say "NOX-SMOKE: journal in memory, / $(findmnt -no OPTIONS /), swap: $(swapon --show=NAME --noheadings | tr '\n' ' ')"
  systemctl is-active --quiet nox-console.service || finish "NOX-SMOKE-FAIL: console status not running"
  screen="$(python3 -I -S /usr/local/lib/nox-os/nox_console.py --once 2>&1)"
  echo "$screen" | grep -q '^Nox-OS ' && echo "$screen" | grep -q 'Remote access:  SSH off' \
    || finish "NOX-SMOKE-FAIL: console status: $(echo "$screen" | head -n 3)"
  echo "$screen" | sed 's/^/NOX-SMOKE: console | /' >> "$out"
  # The switch, end to end through the root helper. The user nox cannot reach the socket at all
  # (group nox-os-helper: only nox.service carries it) ...
  probe='import socket
s = socket.socket(socket.AF_UNIX); s.connect("/run/nox-os/helper/helper.sock")
s.sendall(b"status"); s.shutdown(socket.SHUT_WR); print(s.makefile().readline().strip())'
  answer="$(runuser -u nox -- python3 -I -S -c "$probe" 2>&1)"
  echo "$answer" | grep -q 'PermissionError' || finish "NOX-SMOKE-FAIL: the user nox reached the helper: $answer"
  # ... and a process that inherited the group (a command the core ran) is not its main process.
  answer="$(setpriv --reuid=nox --regid=nox --groups=nox-os-helper python3 -I -S -c "$probe" 2>&1)"
  echo "$answer" | grep -q '"not allowed"' || finish "NOX-SMOKE-FAIL: the helper answered another process: $answer"
  [ -z "$(getent group nox-os-helper | cut -d: -f4)" ] || finish "NOX-SMOKE-FAIL: the group nox-os-helper has members"
  systemctl show --property=SupplementaryGroups --value nox.service | grep -qw nox-os-helper \
    || finish "NOX-SMOKE-FAIL: nox.service does not carry the helper's group"
  # ... and the core is out of that user's reach: not dumpable, so its /proc files belong to root
  # and another process of the user nox cannot read them (or its memory).
  tries=0
  while :; do
    main="$(systemctl show --property=MainPID --value nox.service)"
    if [ "$(stat -c %U "/proc/$main/environ" 2>/dev/null)" = root ] \
      && ! setpriv --reuid=nox --regid=nox --clear-groups cat "/proc/$main/environ" >/dev/null 2>&1; then
      break
    fi
    [ "$tries" -lt 30 ] || finish "NOX-SMOKE-FAIL: the core is dumpable (pid $main)"
    sleep 5
    tries=$((tries + 1))
  done
  # The helper's own peer check against the real core: its pidfd, cgroup and start time pass;
  # another process of the same user does not.
  verdict="$(python3 -I -S -c 'import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("nox_os_helper", "/usr/local/lib/nox-os/nox_os_helper.py")
h = importlib.util.module_from_spec(spec); sys.modules[spec.name] = h; spec.loader.exec_module(h)
s = h.System(); main = int(sys.argv[1]); uid = s.uid_of("nox")
core = h.peer_allowed(h.Peer(main, uid, os.pidfd_open(main)), s)
fallback = h.peer_allowed(h.Peer(main, uid, None), s)
other = h.peer_allowed(h.Peer(os.getpid(), uid, os.pidfd_open(os.getpid())), s)
print(f"core={core} fallback={fallback} other={other}")' "$main" 2>&1)"
  [ "$verdict" = "core=True fallback=True other=False" ] || finish "NOX-SMOKE-FAIL: peer check: $verdict"
  say "NOX-SMOKE: helper peer check on the real core: $verdict"
  # A real request through the core: `noxos.ssh.status` as the local shell, which the core answers
  # by asking the sandboxed helper itself - the path the dashboard takes, not root's shortcut.
  answer="$(core_call noxos.ssh.status)"
  echo "$answer" | grep -q '"available": true' && echo "$answer" | grep -q '"passphrase_set": true' \
    || finish "NOX-SMOKE-FAIL: noxos.ssh.status through the core: $answer"
  say "NOX-SMOKE: noxos.ssh.status through the core and the sandboxed helper: available, passphrase from setup"
  rm -f /run/nox-smoke-key /run/nox-smoke-key.pub
  ssh-keygen -q -t ed25519 -N '' -C smoke -f /run/nox-smoke-key
  fp="$(ssh-keygen -lf /run/nox-smoke-key.pub | cut -d' ' -f2)"
  # The maintenance passphrase came with the setup (the portal, O3): set once, only root's scrypt
  # hash, and never again without the old one - setup mode ended with the claim.
  answer="$(ask "maint set
new $(b64 "another smoke passphrase")")"
  echo "$answer" | grep -q '"code": "not_setup"' || finish "NOX-SMOKE-FAIL: passphrase set outside setup: $answer"
  [ "$(stat -c '%a %U' /var/lib/nox-os/maintenance)" = '600 root' ] || finish "NOX-SMOKE-FAIL: passphrase not root's 0600"
  if grep -rqs "$pass" /var/lib/nox /var/lib/nox-os /run/nox-os; then finish "NOX-SMOKE-FAIL: the passphrase is stored in clear"; fi
  # ... keys stored root-owned, answered with their fingerprints; on only with exactly those.
  answer="$(ask "keys set
pass $(b64 'not the maintenance passphrase')
$(cat /run/nox-smoke-key.pub)")"
  echo "$answer" | grep -q '"code": "passphrase_wrong"' || finish "NOX-SMOKE-FAIL: keys with a wrong passphrase: $answer"
  answer="$(ask "keys set
pass $(b64 "$pass")
$(cat /run/nox-smoke-key.pub)")"
  echo "$answer" | grep -qF "\"$fp\"" || finish "NOX-SMOKE-FAIL: keys set: $answer"
  [ "$(stat -c '%a %U' /var/lib/nox-os/ssh_keys)" = '600 root' ] || finish "NOX-SMOKE-FAIL: stored keys not root's 0600"
  answer="$(ask "ssh on 5 SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
pass $(b64 "$pass")")"
  echo "$answer" | grep -q 'not the ones confirmed' || finish "NOX-SMOKE-FAIL: on with unconfirmed keys: $answer"
  ssh_on() {
    answer="$(ask "ssh on 5 $fp
pass $(b64 "$pass")")"
    echo "$answer" | grep -q '"ssh": "on"' || finish "NOX-SMOKE-FAIL: ssh on: $answer"
    tries=0
    until listening22 || [ "$tries" -ge 30 ]; do sleep 2; tries=$((tries + 1)); done
    systemctl is-active --quiet nox-ssh.service && listening22 \
      || finish "NOX-SMOKE-FAIL: sshd not started: $(journalctl -u nox-ssh.service -b --no-pager -n 20 2>&1)"
  }
  ssh_on
  nft list table inet nox_os | grep -q 'ip saddr @private4 tcp dport 22 ct state new accept' \
    || finish "NOX-SMOKE-FAIL: port 22 not opened for the home network"
  systemctl list-timers --all --no-pager 2>/dev/null | grep -q 'nox-ssh-off-' || finish "NOX-SMOKE-FAIL: no off timer"
  [ -e /run/nox-os/sudoers.d/nox-admin ] || finish "NOX-SMOKE-FAIL: no sudo rule while sshd runs"
  effective="$(sshd -T -f /etc/nox-os/ssh/sshd_config -C user=nox-admin,host=nox,addr=192.0.2.10 2>&1)"
  for want in 'passwordauthentication no' 'permitrootlogin no' 'kbdinteractiveauthentication no' 'authenticationmethods publickey'; do
    echo "$effective" | grep -qx "$want" || finish "NOX-SMOKE-FAIL: sshd: $want missing"
  done
  # The firewall alone drops SSH from the device itself - before sshd (and its AllowUsers) could
  # even answer: a TCP connection to its own addresses never completes.
  tcp22() {
    python3 -I -S -c 'import socket, sys
try:
    socket.create_connection((sys.argv[1], 22), timeout=8).close()
    print("open")
except OSError as exc:
    print(type(exc).__name__)' "$1"
  }
  verdict="$(tcp22 127.0.0.1)"
  [ "$verdict" = TimeoutError ] || finish "NOX-SMOKE-FAIL: SSH from the device itself: $verdict"
  # A home network in a namespace: 10.200.0.0/24 is private, 192.0.2.0/24 (documentation) is not.
  modprobe veth 2>/dev/null || true
  ip netns add nox-lan
  ip link add nox-pi type veth peer name nox-peer
  nmcli device set nox-pi managed no >/dev/null 2>&1 || true
  ip link set nox-peer netns nox-lan
  ip addr add 10.200.0.1/24 dev nox-pi
  ip addr add 192.0.2.1/24 dev nox-pi
  ip link set nox-pi up
  ip -n nox-lan addr add 10.200.0.2/24 dev nox-peer
  ip -n nox-lan addr add 192.0.2.10/24 dev nox-peer
  ip -n nox-lan link set nox-peer up
  ip -n nox-lan link set lo up
  sleep 3
  verdict="$(tcp22 10.200.0.1)"
  [ "$verdict" = TimeoutError ] || finish "NOX-SMOKE-FAIL: SSH from the device's own LAN address: $verdict"
  sshopts="-o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i /run/nox-smoke-key"
  # shellcheck disable=SC2086 # the options are words on purpose
  login="$(ip netns exec nox-lan timeout 90 ssh $sshopts -b 10.200.0.2 nox-admin@10.200.0.1 'id -un; sudo -n true && echo SUDO-OK' 2>&1)"
  echo "$login" | grep -qx nox-admin && echo "$login" | grep -qx SUDO-OK \
    || finish "NOX-SMOKE-FAIL: key login from the home network: $login"
  say "NOX-SMOKE: key login as nox-admin from 10.200.0.2 with sudo -n: ok"
  # shellcheck disable=SC2086
  if ip netns exec nox-lan timeout 60 ssh $sshopts -b 192.0.2.10 nox-admin@192.0.2.1 true >/dev/null 2>&1; then
    finish "NOX-SMOKE-FAIL: SSH login from a non-private address"
  fi
  # The timer's way out, with an open session: the end moves into the past, the expiry runs.
  # shellcheck disable=SC2086
  ip netns exec nox-lan ssh $sshopts -b 10.200.0.2 nox-admin@10.200.0.1 'sleep 600' >/dev/null 2>&1 &
  session=$!
  tries=0
  until loginctl list-sessions --no-legend 2>/dev/null | grep -q nox-admin || [ "$tries" -ge 30 ]; do sleep 1; tries=$((tries + 1)); done
  loginctl list-sessions --no-legend 2>/dev/null | grep -q nox-admin || finish "NOX-SMOKE-FAIL: no SSH session to end"
  python3 -I -S -c 'import json
p = "/run/nox-os/ssh.json"
s = json.load(open(p)); s["until"] = 1
open(p, "w").write(json.dumps(s))'
  python3 -I -S /usr/local/lib/nox-os/nox_os_helper.py --expire
  tries=0
  while kill -0 "$session" 2>/dev/null && [ "$tries" -lt 30 ]; do sleep 1; tries=$((tries + 1)); done
  if kill -0 "$session" 2>/dev/null; then kill "$session"; finish "NOX-SMOKE-FAIL: the open session outlived the expiry"; fi
  if loginctl list-sessions --no-legend 2>/dev/null | grep -q nox-admin; then finish "NOX-SMOKE-FAIL: nox-admin still logged in"; fi
  if systemctl is-active --quiet nox-ssh.service || listening22; then finish "NOX-SMOKE-FAIL: sshd outlived the expiry"; fi
  [ ! -e /run/nox-os/ssh/authorized_keys ] && [ ! -e /run/nox-os/sudoers.d/nox-admin ] \
    || finish "NOX-SMOKE-FAIL: keys or sudo rule left after the expiry"
  say "NOX-SMOKE: expiry ended sshd and the open session, keys and sudo rule gone"
  # And `ssh off` from the dashboard's side.
  ssh_on
  answer="$(ask "ssh off")"
  echo "$answer" | grep -q '"ssh": "off"' || finish "NOX-SMOKE-FAIL: ssh off: $answer"
  sleep 2
  if systemctl is-active --quiet nox-ssh.service || listening22; then finish "NOX-SMOKE-FAIL: sshd still running"; fi
  [ ! -e /run/nox-os/ssh/authorized_keys ] && [ ! -e /run/nox-os/sudoers.d/nox-admin ] \
    || finish "NOX-SMOKE-FAIL: keys or sudo rule left behind"
  if nft list table inet nox_os | grep -q 'dport 22 .*accept'; then finish "NOX-SMOKE-FAIL: port 22 left open"; fi
  ip netns del nox-lan 2>/dev/null || true
  ip link del nox-pi 2>/dev/null || true
  ask "keys clear" >/dev/null
  rm -f /run/nox-smoke-key /run/nox-smoke-key.pub
  say "NOX-SMOKE: SSH off again: sshd stopped, keys and sudo rule gone, port 22 closed"
}
# ---- O3: first boot without a screen ---------------------------------------------------------
scenario="$(cat /etc/nox-smoke-scenario 2>/dev/null || echo fresh)"
b64() { printf '%s' "$1" | base64 -w0; }
uptime_s() { cut -d. -f1 /proc/uptime; }
mode_of() { stat -c '%a %U %G' "$1" 2>/dev/null; }
# A JSON field of a file (python3 -I -S, as root).
field() {
  python3 -I -S -c 'import json, sys
try:
    v = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    v = None
for key in sys.argv[2].split("."):
    v = v.get(key) if isinstance(v, dict) else None
print("" if v is None else v if isinstance(v, str) else json.dumps(v))' "$1" "$2"
}
# One request through the core, as the local shell (the path the dashboard takes).
core_call() {
  NOX_HOME=/var/lib/nox HOME=/var/lib/nox /opt/nox/current/.venv/bin/python -I -c 'import asyncio, json, sys
from pathlib import Path
from nox.cli import _config
from nox.lan.terminal import _call, _text
print(json.dumps(asyncio.run(_call(Path(_config().paths.runtime_dir), sys.argv[1], {}, _text("en")))))' "$1" 2>&1 | tail -n 1
}
# The claim, the way the claim page sends it: same origin, JSON, over the listener's TLS.
claim() {
  curl -sk -o /run/nox-smoke-claim.json -w '%{http_code}' -H 'Origin: https://127.0.0.1:47843' \
    -H 'Content-Type: application/json' --data "$1" https://127.0.0.1:47843/api/claim
}
status_code() { curl -sk -o /dev/null -w '%{http_code}' "$@"; }
# What the setup window looks like right at boot - before Nox is even up (the window counts from
# power-on).
o3_at_boot() {
  systemctl is-active --quiet nox-firstboot.service \
    || finish "NOX-SMOKE-FAIL: nox-firstboot.service: $(systemctl status nox-firstboot.service --no-pager 2>&1 | tail -n 5)"
  [ "$(systemctl is-enabled systemd-firstboot.service 2>/dev/null)" = masked ] \
    || finish "NOX-SMOKE-FAIL: systemd-firstboot.service not masked"
  if systemctl is-failed --quiet systemd-firstboot.service; then finish "NOX-SMOKE-FAIL: systemd-firstboot failed"; fi
  [ "$(mode_of /run/nox-os/setup-mode)" = '644 root root' ] || finish "NOX-SMOKE-FAIL: no setup window at boot"
  [ "$(field /run/nox-os/setup-mode reason)" = unclaimed ] || finish "NOX-SMOKE-FAIL: setup window reason"
  [ "$(field /run/nox-os/setup-mode boot_id)" = "$(cat /proc/sys/kernel/random/boot_id)" ] \
    || finish "NOX-SMOKE-FAIL: setup window not this boot's"
  [ "$(mode_of /var/lib/nox-os/claim.json)" = '600 root root' ] || finish "NOX-SMOKE-FAIL: claim code hash not root's 0600"
  [ "$(mode_of /run/nox-os/setup/console.json)" = '640 root nox-os-setup' ] \
    || finish "NOX-SMOKE-FAIL: setup secrets for the console: $(mode_of /run/nox-os/setup/console.json)"
  if [ -e /var/lib/nox-os-claim/claimed ]; then finish "NOX-SMOKE-FAIL: claimed before anyone claimed"; fi
  say "NOX-SMOKE: setup window open at boot until $(field /run/nox-os/setup-mode until) s, uptime $(uptime_s) s"
}
# The hotspot's profile: written when setup mode wants the hotspot (QEMU has no Wi-Fi radio, so it
# stays a profile - in RAM, root only, WPA2 with this device's password).
o3_hotspot_profile() {
  profile=/run/NetworkManager/system-connections/nox-setup-hotspot.nmconnection
  tries=0
  until [ -e "$profile" ] || [ "$tries" -ge 30 ]; do sleep 5; tries=$((tries + 1)); done
  [ "$(mode_of "$profile")" = '600 root root' ] || finish "NOX-SMOKE-FAIL: no hotspot profile (or not root's 0600)"
  for want in 'mode=ap' 'key-mgmt=wpa-psk' 'proto=rsn' 'method=shared' 'address1=10.42.0.1/24' "psk=$1"; do
    grep -qxF "$want" "$profile" || finish "NOX-SMOKE-FAIL: hotspot profile without $want"
  done
  [ "$(field /run/nox-os/setup/status.json hotspot)" = no_wifi_device ] \
    || finish "NOX-SMOKE-FAIL: hotspot state $(field /run/nox-os/setup/status.json hotspot)"
  say "NOX-SMOKE: setup hotspot profile ready ($(field /run/nox-os/setup/console.json ssid), WPA2, 10.42.0.1/24), no radio in QEMU"
}
# Unclaimed: the dashboard port serves the claim only; the node port is closed.
o3_unclaimed_firewall() {
  # The listener comes up a little after /health (it makes its certificates first).
  tries=0
  until [ "$(status_code https://127.0.0.1:47843/api/lan/info)" = 200 ] || [ "$tries" -ge 60 ]; do
    sleep 3
    tries=$((tries + 1))
  done
  rules="$(nft list table inet nox_os 2>&1)"
  echo "$rules" | grep -q 'tcp dport 47843 ct state new accept' || finish "NOX-SMOKE-FAIL: unclaimed: claim port closed"
  if echo "$rules" | grep -q 'dport 47844'; then finish "NOX-SMOKE-FAIL: unclaimed: node port open"; fi
  if echo "$rules" | grep -q 'dport 80 '; then finish "NOX-SMOKE-FAIL: setup port 80 open without a hotspot"; fi
  [ "$(status_code https://127.0.0.1:47843/dashboard/)" = 302 ] || finish "NOX-SMOKE-FAIL: unclaimed dashboard served"
  [ "$(status_code https://127.0.0.1:47843/claim/)" = 200 ] || finish "NOX-SMOKE-FAIL: no claim page"
  [ "$(status_code -X POST -H 'Origin: https://127.0.0.1:47843' https://127.0.0.1:47843/api/pair)" = 403 ] \
    || finish "NOX-SMOKE-FAIL: unclaimed pairing answered"
  say "NOX-SMOKE: unclaimed: firewall opens the claim port only (no node port), Nox serves the claim only"
}
o3_provisioned() {
  [ ! -e /boot/firmware/nox-provision.json ] || finish "NOX-SMOKE-FAIL: provisioning file not deleted"
  [ "$(field /run/nox-os/setup/status.json provision.imported)" = true ] \
    || finish "NOX-SMOKE-FAIL: provisioning not imported: $(cat /run/nox-os/setup/status.json)"
  keyfile=/etc/NetworkManager/system-connections/nox-wifi.nmconnection
  [ "$(mode_of "$keyfile")" = '600 root root' ] || finish "NOX-SMOKE-FAIL: Wi-Fi keyfile not root's 0600"
  grep -qxF "psk=$SMOKE_WIFI_PSK" "$keyfile" || finish "NOX-SMOKE-FAIL: Wi-Fi password not in the keyfile"
  [ "$(readlink /etc/localtime)" = /usr/share/zoneinfo/Europe/Berlin ] || finish "NOX-SMOKE-FAIL: time zone"
  [ "$(cat /etc/hostname)" = nox ] || finish "NOX-SMOKE-FAIL: hostname"
  [ "$(field /var/lib/nox-os/claim.json source)" = card ] || finish "NOX-SMOKE-FAIL: the card's claim code was not kept"
  [ ! -e /run/nox-os/setup/claim-code ] || finish "NOX-SMOKE-FAIL: a device code next to the card's"
  if journalctl -b --no-pager 2>/dev/null | grep -qF "$SMOKE_WIFI_PSK"; then finish "NOX-SMOKE-FAIL: the Wi-Fi password is in the journal"; fi
  if grep -rqsF -D skip "$SMOKE_WIFI_PSK" /run/nox-os /run/NetworkManager /var/lib/nox /var/log; then finish "NOX-SMOKE-FAIL: the Wi-Fi password outside its keyfile"; fi
  say "NOX-SMOKE: provisioning imported (Wi-Fi keyfile root 0600, time zone, claim code hash), file deleted, password in no log"
}
# The portal as a phone sees it: a phone in a namespace on a virtual hotspot interface.
o3_portal() {
  modprobe veth 2>/dev/null || true
  ip netns add nox-phone
  ip link add noxap0 type veth peer name nox-phone0
  nmcli device set noxap0 managed no >/dev/null 2>&1 || true
  ip link set nox-phone0 netns nox-phone
  ip addr add 10.42.0.1/24 dev noxap0
  ip link set noxap0 up
  ip -n nox-phone addr add 10.42.0.20/24 dev nox-phone0
  ip -n nox-phone link set nox-phone0 up
  ip -n nox-phone link set lo up
  printf '{"iface": "noxap0", "address": "10.42.0.1", "prefix": 24}\n' > /run/nox-os/setup/hotspot.json
  chmod 0644 /run/nox-os/setup/hotspot.json
  systemctl restart nox-firewall.service
  rules="$(nft list table inet nox_os 2>&1)"
  echo "$rules" | grep -qF 'iifname "noxap0" ip saddr 10.42.0.0/24 tcp dport 80 accept' \
    || finish "NOX-SMOKE-FAIL: no portal port on the hotspot: $rules"
  echo "$rules" | grep -qF 'iifname "noxap0" drop' || finish "NOX-SMOKE-FAIL: the hotspot reaches more than the portal"
  systemctl start nox-setup-portal.service
  tries=0
  until ss -Htln 'sport = :80' | grep -q '10.42.0.1:80' || [ "$tries" -ge 20 ]; do sleep 1; tries=$((tries + 1)); done
  [ "$(systemctl show -p User --value nox-setup-portal.service)" = nox-setup ] || finish "NOX-SMOKE-FAIL: portal user"
  phone() { ip netns exec nox-phone curl -s --max-time 10 "$@"; }
  answer="$(phone -o /dev/null -w '%{http_code} %{redirect_url}' -H 'Host: connectivitycheck.gstatic.com' http://10.42.0.1/generate_204)"
  [ "$answer" = '302 http://10.42.0.1/' ] || finish "NOX-SMOKE-FAIL: captive probe: $answer"
  page="$(phone -D /run/nox-smoke-headers.txt http://10.42.0.1/)"
  grep -qi "^content-security-policy: default-src 'none'" /run/nox-smoke-headers.txt || finish "NOX-SMOKE-FAIL: portal CSP"
  token="$(printf '%s' "$page" | sed -n 's/.*name="token" value="\([^"]*\)".*/\1/p' | head -n1)"
  [ -n "$token" ] || finish "NOX-SMOKE-FAIL: no form token: $(printf '%s' "$page" | head -c 300)"
  secrets="network=__cable__&pin=$SMOKE_PIN&pin2=$SMOKE_PIN&passphrase=$(printf '%s' "$pass" | sed 's/ /+/g')&passphrase2=$(printf '%s' "$pass" | sed 's/ /+/g')"
  form="token=$token&lang=en&claim_code=$SMOKE_CLAIM_CODE&$secrets"
  code="$(phone -o /dev/null -w '%{http_code}' --data "$form" http://10.42.0.1/setup)"
  [ "$code" = 403 ] || finish "NOX-SMOKE-FAIL: a post without Origin: $code"
  # A wrong claim code: the portal passes it on, root keeps and applies nothing.
  code="$(phone -o /dev/null -w '%{http_code}' -H 'Origin: http://10.42.0.1' \
    --data "token=$token&lang=en&claim_code=ZZZZ-ZZZZ-ZZZZ&$secrets" http://10.42.0.1/setup)"
  [ "$code" = 200 ] || finish "NOX-SMOKE-FAIL: portal form (wrong code): $code"
  tries=0
  until [ "$(field /run/nox-os/setup/status.json error)" = claim_wrong ] || [ "$tries" -ge 30 ]; do sleep 1; tries=$((tries + 1)); done
  [ "$(field /run/nox-os/setup/status.json error)" = claim_wrong ] || finish "NOX-SMOKE-FAIL: a wrong code in the portal: $(cat /run/nox-os/setup/status.json)"
  [ ! -e /run/nox-os/setup/pending.json ] && [ ! -e /var/lib/nox-os/maintenance ] \
    || finish "NOX-SMOKE-FAIL: a wrong claim code in the portal kept or set something"
  sleep 3  # that phone's address now waits a moment before its next code
  code="$(phone -o /run/nox-smoke-done.html -w '%{http_code}' -H 'Origin: http://10.42.0.1' --data "$form" http://10.42.0.1/setup)"
  [ "$code" = 200 ] && grep -q 'nox.local:47843/claim/' /run/nox-smoke-done.html || finish "NOX-SMOKE-FAIL: portal form: $code"
  tries=0
  until [ -e /run/nox-os/setup/pending.json ] || [ "$tries" -ge 30 ]; do sleep 1; tries=$((tries + 1)); done
  [ "$(mode_of /run/nox-os/setup/pending.json)" = '600 root root' ] || finish "NOX-SMOKE-FAIL: the portal's secrets not kept root-only"
  [ ! -e /run/nox-os/setup/portal/request.json ] || finish "NOX-SMOKE-FAIL: the portal's request left behind"
  # Kept, not applied: the passphrase is set only by the claim.
  [ ! -e /var/lib/nox-os/maintenance ] || finish "NOX-SMOKE-FAIL: the portal set the passphrase before the claim"
  verdict="$(ip netns exec nox-phone python3 -I -S -c 'import socket
try:
    socket.create_connection(("10.42.0.1", 47843), timeout=6).close()
    print("open")
except OSError as exc:
    print(type(exc).__name__)')"
  [ "$verdict" = TimeoutError ] || finish "NOX-SMOKE-FAIL: Nox's port from the hotspot: $verdict"
  say "NOX-SMOKE: portal: captive redirect, CSP, Origin checked, wrong claim code keeps nothing, right one keeps PIN and passphrase root-only (unapplied), Nox's port closed to the hotspot"
}
o3_claim() {
  code="$(claim '{"code": "ZZZZ-ZZZZ-ZZZZ", "name": "Smoke phone"}')"
  [ "$code" = 401 ] || finish "NOX-SMOKE-FAIL: a wrong claim code: $code $(cat /run/nox-smoke-claim.json)"
  [ ! -e /var/lib/nox-os/maintenance ] || finish "NOX-SMOKE-FAIL: a wrong claim applied the passphrase"
  sleep 3  # the slow-down of this address after a wrong code
  status="$(curl -sk https://127.0.0.1:47843/api/claim)"
  echo "$status" | grep -q '"needs_pin": *false' && echo "$status" | grep -q '"needs_passphrase": *false' \
    || finish "NOX-SMOKE-FAIL: claim status after the portal: $status"
  code="$(claim "{\"code\": \"$SMOKE_CLAIM_CODE\", \"name\": \"Smoke phone\"}")"
  [ "$code" = 201 ] && grep -q '"token"' /run/nox-smoke-claim.json \
    || finish "NOX-SMOKE-FAIL: the claim: $code $(cat /run/nox-smoke-claim.json)"
  [ "$(mode_of /var/lib/nox-os-claim/claimed)" = '644 root root' ] || finish "NOX-SMOKE-FAIL: no root claim mark"
  [ ! -e /run/nox-os/setup-mode ] || finish "NOX-SMOKE-FAIL: setup mode outlived the claim"
  [ ! -e /run/nox-os/setup/pending.json ] || finish "NOX-SMOKE-FAIL: the portal's secrets stayed kept"
  [ "$(mode_of /var/lib/nox-os/maintenance)" = '600 root root' ] || finish "NOX-SMOKE-FAIL: the claim did not set the portal's passphrase"
  tries=0
  while systemctl is-active --quiet nox-setup-portal.service && [ "$tries" -lt 30 ]; do sleep 1; tries=$((tries + 1)); done
  if systemctl is-active --quiet nox-setup-portal.service; then finish "NOX-SMOKE-FAIL: the portal outlived the claim"; fi
  for gone in /run/nox-os/setup/hotspot.json /run/nox-os/setup/console.json /var/lib/nox-os/claim.json \
    /run/NetworkManager/system-connections/nox-setup-hotspot.nmconnection; do
    [ ! -e "$gone" ] || finish "NOX-SMOKE-FAIL: $gone left after the claim"
  done
  rules="$(nft list table inet nox_os 2>&1)"
  echo "$rules" | grep -q 'tcp dport 47844 ct state new accept' || finish "NOX-SMOKE-FAIL: claimed: node port still closed"
  if echo "$rules" | grep -q 'iifname "noxap0"'; then finish "NOX-SMOKE-FAIL: setup ports after the claim"; fi
  [ "$(status_code https://127.0.0.1:47843/dashboard/)" = 200 ] || finish "NOX-SMOKE-FAIL: claimed dashboard not served"
  [ "$(claim "{\"code\": \"$SMOKE_CLAIM_CODE\"}")" = 409 ] || finish "NOX-SMOKE-FAIL: a second claim"
  answer="$(core_call security.pin.status)"
  echo "$answer" | grep -q '"configured": true' || finish "NOX-SMOKE-FAIL: the portal's PIN not set: $answer"
  ip netns del nox-phone 2>/dev/null || true
  ip link del noxap0 2>/dev/null || true
  say "NOX-SMOKE: claimed with the card's code: owner paired, PIN set, setup closed (portal, hotspot, secrets), node port open"
}
o3_fresh() {
  shown="$(cat /run/nox-os/setup/claim-code)"
  echo "$shown" | grep -qE '^[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$' \
    || finish "NOX-SMOKE-FAIL: device claim code: $shown"
  [ "$(mode_of /run/nox-os/setup/claim-code)" = '640 root nox-os-setup' ] || finish "NOX-SMOKE-FAIL: claim code file mode"
  psk="$(field /run/nox-os/setup/console.json psk)"
  echo "$psk" | grep -qE '^[0-9]{12}$' || finish "NOX-SMOKE-FAIL: generated hotspot password: $psk"
  [ "$(cat /run/nox-os/setup/blink)" = "$psk" ] && [ "$(mode_of /run/nox-os/setup/blink)" = '600 root root' ] \
    || finish "NOX-SMOKE-FAIL: blink code for the LED"
  [ "$(field /run/nox-os/setup-mode until)" = 600 ] || finish "NOX-SMOKE-FAIL: test window not used"
  o3_hotspot_profile "$psk"
  say "NOX-SMOKE: no provisioning: claim code and hotspot password made on the device"
}
o3_fresh_after_nox() {
  # As the console runs: a throwaway user that reads the setup secrets through its group only.
  screen="$(systemd-run --quiet --wait --pipe -p DynamicUser=yes -p SupplementaryGroups=nox-os-setup \
    /usr/bin/python3 -I -S /usr/local/lib/nox-os/nox_console.py --once 2>&1)"
  echo "$screen" | grep -qF "$shown" && echo "$screen" | grep -q '^Setup Wi-Fi:    Nox-Setup-' || finish "NOX-SMOKE-FAIL: console setup block: $(echo "$screen" | head -n 20)"
  echo "$screen" | sed 's/^/NOX-SMOKE: console | /' | grep -v 'password\|Claim code' >> "$out"
  o3_unclaimed_firewall
  status="$(curl -sk https://127.0.0.1:47843/api/claim)"
  echo "$status" | grep -q '"window_open": *true' || finish "NOX-SMOKE-FAIL: claim status: $status"
  # The window ends by itself (a shortened test window: 600 s after power-on).
  until="$(field /run/nox-os/setup-mode until)"
  while [ "$(uptime_s)" -lt $((until + 45)) ]; do sleep 10; done
  [ ! -e /run/nox-os/setup-mode ] || finish "NOX-SMOKE-FAIL: setup mode outlived its window"
  for gone in /run/nox-os/setup/claim-code /run/nox-os/setup/console.json /run/nox-os/setup/blink \
    /var/lib/nox-os/claim.json /run/NetworkManager/system-connections/nox-setup-hotspot.nmconnection; do
    [ ! -e "$gone" ] || finish "NOX-SMOKE-FAIL: $gone left after the window"
  done
  [ "$(field /run/nox-os/setup/status.json reason)" = expired ] || finish "NOX-SMOKE-FAIL: window close reason"
  if systemctl is-active --quiet nox-setup.timer; then finish "NOX-SMOKE-FAIL: the setup timer outlived the window"; fi
  code="$(claim "{\"code\": \"$shown\", \"pin\": \"$SMOKE_PIN\", \"passphrase\": \"$pass\"}")"
  [ "$code" = 403 ] && grep -q window_closed /run/nox-smoke-claim.json \
    || finish "NOX-SMOKE-FAIL: a claim after the window: $code $(cat /run/nox-smoke-claim.json)"
  answer="$(ask "maint set
new $(b64 "$pass")")"
  echo "$answer" | grep -q '"code": "not_setup"' || finish "NOX-SMOKE-FAIL: passphrase after the window: $answer"
  [ ! -e /var/lib/nox-os-claim/claimed ] || finish "NOX-SMOKE-FAIL: claimed without a claim"
  screen="$(python3 -I -S /usr/local/lib/nox-os/nox_console.py --once 2>&1)"
  if echo "$screen" | grep -q '^Setup'; then finish "NOX-SMOKE-FAIL: console still shows setup"; fi
  say "NOX-SMOKE: window expired at ${until} s: secrets, profile and timer gone, claim and passphrase refused"
}
# Fictitious values only (the same as the provisioning file this script writes).
pass="smoke maintenance passphrase"
SMOKE_PIN="135790"
SMOKE_WIFI_PSK="springfield-smoke-psk"
SMOKE_HOTSPOT_PSK="smoke-setup-hotspot"
SMOKE_CLAIM_CODE="ABCD-EFGH-JKMN"
say "NOX-SMOKE: started ($scenario), waiting for nox.service"
o3_at_boot
case "$scenario" in
  provisioned) o3_provisioned; o3_hotspot_profile "$SMOKE_HOTSPOT_PSK" ;;
  *) o3_fresh ;;
esac
waited=0
while [ "$waited" -lt 2100 ]; do
  if systemctl is-active --quiet nox.service && curl -fsS -o /dev/null http://127.0.0.1:47801/health; then
    say "NOX-SMOKE: nox.service active and /health answers after ${waited}s"
    if systemctl is-active --quiet ssh.service; then finish "NOX-SMOKE-FAIL: ssh.service is running"; fi
    say "NOX-SMOKE: ssh.service $(systemctl is-enabled ssh.service 2>/dev/null || true)"
    if ! ls /etc/ssh/ssh_host_ed25519_key >/dev/null 2>&1; then finish "NOX-SMOKE-FAIL: no SSH host keys made"; fi
    say "NOX-SMOKE: host key $(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | cut -d' ' -f2)"
    if grep -qx uninitialized /etc/machine-id; then finish "NOX-SMOKE-FAIL: machine-id not made"; fi
    if [ "$scenario" = provisioned ]; then
      o3_unclaimed_firewall
      o3_portal
      o3_claim
      o2_checks
    else
      o3_fresh_after_nox
    fi
    finish "NOX-SMOKE-OK $scenario $(. /etc/nox-os-release && echo "$NOX_OS_VERSION")"
  fi
  if [ $((waited % 120)) -eq 0 ]; then
    say "NOX-SMOKE: ${waited}s, nox.service $(systemctl is-active nox.service 2>/dev/null)"
    sync
  fi
  sleep 10
  waited=$((waited + 10))
done
finish "NOX-SMOKE-FAIL: nox did not answer in ${waited}s"
SMOKE

# One boot: a fresh copy of the image, the CI-only unit and the scenario added, until a verdict.
boot_one() {
  local scenario="$1" copy="$work/smoke-$1.img"
  echo "==== boot: $scenario ===="
  cp --sparse=always "$image" "$copy"
  loop="$(losetup --find --show --partscan "$copy")"
  udevadm settle 2>/dev/null || true
  mount "${loop}p2" "$work/root"
  install -m 0755 "$work/nox-smoke" "$work/root/usr/local/sbin/nox-smoke"
  printf '%s\n' "$scenario" > "$work/root/etc/nox-smoke-scenario"
  if [ "$scenario" = fresh ]; then
    # Test images only: a shorter setup window (check-image.sh refuses this folder).
    install -d -m 0755 "$work/root/etc/nox-os/testing"
    echo 600 > "$work/root/etc/nox-os/testing/setup-window-s"
  else
    mount "${loop}p1" "$work/boot"
    # Fictitious data only. claim_hash: sha256("nox-claim-v1:" + "ABCDEFGHJKMN").
    hash="$(printf '%s' 'nox-claim-v1:ABCDEFGHJKMN' | sha256sum | cut -d' ' -f1)"
    cat > "$work/boot/nox-provision.json" <<PROVISION
{"v": 1, "hostname": "nox", "timezone": "Europe/Berlin", "locale": "de_DE", "country": "DE",
 "wifi": {"ssid": "Springfield-WLAN", "psk": "springfield-smoke-psk"},
 "claim_hash": "sha256:$hash", "hotspot_psk": "smoke-setup-hotspot"}
PROVISION
    umount "$work/boot"
  fi
  cat > "$work/root/etc/systemd/system/nox-smoke.service" <<'UNIT'
[Unit]
Description=Nox-OS boot smoke (CI only)
After=nox.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/nox-smoke

[Install]
WantedBy=multi-user.target
UNIT
  ln -s /etc/systemd/system/nox-smoke.service "$work/root/etc/systemd/system/multi-user.target.wants/nox-smoke.service"
  umount "$work/root"
  losetup -d "$loop"
  loop=""

  # QEMU's SD card wants a power-of-two size.
  local size sd=1
  size="$(stat -c %s "$copy")"
  while [ "$sd" -lt "$size" ]; do sd=$((sd * 2)); done
  qemu-img resize -q -f raw "$copy" "$sd"

  # A read-only view of the root partition, to read the result while the machine runs. debugfs
  # reads the file system itself (-D: past every cache), not a mount that would never see changes.
  local start
  start="$(partx -g -o START -n 2 "$copy" | tr -d ' ')"
  probe="$(losetup --read-only --find --show --offset $((start * 512)) "$copy")"
  guest_file() { debugfs -D -R "cat $1" "$probe" 2>/dev/null || true; }

  local log="$work/serial-$scenario.log"
  # The image's own kernel; the command line replaces cmdline.txt: no first-boot resize in a test,
  # and no hardware watchdog - QEMU's raspi model resets the machine the moment one is started.
  local cmdline="earlycon=pl011,0x3f201000 console=ttyAMA0,115200 root=/dev/mmcblk0p2 rootfstype=ext4"
  cmdline="$cmdline rootwait rw dwc_otg.lpm_enable=0 systemd.show_status=1 audit=0"
  cmdline="$cmdline systemd.mask=rpi-resize.service systemd.watchdog_sec=off"
  cmdline="$cmdline modprobe.blacklist=bcm2835_wdt,hci_uart,btbcm initcall_blacklist=bcm2835_wdt_driver_init"
  # On a Pi 3 the PL011 UART (QEMU's only serial port) belongs to Bluetooth: the test boots with a
  # copy of the device tree without the Bluetooth node, and the Bluetooth helpers masked.
  cmdline="$cmdline systemd.mask=hciuart.service systemd.mask=bluetooth.service"
  local dtb="$work/console.dtb" uart0
  cp "$bootfiles/bcm2710-rpi-3-b-plus.dtb" "$dtb"
  uart0="$(fdtget -t s "$dtb" /aliases uart0)"
  fdtput -r "$dtb" "$uart0/bluetooth" 2>/dev/null || echo "no Bluetooth node under $uart0"
  qemu-system-aarch64 -machine raspi3b -m 1G -smp 4 \
    -kernel "$bootfiles/kernel8.img" -dtb "$dtb" \
    -drive "file=$copy,if=sd,format=raw" -append "$cmdline" \
    -display none -serial "file:$log" -monitor none -no-reboot \
    >"$work/qemu.out" 2>&1 &
  qemu_pid=$!

  local verdict="" deadline=$((SECONDS + timeout))
  while [ "$SECONDS" -lt "$deadline" ] && kill -0 "$qemu_pid" 2>/dev/null; do
    sleep 30
    verdict="$(guest_file /var/log/nox-smoke.log | grep -aE '^NOX-SMOKE-(OK|FAIL)' | head -n1 || true)"
    [ -z "$verdict" ] || break
  done
  if [ -z "$verdict" ] && ! kill -0 "$qemu_pid" 2>/dev/null; then echo "QEMU stopped before the smoke finished" >&2; fi
  sleep 5  # the report next to the verdict is complete once the verdict is written; let it settle
  kill "$qemu_pid" 2>/dev/null || true
  wait "$qemu_pid" 2>/dev/null || true
  qemu_pid=""
  grep -v '^SD: Unknown CMD' "$work/qemu.out" | tail -n 10 || true
  [ -z "${NOX_SMOKE_LOG:-}" ] || cp "$log" "${NOX_SMOKE_LOG%.log}-$scenario.log"

  echo "---- /var/log/nox-smoke.log ($scenario) ----"
  guest_file /var/log/nox-smoke.log
  echo "---- /var/log/nox-smoke-journal.txt ($scenario) ----"
  guest_file /var/log/nox-smoke-journal.txt | tail -n 150
  losetup -d "$probe"
  probe=""
  rm -f -- "$copy"
  case "$verdict" in
    NOX-SMOKE-OK*) echo "Boot smoke ($scenario) passed after ${SECONDS}s." ;;
    "") echo "Boot smoke ($scenario) failed: no result within ${timeout}s." >&2; return 1 ;;
    *) echo "Boot smoke ($scenario) failed: $verdict" >&2; return 1 ;;
  esac
}

qemu-system-aarch64 --version | head -n1
failed=0
for scenario in provisioned fresh; do
  [ -z "${NOX_SMOKE_ONLY:-}" ] || [ "$NOX_SMOKE_ONLY" = "$scenario" ] || continue
  boot_one "$scenario" || failed=1
done
exit "$failed"
