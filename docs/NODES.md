# Nodes: Nox's hands on your other computers

*Deutsch: [NODES.de.md](NODES.de.md)*

A household has **one** Nox - the *core*, ideally on a Raspberry Pi. Every other PC or laptop can
run a small **`nox node`**: it connects to the core and lets Nox do things *on that device* - and
only the things you allowed there.

> Status: wave C is complete. A node pairs with a code, holds its own allow-list, audit trail and
> budget, asks high-risk questions on the device itself, and offers system values, notices, music,
> files in folders you allow, commands in its own sandbox, screen and camera pictures, a browser,
> windows and programs, mouse and keyboard (Windows), and the voice, OBS and pet-overlay
> satellites. Installing programs (`package.*`) is **not** available on nodes yet - see
> [Installing programs](#installing-programs-not-available-yet). The node finds the core only at
> the address in its pairing link; discovery in the home network comes with wave D.

Two rules hold everywhere below. **Every call passes two gates:** the core's permission engine and
the node's own gate - either can refuse, only both together allow. **The device decides its hard
limits itself:** the node treats the core as untrusted for them, so a game lock, a hard
prohibition, a group that is off on the device or the device's own dialog for high risk hold even
against a core that was taken over or patched to allow everything.

## Installing a node

| Device | How | Starts with |
| --- | --- | --- |
| Windows | The Nox installer, mode **"Connect to a Nox in the home network (only this device)"** (silent: `Nox-Setup-<version>.exe /MODE=node [/PAIR=<link>]`). No dashboards; the optional sandbox rules are offered as in the core install. | A per-user logon task "Nox Node": runs as you, *least privilege*, never a service, never elevated. Start menu "Nox Node" starts it now. |
| Linux desktop | `curl -fsSL https://github.com/Crackxsy/nox/releases/latest/download/install-linux.sh \| bash -s -- --node` - as yourself, **without** sudo. Needs `git curl tar python3`. | A systemd **user** unit `nox-node.service`. Command: `nox-node ...` (in `~/.local/bin`). |
| Linux without a screen (a Pi) | the same with `sudo bash -s -- --node --headless` | A hardened system unit for its own system user `nox-node`; data in `/var/lib/nox-node`. Command: `sudo nox-node ...`. |
| macOS (**beta**) | `bash scripts/install-macos.sh --node` in a checkout of this repository | A LaunchAgent at login, as you. **This script does not check release signatures yet:** once signing is set up it installs nothing from the network (no tag, branch or commit), only a checkout you verified (`NOX_SOURCE=<folder>`). |

`--pair '<link>'` pairs right away; running the installer again updates; `--uninstall` removes the
program and the autostart (the node's identity and audit trail stay; remove the device in Nox
too). On Windows, `nox-node.cmd` in the install folder is the `nox node` command, and the
installer has a page "Connect to Nox" where you paste the pairing link (or leave it empty and pair
later).

## Pairing a node

1. **Make a code on Nox.** In the dashboard: Devices & skills › Devices › Computers and servers ›
   **Add a PC or laptop** - pick the device's system, give it a name and press the button. Or, in
   a terminal on the core machine: `nox pair --node`. Network access must be on
   (`ipc.lan.enabled`, see [NETWORK_ACCESS.md](NETWORK_ACCESS.md)). Pairing a node hands a PC to
   Nox, so it asks for the security PIN when one is set - and from a paired phone or tablet always
   (without a PIN set, nodes are paired only on the computer Nox runs on).
2. **Pair on the new PC.** Nox shows a one-time link - as text, as a ready-made command and as a
   QR code that holds the same link - plus the fingerprint of its certificate authority. Paste the
   link into the Windows installer's "Connect to Nox" page, or run the command shown, for example

   ```text
   nox node pair "nox-node://pair?h=192.0.2.20&p=47843&c=ABCD2345&fp=…" --name Laptop
   ```

   The link carries the SHA-256 fingerprint of your Nox's certificate authority. The node fetches
   the authority, compares it with that fingerprint and only then sends the code - a device in
   your network that pretends to be Nox never sees it. That is also why there is **no browser
   certificate warning to click through** for a node: the authority is pinned from the link, not
   trusted on first sight. The code is valid for five minutes, once. The node generates its own
   key (P-256) and gets a 30-day client certificate for it; the private key never leaves the
   device. Nox accepts up to 10 nodes (`ipc.lan.max_nodes`).
3. **Start the node:** `nox node run` (the logon task, user unit or LaunchAgent does this at
   login). It connects to the core's node port (47844), reconnects on its own (1 s, then up to
   60 s apart) and never acts while it is not connected. The dashboard's sheet flips from
   "Waiting for the device …" to "… is connected." when it arrives.

