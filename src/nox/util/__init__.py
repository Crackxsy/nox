"""Small, dependency-free helpers shared across packages (async plumbing, process spawning).

Nothing in here knows about Nox domain concepts: if a helper needs the event bus, the config or
the security engine, it belongs in the package that owns that concept, not here.
"""
