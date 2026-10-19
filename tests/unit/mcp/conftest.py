"""MCP client tests reuse the tool-pipeline fixtures (real permission engine, audit, kill
switch)."""

from tests.unit.tools.conftest import (  # noqa: F401 - pytest fixtures, used by name
    FakeKillSwitch,
    FakePrivacy,
    audit,
    bus,
    clock,
    conn,
    engine,
    killswitch,
    privacy,
    profiles,
    registry,
    test_profile,
)
