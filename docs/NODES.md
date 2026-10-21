# Nodes: Nox's hands on your other computers

*Deutsch: [NODES.de.md](NODES.de.md)*

A household has **one** Nox - the *core*, ideally on a Raspberry Pi. Every other PC or laptop can
run a small **`nox node`**: it connects to the core and lets Nox do things *on that device* - and
only the things you allowed there.

> Status: wave C is being built. This release has pairing, the connection, the device's own
> allow-list and audit trail, and two tools: `system.info` (read system values) and
> `notify.show` (show a notice). Commands, files, the screen and the rest follow.

## Installing a node

| Device | How | Starts with |
| --- | --- | --- |
| Windows | The Nox installer, mode **"Connect to a Nox in the home network (only this device)"** (silent: `Nox-Setup-<version>.exe /MODE=node [/PAIR=<link>]`). No dashboards; the optional sandbox rules are offered as in the core install. | A per-user logon task "Nox Node": runs as you, *least privilege*, never a service, never elevated. Start menu "Nox Node" starts it now. |
| Linux desktop | `curl -fsSL https://github.com/Crackxsy/nox/releases/latest/download/install-linux.sh \| bash -s -- --node` - as yourself, **without** sudo. Needs `git curl tar python3`. | A systemd **user** unit `nox-node.service`. Command: `nox-node ...` (in `~/.local/bin`). |
| Linux without a screen (a Pi) | the same with `sudo bash -s -- --node --headless` | A hardened system unit for its own system user `nox-node`; data in `/var/lib/nox-node`. Command: `sudo nox-node ...`. |
| macOS (**beta**) | `bash scripts/install-macos.sh --node` in a checkout of this repository | A LaunchAgent at login, as you. Release signatures are not checked by this script yet (it refuses tagged releases once signing is set up). |

`--pair '<link>'` pairs right away; running the installer again updates; `--uninstall` removes the
program and the autostart (the node's identity and audit trail stay; remove the device in Nox
too). On Windows, `nox-node.cmd` in the install folder is the `nox node` command.

## Pairing a node

1. On the core machine, run `nox pair --node` in a terminal (the dashboard's Devices page gets
   nodes in a later release). Network access must be on (`ipc.lan.enabled`, see
   [NETWORK_ACCESS.md](NETWORK_ACCESS.md)). With a PIN set, it asks for it.
2. On the new PC (Nox installed): run the command shown, for example

   ```text
   nox node pair "nox-node://pair?h=192.168.1.20&p=47843&c=ABCD2345&fp=…" --name Laptop
   ```

   The link carries the fingerprint of your Nox's certificate authority. The node fetches the
   authority, compares it with that fingerprint and only then sends the code - a device in your
   network that pretends to be Nox never sees it. The code is valid for five minutes, once.
3. Start the node: `nox node run`. It connects, reconnects on its own (1 s, then up to 60 s apart)
   and never acts while it is not connected.

`nox node status` shows whether it is paired and connected and what Nox may do here.

Names: only `A-Z a-z 0-9 ä ö ü ß`, space and `- _ .`, up to 40 characters. A name Nox already
has (also as "LAPTOP" or "Kueche" for "Küche") is refused - pick another with `--name`. Nox always
shows a device with a short id: "Laptop · 3f2a".

## Which device does it?

Say it: "Wie viel Akku hat mein Laptop?" runs `system.info` on the laptop, "Zeig mir auf dem
Laptop einen Hinweis: ..." shows it there. Every turn tells the model which devices exist and
whether they are online ("Geräte: dieser Core (core), Arbeits-PC (online), Laptop (offline seit
14:02)"). When you do not say:

1. the default in `devices.defaults` (`core`, `origin` - the PC whose dashboard you are using,
   once a node is linked to it with `node.link` - or a device name),
2. otherwise the only device that can do it,
3. otherwise a question that only reads may stay on the core; one that changes something is
   **never guessed**: Nox asks which device you mean.

An offline device gets an honest "offline since 14:02" - Nox never uses another device instead
and never queues the request. A remembered "allow" for the laptop covers only the laptop.

## What Nox may do on this device

The file `node.yaml` in the node folder decides, and **only this device can widen it**:

```text
nox node allow shell     # let Nox run commands here (once that tool exists); asks "yes?"
nox node deny notify     # no more notices on this device - at once, also while it runs
```

