#!/usr/bin/env bash
# BETA - Nox as a node on a Mac (docs/NODES.md): this Mac as the hands of a Nox elsewhere in the
# home, never the Nox itself. Tested less than Windows and Linux; CI only lints it so far.
#
#   bash install-macos.sh --node                       install or update, as yourself (no sudo)
#   bash install-macos.sh --node --pair '<link>'       ... and pair right away
#   bash install-macos.sh --node --uninstall           remove the program and the LaunchAgent
#
# What it does: takes the version (NOX_REF, default the newest release; NOX_SOURCE=<checkout> for a
# local one) from git into ~/Library/Application Support/Nox/node-app/releases/<commit>, builds its
# Python environment with uv (pinned and checked by SHA-256), switches the `current` link only
# after the new version started once, and registers a per-user LaunchAgent that runs
# installer/start_node.pyw at login. The node then updates itself from signed releases.
#
# Signed releases: this beta does not check release signatures itself. While no release key is
# trusted (TRUSTED_RELEASE_KEYS, kept equal to install-linux.sh and src/nox/release/trusted_keys.py)
# it says so; once one is, it refuses tagged releases instead of installing them unchecked - use a
# checkout you verified (NOX_SOURCE) until this script verifies on its own.
set -euo pipefail
umask 022

REPO_URL="https://github.com/Crackxsy/nox"
UV_VERSION="0.12.18"
UV_SHA256_AARCH64="cf40e0c6a202190ccd9e0406dcfdd5b2d6668a9a5c779b17948963df32aafe5b"
UV_SHA256_X86_64="2e4108f5395397c8bc5d43bf83d3bdbb2d0e92b90d0efa607756be704905fa33"
TRUSTED_RELEASE_KEYS=(
)
SUPPORT="$HOME/Library/Application Support/Nox"
PREFIX="$SUPPORT/node-app"
NODE_DIR="$SUPPORT/node"
SRC="$PREFIX/src"
RELEASES="$PREFIX/releases"
CURRENT="$PREFIX/current"
LABEL="io.github.crackxsy.nox.node"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
NODE_BIN="$HOME/.local/bin/nox-node"
CURL=(curl --proto '=https' --tlsv1.2 -fsSL)

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
fail() { printf '\nError: %s\n' "$*" >&2; exit 1; }

NODE_MODE="" UNINSTALL="" PAIR=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --node) NODE_MODE=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --pair) shift; PAIR="${1:-}" ;;
    *) fail "unknown option: $1 (known: --node --pair <link> --uninstall)" ;;
  esac
  shift
done
[ -n "$NODE_MODE" ] || fail "on a Mac, Nox runs as a node only: bash install-macos.sh --node"
[ "$(uname -s)" = "Darwin" ] || fail "this script is for macOS. Linux: install-linux.sh --node"
[ "$(id -u)" -ne 0 ] || fail "run it without sudo, as the person who uses this Mac"

agent() { launchctl "$@" 2>/dev/null || true; }

if [ -n "$UNINSTALL" ]; then
  agent bootout "gui/$(id -u)" "$PLIST"
  rm -f -- "$PLIST" "$NODE_BIN"
  rm -rf -- "$PREFIX" "$NODE_DIR/app"
  say "Nox node removed. Its identity and audit trail stay in $NODE_DIR (delete it to forget this Mac; remove it in Nox's Devices page too)."
  exit 0
fi

for tool in git curl tar shasum; do command -v "$tool" >/dev/null 2>&1 || fail "$tool is missing (xcode-select --install)"; done
mkdir -p "$RELEASES"
exec 9>"$PREFIX/.install.lock"
command -v flock >/dev/null 2>&1 && { flock -n 9 || fail "another install is running"; }

say "BETA: Nox node for macOS"
SOURCE="${NOX_SOURCE:-$REPO_URL}"
if [ -d "$SRC/.git" ]; then
  git -C "$SRC" remote set-url origin "$SOURCE"
  git -C "$SRC" fetch --quiet --tags --force origin
else
  git clone --quiet "$SOURCE" "$SRC"
fi
[ -z "${NOX_SOURCE:-}" ] || git -C "$SRC" fetch --quiet origin HEAD
if [ -z "${NOX_REF:-}" ]; then
  if [ -n "${NOX_SOURCE:-}" ]; then
    NOX_REF="$(git -C "$NOX_SOURCE" rev-parse HEAD)"
  else
    NOX_REF="$(git -C "$SRC" tag --list 'v*' --sort=-v:refname | grep -E '^v[0-9]+[.][0-9]+[.][0-9]+$' | head -n1 || true)"
  fi
