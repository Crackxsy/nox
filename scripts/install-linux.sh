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
CURL=(curl --proto '=https' --tlsv1.2 -fsSL)
SOURCE="${NOX_SOURCE:-$REPO_URL}"
# Ownership checks stay on, except for the one checkout NOX_SOURCE names (CI's is not root's).
GIT=(git)
if [ -n "${NOX_SOURCE:-}" ]; then
  GIT+=(-c "safe.directory=$NOX_SOURCE" -c "safe.directory=$NOX_SOURCE/.git")
fi
ACTION="${NOX_ACTION:-install}"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }

#: What the ERR trap has to undo: "" (nothing), "stopped" (old version stopped, link unchanged) or
#: "switched" (link already points at the new version).
phase=""
previous_release=""
snapshot=""

fail() {
  trap - ERR  # a failure while undoing must not start the undoing again
  printf '\nFehler: %s\n' "$*" >&2
  case "$phase" in
    stopped)
      systemctl start nox 2>/dev/null && printf 'Die bisherige Version läuft wieder, unverändert.\n' >&2 ;;
    switched)
      rollback_to "$previous_release" "$snapshot" || true ;;
  esac
  exit 1
}
trap 'fail "Schritt in Zeile $LINENO fehlgeschlagen. Internetverbindung prüfen und den Befehl noch einmal starten - er macht dort weiter, wo es geht."' ERR

# ---- helpers --------------------------------------------------------------------------------------

version_at_least() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]; }

docs_url() { printf '%s/blob/%s/docs/RASPBERRY_PI.de.md' "$REPO_URL" "${1:-develop}"; }

wait_healthy() {
  local _
  for _ in $(seq 1 "${1:-90}"); do
    if curl -fsS -o /dev/null "http://127.0.0.1:$HTTP_PORT/health" 2>/dev/null; then return 0; fi
    sleep 1
  done
  return 1
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
  [ -f "$file" ] || { printf 'Schnappschuss %s gibt es nicht.\n' "$file" >&2; return 1; }
  # A truncated or corrupt archive is found before anything is moved.
  tar -tzf "$file" >/dev/null 2>&1 || { printf 'Schnappschuss %s ist beschädigt; nichts wurde geändert.\n' "$file" >&2; return 1; }
  stamp="$(date +%Y%m%d-%H%M%S)"
  aside="$NOX_HOME_DIR.vor-wiederherstellung-$stamp"
  mv -T "$NOX_HOME_DIR" "$aside" || return 1
  if ! tar -xzf "$file" -C "$(dirname "$NOX_HOME_DIR")"; then
    rm -rf -- "$NOX_HOME_DIR"
    mv -T "$aside" "$NOX_HOME_DIR"
    printf 'Entpacken fehlgeschlagen (Speicher voll?); die bisherigen Daten sind unverändert zurück.\n' >&2
    return 1
  fi
  # What snapshots leave out (the AI's own install, caches, models) moves back from the old folder.
  for kept in .local .cache models; do
    if [ -e "$aside/$kept" ] && [ ! -e "$NOX_HOME_DIR/$kept" ]; then mv "$aside/$kept" "$NOX_HOME_DIR/"; fi
  done
  chmod 0700 "$NOX_HOME_DIR"
  note "Daten aus $file wiederhergestellt. Der Stand davor liegt in $aside - löschen, sobald alles läuft."
}

