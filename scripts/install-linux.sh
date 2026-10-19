#!/usr/bin/env bash
# Install or update Nox as a server on Linux - a Raspberry Pi (64-bit Raspberry Pi OS), Debian or
# Ubuntu, on ARM64 or x86-64. One command; run it again (or `nox update`) to update:
#
#   curl -fsSL https://github.com/Crackxsy/nox/releases/latest/download/install-linux.sh | sudo bash
#
# Prefer to read it first: download it, look at it, then run `sudo bash install-linux.sh`. Each
# release also carries install-linux.sh.sha256.
#
# Layout - every version is complete before it is used, so an update can always be undone:
#   /opt/nox/releases/<commit>   one finished version each: code, Python environment, dashboard
#   /opt/nox/current             a link to the version that runs
#   /opt/nox/src                 the git checkout versions are taken from
#   /var/lib/nox                 your data (the service's home); never inside a release
#   /var/backups/nox             a snapshot of /var/lib/nox before every update (the last five)
#
# An update builds the new version while the old one keeps running, then stops Nox, takes the
# snapshot, switches the link and starts. If the new version does not answer, it switches back and
# restores the data from the snapshot by itself.
#
# Actions (normally through the `nox` command): NOX_ACTION=install (default) | rollback | restore
# (NOX_SNAPSHOT=<file>) | verify-bundle (NOX_BUNDLE=<file>, for tests). NOX_REF picks a version;
# NOX_SOURCE=<checkout> installs from a local checkout (CI uses it); root runs git in it, so it
# must be a checkout you trust.
#
# Language: German when the system language is German (LC_ALL, LC_MESSAGES or LANG starting with
# "de"), English otherwise; NOX_LANG=de|en overrides it (`curl ... | sudo NOX_LANG=en bash`).
set -euo pipefail
umask 022

REPO_URL="https://github.com/Crackxsy/nox"
PREFIX="/opt/nox"
SRC="$PREFIX/src"
RELEASES="$PREFIX/releases"
CURRENT="$PREFIX/current"
STATE="$PREFIX/state"
NOX_HOME_DIR="/var/lib/nox"
BACKUP_DIR="/var/backups/nox"
SERVICE_USER="nox"
HTTP_PORT="47801"
#: The first release that runs on Linux. Older tags would install a Windows-only Nox.
MIN_VERSION="0.6.0"
UV_VERSION="0.12.18"
#: SHA-256 of uv's release archives for UV_VERSION (github.com/astral-sh/uv/releases). Pinned here,
#: not fetched next to the archive, so a compromised download cannot hand root another program.
UV_SHA256_AARCH64="afb6291f3f0a6b4521fc67b947822506c41dde5b60d2189dd8f3695b2ac8c9e7"
UV_SHA256_X86_64="89eadd7c76fc063887959510d5ba0ab1264dfd5f1143b925ddb73021a40acf16"
CURL=(curl --proto '=https' --tlsv1.2 -fsSL)
SOURCE="${NOX_SOURCE:-$REPO_URL}"
# Ownership checks stay on, except for the one checkout NOX_SOURCE names (CI's is not root's).
GIT=(git)
if [ -n "${NOX_SOURCE:-}" ]; then
  GIT+=(-c "safe.directory=$NOX_SOURCE" -c "safe.directory=$NOX_SOURCE/.git")
fi
ACTION="${NOX_ACTION:-install}"

# ---- messages -------------------------------------------------------------------------------------

#: "de" or "en": NOX_LANG when it names one of them, else the first locale variable that is set.
detect_language() {
  case "${NOX_LANG:-}" in de|en) printf '%s' "$NOX_LANG"; return ;; esac
  case "${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}" in de*) printf 'de' ;; *) printf 'en' ;; esac
}
UI_LANG="$(detect_language)"

declare -A MSG_DE=() MSG_EN=()
#: One message in both languages: key, German, English. Placeholders are printf's %s.
define() { MSG_DE[$1]="$2"; MSG_EN[$1]="$3"; }

#: The message KEY in the user's language, its %s filled with the remaining arguments in order.
msg() {
  local key="$1" template
  shift
  if [ "$UI_LANG" = de ]; then template="${MSG_DE[$key]-}"; else template="${MSG_EN[$key]-}"; fi
  [ -n "$template" ] || template="${MSG_EN[$key]-$key}"
  # shellcheck disable=SC2059  # the templates are the tables below, written with %s on purpose
  printf "$template" "$@"
}

# Failures and the undoing after them
define error \
  'Fehler: %s' \
  'Error: %s'
define step_failed \
  'Schritt in Zeile %s fehlgeschlagen. Internetverbindung prüfen und den Befehl noch einmal starten - er macht dort weiter, wo es geht.' \
  'Step on line %s failed. Check the internet connection and run the command again - it carries on where it can.'
define old_running_again \
  'Die bisherige Version läuft wieder, unverändert.' \
  'The previous version is running again, unchanged.'
define need_root \
  'bitte mit sudo starten: curl -fsSL <Adresse> | sudo bash  (oder: sudo bash install-linux.sh)' \
  'please run it with sudo: curl -fsSL <address> | sudo bash  (or: sudo bash install-linux.sh)'
define not_linux \
  'dieses Skript ist für Linux. Unter Windows: scripts/setup.ps1.' \
  'this script is for Linux. On Windows: scripts/setup.ps1.'
