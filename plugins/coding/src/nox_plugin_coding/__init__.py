"""Coding assistant plugin package (EPIC-14). Entry point: `create(api)`."""

from __future__ import annotations

from .plugin import CodingPlugin, create

__all__ = ["CodingPlugin", "create"]
