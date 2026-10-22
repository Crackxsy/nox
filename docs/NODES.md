# Nodes: Nox's hands on your other computers

*Deutsch: [NODES.de.md](NODES.de.md)*

A household has **one** Nox - the *core*, ideally on a Raspberry Pi. Every other PC or laptop can
run a small **`nox node`**: it connects to the core and lets Nox do things *on that device* - and
only the things you allowed there.

> Status: wave C is being built. This release has pairing, the connection, the device's own
> allow-list and audit trail, `system.info` (read system values), `notify.show` (show a notice)
> and the hands: `file.list/read/write` in folders you allow and `shell.run` in the device's own
> sandbox. Installing programs (`package.*`) is not yet available on nodes. The screen and the
> rest follow.

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
nox node allow shell     # let Nox run commands here, in this device's sandbox; asks "yes?"
nox node allow files --folder "C:\Users\Alex\Documents\Nox"   # one folder Nox may open
nox node deny files --folder "C:\Users\Alex\Documents\Nox"    # take that folder away again
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

## Files and commands on a node

- **Files** (`files`): Nox opens only paths inside the folders you allowed with `--folder` (none
  by default; never a whole drive, your home folder, a system or app-data folder, or a folder
  with keys or passwords), and decides that on the real file at the moment it opens it - a link
  or junction that leads elsewhere, `..`, `~`, a short name like `PROGRA~1`, a network path
  (`\\server\share`) or a device path is refused, and so is anything in the node's own folder,
  the keyring, `~/.ssh` and similar key stores, and browser profiles.
- **Writing** creates only plain data files: `.txt .md .csv .tsv .log .json .yaml .yml .xml
  .html .htm .pdf`, images (`.png .jpg .jpeg .gif .webp`), audio and video (`.mp3 .wav .ogg .flac
  .mp4 .webm .mkv`), office files (`.odt .ods .docx .xlsx .pptx`) and `.ics .vcf .srt`. Never a
  hidden file or anything in a hidden folder (`.git`, `.vscode`, `.bashrc`, ...), never a file
  without an extension, a script, a program or a configuration file tools act on
  (`settings.json`, `tasks.json`, `package.json`, `docker-compose.yml`, anything in `workflows`),
  never an executable file or one with a second name (hard link). A new file is medium risk (asked
  on Nox); **replacing or appending is high risk** and is asked on this device every time. The
  question shows the file, the mode, size, line count and a SHA-256, and the content - in full
  when it is short, otherwise its first and last 200 characters with "content shortened - N
  characters not shown". A write is never remembered.
- **Commands** (`shell`) run only in this device's own sandbox (AppContainer on Windows,
  bubblewrap on Linux), which cannot see the node's folder - on Linux the node refuses commands
  when its folder lies under a system folder the sandbox shows. Without such a sandbox - macOS
  for now, Linux without `bwrap` - commands are refused, never run without one. High risk: asked
  on this device, or on Nox for a node without a screen.
- **Programs** (`packages`): not yet available on nodes - a package manager needs rights the
  sandbox does not give.

## Stopping

The stop button of Nox stops every node within a second; running actions are aborted. On the
device itself: `nox node stop` stops it there and Nox everywhere; only `nox node resume` on that
device lifts it. If a node hears nothing from Nox for 3 seconds while it is doing something, it
aborts that and waits.

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
