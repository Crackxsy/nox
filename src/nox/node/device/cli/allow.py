"""`nox-node allow` and `deny` (`allow` with `enabled` off): the one way to widen what Nox may do
on this device, and the list of watched games (`games`)."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

from nox.node.device.cli.runtime import NodeCliError, open_audit, refuse_during_session
from nox.node.device.cli_text import (
    command,
    group_label,
    language,
    supported_here,
    t,
    unsupported_reason,
)
from nox.node.device.confirmed import (
    MOST_RESTRICTIVE,
    last_confirmed,
    manifest_key,
    record_confirmed,
)
from nox.node.device.home import NodeHome
from nox.node.device.safe_fs.folders import broad_folder
from nox.node.device.safe_fs.secret_places import secret_locations
from nox.node.device.safe_fs.workspace import marked_above
from nox.node.wire.manifest import GAME_NAME, GROUPS, NodeManifest, load_manifest, save_manifest
from nox.node.wire.paths import is_remote_or_device_path
from nox.node.wire.protocol import escape_controls
from nox.security.model import SecretStore


def _local_folder(text: str, node_home: NodeHome, *, must_exist: bool = True) -> str:
    """A folder typed in a terminal here: absolute, local, existing, resolved once, now - and
    never the node's own folder or one inside it."""

    raw = text.strip()
    if not raw or is_remote_or_device_path(raw) or not Path(raw).is_absolute():
        raise NodeCliError(t("folder_absolute"))
    real = Path(os.path.realpath(raw))
    if must_exist and not real.is_dir():
        raise NodeCliError(t("folder_missing", path=escape_controls(str(real))))
    home = Path(os.path.realpath(node_home.path))
    if real == home or home in real.parents:
        raise NodeCliError(t("folder_home"))
    if must_exist:
        if broad_folder(str(real)):
            raise NodeCliError(t("folder_broad"))
        for place in secret_locations():
            secret = Path(os.path.realpath(place))
            if real == secret or secret in real.parents or real in secret.parents:
                raise NodeCliError(t("folder_secret"))

        if marked_above(str(real)) is not None:
            raise NodeCliError(t("folder_marked"))
    return str(real)


def games(
    home: NodeHome,
    name: str,
    add: bool,
    *,
    say: Callable[[str], None],
    interactive: Callable[[], bool] = lambda: sys.stdin.isatty() and sys.stdout.isatty(),
    confirm: Callable[[str], bool] = lambda _prompt: False,
    secrets: SecretStore | None = None,
) -> None:
    """This device's own watched games (`node.yaml` `games`), on top of the core's list. Adding
    one only narrows what Nox may do and needs no question; removing one widens it, so it is
    asked in an interactive terminal like `allow`, and recorded as confirmed."""

    game = name.strip()
    if not GAME_NAME.match(game):
        raise NodeCliError(t("game_name"))
    if not add:
        refuse_during_session(home)
        if not interactive():
            raise NodeCliError(t("game_interactive"))
        if not confirm(t("game_ask", game=game)):
            raise NodeCliError(t("game_not_removed"))
    audit = open_audit(home)
    key = manifest_key(home.path, secrets)
    found = load_manifest(home.manifest_file)
    confirmed = last_confirmed(audit, key)
    if confirmed is None and not add:
        # Checked before anything is written: a removal that cannot be recorded changes nothing.
        raise NodeCliError(t("game_no_manifest"))

    def changed(manifest: NodeManifest) -> NodeManifest:
        rest = [g for g in manifest.games if g.lower() != game.lower()]
        return manifest.model_copy(update={"games": [*rest, game] if add else rest})

    if confirmed is not None and not record_confirmed(audit, changed(confirmed), key):
        # Recorded first, so that an edit of the file can never drop a game again by itself.
        if not add:
            raise NodeCliError(t("game_not_recorded"))
    save_manifest(home.manifest_file, changed(found))
    audit.append(
        actor="local",
        tool="node",
        action="node.games_add" if add else "node.games_remove",
        target=escape_controls(game)[:64],
        decision="allow",
        result="ok",
    )
    say(t("game_watching" if add else "game_unwatched", game=game))


#: Not a group, but confirmed the same way: on a device without a screen, let high risk run when
#: a person confirmed it on the core (see docs/SECURITY.md, "Accepted limits (nodes)").
TRUST_SETTING = "trust_core_for_high"


