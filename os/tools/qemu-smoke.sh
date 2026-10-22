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
