#!/usr/bin/env bash
# `nox` on a Linux server: the same command as on Windows, always acting on the service's own data.
#
# Installed by scripts/install-linux.sh as /usr/local/bin/nox. Without it, every command would
# need the long form `sudo -u nox NOX_HOME=/var/lib/nox /opt/nox/.venv/bin/nox ...`, and a plain
# `nox secrets set` run as yourself would write into your own home folder, where the service never
# looks. So: `nox doctor`, `nox where`, `nox onboard`, `nox secrets set ...` - from any account
# that may use sudo.
#
# Three subcommands exist only here:
#   nox update [version]  install a newer (or the given) version; the data is snapshotted first
#   nox claude-login      install Claude Code for the service user and start its login
#   nox onboard           as everywhere, then restart the service so the answers take effect
set -euo pipefail

SERVICE_USER="nox"
NOX_HOME_DIR="/var/lib/nox"
PROGRAM="/opt/nox/.venv/bin/nox"

as_service() {
  if [ "$(id -un)" = "$SERVICE_USER" ]; then
    cd "$NOX_HOME_DIR" && exec env NOX_HOME="$NOX_HOME_DIR" HOME="$NOX_HOME_DIR" "$@"
  fi
  exec sudo -u "$SERVICE_USER" -- env NOX_HOME="$NOX_HOME_DIR" HOME="$NOX_HOME_DIR" \
    bash -c 'cd "$HOME" && exec "$@"' nox "$@"
}

if [ "${1:-}" = "update" ]; then
  installer="$(mktemp)"
  trap 'rm -f -- "$installer"' EXIT
  # A copy, because the installer replaces the checkout it would otherwise be read from.
  cp /opt/nox/scripts/install-linux.sh "$installer"
  version=()
  if [ -n "${2:-}" ]; then version=("NOX_REF=$2"); fi
  sudo env "${version[@]}" bash "$installer"
  exit $?
fi

if [ "${1:-}" = "onboard" ]; then
  (as_service "$PROGRAM" "$@") || exit $?
  echo "Nox neu starten, damit die Einstellungen gelten ..."
  sudo systemctl restart nox
  exit 0
fi

if [ "${1:-}" = "claude-login" ]; then
  claude="$NOX_HOME_DIR/.local/bin/claude"
  if ! sudo -u "$SERVICE_USER" test -x "$claude"; then
    echo "Claude Code für den Nox-Dienst installieren ..."
    sudo -u "$SERVICE_USER" -- env HOME="$NOX_HOME_DIR" bash -c \
      'cd "$HOME" && curl --proto "=https" --tlsv1.2 -fsSL https://claude.ai/install.sh | bash'
  fi
  cat <<'HOW'

  Gleich startet Claude Code. So meldest du dich an:
    0. Fragt Claude zuerst nach einem Farbschema oder ob es dem Ordner vertrauen darf:
       einfach Enter drücken.
    1. "/login" eintippen und Enter drücken.
    2. Den angezeigten Link auf dem Handy oder PC öffnen und anmelden.
    3. Den Code, den die Seite zeigt, hier einfügen.
    4. Danach mit "/exit" beenden. Nox nutzt die Anmeldung ab dem nächsten Neustart:
       sudo systemctl restart nox

HOW
  as_service "$claude"
fi

as_service "$PROGRAM" "$@"
