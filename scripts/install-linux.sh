#!/usr/bin/env bash
# Install or update Nox as a server on Linux - a Raspberry Pi (64-bit Raspberry Pi OS), Debian or
# Ubuntu, on ARM64 or x86-64. One command, safe to run again for an update:
#
#   curl -fsSL https://raw.githubusercontent.com/Crackxsy/nox/develop/scripts/install-linux.sh | sudo bash
#
# Prefer to read it first: download it, look at it, then run `sudo bash install-linux.sh`.
#
# What it does, and nothing else:
#   * a system user `nox` with no login shell
#   * the program in /opt/nox (read-only to that user), its data in /var/lib/nox
#   * Python 3.13 and the dependencies through uv, inside /opt/nox, from prebuilt wheels only
#   * the prebuilt dashboard from the matching GitHub release, checked against its SHA-256
#   * the systemd service `nox`, started and enabled at boot, and the short command `nox`
#   * before an update: a snapshot of /var/lib/nox in /var/backups/nox (the last five are kept)
#
# Choosing a version: `sudo env NOX_REF=v0.6.0 bash install-linux.sh` (sudo drops other variables).
set -euo pipefail

REPO_URL="https://github.com/Crackxsy/nox"
PREFIX="/opt/nox"
NOX_HOME_DIR="/var/lib/nox"
BACKUP_DIR="/var/backups/nox"
SERVICE_USER="nox"
HTTP_PORT="47801"
#: The first release that runs on Linux. Older tags would install a Windows-only Nox.
MIN_VERSION="0.6.0"
UV_VERSION="0.12.18"
CURL=(curl --proto '=https' --tlsv1.2 -fsSL)

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
die() { printf '\nFehler: %s\n' "$*" >&2; exit 1; }
trap 'die "Schritt in Zeile $LINENO fehlgeschlagen. Internetverbindung prüfen und den Befehl noch einmal starten - er macht dort weiter, wo es geht."' ERR

[ "$(id -u)" -eq 0 ] || die "bitte mit sudo starten: curl -fsSL <Adresse> | sudo bash  (oder: sudo bash install-linux.sh)"
[ "$(uname -s)" = "Linux" ] || die "dieses Skript ist für Linux. Unter Windows: scripts/setup.ps1."

case "$(uname -m)" in
  aarch64|arm64|x86_64) ;;
  armv7l|armv6l) die "32-bit-System erkannt. Nox braucht Raspberry Pi OS 64-bit (Pi 3, 4, 5, 400 oder Zero 2 W)." ;;
  *) die "nicht unterstützte Architektur: $(uname -m)" ;;
esac

if command -v apt-get >/dev/null 2>&1; then
  say "Pakete installieren (git, curl)"
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git curl ca-certificates tar >/dev/null
else
  note "Hinweis: getestet ist Nox auf Raspberry Pi OS, Debian und Ubuntu. Auf diesem System kann etwas fehlen."
  for tool in git curl tar sha256sum; do command -v "$tool" >/dev/null 2>&1 || die "$tool fehlt, bitte installieren."; done
fi

# ---- which version --------------------------------------------------------------------------------

version_at_least() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]; }

if [ -z "${NOX_REF:-}" ]; then
  NOX_REF="$(git ls-remote --tags --refs --sort=-v:refname "$REPO_URL" 'v*' | head -n1 | sed 's#.*refs/tags/##')"
  [ -n "$NOX_REF" ] || die "keine Nox-Version gefunden. Internetverbindung prüfen."
