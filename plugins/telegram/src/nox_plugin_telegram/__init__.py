"""Telegram companion plugin package (EPIC-17, Spec v0.8). Entry point: `create(api)`."""

from __future__ import annotations

from .plugin import TelegramPlugin, create

__all__ = ["TelegramPlugin", "create"]
