"""Nox-OS, the flashable Raspberry Pi image (os/README.md): what the core does only there.

Remote maintenance (SSH), switched on and off from the dashboard through the image's root helper
(`nox.noxos.install`), and the claim - how an unclaimed device gets its owner (`nox.noxos.claim`).
On any other system the requests answer `available: false` and there is nothing to claim.
"""
