"""The one list of game programs Nox watches out of the box.

The game sensor (`sensors.game.process_names`), the Rocket League detector
(`rl.detection.process_names`) and the Rocket League plugin's own defaults all start from this
tuple. `config/defaults.yaml` and `plugins/rl/manifest.yaml` are plain data and cannot import it,
so `tests/unit/core/test_game_list_single_source.py` holds them to it.
"""

from __future__ import annotations

__all__ = ["DEFAULT_GAME_PROCESSES"]

#: Executable names, compared without case. Rocket League is the one game Nox ships knowledge of.
DEFAULT_GAME_PROCESSES: tuple[str, ...] = ("RocketLeague.exe",)
