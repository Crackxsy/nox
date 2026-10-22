#!/usr/bin/env bash
# Boot a Nox-OS image headless in QEMU (an emulated Raspberry Pi 3) until Nox answers (os/README.md).
#
#   sudo os/tools/qemu-smoke.sh <image.img> <boot files folder> [timeout seconds, default 2400]
#
# <boot files folder>: kernel8.img and bcm2710-rpi-3-b-plus.dtb, as os/tools/check-image.sh takes
# them out. The image itself is never changed: a copy boots, with one CI-only unit added that waits
# for nox.service and its /health answer and reports on the serial console what the first boot made
# (host keys, machine id) and what it must not have (SSH running). Exit 0 only for NOX-SMOKE-OK.
set -euo pipefail

image="${1:?usage: qemu-smoke.sh <image.img> <boot files folder> [timeout seconds]}"
bootfiles="${2:?usage: qemu-smoke.sh <image.img> <boot files folder> [timeout seconds]}"
timeout="${3:-2400}"
[ "$(id -u)" -eq 0 ] || { echo "qemu-smoke.sh: needs root (loop devices)" >&2; exit 1; }
command -v qemu-system-aarch64 >/dev/null || { echo "qemu-smoke.sh: qemu-system-aarch64 is missing" >&2; exit 1; }

work="$(mktemp -d "${TMPDIR:-/tmp}/nox-smoke.XXXXXX")"
qemu_pid=""
loop=""
cleanup() {
  [ -z "$qemu_pid" ] || kill "$qemu_pid" 2>/dev/null || true
  umount "$work/root" 2>/dev/null || true
  [ -z "$loop" ] || losetup -d "$loop" 2>/dev/null || true
  rm -rf -- "$work"
}
trap cleanup EXIT

copy="$work/smoke.img"
cp --sparse=always "$image" "$copy"

# The CI-only unit, into the copy.
loop="$(losetup --find --show --partscan "$copy")"
udevadm settle 2>/dev/null || true
mkdir "$work/root"
mount "${loop}p2" "$work/root"
cat > "$work/root/usr/local/sbin/nox-smoke" <<'SMOKE'
#!/bin/sh
# CI only (os/tools/qemu-smoke.sh) - never part of a published image. Every line also goes to the
# kernel log, which reaches the serial port whatever the console setup.
say() { echo "$*"; echo "$*" > /dev/kmsg 2>/dev/null || true; }
dump() { "$@" 2>&1 | while IFS= read -r line; do say "nox-smoke: $line"; done; }
say "NOX-SMOKE: started, waiting for nox.service"
waited=0
while [ "$waited" -lt 2100 ]; do
  if systemctl is-active --quiet nox.service && curl -fsS -o /dev/null http://127.0.0.1:47801/health; then
    say "NOX-SMOKE: nox.service active and /health answers after ${waited}s"
    if systemctl is-active --quiet ssh.service; then say "NOX-SMOKE-FAIL: ssh.service is running"; exit 1; fi
    say "NOX-SMOKE: ssh.service $(systemctl is-enabled ssh.service 2>/dev/null || true)"
    if ! ls /etc/ssh/ssh_host_ed25519_key >/dev/null 2>&1; then say "NOX-SMOKE-FAIL: no SSH host keys made"; exit 1; fi
    say "NOX-SMOKE: host key $(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | cut -d' ' -f2)"
    if grep -qx uninitialized /etc/machine-id; then say "NOX-SMOKE-FAIL: machine-id not made"; exit 1; fi
    say "NOX-SMOKE-OK $(. /etc/nox-os-release && echo "$NOX_OS_VERSION")"
    exit 0
  fi
  if [ $((waited % 120)) -eq 0 ]; then
    say "NOX-SMOKE: ${waited}s, nox.service $(systemctl is-active nox.service 2>/dev/null)"
  fi
  sleep 10
  waited=$((waited + 10))