`nox node status` shows whether it is paired and connected and what Nox may do here.

Names: only `A-Z a-z 0-9 ä ö ü Ä Ö Ü ß`, space and `- _ .`, up to 40 characters. A name Nox
already has (also as "LAPTOP" or "Kueche" for "Küche") is refused - pick another with `--name`.
Nox always shows a device with a short id it cannot choose itself: "Laptop · 3f2a".

## Which device does it?

Say it: "How much battery does my laptop have?" runs `system.info` on the laptop, "Show a notice
on the laptop: ..." shows it there. Every turn tells the model which devices exist and whether
they are online (for example "Geräte: dieser Core (core), Work PC (online), Laptop (offline seit
23.10. 14:02)"). When you do not say:

1. the default in `devices.defaults` (`core`, `origin` - the PC whose dashboard you are using,
   once a node is linked to it with `node.link` - a device name, or `auto` for none); the
   dashboard sets it per tool family in the node's sheet,
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

| Group | What Nox may do | Default | Notes |
| --- | --- | --- | --- |
| `system` | `system.info`: read OS, CPU, memory, disk, battery, temperature, uptime | on | everywhere |
| `notify` | `notify.show`: show a notice | on | needs a notification service (Windows, macOS, `notify-send` on Linux) |
| `media` | `media.control`: play, pause, skip, volume, mute | on | Windows and Linux; not on macOS yet |
| `files` | `file.list`, `file.read`, `file.write` in folders you allow | off | no folder by default |
| `shell` | `shell.run` in this device's sandbox | off | Windows (AppContainer), Linux (bubblewrap); refused on macOS |
| `packages` | install programs | off | **not available on nodes yet** |
| `screen` | `screen.capture` | off | high risk every time, needs a visible notification |
| `camera` | `camera.snapshot` | off | as `screen`; the core's camera switch is off by default |
| `browser` | `browser.*` in Nox's own headless browser | off | public pages only |
| `desktop` | list, focus, minimise, restore, close windows; end a program; type text | off | Windows only |
| `computer` | mouse and key combinations in a session | off | Windows only; `allow` asks twice |
| `voice` | the voice satellite | off | also needs the owner's switch in the dashboard |
| `obs` | `obs.request` to the OBS on this PC, the pet overlay relay | off | |

The core can switch groups off, never on. `allow` works only in an interactive terminal with a
typed confirmation; editing `node.yaml` by hand can only take something away - anything it adds is
ignored until you confirm it with `nox node allow`. `max_risk` lowers what the node accepts
(default `high`; `critical` is never accepted). Pairing confirms only these defaults: a
`node.yaml` that was there before is not adopted.

### High risk is asked on the device

