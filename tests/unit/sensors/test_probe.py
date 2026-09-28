"""Probe selection per platform, and the X11/macOS probes' parsing - including every way a reading
becomes unobservable instead of an empty (and therefore "safe-looking") title."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from nox.sensors.posix import (
    MAC_PERMISSION,
    MAC_TITLE_UNREADABLE,
    X11_FAILED,
    MacProbe,
    X11Probe,
)
from nox.sensors.probe import (
    NO_DISPLAY_REASON,
    WAYLAND_REASON,
    ForegroundInfo,
    ProbeUnavailableError,
    UnobservableProbe,
    select_probe,
)


def _which(*present: str):
    return lambda tool: f"/usr/bin/{tool}" if tool in present else None


class ScriptedRunner:
    """Answers each command by its first two words; unknown commands fail like a missing tool."""

    def __init__(self, answers: dict[tuple[str, ...], str | None]) -> None:
        self._answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> str | None:
        self.calls.append(list(command))
        return self._answers.get(tuple(command[:2]))


# ---- selection ----------------------------------------------------------------------------------


def test_wayland_session_is_unobservable_with_a_reason_the_user_can_act_on() -> None:
    selection = select_probe(
        "linux", {"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"}, _which("xprop", "xprintidle")
    )
    assert isinstance(selection.probe, UnobservableProbe)
    assert selection.probe.foreground().limitation == WAYLAND_REASON
    assert "X11" in WAYLAND_REASON
    assert selection.idle_limitation


def test_wayland_display_without_x_display_is_unobservable() -> None:
    selection = select_probe("linux", {"WAYLAND_DISPLAY": "wayland-0"}, _which("xprop"))
    assert isinstance(selection.probe, UnobservableProbe)


def test_headless_linux_is_unobservable() -> None:
    selection = select_probe("linux", {}, _which("xprop", "xprintidle"))
    assert isinstance(selection.probe, UnobservableProbe)
    assert selection.probe.foreground().limitation == NO_DISPLAY_REASON


def test_x11_without_xprop_is_unobservable_and_names_the_package() -> None:
    selection = select_probe("linux", {"DISPLAY": ":0"}, _which())
    assert isinstance(selection.probe, UnobservableProbe)
    assert "x11-utils" in selection.probe.foreground().limitation


def test_x11_with_tools_gets_the_x11_probe() -> None:
    selection = select_probe(
        "linux", {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"}, _which("xprop", "xprintidle")
    )
    assert isinstance(selection.probe, X11Probe)
    assert selection.idle_limitation == ""


def test_x11_without_xprintidle_reports_idle_as_unsupported() -> None:
    selection = select_probe("linux", {"DISPLAY": ":0"}, _which("xprop"))
    assert isinstance(selection.probe, X11Probe)
    assert "xprintidle" in selection.idle_limitation


def test_macos_gets_the_system_events_probe() -> None:
    selection = select_probe("darwin", {}, _which("osascript", "ioreg"))
    assert isinstance(selection.probe, MacProbe)
    assert selection.idle_limitation == ""


def test_unknown_platform_is_unobservable_never_an_error() -> None:
    selection = select_probe("plan9", {}, _which())
    assert isinstance(selection.probe, UnobservableProbe)
    assert selection.idle_limitation


def test_unobservable_probe_never_invents_an_idle_time() -> None:
    with pytest.raises(ProbeUnavailableError):
        UnobservableProbe("no display").idle_seconds()


# ---- X11 ----------------------------------------------------------------------------------------

ACTIVE = "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x3a00007\n"


def _x11(props: str | None, root: str | None = ACTIVE) -> X11Probe:
    return X11Probe(ScriptedRunner({("xprop", "-root"): root, ("xprop", "-id"): props}))


def test_x11_reads_title_and_falls_back_to_wm_class_for_the_process() -> None:
    props = (
        '_NET_WM_NAME(UTF8_STRING) = "Sparkasse - Online-Banking"\n'
        "_NET_WM_PID:  not found.\n"
        'WM_CLASS(STRING) = "Navigator", "firefox"\n'
    )
    info = _x11(props).foreground()
    assert info == ForegroundInfo("Sparkasse - Online-Banking", "firefox", 0)


def test_x11_unescapes_quotes_in_titles() -> None:
    props = '_NET_WM_NAME(UTF8_STRING) = "say \\"hi\\" - Editor"\n'
    assert _x11(props).foreground().title == 'say "hi" - Editor'


def test_x11_prefers_net_wm_name_over_legacy_wm_name() -> None:
    props = 'WM_NAME(STRING) = "legacy"\n_NET_WM_NAME(UTF8_STRING) = "modern"\n'
    assert _x11(props).foreground().title == "modern"


def test_x11_no_focused_window_is_an_observed_empty_desktop() -> None:
    info = _x11(None, root="_NET_ACTIVE_WINDOW(WINDOW): window id # 0x0\n").foreground()
    assert info == ForegroundInfo("", "", 0)
    assert info.limitation == ""


@pytest.mark.parametrize(
    ("root", "props"),
    [
        (None, "irrelevant"),  # xprop -root failed
        ("garbage", "irrelevant"),  # unexpected output
        (ACTIVE, None),  # the window's properties could not be read
    ],
)
def test_x11_failures_are_unobservable_not_empty(root: str | None, props: str | None) -> None:
    info = _x11(props, root=root).foreground()
    assert info.limitation == X11_FAILED
    assert info.title == ""


def test_x11_idle_is_read_in_milliseconds() -> None:
    probe = X11Probe(ScriptedRunner({("xprintidle",): "12500\n"}))
    assert probe.idle_seconds() == 12.5


def test_x11_idle_garbage_raises_instead_of_guessing() -> None:
    probe = X11Probe(ScriptedRunner({("xprintidle",): "n/a\n"}))
    with pytest.raises(ProbeUnavailableError):
        probe.idle_seconds()


# ---- macOS --------------------------------------------------------------------------------------

SEP = "\x1f"


def _mac(answer: str | None) -> MacProbe:
    return MacProbe(ScriptedRunner({("osascript", "-e"): answer}))


def test_macos_reads_app_pid_and_title() -> None:
    info = _mac(f"Safari{SEP}412{SEP}ok{SEP}PayPal - Log in\n").foreground()
    assert info == ForegroundInfo("PayPal - Log in", "Safari", 412)


def test_macos_title_containing_the_separator_is_kept_whole() -> None:
    info = _mac(f"Notes{SEP}7{SEP}ok{SEP}a{SEP}b\n").foreground()
    assert info.title == f"a{SEP}b"


def test_macos_without_accessibility_permission_is_unobservable() -> None:
    info = _mac(None).foreground()
    assert info.limitation == MAC_PERMISSION
    assert "Accessibility" in MAC_PERMISSION


def test_macos_unreadable_title_is_unobservable_but_keeps_the_app() -> None:
    info = _mac(f"Finder{SEP}99{SEP}unreadable{SEP}\n").foreground()
    assert info.limitation == MAC_TITLE_UNREADABLE
    assert info.process_name == "Finder"


def test_macos_malformed_answer_is_unobservable() -> None:
    assert _mac("something else\n").foreground().limitation == MAC_PERMISSION


def test_macos_idle_is_read_from_hid_idle_time_in_nanoseconds() -> None:
    ioreg = '    |   "HIDIdleTime" = 3000000000\n'
    probe = MacProbe(ScriptedRunner({("ioreg", "-c"): ioreg}))
    assert probe.idle_seconds() == 3.0


def test_macos_idle_without_hid_idle_time_raises() -> None:
    probe = MacProbe(ScriptedRunner({("ioreg", "-c"): "nothing here"}))
    with pytest.raises(ProbeUnavailableError):
        probe.idle_seconds()