define locked \
  'ein anderer Installations- oder Update-Lauf ist gerade aktiv. Bitte warten und erneut versuchen.' \
  'another install or update is running right now. Please wait and try again.'
define unknown_action \
  'unbekannte Aktion: %s' \
  'unknown action: %s'
define bundle_missing \
  'NOX_BUNDLE fehlt' \
  'NOX_BUNDLE is missing'

# Snapshots, restore and rollback
define snapshot_missing \
  'Schnappschuss %s gibt es nicht.' \
  'Snapshot %s does not exist.'
define snapshot_damaged \
  'Schnappschuss %s ist beschädigt; nichts wurde geändert.' \
  'Snapshot %s is damaged; nothing was changed.'
define unpack_failed \
  'Entpacken fehlgeschlagen (Speicher voll?); die bisherigen Daten sind unverändert zurück.' \
  'Unpacking failed (disk full?); the previous data are back, unchanged.'
define restored \
  'Daten aus %s wiederhergestellt. Der Stand davor liegt in %s - löschen, sobald alles läuft.' \
  'Data restored from %s. What was there before is in %s - delete it once everything works.'
define no_step_back \
  'Es gibt keinen weiteren Schritt zurück. Zu einer neueren Version: nox update' \
  'There is no further step back. To a newer version: nox update'
define rollback_title \
  'Zurück zur vorherigen Version (%s)' \
  'Back to the previous version (%s)'
define rollback_aborted \
  'Zurückschalten abgebrochen: Daten unverändert, es läuft weiter Version %s. Protokoll: journalctl -u nox -n 50' \
  'Rollback stopped: data unchanged, version %s keeps running. Log: journalctl -u nox -n 50'
define rollback_done \
  'Die vorherige Version läuft wieder, mit den Daten von vor dem Update.' \
  'The previous version is running again, with the data from before the update.'
define rollback_since \
  'Was seit dem Update dazukam, liegt im beiseitegelegten Ordner (siehe oben).' \
  'Whatever came in since the update is in the folder set aside (see above).'
define rollback_no_answer \
  'Auch die vorherige Version antwortet nicht. Protokoll: journalctl -u nox -n 50' \
  'The previous version does not answer either. Log: journalctl -u nox -n 50'
define restore_which \
  'welcher Schnappschuss? nox restore <datei>  (Liste: nox snapshots)' \
  'which snapshot? nox restore <file>  (list: nox snapshots)'
define restore_missing \
  'Schnappschuss %s gibt es nicht. Liste: nox snapshots' \
  'Snapshot %s does not exist. List: nox snapshots'
define restore_outside \
  'nur Schnappschüsse aus %s lassen sich wiederherstellen (Liste: nox snapshots).' \
  'only snapshots from %s can be restored (list: nox snapshots).'
define restore_not_root \
  '%s gehört nicht root - so legt Nox keine Schnappschüsse an.' \
  '%s is not owned by root - Nox does not make snapshots like that.'
define restore_no_rollback \
  'Ein `nox rollback` gibt es danach nicht mehr: es würde diese Daten wieder überschreiben.' \
  'There is no `nox rollback` after this: it would overwrite these data again.'
define restore_done \
  'Nox läuft wieder, mit den Daten aus %s.' \
  'Nox is running again, with the data from %s.'
define restore_no_answer \
  'Nox antwortet nach dem Wiederherstellen nicht. Protokoll: journalctl -u nox -n 50' \
  'Nox does not answer after the restore. Log: journalctl -u nox -n 50'

# Preconditions
define arch_32bit \
  '32-bit-System erkannt. Nox braucht Raspberry Pi OS 64-bit (Pi 3, 4, 5, 400 oder Zero 2 W).' \
  '32-bit system detected. Nox needs Raspberry Pi OS 64-bit (Pi 3, 4, 5, 400 or Zero 2 W).'
define arch_unsupported \
  'nicht unterstützte Architektur: %s' \
  'unsupported architecture: %s'
define step_packages \
  'Pakete installieren (git, curl, bubblewrap)' \
  'Installing packages (git, curl, bubblewrap)'
define untested_system \
  'Hinweis: getestet ist Nox auf Raspberry Pi OS, Debian und Ubuntu. Auf diesem System kann etwas fehlen.' \
  'Note: Nox is tested on Raspberry Pi OS, Debian and Ubuntu. Something may be missing on this system.'
define tool_missing \
  '%s fehlt, bitte installieren.' \
  '%s is missing, please install it.'
define bwrap_missing \
  'Hinweis: bubblewrap (bwrap) fehlt - ohne es führt Nox keine Befehle aus. Bitte installieren.' \
  'Note: bubblewrap (bwrap) is missing - without it Nox runs no commands. Please install it.'
define step_apparmor \
  'AppArmor: eigene bwrap-Kopie für die Sandbox (Ubuntu)' \
  'AppArmor: a separate bwrap copy for the sandbox (Ubuntu)'
define apparmor_failed \
  'Hinweis: AppArmor-Profil für die Sandbox ließ sich nicht laden - Nox führt keine Befehle aus, bis das geklärt ist.' \
  'Note: the AppArmor profile for the sandbox could not be loaded - Nox runs no commands until that is sorted out.'
