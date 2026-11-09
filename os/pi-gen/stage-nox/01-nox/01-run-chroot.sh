# shellcheck shell=bash
# Runs inside the image (pi-gen pipes it into the chroot's bash -e). Nox is installed by the same
# installer a Pi uses (scripts/install-linux.sh --image): a tag from GitHub with its signed-release
# check, or the commit build.sh cloned into src/ with the dashboard built next to it. Every command
# reads /dev/null, never the rest of this script on stdin.
set -eu
cd /var/tmp/nox-image
. ./image.env
set -- --image --version "$NOX_IMAGE_REF"
[ ! -f nox-ui.tar.gz ] || set -- "$@" --ui-bundle /var/tmp/nox-image/nox-ui.tar.gz
if [ -d src ]; then
	NOX_LANG=en NOX_SOURCE=/var/tmp/nox-image/src bash ./install-linux.sh "$@" </dev/null
else
	NOX_LANG=en bash ./install-linux.sh "$@" </dev/null
fi
cd /
rm -rf /var/tmp/nox-image
