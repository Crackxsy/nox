#!/bin/bash -e
# Nox-OS hardening (O2, stage-nox/README.md, os/README.md): firewall, remote maintenance off with a
# switch in the dashboard, SD-card wear, kernel settings, unused services off, console status.
# os/tools/check-image.sh checks every guarantee here on the finished image.

lib="${ROOTFS_DIR}/usr/local/lib/nox-os"
units="${ROOTFS_DIR}/etc/systemd/system"
install -d -m 0755 "$lib" "${ROOTFS_DIR}/etc/nox-os/ssh"
install -m 0755 files/nox_firewall.py files/nox_os_helper.py files/nox_console.py "$lib/"
for unit in nox-firewall.service nox-firewall.path nox-os-helper.socket nox-os-helper@.service \
	nox-os-helper-recover.service nox-ssh.service nox-console.service; do
	install -m 0644 "files/$unit" "$units/$unit"
done

# ---- firewall: default deny, from boot on -------------------------------------------------------
# nftables.service loads the baseline (no Nox port open) before the network comes up; the
# generator then opens what Nox's configuration asks for (nox-firewall.service, again whenever
# user.yaml changes). If the generator fails, the baseline stays: closed, not open.
on_chroot <<'EOF2'
python3 -I /usr/local/lib/nox-os/nox_firewall.py baseline > /etc/nftables.conf
chmod 0644 /etc/nftables.conf
EOF2

# ---- remote maintenance: off; the root helper is the only way on --------------------------------
# The helper's socket belongs to the group nox-os-helper, which has no members: only nox.service
# carries it (SupplementaryGroups=, a drop-in - the unit itself stays as the installer ships it).
on_chroot <<'EOF2'
getent group nox-os-helper >/dev/null || groupadd --system nox-os-helper
EOF2
install -d -m 0755 "$units/nox.service.d"
install -m 0644 files/nox-service-nox-os.conf "$units/nox.service.d/50-nox-os.conf"
install -m 0644 files/sshd_config "${ROOTFS_DIR}/etc/nox-os/ssh/sshd_config"
install -m 0644 files/tmpfiles-nox-os.conf "${ROOTFS_DIR}/etc/tmpfiles.d/nox-os.conf"
install -m 0440 files/sudoers-nox-os "${ROOTFS_DIR}/etc/sudoers.d/50-nox-os"

# ---- console status on tty1 instead of a login prompt ---------------------------------------
# Nobody can log in on the console anyway (no account has a password); the other consoles keep
# their prompt for that locked account.
on_chroot <<'EOF2'
systemctl enable nftables.service nox-firewall.service nox-firewall.path nox-os-helper.socket \
	nox-console.service
systemctl disable getty@tty1.service >/dev/null 2>&1 || true
systemctl mask getty@tty1.service autovt@tty1.service
EOF2

# ---- SD-card wear --------------------------------------------------------------------------
# journald in memory, the root file system without access times and with a 60 s commit interval,
# /tmp and /var/tmp in memory, swap only as compressed RAM (zram), no man-db rebuilds. What a
# power cut costs is in os/README.md.
install -d -m 0755 "${ROOTFS_DIR}/etc/systemd/journald.conf.d"
install -m 0644 files/journald-nox-os.conf "${ROOTFS_DIR}/etc/systemd/journald.conf.d/50-nox-os.conf"
rm -rf "${ROOTFS_DIR}/var/log/journal"
fstab="${ROOTFS_DIR}/etc/fstab"
sed -i -E '/^[^#[:space:]]+[[:space:]]+\/[[:space:]]+ext4[[:space:]]/{/commit=/!s/^([^[:space:]]+[[:space:]]+\/[[:space:]]+ext4[[:space:]]+)([^[:space:]]+)/\1\2,commit=60/}' "$fstab"
grep -qE '^[^#[:space:]]+[[:space:]]+/[[:space:]]+ext4[[:space:]]+[^[:space:]]*noatime' "$fstab" \
	|| sed -i -E 's/^([^#[:space:]]+[[:space:]]+\/[[:space:]]+ext4[[:space:]]+)([^[:space:]]+)/\1\2,noatime/' "$fstab"
grep -qE '^tmpfs[[:space:]]+/tmp[[:space:]]' "$fstab" \
	|| echo 'tmpfs  /tmp      tmpfs  defaults,noatime,nosuid,nodev,mode=1777,size=50%  0  0' >> "$fstab"
grep -qE '^tmpfs[[:space:]]+/var/tmp[[:space:]]' "$fstab" \
	|| echo 'tmpfs  /var/tmp  tmpfs  defaults,noatime,nosuid,nodev,mode=1777,size=25%  0  0' >> "$fstab"
install -d -m 0755 "${ROOTFS_DIR}/etc/rpi/swap.conf.d"
install -m 0644 files/swap-nox-os.conf "${ROOTFS_DIR}/etc/rpi/swap.conf.d/50-nox-os.conf"
rm -f "${ROOTFS_DIR}/var/swap"
on_chroot <<'EOF2'
for pkg in dphys-swapfile rsyslog; do
	if dpkg-query -W "$pkg" >/dev/null 2>&1; then
		DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq "$pkg"
	fi
done
echo 'man-db man-db/auto-update boolean false' | debconf-set-selections
rm -f /var/lib/man-db/auto-update
EOF2

# ---- kernel and network settings ----------------------------------------------------------
install -m 0644 files/90-nox-os.conf "${ROOTFS_DIR}/etc/sysctl.d/90-nox-os.conf"

# ---- services a headless Nox does not use: masked, so nothing pulls them in again ---------------
# Bluetooth (and the Pi 3/Zero 2 W UART helper for it), the keyboard-hotkey daemon, modem
# management. avahi stays: it answers for nox.local. Updates (unattended-upgrades) are step O6
# (sub-stage 05-updates), not here.
on_chroot <<'EOF2'
for unit in bluetooth.service hciuart.service triggerhappy.service triggerhappy.socket \
	ModemManager.service; do
	systemctl mask "$unit"
done
EOF2
