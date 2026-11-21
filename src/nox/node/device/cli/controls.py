"""`nox-node status | log | stop | resume`: what the node says about itself, and stopping and
resuming it on this device."""

from __future__ import annotations

import json

from nox.node.device.cli.runtime import open_audit, refuse_during_session
from nox.node.device.cli_text import (
    command,
    group_label,
    supported_here,
    t,
)
from nox.node.device.home import NodeHome
from nox.node.device.world.notices import session_marker_state
from nox.node.wire.manifest import GROUPS, load_manifest
from nox.node.wire.protocol import escape_controls


def shown_label(group: str, lang: str | None = None) -> str:
    """A group's label, marked when it can do nothing on this device (`supported_here`)."""
    label = group_label(group, lang)
    return label if supported_here(group) else f"{label} ({t('status_not_here', lang)})"


def status(home: NodeHome) -> list[str]:
    identity = home.load_identity()
    if identity is None:
        last = home.read_status().get("state")
        return [t("status_removed") if last == "removed" else t("status_unpaired")]
    manifest = load_manifest(home.manifest_file)
    state = home.read_status()
    allowed = [shown_label(g) for g in GROUPS if manifest.groups.get(g)]
    x = escape_controls
    running = x(str(state.get("state", t("status_not_running"))))
    lines = [
        t("status_name", value=x(identity.name)),
        t("status_core", value=f"{x(identity.core_host)}:{identity.node_port}"),
        t("status_state", value=f"{running} ({x(str(state.get('at', '-')))})"),
        t("status_may", value=", ".join(allowed) or t("status_nothing")),
        t("status_risk", value=manifest.max_risk),
        t("status_folders", value=", ".join(x(f) for f in manifest.folders) or t("status_none")),
    ]
    if session_marker_state(home.path) is not None:
        lines.append(t("status_session"))
    try:
        update_info = json.loads((home.path / "update.json").read_text(encoding="utf-8"))
        lines.append(t("status_update", value=x(str(update_info.get("message", "-")))[:160]))
        stale = update_info.get("stale_days")
        if isinstance(stale, int):
            lines.append(t("update_stale", days=stale))
    except (OSError, ValueError, AttributeError):
        pass
    return lines


def log_lines(home: NodeHome, *, limit: int = 50) -> list[str]:
    audit = open_audit(home)
    entries = audit.entries(since_seq=max(0, audit.count() - limit), limit=limit)
    # Every field is escaped on the way out too: an entry written before this check existed,
    # or by anything else, still cannot drive the terminal or fake a line.
    x = escape_controls
    lines = [
        f"{e.seq:>6}  {x(e.decision):<5}  {x(e.actor):<6} {x(e.tool):<14} {x(e.action):<12} "
        f"{x(e.target)} {x(e.result)}".rstrip()
        for e in entries
    ]
    lines.append(t("log_intact") if audit.verify_chain() else t("log_broken"))
    return lines


def stop(home: NodeHome) -> str:
    """`nox-node stop`: stop Nox here at once - and everywhere (the core gets a stop too)."""
    home.ensure()
    home.stop_file.write_text("stopped on this device\n", encoding="utf-8")
    return t("stop_done", resume=command("resume"))


def resume(home: NodeHome) -> str:
    """`nox-node resume`: undo a stop made on this device - never while Nox controls it."""
    refuse_during_session(home)
    home.stop_file.unlink(missing_ok=True)
    return t("resume_done")