Default: `system` (read system values), `notify` (show notices) and `media` (control music) are
on; everything else - files, commands, programs, screen, camera, browser, mouse and keyboard - is
off. The core can switch groups off for a while, never on. `allow` works only in an
interactive terminal with a typed confirmation; editing `node.yaml` by hand can only take
something away - anything it adds is ignored until you confirm it with `nox node allow`. `max_risk` lowers what the node accepts.

**High risk is asked here, on this device**: a system dialog shows what Nox wants to do, with every
detail, for 60 s - no answer means no, and a "yes" within the first 1.5 seconds counts as no.
A node without a screen refuses high risk, unless a person confirmed it on Nox itself and you
allowed that on the device: `nox node allow trust_core_for_high`. The node also keeps
its own budget: 60 actions and 5 high-risk ones per hour (`max_calls_per_hour`,
`max_high_per_hour` in `node.yaml`).

## Stopping

The stop button of Nox stops every node within a second; running actions are aborted. On the
device itself: `nox node stop` stops it there and Nox everywhere; only `nox node resume` on that
device lifts it. If a node hears nothing from Nox for 3 seconds while it is doing something, it
aborts that and waits.

On a desktop with the tray (Windows; Linux where PySide6 is installed) the icon shows the state -
connected, "Nicht verbunden mit Nox", stopped - and its menu has **Not-Aus hier** (also
`Ctrl+Alt+Shift+K` anywhere, where a global key hook is available), **Hier fortsetzen** and **Was
Nox hier darf**. The tray only writes the same stop file as `nox node stop`. Notices from a node
installed in node mode on Windows carry Nox's own name (AppUserModelID `Nox.Node`), not
"Windows PowerShell".

Hard prohibitions (anti-cheat, game input, stream key, ...) are checked on the node itself as well.

## Updates

The node updates itself **only from the project's signed GitHub release** - the core can say that
an update exists, it never sends code. About once a day the node fetches `release-manifest.json`
and its signature and checks, in this order (any "no" stops it; nothing is changed):

1. a release key is trusted by the installed version - until the owner has created one, the
   answer is "release signing is not configured" and there is no update at all;
2. the Ed25519 signature is valid, by a trusted key that has not expired;
3. the manifest is well-formed and names this project;
4. no downgrade: not below the running version nor below the highest version that ran here;
   a version that failed to start here before is skipped;
5. a **patch** that still speaks this node's protocol installs by itself; a minor or major update
   waits for `nox node update` on the device (a notice says so);
6. the bundle `nox-app-<tag>.zip` is listed in the signed manifest and matches its size and
   SHA-256;
7. it holds only Nox's code and configuration, built for the library versions installed here
   (otherwise: "run the installer");
8. a trial start imports the new code with this Python.

Then one atomic file switch (`app/state.json`), and the launcher starts the new version. It counts
as working once it connected or ran two minutes; a start that fails before that rolls back to the
previous version, and that version is not tried again. `nox node update --check` only looks;
exit status 3 means release signing is not configured yet.

The certificate (30 days) renews itself over the connection from day 20, for the same key; a node
that was off for longer than 30 days pairs again.

## What the node keeps, and where

| File | What |
| --- | --- |
| `node.key` | The node's private key, made on this device and never sent anywhere. Encrypted when the OS has a keyring (Windows Credential Manager, macOS Keychain, a desktop Linux secret service); on a headless Linux node it is protected by its file mode (0600) only. |
| `node.crt`, `ca.crt`, `node.json` | Its certificate (30 days), the pinned authority, the core's address |
| `node.yaml` | What Nox may do here |
| `audit.db` | Every call, allowed or refused, hash-chained: `nox node log` |
| `app/` | Versions the node installed itself (`releases/<version>`) and `state.json`, which one runs |
| `update.json` | The last update check, shown by `nox node status` and the tray |

Folder: `%LOCALAPPDATA%\Nox\node` (Windows), `~/.local/share/nox/node` (Linux),
`~/Library/Application Support/Nox/node` (macOS); `NOX_NODE_HOME` overrides it. The node runs as
the logged-in user, never as administrator or root.

## Removing a node

On the core machine: `nox unpair --node Laptop`. The connection closes at once, the certificate never
works again, and the node deletes its key and certificate ("Removed from Nox"). A node removed while
it was off is refused when it comes back.

## Security

See [SECURITY.md](SECURITY.md#nodes-nox-node). Known limits in this release: the node finds the
core only at the address in the pairing link (discovery comes with wave D). The Windows logon
task is checked in CI on an administrator account; that a standard account can register it is the
documented Task Scheduler behaviour for a task that runs as that account, not yet a tested fact.
The macOS installer is a beta.
