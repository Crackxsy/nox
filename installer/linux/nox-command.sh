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
#   nox update [version] [--allow-downgrade]
#                          install a newer (or the given) version; the data is snapshotted first.
#                          An installation from a branch (develop) stays on that branch. Once this
#                          version trusts a release key, a release is checked against its signed
#                          manifest with this version's own `nox release verify` before its
#                          installer runs (docs/RELEASES.md); an older release only with
#                          --allow-downgrade.
#   nox doctor             as everywhere, plus one line on whether a newer release exists
#   nox rollback           back to the version before the last update, with its data
#   nox snapshots          list the snapshots taken before updates
#   nox restore <file>     put the data of one snapshot back (the program stays as it is)
#   nox claude-login       install Claude Code for the service user and start its login
#   nox pair               pair a phone, tablet or PC (as everywhere; QR code and link)
#   nox devices            the paired devices;  nox unpair <name>  removes one (a lost phone)
#   nox onboard            as everywhere, then restart the service so the answers take effect
#
# Language of this script's own messages: German when the system language is German (LC_ALL,
# LC_MESSAGES or LANG starting with "de"), English otherwise; NOX_LANG=de|en overrides it. The
# installer it runs gets the same language passed on.
set -euo pipefail

SERVICE_USER="nox"
NOX_HOME_DIR="/var/lib/nox"
CURRENT="/opt/nox/current"
PROGRAM="$CURRENT/.venv/bin/nox"
SRC="/opt/nox/src"
BACKUP_DIR="/var/backups/nox"
REPO_URL="https://github.com/Crackxsy/nox"

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

#: `msg`, then a line break.
msgln() { msg "$@"; printf '\n'; }

define updates_follow \
  'Updates: diese Installation folgt %s - nox update holt dessen neuesten Stand.' \
  'Updates: this installation follows %s - nox update brings it to its latest state.'
define updates_unchecked \
  'Updates: nicht geprüft (keine Verbindung zu GitHub).' \
  'Updates: not checked (no connection to GitHub).'
define updates_available \
  'Updates: %s ist da (installiert: %s). Installieren: nox update' \
  'Updates: %s is available (installed: %s). Install it: nox update'
define updates_current \
  'Updates: %s ist die neueste Version.' \
  'Updates: %s is the newest version.'
define invalid_version \
  'ungültige Version: %s' \
  'invalid version: %s'
define follows_branch \
  'Diese Installation folgt %s - sie wird auf dessen neuesten Stand gebracht.' \
  'This installation follows %s - it is brought to its latest state.'
define newest_release_hint \
  'Zum neuesten Release stattdessen: nox update <version>, z. B. nox update %s' \
  'For the newest release instead: nox update <version>, e.g. nox update %s'
define restore_which \
  'Welcher Schnappschuss? Liste: nox snapshots' \
  'Which snapshot? List: nox snapshots'
define snapshots_hint \
  'Wiederherstellen: nox restore <dateiname>   ·   Version und Daten zurück: nox rollback' \
  'Restore one: nox restore <file name>   ·   Version and data back: nox rollback'
define snapshots_none \
  'Noch keine Schnappschüsse - sie entstehen vor jedem Update.' \
  'No snapshots yet - one is taken before every update.'
define onboard_restart \
  'Nox neu starten, damit die Einstellungen gelten ...' \
  'Restarting Nox so that the settings take effect ...'
define sig_not_configured \
  'Hinweis: Release-Signaturen sind in dieser Version noch nicht eingerichtet - geprüft werden nur Prüfsummen.' \
  'Note: release signatures are not set up in this version yet - only checksums are checked.'
define sig_missing \
  'Release %s hat kein signiertes Manifest. Aus Sicherheitsgründen wird es nicht installiert; nichts wurde geändert.' \
  'Release %s has no signed manifest. For safety it is not installed; nothing was changed.'
define sig_refused \
  'Release %s wird nicht installiert (siehe oben); nichts wurde geändert.' \
  'Release %s is not installed (see above); nothing was changed.'
define sig_broken \
  'Die Signaturprüfung dieser Version funktioniert nicht (%s release keys). Nichts wurde geändert.' \
  'The signature check of this version does not work (%s release keys). Nothing was changed.'
define unknown_option \
  'unbekannte Option: %s' \
  'unknown option: %s'
define claude_install \
  'Claude Code für den Nox-Dienst installieren ...' \
  'Installing Claude Code for the Nox service ...'

as_service() {
  if [ "$(id -un)" = "$SERVICE_USER" ]; then
    cd "$NOX_HOME_DIR" && exec env NOX_HOME="$NOX_HOME_DIR" HOME="$NOX_HOME_DIR" "$@"
  fi
  exec sudo -u "$SERVICE_USER" -- env NOX_HOME="$NOX_HOME_DIR" HOME="$NOX_HOME_DIR" \
    bash -c 'cd "$HOME" && exec "$@"' nox "$@"
}