A system dialog on this device shows what Nox wants to do, with every detail, for 60 s - no answer
means no, and a "yes" within the first 1.5 seconds counts as no. While it is open, other running
actions with a side effect are stopped and new ones refused (only reads go on). A node without a
screen refuses high risk, unless a person confirmed it on Nox itself and you allowed that on the
device: `nox node allow trust_core_for_high`. The node also keeps its own budget: 60 actions and 5
high-risk ones per hour (`max_calls_per_hour`, `max_high_per_hour` in `node.yaml`), independent of
the core's.

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
  folder with `.git`, `.hg`, `.svn`, `.jj`, `.bzr`, `_darcs` or `.sl`) or an app workspace whose
  notes can run code (an Obsidian vault with `.obsidian`, a Logseq graph, Foam, Dendron, a
  `.vscode`/`.idea` project) - inside the allowed folder or around it - never a file without an
  extension, a script, a program or a configuration file tools act on (`settings.json`,
  `package.json`, compose and pipeline files, `configuration.yaml`, anything in `workflows`,
  ...), never an executable file or one with a second name (hard link). The question shows the
  file, the mode, size, line count and a SHA-256, and the content - in full when it is short,
  otherwise its first and last 200 characters with "content shortened - N characters not
  shown". A write is never remembered, and a profile rule that allows writing on Nox does not
  allow it on a node unless it names exactly that device (`device:` with its full id). A
  `.md` file is refused when it contains anything a note app could run - the words
  `dataviewjs`, `js-engine`, `templater` or `meta-bind-js`, Templater `<%`, inline `` `$= ``,
  `<script`, `javascript:` - wherever it stands. So Nox cannot write a note that merely mentions
  these plugin keywords (or a link starting with `obsidian://` or `file:`). Code blocks in
  notes Nox writes on devices may only contain text or data formats (`text`, `md`, `json`,
  `yaml`, `toml`, `ini`, `csv`, `diff`, `log`) or name no language - a block in a programming
  language (a plugin can run it on a click), for a plugin (`dataview`, `jsx`, ...) or one the app
  draws (`mermaid`, `math`, `html`) is refused. An append is judged together with what the file
  already holds, and a refusal says what blocks it. Notes are at most 256 KB, JSON/YAML files 64 KB.
- **Your notes vault:** `nox node allow files --folder` refuses a folder that is (or lies in) a
  repository or an app workspace - also a vault whose config folder was renamed (found by what
  it holds). Allow a plain folder outside it - for example `Nox` next to the vault - and move
  what Nox wrote into the vault yourself. **Don't link (symlink, junction) Nox's allowed folder
  into a vault:** from the folder's side that cannot be detected. The settings folders of apps
  in your home folder (`~/.vscode`, `~/.idea`) do not count as a workspace - a vault there
  (`~/.obsidian`) does.
- **Accepted for plain text:** a `.csv` can hold a spreadsheet formula, which a spreadsheet runs
  only after its own warning and your click; the other plain formats run nothing.
- **Commands** (`shell`) run only in this device's own sandbox (AppContainer on Windows,
  bubblewrap on Linux), which cannot see the node's folder - on Linux the node refuses commands
  when its folder lies under a system folder the sandbox shows. Without such a sandbox - macOS
  for now, Linux without `bwrap` - commands are refused, never run without one. High risk: asked
  on this device, or on Nox for a node without a screen. A stop kills the whole sandboxed process
  tree.

## Installing programs: not available yet

`package.install` and `package.remove` exist only on the computer Nox itself runs on. On a node
they are not offered, and the core does not route them there: a package manager needs rights the
node's sandbox does not give (a system-wide install needs an administrator or root; even a
per-user install writes outside every folder the sandbox may see). `nox node allow packages` can
be set, but changes nothing yet.

This is an **open decision** for the project owner, between two designs:

- **A privileged helper service** on the device with a fixed, small set of requests (install or
  remove one package from the official source, nothing else) - like the root helper of the Nox
  Pi image. It can install anything the package manager can, so its requests need the device's
  own dialog and a tight allow-list.
- **A sandbox profile for package managers** that allows only per-user installs into the
  package manager's own folders. Safer, but it cannot install everything (system-wide programs,
  drivers).

What this means today: "Install VLC on the laptop" cannot be done on the laptop - Nox should say
so, and you install it there yourself. If Nox nevertheless offers an install, that install would
run on the computer Nox runs on, not on the laptop: check what the question shows and decline it.

## Screen, camera, browser, windows, mouse and keyboard, music

All off by default except music; each is switched on only here, with `nox node allow <group>`.

### Screen and camera

