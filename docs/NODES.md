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

## Installing a node

| Device | How | Starts with |
| --- | --- | --- |
| Windows | The Nox installer, mode **"Connect to a Nox in the home network (only this device)"** (silent: `Nox-Setup-<version>.exe /MODE=node [/PAIR=<link>]`). No dashboards; the optional sandbox rules are offered as in the core install. | A per-user logon task "Nox Node": runs as you, *least privilege*, never a service, never elevated. Start menu "Nox Node" starts it now. |
| Linux desktop | `curl -fsSL https://github.com/Crackxsy/nox/releases/latest/download/install-linux.sh \| bash -s -- --node` - as yourself, **without** sudo. Needs `git curl tar python3`. | A systemd **user** unit `nox-node.service`. Command: `nox-node ...` (in `~/.local/bin`). |
| Linux without a screen (a Pi) | the same with `sudo bash -s -- --node --headless` | A hardened system unit for its own system user `nox-node`; data in `/var/lib/nox-node`. Command: `sudo nox-node ...`. |
| macOS (**beta**) | `bash scripts/install-macos.sh --node` in a checkout of this repository | A LaunchAgent at login, as you. **This script does not check release signatures yet:** once signing is set up it installs nothing from the network (no tag, branch or commit), only a checkout you verified (`NOX_SOURCE=<folder>`). |

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
   once a node is linked to it with `node.link` - a device name, or `auto` for none); the
   dashboard sets it per tool family under Geräte › Computer und Server,
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
- **Writing** creates only text files, in two tiers. *Plain text* - `.txt .md .csv .tsv .log
  .ics .vcf .srt`: a new file is medium risk (asked on Nox). *Structured text* - `.json .yaml
  .yml`, which a program may read: every write is high risk and asked on this device, the whole
  file is written at once (no appending), and the text must parse plainly - YAML with a tag, an
  anchor or an alias is refused. Documents, pages, PDFs and media (`.html .xml .svg .pdf .docx
  .xlsx .pptx .odt .png .mp3 ...`) are not written on a device at all - Nox writes `.md` or
  `.txt` instead. Replacing or appending is always high risk. Never a hidden file or anything in
  a hidden folder (`.git`, `.vscode`, `.bashrc`, ...), never anything inside a repository (a
  folder with `.git`, `.hg` or `.svn`, up to the allowed folder), never a file without an
  extension, a script, a program or a configuration file tools act on (`settings.json`,
  `package.json`, compose and pipeline files, `configuration.yaml`, anything in `workflows`,
  ...), never an executable file or one with a second name (hard link). The question shows the
  file, the mode, size, line count and a SHA-256, and the content - in full when it is short,
  otherwise its first and last 200 characters with "content shortened - N characters not
  shown". A write is never remembered, and a profile rule that allows writing on Nox does not
  allow it on a node unless it names the device (`device:`).
- **Accepted for plain text:** a `.csv` can hold a spreadsheet formula, which Excel runs only
  after its own warning and your click; the other plain formats run nothing.
- **Commands** (`shell`) run only in this device's own sandbox (AppContainer on Windows,
  bubblewrap on Linux), which cannot see the node's folder - on Linux the node refuses commands
  when its folder lies under a system folder the sandbox shows. Without such a sandbox - macOS
  for now, Linux without `bwrap` - commands are refused, never run without one. High risk: asked
  on this device, or on Nox for a node without a screen.
- **Programs** (`packages`): not yet available on nodes - a package manager needs rights the
  sandbox does not give.

## Screen, camera, browser, windows, mouse and keyboard, music

All off by default except music; each is switched on only here, with `nox node allow <group>`.

- **Screen and camera** (`screen`, `camera`): every picture is high risk - the dialog on this
  device asks each time, and it is never remembered. Before the picture is taken a notification
  here says "Nox nimmt gerade ein Bildschirmfoto auf" (or "ein Kamerabild"); if no notification
  can be shown, there is no picture (`no_visible_indicator`) - so a node without a desktop takes
  none. The picture stays in memory for that one call and goes only to the Nox that asked; the
  node writes nothing. Nox itself must allow captures too: privacy mode FULL or BALANCED and the
  capture switch for that kind on (the camera's is off by default). Nox keeps the picture in its
  own captures folder; camera pictures are never sent to a cloud model.
- **Browser** (`browser`): Nox's own headless browser with a profile in the node folder, never
  yours; it reaches public pages only (no home network, no loopback), as on Nox itself. Reading
  is low risk, clicking, typing and submitting is medium (asked on Nox, never remembered for a
  node). No screenshots of pages on a node.
- **Windows and programs** (`desktop`, Windows only): list windows and programs, bring a window
  to the front, minimise, restore or close it (low/medium), end a program or type text into a
  window (high: asked here). Windows itself, Nox and a running game are never touched.
- **Mouse and keyboard** (`computer`, Windows only; elsewhere refused): `nox node allow computer`
  asks twice. A session is asked for on this device every time (high), lasts at most 15 minutes
  and 200 actions, shows a notification, and ends on Ctrl+Alt+Shift+K, the kill switch, a lost
  connection or its time. Inside it Nox may move, click, scroll and press key combinations - never
  into Nox's own windows, never while a question is open here. It needs the browser on this PC
  linked to the node in the dashboard. Nox's dashboard shows "Nox steuert gerade Maus und
  Tastatur auf <device>" with a Stopp while it runs; meanwhile no paired device (phone, browser
  on any PC) can allow anything - allow on the computer Nox runs on, or stop.
