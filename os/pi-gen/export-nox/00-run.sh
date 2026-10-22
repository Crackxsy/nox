#!/bin/bash -e
# Runs in pi-gen's export-image, right after its 01-user-rename (which installs userconf-pi):
# os/pi-gen/build.sh copies it there as 01-user-rename-nox. Nox-OS has no first-boot user dialog
# and creates no user from the boot partition, so userconf-pi goes again.
on_chroot <<'EOF2'
if dpkg-query -W userconf-pi >/dev/null 2>&1; then
	DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq userconf-pi
fi
EOF2