Every picture (`screen`, `camera`) is high risk - the dialog on this device asks each time, and it
is never remembered. Before the picture is taken a notification here says "Nox nimmt gerade ein
Bildschirmfoto auf" (or "ein Kamerabild" - Nox is taking a screenshot or a camera picture); if no
notification can be shown, there is no picture (`no_visible_indicator`) - so a node without a
desktop takes none. The picture stays in memory for that one call and goes only to the Nox that
asked; the node writes nothing. Nox itself must allow captures too: privacy mode FULL or BALANCED
and the capture switch for that kind on (the camera's is off by default), checked before the node
is asked and again when the picture arrives. Nox keeps the picture in its own captures folder;
camera pictures are never sent to a cloud model. Privacy zones of the node's own screen are not
applied yet.

### Browser

`browser`: Nox's own headless browser with a profile in the node folder, never yours; it reaches
public pages only (no home network, no loopback), as on Nox itself. Reading is low risk,
clicking, typing and submitting is medium (asked on Nox, never remembered for a node). No
screenshots of pages on a node.

### Windows and programs

`desktop`, Windows only: list windows and programs, bring a window to the front, minimise, restore
or close it (low/medium), end a program or type text into a window (high: asked here). Windows
itself, Nox and a running game are never touched. Window titles from a node are hidden while Nox
is in panic or safe mode.

### Mouse and keyboard (computer use)

`computer`, Windows only (elsewhere refused): `nox node allow computer` asks twice. A session is
asked for on this device every time (high), lasts at most 15 minutes and 200 actions, shows a
notification, and ends on Ctrl+Alt+Shift+K, the kill switch, a lost connection or its time.
Inside it Nox may move, click, scroll and press key combinations - never into Nox's own windows,
never while a question is open here. It needs the browser on this PC linked to the node in the
dashboard (`node.link`), so that Nox can tell which browser sits on the PC it controls. Nox's
dashboard shows "Nox steuert gerade Maus und Tastatur auf <device>" (Nox is controlling mouse and
keyboard on ...) with a Stopp while it runs.

### The game lock

While a game runs on this device or is in front, every mouse, key and typing call and every
change to windows and programs is refused - by the node itself, before anything else, whatever
Nox sends. A game counts when:

- it is on a watched list: the core's (`sensors.game.process_names`, sent to the node on every
  connection; the core can only add), or this device's own (`nox node games add
  StarfallArena.exe`; removing asks first: `nox node games remove StarfallArena.exe`);
- any anti-cheat software runs - process, service or kernel driver;
- something owns the screen: a full-screen game, a busy full-screen app or presentation mode -
  this catches games that are on no list;
- the node cannot tell (an unreadable foreground window, a check that fails) - then it refuses
  too.

An anti-cheat driver that starts with Windows runs all the time - mouse and keyboard stay locked on
that PC for as long as it is installed. That is on purpose: Nox cannot tell whether the game is
about to start. A full-screen video also counts as "owns the screen". Listing windows and
programs stays possible. `game.input.send` is a hard prohibition everywhere and can never be
allowed.

### The session lock and the PIN

While Nox controls mouse and keyboard on a PC, Nox could click "allow" on its own question there.
So for as long as the session runs (the node is the authority on that, and the lock also holds a
while after a node went silent):

- **The browser on that PC** can neither allow nor change anything; it sees "nothing can be
  approved from here" instead of a PIN field. A paired device that connects from that PC's
  address counts as that browser too.
- **Every other paired device and the computer Nox runs on** can allow and change things only
  with the **security PIN**. On the computer Nox runs on this is refused through a remote
  desktop (a Pi running Nox as a service has no remote session: the PIN applies).
- **No PIN set:** nothing can be allowed or changed anywhere until the session ends.
- **Deny and Stopp always work**, and talking to Nox (chat, push-to-talk) needs no PIN - except
  from the browser on the controlled PC. Messages from other paired devices during the session
  count as unchecked: anything they lead to that sends data out asks first.
- **A PC that went silent:** if its line stays in the dashboard, "Sperre lösen" (release the
  lock) with the PIN on the computer Nox runs on lifts it; otherwise it lifts by itself at the
  latest 120 minutes after the node's last word. A node that still reports a session locks
  again at once.

Remote-control software that draws into the PC's own console session cannot be told apart from a
person sitting there; the PIN is the guard in that case (see
[SECURITY.md](SECURITY.md#accepted-limits-nodes)).

### Music

`media`, on by default, low risk; the node says so once in a notification: play, pause, next,
previous, volume, louder, quieter, mute - on Windows through the system media controls, on Linux
through MPRIS (`playerctl` or D-Bus) and PipeWire (`wpctl`) or PulseAudio (`pactl`). Not on macOS
yet. "Turn the music down on the Work PC" runs `media.control` there without a question.

## Voice, OBS and the pet overlay (satellites)

Three things a node can do for Nox beyond its hands - all off by default, each switched on only
here (`nox node allow voice`, `nox node allow obs`). How they work is set in `satellites.yaml`
in the node folder (preferences only, nothing secret, never *whether*).

### Voice satellite

`voice`: Nox listens and speaks on this device - a Living room Pi with a USB speakerphone, say.

- **Two keys.** `nox node allow voice` here *and* "Switch on listening and speaking" for this
  device in the dashboard (the node's sheet; with the PIN when one is set, from a paired device
  always). Until then the group shows as switched off there. The core checks the second key again
  for every utterance.
- **Listening** is push-to-talk by default (`mode: push_to_talk`): hold `Ctrl+Alt+Space` where the
  tray runs, or run `nox node voice talk` and press Enter when you are done. With
  `mode: wake_word` the microphone stays open and Nox reacts to its name ("Nox, ...").
- **Text, never audio.** Speech recognition runs here (faster-whisper, `tiny` on a Pi, `base`
  elsewhere - `stt_model` changes it); only the recognised **text** goes to Nox, at most 500
  characters per utterance. Audio never leaves the device and is never saved; the audit trails
  record that something was heard and its length, never the words. The answer comes back as text
  and is spoken here (Supertonic 3, voice F3; Piper when Supertonic is missing). Download the
  voice models once with the voice setup (`python -m nox.worker --download-supertonic
  --models-dir <node folder>/models`); nothing is downloaded by itself.
- **You see and hear when it listens:** the tray icon says "Nox is listening" while the
  microphone is open, and a short tone plays when push-to-talk starts and when Nox took your
  question (`earcon: false` turns the tones off). `nox node voice status` says the same in a
  terminal.
- **Mute and privacy.** `nox node voice mute` mutes the microphone here (also in the tray menu);
  only this device unmutes it (`nox node voice unmute`). Nox's privacy closes it too: the
  microphone (capture) switch, panic, safe mode, a privacy zone and the stop button. While Nox
  controls mouse and keyboard on this PC, it does not listen to it.
- **"Nox, Notaus"** (or "Nox, emergency stop") stops this node - and with it Nox everywhere - and
  never reaches Nox as text.
- **Lower trust.** A microphone in a room hears whoever is there, not necessarily you. What it
  hears becomes a conversation of its own on Nox (`voice:node:<id>`), recorded or not by Nox's
  privacy mode, and that conversation counts as holding outside content from its first word:
  anything that sends data out asks first, a "remember for this session" you gave elsewhere does
  not count for it, and the question says "von Sprach-Satellit <name>" (from voice satellite
  ...). Facts Nox learns from it wait for your confirmation. **Approvals are never given by
  voice.** "Here" means this device: "Turn the music down here". It waits for whatever Nox is
  doing on the dashboard instead of interrupting it (at most 90 s). Locks, alarm panels, garage
  doors and valves are out of reach as for every channel - Nox has no tool for them.
- **No shortcuts from the room.** Presets and mode changes do not start from here - only a preset
  you marked "Also from voice satellites" (with the PIN when one is set), and then every step
  follows the normal rules; the mode never changes by voice from a satellite, and "new
  conversation" said there neither discards the conversation nor ends its grants.
- **Echo window.** While Nox speaks here, and for 0.3 s after, it does not take what it hears as a
  question, so it does not answer itself; the core also drops an utterance from a node that spoke
  within the last second. "Nox, Notaus" still works, and so does push-to-talk, which stops Nox
  speaking. Nox never says its own name or a stop phrase out of this speaker, and it says only
  plain Latin letters, digits and common punctuation - words in other scripts, symbols and
  emoji are left out, so nothing can spell its name in lookalike letters. One question per
  "Nox": the next one needs the name again. The node speaks only answers to a question it asked
  in the last three minutes, at most 40 sentences.
- **Rate limits.** At most 12 utterances per minute per node; more are refused. A node that
  sends 30 in a minute counts as flooding and its connection is cut.
- **Saying something on purpose:** `voice.say` (low risk; asked when the conversation holds
  outside content): "Say in the living room that dinner is ready". It never says its own name or
  a stop phrase.

### OBS

`obs`: OBS runs on this PC, Nox on another one.

- `nox node obs password` stores the obs-websocket password in this device's keyring; it never
  goes to Nox's core. The node connects only to the OBS on this PC (`127.0.0.1`, port `4455` -
  `obs: port:` changes it).
- In `satellites.yaml` name this OBS's privacy scene and the scenes Nox may switch to
  (`obs: privacy_scene: "Technical Problems"`, `scene_set: ["Live", "Pause"]`).
- `obs.request` goes here when you say so ("on the stream PC") or when the dashboard makes this
  device the default for OBS. The rules are the same as on the core: reads run at once, changes
  are confirmed, consequential ones (going live, recording, deleting) are confirmed every time -
  in the dialog on this PC - and stopping the stream or reading the stream key is never possible.
  A scene switch through `obs.request` on a node is always asked here (Nox's core does not know
  this PC's scene set). The privacy scene and everything in it cannot be changed.
- The panic button switches this OBS to its privacy scene (also when this PC was off and comes
  back during the panic); after that Nox changes nothing in OBS here until you run
  `nox node obs resume` on this PC - that works at once, while the node runs.

### Pet overlay

The pet as an OBS browser source on this PC, from the Nox at home.

- With `obs` allowed, the node serves the pet on this PC only. `nox node overlay` prints the URL
  to put into an OBS browser source (or a browser here), e.g.
  `http://127.0.0.1:47845/pet/?overlay=1&device=<id>#overlay=relay`. The URL holds no secret: the
  node adds its own read-only credential, renews it on every connection and loses it when the
  node is removed or OBS is switched off for it in the dashboard.
