"""Telegram companion plugin package. Entry point: `create(api)`."""

from __future__ import annotations

from .plugin import TelegramPlugin, create

__all__ = ["TelegramPlugin", "create"]
