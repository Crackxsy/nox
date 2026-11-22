"""Recorded event shapes: the files are fit to be public, and replaying them through the real
plugins gives exactly the events the shapes stand for.

The recordings hold what Twitch's IRC server and obs-websocket send in a typical session - chat
with every kind of tag, subs, gifts, raids, notices, a `PING`; scene changes, going live and
recording - with invented names. A replay sends them in order, with no timing, to a plugin that is
connected to the fake; the test then states, event by event, what the plugin made of them.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from nox_plugin_obs import create as create_obs
from nox_plugin_twitch import create as create_twitch
from nox_plugin_twitch.plugin import command_list

from tests.unit.fakes import wait_until
from tests.unit.plugins.obs.conftest import FakeClient as ObsFakeClient
from tests.unit.plugins.obs.conftest import make_api as make_obs_api
from tests.unit.plugins.twitch.conftest import FakeClient as TwitchFakeClient
from tests.unit.plugins.twitch.conftest import RecordingLogger
from tests.unit.plugins.twitch.conftest import make_api as make_twitch_api
from tests.workshop.fake_obs import FakeObs
from tests.workshop.fake_twitch import FakeTwitchIrc
from tests.workshop.recordings import (
    RECORDINGS_DIR,
    check_recording,
    irc_lines,
    load,
    obs_events,
)

pytestmark = pytest.mark.timeout(30)

ALL_RECORDINGS = sorted(path.stem for path in RECORDINGS_DIR.glob("*.ndjson"))


# -- the files ----------------------------------------------------------------------------------


def test_the_two_recordings_are_there() -> None:
    assert ALL_RECORDINGS == ["obs_stream_session", "twitch_chat_evening"]


@pytest.mark.parametrize("name", ALL_RECORDINGS)
def test_a_recording_is_ordered_invented_and_free_of_credentials(name: str) -> None:
    assert check_recording(name) == []


@pytest.mark.parametrize("name", ALL_RECORDINGS)
def test_a_recording_starts_with_a_note_saying_it_is_invented(name: str) -> None:
    first = load(name)[0]
    assert first["dir"] == "note"
    assert "invented" in first["text"] or "made up" in first["text"]


@pytest.fixture
def bad_recording(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "bad.ndjson"
    yield path


def _write(path: Path, entries: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("entry", "complaint"),
    [
        (
            {
                "dir": "server",
                "line": ":someone_real!someone_real@someone_real.tmi.twitch.tv PRIVMSG #c :hi",
            },
            "is not an invented one",
        ),
        (
            {"dir": "server", "line": "@login=some_streamer;x=1 :tmi.twitch.tv USERNOTICE #c"},
            "is not",
        ),
        (
            {
                "dir": "server",
                "line": ":v!v@v.tmi.twitch.tv PRIVMSG #c :see https://real.example.org",
            },
            "link",
        ),
        ({"dir": "server", "line": ":v!v@v.tmi.twitch.tv PRIVMSG #c :mail me: a@b.com"}, "e-mail"),
        ({"dir": "server", "line": "PASS oauth:abcdef"}, "credential"),
        ({"dir": "client", "line": "x"}, "unknown dir"),
    ],
)
def test_the_check_names_what_does_not_belong_in_a_public_recording(
    bad_recording: Path, entry: dict[str, Any], complaint: str
) -> None:
    path = _write(bad_recording, [{"seq": 0, **entry}])
    problems = check_recording(path)
    assert any(complaint in problem for problem in problems), problems


def test_the_check_notices_a_gap_in_the_sequence(bad_recording: Path) -> None:
    path = _write(
        bad_recording,
        [{"seq": 0, "dir": "note", "text": "a"}, {"seq": 2, "dir": "note", "text": "b"}],
    )
    assert check_recording(path) == ["seq is not 0, 1, 2, ... in order"]


def test_the_invented_names_pass_the_check(bad_recording: Path) -> None:
    line = (
        ":viewer_42!viewer_42@viewer_42.tmi.twitch.tv PRIVMSG #examplechannel :see example.invalid"
    )
    path = _write(bad_recording, [{"seq": 0, "dir": "server", "line": line}])
    assert check_recording(path) == []


# -- a chat evening, replayed through the Twitch plugin ---------------------------------


def _events(client: TwitchFakeClient, name: str) -> list[dict[str, Any]]:
    return [payload for event, payload in client.events if event == name]


async def test_a_chat_evening_replayed_gives_the_events_it_stands_for(
    irc: FakeTwitchIrc,
) -> None:
    client = TwitchFakeClient()
    api = make_twitch_api(client, port=irc.port, logger=RecordingLogger(), rate_limit_min_gap_s=0.0)
    plugin = create_twitch(api)
    await plugin.start()
    try:
        await wait_until(lambda: plugin.client.joined, what="the plugin to join")
        await irc.send_lines(irc_lines("twitch_chat_evening"))
        # The last line of the recording is a chat line: when it is through, so is the rest.
        await wait_until(
            lambda: len(_events(client, "twitch.chat_message")) == 8, what="all chat lines"
        )
        await wait_until(lambda: irc.delivered, what="the answer to !befehle")
        await wait_until(lambda: irc.pongs, what="the answer to PING")
    finally:
        await plugin.stop()

    chat = [
        (c["viewer_id"], c["text"], c["addressed_to_nox"])
        for c in _events(client, "twitch.chat_message")
    ]
    assert chat == [
        ("100001", "guten abend zusammen", False),
        ("100002", "Kappa nice", False),
        ("100003", "cheer100 gg", False),
        ("100004", "genau das", True),  # a reply to the bot's own message
        ("100001", "\x01ACTION winkt\x01", False),
        ("100005", "!befehle", False),  # a command is not a request to Nox
        ("100007", "ein Satz mit Umlauten: äöüß und einem Emoji \U0001f44b", False),
        ("100001", "bis morgen", False),
    ]  # no line from nightbot: another bot is not heard

    awards = [
        (a["viewer_id"], a["reason"], a["units"]) for a in _events(client, "stream.funken_awarded")
    ]
    assert awards == [
        ("100001", "message", 1.0),
        ("100002", "message", 1.0),
        ("100003", "message", 1.0),
        ("100003", "bit", 100.0),
        ("100004", "message", 1.0),
        ("100007", "message", 1.0),
        ("100008", "sub", 1.0),
        ("100009", "sub", 1.0),
        ("100010", "sub", 3.0),  # the mass gift once; its three single gifts add nothing
        ("100014", "raid", 1.0),
    ]
    assert [d.text for d in irc.delivered] == [command_list("de")]
    assert irc.pongs == ["PONG :tmi.twitch.tv"]
    assert len(irc.connections) == 1  # nothing in the evening made the client reconnect


# -- an OBS session, replayed through the OBS plugin -----------------------------------


async def test_an_obs_session_replayed_gives_the_events_it_stands_for(obs: FakeObs) -> None:
    client = ObsFakeClient(secret=None)
    plugin = create_obs(make_obs_api(client, port=obs.port))
    await plugin.start()
    try:
        await wait_until(lambda: plugin.client.identified, what="the plugin to connect")
        await obs.replay_events(obs_events("obs_stream_session"))
        await wait_until(
            lambda: any(name == "stream.ended" for name, _ in client.events), what="stream.ended"
        )
        await wait_until(
            lambda: sum(name == "obs.recording_changed" for name, _ in client.events) == 2,
            what="both recording changes",
        )
        seen = list(client.events)  # before stopping: stopping adds an `obs.disconnected`
    finally:
        await plugin.stop()

    names = [name for name, _ in seen]
    assert names == [
        "obs.connected",
        "obs.scene_changed",
        "stream.started",  # STARTING and STARTED are one start
        "obs.recording_changed",
        "obs.scene_changed",
        "stream.ended",  # STOPPING and STOPPED are one end
        "obs.recording_changed",
    ]
    scenes = [payload for name, payload in seen if name == "obs.scene_changed"]
    assert scenes == [
        {"previous_scene": "", "current_scene": "Live", "by": "manual"},
        {"previous_scene": "Live", "current_scene": "Pause", "by": "manual"},
    ]
    assert [p["recording"] for n, p in seen if n == "obs.recording_changed"] == [
        True,
        False,
    ]


async def test_events_of_a_category_the_plugin_did_not_ask_for_are_not_even_sent(
    obs: FakeObs,
) -> None:
    """The recording holds an `InputMuteStateChanged`; the plugin subscribes to general, scene and
    output events only, so the fake never delivers it."""
    client = ObsFakeClient(secret=None)
    plugin = create_obs(make_obs_api(client, port=obs.port))
    await plugin.start()
    try:
        await wait_until(lambda: plugin.client.identified, what="the plugin to connect")
        kinds = {event["eventType"] for event in obs_events("obs_stream_session")}
        assert "InputMuteStateChanged" in kinds
        delivered: list[str] = []
        original = plugin._on_obs_event

        async def spy(event_type: str, data: dict[str, Any]) -> None:
            delivered.append(event_type)
            await original(event_type, data)

        plugin.client._on_event = spy
        await obs.replay_events(obs_events("obs_stream_session"))
        await wait_until(lambda: "ExitStarted" in delivered, what="the last event")
    finally:
        await plugin.stop()
    assert "InputMuteStateChanged" not in delivered
    assert delivered[0] == "CurrentProgramSceneChanged"
