"""What this device lets Nox do: the capability manifest `node.yaml`, kept on the device itself.

Only the device widens it - by editing the file or with `nox node allow <group>` in a terminal of
the logged-in user. The core can narrow what a node offers for as long as the connection lasts
(`nox/policy`), never widen it: `effective()` is always a subset of what is enabled here.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "GAME_NAME",
    "GROUPS",
    "GroupInfo",
    "NodeManifest",
    "load_manifest",
    "manifest_hash",
    "save_manifest",
]

#: A program name on the watched-game list (`RocketLeague.exe`): no path, no wildcard.
GAME_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.+()\-]{0,63}$")


@dataclass(frozen=True)
class GroupInfo:
    default: bool
    #: In plain words, for `nox node status` and the Devices page ("Nox darf hier: ...").
    label_de: str
    label_en: str


#: Spec C §1.4. Off by default unless it only reads or shows something harmless.
GROUPS: dict[str, GroupInfo] = {
    "system": GroupInfo(True, "Systemwerte lesen", "read system values"),
    "notify": GroupInfo(True, "Hinweise zeigen", "show notices"),
    "media": GroupInfo(True, "Musik steuern", "control music"),
    "files": GroupInfo(False, "Dateien", "files"),
    "shell": GroupInfo(False, "Befehle ausführen", "run commands"),
    "packages": GroupInfo(False, "Programme installieren", "install programs"),
    "screen": GroupInfo(False, "Bildschirm sehen", "see the screen"),
    "camera": GroupInfo(False, "Kamera", "camera"),
    "browser": GroupInfo(False, "Browser", "browser"),
    "desktop": GroupInfo(False, "Fenster und Programme", "windows and programs"),
    "computer": GroupInfo(False, "Maus und Tastatur", "mouse and keyboard"),
    "voice": GroupInfo(False, "Sprache", "voice"),
    "obs": GroupInfo(False, "OBS", "OBS"),
}

RiskName = Literal["read", "low", "medium", "high"]


class NodeManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    groups: dict[str, bool] = Field(
        default_factory=lambda: {k: v.default for k, v in GROUPS.items()}
    )
    #: Highest risk this node accepts at all. `critical` is not a choice.
    max_risk: RiskName = "high"
    #: A node without a screen cannot ask locally; high risk is then refused unless this was set
    #: here, on the device (spec C §1.6 step 5, a documented residual risk).
    trust_core_for_high: bool = False
    #: The node's own budget, independent of the core's: calls with a side effect, and high-risk
    #: calls, per hour (spec C §1.6 step 6).
    max_calls_per_hour: int = Field(default=60, ge=0, le=1000)
    max_high_per_hour: int = Field(default=5, ge=0, le=100)
    #: The folders `file.*` may open on this device (group `files`); none by default. Absolute,
    #: local paths only, added on the device (`nox node allow files --folder <path>`).
    folders: list[str] = Field(default_factory=list, max_length=16)
    #: Games of this device on top of the core's default list (`nox node games add`): while one
    #: runs or is in front, Nox sends no input here. Adding only narrows; removing is confirmed
    #: like a widening (`nox.node.cli.games`).
    games: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("games")
    @classmethod
    def _game_names(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for raw in value:
            name = str(raw).strip()
            if not GAME_NAME.match(name):
                raise ValueError(f"not a program name: {name!r}")
            if name.lower() not in (o.lower() for o in out):
                out.append(name)
        return out

    @field_validator("folders")
    @classmethod
    def _local_folders(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for raw in value:
            text = str(raw).strip()
            if not text or len(text) > 400 or "\x00" in text:
                raise ValueError("a folder entry is empty or too long")
            if text.startswith(("\\\\", "//")) or not Path(text).is_absolute():
                raise ValueError(f"folders must be absolute local paths: {text!r}")
            if text not in out:
                out.append(text)
        return out

    @field_validator("groups")
    @classmethod
    def _known_groups(cls, value: dict[str, bool]) -> dict[str, bool]:
        unknown = set(value) - set(GROUPS)
        if unknown:
            raise ValueError(f"unknown capability groups: {', '.join(sorted(unknown))}")
        return {name: bool(value.get(name, GROUPS[name].default)) for name in GROUPS}

    def enabled(self) -> set[str]:
        return {name for name, on in self.groups.items() if on}

    def effective(self, narrowed: Iterable[str]) -> set[str]:
        """Enabled here minus what the core narrowed. Never more than `enabled()`."""
        return self.enabled() - set(narrowed)


def load_manifest(path: Path) -> NodeManifest:
    """The manifest, or the defaults written out when there is none yet. A broken file is an
    error, not a reason to fall back to defaults: the device decided, and Nox does not guess."""
    if not path.is_file():
        manifest = NodeManifest()
        save_manifest(path, manifest)
        return manifest
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("node.yaml must be a mapping")
    return NodeManifest.model_validate(data)


def save_manifest(path: Path, manifest: NodeManifest) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# What Nox may do on this device. Only this device can widen it:\n"
        "# edit this file or run `nox node allow <group>` / `nox node deny <group>`.\n"
    )
    body = yaml.safe_dump(manifest.model_dump(), sort_keys=False, allow_unicode=True)
    path.write_text(header + body, encoding="utf-8")


def manifest_hash(groups: Iterable[str], tools: Iterable[str]) -> str:
    canonical = json.dumps(
        {"groups": sorted(groups), "tools": sorted(tools)}, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
