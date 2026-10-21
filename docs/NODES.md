# Nodes: Nox's hands on your other computers

*Deutsch: [NODES.de.md](NODES.de.md)*

A household has **one** Nox - the *core*, ideally on a Raspberry Pi. Every other PC or laptop can
run a small **`nox node`**: it connects to the core and lets Nox do things *on that device* - and
only the things you allowed there.

> Status: wave C is being built. This release has pairing, the connection, the device's own
> allow-list and audit trail, and two tools: `system.info` (read system values) and
> `notify.show` (show a notice). Commands, files, the screen and the rest follow.

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
nox node allow shell     # let Nox run commands here (once that tool exists)
nox node deny notify     # no more notices on this device
```

Default: `system` (read system values), `notify` (show notices) and `media` (control music) are
on; everything else - files, commands, programs, screen, camera, browser, mouse and keyboard - is
off. The core can switch groups off for a while, never on. `max_risk` lowers what the node accepts;
in this release nothing above "low" runs on a node at all, whatever the core says.

Hard prohibitions (anti-cheat, game input, stream key, ...) are checked on the node itself as well.

## What the node keeps, and where

| File | What |
| --- | --- |
| `node.key` | The node's private key, made on this device and never sent anywhere. Encrypted when the OS has a keyring (Windows Credential Manager, macOS Keychain, a desktop Linux secret service); on a headless Linux node it is protected by its file mode (0600) only. |
| `node.crt`, `ca.crt`, `node.json` | Its certificate (30 days), the pinned authority, the core's address |
| `node.yaml` | What Nox may do here |
| `audit.db` | Every call, allowed or refused, hash-chained: `nox node log` |

Folder: `%LOCALAPPDATA%\Nox\node` (Windows), `~/.local/share/nox/node` (Linux),
`~/Library/Application Support/Nox/node` (macOS); `NOX_NODE_HOME` overrides it. The node runs as
the logged-in user, never as administrator or root.

## Removing a node

On the core machine: `nox unpair --node Laptop`. The connection closes at once, the certificate never
works again, and the node deletes its key and certificate ("Removed from Nox"). A node removed while
it was off is refused when it comes back.

## Security

See [SECURITY.md](SECURITY.md#nodes-nox-node). Known limits in this release: the node finds the
core only at the address in the pairing link (discovery comes with wave D); a node's certificate
is not renewed yet, so pair again after 30 days.
