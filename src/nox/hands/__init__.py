"""Nox's hands: the tools that reach the machine and the world (shell, packages, web, ...).

Everything here that runs code or touches the network does so through `nox.sandbox` or the core's
`EgressGuard`; nothing in this package opens a socket or starts a process on its own.
"""
