"""Nox as an MCP server for Claude Code: a stdio bridge process and the core-side gate.

`bridge` runs as the MCP server Claude Code starts (`python -m nox.mcp.bridge`); it speaks MCP on
stdio and forwards every request over the loopback hub, under the IPC role `mcp`, to `gate` in the
core, where `ToolLoop` -> `ToolExecutor` -> `PermissionEngine` -> audit decide what happens.
"""
