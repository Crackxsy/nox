#!/usr/bin/env bash
# Boot a Nox-OS image headless in QEMU (an emulated Raspberry Pi 3) until Nox answers (os/README.md).
#
#   sudo os/tools/qemu-smoke.sh <image.img> <boot files folder> [timeout seconds, default 2700]
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
  umount "$work/root" 2>/dev/null || true
  [ -z "$loop" ] || losetup -d "$loop" 2>/dev/null || true
  [ -z "$probe" ] || losetup -d "$probe" 2>/dev/null || true
  rm -rf -- "$work"
}
trap cleanup EXIT

copy="$work/smoke.img"
cp --sparse=always "$image" "$copy"
mkdir "$work/root"

# The CI-only unit, into the copy.
loop="$(losetup --find --show --partscan "$copy")"
udevadm settle 2>/dev/null || true
mount "${loop}p2" "$work/root"
cat > "$work/root/usr/local/sbin/nox-smoke" <<'SMOKE'
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
  answer="$(NOX_HOME=/var/lib/nox HOME=/var/lib/nox /opt/nox/current/.venv/bin/python -I -c 'import asyncio, json
from pathlib import Path
from nox.cli import _config
from nox.lan.terminal import _call, _text
print(json.dumps(asyncio.run(_call(Path(_config().paths.runtime_dir), "noxos.ssh.status", {}, _text("en")))))' 2>&1 | tail -n 1)"
  echo "$answer" | grep -q '"available": true' && echo "$answer" | grep -q '"passphrase_set": false' \
    || finish "NOX-SMOKE-FAIL: noxos.ssh.status through the core: $answer"
  say "NOX-SMOKE: noxos.ssh.status through the core and the sandboxed helper: available, no passphrase yet"
  b64() { printf '%s' "$1" | base64 -w0; }
  pass="smoke maintenance passphrase"
  rm -f /run/nox-smoke-key /run/nox-smoke-key.pub
  ssh-keygen -q -t ed25519 -N '' -C smoke -f /run/nox-smoke-key
  fp="$(ssh-keygen -lf /run/nox-smoke-key.pub | cut -d' ' -f2)"
  # The maintenance passphrase: nothing works without one, and it is set only in setup mode.
  answer="$(ask "keys set
pass $(b64 "$pass")
$(cat /run/nox-smoke-key.pub)")"
  echo "$answer" | grep -q '"code": "passphrase_missing"' || finish "NOX-SMOKE-FAIL: keys without a passphrase: $answer"
  answer="$(ask "maint set
new $(b64 "$pass")")"
  echo "$answer" | grep -q '"code": "not_setup"' || finish "NOX-SMOKE-FAIL: passphrase set outside setup: $answer"
  touch /run/nox-os/setup-mode
  answer="$(ask "maint set
new $(b64 "$pass")")"
  echo "$answer" | grep -q '"passphrase": true' || finish "NOX-SMOKE-FAIL: passphrase set in setup mode: $answer"
  # Set once: even in setup mode, not again without the old one.
  answer="$(ask "maint set
new $(b64 "another smoke passphrase")")"
  rm -f /run/nox-os/setup-mode
  echo "$answer" | grep -q '"code": "passphrase_exists"' || finish "NOX-SMOKE-FAIL: passphrase replaced without the old one: $answer"
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
say "NOX-SMOKE: started, waiting for nox.service"
waited=0
while [ "$waited" -lt 2100 ]; do
  if systemctl is-active --quiet nox.service && curl -fsS -o /dev/null http://127.0.0.1:47801/health; then
    say "NOX-SMOKE: nox.service active and /health answers after ${waited}s"
    if systemctl is-active --quiet ssh.service; then finish "NOX-SMOKE-FAIL: ssh.service is running"; fi
    say "NOX-SMOKE: ssh.service $(systemctl is-enabled ssh.service 2>/dev/null || true)"
    if ! ls /etc/ssh/ssh_host_ed25519_key >/dev/null 2>&1; then finish "NOX-SMOKE-FAIL: no SSH host keys made"; fi
    say "NOX-SMOKE: host key $(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | cut -d' ' -f2)"
    if grep -qx uninitialized /etc/machine-id; then finish "NOX-SMOKE-FAIL: machine-id not made"; fi
    o2_checks
    finish "NOX-SMOKE-OK $(. /etc/nox-os-release && echo "$NOX_OS_VERSION")"
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
chmod 0755 "$work/root/usr/local/sbin/nox-smoke"
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
size="$(stat -c %s "$copy")"
sd=1
while [ "$sd" -lt "$size" ]; do sd=$((sd * 2)); done
qemu-img resize -q -f raw "$copy" "$sd"

# A read-only view of the root partition, to read the result while the machine runs. debugfs
# reads the file system itself (-D: past every cache), not a mount that would never see changes.
start="$(partx -g -o START -n 2 "$copy" | tr -d ' ')"
probe="$(losetup --read-only --find --show --offset $((start * 512)) "$copy")"
guest_file() { debugfs -D -R "cat $1" "$probe" 2>/dev/null || true; }

log="$work/serial.log"
qemu-system-aarch64 --version | head -n1
# The image's own kernel; the command line replaces cmdline.txt: no first-boot resize in a test,
# and no hardware watchdog - QEMU's raspi model resets the machine the moment one is started.
cmdline="earlycon=pl011,0x3f201000 console=ttyAMA0,115200 root=/dev/mmcblk0p2 rootfstype=ext4"
cmdline="$cmdline rootwait rw dwc_otg.lpm_enable=0 systemd.show_status=1 audit=0"
cmdline="$cmdline systemd.mask=rpi-resize.service systemd.watchdog_sec=off"
cmdline="$cmdline modprobe.blacklist=bcm2835_wdt,hci_uart,btbcm initcall_blacklist=bcm2835_wdt_driver_init"
# On a Pi 3 the PL011 UART (QEMU's only serial port) belongs to Bluetooth: the test boots with a
# copy of the device tree without the Bluetooth node, and the Bluetooth helpers masked.
cmdline="$cmdline systemd.mask=hciuart.service systemd.mask=bluetooth.service"
dtb="$work/console.dtb"
cp "$bootfiles/bcm2710-rpi-3-b-plus.dtb" "$dtb"
uart0="$(fdtget -t s "$dtb" /aliases uart0)"
fdtput -r "$dtb" "$uart0/bluetooth" 2>/dev/null || echo "no Bluetooth node under $uart0"
qemu-system-aarch64 -machine raspi3b -m 1G -smp 4 \
  -kernel "$bootfiles/kernel8.img" -dtb "$dtb" \
  -drive "file=$copy,if=sd,format=raw" -append "$cmdline" \
  -display none -serial "file:$log" -monitor none -no-reboot \
  >"$work/qemu.out" 2>&1 &
qemu_pid=$!

verdict=""
deadline=$((SECONDS + timeout))
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
[ -z "${NOX_SMOKE_LOG:-}" ] || cp "$log" "$NOX_SMOKE_LOG"

echo "---- /var/log/nox-smoke.log ----"
guest_file /var/log/nox-smoke.log
echo "---- /var/log/nox-smoke-journal.txt ----"
guest_file /var/log/nox-smoke-journal.txt | tail -n 150
case "$verdict" in
  NOX-SMOKE-OK*) echo "Boot smoke passed in ${SECONDS}s." ;;
  "") echo "Boot smoke failed: no result within ${timeout}s." >&2; exit 1 ;;
  *) echo "Boot smoke failed: $verdict" >&2; exit 1 ;;
esac
