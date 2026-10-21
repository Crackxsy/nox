"""`installer/start_node.pyw`: which copy of Nox a node starts, the rollback of a new version that
does not start, and the Windows logon task it registers (as the user, never elevated). Nothing
here starts a node, registers a task or touches an installed copy: children are stubs, folders
live in `tmp_path`."""

from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType
from xml.etree import ElementTree

import pytest

from nox.node.home import default_home
from nox.node.update import RESTART_EXIT_CODE, UpdateState

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


def test_the_launcher_finds_the_same_node_folder_as_nox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOX_NODE_HOME", raising=False)
    assert launcher.node_home() == default_home()
    monkeypatch.setenv("NOX_NODE_HOME", str(tmp_path / "elsewhere"))
    assert launcher.node_home() == default_home() == tmp_path / "elsewhere"


def test_without_updates_the_installed_copy_runs(base: Path, root: Path) -> None:
    assert launcher.choose(base, root) == (base, None)


def test_a_pending_version_is_tried_and_counted(base: Path, root: Path) -> None:
    release = copy_of(root / "releases" / "0.5.1", "0.5.1")
    UpdateState(pending="0.5.1").save(root / "state.json")
    assert launcher.choose(base, root) == (release, "0.5.1")
    assert UpdateState.load(root / "state.json").attempts == 1


def test_a_pending_version_that_never_confirms_is_rolled_back(base: Path, root: Path) -> None:
    copy_of(root / "releases" / "0.5.1", "0.5.1")
    UpdateState(pending="0.5.1").save(root / "state.json")
    for _ in range(launcher.MAX_TRIALS):
        assert launcher.choose(base, root)[1] == "0.5.1"
    assert launcher.choose(base, root) == (base, None)
    state = UpdateState.load(root / "state.json")
    assert state.pending is None and state.bad == ["0.5.1"]
    assert not (root / "releases" / "0.5.1").exists()


def test_a_failed_start_of_a_new_version_rolls_back_to_the_previous_one(
    tmp_path: Path, base: Path, root: Path
) -> None:
    previous = copy_of(root / "releases" / "0.5.1", "0.5.1")
    copy_of(root / "releases" / "0.5.2", "0.5.2")
    UpdateState(current="0.5.1", pending="0.5.2").save(root / "state.json")
    started: list[Path] = []
    exits = iter([1, 0])  # the new version crashes; the previous one stops normally

    def child(app: Path) -> int:
        started.append(app)
        return next(exits)

    assert launcher.supervise(base, root, run_child=child, sleep=lambda _s: None) == 0
    assert started == [root / "releases" / "0.5.2", previous]
    state = UpdateState.load(root / "state.json")
    assert (state.current, state.pending, state.bad) == ("0.5.1", None, ["0.5.2"])


def test_a_confirmed_new_version_is_kept_after_a_crash(base: Path, root: Path) -> None:
    copy_of(root / "releases" / "0.5.1", "0.5.1")
    UpdateState(pending="0.5.1").save(root / "state.json")
    exits = iter([1, 0])

    def child(app: Path) -> int:
        state = UpdateState.load(root / "state.json")
        if state.pending:  # the node confirmed itself, then crashed later
            UpdateState(current="0.5.1").save(root / "state.json")
        return next(exits)

    assert launcher.supervise(base, root, run_child=child, sleep=lambda _s: None) == 0
    assert UpdateState.load(root / "state.json").current == "0.5.1"


def test_the_restart_code_starts_the_new_version(base: Path, root: Path) -> None:
    copy_of(root / "releases" / "0.5.1", "0.5.1")
    started: list[Path] = []

    def child(app: Path) -> int:
        started.append(app)
        if len(started) == 1:
            UpdateState(pending="0.5.1").save(root / "state.json")
            return RESTART_EXIT_CODE
        return 0

    launcher.supervise(base, root, run_child=child, sleep=lambda _s: None)
    assert started == [base, root / "releases" / "0.5.1"]


def test_a_newer_installer_wins_over_an_older_self_update(tmp_path: Path, root: Path) -> None:
    base = copy_of(tmp_path / "base", "0.6.0")
    copy_of(root / "releases" / "0.5.1", "0.5.1")
    UpdateState(current="0.5.1").save(root / "state.json")
    assert launcher.choose(base, root) == (base, None)


def test_a_copy_that_keeps_crashing_is_given_up(base: Path, root: Path) -> None:
    pauses: list[float] = []
    code = launcher.supervise(
        base, root, run_child=lambda _app: 1, sleep=pauses.append, clock=lambda: 100.0
    )
    assert code == 1
    assert len(pauses) == launcher.MAX_CRASHES_PER_HOUR - 1
    assert max(pauses) == 60.0


def test_a_one_off_command_never_counts_a_trial(base: Path, root: Path) -> None:
    copy_of(root / "releases" / "0.5.1", "0.5.1")
    UpdateState(pending="0.5.1").save(root / "state.json")
    assert launcher.choose(base, root, trial=False) == (base, None)
    assert UpdateState.load(root / "state.json").attempts == 0


def test_the_logon_task_runs_as_the_user_with_least_privilege() -> None:
    xml = launcher.task_xml(
        user="SPRINGFIELD\\alex",
        command="C:\\Users\\alex\\AppData\\Local\\Programs\\Nox\\runtime\\pythonw.exe",
        arguments='"C:\\Users\\alex\\AppData\\Local\\Programs\\Nox\\app\\start_node.pyw"',
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
    assert argv[1] == "-I" and Path(argv[2]).is_absolute()
    assert cwd == str(tmp_path)
    assert env["NOX_NODE_APP"] == str(tmp_path)
    assert not any(k.upper().startswith("PYTHON") for k in env)
