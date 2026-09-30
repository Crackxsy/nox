"""What Nox may never touch. A risk level is a question; this is where the question is not asked.

Getting this list wrong does not produce a wrong answer, it produces a machine that logs the user
out or a game that closes mid-match. So every group is asserted by name, including the one that is
easy to forget: Nox's own process, which holds the audit log and the kill switch.
"""

from __future__ import annotations

import os

import pytest

from nox.desktop.boundary import CRITICAL_PROCESSES, BoundaryError, check_process, is_critical


@pytest.mark.parametrize(
    "name", ["csrss.exe", "winlogon.exe", "lsass.exe", "services.exe", "System", "explorer.exe"]
)
def test_windows_itself_is_never_a_target(name: str) -> None:
    with pytest.raises(BoundaryError, match="Windows itself"):
        check_process(4321, name)


def test_the_case_of_the_name_does_not_matter() -> None:
    """Process names come from three different APIs; none of them promises a casing."""
    assert is_critical("CSRSS.EXE") and is_critical("  Csrss.Exe  ")


def test_an_ordinary_program_is_allowed() -> None:
    check_process(4321, "notepad.exe")  # no exception is the assertion


def test_nox_cannot_end_itself() -> None:
    """A tool that can kill the process holding the audit log is a kill switch with no record."""
    with pytest.raises(BoundaryError, match="Nox itself"):
        check_process(os.getpid(), "python.exe")


def test_nox_own_children_are_refused_by_pid() -> None:
    """The voice worker is where "close whatever is using my microphone" would otherwise land."""
    with pytest.raises(BoundaryError, match="Nox itself"):
        check_process(999_001, "python.exe", own_pids={999_001})


def test_a_packaged_nox_is_refused_by_name() -> None:
    with pytest.raises(BoundaryError, match="Nox itself"):
        check_process(4321, "nox-worker.exe")


def test_the_game_is_refused_even_though_it_is_an_ordinary_program() -> None:
    """Observation-only is not only about input: closing it mid-match breaks the same promise."""
    with pytest.raises(BoundaryError, match="observed only"):
        check_process(777, "RocketLeague.exe", game_pids={777})


def test_the_same_program_is_fine_when_it_is_not_the_watched_game() -> None:
    check_process(778, "RocketLeague.exe", game_pids={777})


def test_the_critical_list_covers_the_session_processes() -> None:
    """A regression fence: someone trimming this list should have to change a test that says why."""
    for required in ("csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe"):
        assert required in CRITICAL_PROCESSES
