"""The things Nox cannot do, written down.

Every assistant has this list. Most keep it only in the heads of the people who built it, which is
why "can you do X?" so often gets a confident wrong answer. Here it is data, it is served next to
the capabilities, and the model answers from it instead of guessing.

Two kinds of entry live here:

* **Curated** - the table below. It has to be maintained by hand, and the rule for maintaining it
  is simple: when a capability ships, its row moves out of here and into the registry. A row left
  behind after the tool exists is worse than no row at all, so the tests check that no entry names
  a tool that is actually registered.
* **Derived** - produced from the running system: a plugin present but not enabled, a tool whose
  credential is missing. Those cannot be curated because they depend on this installation.
"""

from __future__ import annotations

from collections.abc import Iterable

from nox.capabilities.model import Gap, GapReason

__all__ = ["CURATED_GAPS", "derived_gaps"]


#: What a user may reasonably ask for and not get. Ordered roughly by how often it comes up.
CURATED_GAPS: tuple[Gap, ...] = (
    # ---- the desktop, absent until the PC tools ship ------------------------------------------
    # Gap names carry the name the tool would have, so `stale_gaps()` notices when one ships.
    # Files are no longer here: `file.list/read/write/move/delete` exist, bounded by
    # `files.roots`. A row left behind after a capability ships is worse than no row, which is
    # what `stale_gaps()` is for.
    Gap(
        name="desktop.process_start",
        what="start any program",
        reason=GapReason.MISSING,
        detail=(
            "only programs the user registered as preset actions can be started, by id. "
            "Processes can be listed and ended - `desktop.processes`, `desktop.process_stop`"
        ),
    ),
    Gap(
        name="desktop.input_send",
        what="press a key combination, or click and move the mouse",
        reason=GapReason.MISSING,
        detail=(
            "typing text into an ordinary window exists - `desktop.type_text` - and it types "
            "characters only. Shortcuts like Ctrl+S and every kind of mouse input do not exist"
        ),
    ),
    Gap(
        name="browser.control",
        what="open and drive a web page",
        reason=GapReason.MISSING,
        detail="no browser automation of any kind",
    ),
    Gap(
        name="desktop.screen_capture",
        what="show what is on the screen right now",
        reason=GapReason.MISSING,
        detail="screenshots exist only for a creative application's own window, consent-gated",
    ),
    Gap(
        name="desktop.audio",
        what="change the volume or switch the output device",
        reason=GapReason.MISSING,
        detail="audio is output only, through the voice worker",
    ),
    Gap(
        name="desktop.devices",
        what="see which devices are attached",
        reason=GapReason.MISSING,
        detail="no device enumeration; a new microphone cannot be noticed, let alone set up",
    ),
    Gap(
        name="desktop.settings",
        what="change a Windows setting, service or driver",
        reason=GapReason.MISSING,
        detail="`config.set` changes Nox's own settings and nothing else on the machine",
    ),
    Gap(
        name="desktop.software_install",
        what="install or remove a program",
        reason=GapReason.MISSING,
        detail="nothing downloads or installs software",
    ),
    Gap(
        name="security.scan",
        what="scan for malware and remove it",
        reason=GapReason.MISSING,
        detail=(
            "no scanner integration. What is possible later is asking Windows Defender and "
            "reporting its answer - deciding for itself what is malware is not"
        ),
    ),
    # ---- absent on purpose --------------------------------------------------------------------
    Gap(
        name="home.lock",
        what="unlock a door, disarm an alarm, open a garage or a valve",
        reason=GapReason.FORBIDDEN,
        detail=(
            "a boundary, not a risk level: these entities are refused before a service call is "
            "built and never appear in the device list"
        ),
    ),
    Gap(
        name="game.input",
        what="play or assist inside a running game",
        reason=GapReason.FORBIDDEN,
        detail=(
            "Rocket League is observation only - no input, no process memory, no injection. The "
            "one file that can type refuses while any watched game runs, and CI checks both that "
            "exemption and its guard"
        ),
    ),
    # ---- on the roadmap, and honest about it --------------------------------------------------
    Gap(
        name="self.extend",
        what="write and activate a new capability for itself",
        reason=GapReason.MISSING,
        detail="the parts exist (coding sessions, plugin manifests, tests) and are not connected",
    ),
    Gap(
        name="view.render",
        what="draw a chart, a table or a live view of its own",
        reason=GapReason.MISSING,
        detail="the dashboard has fixed pages; nothing lets the core produce a view",
    ),
)


def derived_gaps(
    *,
    installed_plugins: Iterable[str],
    enabled_plugins: Iterable[str],
    missing_prerequisites: Iterable[tuple[str, str]] = (),
) -> list[Gap]:
    """The gaps this installation has that another would not.

    `missing_prerequisites` is pairs of (capability name, what is missing). "The tool is there and
    does nothing" is the most confusing state a user can meet, and it deserves a named entry
    rather than an empty device list and a shrug.
    """
    enabled = set(enabled_plugins)
    gaps = [
        Gap(
            name=f"{plugin}.*",
            what=f"everything the {plugin!r} plugin provides",
            reason=GapReason.DISABLED,
            detail=f"installed but not in `plugins.enabled`; add {plugin!r} there to use it",
        )
        for plugin in sorted(set(installed_plugins) - enabled)
    ]
    gaps += [
        Gap(
            name=name,
            what="exists, but needs something you have to supply",
            reason=GapReason.PREREQUISITE,
            detail=detail,
        )
        for name, detail in missing_prerequisites
    ]
    return gaps