define prefix_foreign \
  '%s existiert schon, ist aber keine Nox-Installation. Bitte prüfen und den Ordner entfernen oder umbenennen.' \
  '%s already exists but is not a Nox installation. Please check it, then remove or rename the folder.'

# Which version
define step_source \
  'Nox-Quelle aktualisieren' \
  'Updating the Nox source'
define no_version \
  'keine Nox-Version gefunden. Internetverbindung prüfen.' \
  'no Nox version found. Check the internet connection.'
define invalid_version \
  'ungültige Version: %s' \
  'invalid version: %s'
define version_missing \
  'Version %s gibt es nicht.' \
  'Version %s does not exist.'
define windows_only \
  'Nox %s läuft nur unter Windows. Linux-Server gibt es ab v%s.' \
  'Nox %s runs on Windows only. Linux servers are supported from v%s on.'

# Building the release
define step_user \
  'Systembenutzer %s anlegen' \
  'Creating the system user %s'
define release_ready \
  'Nox %s ist schon vorbereitet' \
  'Nox %s is already prepared'
define step_release \
  'Nox %s vorbereiten' \
  'Preparing Nox %s'
define step_python \
  'Python 3.13 und Abhängigkeiten (uv %s, nur fertige Pakete)' \
  'Python 3.13 and dependencies (uv %s, prebuilt packages only)'
define step_ui \
  'Oberflächen (Dashboard und Pet) installieren' \
  'Installing the interfaces (dashboard and pet)'
define ui_checksum \
  'das Oberflächen-Paket stimmt nicht mit seiner Prüfsumme überein. Nichts wurde geändert.' \
  'the interface package does not match its checksum. Nothing was changed.'
define ui_unexpected \
  'das Oberflächen-Paket enthält Unerwartetes (Verknüpfungen oder fremde Dateien). Nichts wurde geändert.' \
  'the interface package contains something unexpected (links or foreign files). Nothing was changed.'
define ui_none \
  'Für %s gibt es kein fertiges Oberflächen-Paket (Entwicklungsversion).' \
  'There is no prebuilt interface package for %s (development version).'
define ui_none_hint \
  'Der Kern läuft trotzdem; das Dashboard zeigt bis dahin eine Hinweisseite.' \
  'The core runs anyway; until then the dashboard shows a notice page.'
define settings_link \
  '%s ist eine Verknüpfung. Aus Sicherheitsgründen wird sie nicht angefasst.' \
  '%s is a symbolic link. For safety it is left untouched.'
define step_settings \
  'Grundeinstellungen für einen Server schreiben' \
  'Writing the basic settings for a server'

# Switching over
define step_backup \
  'Sicherung vor dem Update' \
  'Backup before the update'
define backup_failed \
  'die Sicherung ist fehlgeschlagen (Speicher voll?). Nichts wurde umgestellt.' \
  'the backup failed (disk full?). Nothing was switched.'
define backup_done \
  'Gesichert: %s (%s). Enthält auch Zugangsdaten - nur root kann es lesen.' \
  'Saved: %s (%s). It holds credentials too - only root can read it.'
define step_service \
  'Dienst einrichten und starten' \
  'Setting up and starting the service'
define no_answer \
  'Nox %s antwortet nicht. Die letzten Zeilen des Protokolls:' \
  'Nox %s does not answer. The last lines of the log:'

# Done: what to know and what to do next
define running \
  'Nox läuft (%s).' \
  'Nox is running (%s).'
define rollback_hint \
  'Zurück zur vorherigen Version samt Daten: nox rollback' \
  'Back to the previous version, data included: nox rollback'
define kept_aside \
  'Beiseitegelegt: %s (%s) - wenn alles läuft: sudo rm -r %s' \
  'Set aside: %s (%s) - once everything works: sudo rm -r %s'
define lan_off \
  'Andere Geräte im Heimnetz erreichen Nox noch nicht. Einschalten: in %s' \
  'Other devices in the home network cannot reach Nox yet. To turn it on, in %s'
define lan_off_how \
  '  ipc: / lan: / enabled: true eintragen, dann sudo systemctl restart nox' \
  '  set ipc: / lan: / enabled: true, then sudo systemctl restart nox'
define firewall \
  'Die Firewall (ufw) ist aktiv. Damit Handy und Tablet Nox erreichen: sudo ufw allow 47843/tcp' \
  'The firewall (ufw) is active. So that phones and tablets reach Nox: sudo ufw allow 47843/tcp'
define no_ai \
  'Noch keine KI angemeldet: Nox antwortet mit einfachen eingebauten Regeln, bis du "nox claude-login" ausführst.' \
  'No AI signed in yet: Nox answers with simple built-in rules until you run "nox claude-login".'
define timezone \
  'Zeitzone: %s. Erinnerungen und Kalender richten sich danach.' \
  'Time zone: %s. Reminders and the calendar follow it.'
define timezone_unknown \
  'unbekannt' \
  'unknown'
define timezone_hint \
  'In Deutschland: sudo timedatectl set-timezone Europe/Berlin' \
  'In Germany: sudo timedatectl set-timezone Europe/Berlin'

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }

#: What the ERR trap has to undo: "" (nothing), "stopped" (old version stopped, link unchanged) or
#: "switched" (link already points at the new version).
phase=""
previous_release=""
snapshot=""

