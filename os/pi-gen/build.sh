#!/usr/bin/env bash
# Build the Nox-OS image with pi-gen in Docker (os/README.md).
#
#   os/pi-gen/build.sh --ref <tag or commit> --out <folder> [--source <checkout>] [--ui-bundle <file>]
#
# --ref        the Nox version in the image. Without --source it is installed the way a Pi installs
#              it - from GitHub, with the signed-release check (docs/RELEASES.md) - so use a tag.
# --source     a git checkout whose HEAD is --ref: Nox comes from there (branches and pull requests,
#              which have no release); --ui-bundle then supplies the dashboard built from it.
#
# Result: <out>/nox-os-<label>-arm64.img and .info (pi-gen's package list); <label> is the tag or
# the first 12 characters of the commit. Needs Docker that may run --privileged containers (pi-gen
# uses loop devices) and about 25 GB of disk; on an arm64 machine no emulation is involved (CI:
# ubuntu-24.04-arm). Linux only.
#
# Supply chain: pi-gen is pinned by tag and commit below. Its build-docker.sh builds its container
# FROM docker.io/debian:trixie by tag, not by digest - the build container (not shipped; the image's
# packages come from the Debian and Raspberry Pi archives, signed by their apt keys) follows that tag.
set -euo pipefail

PI_GEN_REPO="https://github.com/RPi-Distro/pi-gen"
#: The pi-gen release Nox-OS is built from - Raspberry Pi OS (trixie, arm64) - pinned by tag AND
#: commit: a tag that moved stops the build. Updated per Nox release, never followed automatically.
PI_GEN_TAG="2026-10-06-raspios-trixie-arm64"
PI_GEN_COMMIT="b2cd98a9ee08a31ef7d0110ed09f2492bbd972ec"

die() { printf 'build.sh: %s\n' "$*" >&2; exit 1; }

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
ref="" out="" source="" ui_bundle=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --ref) ref="${2:-}"; shift ;;
    --out) out="${2:-}"; shift ;;
    --source) source="${2:-}"; shift ;;
    --ui-bundle) ui_bundle="${2:-}"; shift ;;
    *) die "unknown option: $1" ;;
  esac
  shift
done
[ -n "$ref" ] && [ -n "$out" ] || die "usage: build.sh --ref <tag or commit> --out <folder> [--source <checkout>] [--ui-bundle <file>]"
# The installer's own rule for a version name.
[[ "$ref" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] || die "invalid --ref: $ref"
[ -z "$ui_bundle" ] || [ -n "$source" ] || die "--ui-bundle belongs to --source (a release brings its own)"
[ -z "$ui_bundle" ] || [ -f "$ui_bundle" ] || die "no such file: $ui_bundle"

label="$ref"
if [ -n "$source" ]; then
  [[ "$ref" =~ ^[0-9a-f]{40}$ ]] || die "--source needs --ref as a full commit id"
  [ "$(git -C "$source" rev-parse HEAD)" = "$ref" ] || die "the checkout in $source is not at $ref"
  label="${ref:0:12}"
fi
label="${label//\//-}"
name="nox-os-$label-arm64"

work="$(mktemp -d "${TMPDIR:-/tmp}/nox-os.XXXXXX")"
trap 'rm -rf -- "$work"' EXIT

git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$PI_GEN_TAG" "$PI_GEN_REPO" "$work/pi-gen"
[ "$(git -C "$work/pi-gen" rev-parse HEAD)" = "$PI_GEN_COMMIT" ] \
  || die "pi-gen tag $PI_GEN_TAG no longer points to $PI_GEN_COMMIT - check upstream before trusting it"
pigen="$work/pi-gen"

# Lite only: stage2 is not exported as an image of its own, and cloud-init is not installed at all
# (see config).
stage2="$pigen/stage2"
cloud_init="$stage2/04-cloud-init"
touch "$stage2/SKIP_IMAGES" "$cloud_init/SKIP"
cp -R "$here/stage-nox" "$pigen/stage-nox"
chmod 0755 "$pigen"/stage-nox/prerun.sh "$pigen"/stage-nox/*/*-run.sh
# One step in pi-gen's export, right after it installs userconf-pi: removes it again.
mkdir "$pigen/export-image/01-user-rename-nox"
install -m 0755 "$here/export-nox/00-run.sh" "$pigen/export-image/01-user-rename-nox/00-run.sh"

# What stage-nox/01-nox puts into the image: the installer, the version, and for a build from a
# checkout the source (a one-commit clone) and the dashboard.
files="$pigen/stage-nox/01-nox/files"
mkdir -p "$files"
install -m 0644 "$repo/scripts/install-linux.sh" "$files/install-linux.sh"
printf 'NOX_IMAGE_REF=%q\n' "$ref" > "$files/image.env"
if [ -n "$source" ]; then
  git clone --quiet --no-local --depth 1 "file://$(cd "$source" && pwd)" "$files/src"
  [ "$(git -C "$files/src" rev-parse HEAD)" = "$ref" ] || die "the clone of $source is not at $ref"
fi
[ -z "$ui_bundle" ] || install -m 0644 "$ui_bundle" "$files/nox-ui.tar.gz"

# /etc/nox-os-release in the image: which Nox-OS this is (O5/O6 read it).
mkdir -p "$pigen/stage-nox/02-system/files"
{
  printf 'NOX_OS_VERSION=%q\n' "$label"
  printf 'NOX_OS_NAME=%q\n' "Nox-OS (based on Raspberry Pi OS)"
  printf 'NOX_OS_BASE=%q\n' "pi-gen $PI_GEN_TAG ($PI_GEN_COMMIT)"
  printf 'NOX_OS_BUILD_DATE=%q\n' "$(date -u +%Y-%m-%d)"
} > "$pigen/stage-nox/02-system/files/nox-os-release"

# pi-gen refuses to skip the first-boot rename dialog without a password for the first user. It
# gets a random one that nobody ever sees, and stage-nox/02-system replaces it by a lock.
throwaway="$(openssl rand -hex 32)"
[ "${GITHUB_ACTIONS:-}" != "true" ] || printf '::add-mask::%s\n' "$throwaway"
umask 077
{
  cat "$here/config"
  printf 'IMG_FILENAME=%q\n' "$name"
  printf 'ARCHIVE_FILENAME=%q\n' "$name"
  printf 'IMG_DATE=%q\n' "$(date -u +%Y-%m-%d)"
  printf 'FIRST_USER_PASS=%q\n' "$throwaway"
} > "$pigen/config"
umask 022
unset throwaway

(cd "$pigen" && CONTAINER_NAME="nox-os-$$" ./build-docker.sh -c "$pigen/config")
rm -f -- "$pigen/config"

mkdir -p "$out"
[ -f "$pigen/deploy/$name.img" ] || die "pi-gen produced no $name.img"
[ -f "$pigen/deploy/$name.info" ] || die "pi-gen produced no $name.info"
mv -- "$pigen/deploy/$name.img" "$pigen/deploy/$name.info" "$out/"
printf 'Built %s\n' "$out/$name.img"