done
say "NOX-SMOKE-FAIL: nox did not answer in ${waited}s"
dump systemctl status nox.service --no-pager -l
dump journalctl -u nox.service -n 80 --no-pager
dump systemctl --failed --no-pager
exit 1
SMOKE
chmod 0755 "$work/root/usr/local/sbin/nox-smoke"
cat > "$work/root/etc/systemd/system/nox-smoke.service" <<'UNIT'
[Unit]
Description=Nox-OS boot smoke (CI only)
After=nox.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/nox-smoke
StandardOutput=journal+console
StandardError=journal+console

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

log="$work/serial.log"
qemu_log="$work/qemu.log"
qemu-system-aarch64 --version | head -n1
file "$bootfiles/kernel8.img" 2>/dev/null || true
# The image's own kernel; the command line replaces cmdline.txt: no first-boot resize in a test,
# and no hardware watchdog - QEMU's raspi model resets the machine the moment one is started.
# earlycon: the kernel's first lines reach the serial log before its console driver is up.
cmdline="earlycon=pl011,0x3f201000 console=ttyAMA0,115200 root=/dev/mmcblk0p2 rootfstype=ext4"
cmdline="$cmdline rootwait rw dwc_otg.lpm_enable=0 systemd.show_status=1"
cmdline="$cmdline systemd.mask=rpi-resize.service systemd.watchdog_sec=off"
cmdline="$cmdline modprobe.blacklist=bcm2835_wdt initcall_blacklist=bcm2835_wdt_driver_init"
# The boot's own log on the serial port too, so a failure can be read from the CI log.
cmdline="$cmdline printk.devkmsg=on audit=0 systemd.journald.forward_to_kmsg=1"
qemu-system-aarch64 -machine raspi3b -m 1G -smp 4 \
  -kernel "$bootfiles/kernel8.img" -dtb "$bootfiles/bcm2710-rpi-3-b-plus.dtb" \
  -drive "file=$copy,if=sd,format=raw" -append "$cmdline" \
  -display none -serial "file:$log" -monitor none -d guest_errors -D "$qemu_log" -no-reboot \
  >"$work/qemu.out" 2>&1 &
qemu_pid=$!

result=1
deadline=$((SECONDS + timeout))
while [ "$SECONDS" -lt "$deadline" ]; do
  if grep -aq 'NOX-SMOKE-OK' "$log" 2>/dev/null; then result=0; break; fi
  if grep -aq 'NOX-SMOKE-FAIL' "$log" 2>/dev/null; then sleep 20; break; fi
  if ! kill -0 "$qemu_pid" 2>/dev/null; then echo "QEMU stopped before the smoke finished" >&2; break; fi
  sleep 10
done
kill "$qemu_pid" 2>/dev/null || true
qemu_status=0
wait "$qemu_pid" 2>/dev/null || qemu_status=$?
qemu_pid=""

echo "---- QEMU (exit status $qemu_status) ----"
cat "$work/qemu.out" 2>/dev/null || true
grep -v '^SD: Unknown CMD' "$qemu_log" 2>/dev/null | tail -n 10 || true
# NOX_SMOKE_LOG=<file>: keep the whole serial log (CI uploads it when the boot fails).
[ -z "${NOX_SMOKE_LOG:-}" ] || cp "$log" "$NOX_SMOKE_LOG"
echo "---- serial console: nox, failures (first 150) ----"
grep -aiE 'nox|fail|panic|oom|timed out' "$log" | grep -av 'NOX-SMOKE' | tr -d '\r' | head -n 150 || true
echo "---- serial console (last 150 lines) ----"
tail -n 150 "$log" | tr -d '\r' || true
echo "---- markers ----"
grep -a 'NOX-SMOKE' "$log" | tr -d '\r' || true
if [ "$result" -ne 0 ]; then
  [ "$SECONDS" -lt "$deadline" ] || echo "no NOX-SMOKE-OK within ${timeout}s" >&2
  exit 1
fi
echo "Boot smoke passed in ${SECONDS}s."