fail() {
  trap - ERR  # a failure while undoing must not start the undoing again
  printf '\n%s\n' "$(msg error "$*")" >&2
  case "$phase" in
    stopped)
      systemctl start nox 2>/dev/null && printf '%s\n' "$(msg old_running_again)" >&2 ;;
    switched)
      rollback_to "$previous_release" "$snapshot" || true ;;
  esac
  exit 1
}
trap 'fail "$(msg step_failed "$LINENO")"' ERR

# ---- helpers --------------------------------------------------------------------------------------

version_at_least() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]; }

docs_url() {
  local page="RASPBERRY_PI.md"
  [ "$UI_LANG" = de ] && page="RASPBERRY_PI.de.md"
  printf '%s/blob/%s/docs/%s' "$REPO_URL" "${1:-develop}" "$page"
}

#: The HTTP port the core actually took. It moves to the next free port when 47801 is busy and
#: writes the one it got to runtime/ipc.json at every start - stale until then, so it is re-read
#: on every try. Without the file (first start), the default.
http_port() {
  local port=""
  if [ -r "$NOX_HOME_DIR/runtime/ipc.json" ]; then
    port="$(sed -n 's/^[[:space:]]*"http_port":[[:space:]]*\([0-9]\{1,5\}\),\{0,1\}[[:space:]]*$/\1/p' \
      "$NOX_HOME_DIR/runtime/ipc.json" 2>/dev/null | head -n1 || true)"
  fi
  printf '%s' "${port:-$HTTP_PORT}"
}

wait_healthy() {
  local _
  for _ in $(seq 1 "${1:-90}"); do
    if curl -fsS -o /dev/null "http://127.0.0.1:$(http_port)/health" 2>/dev/null; then return 0; fi
    sleep 1
  done
  return 1
}

#: uv from its release archive, checked against the pinned SHA-256 before anything runs it.
install_uv() {
  local arch sum archive="$work/uv.tar.gz"
  case "$(uname -m)" in
    aarch64|arm64) arch="aarch64"; sum="$UV_SHA256_AARCH64" ;;
    x86_64) arch="x86_64"; sum="$UV_SHA256_X86_64" ;;
    *) fail "keine uv-Version für $(uname -m)." ;;
  esac
  "${CURL[@]}" -o "$archive" \
    "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$arch-unknown-linux-gnu.tar.gz"
  printf '%s  %s\n' "$sum" "$archive" | sha256sum --quiet -c - \
    || fail "uv $UV_VERSION stimmt nicht mit der hinterlegten Prüfsumme überein. Nichts wurde geändert."
  mkdir -p "$work/uv"
  tar -xzf "$archive" -C "$work/uv" --no-same-owner --no-same-permissions
  install -d -m 0755 "$UV_INSTALL_DIR"
  install -m 0755 "$work/uv/uv-$arch-unknown-linux-gnu/uv" "$UV_INSTALL_DIR/uv"
}

switch_to() {
  # Atomic: a new link is created next to the old one and renamed over it.
  ln -sfn "$RELEASES/$1" "$CURRENT.next"
  mv -Tf "$CURRENT.next" "$CURRENT"
}

install_service_files() {
  install -m 0644 "$CURRENT/installer/linux/nox.service" /etc/systemd/system/nox.service
  install -m 0755 "$CURRENT/installer/linux/nox-command.sh" /usr/local/bin/nox
  systemctl daemon-reload
}

#: Data back from a snapshot - into a fresh folder, never over the current one: files a newer
#: version wrote (a SQLite write-ahead log, say) must not survive next to restored ones.
#: Returns non-zero - with the current folder back in place - when anything goes wrong, because it
#: is also called from inside `|| true` (the automatic rollback), where `set -e` does not apply.
restore_data() {
  local file="$1" stamp aside kept
  [ -f "$file" ] || { printf '%s\n' "$(msg snapshot_missing "$file")" >&2; return 1; }
  # A truncated or corrupt archive is found before anything is moved.
  tar -tzf "$file" >/dev/null 2>&1 || { printf '%s\n' "$(msg snapshot_damaged "$file")" >&2; return 1; }
  stamp="$(date +%Y%m%d-%H%M%S)"
  aside="$NOX_HOME_DIR.vor-wiederherstellung-$stamp"
  mv -T "$NOX_HOME_DIR" "$aside" || return 1
  if ! tar -xzf "$file" -C "$(dirname "$NOX_HOME_DIR")"; then
    rm -rf -- "$NOX_HOME_DIR"
    mv -T "$aside" "$NOX_HOME_DIR"
    printf '%s\n' "$(msg unpack_failed)" >&2
    return 1
  fi
  # What snapshots leave out (the AI's own install, caches, models) moves back from the old folder.
  for kept in .local .cache models; do
    if [ -e "$aside/$kept" ] && [ ! -e "$NOX_HOME_DIR/$kept" ]; then mv "$aside/$kept" "$NOX_HOME_DIR/"; fi
  done
  chmod 0700 "$NOX_HOME_DIR"
  note "$(msg restored "$file" "$aside")"
}

