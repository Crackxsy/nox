"""Recorded event shapes and how to play them back.

A recording is a JSON Lines file under `recordings/`. Every line is one object with a `seq` and a
`dir`:

- `{"dir": "note", "text": ...}` - a comment about the recording (where the shape comes from);
- `{"dir": "server", "line": "<one IRC line>"}` - what a Twitch chat server sent (Twitch);
- `{"dir": "server", "event": {"eventType": ..., "eventData": ...}}` - an obs-websocket `Event`
  (OBS).

Only `server` entries are played back; what the client sent in answer is judged by the test, not
replayed. The shapes follow what the services document and send; every name, id, channel, file and
text in them is invented (`viewer_01`, `examplechannel`, `example.invalid`). `check_recording`
holds a recording to that: it is what `tests/unit/workshop/test_recordings.py` runs over every
file in the folder.

There is no timing in a recording: lines are played in order, as fast as the receiver reads them,
so a replay never depends on how long anything takes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

RECORDINGS_DIR = Path(__file__).resolve().parent / "recordings"

#: Logins a recording may mention. A login that is not here is a real account until proven
#: otherwise, and does not belong in a public repository.
ALLOWED_LOGINS = re.compile(r"^(viewer_\d{2}|raider_\d{2}|noxbot|nightbot|streamelements)$")
_LOGIN_IN_LINE = re.compile(r"(?:^|[; ]):?(\w+)!\1@\1\.tmi\.twitch\.tv|[;@]login=(\w+)")


def load(source: str | Path) -> list[dict[str, Any]]:
    """Every entry of a recording, in order: `recordings/<name>.jsonl` for a name, or a file."""
    path = source if isinstance(source, Path) else RECORDINGS_DIR / f"{source}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def irc_lines(name: str) -> list[str]:
    """The IRC lines a Twitch recording has the server send."""
    return [entry["line"] for entry in load(name) if entry["dir"] == "server" and "line" in entry]


def obs_events(name: str) -> list[dict[str, Any]]:
    """The `{"eventType", "eventData"}` objects an OBS recording has the server send."""
    return [entry["event"] for entry in load(name) if entry["dir"] == "server" and "event" in entry]


def check_recording(source: str | Path) -> list[str]:
    """What is wrong with a recording as a public fixture; an empty list means nothing."""
    problems: list[str] = []
    entries = load(source)
    if [entry["seq"] for entry in entries] != list(range(len(entries))):
        problems.append("seq is not 0, 1, 2, ... in order")
    for entry in entries:
        if entry["dir"] not in ("note", "server"):
            problems.append(f"seq {entry['seq']}: unknown dir {entry['dir']!r}")
        text = json.dumps(entry)
        for match in _LOGIN_IN_LINE.finditer(entry.get("line", "")):
            login = match.group(1) or match.group(2)
            if not ALLOWED_LOGINS.match(login):
                problems.append(f"seq {entry['seq']}: login {login!r} is not an invented one")
        if re.search(r"https?://(?!example\.invalid)", text):
            problems.append(f"seq {entry['seq']}: a link that is not example.invalid")
        if re.search(r"[\w.]+@[\w-]+\.(com|net|org|de)\b", text):
            problems.append(f"seq {entry['seq']}: looks like an e-mail address")
        if re.search(r"oauth:|password|token", text, re.IGNORECASE):
            problems.append(f"seq {entry['seq']}: mentions a credential")
    return problems