- The pet moves its mouth with what this device says (only this overlay gets that). Without a
  connection it shows "offline" and reconnects by itself.
- Anything running on this PC can open that address too and see what the overlay sees (how the
  pet looks and its speech levels, never what is said).

## In the dashboard

Devices & skills › Devices › Computers and servers lists every node: whether it is connected
("Connected", "Offline since ...", "Update needed"), what Nox may do there, what it is the default
device for, its version. Its sheet has one switch per group:

- **Off** works there at once (no PIN on the computer Nox runs on; from a phone, the PIN). The
  core then routes nothing of that group to the device and tells the node (`nox/policy`), also
  after every reconnect.
- **On again** works there only for a group you switched off there, with the PIN when one is set.
  A group the device itself does not allow is switched on only on the device:
  `nox node allow <group>` in a terminal there (the tray icon only shows what is allowed; a Pi
  without a screen has none - use a terminal there, over SSH for example).

The same sheet sets the default device per tool family, shows "Recently done here" from the
audit, and removes the node ("Really remove", pressed twice); defaults that named a removed node
go back to `auto`. The requests are `node.narrow` and `node.defaults.set` (see
[SECURITY.md](SECURITY.md#nodes-nox-node)).

## Stopping

- **The stop button of Nox stops every node within a second**: the kill switch reaches every
  connected node over its open connection, running actions are aborted (a sandboxed command's
  whole process tree with them), a computer-use session ends, and the node refuses everything
  until Nox resumes.
- **On the device itself:** `nox node stop`, the tray's **Stop here** or `Ctrl+Alt+Shift+K`
  (where a global key hook is available) - or "Nox, Notaus" on a voice satellite - stops it there
  and Nox everywhere; only `nox node resume` (or **Resume here**) on that device lifts the node's
  own stop.
- **Dead man:** if a node hears nothing from Nox for 3 seconds while it is doing something, it
  aborts that and refuses everything until Nox is back and not stopped.

On a desktop with the tray (Windows; Linux where PySide6 is installed) the icon shows the state -
connected, "Not connected to Nox", update needed, stopped - and its menu has **Stop here**,
**Resume here**, **What Nox may do here** and, with voice, the microphone switch. The tray only
writes the same stop file as `nox node stop`. Notices from a node installed in node mode on
Windows carry Nox's own name (AppUserModelID `Nox.Node`), not "Windows PowerShell".

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

A node whose protocol the core does not accept shows "update needed" and offers only
`system.info` until it is updated.

The certificate (30 days) renews itself over the connection from day 20, for the same key, at
most once in 12 hours. The old certificate stays valid until the node first connects with the new
one (or it ends by itself): an answer lost on the way, or a node whose clock is behind (up to an
hour is accepted), keeps working and asks again 10 minutes later. A node that was off for longer
than 30 days pairs again.

The accepted residual risks of self-updates are listed in
[SECURITY.md](SECURITY.md#accepted-limits-nodes).

## What the node keeps, and where

| File | What |
| --- | --- |
| `node.key` | The node's private key, made on this device and never sent anywhere. Encrypted when the OS has a keyring (Windows Credential Manager, macOS Keychain, a desktop Linux secret service); on a headless Linux node it is protected by its file mode (0600) only. |
| `node.crt`, `ca.crt`, `node.json` | Its certificate (30 days), the pinned authority, the core's address and the node's identity |
| `node.yaml` | What Nox may do here |
| `manifest.key` | Only where there is no keyring: the key that signs the confirmations of `node.yaml` (0600) |
| `audit.db` | Every call, allowed or refused, hash-chained: `nox node log` |
| `app/` | Versions the node installed itself (`releases/<version>`) and `state.json`, which one runs |
| `update.json` | The last update check, shown by `nox node status` and the tray |
| `satellites.yaml` | How voice, OBS and the overlay relay work here (nothing secret) |
| `voice.json`, `voice-muted`, `voice-ptt` | What the voice satellite does now (for the tray), the mute switch, push-to-talk held |
| `obs-panic` | Present after the panic button: Nox changes nothing in OBS here until `nox node obs resume` |

Folder: `%LOCALAPPDATA%\Nox\node` (Windows), `~/.local/share/nox/node` (Linux),
`~/Library/Application Support/Nox/node` (macOS); `NOX_NODE_HOME` overrides it. The node runs as
the logged-in user, never as administrator or root. Pictures, audio and recognised speech are
not stored on the node; the obs-websocket password is in the keyring only.

## Removing a node

In the dashboard ("Really remove" in the node's sheet) or on the core machine:
`nox unpair --node Laptop`. Removing needs no PIN on the computer Nox runs on (it only takes
access away). The connection closes at once, every certificate of the node goes on the denylist
and never works again, its remembered grants end, defaults that named it go back to `auto`, and a
connected node deletes its key and certificate ("Removed from Nox"). A node removed while it was
off is refused when it comes back. To pair such a device again, see
[Certificate and authority errors](#certificate-and-authority-errors).

`--uninstall` on the device removes the program and the autostart; remove the device in Nox too.

## Troubleshooting

`nox node status` on the device and the node's line in the dashboard are the first places to
look; `nox node log` shows every decision the node made, with its reason.

### The node shows "offline"

- **Is it running?** `nox node status` says `not running`, `disconnected` (it cannot reach Nox),
  `connected`, `update_needed`, `pin_mismatch` or "Removed from Nox". Start it with
  `nox node run`, or check the logon task "Nox Node" (Windows), `systemctl --user status
  nox-node` (Linux desktop) or `sudo systemctl status nox-node` (headless).
- **Is network access on?** The node listener runs only while `ipc.lan.enabled` is on; the
  dashboard says "You can connect computers once other devices may open Nox." otherwise.
- **Can it reach the core's port?** See [Firewall](#firewall-port-47844) below.
- **Did the core's address change?** The node connects only to the address in its pairing link
  (discovery comes with wave D). Give the core a fixed address in your router, or pair again.
- **"Update needed"** means the core does not accept the node's protocol: update the node (or
  the core); until then Nox only reads system values there.

### The pairing code expired or was refused

A code is valid for five minutes and only once. The node says why it failed: "The code has
expired" or "was already used" - make a new one; "Too many attempts. Wait a minute." - wrong
codes are limited per address and in total; "Nox has as many nodes as it accepts" - remove one
first; a name that looks like an existing device - choose another with `--name`; "This device is
already paired" - see the next section.

### Certificate and authority errors

- **"The device answering is not the Nox from the link (its fingerprint differs). Nothing was
  sent."** The address in the link answered with another certificate authority than the link
  names. Make sure the link came from your own Nox and that you are on your own network; never
  pair through a network you do not control.
- **"Nox: pair again" / `pin_mismatch`:** the authority stored on the node no longer matches the
  one it pinned at pairing; the node stops and accepts no other.
- **The certificate expired** (the node was off for more than 30 days) or **the node was removed
  while it was off:** it cannot connect any more and has to be paired again.
- **The core's authority was replaced** (for example a fresh install with a new data folder):
  every node has to be paired again.

To pair a device again: remove it in Nox (if it is still listed), stop the node on the device,
delete `node.json`, `node.crt` and `node.key` from the node folder, then run `nox node pair`
with a new link. Pairing again starts from the default groups; allow what you need again with
`nox node allow`.

### Firewall: port 47844

The node dials out; the **core** must accept it. Pairing uses the core's network port 47843, the
running node connects to the node port 47844 (`ipc.lan.node_port`). On a Windows core, allow
Python in private networks when the firewall asks. On a Linux core with `ufw`:
`sudo ufw allow 47843/tcp` and `sudo ufw allow 47844/tcp`. The Nox Pi image opens both by itself
while network access is on, from private networks only. The node itself needs no open port; the
pet-overlay relay (47845) listens on its loopback only. See
[NETWORK_ACCESS.md](NETWORK_ACCESS.md#firewall).

### The game lock refuses

A refusal `game_running`, `game_in_foreground` or `windows_only` comes from the node itself and
cannot be overridden from Nox. Check:

- whether a watched game - or its launcher - still runs in the background;
- whether anti-cheat software is installed and running (a driver that starts with Windows keeps
  mouse and keyboard locked for good);
- whether something is in full screen or in presentation mode (a video counts);
- `nox node games remove <program>` removes only the device's own entries; the core's list is
  changed in the core's settings (`sensors.game.process_names`), and what the core added cannot be
  removed on the node;
- mouse, keyboard and windows exist on Windows nodes only (`windows_only`).

### The voice satellite does not listen

Run `nox node voice status`. In order:

- **"Voice: off"** - `nox node allow voice` on the device;
- **"Muted here: yes"** - `nox node voice unmute` on the device;
- **"Core says: no (...)"** - the reason is Nox's: the device is not switched on in the dashboard
  (the second key), the microphone or capture switch is off, Nox is in panic or safe mode, a
  privacy zone is active, Nox is stopped, or Nox controls mouse and keyboard on this PC;
- **"Running: no"** or a **"Problem:"** line - the voice pipeline did not start: usually the
  voice models are missing (download them once, see above) or no microphone was found;
- in `mode: push_to_talk` nothing is heard without the key or `nox node voice talk`; in
  `mode: wake_word` every question starts with "Nox";
- while Nox speaks here, and just after, what it hears is not taken as a question.

### An update failed or was rolled back

`nox node status` shows the last check in its "Update:" line. "Release signing is not configured"
means there are no self-updates yet (exit status 3 of `nox node update --check`). A version that
failed is skipped for 7 days; `nox node update --yes` tries it again. A version that crashed twice
before it connected, or whose protocol the core refused, was rolled back to the previous one - the
node keeps working on that. A release that changed libraries needs the installer; a minor or
major release waits for `nox node update` on the device. A warning that no newer signed release
was seen for 30 days means updates may be held back on the way: check the network, or install
the current release with the installer.

## Security

See [SECURITY.md](SECURITY.md#nodes-nox-node) for the node's threat model and
[Accepted limits (nodes)](SECURITY.md#accepted-limits-nodes) for every accepted limit in one
place. Known limits in this release: the node finds the core only at the address in the pairing
link (discovery comes with wave D); installing programs on nodes is an open decision. The Windows
logon task is checked in CI on an administrator account; that a standard account can register it
is the documented Task Scheduler behaviour for a task that runs as that account, not yet a tested
fact. The macOS installer is a beta.