#: Back to an earlier release, with the data as they were before the update that left it. Once:
#: afterwards there is no step further back, so a second rollback cannot put the same old data
#: back over everything written since.
rollback_to() {
  local release="$1" file="$2"
  # Cleared first: a failure from here on must not make `fail` start another rollback.
  phase=""
  [ -n "$release" ] && [ -d "$RELEASES/$release" ] || {
    printf '%s\n' "$(msg no_step_back)" >&2
    return 1
  }
  say "$(msg rollback_title "$release")"
  systemctl stop nox 2>/dev/null || true
  if [ -n "$file" ]; then
    if ! restore_data "$file"; then
      systemctl start nox 2>/dev/null || true
      printf '%s\n' "$(msg rollback_aborted "$(basename "$(readlink "$CURRENT")")")" >&2
      return 1
    fi
  fi
  switch_to "$release"
  install_service_files
  systemctl start nox
  phase=""
  rm -f -- "$STATE/previous-release" "$STATE/previous-snapshot"
  if wait_healthy 90; then
    note "$(msg rollback_done)"
    note "$(msg rollback_since)"
  else
    note "$(msg rollback_no_answer)"
    return 1
  fi
}

#: Exit status 0 when the UI archive is exactly what a release builds: plain files and folders,
#: only below ui/dashboard/dist and ui/pet/dist (plus their bare parent folders), no `..`, both
#: folders present.
verify_bundle() {
  local archive="$1" listing
  listing="$(mktemp -d)"
  tar -tvzf "$archive" > "$listing/long" || { rm -rf -- "$listing"; return 1; }
  tar -tzf "$archive" > "$listing/names" || { rm -rf -- "$listing"; return 1; }
  # Every list goes to a file before grep reads it: in `producer | grep -q` under pipefail, grep's
  # early exit gives the producer SIGPIPE and turns a hit into a pass.
  awk '{print substr($1,1,1)}' "$listing/long" > "$listing/types"
  local status=0
  if grep -qv '^[-d]$' "$listing/types"; then status=1; fi
  if grep -qvE '^ui/$|^ui/(dashboard|pet)/$|^ui/(dashboard|pet)/dist(/|$)' "$listing/names"; then status=1; fi
  if grep -q '\.\.' "$listing/names"; then status=1; fi
  for app in dashboard pet; do
    grep -qE "^ui/$app/dist/" "$listing/names" || status=1
  done
  rm -rf -- "$listing"
  return "$status"
}

# ---- preconditions --------------------------------------------------------------------------------

[ "$(id -u)" -eq 0 ] || fail "$(msg need_root)"
[ "$(uname -s)" = "Linux" ] || fail "$(msg not_linux)"

# One installer at a time: two runs (two `nox update`s from two terminals, say) would race on the
# release folders, the snapshot and the link.
exec 9>/run/nox-install.lock
flock -n 9 || fail "$(msg locked)"

if [ "$ACTION" = "verify-bundle" ]; then
  trap - ERR
  [ -n "${NOX_BUNDLE:-}" ] || fail "$(msg bundle_missing)"
  verify_bundle "$NOX_BUNDLE"
  exit $?
fi

if [ "$ACTION" = "rollback" ]; then
  previous_release="$(cat "$STATE/previous-release" 2>/dev/null || true)"
  snapshot="$(cat "$STATE/previous-snapshot" 2>/dev/null || true)"
  rollback_to "$previous_release" "$snapshot" || exit 1
  exit 0
fi

if [ "$ACTION" = "restore" ]; then
  [ -n "${NOX_SNAPSHOT:-}" ] || fail "$(msg restore_which)"
  requested="$NOX_SNAPSHOT"
  # Root unpacks it into the service's home, so only what Nox itself made counts: a root-owned
  # snapshot directly in the backup folder. `realpath` resolves `..` and links before the check.
  file="$(realpath -e -- "$requested" 2>/dev/null || true)"
  [ -n "$file" ] && [ -f "$file" ] || fail "$(msg restore_missing "$requested")"
  [[ "$file" == "$BACKUP_DIR"/nox-*.tar.gz && "$(dirname "$file")" == "$BACKUP_DIR" ]] \
    || fail "$(msg restore_outside "$BACKUP_DIR")"
  [ "$(stat -c %u "$file")" = 0 ] || fail "$(msg restore_not_root "$file")"
  systemctl stop nox 2>/dev/null || true
  phase="stopped"
  restore_data "$file"
  systemctl start nox
  phase=""
  # A later `nox rollback` would put the pre-update snapshot over the data just restored.
  if [ -e "$STATE/previous-release" ]; then
    rm -f -- "$STATE/previous-release" "$STATE/previous-snapshot"
    note "$(msg restore_no_rollback)"
  fi
  wait_healthy 90 && say "$(msg restore_done "$file")" || fail "$(msg restore_no_answer)"
  exit 0
fi

[ "$ACTION" = "install" ] || fail "$(msg unknown_action "$ACTION")"

case "$(uname -m)" in
  aarch64|arm64|x86_64) ;;
  armv7l|armv6l) fail "$(msg arch_32bit)" ;;
  *) fail "$(msg arch_unsupported "$(uname -m)")" ;;
esac

