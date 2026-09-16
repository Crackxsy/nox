"""Personality & Proactivity runtime (Spec v0.5 EPIC-19, ST-19-02..08).

Wired into the core by `nox.proactive.install.install(core)` (the integrator adds one call site in
`app.py`; this package never imports or edits `app.py` itself, per the task's shared-file rule).
"""

from __future__ import annotations
