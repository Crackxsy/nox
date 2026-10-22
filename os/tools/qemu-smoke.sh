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
# CI only (os/tools/qemu-smoke.sh) - never part of a published image.
waited=0
while [ "$waited" -lt 2100 ]; do
  if systemctl is-active --quiet nox.service && curl -fsS -o /dev/null http://127.0.0.1:47801/health; then
    echo "NOX-SMOKE: nox.service active and /health answers after ${waited}s"
    if systemctl is-active --quiet ssh.service; then echo "NOX-SMOKE-FAIL: ssh.service is running"; exit 1; fi
    echo "NOX-SMOKE: ssh.service $(systemctl is-enabled ssh.service 2>/dev/null || true)"
    if ! ls /etc/ssh/ssh_host_ed25519_key >/dev/null 2>&1; then echo "NOX-SMOKE-FAIL: no SSH host keys made"; exit 1; fi
    echo "NOX-SMOKE: host key $(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | cut -d' ' -f2)"
    if grep -qx uninitialized /etc/machine-id; then echo "NOX-SMOKE-FAIL: machine-id not made"; exit 1; fi
    echo "NOX-SMOKE-OK $(. /etc/nox-os-release && echo "$NOX_OS_VERSION")"
    exit 0
  fi
  sleep 10
  waited=$((waited + 10))
done
echo "NOX-SMOKE-FAIL: nox did not answer in ${waited}s"
systemctl status nox.service --no-pager -l || true
journalctl -u nox.service -n 80 --no-pager || true
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
# The image's own kernel; the command line replaces cmdline.txt (no first-boot resize in a test).
qemu-system-aarch64 -machine raspi3b -m 1G -smp 4 \
  -kernel "$bootfiles/kernel8.img" -dtb "$bootfiles/bcm2710-rpi-3-b-plus.dtb" \
  -drive "file=$copy,if=sd,format=raw" \
  -append "console=ttyAMA0,115200 root=/dev/mmcblk0p2 rootfstype=ext4 rootwait rw dwc_otg.lpm_enable=0 systemd.mask=rpi-resize.service systemd.show_status=1" \
  -display none -serial "file:$log" -monitor none -no-reboot &
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
wait "$qemu_pid" 2>/dev/null || true
qemu_pid=""

echo "---- serial console (last 200 lines) ----"
tail -n 200 "$log" | tr -d '\r' || true
echo "---- markers ----"
grep -a 'NOX-SMOKE' "$log" | tr -d '\r' || true
if [ "$result" -ne 0 ]; then
  [ "$SECONDS" -lt "$deadline" ] || echo "no NOX-SMOKE-OK within ${timeout}s" >&2
  exit 1
fi
echo "Boot smoke passed in ${SECONDS}s."
