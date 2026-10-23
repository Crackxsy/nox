#!/bin/bash -e
# Nox-OS first boot without a screen (O3, stage-nox/README.md, os/README.md): the provisioning
# file, the setup window, the setup hotspot with its captive portal, and the claim's root side.
# os/tools/check-image.sh checks every guarantee here on the finished image.

lib="${ROOTFS_DIR}/usr/local/lib/nox-os"
units="${ROOTFS_DIR}/etc/systemd/system"
install -d -m 0755 "$lib"
install -m 0755 files/nox_setup.py files/nox_setup_portal.py "$lib/"
for unit in nox-firstboot.service nox-setup.timer nox-setup.service nox-setup-close.service \
	nox-setup-apply.path nox-setup-apply.service nox-setup-portal.service; do
	install -m 0644 "files/$unit" "$units/$unit"
done

# The portal's own user (no login, no home), and the group that may read this boot's setup
# secrets: the portal and the console status carry it, nobody is a member.
on_chroot <<'EOF2'
getent group nox-os-setup >/dev/null || groupadd --system nox-os-setup
getent passwd nox-setup >/dev/null || useradd --system --user-group --no-create-home \
	--home-dir /nonexistent --shell /usr/sbin/nologin nox-setup
EOF2

# The console shows the setup secrets during setup; the root helper writes the claim mark.
install -d -m 0755 "$units/nox-console.service.d" "$units/nox-os-helper@.service.d"
install -m 0644 files/nox-console-setup.conf "$units/nox-console.service.d/50-nox-os-setup.conf"
install -m 0644 files/nox-os-helper-claim.conf "$units/nox-os-helper@.service.d/50-nox-os-claim.conf"
install -m 0644 files/tmpfiles-nox-os-claim.conf "${ROOTFS_DIR}/etc/tmpfiles.d/nox-os-claim.conf"

# NetworkManager's shared mode (the setup hotspot): every name answers with the portal.
install -d -m 0755 "${ROOTFS_DIR}/etc/NetworkManager/dnsmasq-shared.d"
install -m 0644 files/dnsmasq-nox-setup.conf \
	"${ROOTFS_DIR}/etc/NetworkManager/dnsmasq-shared.d/50-nox-setup.conf"

# The locales a provisioning file may choose (nox_setup.py LOCALES).
on_chroot <<'EOF2'
for locale in de_DE.UTF-8 en_GB.UTF-8 en_US.UTF-8; do
	sed -i -E "s/^#[[:space:]]*(${locale} UTF-8)/\1/" /etc/locale.gen
	grep -qx "${locale} UTF-8" /etc/locale.gen || echo "${locale} UTF-8" >> /etc/locale.gen
done
locale-gen >/dev/null
EOF2

# systemd-firstboot asks for locale, keymap, time zone and a root password on the console - on a
# headless Pi it has nobody to ask and fails. Nox-OS answers those itself (the image's defaults,
# the provisioning file; root stays locked), so it is masked.
on_chroot <<'EOF2'
systemctl mask systemd-firstboot.service
systemctl enable nox-firstboot.service nox-setup.timer nox-setup-apply.path
EOF2
