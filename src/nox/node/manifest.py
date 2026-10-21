"""What this device lets Nox do: the capability manifest `node.yaml`, kept on the device itself.

Only the device widens it - by editing the file or with `nox node allow <group>` in a terminal of
the logged-in user. The core can narrow what a node offers for as long as the connection lasts
(`nox/policy`), never widen it: `effective()` is always a subset of what is enabled here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["GROUPS", "GroupInfo", "NodeManifest", "load_manifest", "manifest_hash", "save_manifest"]


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
