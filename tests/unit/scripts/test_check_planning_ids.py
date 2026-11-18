"""The readability guard, run locally (`scripts/check_planning_ids.py`): comments, docstrings and
docs name no document a reader of this repository cannot open. Every kind of reference the guard
knows has a literal case here; the samples are assembled from pieces so that this file does not
trip the scan it tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
SECTION = chr(0xA7)
MISREAD = chr(0xC2)  # what precedes it when UTF-8 was read as Latin-1
#: The letters of the delivery-step ids, apart from their numbers.
STEP_C, STEP_O = "C", "O"
#: Words of the private plans, spelled in pieces for the same reason.
SPEC, SPEC_LC, NEUBAU = "Spec", "spec", "Neubau"
WAVE, WELLE = "wa" + "ve", "Wel" + "le"


def guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_planning_ids", ROOT / "scripts" / "check_planning_ids.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tree(tmp_path: Path, text: str, *, name: str = "src/nox/sample.py") -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
    return tmp_path


def test_the_repository_is_clean() -> None:
    assert guard().problems() == []


@pytest.mark.parametrize(
    "line",
    [
        f"# refused on its own authority (spec C {SECTION}1.6)",
        f"# routed to nodes (spec C {SECTION}1.4/{SECTION}1.5)",
        f"-- a marker path (Spec v0.6 {SECTION}4.3)",
        f"-- five minutes by default (Spec {SECTION}5.2)",
        f"# the stop key ({SECTION}1.7, {SECTION}1.8)",
        "# asked on the device (owner " + "decision D1)",
        "# confirmed twice (owner " + "decision 2026-10-18)",
        "# every message counts (owner " + f"decision, {STEP_C}10)",
        "# it counts as no (re" + "view M-c)",
        "# never clicked (re" + "view H2)",
        '    """Re' + 'view N5."""',
        "# one container per run (re-re" + "view N6)",
        "# ---- re" + "view round 2: an allowlist of data files ----",
        '    """Re' + "view panel round 1: the fallback is offered" + '"""',
        "# words from a room (" + f"{STEP_C}9): not the owner",
        "# the claim (" + f"{STEP_O}3)",
        "# its own addresses (" + "B4)",
        "# end to end (" + "L5/L6)",
        "# through the real wiring (" + "R1e, R1f)",
        "# tested with a permissive core (acceptance " + "criterion 1.12.3)",
        "# the " + f"{STEP_C}6 tools of this device",
        "# " + f"{STEP_O}2's sub-stage puts these into the image",
        '    """' + "N3: reachable through the confirmed path" + '"""',
        '    """' + 'N2."""',
        "# ---- " + "M-c: nothing types while the person is asked ----",
        "# ---- " + "H2/H3/M-a/M-b: approvals while the lock holds ----",
        "# ---- " + "H1 a: the lock comes only from this core ----",
        "# ---- " + "M2 (architecture): one writer ----",
        "x = remote(1)  # " + "N7",
        f"# in its last days (spec C {MISREAD}{SECTION}1.2)",
        f"# {SPEC} UX-{NEUBAU}, glossary: one name for one control",
        # delivery waves, in either language, with a letter
        f"# the {WAVE} C tasks run against a simulated device",
        f"> Status: {WAVE} C is complete.",
        f"# discovery comes with {WAVE} D",
        f"{WELLE} C ist fertig, die Suche kommt mit {WELLE} D.",
        f"# {SPEC_LC} {WAVE} and its follow-ups",
        f"# the later {WELLE}n C-F",
        # epic and story numbers
        "# EPIC-" + "13 PM index mirror",
        "-- vault id, e.g. ST-" + "13-01",
        '"""Story ST-' + '07-04 keeps the previous version."""',
        # review findings, panels and decisions without an id letter
        "# stays closed (review " + "finding 8).",
        "    # Review " + "finding #18: the migration used to strip",
        "# keep the page clear of it (review " + "finding).",
        "# ---- web.search backend exception (review " + "finding 16) ----",
        "# the review " + "panel found it",
        "# a deliberate choice, not an owner " + "decision",
        "x = 1  # (owner " + "decision)",
        # delivery-step ids on their own
        f"# the {STEP_C}6 tools of this device",
        f"# {STEP_O}2's dashboard toggle unmasks it",
        f"# Hardening proper is {STEP_O}2 and then {STEP_O}3 first boot",
        f"| `03-hardening` | {STEP_O}2 | nftables |",
        f"## Hardening ({STEP_O}2)",
        f"# a later step ({STEP_O}8)",
        f"# {STEP_C}1 and {STEP_C}10 are the ends of the range",
        f"# the setup window (step {STEP_O}3): while unclaimed",
        "# one question, UX " + "K2",
        "# see spec " + "D1 for why",
        '  needs: "E' + '2: USB backup target"',
        '  needs: "D' + '1/D2: household discovery"',
        "  needs: 'F" + "1/F2: remote access'",
        f'    "os-update-status": "{STEP_O}6: update status tool"',
    ],
)
def test_every_kind_of_private_reference_is_found(tmp_path: Path, line: str) -> None:
    found = guard().problems(tree(tmp_path, line), allowed=[])
    assert len(found) == 1 and found[0].startswith("src/nox/sample.py:1: ")


@pytest.mark.parametrize(
    "line",
    [
        "# The node refuses on its own authority, whatever the core decided.",
        f"# Security Model {SECTION}10: input synthesis stays in two guarded files",
        '"voice_styles/M1.json": (',
        "# every C0/C1 control character is escaped",
        "# sqlite-vec's L2 distance converts exactly to cosine similarity",
        "#: 30 words, no marker",
        'B64 = "QWxhZGRpbjpvcGVuIHNlc2FtZQ=="',
        "# The review found nothing to change here.",
        "# see docs/ARCHITECTURE-NODES.md, 6a (session generations)",
        "# TLS 1.3 only (RFC 8446)",
        "assert spec.options == ['', 'F3', 'M1']",
        "# a heading: H1 is the title of the page",
        # the same letters and numbers in their ordinary meanings
        "# every C0/C1 control character is escaped",
        "# C1 controls (U+0080..U+009F) are escaped as well",
        "# a bare name like git, not CO2 or H2O",
        "# a sine " + "wave A4 tone, 440 Hz",
        "import " + "wave  # the standard library module",
        "# a wave of requests, a wave file, the wave module",
        "# Press F1 for help; F3 is the voice; F5 reloads the page",
        "# Playwright E2E tests run on D1 hardware",
        "# an epic story: the owner decides, and that decision is final",
        "# step 5 of the installer; step B of the wizard",
        "# the spec for the file format says so",
        "# Stage 2 of the pipeline, O(1) lookup, not an id",
    ],
)
def test_ordinary_text_is_not_a_hit(tmp_path: Path, line: str) -> None:
    assert guard().problems(tree(tmp_path, line), allowed=[]) == []


@pytest.mark.parametrize(
    "path",
    [
        "src/nox/x.py",
        "src/nox/data/migrations/0099_x.sql",
        "tests/unit/test_x.py",
        "docs/X.md",
        "ui/dashboard/src/App.tsx",
        "ui/pet/src/styles.css",
        "installer/linux/nox-node.service",
        "os/README.md",
        "os/pi-gen/config",
        "os/pi-gen/stage-nox/03-hardening/00-run.sh",
        "evals/kann_alles/tasks/x.yaml",
        ".github/workflows/x.yml",
        "config/defaults.yaml",
        "plugins/creative/src/x.py",
        "scripts/x.py",
        "README.md",
        "CONTRIBUTING.md",
        "CHANGELOG.md",
        "SECURITY.md",
    ],
)
def test_every_part_of_the_repository_is_scanned(tmp_path: Path, path: str) -> None:
    root = tree(tmp_path, "see (" + f"{STEP_C}9) for why", name=path)
    assert len(guard().problems(root, allowed=[])) == 1


@pytest.mark.parametrize(
    "path",
    [
        "spikes/x.py",
        "src/nox/icon.png",
        "docs/node_modules/pkg/readme.md",
        "ui/dashboard/node_modules/pkg/readme.md",
        "ui/dashboard/dist/assets/index.js",
        "ui/dashboard/package-lock.json",
        ".venv/lib/x.py",
        "NOTES.md",
        "uv.lock",
    ],
)
def test_other_folders_generated_files_and_binary_files_are_not_scanned(
    tmp_path: Path, path: str
) -> None:
    tree(tmp_path, "see (" + f"{STEP_C}9)", name=path)
    assert guard().problems(tmp_path, allowed=[]) == []


def test_a_file_with_a_nul_byte_is_binary_and_not_scanned(tmp_path: Path) -> None:
    path = tmp_path / "os" / "image.part"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\x00\x01 see (" + f"{STEP_C}9)".encode() + b"\n")
    assert guard().problems(tmp_path, allowed=[]) == []


def test_an_allowlisted_line_passes_and_only_that_line(tmp_path: Path) -> None:
    module = guard()
    step, other = STEP_O + "6", STEP_C + "9"
    root = tree(tmp_path, f'LATER = {{"task": "{step}"}}  # ({step})\n# and ({other}) here')
    entry = module.Allowed("src/nox/sample.py", "LATER = ", "benchmark data")
    found = module.problems(root, allowed=[entry])
    assert len(found) == 1 and found[0].startswith("src/nox/sample.py:2: ")


def test_an_allowlist_entry_for_another_file_does_not_apply(tmp_path: Path) -> None:
    module = guard()
    root = tree(tmp_path, "see (" + f"{STEP_C}9)")
    entry = module.Allowed("src/nox/other.py", "see", "another file")
    found = module.problems(root, allowed=[entry])
    assert any(line.startswith("src/nox/sample.py:1: ") for line in found)


def test_an_allowlist_entry_that_matches_nothing_fails(tmp_path: Path) -> None:
    module = guard()
    root = tree(tmp_path, "# nothing to see")
    entry = module.Allowed("src/nox/sample.py", "gone", "was needed once")
    assert module.problems(root, allowed=[entry]) == [
        "allowlist: `src/nox/sample.py :: gone` matches nothing any more"
    ]


def test_a_malformed_allowlist_line_is_an_error(tmp_path: Path) -> None:
    listing = tmp_path / "allow.txt"
    listing.write_text("# comment\n\nsrc/x.py :: only two parts\n", encoding="utf-8")
    with pytest.raises(ValueError, match="allow.txt:3"):
        guard().allowlist(listing)


def test_the_allowlist_file_parses_and_every_entry_has_a_reason() -> None:
    for entry in guard().allowlist():
        assert entry.path and entry.text and len(entry.reason) >= 10


def test_main_reports_each_hit_and_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = guard()
    monkeypatch.setattr(module, "problems", lambda: ["src/x.py:1: an id in parentheses: ..."])
    assert module.main() == 1
    assert "::error::src/x.py:1:" in capsys.readouterr().out
    monkeypatch.setattr(module, "problems", lambda: [])
    assert module.main() == 0
    assert "clean" in capsys.readouterr().out
