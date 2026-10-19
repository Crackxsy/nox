"""Per-call permission scopes for plugin tools whose risk depends on their input.

A plugin worker declares a tool with one risk in its manifest; the worker cannot lower it. For a
generic tool - `obs.request` sends any obs-websocket request - one risk is either too strict to
be useful or too loose to be safe, so the core classifies each call itself (`CallScope`). The
classifier is core code, keyed by plugin and tool, never something a plugin supplies: a plugin
deciding the risk of its own calls would be a plugin granting itself permissions.

A tool without an entry here keeps its manifest risk for every call, which is the safe default.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from nox.stream.obs_requests import scope_for_tool as obs_request_scope
from nox.tools.registry import Scope

ScopeFactory = Callable[[Mapping[str, Any]], Scope]

#: (plugin id, tool name) -> factory taking the plugin's configuration.
PLUGIN_TOOL_SCOPES: Mapping[tuple[str, str], ScopeFactory] = {
    ("obs", "obs.request"): obs_request_scope,
}


def scope_for(plugin_id: str, tool: str, config: Mapping[str, Any]) -> Scope | None:
    """The core's classifier for this plugin tool, or None to keep the manifest risk."""
    factory = PLUGIN_TOOL_SCOPES.get((plugin_id, tool))
    return factory(config) if factory is not None else None
