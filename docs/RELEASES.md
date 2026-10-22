# Signed releases

Every Nox release on GitHub carries a **release manifest** - `release-manifest.json`, the list of
the release's files with their sizes and SHA-256 and the git commit its tag points to - and an
**Ed25519 signature** of it, `release-manifest.json.sig`. The signature is made with the project's
release key, whose private half only the owner holds (and the GitHub environment that signs
releases). Installations check it before they use anything they downloaded and **refuse** a
release whose manifest is missing, unsigned, signed by a key they do not trust, or does not match
what they downloaded.

A checksum next to a file only catches a damaged download: whoever can replace the file can
replace the checksum too. A signature can only be made with the private key, so changing a release
afterwards - or publishing a fake one - is detected even by someone who has write access to the
repository but not the key.

> **Status: not configured yet.** The trust list in this repository is empty until the owner has
> created the release key ([Setting up the release key](#setting-up-the-release-key-owner)). Until
> then every verifier says "release signing is not configured" - it never claims to have verified
> anything - releases are checked by checksum only, exactly as before, and the release workflow
> marks each release run as failed with "Release not signed".

## What is checked, and where

| Where | What it does |
| --- | --- |
| `scripts/install-linux.sh` (first install and every update) | For a tagged release: downloads the manifest and its signature, verifies them with `openssl` (version 3 or later) against the keys built into the script, checks that the manifest names exactly the commit the tag points to and is not older than the newest release this machine already accepted, then requires the dashboard package to match the size and SHA-256 in the manifest. Any doubt stops the install before anything is built; nothing is changed. |
| `nox update` on Linux / Raspberry Pi | Before it runs the new version's installer, the **running** version checks the new release with its own `nox release verify` - so trust always comes from what is already installed, never from what is being installed. Then the new installer checks again with its own key list. |
| `nox release verify <manifest>` | The same check by hand, on any system Nox runs on, optionally with `--file <downloaded file>` (repeatable) to compare files against the manifest. Exit status: 0 verified, 1 refused, 3 not configured. |
| `nox release keys` | Lists the keys this version trusts and whether each is still valid. |
| Node self-update (`nox.node.update`, daily, and `nox node update`) | The same library and manifest: the signature against the keys of the node version already installed (none trusted: no update at all), no downgrade below the highest version that ran there, then `nox-app-<tag>.zip` must match the manifest's size and SHA-256 before it is unpacked, trial-started and switched to atomically; a version that does not start is rolled back. Order and details: [NODES.md](NODES.md#updates). |
| Nox-OS images (later waves) | Use the same library (`nox.release`) and the same manifest. |
| Windows installer | Not yet a release asset built by CI, so it is not in the manifest yet. Once it is, it is listed like the other files and can be checked with `nox release verify --file Nox-Setup-<version>.exe`. The Inno Setup installer cannot check an Ed25519 signature by itself; on Windows, Authenticode code signing is the matching control, and it is not configured yet either (`installer/README.md`). |

Branches and single commits (`nox update develop`, a CI install from a checkout) have no manifest.
The installer says so ("development version without a signed manifest") and installs them only
because the owner asked for exactly that version by name.

### Rollback protection

After every successful install of a verified release the installer records its version in
`/opt/nox/state/release-floor`. A release older than that is refused afterwards, even with a valid
signature - otherwise an attacker who controls the network could offer an old release with a known
hole as "the update". To go back on purpose:

```bash
nox rollback                              # one step back, with the data from before the update
nox update v0.7.1 --allow-downgrade       # an older release, deliberately
```

`nox rollback` uses the copy that is still on the machine and is not affected by the floor.

### Checking the installer before running it

The first install starts with a downloaded script that cannot vouch for itself. To check it by
hand, with nothing but `openssl`, `python3` and the public key printed in
`src/nox/release/trusted_keys.py`:

```bash
base=https://github.com/Crackxsy/nox/releases/latest/download
curl -fsSLO "$base/install-linux.sh" -O "$base/release-manifest.json" -O "$base/release-manifest.json.sig"
# The public key: the public_key value from src/nox/release/trusted_keys.py, between PEM markers.
printf -- '-----BEGIN PUBLIC KEY-----\n%s\n-----END PUBLIC KEY-----\n' '<public_key value>' > release.pub
python3 -c 'import base64, json; open("sig.bin", "wb").write(base64.b64decode(json.load(open("release-manifest.json.sig"))["signature"]))'
openssl pkeyutl -verify -pubin -inkey release.pub -rawin -in release-manifest.json -sigfile sig.bin
grep -A2 '"install-linux.sh"' release-manifest.json   # size and SHA-256 listed for the script
sha256sum install-linux.sh                              # must be the same SHA-256
```

`Signature Verified Successfully` and an identical SHA-256 mean the script is the one the release
key signed. Or, on a machine that already runs Nox: `nox release verify release-manifest.json
--file install-linux.sh`.

## The manifest

```json
{
  "schema": "nox-release-manifest/1",
  "project": "Crackxsy/nox",
  "version": "0.7.0",
  "tag": "v0.7.0",
  "channel": "stable",
  "created": "2026-10-21T12:00:00Z",
  "source_commit": "<the 40-character commit the tag points to>",
  "node_protocol": {"min": 1, "max": 1},
  "node_speaks": 1,
  "deps_changed": false,
  "min_node_version": null,
  "next_keys": [{"key_id": "...", "public_key": "...", "not_after": "YYYY-MM-DD"}],
  "assets": [
    {"name": "install-linux.sh", "size": 51234, "sha256": "<64 hex>"},
    {"name": "nox-app-v0.7.0.zip", "size": 1234567, "sha256": "<64 hex>"},
    {"name": "nox-ui-v0.7.0.tar.gz", "size": 812345, "sha256": "<64 hex>"}
  ]
}
```

- The signature covers the file's exact bytes; nothing is re-serialised before checking.
- `channel` is `prerelease` exactly for versions like `0.7.0-rc.1`, which sort below `0.7.0`.
- `next_keys` records the trust list this release ships, so a rotation is announced inside a
  signed document. Trust itself comes from the list built into the installed version, never from
  a downloaded manifest.
- `node_protocol` is the node protocol range the release's core accepts (`NODE_PROTOCOL_RANGE`
  in `src/nox/node/protocol.py`), `node_speaks` the protocol its node speaks (`NODE_PROTOCOL`),
  and `deps_changed` whether `uv.lock` changed since the previous release tag - all three filled
  by the release workflow. A node installs a release by itself only when its *paired core*
  accepts `node_speaks` and `deps_changed` is `false` (a code-only patch); otherwise the device's
  owner confirms (`nox node update`) or the installer is needed. `min_node_version` stays `null`
  until a core drops support for old nodes.
- **Freshness:** a node refuses a manifest whose `created` is older than the newest one it has
  already seen (a replayed old release cannot hold it back silently) and warns after 30 days
  without a newer one (a network that withholds releases - a freeze - cannot be told from a quiet
  month, so it is said, not refused).
- `nox-app-<tag>.zip` is what a node updates itself from: Nox's code and configuration only
  (`scripts/build_app_bundle.py`, deterministic, standard library only), built in the manifest job
  from the clean checkout of the tag, with `BUNDLE.json` naming the library versions it needs.
- Fields a later version adds are ignored by older verifiers; the known fields are checked
  strictly.

The signature file:

```json
{"schema": "nox-release-signature/1", "algorithm": "ed25519", "key_id": "<16 hex>", "signature": "<base64, 64 bytes>"}
```

A **key id** is the first 16 hex characters of the SHA-256 of the raw 32-byte public key. Each
trusted key has a last valid day (`not_after`, UTC); after it, signatures by that key are refused.

## How a release is signed

`.github/workflows/release-assets.yml` runs when a release is published:

1. **build** - builds the dashboard bundle (third-party npm code; read-only token, no secrets).
2. **manifest** - runs in the GitHub environment `release-signing`, the only place the secret
   `NOX_RELEASE_SIGNING_KEY` is available. It checks out the tag and installs **only**
   `cryptography` and its two dependencies, pinned by version and SHA-256
   (`scripts/signing-requirements.txt`, copied from `uv.lock`; wheels only) into a folder of
   their own. The signer `scripts/sign_release.py` is self-contained - it imports nothing from
   Nox - and runs as `python -I -S` (no site-packages, no `.pth` files, no `PYTHON*` variables),
   so no other installed package can run code in the process that holds the key. The key reaches
   it on **stdin** only (`printf` builtin, removed from the environment with `env -u` before Python
   starts): never on a command line, never inherited. It refuses a key that the tagged release
   does not itself trust or that has expired, signs, and checks the new signature before writing
   anything. All actions in the workflow are pinned to commit SHAs.
3. **upload** - checks that the manifest lists exactly the files being uploaded, then attaches
   them. If the manifest is unsigned (no secret), it attaches the checksummed files as before but
   **not** the manifest, and fails the run with "Release not signed".

## Setting up the release key (owner)

Only the owner does this; nobody else - no contributor, no automation - creates or handles the
release key. Use a machine you trust, ideally offline (a live USB system without network, for
example). Keep `umask 077` so the private key is never readable by others.

```bash
umask 077
# 1. The CI signing key and a second, offline-only recovery key (see "If the key leaks").
openssl genpkey -algorithm ed25519 -out nox-release-ci.key
openssl genpkey -algorithm ed25519 -out nox-release-recovery.key
openssl pkey -in nox-release-ci.key -pubout -out nox-release-ci.pub
openssl pkey -in nox-release-recovery.key -pubout -out nox-release-recovery.pub

# 2. Encrypted copies for offline storage (two USB sticks in different places, or a password
#    manager). Choose a long passphrase; it is asked for twice.
openssl pkcs8 -topk8 -v2 aes-256-cbc -in nox-release-ci.key -out nox-release-ci.key.enc
openssl pkcs8 -topk8 -v2 aes-256-cbc -in nox-release-recovery.key -out nox-release-recovery.key.enc
```

3. **Trust them.** On your development machine (public keys only - `describe-key` refuses a
   private key):

   ```bash
   nox release describe-key nox-release-ci.pub --not-after 2028-10-31
   nox release describe-key nox-release-recovery.pub --not-after 2031-10-31
   ```

   Each prints one line for `TRUSTED_KEYS` in `src/nox/release/trusted_keys.py` and one for
   `TRUSTED_RELEASE_KEYS` in `scripts/install-linux.sh`. Paste both (the second line also into
   `TRUSTED_RELEASE_KEYS` in `scripts/install-macos.sh`), open a pull request, merge it.
   (Without Nox: the key id is `openssl pkey -pubin -in nox-release-ci.pub -outform DER | tail -c 32
   | sha256sum | cut -c1-16`.)

4. **The signing environment**, with yourself as required reviewer and only `v*` tags allowed:

   ```bash
   printf '{"reviewers":[{"type":"User","id":%s}],"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}' \
     "$(gh api user -q .id)" | gh api -X PUT repos/Crackxsy/nox/environments/release-signing --input -
   gh api -X POST repos/Crackxsy/nox/environments/release-signing/deployment-branch-policies \
     -f name='v*' -f type=tag
   ```

5. **The secret** - the CI key only, never the recovery key. Read from the file, so it never
   appears on a command line or in your shell history:

   ```bash
   gh secret set NOX_RELEASE_SIGNING_KEY --env release-signing --repo Crackxsy/nox < nox-release-ci.key
   ```

6. **Remove the unencrypted files** from every machine that is not offline storage:
   `shred -u nox-release-ci.key nox-release-recovery.key` (keep the `.pub` and `.key.enc` files).

7. **The first signed release:** publish a release as usual, approve the "release-signing"
   deployment when GitHub asks, and check that `release-manifest.json` and
   `release-manifest.json.sig` are attached. Verify it by hand once ([Checking the installer
   before running it](#checking-the-installer-before-running-it)).

Installations that run a version from **before** the key was added trust no key yet: they install
the first signed release by checksum, as before, and from then on verify every update.

## Rotation

Rotate before the CI key's `not_after`, at the latest six months ahead, so installations that
update rarely still get the release that introduces the new key:

1. Create a new CI key offline (steps 1-2 above, a new file name).
2. Pull request: **add** the new key to both lists; keep the old one. Merge, release `vN` - still
   signed by the old key, but shipping trust in both.
3. Replace the secret with the new key (step 5) and delete the old key's unencrypted copies.
4. Release `vN+1` - signed by the new key. Installations on `vN` accept it.
5. Some releases later, remove the old key from both lists.

An installation that skips `vN` entirely does not know the new key and refuses `vN+1`; it is told
so, and `nox update vN` followed by `nox update` brings it across. The CI check in the release
workflow refuses to sign with a key the release itself does not list, so a rotation cannot
accidentally ship a release its own version would refuse.

## If the key leaks

The leaked key stays trusted by every installation until it updates to a release that no longer
lists it, or until its `not_after` day - that is the window to close quickly.

1. **Cut it off at once:** `gh secret delete NOX_RELEASE_SIGNING_KEY --env release-signing --repo
   Crackxsy/nox`, and look at the environment's deployment history for runs you did not approve.
2. **New CI key** offline (steps 1-2 above).
3. **Rotation release, signed with the recovery key** - not with the leaked one, whose signatures
   are worthless now. Pull request: remove the leaked key, add the new CI key. Tag the release; the
   workflow attaches the checksummed files and fails with "Release not signed" (the secret is
   gone). Download the two files it attached, check them, and sign on the offline machine, in a
   checkout of that tag:

   ```bash
   gh release download vX.Y.Z -p 'nox-ui-vX.Y.Z.tar.gz' -p install-linux.sh
   python3 -m pip install --require-hashes --no-deps --only-binary :all: --target signer-lib \
     -r scripts/signing-requirements.txt
   openssl pkey -in nox-release-recovery.key.enc | \
     python3 -I -S scripts/sign_release.py --key-stdin --lib signer-lib --tag vX.Y.Z \
       --commit "$(git rev-parse 'vX.Y.Z^{commit}')" --out signed/ nox-ui-vX.Y.Z.tar.gz install-linux.sh
   gh release upload vX.Y.Z signed/release-manifest.json signed/release-manifest.json.sig
   ```
4. **Set the new CI key** as the secret (step 5).
5. **Tell users:** a GitHub security advisory naming the leaked key id and the time window, the
   rotation release, and "update now with `nox update`". Delete any release or file that was
   signed with the leaked key after the leak and was not made by you.

Without a recovery key, step 3 has to be signed with the leaked key itself (indistinguishable from
the attacker's signatures) or installations have to be updated by hand from a verified checkout -
which is why the recovery key is created on day one and never leaves offline storage.

## What signatures do not cover

- **The very first download** of `install-linux.sh` is trusted as it is, unless it is checked by
  hand as shown above.
- **A version that trusts no key** (older than this feature, or before the owner set it up) accepts
  the next release by checksum only.
- **The owner's GitHub account.** Whoever controls it can change the trust list in the code and
  approve a signing run. Protect it with a hardware security key; the required reviewer on the
  signing environment means a stolen *token* alone cannot sign.
- **Third-party packages** a release installs come from PyPI and are pinned by hash in `uv.lock`,
  which is part of the signed commit - their integrity follows from the commit, their
  trustworthiness does not.