#: Back to an earlier release, with the data as they were before the update that left it. Once:
#: afterwards there is no step further back, so a second rollback cannot put the same old data
#: back over everything written since.
rollback_to() {
  local release="$1" file="$2"
  # Cleared first: a failure from here on must not make `fail` start another rollback.
  phase=""
  [ -n "$release" ] && [ -d "$RELEASES/$release" ] || {
    printf 'Es gibt keinen weiteren Schritt zurück. Zu einer neueren Version: nox update\n' >&2
    return 1
  }
  say "Zurück zur vorherigen Version ($release)"
  systemctl stop nox 2>/dev/null || true
  if [ -n "$file" ]; then
    if ! restore_data "$file"; then
      systemctl start nox 2>/dev/null || true
      printf 'Zurückschalten abgebrochen: Daten unverändert, es läuft weiter Version %s. Protokoll: journalctl -u nox -n 50\n' \
        "$(basename "$(readlink "$CURRENT")")" >&2
      return 1
    fi
  fi
  switch_to "$release"
  install_service_files
  systemctl start nox
  phase=""
  rm -f -- "$STATE/previous-release" "$STATE/previous-snapshot"
  if wait_healthy 90; then
    note "Die vorherige Version läuft wieder, mit den Daten von vor dem Update."
    note "Was seit dem Update dazukam, liegt im beiseitegelegten Ordner (siehe oben)."
  else
    note "Auch die vorherige Version antwortet nicht. Protokoll: journalctl -u nox -n 50"
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

[ "$(id -u)" -eq 0 ] || fail "bitte mit sudo starten: curl -fsSL <Adresse> | sudo bash  (oder: sudo bash install-linux.sh)"
[ "$(uname -s)" = "Linux" ] || fail "dieses Skript ist für Linux. Unter Windows: scripts/setup.ps1."

# One installer at a time: two runs (two `nox update`s from two terminals, say) would race on the
# release folders, the snapshot and the link.
exec 9>/run/nox-install.lock
flock -n 9 || fail "ein anderer Installations- oder Update-Lauf ist gerade aktiv. Bitte warten und erneut versuchen."

if [ "$ACTION" = "verify-bundle" ]; then
  trap - ERR
  verify_bundle "${NOX_BUNDLE:?NOX_BUNDLE fehlt}"
  exit $?
fi

if [ "$ACTION" = "rollback" ]; then
  previous_release="$(cat "$STATE/previous-release" 2>/dev/null || true)"
  snapshot="$(cat "$STATE/previous-snapshot" 2>/dev/null || true)"
  rollback_to "$previous_release" "$snapshot" || exit 1
  exit 0
fi

if [ "$ACTION" = "restore" ]; then
  requested="${NOX_SNAPSHOT:?welcher Schnappschuss? nox restore <datei>  (Liste: nox snapshots)}"
  # Root unpacks it into the service's home, so only what Nox itself made counts: a root-owned
  # snapshot directly in the backup folder. `realpath` resolves `..` and links before the check.
  file="$(realpath -e -- "$requested" 2>/dev/null || true)"
  [ -n "$file" ] && [ -f "$file" ] || fail "Schnappschuss $requested gibt es nicht. Liste: nox snapshots"
  [[ "$file" == "$BACKUP_DIR"/nox-*.tar.gz && "$(dirname "$file")" == "$BACKUP_DIR" ]] \
    || fail "nur Schnappschüsse aus $BACKUP_DIR lassen sich wiederherstellen (Liste: nox snapshots)."
  [ "$(stat -c %u "$file")" = 0 ] || fail "$file gehört nicht root - so legt Nox keine Schnappschüsse an."
  systemctl stop nox 2>/dev/null || true
  phase="stopped"
  restore_data "$file"
  systemctl start nox
  phase=""
  # A later `nox rollback` would put the pre-update snapshot over the data just restored.
  if [ -e "$STATE/previous-release" ]; then
    rm -f -- "$STATE/previous-release" "$STATE/previous-snapshot"
    note "Ein \`nox rollback\` gibt es danach nicht mehr: es würde diese Daten wieder überschreiben."
  fi
  wait_healthy 90 && say "Nox läuft wieder, mit den Daten aus $file." || fail "Nox antwortet nach dem Wiederherstellen nicht. Protokoll: journalctl -u nox -n 50"
  exit 0
fi

[ "$ACTION" = "install" ] || fail "unbekannte Aktion: $ACTION"

case "$(uname -m)" in
  aarch64|arm64|x86_64) ;;
  armv7l|armv6l) fail "32-bit-System erkannt. Nox braucht Raspberry Pi OS 64-bit (Pi 3, 4, 5, 400 oder Zero 2 W)." ;;
  *) fail "nicht unterstützte Architektur: $(uname -m)" ;;