fi
[[ "${NOX_REF:-}" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] || fail "no valid version (NOX_REF)"
commit="$(git -C "$SRC" rev-parse --verify --quiet "refs/tags/$NOX_REF^{commit}" \
  || git -C "$SRC" rev-parse --verify --quiet "refs/remotes/origin/$NOX_REF^{commit}" \
  || git -C "$SRC" rev-parse --verify --quiet "$NOX_REF^{commit}" || true)"
[ -n "$commit" ] || fail "version $NOX_REF does not exist"
git -C "$SRC" cat-file -e "$commit:installer/start_node.pyw" 2>/dev/null || fail "$NOX_REF cannot run as a node yet"
if [[ "$NOX_REF" =~ ^v[0-9] ]] && [ -z "${NOX_SOURCE:-}" ]; then
  if [ "${#TRUSTED_RELEASE_KEYS[@]}" -eq 0 ]; then
    note "Note: release signatures are not set up yet (docs/RELEASES.md); this version is not checked against one."
  else
    fail "this beta cannot check release signatures yet, so it installs no tagged release. Use a checkout you verified: NOX_SOURCE=<folder> bash install-macos.sh --node"
  fi
fi
release="${commit:0:12}"

work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT
export UV_INSTALL_DIR="$PREFIX/.tools" UV_PYTHON_INSTALL_DIR="$PREFIX/.python" UV_NO_MODIFY_PATH=1
export UV_CACHE_DIR="$PREFIX/.cache/uv"
if [ ! -x "$UV_INSTALL_DIR/uv" ] || [ "$("$UV_INSTALL_DIR/uv" --version | awk '{print $2}')" != "$UV_VERSION" ]; then
  case "$(uname -m)" in
    arm64) arch="aarch64"; sum="$UV_SHA256_AARCH64" ;;
    x86_64) arch="x86_64"; sum="$UV_SHA256_X86_64" ;;
    *) fail "unsupported architecture $(uname -m)" ;;
  esac
  "${CURL[@]}" -o "$work/uv.tar.gz" "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$arch-apple-darwin.tar.gz"
  printf '%s  %s\n' "$sum" "$work/uv.tar.gz" | shasum -a 256 -c - >/dev/null \
    || fail "uv $UV_VERSION does not match its pinned SHA-256; nothing was changed"
  tar -xzf "$work/uv.tar.gz" -C "$work"
  mkdir -p "$UV_INSTALL_DIR"
  install -m 0755 "$work/uv-$arch-apple-darwin/uv" "$UV_INSTALL_DIR/uv"
fi

if [ ! -f "$RELEASES/$release/.complete" ]; then
  say "Setting up node $NOX_REF"
  rm -rf -- "${RELEASES:?}/$release"
  mkdir -p "$RELEASES/$release"
  git -C "$SRC" archive "$commit" | tar -x -C "$RELEASES/$release"
  (cd "$RELEASES/$release" && UV_COMPILE_BYTECODE=1 "$UV_INSTALL_DIR/uv" sync --quiet --frozen --no-build --python 3.13)
  printf '%s\n' "$NOX_REF" > "$RELEASES/$release/.complete"
fi

python="$CURRENT/.venv/bin/python"
launcher="$CURRENT/installer/start_node.pyw"
previous="$(basename "$(readlink "$CURRENT" 2>/dev/null || true)")"
ln -sfn "$RELEASES/$release" "$CURRENT.next"
mv -fh "$CURRENT.next" "$CURRENT"
if ! "$python" -I "$launcher" --selftest >/dev/null; then
  if [ -n "$previous" ] && [ "$previous" != "." ] && [ -d "$RELEASES/$previous" ]; then
    ln -sfn "$RELEASES/$previous" "$CURRENT.next"
    mv -fh "$CURRENT.next" "$CURRENT"
  fi
  fail "the new node does not start; the previous version stays"
fi

say "LaunchAgent $LABEL (at login, as you)"
mkdir -p "$(dirname "$PLIST")" "$(dirname "$NODE_BIN")" "$NODE_DIR"
xml() { local s="${1//&/&amp;}"; s="${s//</&lt;}"; printf '%s' "${s//>/&gt;}"; }
cat > "$PLIST.next" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$(xml "$python")</string><string>-I</string><string>$(xml "$launcher")</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ProcessType</key><string>Background</string>
  <key>EnvironmentVariables</key><dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>StandardErrorPath</key><string>$(xml "$SUPPORT/node-launchd.log")</string>
</dict>
</plist>
PLIST
mv -f "$PLIST.next" "$PLIST"
{
  printf '#!/usr/bin/env bash\n# nox node ... for this Mac (written by install-macos.sh --node).\n'
  printf 'run() { %q -I %q --cli node "$@"; }\n' "$python" "$launcher"
  # shellcheck disable=SC2016  # written literally into the wrapper
  printf 'run "$@" || exit $?\n[ "${1:-}" = pair ] && launchctl kickstart -k "gui/$(id -u)/%s"\nexit 0\n' "$LABEL"
} > "$NODE_BIN.next"
chmod 0755 "$NODE_BIN.next"
mv -f "$NODE_BIN.next" "$NODE_BIN"
agent bootout "gui/$(id -u)" "$PLIST"
agent bootstrap "gui/$(id -u)" "$PLIST"

for old in "$RELEASES"/*/; do
  name="$(basename "$old")"
  [ "$name" = "$release" ] || [ "$name" = "$previous" ] || rm -rf -- "$old"
done

say "Nox node $NOX_REF is set up (beta)."
if [ -n "$PAIR" ]; then
  "$NODE_BIN" pair "$PAIR" || note "Pairing did not work. Later: nox-node pair \"<link>\""
else
  note "Pair: on the Nox at home, Devices -> \"Add a PC or laptop\", then here: nox-node pair \"<link>\""
fi
note "Status: nox-node status   Log: nox-node log   Update: nox-node update   Remove: bash install-macos.sh --node --uninstall"
