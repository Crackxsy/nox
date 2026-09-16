"""Hard prohibitions: the immutable deny list (security/model.py, config/defaults.yaml, ADR-007).

This is the only module-level constant allowed by the engineering brief. Configuration layers may
only add entries; `nox.core.config` rejects any layer that removes one of these.
"""

from __future__ import annotations

HARD_PROHIBITIONS: frozenset[str] = frozenset(
    {
        "game.input.send",
        "game.memory.read",
        "game.process.inject",
        "anticheat.bypass",
        "stream.key.read",
        "stream.stop",
        "recording.delete",
        "security.core.modify_without_pin",
        "permission.self_elevate",
    }
)