- **Never in a game:** while a game runs on this device or is in front - the core's list
  (default Rocket League), this device's own list (`nox node games add Game.exe`; removing asks
  first: `nox node games remove Game.exe`) or any anti-cheat software (process, service or
  driver) - or while something owns the screen (a full-screen game, presentation mode), every
  mouse, key and typing call and every change to windows and programs is refused - by the node
  itself, before anything else, whatever Nox sends; when the node cannot tell, it refuses too.
  `game.input.send` stays forbidden everywhere.
- **Approving while Nox controls a PC:** no paired device (phone, browser on any PC) can allow
  anything; on the computer Nox runs on, allowing needs the security PIN and is refused through a
  remote desktop. Deny and Stopp always work. If a PC went silent and its line stays, "Sperre
  lösen" with the PIN on the computer Nox runs on lifts it.
- **Music** (`media`, on by default, low risk; the node says so once in a notification): play, pause, next, previous, volume, louder,
  quieter, mute - on Windows through the system media controls, on Linux through MPRIS
  (`playerctl` or D-Bus) and PipeWire (`wpctl`) or PulseAudio (`pactl`). Not on macOS yet.

## In the dashboard

Geräte › Computer und Server lists every node: whether it is connected, what Nox may do there,
what it is the default device for. Its sheet has one switch per group:

- **Off** works there at once (no PIN on the computer Nox runs on; from a phone, the PIN). The
  core then routes nothing of that group to the device and tells the node (`nox/policy`), also
  after every reconnect.
- **On again** works there only for a group you switched off there, with the PIN when one is set.
  A group the device itself does not allow is switched on only on the device:
  `nox node allow <group>` in a terminal there (the tray icon only shows what is allowed; a Pi
  without a screen has none).

The same sheet sets the default device per tool family and removes the node ("Wirklich
entfernen"); defaults that named a removed node go back to `auto`. The requests are
`node.narrow` and `node.defaults.set` (see [SECURITY.md](SECURITY.md#nodes-nox-node)).

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
4. no downgrade and no replay: not below the running version nor below the highest version that
   ran here, and not created before the newest manifest this node already saw. A version that
   failed here is skipped for 7 days (or tried again with `nox node update --yes`);
5. it installs **by itself** only when it is a stable patch release, it changed no library
   (`deps_changed: false` in the manifest) and the **paired core accepts the protocol the new node
   speaks** (`node_speaks`, against the range the core sent at the last connection). A minor or
   major update, a pre-release or an unknown or refused protocol waits for `nox node update` on
   the device; a release that changed libraries needs the installer (a notice says which);
6. the bundle `nox-app-<tag>.zip`, from that tag's own release, matches the manifest's size and
   SHA-256 (hashed and unpacked from one open file);
7. it holds only Nox's code and configuration, every file with the SHA-256 its `BUNDLE.json`
   lists, and it was built from the same `uv.lock` as the code that runs here;
8. a trial start imports the new code with this Python, isolated.

Then one atomic switch of `app/state.json` (under a file lock), and the launcher starts the new
version - after checking its folder again, at every start, against the signed manifest with the
*installed* program's code and keys: a changed, added or missing file, or a forged state file,
runs the installed program instead. The new version counts as working once it connected and the
core accepted its protocol. Two real crashes before that (not a logoff, a shutdown or a stop), or
a core that refuses its protocol, roll it back. Installing the program again (any installer)
drops the versions the node installed itself. `nox node update --check` only looks; exit status 3
means release signing is not configured yet. When no newer signed release information was seen
for more than 30 days, `nox node status` and a notice say so: updates may be held back.

The certificate (30 days) renews itself over the connection from day 20, for the same key, at
most once in 12 hours. The old certificate stays valid until the node first connects with the new
one (or it ends by itself): an answer lost on the way, or a node whose clock is behind (up to an
hour is accepted), keeps working and asks again 10 minutes later. A node that was off for longer
than 30 days pairs again.

Accepted residual risks of self-updates, stated plainly:

- A process that a taken-over node account leaves running inside the node's service (headless
  Linux) can change a release folder between the launcher's check and Python importing a module
  later. It survives the launcher's own restarts of the node, not a `systemctl stop`, a reboot or
  a reinstall.
- A taken-over node account can make the node keep running an *older* signed release that is
  still newer than the installed program (by editing `state.json`) until the next install.

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
