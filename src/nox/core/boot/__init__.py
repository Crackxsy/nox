"""The boot sequence of the core process, one module per step.

`nox.app` is the composition root: it holds the components and calls these builders in order. Each
module here builds one area and returns it, so a step can be read, tested and changed without
scrolling past the twelve that surround it:

* `persistence` - the database, its corruption handling, and the conversation store on top of it
* `ai` - the model providers, every one of them built through the egress guard, and the router
* `workers` - spawning, tracking and stopping the voice worker and its siblings
* `extensions` - importing and installing the release extensions
"""

from __future__ import annotations
