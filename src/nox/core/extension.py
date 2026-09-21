"""The contract between the core and a release extension.

An extension is a module `nox.<name>.install` that exposes `install(core)`. It is given the fully
built core and wires itself onto it: bus subscriptions, IPC handlers, tools, background tasks. It
may return a runtime object; if that object has a `stop()`, the core calls it during shutdown,
before the core's own components go down, and gives it a short budget.

Returning `None` is a valid answer and means "nothing to shut down". Nothing else is required, and
in particular an extension is never asked to unregister what it registered - the core process ends
with it.

Two rules make the contract hold in practice:

* `install(core)` runs **on the event loop**, so it must not block. The expensive half is usually
  the import - a vision extension pulls in an image library and takes seconds on a cold start -
  and the core already does that import in a worker thread.
* A failing extension is reported as unavailable and never aborts the boot. Nox starts without it
  and says so, rather than pretending the capability is there.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["Extension", "ExtensionRuntime", "StoppableRuntime"]


@runtime_checkable
class StoppableRuntime(Protocol):
    """What the core looks for on an extension's return value."""

    def stop(self) -> Any:
        """Shut the extension down. May be a coroutine function; the core awaits either form."""
        ...


#: What `install(core)` may return: a runtime the core can stop, or nothing.
ExtensionRuntime = StoppableRuntime | None


class Extension(Protocol):
    """The module-level shape of `nox.<name>.install`."""

    def install(self, core: Any) -> ExtensionRuntime: ...
