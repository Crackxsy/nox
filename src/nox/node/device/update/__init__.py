"""A node updates itself - only from a signed release, never from its core.

About once a day the node fetches `release-manifest.json` and its signature from the project's
latest GitHub release and decides, in this order. Any "no" stops it and changes nothing:

1. **A release key is trusted** by the version that runs here (`nox.release.trusted_keys`).
   None yet: "release signing is not configured", no update at all.
2. **The signature** is valid Ed25519 by a trusted, unexpired key over the manifest's exact bytes
   (`nox.release.verify.verify_release`: unknown key, bad signature, expired key are refusals).
3. **The manifest** is well-formed and names this project.
4. **No downgrade, no replay:** its version is not below the running one nor below the highest
   version that ran here, and it was not created before the newest manifest this node has
   already seen (an old, validly signed manifest cannot be replayed to freeze the node). Equal
   version: up to date. A version that failed to start here is skipped for `BAD_FOR_DAYS`.
5. **Policy:** only the `stable` channel installs by itself, only a patch, only when the release
   changed no library (`deps_changed: false`), and only when the *paired core* accepts the
   protocol the *new* node speaks (`node_speaks` in the manifest, within the range the core sent
   at the last connection). Anything else waits for `nox node update` on the device - or, when
   the libraries changed, for the installer.
6. **The bundle** `nox-app-<tag>.zip` (from that tag's own release) is listed in the signed
   manifest, downloaded with a size cap and matches the listed size and **SHA-256** - hashed and
   unpacked from one open file.
7. **Its contents** (`nox.node.device.bundle`): only listed, regular, bounded files under `nox/` and
   `config/`, each with the SHA-256 from `BUNDLE.json`; the same version, the protocol the
   manifest names, and the `uv.lock` of the running code.
8. **A trial start:** the new code is imported with this Python, isolated, and reports its
   version.

Then one atomic replace of `state.json` (under a file lock shared with the launcher) makes the
new version `pending`, and the node restarts through its launcher (`installer/start_node.pyw`),
which checks the folder again against the signed manifest before it runs it. The new version
becomes `current` once it connected to its core *and* the core accepted its protocol; a start
that crashes `MAX_TRIALS` times, or a core that refuses its protocol, rolls it back.

The package is cut along the chain above:

* `fetch` - how the manifest, its signature and the bundle are fetched (HTTPS only, size-capped).
* `state` - `state.json`, shared with the launcher and written under a file lock, and the answer
  of a check (`UpdateCheck`).
* `updater` - `NodeUpdater`: the decisions in the order above, the trial start and the restart.
"""
