#!/bin/bash -e
# The files os/pi-gen/build.sh put next to this script go into the image for 01-run-chroot.sh -
# under /var/tmp, because on_chroot mounts a fresh tmpfs over /tmp.
install -d -m 0700 "${ROOTFS_DIR}/var/tmp/nox-image"
cp -R files/. "${ROOTFS_DIR}/var/tmp/nox-image/"