if command -v apt-get >/dev/null 2>&1; then
  # bubblewrap + python3: the command sandbox (bwrap, and the tiny relay that gives a sandboxed
  # program its only way out - the egress proxy). Without them Nox refuses to run commands.
  say "$(msg step_packages)"
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git curl ca-certificates tar sudo bubblewrap python3 >/dev/null
else
  note "$(msg untested_system)"
  for tool in git curl tar sha256sum sudo; do command -v "$tool" >/dev/null 2>&1 || fail "$(msg tool_missing "$tool")"; done
  command -v bwrap >/dev/null 2>&1 || note "$(msg bwrap_missing)"
fi

if [ -e "$PREFIX" ] && [ ! -d "$SRC/.git" ] && [ -n "$(ls -A "$PREFIX" 2>/dev/null)" ]; then
  fail "$(msg prefix_foreign "$PREFIX")"
fi

# ---- which version --------------------------------------------------------------------------------

say "$(msg step_source)"
install -d -m 0755 "$PREFIX" "$RELEASES" "$STATE"
if [ -d "$SRC/.git" ]; then
  "${GIT[@]}" -C "$SRC" remote set-url origin "$SOURCE"
  "${GIT[@]}" -C "$SRC" fetch --quiet --tags --force origin
else
  "${GIT[@]}" clone --quiet "$SOURCE" "$SRC"
fi
if [ -n "${NOX_SOURCE:-}" ]; then
  # A local checkout may sit on a commit no branch points to (CI checks out a detached HEAD).
  "${GIT[@]}" -C "$SRC" fetch --quiet origin HEAD
fi

if [ -z "${NOX_REF:-}" ]; then
  if [ -n "${NOX_SOURCE:-}" ]; then
    NOX_REF="$("${GIT[@]}" -C "$NOX_SOURCE" rev-parse HEAD)"
  else
    # Releases only: a tag like v0.7.0-rc1 sorts above v0.7.0 but is not "the newest version".
    NOX_REF="$("${GIT[@]}" -C "$SRC" tag --list 'v*' --sort=-v:refname | grep -E '^v[0-9]+[.][0-9]+[.][0-9]+$' | head -n1 || true)"
  fi
  [ -n "$NOX_REF" ] || fail "$(msg no_version)"
