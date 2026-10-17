#!/usr/bin/env bash
# `nox` on a Linux server: the same command as on Windows, always acting on the service's own data.
#
# Installed by scripts/install-linux.sh as /usr/local/bin/nox. Without it, every command would
# need the long form `sudo -u nox NOX_HOME=/var/lib/nox /opt/nox/current/.venv/bin/nox ...`, and a
# plain `nox secrets set` run as yourself would write into your own home folder, where the service
# never looks. So: `nox doctor`, `nox where`, `nox onboard`, `nox secrets set ...` - from any
# account that may use sudo.
#
# Subcommands that exist only here:
#   nox update [version]   install a newer (or the given) version; the data is snapshotted first
#   nox rollback           back to the version before the last update, with its data
#   nox snapshots          list the snapshots taken before updates
#   nox restore <file>     put the data of one snapshot back (the program stays as it is)
#   nox claude-login       install Claude Code for the service user and start its login
#   nox onboard            as everywhere, then restart the service so the answers take effect
set -euo pipefail

SERVICE_USER="nox"
NOX_HOME_DIR="/var/lib/nox"
CURRENT="/opt/nox/current"
PROGRAM="$CURRENT/.venv/bin/nox"
BACKUP_DIR="/var/backups/nox"

as_service() {
  if [ "$(id -un)" = "$SERVICE_USER" ]; then
    cd "$NOX_HOME_DIR" && exec env NOX_HOME="$NOX_HOME_DIR" HOME="$NOX_HOME_DIR" "$@"
  fi
  exec sudo -u "$SERVICE_USER" -- env NOX_HOME="$NOX_HOME_DIR" HOME="$NOX_HOME_DIR" \
    bash -c 'cd "$HOME" && exec "$@"' nox "$@"
}

#: The installer of the running version, run from a copy: an update replaces what it reads from.
run_installer() {
  local installer
  installer="$(mktemp)"
  trap 'rm -f -- "$installer"' EXIT
  cp "$CURRENT/scripts/install-linux.sh" "$installer"
  sudo env "$@" bash "$installer"
}

case "${1:-}" in
  update)
    if [ -n "${2:-}" ]; then run_installer NOX_ACTION=install "NOX_REF=$2"; else run_installer NOX_ACTION=install; fi
    exit $?
    ;;
  rollback)
    run_installer NOX_ACTION=rollback
    exit $?
    ;;
  restore)
    [ -n "${2:-}" ] || { echo "Welcher Schnappschuss? Liste: nox snapshots" >&2; exit 2; }
    file="$2"
    [ -f "$file" ] || file="$BACKUP_DIR/$2"
    run_installer NOX_ACTION=restore "NOX_SNAPSHOT=$file"
    exit $?
    ;;
  snapshots)
    listing="$(sudo find "$BACKUP_DIR" -maxdepth 1 -name 'nox-*.tar.gz' \
      -printf '%TY-%Tm-%Td %TH:%TM  %8s  %f\n' 2>/dev/null | sort -r || true)"
    if [ -n "$listing" ]; then
      printf '%s\n' "$listing"
      echo "Wiederherstellen: nox restore <dateiname>   ·   Version und Daten zurück: nox rollback"
    else
      echo "Noch keine Schnappschüsse - sie entstehen vor jedem Update."
    fi
    exit 0
    ;;
  onboard)
    (as_service "$PROGRAM" "$@") || exit $?
    echo "Nox neu starten, damit die Einstellungen gelten ..."
    sudo systemctl restart nox
    exit 0
    ;;
  claude-login)
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
    ;;
esac

as_service "$PROGRAM" "$@"
