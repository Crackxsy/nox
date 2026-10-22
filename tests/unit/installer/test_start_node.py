"""`installer/start_node.pyw`: which copy of Nox a node starts, the check of a self-installed
version against its signed release, the rollback of a new version that crashes or is refused by
its core, and the Windows logon task (as the user, never elevated). Nothing here starts a node,
registers a task or touches an installed copy: children are stubs, folders live in `tmp_path`."""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType
from xml.etree import ElementTree

import pytest

from nox.node.home import default_home
from nox.node.update import RESTART_EXIT_CODE, TRIAL_FAILED_EXIT_CODE, UpdateState

REPO = Path(__file__).resolve().parents[3]
TASK_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def _load() -> ModuleType:
    loader = SourceFileLoader("nox_start_node", str(REPO / "installer" / "start_node.pyw"))
    spec = importlib.util.spec_from_loader("nox_start_node", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


launcher = _load()


def copy_of(folder: Path, version: str) -> Path:
    (folder / "nox").mkdir(parents=True)
    (folder / "nox" / "__init__.py").write_text(f'__version__ = "{version}"\n')
    return folder


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return copy_of(tmp_path / "base", "0.5.0")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "node" / "app"


@pytest.fixture
def signed(monkeypatch: pytest.MonkeyPatch) -> Iterator[set[str]]:
    """Release folders count as signed when their name is in the set (the real check has its
    own tests in tests/unit/node/test_update.py and below)."""
    names: set[str] = set()
    monkeypatch.setattr(launcher, "verified", lambda folder: folder.name in names)
    yield names


def release(root: Path, version: str, signed: set[str]) -> Path:
    signed.add(version)
    return copy_of(root / "releases" / version, version)


def state(root: Path) -> UpdateState:
    return UpdateState.load(root / "state.json")


def test_the_launcher_finds_the_same_node_folder_as_nox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOX_NODE_HOME", raising=False)
    assert launcher.node_home() == default_home()
    monkeypatch.setenv("NOX_NODE_HOME", str(tmp_path / "elsewhere"))
    assert launcher.node_home() == default_home() == tmp_path / "elsewhere"


def test_without_updates_the_installed_copy_runs(base: Path, root: Path) -> None:
    assert launcher.choose(base, root) == (base, None)


def test_a_signed_pending_version_is_tried(base: Path, root: Path, signed: set[str]) -> None:
    folder = release(root, "0.5.1", signed)
    UpdateState(pending="0.5.1").save(root / "state.json")
    assert launcher.choose(base, root) == (folder, "0.5.1")
    assert launcher.choose(base, root, trial=False) == (base, None)  # a one-off command


def test_an_unsigned_or_changed_release_never_runs(base: Path, root: Path) -> None:
    copy_of(root / "releases" / "0.5.1", "0.5.1")  # with the real check: no signed manifest
    UpdateState(pending="0.5.1", current="0.5.1").save(root / "state.json")
    assert launcher.choose(base, root) == (base, None)
    assert (state(root).pending, state(root).current) == (None, None)


@pytest.mark.parametrize("version", ["0.5.0", "0.4.9"])
def test_a_release_not_newer_than_the_base_is_dropped(
    base: Path, root: Path, signed: set[str], version: str
) -> None:
    release(root, version, signed)
    UpdateState(pending=version, current=version).save(root / "state.json")
    assert launcher.choose(base, root) == (base, None)
    assert (state(root).pending, state(root).current) == (None, None)


@pytest.mark.parametrize("name", ["..", ".", "../..", "0.5.1/../..", ""])
def test_a_forged_pending_name_touches_nothing(
    tmp_path: Path, base: Path, root: Path, name: str
) -> None:
    keep = tmp_path / "node" / "keep.txt"
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_text("x")
    root.mkdir(parents=True, exist_ok=True)
    (root / "state.json").write_text(f'{{"pending": "{name}", "current": "{name}"}}')
    assert launcher.choose(base, root) == (base, None)
    launcher.mark_bad(root, name, "test")
    assert keep.exists() and base.exists()


def _supervise(base: Path, root: Path, exits: list[int], started: list[Path]) -> int:
    codes = iter(exits)

    def child(app: Path) -> int:
        started.append(app)
        return next(codes)

    return int(launcher.supervise(base, root, run_child=child, sleep=lambda _s: None))


def test_two_real_crashes_roll_a_new_version_back(base: Path, root: Path, signed: set[str]) -> None:
    previous = release(root, "0.5.1", signed)
    new = release(root, "0.5.2", signed)
    UpdateState(current="0.5.1", pending="0.5.2").save(root / "state.json")
    started: list[Path] = []
    assert _supervise(base, root, [1, 1, 0], started) == 0
    assert started == [new, new, previous]
    after = state(root)
    assert (after.current, after.pending) == ("0.5.1", None) and "0.5.2" in after.bad
    assert not new.exists()


def test_one_crash_is_not_enough(base: Path, root: Path, signed: set[str]) -> None:
    new = release(root, "0.5.1", signed)
    UpdateState(pending="0.5.1").save(root / "state.json")
    started: list[Path] = []
    assert _supervise(base, root, [1, 0], started) == 0
    assert started == [new, new]
    assert (state(root).pending, state(root).failures) == ("0.5.1", 1)


@pytest.mark.parametrize("code", [-15, -2, -1, 130, 143, 0xC000013A, 0x40010004])
def test_an_end_from_outside_is_no_crash(
    base: Path, root: Path, signed: set[str], code: int
) -> None:
    release(root, "0.5.1", signed)
    UpdateState(pending="0.5.1").save(root / "state.json")
    assert _supervise(base, root, [code], []) == 0
    assert (state(root).pending, state(root).failures, state(root).bad) == ("0.5.1", 0, {})


def test_an_orderly_end_marked_by_the_node_is_no_crash(
    base: Path, root: Path, signed: set[str]
) -> None:
    release(root, "0.5.1", signed)
    UpdateState(pending="0.5.1").save(root / "state.json")

    def child(_app: Path) -> int:
        (root / launcher.CLEAN_EXIT).write_text("stopped")
        return 3

    assert launcher.supervise(base, root, run_child=child, sleep=lambda _s: None) == 0
    assert state(root).failures == 0


def test_a_core_refusing_the_new_protocol_rolls_back_at_once(
    base: Path, root: Path, signed: set[str]
) -> None:
    release(root, "0.5.1", signed)
    UpdateState(pending="0.5.1").save(root / "state.json")
    started: list[Path] = []
    assert _supervise(base, root, [TRIAL_FAILED_EXIT_CODE, 0], started) == 0
    assert started[1] == base
    assert state(root).pending is None and "0.5.1" in state(root).bad


def test_a_confirmed_new_version_is_kept_after_a_crash(
    base: Path, root: Path, signed: set[str]
) -> None:
    release(root, "0.5.1", signed)
    UpdateState(pending="0.5.1").save(root / "state.json")
    exits = iter([1, 0])

    def child(_app: Path) -> int:
        if state(root).pending:  # the node confirmed itself, then crashed later
            UpdateState(current="0.5.1").save(root / "state.json")
        return next(exits)

    assert launcher.supervise(base, root, run_child=child, sleep=lambda _s: None) == 0
    assert state(root).current == "0.5.1" and state(root).bad == {}


def test_the_restart_code_starts_the_new_version(base: Path, root: Path, signed: set[str]) -> None:
    new = release(root, "0.5.1", signed)
    started: list[Path] = []

    def child(app: Path) -> int:
        started.append(app)
        if len(started) == 1:
            UpdateState(pending="0.5.1").save(root / "state.json")
            return RESTART_EXIT_CODE
        return 0

    launcher.supervise(base, root, run_child=child, sleep=lambda _s: None)
    assert started == [base, new]


def test_a_copy_that_keeps_crashing_is_given_up(base: Path, root: Path) -> None:
    pauses: list[float] = []
    code = launcher.supervise(
        base, root, run_child=lambda _app: 1, sleep=pauses.append, clock=lambda: 100.0
    )
    assert code == 1
    assert len(pauses) == launcher.MAX_CRASHES_PER_HOUR - 1
    assert max(pauses) == 60.0


def test_a_new_base_drops_self_installed_versions_but_keeps_the_record(
    tmp_path: Path, root: Path, signed: set[str]
) -> None:
    release(root, "0.5.1", signed)
    (root / "releases" / "0.5.2.staging").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    try:
        (root / "releases" / "0.5.9").symlink_to(outside, target_is_directory=True)
    except OSError:
        pass  # no symlinks for this account (Windows without developer mode)
    UpdateState(current="0.5.1", floor="0.5.1", bad={"0.5.3": "2026-10-20T00:00:00+00:00"}).save(
        root / "state.json"
    )
    launcher.reset_overlays(root)
    after = state(root)
    assert (after.current, after.pending, after.floor) == (None, None, "0.5.1")
    assert "0.5.3" in after.bad
    assert not any((root / "releases").iterdir())
    assert (outside / "keep").exists()


def test_the_real_check_runs_a_signed_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installed by the updater, checked by the launcher with the (test) trusted key."""
    from nox.release import trusted_keys
    from tests.unit.node.test_update import Key, updater
    from tests.unit.node.test_update import release as signed_release

    key = Key()
    monkeypatch.setattr(trusted_keys, "TRUSTED_KEYS", (key.trusted,))
    up = updater(tmp_path, signed_release(key, "0.5.1"), [key])
    assert up.apply(up.check()).status == "installed"
    root = tmp_path / "app"
    base = copy_of(tmp_path / "base", "0.5.0")
    folder = root / "releases" / "0.5.1"
    assert launcher.verified(folder)
    assert launcher.choose(base, root) == (folder, "0.5.1")
    (folder / "nox" / "__init__.py").write_text('__version__ = "0.5.1"\nimport os  # planted\n')
    assert not launcher.verified(folder)
    assert launcher.choose(base, root) == (base, None)


def test_the_logon_task_runs_as_the_user_with_least_privilege() -> None:
    xml = launcher.task_xml(
        user="SPRINGFIELD\\alex",
        command="C:\\Users\\alex\\AppData\\Local\\Programs\\Nox\\runtime\\pythonw.exe",
        arguments='-I "C:\\Users\\alex\\AppData\\Local\\Programs\\Nox\\app\\start_node.pyw"',
        workdir="C:\\Users\\alex\\AppData\\Local\\Programs\\Nox\\app",
    )
    task = ElementTree.fromstring(xml.split("\n", 1)[1])
    trigger = task.find("t:Triggers/t:LogonTrigger", TASK_NS)
    assert trigger is not None
    assert trigger.findtext("t:UserId", namespaces=TASK_NS) == "SPRINGFIELD\\alex"
    principal = task.find("t:Principals/t:Principal", TASK_NS)
    assert principal is not None
    assert principal.findtext("t:RunLevel", namespaces=TASK_NS) == "LeastPrivilege"
    assert principal.findtext("t:LogonType", namespaces=TASK_NS) == "InteractiveToken"
    assert principal.findtext("t:UserId", namespaces=TASK_NS) == "SPRINGFIELD\\alex"
    assert "HighestAvailable" not in xml and "S-1-5-18" not in xml and "SYSTEM" not in xml


def test_task_values_are_escaped() -> None:
    xml = launcher.task_xml(user="a&b", command="<c>", arguments='"x"', workdir="w")
    ElementTree.fromstring(xml.split("\n", 1)[1])  # still well-formed
    assert "a&amp;b" in xml and "&lt;c&gt;" in xml


def test_the_node_process_starts_isolated_in_its_own_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONPATH", "somewhere-else")
    argv, env, cwd = launcher.child_command(tmp_path)
    assert argv[1:3] == ["-I", "-B"] and Path(argv[3]).is_absolute()
    assert cwd == str(tmp_path)
    assert env["NOX_NODE_APP"] == str(tmp_path)
    assert not any(k.upper().startswith("PYTHON") for k in env)


@pytest.mark.parametrize("code", [-9, -6, -11, 1])
def test_a_kill_an_abort_a_segfault_or_an_error_is_a_crash(
    base: Path, root: Path, signed: set[str], code: int
) -> None:
    """SIGKILL (the OOM killer), SIGABRT, SIGSEGV, exit 1: a pending version that ends that way
    twice is rolled back; and the launcher never reports such an end as an orderly one."""
    new = release(root, "0.5.1", signed)
    UpdateState(pending="0.5.1").save(root / "state.json")
    started: list[Path] = []
    assert _supervise(base, root, [code, code, 0], started) == 0
    assert started == [new, new, base]
    assert state(root).pending is None and "0.5.1" in state(root).bad


@pytest.mark.parametrize("code", [-9, -11])
def test_a_base_that_keeps_getting_killed_ends_the_launcher_with_an_error(
    base: Path, root: Path, code: int
) -> None:
    """Exit non-zero, so systemd's Restart=on-failure starts it again (with its own pause)."""
    assert (
        launcher.supervise(
            base, root, run_child=lambda _app: code, sleep=lambda _s: None, clock=lambda: 1.0
        )
        == 1
    )


def test_sigterm_is_an_orderly_end_and_the_launcher_exits_zero(base: Path, root: Path) -> None:
    assert launcher.supervise(base, root, run_child=lambda _app: -15, sleep=lambda _s: None) == 0


def test_a_change_while_checking_is_applied_at_the_next_start(
    base: Path, root: Path, signed: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Folders are checked without the lock; if the state changed meanwhile, the base runs now
    and the new state is judged at the next start."""
    release(root, "0.5.1", signed)
    release(root, "0.5.2", signed)
    UpdateState(pending="0.5.1").save(root / "state.json")
    real = launcher.verified

    def racing(folder: Path) -> bool:
        UpdateState(pending="0.5.2").save(root / "state.json")  # an update was just installed
        return bool(real(folder))

    monkeypatch.setattr(launcher, "verified", racing)
    assert launcher.choose(base, root) == (base, None)
    assert state(root).pending == "0.5.2"
