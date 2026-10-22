#!/usr/bin/env bash
# Boot a Nox-OS image headless in QEMU (an emulated Raspberry Pi 3) until Nox answers (os/README.md).
#
#   sudo os/tools/qemu-smoke.sh <image.img> <boot files folder> [timeout seconds, default 2700]
#
# <boot files folder>: kernel8.img and bcm2710-rpi-3-b-plus.dtb, as os/tools/check-image.sh takes
# them out. The image itself is never changed: a copy boots, with one CI-only unit added that waits
# for nox.service and its /health answer, checks what the first boot made (host keys, machine id)
# and what it must not have (SSH running), writes the result to /var/log/nox-smoke.log and powers
# the machine off. The result is read from the copy's disk afterwards - not from the serial
# console, which the emulated Pi 3 does not keep reliably - together with the boot's journal.
# Exit 0 only for NOX-SMOKE-OK. NOX_SMOKE_LOG=<file> keeps the serial log.
set -euo pipefail

image="${1:?usage: qemu-smoke.sh <image.img> <boot files folder> [timeout seconds]}"
bootfiles="${2:?usage: qemu-smoke.sh <image.img> <boot files folder> [timeout seconds]}"
timeout="${3:-2700}"
[ "$(id -u)" -eq 0 ] || { echo "qemu-smoke.sh: needs root (loop devices)" >&2; exit 1; }
for tool in qemu-system-aarch64 qemu-img fdtget fdtput journalctl; do
  command -v "$tool" >/dev/null || { echo "qemu-smoke.sh: $tool is missing (qemu-system-arm, qemu-utils, device-tree-compiler)" >&2; exit 1; }
done

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
mkdir "$work/root"
mount_copy() {
  loop="$(losetup --find --show --partscan "$copy")"
  udevadm settle 2>/dev/null || true
  mount "${loop}p2" "$work/root"
}
unmount_copy() {
  umount "$work/root"
  losetup -d "$loop"
  loop=""
}

# The CI-only unit, into the copy - and a persistent journal, so the boot can be read afterwards.
mount_copy
# (systemd-tmpfiles gives it the guest's systemd-journal group at boot.)
install -d -m 2755 "$work/root/var/log/journal"
cat > "$work/root/usr/local/sbin/nox-smoke" <<'SMOKE'
#!/bin/sh
# CI only (os/tools/qemu-smoke.sh) - never part of a published image.
say() { echo "$*"; echo "$*" >> /var/log/nox-smoke.log; echo "$*" > /dev/kmsg 2>/dev/null || true; }
finish() { sync; systemctl poweroff --no-block; exit "$1"; }
say "NOX-SMOKE: started, waiting for nox.service"
waited=0
while [ "$waited" -lt 2100 ]; do
  if systemctl is-active --quiet nox.service && curl -fsS -o /dev/null http://127.0.0.1:47801/health; then
    say "NOX-SMOKE: nox.service active and /health answers after ${waited}s"
    if systemctl is-active --quiet ssh.service; then say "NOX-SMOKE-FAIL: ssh.service is running"; finish 1; fi
    say "NOX-SMOKE: ssh.service $(systemctl is-enabled ssh.service 2>/dev/null || true)"
    if ! ls /etc/ssh/ssh_host_ed25519_key >/dev/null 2>&1; then say "NOX-SMOKE-FAIL: no SSH host keys made"; finish 1; fi
    say "NOX-SMOKE: host key $(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | cut -d' ' -f2)"
    if grep -qx uninitialized /etc/machine-id; then say "NOX-SMOKE-FAIL: machine-id not made"; finish 1; fi
    say "NOX-SMOKE-OK $(. /etc/nox-os-release && echo "$NOX_OS_VERSION")"
    finish 0
  fi
  if [ $((waited % 120)) -eq 0 ]; then
    say "NOX-SMOKE: ${waited}s, nox.service $(systemctl is-active nox.service 2>/dev/null)"
  fi
  sleep 10
  waited=$((waited + 10))
done
say "NOX-SMOKE-FAIL: nox did not answer in ${waited}s"
finish 1
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
unmount_copy

# QEMU's SD card wants a power-of-two size.
size="$(stat -c %s "$copy")"
sd=1
while [ "$sd" -lt "$size" ]; do sd=$((sd * 2)); done
qemu-img resize -q -f raw "$copy" "$sd"

log="$work/serial.log"
qemu-system-aarch64 --version | head -n1
# The image's own kernel; the command line replaces cmdline.txt: no first-boot resize in a test,
# and no hardware watchdog - QEMU's raspi model resets the machine the moment one is started.
# earlycon: the kernel's first lines reach the serial log before its console driver is up.
cmdline="earlycon=pl011,0x3f201000 console=ttyAMA0,115200 root=/dev/mmcblk0p2 rootfstype=ext4"
cmdline="$cmdline rootwait rw dwc_otg.lpm_enable=0 systemd.show_status=1 printk.devkmsg=on audit=0"
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

# Until the smoke unit powers the machine off (QEMU then exits), or the time is up.
deadline=$((SECONDS + timeout))
while [ "$SECONDS" -lt "$deadline" ] && kill -0 "$qemu_pid" 2>/dev/null; do
  sleep 10
done
stopped_by_itself=yes
if kill -0 "$qemu_pid" 2>/dev/null; then stopped_by_itself="no (killed after ${timeout}s)"; fi
kill "$qemu_pid" 2>/dev/null || true
wait "$qemu_pid" 2>/dev/null || true
qemu_pid=""
echo "---- QEMU stopped by itself: $stopped_by_itself ----"
grep -v '^SD: Unknown CMD' "$work/qemu.out" | tail -n 10 || true
[ -z "${NOX_SMOKE_LOG:-}" ] || cp "$log" "$NOX_SMOKE_LOG"

# The result and the boot's journal, from the copy's disk.
mount_copy
journal=(journalctl -D "$work/root/var/log/journal" --no-pager -o short-monotonic)
echo "---- nox.service (journal) ----"
"${journal[@]}" -u nox.service -n 60 2>&1 || true
echo "---- warnings and errors of the boot (journal) ----"
"${journal[@]}" -p warning -n 80 2>&1 || true
echo "---- units that failed (journal) ----"
"${journal[@]}" -n 40 --grep 'Failed to start|failed with result' 2>&1 || true
echo "---- result (/var/log/nox-smoke.log) ----"
cat "$work/root/var/log/nox-smoke.log" 2>/dev/null || echo "(none: the smoke unit never ran)"
result=1
grep -q '^NOX-SMOKE-OK' "$work/root/var/log/nox-smoke.log" 2>/dev/null && result=0
unmount_copy

if [ "$result" -ne 0 ]; then
  echo "Boot smoke failed." >&2
  exit 1
fi
echo "Boot smoke passed in ${SECONDS}s."