#: The copy of the installer, removed on exit. Global on purpose: the EXIT trap runs after
#: `run_installer` has returned, when a local variable would no longer exist (and `set -u` fails).
installer_copy=""
trap 'if [ -n "$installer_copy" ]; then rm -f -- "$installer_copy"; fi
if [ -n "${verify_dir:-}" ]; then rm -rf -- "$verify_dir"; fi' EXIT

#: The installer of the running version, run from a copy: an update replaces what it reads from.
run_installer() {
  installer_copy="$(mktemp)"
  cp "$CURRENT/scripts/install-linux.sh" "$installer_copy"
  sudo env "NOX_LANG=$UI_LANG" "$@" bash "$installer_copy"
}

#: The version the running release was installed from: a tag (v0.6.0), a branch (develop) or, for
#: a local checkout, a commit.
installed_ref() { sudo cat "$CURRENT/.complete" 2>/dev/null || true; }

#: The newest real release (vX.Y.Z, no pre-release) in the checkout the installer keeps.
newest_tag() {
  sudo git -C "$SRC" tag --list 'v*' --sort=-v:refname | grep -E '^v[0-9]+[.][0-9]+[.][0-9]+$' | head -n1 || true
}

#: The version `fetch_target_installer` resolved: its name (a tag, a branch) and its commit.
target_ref=""
target_commit=""
#: A folder for the downloaded manifest, removed on exit like the installer copy.
verify_dir=""

#: For an update: the installer of the version being installed, so a fix in it applies to this
#: update and not only to the next one. Taken from that version's git tree in the checkout the
#: installer keeps - the very code about to be installed, so it needs no trust beyond that code.
#: Without network, the running version's installer does the job.
fetch_target_installer() {
  local ref="$1" commit=""
  sudo git -C "$SRC" fetch --quiet --tags --force origin 2>/dev/null || return 1
  [ -n "$ref" ] || ref="$(newest_tag)"
  [ -n "$ref" ] || return 1
  if sudo git -C "$SRC" rev-parse --verify --quiet "refs/remotes/origin/$ref^{commit}" >/dev/null; then
    commit="$(sudo git -C "$SRC" rev-parse "refs/remotes/origin/$ref^{commit}")"
  else
    commit="$(sudo git -C "$SRC" rev-parse --verify --quiet "refs/tags/$ref^{commit}" || true)"
  fi
  [ -n "$commit" ] || return 1
  target_ref="$ref"
  target_commit="$commit"
  installer_copy="$(mktemp)"
  # Root reads the checkout; the copy belongs to the caller (pipefail keeps git's exit status).
  sudo git -C "$SRC" show "$commit:scripts/install-linux.sh" 2>/dev/null | tee "$installer_copy" >/dev/null
}

#: Exit status 0 when the release about to be installed may run: its manifest is signed by a key
#: the RUNNING version trusts, names the commit its tag points to, and is not older than what this
#: machine already accepted. The check is the running version's own code - never the new one's,
#: which is what is being checked. Branches and commits have no manifest; the installer notes it.
verify_target() {
  local ref="$1" commit="$2" allow="$3" status=0 floor file
  [[ "$ref" =~ ^v[0-9] ]] || return 0
  verify_dir="$(mktemp -d)"
  NOX_HOME="$verify_dir" "$PROGRAM" release keys >/dev/null 2>&1 || status=$?
  case "$status" in
    0) ;;
    3) msgln sig_not_configured; return 0 ;;
    *) msgln sig_broken "$PROGRAM" >&2; return 1 ;;
  esac
  for file in release-manifest.json release-manifest.json.sig; do
    curl --proto '=https' --tlsv1.2 -fsSL -o "$verify_dir/$file" \
      "$REPO_URL/releases/download/$ref/$file" 2>/dev/null || { msgln sig_missing "$ref" >&2; return 1; }
  done
  floor="$(sudo cat /opt/nox/state/release-floor 2>/dev/null || true)"
  local args=(--expect-tag "$ref" --expect-commit "$commit")
  if [ -n "$floor" ]; then args+=(--min-version "$floor"); fi
  if [ "$allow" = 1 ]; then args+=(--allow-downgrade); fi
  if ! NOX_HOME="$verify_dir" "$PROGRAM" release verify "$verify_dir/release-manifest.json" "${args[@]}"; then
    msgln sig_refused "$ref" >&2
    return 1
  fi
}