esac

if command -v apt-get >/dev/null 2>&1; then
  say "Pakete installieren (git, curl)"
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git curl ca-certificates tar sudo >/dev/null
else
  note "Hinweis: getestet ist Nox auf Raspberry Pi OS, Debian und Ubuntu. Auf diesem System kann etwas fehlen."
  for tool in git curl tar sha256sum sudo; do command -v "$tool" >/dev/null 2>&1 || fail "$tool fehlt, bitte installieren."; done
fi

if [ -e "$PREFIX" ] && [ ! -d "$SRC/.git" ] && [ -n "$(ls -A "$PREFIX" 2>/dev/null)" ]; then
  fail "$PREFIX existiert schon, ist aber keine Nox-Installation. Bitte prüfen und den Ordner entfernen oder umbenennen."
fi

# ---- which version --------------------------------------------------------------------------------

say "Nox-Quelle aktualisieren"
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
  [ -n "$NOX_REF" ] || fail "keine Nox-Version gefunden. Internetverbindung prüfen."
fi
[[ "$NOX_REF" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] || fail "ungültige Version: $NOX_REF"
if "${GIT[@]}" -C "$SRC" rev-parse --verify --quiet "refs/remotes/origin/$NOX_REF^{commit}" >/dev/null; then
  commit="$("${GIT[@]}" -C "$SRC" rev-parse "refs/remotes/origin/$NOX_REF^{commit}")"
elif "${GIT[@]}" -C "$SRC" rev-parse --verify --quiet "refs/tags/$NOX_REF^{commit}" >/dev/null; then
  commit="$("${GIT[@]}" -C "$SRC" rev-parse "refs/tags/$NOX_REF^{commit}")"
else
  commit="$("${GIT[@]}" -C "$SRC" rev-parse --verify --quiet "$NOX_REF^{commit}" || true)"
fi
[ -n "$commit" ] || fail "Version $NOX_REF gibt es nicht."
# The guard is about tags older than Linux support; a commit is judged by what it contains.
"${GIT[@]}" -C "$SRC" cat-file -e "$commit:installer/linux/nox.service" 2>/dev/null \
  || fail "Nox $NOX_REF läuft nur unter Windows. Linux-Server gibt es ab v$MIN_VERSION."
if [[ "$NOX_REF" =~ ^v[0-9] ]] && ! version_at_least "${NOX_REF#v}" "$MIN_VERSION"; then
  fail "Nox $NOX_REF läuft nur unter Windows. Linux-Server gibt es ab v$MIN_VERSION."
fi
release="${commit:0:12}"
label="$NOX_REF"; [[ "$NOX_REF" =~ ^[0-9a-f]{40}$ ]] && label="$release"

# ---- service user ---------------------------------------------------------------------------------

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  say "Systembenutzer $SERVICE_USER anlegen"
  useradd --system --home-dir "$NOX_HOME_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$NOX_HOME_DIR"

# ---- build the release (the running version is untouched) ----------------------------------------

work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT

if [ -f "$RELEASES/$release/.complete" ]; then
  say "Nox $label ist schon vorbereitet"
else
  say "Nox $label vorbereiten"
  # Built under its final name: a Python environment cannot be moved (its scripts carry absolute
  # paths). Until `.complete` exists the folder counts as unfinished and is rebuilt next time.
  staging="$RELEASES/$release"
  rm -rf -- "$staging"
  mkdir -p "$staging"
  "${GIT[@]}" -C "$SRC" archive "$commit" | tar -x -C "$staging"

  say "Python 3.13 und Abhängigkeiten (uv $UV_VERSION, nur fertige Pakete)"
  export UV_INSTALL_DIR="$PREFIX/.tools" UV_PYTHON_INSTALL_DIR="$PREFIX/.python" UV_NO_MODIFY_PATH=1
  export UV_CACHE_DIR="$PREFIX/.cache/uv"
  if [ ! -x "$UV_INSTALL_DIR/uv" ] || [ "$("$UV_INSTALL_DIR/uv" --version | awk '{print $2}')" != "$UV_VERSION" ]; then
    "${CURL[@]}" "https://astral.sh/uv/$UV_VERSION/install.sh" | sh >/dev/null
  fi
  # Bytecode is compiled now: the service may not write into its own release folder, and without
  # .pyc files a Pi would recompile every module on every start.
  (cd "$staging" && UV_COMPILE_BYTECODE=1 "$UV_INSTALL_DIR/uv" sync --quiet --frozen --no-build --python 3.13)
  "$UV_INSTALL_DIR/uv" cache prune --ci --quiet || true  # an SD card is small

  say "Oberflächen (Dashboard und Pet) installieren"
  asset="nox-ui-${NOX_REF//\//-}.tar.gz"
  base="$REPO_URL/releases/download/$NOX_REF"
  if [ -z "${NOX_SOURCE:-}" ] \
    && "${CURL[@]}" -o "$work/$asset" "$base/$asset" 2>/dev/null \
    && "${CURL[@]}" -o "$work/$asset.sha256" "$base/$asset.sha256" 2>/dev/null; then
    (cd "$work" && sha256sum --quiet -c "$asset.sha256") || fail "das Oberflächen-Paket stimmt nicht mit seiner Prüfsumme überein. Nichts wurde geändert."
    verify_bundle "$work/$asset" || fail "das Oberflächen-Paket enthält Unerwartetes (Verknüpfungen oder fremde Dateien). Nichts wurde geändert."
    mkdir "$work/extract"
    tar -xzf "$work/$asset" -C "$work/extract" --no-same-owner --no-same-permissions
    for app in dashboard pet; do
      rm -rf -- "$staging/ui/$app/dist"
      mv "$work/extract/ui/$app/dist" "$staging/ui/$app/dist"
    done
  else
    note "Für $label gibt es kein fertiges Oberflächen-Paket (Entwicklungsversion)."
    note "Der Kern läuft trotzdem; das Dashboard zeigt bis dahin eine Hinweisseite."
  fi
  printf '%s\n' "$NOX_REF" > "$staging/.complete"
fi

# ---- settings (first install only) ---------------------------------------------------------------

[ -L "$NOX_HOME_DIR/user.yaml" ] && fail "$NOX_HOME_DIR/user.yaml ist eine Verknüpfung. Aus Sicherheitsgründen wird sie nicht angefasst."
if [ ! -e "$NOX_HOME_DIR/user.yaml" ]; then
  say "Grundeinstellungen für einen Server schreiben"
  cat > "$work/user.yaml" <<'YAML'
# Nox als Server (einmal von install-linux.sh geschrieben; frei änderbar, wird nie überschrieben).
# Desktop-Plugins (Twitch, OBS) gehören auf den PC, von dem aus gestreamt wird.
plugins:
  enabled: []
YAML
  # Written into a root-owned temp folder and installed with its final owner and mode, so root
  # never writes through a path inside the service user's folder.
  install -m 0600 -o "$SERVICE_USER" -g "$SERVICE_USER" "$work/user.yaml" "$NOX_HOME_DIR/user.yaml"
fi

# ---- switch ---------------------------------------------------------------------------------------

previous_release="$(basename "$(readlink "$CURRENT" 2>/dev/null || true)")"
[ "$previous_release" = "." ] && previous_release=""

if [ -n "$previous_release" ] && [ "$previous_release" != "$release" ]; then
  say "Sicherung vor dem Update"
  install -d -m 0700 "$BACKUP_DIR"
  systemctl stop nox 2>/dev/null || true
  phase="stopped"
  snapshot="$BACKUP_DIR/nox-$(date +%Y%m%d-%H%M%S)-$previous_release.tar.gz"
  home_name="$(basename "$NOX_HOME_DIR")"
  # Program caches and the AI's own install are not data; they would fill an SD card fast.
  (umask 077 && tar -czf "$snapshot" -C "$(dirname "$NOX_HOME_DIR")" \
    --exclude="./$home_name/.local" --exclude="./$home_name/.cache" \
    --exclude="./$home_name/models" --exclude="./$home_name/runtime" "./$home_name") \
    || fail "die Sicherung ist fehlgeschlagen (Speicher voll?). Nichts wurde umgestellt."
  find "$BACKUP_DIR" -maxdepth 1 -name 'nox-*.tar.gz' -printf '%T@ %p\n' | sort -rn | tail -n +6 \
    | cut -d' ' -f2- | xargs -r rm -f --
  note "Gesichert: $snapshot ($(du -h "$snapshot" | cut -f1)). Enthält auch Zugangsdaten - nur root kann es lesen."
  printf '%s\n' "$previous_release" > "$STATE/previous-release"
  printf '%s\n' "$snapshot" > "$STATE/previous-snapshot"
fi

say "Dienst einrichten und starten"
switch_to "$release"
[ -n "$snapshot" ] && phase="switched"
install_service_files
systemctl enable --quiet nox
systemctl restart nox

if ! wait_healthy 90; then
  say "Nox $label antwortet nicht. Die letzten Zeilen des Protokolls:"
  journalctl -u nox -n 40 --no-pager || true
  if [ "$phase" = "switched" ]; then
    rollback_to "$previous_release" "$snapshot" || true
  fi
  trap - ERR
  exit 1
fi
phase=""
trap - ERR

# Keep the running and the previous version; older ones go.
for old in "$RELEASES"/*/; do
  name="$(basename "$old")"
  [ "$name" = "$release" ] || [ "$name" = "$previous_release" ] || rm -rf -- "$old"
done

say "Nox läuft ($label)."
if [ -n "$snapshot" ]; then
  note "Zurück zur vorherigen Version samt Daten: nox rollback"
fi
# Folders kept aside by an earlier restore hold a full copy of the data (secrets included) and stay
# until the user removes them; an SD card fills up otherwise.
for kept_aside in "$NOX_HOME_DIR".vor-wiederherstellung-*; do
  [ -d "$kept_aside" ] || continue
  note "Beiseitegelegt: $kept_aside ($(du -sh "$kept_aside" | cut -f1)) - wenn alles läuft: sudo rm -r $kept_aside"
done
if ! sudo -u "$SERVICE_USER" test -s "$NOX_HOME_DIR/.claude/.credentials.json"; then
  note "Noch keine KI angemeldet: Nox antwortet mit einfachen eingebauten Regeln, bis du \"nox claude-login\" ausführst."
fi
case "$(timedatectl show -p Timezone --value 2>/dev/null || echo unknown)" in
  Etc/UTC|UTC|Europe/London|unknown)
    note "Zeitzone: $(timedatectl show -p Timezone --value 2>/dev/null || echo unbekannt). Erinnerungen und Kalender richten sich danach."
    note "In Deutschland: sudo timedatectl set-timezone Europe/Berlin" ;;
esac
guide="$(docs_url "$([[ "$NOX_REF" =~ ^v[0-9] ]] && echo "$NOX_REF" || echo develop)")"
cat <<INFO

  Nächste Schritte:
    1. KI anmelden (empfohlen):   nox claude-login
    2. Name, PIN, Zugänge:        nox onboard
    3. Prüfen:                    nox doctor
  Anleitung: $guide

  Im Alltag:
    Zustand        systemctl status nox
    Protokoll      journalctl -u nox -f
    Dateien        nox where
    Update         nox update          (vorher wird gesichert)
    Zurück         nox rollback        (Version und Daten von vor dem letzten Update)

INFO
