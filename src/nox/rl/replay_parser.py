"""Pure-Python Rocket League replay header/property-tree parser (executes). Reads only the replay's
header block - engine/licensee/net version, game type, and the property tree (playlist/map/team
size/duration/per-player summary stats/result) - never the full network-frame body (deliberate
scope cut). No input synthesis, no process/memory access: this only reads bytes the caller already
has (a local `.replay` file), the same observation-only posture as the rest of `rl`.

Binary layout (reverse-engineered and validated against the user's real replay batch - see
``'s report and the spike note for the measured success rate):

int32 header_size uint32 header_crc int32 engine_version int32 licensee_version int32 net_version
(only if engine_version >= 868 and licensee_version >= 18) String game_type Properties
(name/type/size/array_index/value tuples, terminated by "None")

Property encoding is `name:String, type:String, size:int32, array_index:int32, value` where `value`
is read according to `type` (Int/Float/QWord/Str/Name/Bool/Byte-enum/Array-of-Properties/Struct).
`size` is a redundant hint the encoder writes for the *trailing* value only: it excludes
`ByteProperty`'s enum-type-name sub-string and `StructProperty`'s struct-name sub-string, and is
unreliably `0` for `BoolProperty` even though one byte is still on the wire. This parser therefore
never repositions the cursor from `size` for a type it understands; only `StructProperty` (whose
platform-specific payload - e.g. `PlayerID`'s platform id - is irrelevant to Stage 1) and any
wholly unrecognised property type are skipped using `size`, measured from just after their sub-
header, which is the only way to skip a value this parser does not need to interpret.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

PARSER_VERSION = "0.1.0"

#: Engine-native structs with a fixed, non-property-list binary layout (N little-endian floats).
_RAW_STRUCTS: dict[str, int] = {"Vector": 3, "Rotator": 3, "Vector2D": 2}

#: Header property names copied verbatim into the header_json returned by the parser (Spec).
_WANTED_SCALAR_KEYS = (
    "TeamSize",
    "UnfairTeamSize",
    "Team0Score",
    "Team1Score",
    "PrimaryPlayerTeam",
    "WinningTeam",
    "TotalSecondsPlayed",
    "MatchStartEpoch",
    "MapName",
    "MatchType",
    "Date",
    "NumFrames",
    "RecordFPS",
    "ReplayName",
    "Id",
    "MatchGuid",
)


class ReplayParseError(RuntimeError):
    """A field-attributed parse failure (Spec/ AC: attribute to a specific field
    where possible, never an opaque single failure)."""

    def __init__(self, field_name: str, detail: str) -> None:
        super().__init__(f"{field_name}: {detail}")
        self.field_name = field_name
        self.detail = detail


class _Cursor:
    __slots__ = ("data", "off")

    def __init__(self, data: bytes, off: int = 0) -> None:
        self.data = data
        self.off = off

    def _need(self, n: int, what: str) -> None:
        if self.off + n > len(self.data):
            raise ReplayParseError(
                what, f"truncated: need {n} bytes at {self.off}, have {len(self.data) - self.off}"
            )

    def i32(self, what: str = "int32") -> int:
        self._need(4, what)
        v: int = struct.unpack_from("<i", self.data, self.off)[0]
        self.off += 4
        return v

    def u32(self, what: str = "uint32") -> int:
        self._need(4, what)
        v: int = struct.unpack_from("<I", self.data, self.off)[0]
        self.off += 4
        return v

    def i64(self, what: str = "int64") -> int:
        self._need(8, what)
        v: int = struct.unpack_from("<q", self.data, self.off)[0]
        self.off += 8
        return v

    def f32(self, what: str = "float32") -> float:
        self._need(4, what)
        v: float = struct.unpack_from("<f", self.data, self.off)[0]
        self.off += 4
        return v

    def byte(self, what: str = "byte") -> int:
        self._need(1, what)
        v = self.data[self.off]
        self.off += 1
        return v

    def raw(self, n: int, what: str = "bytes") -> bytes:
        self._need(n, what)
        v = self.data[self.off : self.off + n]
        self.off += n
        return v

    def string(self, what: str = "string") -> str:
        n = self.i32(what)
        if n == 0:
            return ""
        if n > 0:
            raw = self.raw(n, what)
            return raw.split(b"\x00", 1)[0].decode("latin1", errors="replace")
        raw = self.raw(-n * 2, what)
        return raw.decode("utf-16-le", errors="replace").split("\x00", 1)[0]


_SIMPLE_READERS: dict[str, Any] = {
    "IntProperty": lambda c, name: c.i32(name),
    "FloatProperty": lambda c, name: c.f32(name),
    "QWordProperty": lambda c, name: c.i64(name),
    "StrProperty": lambda c, name: c.string(name),
    "NameProperty": lambda c, name: c.string(name),
    "BoolProperty": lambda c, name: bool(c.byte(name)),
}


def _read_byte_property(c: _Cursor, name: str) -> dict[str, str]:
    """`ByteProperty` (UE enum): enum type name, then the enum value name (e.g. `Platform` ->
    `OnlinePlatform` / `OnlinePlatform_Epic`)."""
    enum_type = c.string(name)
    value = c.string(name)
    return {"enum": enum_type, "value": value}


def _read_properties(c: _Cursor, depth: int = 0, max_depth: int = 8) -> dict[str, Any]:
    """One property list, terminated by a property named `"None"` - used for the header's top
    level and recursively for each `ArrayProperty` element."""
    if depth > max_depth:
        raise ReplayParseError("properties", f"nesting too deep (>{max_depth})")
    props: dict[str, Any] = {}
    while True:
        name = c.string("property.name")
        if name in ("None", ""):
            break
        ptype = c.string(f"{name}.type")
        # Redundant trailing-value-size hint the encoder writes for *this* value only (see module
        # docstring: excludes ByteProperty's enum-type-name / StructProperty's struct-name
        # sub-strings, unreliably 0 for BoolProperty). Kept only to skip StructProperty/unknown
        # values we do not interpret; every understood type is read structurally instead.
        size = c.i32(f"{name}.size")
        c.i32(f"{name}.array_index")
        if ptype == "ArrayProperty":
            count = c.i32(f"{name}.count")
            if count < 0 or count > 100_000:
                raise ReplayParseError(name, f"implausible array count {count}")
            props[name] = [_read_properties(c, depth + 1, max_depth) for _ in range(count)]
        elif ptype == "ByteProperty":
            props[name] = _read_byte_property(c, name)
        elif ptype == "StructProperty":
            struct_name = c.string(f"{name}.struct_name")
            after_name = c.off
            if struct_name in _RAW_STRUCTS:
                props[name] = {
                    "struct": struct_name,
                    "values": [c.f32(name) for _ in range(_RAW_STRUCTS[struct_name])],
                }
            else:
                # Platform-specific id blobs (PlayerID's UniqueNetId, NpId, ...) are irrelevant to
                # Stage 1's header summary; skip `size` bytes measured from just after struct_name
                # (validated against the real replay batch - see module docstring).
                props[name] = {"struct": struct_name}
                c.off = after_name + size
        elif ptype in _SIMPLE_READERS:
            props[name] = _SIMPLE_READERS[ptype](c, name)
        else:
            # Unknown/format-drift property type: skip via size (best effort) rather than aborting
            # the whole header - the size hint's base position matches simple types here since no
            # sub-header string precedes an unknown scalar value.
            props[name] = None
            c.off += size
    return props


def parse_header(data: bytes) -> ParsedReplayHeader:
    """Parse one `.replay` file's header. Never raises: returns `parse_status="failed"` with the
    first field-attributed error on any unrecoverable problem (truncated/corrupt file, unknown
    format drift)."""
    errors: list[str] = []
    c = _Cursor(data)
    try:
        header_size = c.i32("header_size")
        c.u32("header_crc")
        engine_version = c.i32("engine_version")
        licensee_version = c.i32("licensee_version")
        net_version: int | None = None
        if engine_version >= 868 and licensee_version >= 18:
            net_version = c.i32("net_version")
        game_type = c.string("game_type")
    except ReplayParseError as exc:
        return ParsedReplayHeader(
            parse_status="failed",
            errors=[str(exc)],
            engine_version=None,
            licensee_version=None,
            net_version=None,
            game_type="",
            properties={},
        )

    properties: dict[str, Any] = {}
    try:
        properties = _read_properties(c)
    except ReplayParseError as exc:
        errors.append(str(exc))

    status = "ok" if not errors else ("partial" if properties else "failed")
    return ParsedReplayHeader(
        parse_status=status,
        errors=errors,
        engine_version=engine_version,
        licensee_version=licensee_version,
        net_version=net_version,
        game_type=game_type,
        properties=properties,
        header_size=header_size,
    )


@dataclass(slots=True)
class ParsedReplayHeader:
    parse_status: str  # ok | partial | failed
    errors: list[str]
    engine_version: int | None
    licensee_version: int | None
    net_version: int | None
    game_type: str
    properties: dict[str, Any] = field(default_factory=dict)
    header_size: int | None = None

    def summary(self) -> dict[str, Any]:
        """Extract the Spec/ header_json fields the summary/dashboard needs, from the
        raw property tree. Missing fields are simply absent (never fabricated)."""
        p = self.properties
        out: dict[str, Any] = {}
        for key in _WANTED_SCALAR_KEYS:
            if key in p:
                out[key] = p[key]
        team_size = p.get("TeamSize")
        team0 = p.get("Team0Score", 0)
        team1 = p.get("Team1Score", 0)
        primary_team = p.get("PrimaryPlayerTeam")
        winning_team = p.get("WinningTeam")
        result = "unknown"
        score_self: int | None = None
        score_opponent: int | None = None
        if isinstance(primary_team, int):
            score_self = team0 if primary_team == 0 else team1
            score_opponent = team1 if primary_team == 0 else team0
            if isinstance(winning_team, int):
                result = "win" if winning_team == primary_team else "loss"
        duration_s = p.get("TotalSecondsPlayed")
        if duration_s is None:
            num_frames = p.get("NumFrames")
            fps = p.get("RecordFPS") or 30.0
            if isinstance(num_frames, int) and fps:
                duration_s = num_frames / fps
        players = []
        for entry in p.get("PlayerStats", []) or []:
            if not isinstance(entry, dict):
                continue
            platform = entry.get("Platform")
            players.append(
                {
                    "name": entry.get("Name", ""),
                    "team": entry.get("Team"),
                    "score": entry.get("Score"),
                    "goals": entry.get("Goals"),
                    "assists": entry.get("Assists"),
                    "saves": entry.get("Saves"),
                    "shots": entry.get("Shots"),
                    "is_bot": bool(entry.get("bBot", False)),
                    "platform": platform.get("value") if isinstance(platform, dict) else None,
                }
            )
        out.update(
            {
                "team_size": team_size,
                "map": p.get("MapName"),
                "playlist": p.get("MatchType"),
                "duration_s": duration_s,
                "result": result,
                "score_self": score_self,
                "score_opponent": score_opponent,
                "players": players,
            }
        )
        return out


def parse_replay_file(path: str) -> ParsedReplayHeader:
    """Read `path` and parse its header. I/O errors are reported the same honest way as a format
    parse failure - never raised past this function."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        return ParsedReplayHeader(
            parse_status="failed",
            errors=[f"io: {exc}"],
            engine_version=None,
            licensee_version=None,
            net_version=None,
            game_type="",
            properties={},
        )
    return parse_header(data)
