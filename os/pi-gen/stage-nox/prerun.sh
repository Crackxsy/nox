#!/bin/bash -e
# Nox-OS stage (os/README.md): starts from stage2, Raspberry Pi OS Lite.
if [ ! -d "${ROOTFS_DIR}" ]; then
	copy_previous
fi