def computer_warning() -> str:
    """The second question before `computer` is enabled on a device, in plain words."""
    from nox.core.config.hands import ComputerUseConfig  # noqa: PLC0415

    return t("allow_computer", stop=ComputerUseConfig().stop_hotkey)


def computer_supported() -> bool:
    """Computer use on a node exists on Windows only."""
    return supported_here("computer")


def allow(
    home: NodeHome,
    group: str,
    enabled: bool,
    *,
    say: Callable[[str], None],
    interactive: Callable[[], bool] = lambda: sys.stdin.isatty() and sys.stdout.isatty(),
    confirm: Callable[[str], bool] = lambda _prompt: False,
    secrets: SecretStore | None = None,
    folder: str = "",
) -> None:
    """Change one group. Widening asks in an interactive terminal for a typed "yes", and the
    new manifest is the last confirmed one plus this change, so unconfirmed edits of `node.yaml`
    are never adopted. Widening refuses while Nox controls mouse and keyboard here, and for a
    group that can do nothing on this device (`supported_here`). `allow files --folder X`
    allows the folder and switches `files` on, in one question.

    The terminal check keeps the *core* and remote tools from widening a node - nothing they send
    reaches a terminal. It is NOT a boundary against a local process of the same user, which can
    open a terminal of its own; a real presence check (Windows Hello / UAC consent, polkit) is a
    later task (SECURITY.md, residual risk)."""
    trust = group == TRUST_SETTING
    if group not in GROUPS and not trust:
        known = ", ".join([*GROUPS, TRUST_SETTING])
        raise NodeCliError(t("allow_unknown", group=group, known=known))
    if folder and group != "files":
        raise NodeCliError(t("allow_folder_group"))
    lang = language()
    if enabled and not trust:
        why = unsupported_reason(group)
        if group == "computer":
            why = None if computer_supported() else "not_here"
        if why is not None:
            # Honest: a group that can do nothing here is not switched on at all.
            key_ = "allow_not_yet" if why == "not_yet" else "allow_not_here"
            raise NodeCliError(t(key_, lang, label=group_label(group, lang)))
    if enabled:
        refuse_during_session(home)
    if folder:
        folder = _local_folder(folder, home, must_exist=enabled)
    label = t("allow_trust", lang) if trust else group_label(group, lang)
    if folder:
        label = t("allow_files_folder", lang, folder=escape_controls(folder))
    audit = open_audit(home)
    if enabled:
        if not interactive():
            raise NodeCliError(t("allow_interactive", lang))
        if folder:
            prompt = t("allow_ask_folder", lang, folder=escape_controls(folder))
        else:
            prompt = t("allow_ask", lang, label=label)
        if not confirm(prompt):
            raise NodeCliError(t("allow_refused", lang))
        if group == "computer" and not confirm(computer_warning()):
            # Asked twice, the second time with what it means: mouse and keyboard are too much to
            # hand over on one reflexive "yes".
            raise NodeCliError(t("allow_refused", lang))
    key = manifest_key(home.path, secrets)
    manifest = last_confirmed(audit, key) or NodeManifest(groups=dict(MOST_RESTRICTIVE.groups))
    if trust:
        manifest.trust_core_for_high = enabled
    elif folder:
        # Allowing a folder switches `files` on with it (one question, not two); denying one keeps
        # the group as it is.
        rest = [f for f in manifest.folders if f != folder]
        manifest.folders = [*rest, folder] if enabled else rest
        if enabled:
            manifest.groups["files"] = True
    else:
        manifest.groups[group] = enabled
    save_manifest(home.manifest_file, manifest)
    if not record_confirmed(audit, manifest, key, create=enabled):
        if enabled:
            raise NodeCliError(t("no_confirmation_key", lang))
    audit.append(
        actor="local",
        tool="node",
        action="node.allow" if enabled else "node.deny",
        target=f"files:{folder}"[:200] if folder else group,
        decision="allow",
        result="ok",
    )
    say(t("allow_done" if enabled else "deny_done", lang, label=label))
    if enabled and group == "files" and not manifest.folders:
        say(t("allow_files_no_folder", lang, add=command('allow files --folder "<folder>"')))
