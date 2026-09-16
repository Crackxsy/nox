"""Pure-Python replay header/property-tree parser (ST-12-03, executes SP-17). Runs against the
user's real replay batch on disk - ordinary local files, not a hardware dependency, so these tests
are not marked `hardware` (Spec §12.2) and simply skip if the folder is absent (e.g. a CI runner
without this machine's Documents folder)."""

from __future__ import annotations

import struct

import pytest

from nox.rl import replay_parser
from nox.rl.paths import default_replay_folder

REPLAY_FOLDER = default_replay_folder()


def _sample_files(limit: int = 300) -> list:
    if not REPLAY_FOLDER.is_dir():
        return []
    return sorted(REPLAY_FOLDER.glob("*.replay"))[:limit]


@pytest.mark.skipif(
    not REPLAY_FOLDER.is_dir(), reason="real replay folder not present on this machine"
)
def test_parses_a_sample_of_the_real_replay_batch_with_a_high_success_rate() -> None:
    files = _sample_files(300)
    assert files, "replay folder exists but has no .replay files"
    ok = partial = failed = 0
    for path in files:
        result = replay_parser.parse_replay_file(str(path))
        if result.parse_status == "ok":
            ok += 1
        elif result.parse_status == "partial":
            partial += 1
        else:
            failed += 1
    success_rate = (ok + partial) / len(files)
    # Measured against the real 2,426-file batch: ~98.4% ok, 0% partial, ~1.6% failed (tiny/
    # corrupt files with engine_version==0) - see the SP-17 spike note for the full-batch numbers.
    assert success_rate >= 0.90, f"ok={ok} partial={partial} failed={failed} of {len(files)}"


@pytest.mark.skipif(
    not REPLAY_FOLDER.is_dir(), reason="real replay folder not present on this machine"
)
def test_a_successfully_parsed_replay_has_the_spec_7_header_fields() -> None:
    files = _sample_files(300)
    found_full = False
    for path in files:
        result = replay_parser.parse_replay_file(str(path))
        if result.parse_status != "ok":
            continue
        summary = result.summary()
        assert isinstance(summary.get("team_size"), int)
        assert summary.get("players") is not None
        if summary.get("map") and summary.get("duration_s") and summary.get("players"):
            found_full = True
    assert found_full, "no fully-populated header summary found in the sample"


def test_corrupt_or_truncated_file_fails_honestly_without_raising(tmp_path) -> None:
    garbage = b"\x00" * 4
    result = replay_parser.parse_header(garbage)
    assert result.parse_status == "failed"
    assert result.errors


@pytest.mark.skipif(
    not REPLAY_FOLDER.is_dir(), reason="real replay folder not present on this machine"
)
def test_a_truncated_real_replay_fails_without_crashing(tmp_path) -> None:
    files = _sample_files(5)
    assert files
    data = files[0].read_bytes()
    # Cut well inside the header's property tree (real headers run several KB) so the parser hits
    # a genuine truncation, not just the (irrelevant to Stage 1) network-frame body past it.
    truncated = data[:600]
    result = replay_parser.parse_header(truncated)
    assert result.parse_status in ("failed", "partial")


def test_parse_header_never_raises_on_random_bytes() -> None:
    rng_bytes = bytes((i * 37 + 11) % 256 for i in range(5000))
    result = replay_parser.parse_header(rng_bytes)
    assert result.parse_status in ("failed", "partial", "ok")


def test_summary_extracts_result_from_primary_team_and_winning_team() -> None:
    header = replay_parser.ParsedReplayHeader(
        parse_status="ok",
        errors=[],
        engine_version=868,
        licensee_version=32,
        net_version=10,
        game_type="TAGame.Replay_Soccar_TA",
        properties={
            "TeamSize": 2,
            "Team0Score": 3,
            "Team1Score": 5,
            "PrimaryPlayerTeam": 0,
            "WinningTeam": 1,
            "MapName": "cs_p",
            "MatchType": "Online",
            "TotalSecondsPlayed": 301.2,
            "PlayerStats": [
                {
                    "Name": "PlayerOne",
                    "Team": 0,
                    "Score": 400,
                    "Goals": 1,
                    "Assists": 2,
                    "Saves": 0,
                    "Shots": 3,
                    "bBot": False,
                    "Platform": {"enum": "OnlinePlatform", "value": "OnlinePlatform_Epic"},
                }
            ],
        },
    )
    summary = header.summary()
    assert summary["result"] == "loss"
    assert summary["score_self"] == 3
    assert summary["score_opponent"] == 5
    assert summary["players"][0]["name"] == "PlayerOne"
    assert summary["players"][0]["platform"] == "OnlinePlatform_Epic"


def test_string_reader_handles_utf16_negative_length() -> None:
    # A negative length is a UTF-16LE string of (-n) code units (incl. trailing null).
    payload = "hi".encode("utf-16-le") + b"\x00\x00"
    data = struct.pack("<i", -3) + payload
    cursor = replay_parser._Cursor(data)  # noqa: SLF001 - internal, tested directly for this edge case
    assert cursor.string() == "hi"
