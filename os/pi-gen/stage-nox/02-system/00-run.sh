#!/bin/bash -e
# The baseline every Nox-OS image has (stage-nox/README.md). Hardening proper is O2's sub-stage.

install -m 0644 files/nox-os-release "${ROOTFS_DIR}/etc/nox-os-release"

# No second remote-access path next to Nox: Raspberry Pi Connect (in the Lite base) is removed.
on_chroot <<'EOF2'
if dpkg-query -W rpi-connect-lite >/dev/null 2>&1; then
	DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq rpi-connect-lite
fi
EOF2

# Nobody can log in: the first user's throwaway build password (os/pi-gen/build.sh) becomes a lock
# without any hash, root stays locked as pi-gen leaves it, and no account has a key. Remote access is
# switched on deliberately on the device later (O2), never shipped.
on_chroot <<EOF2
usermod --password '!' "${FIRST_USER_NAME}"
EOF2
rm -rf "${ROOTFS_DIR}/home/${FIRST_USER_NAME}/.ssh" "${ROOTFS_DIR}/root/.ssh"

# SSH off: disabled and masked, so neither an "ssh" file on the boot partition (sshswitch) nor
# anything else starts it. O2's dashboard toggle unmasks it on purpose, key-only.
on_chroot <<'EOF2'
systemctl disable ssh.service ssh.socket >/dev/null 2>&1 || true
systemctl mask ssh.service ssh.socket sshswitch.service
EOF2

# Host keys are this device's own: none ship in the image, the first boot makes them.
rm -f "${ROOTFS_DIR}"/etc/ssh/ssh_host_*
install -m 0644 files/nox-ssh-hostkeys.service "${ROOTFS_DIR}/etc/systemd/system/nox-ssh-hostkeys.service"
on_chroot <<'EOF2'
systemctl enable nox-ssh-hostkeys.service
EOF2

# No private key that every image would share (Debian's snakeoil certificate; nothing uses it).
rm -f "${ROOTFS_DIR}/etc/ssl/private/ssl-cert-snakeoil.key" "${ROOTFS_DIR}/etc/ssl/certs/ssl-cert-snakeoil.pem"

# Belt and braces to the skipped cloud-init sub-stage: it never reads the boot partition.
if [ -d "${ROOTFS_DIR}/etc/cloud" ]; then
	touch "${ROOTFS_DIR}/etc/cloud/cloud-init.disabled"
fi

# Seeds and secrets of the system are made per device, never shipped (os/tools/check-image.sh
# checks all of this on the finished image).
rm -f "${ROOTFS_DIR}/var/lib/systemd/random-seed" "${ROOTFS_DIR}/var/lib/systemd/credential.secret" \
	"${ROOTFS_DIR}/var/lib/NetworkManager/secret_key" "${ROOTFS_DIR}/var/lib/dbus/machine-id"
# No passwordless sudo, and nothing the build left in a home folder.
rm -f "${ROOTFS_DIR}"/etc/sudoers.d/010_*-nopasswd
rm -rf "${ROOTFS_DIR}"/root/.bash_history "${ROOTFS_DIR}"/root/.cache "${ROOTFS_DIR}"/root/.gitconfig \
	"${ROOTFS_DIR}"/root/.wget-hsts "${ROOTFS_DIR}"/root/.lesshst "${ROOTFS_DIR}"/home/*/.cache \
	"${ROOTFS_DIR}"/home/*/.bash_history
