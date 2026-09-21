"""IPC request handlers that belong to the core itself.

An extension registers its own handlers from its `install(core)`; these are the ones that exist in
every Nox, with or without extensions.
"""

from __future__ import annotations
