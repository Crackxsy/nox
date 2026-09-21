"""Rocket League Stage 1 core-side package.

Pure-Python replay parsing (`replay_parser`), the OneDrive-redirected Documents special-folder
resolver (`paths`), typed DB access for `rl_matches`/`rl_events`/`rl_replays` (`nox.data.rl_repos`)
and the core-side services wired by `install(core)` - the `rl` plugin worker (`plugins/rl`) never
touches SQLite directly (Plugin API has no DB access), it only emits events this package persists.
"""

from __future__ import annotations
