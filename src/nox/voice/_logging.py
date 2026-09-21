"""Logger lookup for the voice package: uses nox.core.logging when present, structlog otherwise.

nox.core.logging is owned by the core agent and may not exist yet in this checkout; the voice
package must not depend on its import timing (the project standards: structured logging via
structlog).
"""

from __future__ import annotations

from typing import Any


def get_logger(name: str) -> Any:
    try:
        from nox.core.logging import get_logger as core_get_logger
    except ImportError:
        import structlog

        return structlog.get_logger(name)
    return core_get_logger(name)