#: One line in `nox doctor` when a newer release exists. Asked only when the owner runs it; GitHub
#: sees the same request as for any `git fetch`, nothing about this installation.
update_notice() {
  local installed newest
  installed="$(installed_ref)"
  if [[ ! "$installed" =~ ^v[0-9]+[.][0-9]+[.][0-9]+$ ]]; then
    # A commit from a local checkout (how CI installs) has no newer version to point at.
    [ -n "$installed" ] && [[ ! "$installed" =~ ^[0-9a-f]{40}$ ]] && printf '\n%s\n' "$(msg updates_follow "$installed")"
    return 0
  fi
  newest="$(timeout 10 git ls-remote --tags --refs "$REPO_URL" 'v*' 2>/dev/null | sed 's#.*refs/tags/##' \
    | grep -E '^v[0-9]+[.][0-9]+[.][0-9]+$' | sort -V | tail -n1 || true)"
  if [ -z "$newest" ]; then
    printf '\n%s\n' "$(msg updates_unchecked)"
  elif [ "$newest" != "$installed" ] && [ "$(printf '%s\n%s\n' "$installed" "$newest" | sort -V | tail -n1)" = "$newest" ]; then
    printf '\n%s\n' "$(msg updates_available "$newest" "$installed")"
  else
    printf '\n%s\n' "$(msg updates_current "$installed")"
  fi
}

case "${1:-}" in
  update)
    target=""
    allow_downgrade=0
    for argument in "${@:2}"; do
      case "$argument" in
        --allow-downgrade) allow_downgrade=1 ;;
        -*) msgln unknown_option "$argument" >&2; exit 2 ;;
        *) target="$argument" ;;
      esac
    done
    if [ -n "$target" ] && [[ ! "$target" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]]; then
      msgln invalid_version "$target" >&2; exit 2
    fi
    # An installation from a branch stays on it: a plain `nox update` must not quietly go back to
    # the last release, which is older than what runs.
    installed="$(installed_ref)"
    if [ -z "$target" ] && [ -n "$installed" ] && [[ ! "$installed" =~ ^v[0-9] ]] \
      && [[ ! "$installed" =~ ^[0-9a-f]{40}$ ]]; then
      target="$installed"
      msgln follows_branch "$target"
      msgln newest_release_hint "$(newest_tag)"
    fi
    version=(NOX_ACTION=install "NOX_LANG=$UI_LANG" "NOX_ALLOW_DOWNGRADE=$allow_downgrade")
    if [ -n "$target" ]; then version+=("NOX_REF=$target"); fi
    if fetch_target_installer "$target"; then
      verify_target "$target_ref" "$target_commit" "$allow_downgrade" || exit 1
      sudo env "${version[@]}" bash "$installer_copy"
    else
      run_installer "${version[@]}"
    fi
    exit $?
    ;;
  doctor)
    status=0
    (as_service "$PROGRAM" "$@") || status=$?
    update_notice
    exit "$status"
    ;;
  rollback)
    run_installer NOX_ACTION=rollback
    exit $?
    ;;
  restore)
    [ -n "${2:-}" ] || { msgln restore_which >&2; exit 2; }
    # A name is looked up among the snapshots. The folder is root-only, so the check - only a
    # snapshot Nox made, in that folder - is the installer's, which runs as root.
    case "$2" in
      /*) file="$2" ;;
      *) file="$BACKUP_DIR/$2" ;;
    esac
    run_installer NOX_ACTION=restore "NOX_SNAPSHOT=$file"
    exit $?
    ;;
  snapshots)
    listing="$(sudo find "$BACKUP_DIR" -maxdepth 1 -name 'nox-*.tar.gz' \
      -printf '%TY-%Tm-%Td %TH:%TM  %8s  %f\n' 2>/dev/null | sort -r || true)"
    if [ -n "$listing" ]; then
      printf '%s\n' "$listing"
      msgln snapshots_hint
    else
      msgln snapshots_none
    fi
    exit 0
    ;;
  onboard)
    (as_service "$PROGRAM" "$@") || exit $?
    msgln onboard_restart
    sudo systemctl restart nox
    exit 0
    ;;
  claude-login)
    claude="$NOX_HOME_DIR/.local/bin/claude"
    if ! sudo -u "$SERVICE_USER" test -x "$claude"; then
      msgln claude_install
      sudo -u "$SERVICE_USER" -- env HOME="$NOX_HOME_DIR" bash -c \
        'cd "$HOME" && curl --proto "=https" --tlsv1.2 -fsSL https://claude.ai/install.sh | bash'
    fi
    if [ "$UI_LANG" = de ]; then
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
    else
      cat <<'HOW'

  Claude Code starts in a moment. To sign in:
    0. If Claude first asks for a colour scheme or whether to trust the folder:
       just press Enter.
    1. Type "/login" and press Enter.
    2. Open the link it shows on your phone or PC and sign in.
    3. Paste the code that page shows here.
    4. Then quit with "/exit". Nox uses the sign-in from its next restart on:
       sudo systemctl restart nox

HOW
    fi
    as_service "$claude"
    ;;
esac

as_service "$PROGRAM" "$@"