fi
[[ "$NOX_REF" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] || die "ungültige Version: $NOX_REF"
if [[ "$NOX_REF" =~ ^v[0-9] ]] && ! version_at_least "${NOX_REF#v}" "$MIN_VERSION"; then
  die "Nox $NOX_REF läuft nur unter Windows. Linux-Server gibt es ab v$MIN_VERSION. Zum Ausprobieren der Entwicklungsversion: sudo env NOX_REF=develop bash install-linux.sh"
fi

# ---- user, folders, snapshot ---------------------------------------------------------------------

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  say "Systembenutzer $SERVICE_USER anlegen"
  useradd --system --home-dir "$NOX_HOME_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$NOX_HOME_DIR"

if [ -d "$PREFIX/.git" ]; then
  previous="$(git -C "$PREFIX" describe --tags --always 2>/dev/null || echo unknown)"
  say "Sicherung vor dem Update ($previous)"
  install -d -m 0700 "$BACKUP_DIR"
  systemctl stop nox 2>/dev/null || true
  tar -czf "$BACKUP_DIR/nox-$(date +%Y%m%d-%H%M%S)-$previous.tar.gz" -C "$(dirname "$NOX_HOME_DIR")" "$(basename "$NOX_HOME_DIR")"
  ls -1t "$BACKUP_DIR"/nox-*.tar.gz | tail -n +6 | xargs -r rm -f --
  printf '%s\n' "$previous" > "$PREFIX/.previous-version"
  note "Zurück zur alten Version: sudo env NOX_REF=$previous bash install-linux.sh"
elif [ -e "$PREFIX" ] && [ -n "$(ls -A "$PREFIX" 2>/dev/null)" ]; then
  die "$PREFIX existiert schon, ist aber keine Nox-Installation. Bitte prüfen und den Ordner entfernen oder umbenennen."
fi

# ---- program --------------------------------------------------------------------------------------

say "Nox $NOX_REF nach $PREFIX holen"
if [ -d "$PREFIX/.git" ]; then
  git -C "$PREFIX" fetch --quiet --tags --force origin
else
  git clone --quiet "$REPO_URL" "$PREFIX"
fi
if git -C "$PREFIX" rev-parse --verify --quiet "refs/remotes/origin/$NOX_REF" >/dev/null; then
  git -C "$PREFIX" -c advice.detachedHead=false checkout --quiet --force --detach "origin/$NOX_REF" --
else
  git -C "$PREFIX" -c advice.detachedHead=false checkout --quiet --force "refs/tags/$NOX_REF" --
fi
[ -f "$PREFIX/installer/linux/nox.service" ] || die "Nox $NOX_REF enthält keinen Linux-Dienst. Bitte eine Version ab v$MIN_VERSION wählen."

say "Python 3.13 und Abhängigkeiten (uv $UV_VERSION, nur fertige Pakete)"
export UV_INSTALL_DIR="$PREFIX/.tools" UV_PYTHON_INSTALL_DIR="$PREFIX/.python" UV_NO_MODIFY_PATH=1
export UV_CACHE_DIR="$PREFIX/.cache/uv"
if [ ! -x "$UV_INSTALL_DIR/uv" ] || [ "$("$UV_INSTALL_DIR/uv" --version | awk '{print $2}')" != "$UV_VERSION" ]; then
  "${CURL[@]}" "https://astral.sh/uv/$UV_VERSION/install.sh" | sh >/dev/null
fi
(cd "$PREFIX" && "$UV_INSTALL_DIR/uv" sync --quiet --frozen --no-build --python 3.13)

# ---- dashboard and pet window ---------------------------------------------------------------------

say "Oberflächen (Dashboard und Pet) installieren"
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT
asset="nox-ui-${NOX_REF//\//-}.tar.gz"
if "${CURL[@]}" -o "$work/$asset" "$REPO_URL/releases/download/$NOX_REF/$asset" 2>/dev/null \
  && "${CURL[@]}" -o "$work/$asset.sha256" "$REPO_URL/releases/download/$NOX_REF/$asset.sha256" 2>/dev/null; then
  (cd "$work" && sha256sum --quiet -c "$asset.sha256") || die "das Oberflächen-Paket stimmt nicht mit seiner Prüfsumme überein. Nichts wurde installiert."
  # Only the two bundle folders, only plain files and folders - no links, nothing else.
  if tar -tvzf "$work/$asset" | awk '{print substr($1,1,1)}' | grep -qv '^[-d]$'; then
    die "das Oberflächen-Paket enthält Verknüpfungen. Nichts wurde installiert."
  fi
  if tar -tzf "$work/$asset" | grep -qvE '^ui/(dashboard|pet)/dist(/|$)'; then
    die "das Oberflächen-Paket enthält unerwartete Dateien. Nichts wurde installiert."
  fi
  mkdir "$work/extract"
  tar -xzf "$work/$asset" -C "$work/extract" --no-same-owner --no-same-permissions
  for app in dashboard pet; do
    rm -rf -- "$PREFIX/ui/$app/dist"
    mv "$work/extract/ui/$app/dist" "$PREFIX/ui/$app/dist"
  done
else
  note "Für $NOX_REF gibt es kein fertiges Oberflächen-Paket (Entwicklungsversion)."
  note "Der Kern läuft trotzdem; das Dashboard zeigt bis dahin eine Hinweisseite."
fi

# ---- settings, service, command -------------------------------------------------------------------

if [ ! -f "$NOX_HOME_DIR/user.yaml" ]; then
  say "Grundeinstellungen für einen Server schreiben"
  cat > "$NOX_HOME_DIR/user.yaml" <<'YAML'
# Nox als Server (einmal von install-linux.sh geschrieben; frei änderbar, wird nie überschrieben).
# Desktop-Plugins (Twitch, OBS) gehören auf den PC, von dem aus gestreamt wird.
plugins:
  enabled: []
YAML
  chown "$SERVICE_USER:$SERVICE_USER" "$NOX_HOME_DIR/user.yaml"
  chmod 0600 "$NOX_HOME_DIR/user.yaml"
fi

say "Dienst einrichten und starten"
install -m 0644 "$PREFIX/installer/linux/nox.service" /etc/systemd/system/nox.service
install -m 0755 "$PREFIX/installer/linux/nox-command.sh" /usr/local/bin/nox
systemctl daemon-reload
systemctl enable --quiet nox
systemctl restart nox

ready=""
for _ in $(seq 1 60); do
  if curl -fsS -o /dev/null "http://127.0.0.1:$HTTP_PORT/health" 2>/dev/null; then ready=1; break; fi
  systemctl is-active --quiet nox || break
  sleep 1
done
if [ -z "$ready" ]; then
  say "Nox ist nicht gestartet. Die letzten Zeilen des Protokolls:"
  journalctl -u nox -n 40 --no-pager || true
  exit 1
fi
trap - ERR

say "Nox läuft ($NOX_REF)."
if ! sudo -u "$SERVICE_USER" test -x "$NOX_HOME_DIR/.local/bin/claude"; then
  note "Antworten kommen gerade nur aus einfachen eingebauten Regeln - es ist noch keine KI angemeldet."
fi
case "$(timedatectl show -p Timezone --value 2>/dev/null || echo unknown)" in
  Etc/UTC|UTC|Europe/London|unknown)
    note "Zeitzone: $(timedatectl show -p Timezone --value 2>/dev/null || echo unbekannt). Erinnerungen und Kalender richten sich danach."
    note "In Deutschland: sudo timedatectl set-timezone Europe/Berlin" ;;
esac
cat <<INFO

  Nächste Schritte:
    1. KI anmelden (empfohlen):   nox claude-login        (Anleitung: docs/RASPBERRY_PI.md)
    2. Name, PIN, Zugänge:        nox onboard
    3. Prüfen:                    nox doctor

  Im Alltag:
    Zustand        systemctl status nox
    Protokoll      journalctl -u nox -f
    Dateien        nox where
    Update         denselben Befehl noch einmal ausführen (vorher wird gesichert)

INFO