fi
[[ "$NOX_REF" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] || fail "$(msg invalid_version "$NOX_REF")"
if "${GIT[@]}" -C "$SRC" rev-parse --verify --quiet "refs/remotes/origin/$NOX_REF^{commit}" >/dev/null; then
  commit="$("${GIT[@]}" -C "$SRC" rev-parse "refs/remotes/origin/$NOX_REF^{commit}")"
elif "${GIT[@]}" -C "$SRC" rev-parse --verify --quiet "refs/tags/$NOX_REF^{commit}" >/dev/null; then
  commit="$("${GIT[@]}" -C "$SRC" rev-parse "refs/tags/$NOX_REF^{commit}")"
else
  commit="$("${GIT[@]}" -C "$SRC" rev-parse --verify --quiet "$NOX_REF^{commit}" || true)"
fi
[ -n "$commit" ] || fail "$(msg version_missing "$NOX_REF")"
# The guard is about tags older than Linux support; a commit is judged by what it contains.
"${GIT[@]}" -C "$SRC" cat-file -e "$commit:installer/linux/nox.service" 2>/dev/null \
  || fail "$(msg windows_only "$NOX_REF" "$MIN_VERSION")"
if [[ "$NOX_REF" =~ ^v[0-9] ]] && ! version_at_least "${NOX_REF#v}" "$MIN_VERSION"; then
  fail "$(msg windows_only "$NOX_REF" "$MIN_VERSION")"
fi
release="${commit:0:12}"
label="$NOX_REF"; [[ "$NOX_REF" =~ ^[0-9a-f]{40}$ ]] && label="$release"

# ---- service user ---------------------------------------------------------------------------------

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  say "$(msg step_user "$SERVICE_USER")"
  useradd --system --home-dir "$NOX_HOME_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$NOX_HOME_DIR"

# ---- the command sandbox on Ubuntu 23.10+ -------------------------------------------------------
# There AppArmor lets a program create user namespaces only with a profile that allows it, and
# bwrap needs one. Nothing is changed for /usr/bin/bwrap or any other user: Nox gets its own copy,
# executable only by the service user (root:nox 0750), and a profile attached to that path alone.
# Inside the sandbox bwrap drops every capability and, with --disable-userns, forbids further user
# namespaces. Raspberry Pi OS and Debian have no such restriction: nothing is installed there, and
# leftovers of an earlier install are removed.
nox_bwrap=/usr/lib/nox/bwrap
nox_profile=/etc/apparmor.d/nox-bwrap
nox_apt_hook=/etc/apt/apt.conf.d/99nox-bwrap
restrict=/proc/sys/kernel/apparmor_restrict_unprivileged_userns
remove_nox_bwrap() {
  # Unload first: a profile that stays loaded keeps applying after its file is gone.
  if [ -e "$nox_profile" ] && command -v apparmor_parser >/dev/null 2>&1; then
    apparmor_parser -R "$nox_profile" 2>/dev/null || true
  fi
  rm -f "$nox_profile" "$nox_bwrap" "$nox_apt_hook"
}
if [ -r "$restrict" ] && [ "$(cat "$restrict")" = "1" ] && [ -x /usr/bin/bwrap ] && command -v apparmor_parser >/dev/null 2>&1; then
  say "$(msg step_apparmor)"
  remove_nox_bwrap  # also unloads a profile an earlier installer version attached elsewhere
  install -d -m 0755 /usr/lib/nox
  install -m 0750 -o root -g "$SERVICE_USER" /usr/bin/bwrap "$nox_bwrap"
  # bubblewrap security updates reach the copy too: refreshed after every dpkg run. (Nox also
  # refuses to use a copy that differs from /usr/bin/bwrap.)
  cat > "$nox_apt_hook" <<APTHOOK
// Installed by Nox: keep its bwrap copy identical to the bubblewrap package's.
DPkg::Post-Invoke { "if [ -e $nox_bwrap ] && [ -x /usr/bin/bwrap ] && ! cmp -s /usr/bin/bwrap $nox_bwrap; then install -m 0750 -o root -g $SERVICE_USER /usr/bin/bwrap $nox_bwrap; fi"; };
APTHOOK
  cat > "$nox_profile" <<'APPARMOR'
# Installed by Nox (scripts/install-linux.sh). Only Nox's own copy of bwrap - executable by the
# nox service user alone - may create user namespaces; /usr/bin/bwrap stays restricted. Inside,
# bwrap drops every capability and forbids further user namespaces (--disable-userns).
abi <abi/4.0>,
include <tunables/global>

profile nox-bwrap /usr/lib/nox/bwrap flags=(unconfined) {
  userns,
  include if exists <local/nox-bwrap>
}
APPARMOR
  if ! apparmor_parser -r "$nox_profile"; then
    remove_nox_bwrap
    note "$(msg apparmor_failed)"
  fi
elif [ -e "$nox_profile" ] || [ -e "$nox_bwrap" ] || [ -e "$nox_apt_hook" ]; then
  remove_nox_bwrap
fi

# ---- build the release (the running version is untouched) ----------------------------------------

work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT

if [ -f "$RELEASES/$release/.complete" ]; then
  say "$(msg release_ready "$label")"
else
  say "$(msg step_release "$label")"
  # Built under its final name: a Python environment cannot be moved (its scripts carry absolute
  # paths). Until `.complete` exists the folder counts as unfinished and is rebuilt next time.
  staging="$RELEASES/$release"
  rm -rf -- "$staging"
  mkdir -p "$staging"
  "${GIT[@]}" -C "$SRC" archive "$commit" | tar -x -C "$staging"

  say "$(msg step_python "$UV_VERSION")"
  export UV_INSTALL_DIR="$PREFIX/.tools" UV_PYTHON_INSTALL_DIR="$PREFIX/.python" UV_NO_MODIFY_PATH=1
  export UV_CACHE_DIR="$PREFIX/.cache/uv"
  if [ ! -x "$UV_INSTALL_DIR/uv" ] || [ "$("$UV_INSTALL_DIR/uv" --version | awk '{print $2}')" != "$UV_VERSION" ]; then
    install_uv
  fi
  # Bytecode is compiled now: the service may not write into its own release folder, and without
  # .pyc files a Pi would recompile every module on every start.
  (cd "$staging" && UV_COMPILE_BYTECODE=1 "$UV_INSTALL_DIR/uv" sync --quiet --frozen --no-build --python 3.13)
  "$UV_INSTALL_DIR/uv" cache prune --ci --quiet || true  # an SD card is small

  say "$(msg step_ui)"
  asset="nox-ui-${NOX_REF//\//-}.tar.gz"
  base="$REPO_URL/releases/download/$NOX_REF"
  if [ -z "${NOX_SOURCE:-}" ] \
    && "${CURL[@]}" -o "$work/$asset" "$base/$asset" 2>/dev/null \
    && "${CURL[@]}" -o "$work/$asset.sha256" "$base/$asset.sha256" 2>/dev/null; then
    (cd "$work" && sha256sum --quiet -c "$asset.sha256") || fail "$(msg ui_checksum)"
    verify_bundle "$work/$asset" || fail "$(msg ui_unexpected)"
    mkdir "$work/extract"
    tar -xzf "$work/$asset" -C "$work/extract" --no-same-owner --no-same-permissions
    for app in dashboard pet; do
      rm -rf -- "$staging/ui/$app/dist"
      mv "$work/extract/ui/$app/dist" "$staging/ui/$app/dist"
    done
  else
    note "$(msg ui_none "$label")"
    note "$(msg ui_none_hint)"
  fi
  printf '%s\n' "$NOX_REF" > "$staging/.complete"
fi

# ---- settings (first install only) ---------------------------------------------------------------

[ -L "$NOX_HOME_DIR/user.yaml" ] && fail "$(msg settings_link "$NOX_HOME_DIR/user.yaml")"
if [ ! -e "$NOX_HOME_DIR/user.yaml" ]; then
  say "$(msg step_settings)"
  cat > "$work/user.yaml" <<'YAML'
# Nox as a server (written once by install-linux.sh; edit freely, it is never overwritten).
# Desktop plugins (Twitch, OBS) belong on the PC you stream from.
plugins:
  enabled: []
# Paired phones, tablets and PCs in the home network open the dashboard here (pair one with
# `nox pair`). A server nobody can reach is of no use, so this is on.
ipc:
  lan:
    enabled: true
YAML
  # Written into a root-owned temp folder and installed with its final owner and mode, so root
  # never writes through a path inside the service user's folder.
  install -m 0600 -o "$SERVICE_USER" -g "$SERVICE_USER" "$work/user.yaml" "$NOX_HOME_DIR/user.yaml"
fi

# ---- switch ---------------------------------------------------------------------------------------

previous_release="$(basename "$(readlink "$CURRENT" 2>/dev/null || true)")"
[ "$previous_release" = "." ] && previous_release=""

if [ -n "$previous_release" ] && [ "$previous_release" != "$release" ]; then
  say "$(msg step_backup)"
  install -d -m 0700 "$BACKUP_DIR"
  systemctl stop nox 2>/dev/null || true
  phase="stopped"
  snapshot="$BACKUP_DIR/nox-$(date +%Y%m%d-%H%M%S)-$previous_release.tar.gz"
  home_name="$(basename "$NOX_HOME_DIR")"
  # Program caches and the AI's own install are not data; they would fill an SD card fast.
  (umask 077 && tar -czf "$snapshot" -C "$(dirname "$NOX_HOME_DIR")" \
    --exclude="./$home_name/.local" --exclude="./$home_name/.cache" \
    --exclude="./$home_name/models" --exclude="./$home_name/runtime" "./$home_name") \
    || fail "$(msg backup_failed)"
  find "$BACKUP_DIR" -maxdepth 1 -name 'nox-*.tar.gz' -printf '%T@ %p\n' | sort -rn | tail -n +6 \
    | cut -d' ' -f2- | xargs -r rm -f --
  note "$(msg backup_done "$snapshot" "$(du -h "$snapshot" | cut -f1)")"
  printf '%s\n' "$previous_release" > "$STATE/previous-release"
  printf '%s\n' "$snapshot" > "$STATE/previous-snapshot"
fi

say "$(msg step_service)"
switch_to "$release"
[ -n "$snapshot" ] && phase="switched"
install_service_files
systemctl enable --quiet nox
systemctl restart nox

if ! wait_healthy 90; then
  say "$(msg no_answer "$label")"
  journalctl -u nox -n 40 --no-pager || true
  if [ "$phase" = "switched" ]; then
    rollback_to "$previous_release" "$snapshot" || true
  fi
  trap - ERR
  exit 1
fi
phase=""
trap - ERR

# Keep the running version, the one it replaced and the one `nox rollback` points to - after
# running the same version again that is still the one from the last real update. Older ones go.
rollback_target="$(cat "$STATE/previous-release" 2>/dev/null || true)"
for old in "$RELEASES"/*/; do
  name="$(basename "$old")"
  [ "$name" = "$release" ] || [ "$name" = "$previous_release" ] || [ "$name" = "$rollback_target" ] \
    || rm -rf -- "$old"
done

say "$(msg running "$label")"
if [ -n "$snapshot" ]; then
  note "$(msg rollback_hint)"
fi
# Folders kept aside by an earlier restore hold a full copy of the data (secrets included) and stay
# until the user removes them; an SD card fills up otherwise.
for kept_aside in "$NOX_HOME_DIR".vor-wiederherstellung-*; do
  [ -d "$kept_aside" ] || continue
  note "$(msg kept_aside "$kept_aside" "$(du -sh "$kept_aside" | cut -f1)" "$kept_aside")"
done
if ! grep -qE '^[[:space:]]+enabled:[[:space:]]+true' <(sed -n '/^ipc:/,/^[^[:space:]#]/p' "$NOX_HOME_DIR/user.yaml" 2>/dev/null); then
  note "$(msg lan_off "$NOX_HOME_DIR/user.yaml")"
  note "$(msg lan_off_how)"
elif command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
  note "$(msg firewall)"
fi
if ! sudo -u "$SERVICE_USER" test -s "$NOX_HOME_DIR/.claude/.credentials.json"; then
  note "$(msg no_ai)"
fi
case "$(timedatectl show -p Timezone --value 2>/dev/null || echo unknown)" in
  Etc/UTC|UTC|Europe/London|unknown)
    note "$(msg timezone "$(timedatectl show -p Timezone --value 2>/dev/null || msg timezone_unknown)")"
    note "$(msg timezone_hint)" ;;
esac
guide="$(docs_url "$([[ "$NOX_REF" =~ ^v[0-9] ]] && echo "$NOX_REF" || echo develop)")"
if [ "$UI_LANG" = de ]; then
  cat <<INFO

  Nächste Schritte:
    1. KI anmelden (empfohlen):   nox claude-login
    2. Name, PIN, Zugänge:        nox onboard
    3. Handy oder Tablet koppeln: nox pair
    4. Prüfen:                    nox doctor
  Anleitung: $guide

  Im Alltag:
    Zustand        systemctl status nox
    Protokoll      journalctl -u nox -f
    Dateien        nox where
    Update         nox update          (vorher wird gesichert)
    Zurück         nox rollback        (Version und Daten von vor dem letzten Update)

INFO
else
  cat <<INFO

  Next steps:
    1. Sign in an AI (recommended): nox claude-login
    2. Name, PIN, accounts:         nox onboard
    3. Pair a phone or tablet:      nox pair
    4. Check:                       nox doctor
  Guide: $guide

  Day to day:
    Status         systemctl status nox
    Log            journalctl -u nox -f
    Files          nox where
    Update         nox update          (a backup is taken first)
    Back           nox rollback        (version and data from before the last update)

INFO
fi
