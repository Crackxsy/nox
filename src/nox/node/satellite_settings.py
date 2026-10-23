"""`satellites.yaml` in the node's folder: how this device's satellites work (C9).

How it listens and speaks (`voice:`), which port its OBS listens on and the names of its privacy
scene and scene set (`obs:`), the overlay relay's port (`overlay:`). Never *whether* - that is the
capability groups `voice` and `obs` in `node.yaml`, which only this device widens. Nothing secret
is in this file: the OBS password is in the keyring (`nox node obs password`).

A file that does not parse is reported and the defaults are used: these are preferences, not
permissions.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nox.core.logging import get_logger
from nox.node.obs import ObsSettings
from nox.node.overlay import OverlaySettings
from nox.node.voice import VoiceSettings

log = get_logger(__name__)

__all__ = ["SATELLITES_FILE", "SatelliteSettings", "SettingsFile"]

SATELLITES_FILE = "satellites.yaml"
_HEADER = (
    "# How Nox's satellites work on this device (voice, OBS, the overlay relay).\n"
    "# Whether they may run at all is `nox node allow voice|obs` (node.yaml), not this file.\n"
)


class SatelliteSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    obs: ObsSettings = Field(default_factory=ObsSettings)
    overlay: OverlaySettings = Field(default_factory=OverlaySettings)


class SettingsFile:
    """The settings, read again whenever the file changed (a change applies without a restart)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._mtime: float | None = None
        self._value = SatelliteSettings()
        self.error = ""

    def read(self) -> SatelliteSettings:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return self._value
        if mtime == self._mtime:
            return self._value
        self._mtime = mtime
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            self._value = SatelliteSettings.model_validate(data)
            self.error = ""
        except (OSError, ValueError, ValidationError, yaml.YAMLError) as exc:
            self.error = f"{type(exc).__name__}"
            log.warning("node.satellites_unreadable", error=self.error)
            self._value = SatelliteSettings()
        return self._value

    def write(self, settings: SatelliteSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = yaml.safe_dump(settings.model_dump(), sort_keys=False, allow_unicode=True)
        self.path.write_text(_HEADER + body, encoding="utf-8", newline="\n")
        self._mtime = None
