"""SQLite data layer (ADR-006): `Database` wrapper, plain-SQL migrations and typed repositories."""

from nox.data.db import Database

__all__ = ["Database"]
