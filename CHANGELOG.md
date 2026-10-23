# Changelog

All notable changes to Nox are documented here, in [Keep a Changelog](https://keepachangelog.com/)
style. Versioning follows [Semantic Versioning](https://semver.org/). Every release is tagged;
changelog entries are reviewed before merge, not added after the fact.

Nox is pre-v1.0 as of this writing. See `docs/RELEASE_CHECKLIST.md` for what v1.0 itself requires.

## [Unreleased]

### Changed
- **The dashboard is rebuilt around four areas** (Spec UX-Neubau): Heute, Nox (Gespräch, Ansichten,
  Clips, Verlauf), Geräte & Fähigkeiten, Einstellungen (eight pages instead of one form). Every page
  has the same header - what Nox is doing and its Modus, "n warten auf dich", the privacy level and
  **Stopp**, which now acts at once (Ctrl+Alt+Pause too) and is undone with "Fortsetzen" under the
  core's resume rules (PIN after a security-path stop; a paired device can stop but not resume). The
  old two-click Not-Aus with its optional "Grund" field is gone: every dashboard stop is recorded
  with the fixed reason `dashboard stop`. Stopp is also inside every sheet, menu and the first run,
  so it is never behind one. Mode and security profile are one **Modus** control; `mode.set`
  switches both. "Sag Nox, was er tun soll" at the bottom of every page opens a command palette
  (Ctrl+K): lights and scenes, the home command layer, Abläufe, every page and setting, "Nox
  fragen". One permission component in three places (inline on Heute and in the conversation, a
  sheet elsewhere) asks in plain words with the risk as a word and the time left. A first-run flow
  (name, privacy, voice) and empty states that say how to fill them. New design tokens (light and
  dark, WCAG 2.2 AA), phone-first layout with a bottom bar, areas loaded on demand; the start bundle
  fell from 118 KB to about 107 KB gzip including the dictionary (budget enforced in CI by `npm run
  size`). Old tab names (Status, Board, Presets, Remote, Audit) redirect to their new place.
- **Supertonic 3 is now the default voice** (`voice.tts.engine: supertonic`, voice `F3`, new
  `voice.tts.steps: 5`, editable 1-32). Its model files (~400 MB, OpenRAIL-M) are downloaded only
  after the user agrees: a new "Voice" tile in the dashboard's settings (missing files, size,
  license, download with a progress bar; the core runs `python -m nox.worker --download-supertonic
  --progress-json` and refuses before starting it in the privacy modes `offline`/`private`), an
  optional step in `nox onboard`, and `nox voice setup`. Until the files are there, Nox speaks with
  Piper and reports the voice as `limited`. The installer bundles the `voice-supertonic` extra.

### Added
- **Satellites on nodes: voice, OBS and the pet overlay** (Spec C9, docs/NODES.md). A node with
  `voice` allowed (and switched on for it in the Devices page - two keys, stored per node,
  migration 0018) listens and speaks there: push-to-talk (`Ctrl+Alt+Space`, `nox node voice
  talk`) or the wake word, speech recognition on the device (`tiny` on a Pi), only text to the
  core (`nox/voice/heard`), the answer back as text and spoken there (Supertonic 3, Piper as
  fallback); the core's privacy gates the microphone (`nox/voice/gate`), a local mute switch
  only the device lifts, the tray shows "Nox hört zu", "Nox, Notaus" stops the node; `voice.say`
  speaks on a device on purpose. `obs.request` is routed to the PC where OBS runs (`obs`), with the
  password in that device's keyring, the core-local request rules decided again there, and the
  panic button reaching it (`nox/obs/panic`, latched until `nox node obs resume`). The pet
  overlay works on that PC through the node's loopback relay (`nox node overlay`): no credential
  in the URL, a read-only one per node rotated on every connection, an offline label, a pet that
  follows that node's speech (`?device=`). The OBS WebSocket client moved to
  `nox.stream.obs_ws` (the plugin re-exports it).
- **Screen, camera, browser, windows, mouse and keyboard, and music on nodes** (Spec C6,
  docs/NODES.md). `screen.capture`, `camera.snapshot`, `browser.*`, `desktop.*`, `computer.*`
  and the new `media.control` run on a paired PC through the same gate, receipts, budget and
  audit as every node tool; all off on the device by default except music, and narrowable in the
  dashboard. A picture is asked for on the device every time, announced there by a notification
  first (no notification, no picture), held in memory for the call only and stored on Nox, where
  this core's privacy decides. Mouse and keyboard are Windows only, asked for per session on the
  device, and refused by the node itself while a game or an anti-cheat service runs or a game is
  in front; the dashboard shows "Nox steuert gerade Maus und Tastatur auf <Gerät>" with a Stopp.
  `media.control` plays, pauses, skips and sets the volume (SMTC/Core Audio on Windows, MPRIS and
  PipeWire/PulseAudio on Linux).
- **Nodes install, start and update themselves** (Spec C7, docs/NODES.md). The Windows installer
  has a node mode ("Mit einem Nox im Heimnetz verbinden (nur dieses Gerät)", `/MODE=node`): no
  dashboards, a per-user logon task "Nox Node" (least privilege, never a service, never elevated),
  its own notification ID `Nox.Node`, optional pairing during setup. `install-linux.sh --node`
  sets up a desktop node as the user with a systemd user unit, `--node --headless` a hardened
  system unit for a system user `nox-node`; both idempotent with `--uninstall`.
  `scripts/install-macos.sh --node` is a LaunchAgent beta. A node updates itself only from the
  signed release (`nox-app-<tag>.zip`, new release asset): signature against the trusted keys
  (none configured: no update), no downgrade, size and SHA-256 from the signed manifest, safe
  unpacking, matching libraries, a trial start, an atomic switch and an automatic rollback of a
  version that does not start (`installer/start_node.pyw`); patches by themselves, minor and major
  only after `nox node update` on the device. The node certificate renews itself over the
  connection from day 20 (`nox/renew`, same key only, once per connection). A tray icon with
  "Not-Aus hier" and `Ctrl+Alt+Shift+K` where PySide6 and pynput are installed.
- **Nodes, part 4 (the node's gate and stopping):** every call with a side effect carries the
  core's Ed25519 receipt (bound to call, tool, arguments, risk, deadline <= 60 s; replays and
  altered calls are refused); the node keeps its own risk ceiling and budget (60 actions, 5
  high-risk per hour); high risk is confirmed in a dialog on the device itself (D1; all details,
  60 s, default no), delegated by the core only to a node that can ask; nodes without a screen
  refuse it unless trusted locally. Stopping: the kill switch reaches every node in under a
  second (p99, 5 nodes), running calls are aborted, `nox/resume` lifts it; a dead-man rule aborts
  a side effect when the core goes silent for 3 s; `nox node stop` / `nox node resume` on the
  device. Sessions end with their certificate.
- **Nodes, part 3 (routing):** `system.info` and `notify.show` take an optional `device`
  ("core" or a device's name); without it Nox uses `devices.defaults` (`core`, `origin`, a name),
  then the only device that can, and never guesses a side effect onto a device. Offline devices
  answer honestly and are never substituted, nothing is queued. Permission targets, the audit and
  remembered grants are bound to the device. Node results are checked and bounded and taint the
  conversation. Each turn names the devices ("Geräte: ..."). `node.link` ties a node to the
  browser device on the same PC.
- **Nodes, part 2 (the node):** `nox node pair <link>` pins the core's authority from the link
  before it sends the code, generates the key on the device (encrypted with a passphrase in the
  OS keyring where there is one) and stores the 30-day certificate; `nox node run` keeps the
  connection (TLS 1.3, only the pinned authority, reconnect 1 s to 60 s, ping every second);
  `nox node status`, `nox node log` (the device's own hash-chained audit trail), `nox node allow`
  / `deny` (the only way to widen what Nox may do there; the core can only narrow). Tools
  `system.info` and `notify.show`. The node's own gate refuses hard prohibitions, disabled groups,
  unknown tools, invalid arguments and anything above low risk, whatever the core sent. On the
  core: `nox pair --node`, `nox unpair --node`. Guide: `docs/NODES.md`.
- **Nodes, part 1 (core side):** another PC in the home can be paired as a `nox node`. A trusted
  session creates a code and a `nox-node://pair?...` link that pins the installation's authority;
  the node redeems it at `POST /api/node/pair` for a 30-day client certificate (EKU clientAuth,
  P-256, key generated on the node). Nodes connect to a separate listener
  (`ipc.lan.node_port: 47844`, TLS 1.3, client certificate required, serial denylist, no browser
  Origin) and speak JSON-RPC (the core is an MCP client of the node over the new
  `WebSocketTransport`). Role `node` may send only a fixed method list; `node.pair.start`,
  `node.list` and `node.unpair` (closes the connection at once). Migration `0017_nodes`.
- **Signed releases** (docs/RELEASES.md). Every release gets `release-manifest.json` (version, tag,
  source commit, each file with size and SHA-256) and an Ed25519 signature made in the release
  workflow's protected `release-signing` environment from the secret `NOX_RELEASE_SIGNING_KEY`
  (`scripts/sign_release.py`: self-contained, run as `python -I -S` with only hash-pinned
  `cryptography` importable, key on stdin only; workflow actions pinned to SHAs). Once a release
  key is in the trust list (`src/nox/release/trusted_keys.py`, mirrored in
  `scripts/install-linux.sh`), the Linux installer and `nox update` refuse a release whose manifest
  is missing, badly signed, signed by an untrusted or expired key, for another tag or commit, or
  older than the newest release already accepted (`--allow-downgrade` for a deliberate step back);
  the dashboard package must match the signed manifest. `nox release verify|keys|describe-key`
  check a manifest by hand and help the owner add a key; rotation works through several trusted
  keys with expiry dates. The trust list is empty until the owner creates the key: until then
  everything says "not configured", checksums are checked as before, and the release workflow
  fails the run with "Release not signed" instead of publishing an unsigned manifest.
- **Windows: the sandbox keeps off this computer's local services and the LAN.** Where Windows
  does not enforce AppContainer loopback isolation, the installer now adds Windows Filtering
  Platform rules for Nox's sandbox containers (one optional step, ticked by default, one UAC
  prompt; uninstall removes them): loopback, private, link-local and CGNAT destinations are blocked
  except the in-container egress forwarder's own ports. The loopback probe still decides: once it
  observes the container being refused, the AppContainer counts as strong and shell commands run
  without `hands.accept_weak_sandbox`. Workloads other than shell commands now use a fixed pool of
  128 containers (`Nox.Sandbox.w000`...), assigned once per workload class, so the rules can name
  every container SID up front. `nox sandbox firewall show|install|remove`.
- **Nox discloses its AI voice on stream** (`stream.ai_voice_disclosure`, on by default): one line
  in the Twitch chat per stream session, through the permission-checked `twitch.chat.send`, and a
  small, semi-transparent "KI-Stimme" label on the OBS overlay (`system.ai_voice_label`; not on
  the desktop pet). The default chat line follows Nox's language and name.
- **Nox has its own browser, and eyes.** `browser.open/read/screenshot` and, after a
  confirmation remembered per action and site, `browser.click/type/submit` drive a headless
  Chromium with its own profile - never your browser - that reaches public pages only: every
  connection goes through Nox's authenticated egress proxy, which refuses loopback, the LAN, cloud
  metadata and Home Assistant, redirects and subresources included. `screen.capture` and
  `camera.snapshot` ask first, follow the privacy modes, zones and capture switches, and keep the
  picture on the machine; `vision.describe` uses a local vision model when one is installed and
  says plainly when an image went to Claude (never a camera picture, a screen capture only in
  FULL). Optional extras `browser` and `senses`.
- **Long-running sandboxed processes** (`Sandbox.start`) for MCP stdio servers: the same
  confinement as a one-off run, with pipes, stop/kill, kill-on-close and restart hooks.
- **Nox calls its tools natively and answers faster.** Claude Code runs as one warm process per
  core and reaches Nox's tools over MCP (`nox.mcp`, IPC role `mcp`); local models use Ollama's
  native `tools` field, with the old text protocol only as a fallback. The three-round limit is
  gone: a turn may use up to 20 tool calls in 180 seconds, enforced in Nox, not in the prompt.
  Every call still goes through the permission engine and the audit; the kill switch ends the
  Claude Code process tree in under a second. The first sentence is spoken while the rest is
  written (a long opening sentence from its first clause), and a tool loop that keeps the user
  waiting gets one short "Moment, ich schau nach." Answers are shorter, German first, and use
  tools instead of guessing; turns routed to the reasoning chain use a stronger model.
- **Nox uses other people's MCP servers.** Add one under Fähigkeiten or in `mcp.servers`; its
  tools appear as `mcp.<server>.<tool>` and ask before they run. `npx`/`uvx` servers must pin an
  exact version, the first start of each command is approved in the dashboard, secrets are only
  references into the secret store, and descriptions and results travel as untrusted data.
- **Nox teaches itself what it cannot do yet** (`skill.learn`): it builds a small plugin with
  Claude Code (file tools only, no shell), the core checks it and runs its tests in the sandbox,
  and one click under Fähigkeiten switches it on - no restart, never in Nox's own source tree. A
  skill's worker runs only inside the sandbox (no hub token, no access to Nox's data or the
  keyring, only its declared hosts); without a strong sandbox nothing is learned or switched on.
  Its own tests are shown as what they are: written by the skill, no security proof.
- **Watchers - "sag mir, wenn ...":** one read-only tool (risk read and no side effects, or
  `web.fetch` / `web.search` with the address shown when it is confirmed), a condition and an
  interval of at least five minutes; a finding is reported once, and a watcher survives restarts. Nox asks for a
  missing train, station or price instead of guessing.
- **Computer use** (off by default, needs the PIN to switch on): mouse and key combinations in a
  session confirmed once, ended by the kill switch or Ctrl+Alt+Shift+K, and refused while a game
  runs or is in front. It never touches Nox's own windows, and while it runs Nox accepts no
  "allow" and no privileged dashboard action - stop it with the hotkey, then answer.
- **`nox ai setup`**: picks a local model for this machine's RAM/VRAM, downloads it with progress
  and only keeps it if it can answer and call a tool; recommends none on a small Raspberry Pi.
- **Capability benchmark** (`evals/`, `nox eval kann-alles`): 50 everyday tasks in a simulated
  world, an answer-quality set and a latency probe, with a report per run.
- `file.move` can create a missing destination folder when asked (`create_folders`).
- **Nox controls OBS.** `obs.request` sends any obs-websocket request; the core classifies each
  call by request type and fields - reads run, changes are confirmed (a "remember" covers that one
  type), deleting, going live, recording and anything that could put a private microphone or
  screen on air is asked every time, stopping the stream and reading the stream key stay hard
  prohibitions, the privacy scene cannot be touched. Answers are redacted before the model sees
  them. Plus `obs.screenshot` (local file only).
- **A stream overlay that survives restarts**: a persistent, read-only overlay credential and
  "Set up OBS overlay" (dashboard, tray) that creates the browser source after one confirmation.
- **Nox's own OBS audio track** in one step (VB-Audio Virtual Cable), and the voice output
  devices as pick lists in Settings, switched live.
- **The stream session starts by itself** when OBS or the Twitch channel goes live, so chat is
  answered without saying "lass uns streamen" first.
- Twitch and OBS login health: an expired Twitch login starts the device-code login and shows the
  code; a rejected OBS password is reported once.
- **A more natural German voice, as an option: Supertonic 3** (`voice.tts.engine: supertonic`,
  optional extra `voice-supertonic`). Ten preset voices (`F1`–`F5`, `M1`–`M5`, default `F3`), 31
  languages, runs on the CPU (~0.4-0.6 s per short sentence with 4 threads, well below real time),
  so the GPU stays free for games and streaming. Same `TtsEngine` contract as Piper and Kokoro -
  sentence-wise streaming, prepared clips - plus `render(sentence)` for a pipeline that streams
  sentence by sentence. The model files are fetched only on request
  (`python -m nox.worker --download-supertonic` / `nox voice download-supertonic`, ~400 MB), pinned
  to one upstream revision and checked against SHA-256. Licensing: the package is MIT and pulls no
  phonemizer (no GPL), but the **weights are OpenRAIL-M with use-based restrictions** - see
  `NOTICE`. Piper stays the default.
- If the configured TTS engine (Supertonic or Kokoro) cannot load, the voice worker now speaks with
  Piper instead of failing, and reports the voice as `limited` with the reason.
- **Nox runs as a server on a Raspberry Pi or any Linux machine.** One command installs it as a
  hardened systemd service (`scripts/install-linux.sh`, `docs/RASPBERRY_PI.md`): its own system
  user, the program read-only in `/opt/nox`, everything it keeps in `/var/lib/nox`, Python 3.13
  through uv from prebuilt packages only, and the prebuilt dashboard from the release, checked
  against its SHA-256, so the Pi needs no Node.js and compiles nothing. Every version is a
  complete folder of its own (`/opt/nox/releases/<commit>`, `/opt/nox/current` the running one):
  an update builds the new one while the old keeps running, snapshots `/var/lib/nox`, switches and
  starts - and if the new version does not answer, it switches back and restores the data by
  itself. `nox rollback` does the same on request - exactly one step, so the same old data can
  never be put back twice - and `nox snapshots` / `nox restore` put data back without ever writing
  over the current folder (a corrupt snapshot is refused before anything moves; only snapshots Nox
  made itself are accepted). `nox update` runs the installer of the version it installs, taken from
  that version's git tree; an installation from a branch stays on it; only real releases count as
  "newest", one installer runs at a time, and bytecode is compiled at install time so a Pi does
  not recompile on every start. `nox doctor` ends with one line on whether a newer release exists.
  A short `nox` command acts on the service's data (`nox doctor`, `nox onboard`, `nox update`,
  `nox claude-login`). The installer is attached to
  every release with its SHA-256, and CI runs it for real on x86-64 and ARM64: install, update,
  rollback, a deliberately broken version that must roll itself back, and UI bundles with a
  symlink, a `..` path or an extra file that must be refused. Guide in English and German
  (`docs/RASPBERRY_PI.md`, `docs/RASPBERRY_PI.de.md`).
- **Nox on every device in the home.** Phones, tablets and other PCs open the dashboard over
  HTTPS once they are paired (`docs/NETWORK_ACCESS.md`, `ipc.lan.enabled`; on by default on a
  Raspberry Pi). Pairing is one QR code and a five-minute, single-use code - from the dashboard's
  new **Devices** tab or `nox pair` on a server without a screen. Nox issues its own certificate
  authority, technically limited to the home network (name constraints), so installing it once
  removes the browser warning for good and lets the dashboard go on the home screen as an app.
  Every permission question appears on every dashboard at once and the first answer counts; a
  paired device answers for that one time only, and the audit trail names the device. A paired
  device does everything day to day - talk, the day, the house, the kill switch - but not change
  how Nox is set up. Allowing something risky, approving a plan, pairing or removing another
  device takes the security PIN on a paired device, and three wrong PINs remove that device
  before the PIN itself could lock its owner out. Removing a device ends its sessions at once -
  from the dashboard, or on a server without a screen with `nox devices` and `nox unpair <name>`.
  The session token, the pet and the local HTTP API never reach the network; a network connection
  can only ever be a paired device, only private networks are answered, and the listener checks
  Host and Origin against DNS rebinding and foreign pages. A click test drives a PC, a phone and a
  tablet through pairing, confirmations and removal in a real browser and records it on video.
- **One folder per machine.** `nox_home()` is `%APPDATA%/Nox` on Windows and
  `~/.local/share/Nox` on Linux (`~/Library/Application Support/Nox` on macOS, path logic only -
  macOS is not supported yet), and `NOX_HOME` moves it (the Pi service uses `/var/lib/nox`). The
  shipped defaults follow it, and a Windows `user.yaml` with `${APPDATA}` lands in the same folder
  on Linux. `nox where` prints where the settings, data and secrets are.
- **Secrets without a desktop.** Where no OS keyring works - a Pi without a desktop session - Nox
  keeps credentials in an owner-only `secrets.json` instead of failing every save, locked across
  processes and refused when it is a link, foreign or readable by others. The choice is remembered:
  a keyring that disappears later is reported (health check `secrets`, `nox where`, `nox doctor`)
  and PIN-gated changes are refused, instead of starting over with an empty file; Windows and macOS
  never fall back to the file on their own. `security.secret_store` forces either.
- **CI on Linux, x86-64 and ARM64**, next to Windows: the core's unit and integration tests run on
  the Pi's architecture on every pull request.
- **Nox-OS image build (O1)** (`os/`, `.github/workflows/os-image.yml`): a flashable Raspberry Pi
  OS Lite (64-bit) image with Nox preinstalled, built with a pinned pi-gen on a native arm64 runner.
  Nox comes from `install-linux.sh --image` (same layout and signed-release check as a Pi install;
  enabled, not started, refused on a running system). The image is checked read-only (no password,
  key, SSH, Wi-Fi or Nox data baked in; SSH masked; host keys and machine id made at first boot)
  and boot-tested headless in QEMU until Nox answers; xz, SHA-256, a Raspberry Pi Imager entry and
  a licence/source manifest. For a release a second signed manifest, `nox-os-manifest.json`
  (`"kind": "os-image"`; every verifier now names the manifest kind it expects, `nox release verify
  --kind`), and without the release key nothing is attached at all. While `install-linux.sh` trusts
  no release key, the Nox inside the image is pinned by tag/commit and its dashboard by SHA-256, not
  by a signature. The first-boot setup without a screen follows (O3/O4), so the image is for testing
  until then.

- **Nox-OS hardening (O2)** (`os/pi-gen/stage-nox/03-hardening`, os/README.md): nftables with
  inbound default deny (IPv4/IPv6), Nox's dashboard and node ports generated from its own settings
  at boot and on every settings change, only while network access is on, from private networks
  only and rate-limited per address; fails closed; SSH from the device itself always dropped. SSH
  stays off and masked; **Einstellungen › System › Fernwartung (SSH)** - with the security PIN
  **and a maintenance passphrase** that only the root helper checks (scrypt, root 0600, lockout;
  set during setup, changed with the old one) - stores public keys
  (ed25519/ECDSA) with the root helper - root-owned, shown by fingerprint - and switches remote
  maintenance on for the fingerprints the owner confirmed and a time (15 min-4 h), both with the
  security PIN (always, from every client; audited): sshd with its own key-only configuration for
  `nox-admin` from private networks, sudo for that session, then everything - open sessions
  included - ends by itself (`noxos.ssh.*`, through a socket-activated
  root helper reachable only by a group `nox.service` alone carries, answering only its main
  process - checked by pidfd, cgroup and start time - with a fixed set of requests). SD-card
  wear profile: journal in RAM, `/` with `noatime,commit=60`, `/tmp` and `/var/tmp` as tmpfs,
  zram instead of a swap file, SQLite `wal_autocheckpoint` raised and `PRAGMA optimize` on close.
  Kernel/network sysctl hardening, Bluetooth/triggerhappy/ModemManager masked, a console status
  screen on tty1 (versions, addresses, dashboard URL, pairing, health, SSH; no secrets, no login).
  `check-image.sh` and the QEMU boot test check all of it, including an SSH on/off round trip.

### Fixed
- **Nodes, external review of part 4:** the node's gate never resolves or opens anything the
  core sent (a UNC path in a notice could make Windows reach out over SMB) and checks the receipt
  before any argument; path aliases (`..`, `~`, 8.3, UNC, device paths) are refused on the text,
  and every node tool with a side effect must declare its path handling. Receipts also bind the
  node, the core, the issue time, taint and who decided on the core; `trust_core_for_high` needs
  a person's yes on the core and can be set with `nox node allow trust_core_for_high`. The dialog
  refuses invalid Unicode, warns about foreign content, treats a "yes" within 1.5 s as no and
  stops typing/clicking calls while open; Linux uses zenity only. A dead-man stop lifts after
  three pings from a running core and nodes report their state; the budget survives a restart
  and is charged at the higher risk; arguments are NFC-normalised once for hash, dialog and run.
- **Nodes, external review of wave C part 1-3:** an `origin` default never lands on another
  device when the asking PC's node is offline or not linked; the node port bounds TLS handshakes
  (5 s), waiting connections per node and in total; a node's stop always takes effect while Nox
  runs and repeats while stopped are answered `already_engaged` and audited once; the device of a
  call is its own field in the permission request, scope and audit (a target that merely starts
  like `device:<id>` is just text); node tools can never touch the node folder, `nox node allow`
  needs an interactive terminal and a typed yes, the confirmed manifest is HMAC-signed in the
  audit trail and an edit of `node.yaml` widens nothing; `nox node deny` applies to a running
  node; the node folder is owner-only on Windows; nodes that stop answering pings are not
  offered; notices always say "Nox"; PowerShell by absolute path; a node's grants are revoked on
  unpair even with network access off; unfinished blobs expire.
- **certifi 2026.10.19** (Dependabot alert #40, critical) in `uv.lock` and
  `installer/requirements.lock`. The lock is also re-synced with the minimum versions
  `pyproject.toml` already required (ruff, mypy, playwright, pytest-playwright, typer).
- An installed copy never started: the embedded Python's `._pth` did not name the app folder and
  the code looked for its configuration one folder too high. Shortcuts now start a launcher that
  reports a failed start; `installer/README.md` explains how to repair an old installation.
- High-risk confirmations are never remembered, by the core or the shell.
- **Memory did not start on a fresh server.** The vault folder did not exist yet, the vault watcher
  failed on it, and with it the whole memory extension - Nox would have remembered nothing on a new
  Pi. A missing vault inside Nox's own folder is now created; one elsewhere (a drive that is not
  plugged in) is reported instead of invented, and memory about you works either way.
- **Preset actions and self-extension refused every Linux program path** (`/usr/bin/...`): the
  "absolute path" check only knew Windows paths.
- **The supervisor left a zombie process on Linux** when the core exited after the kill switch.
- **Workers and plugins outlived a crashed core on Linux and macOS**, where no job object exists.
  They now end with the process that spawned them.
- **A server without a screen kept restarting the pet window.** The supervisor no longer starts the
  desktop shell or global hotkeys where there is no display.
- Plugin workers on Linux started without `HOME`, a locale or `XDG_*`; they now inherit those.
- **A calendar invite could stall the server.** A FREQ=SECONDLY/MINUTELY series (anyone can drop
  one into a Google calendar) expanded into hundreds of thousands of events. Sub-hourly series now
  count once, impossible rules are dropped, every series is walked from close to today with a step
  and occurrence cap, the feed's series share a time budget, your single appointments are always
  read first, and a feed that had to be cut says so on the dashboard and to the model (at most 50
  events per answer). The credential store is no longer read on every calendar question, and an
  unavailable one is reported as such instead of "no calendar connected".
- **A reminder could be lost when the voice timed out.** It was marked fired, then the speech
  error aborted the toast as well. The toast now always goes out; a callout that fails outright is
  retried on the next poll (three tries), and a failing reminder loop shows in the health check.
- **Failed PIN attempts could break another transaction** on the shared database connection (the
  same bug class as the audit log in 0.5.0): the counter now uses the database's lock and never
  commits a transaction it did not open.
- **One prompt injection could become a permanent memory.** Facts learned from a phone message or
  from pasted text are now kept unconfirmed - listed on the dashboard, never in the prompt - until
  you confirm them there; recently learned facts are marked "new".
- **Linux installer:** uv is installed from its release archive checked against a pinned SHA-256
  instead of a script piped into a root shell; running the same version again no longer deletes the
  release `nox rollback` points to; the health wait follows the port the core actually bound.
- A bare `pytest` no longer reaches real services or builds the UIs (e2e, network, hardware and
  spike tests are opt-in with `-m`).
- **The phone companion counted every paired device against its limit.** Its device limit, its
  device list and revoking now only see Telegram devices.
- Neither web server trusted `X-Forwarded-For` on purpose, but uvicorn's default did for loopback
  peers; it is off now, so rate limits and logs always see the real client address.


## [0.5.0] - 2026-10-14

### Changed
- **Settings with fixed values are picked, not typed.** Active plugins are checkboxes with a line
  on what each one does, the AI backends a dropdown and an ordered list you move up and down, the
  voice a list of the Piper voices actually installed, and the push-to-talk key is recorded by
  pressing it - shown as "Strg + Alt + Leertaste" rather than spelled. Before, each of these was a
  text box that expected exact ids, one per line. A plugin that is not installed is refused when
  saved, and the AI backends only accept the three Nox has. The presets group has its German name.
- **UI build tooling**: Vite 8, `@vitejs/plugin-react` 6, TypeScript 6.0 and vitest 5.0.2 for both the
  pet window and the dashboard. TypeScript 7 and ESLint 10 are held back on purpose -
  `typescript-eslint` supports TypeScript only below 6.1, and `eslint-plugin-jsx-a11y` does not
  support ESLint 10 yet - and Dependabot is told so, with the reasons, instead of opening pull
  requests that cannot pass.
- **CI lints both UIs.** It ran their tests and builds but never `npm run lint`, so findings of the
  React hooks rules went unnoticed; a stale local install had hidden them too. All of them are
  fixed: state that follows a prop is adjusted while rendering instead of one render late from an
  effect, and no component writes a ref during render any more.

### Added
- **Nox says what it cannot do, and why.** Asked to switch off a light with no Home Assistant
  connected, Nox used to improvise a confident sentence. The tool offer in its prompt now ends with
  a "not available right now" list: every tool family that is present but unreachable or forbidden
  in the current profile, with the reason in words the user can act on ("no calendar is connected -
  paste a private iCal URL on the Settings page"), plus the known gaps (nothing installs software,
  no lock or alarm has a tool). The model is told to say so plainly and name what would make it
  work, instead of pretending or working around it.
- **Nox knows your day.** "Was steht heute an?" used to be answered from Nox's own development
  backlog, the only list it had. Now it reads your calendars - a Google Calendar and a school
  timetable, each as a private iCal address stored in the Credential Manager, never in a file -
  and answers with your appointments (`calendar.today`, `calendar.upcoming`). It also keeps
  reminders: "erinner mich morgen um 9 an den Zahnarzt" is stored in the database, survives a
  restart and is spoken when due, once. The Zuhause page opens with "Dein Tag": today's
  appointments, the reminders (spoken ones greyed), the next few after today, and a field to add
  one by hand. A calendar that cannot be fetched is named in the core's words; no calendar at all
  points to the Settings page instead of showing an empty box. The prompt now carries the weekday
  with the time, so "am Freitag" is arithmetic the model can do. New dependencies: `icalendar`
  (BSD-2) and `python-dateutil` (BSD-3/Apache-2.0) for parsing and recurrence.
- **Nox remembers you.** Asked "was weißt du über mich?" after weeks of use, Nox answered "kaum
  etwas": nothing ever wrote to its memory unless you said "merk dir". It now learns durable facts
  from conversations on its own - people, games, likes, projects, dated plans - a little after the
  conversation goes quiet, and keeps them in every prompt rather than hoping a search finds them.
  Nothing is learned while the privacy mode forbids memory writes. The dashboard shows the list
  under Einstellungen as "Was Nox über dich weiß", where any fact can be forgotten and one can be
  typed in. Retrieved vault notes are now labelled as documents - Nox's own project documentation
  was being paraphrased back to you as "what you told me".
- **"Lass uns streamen" starts the stream.** A spoken mode switch is recognised before the model
  is asked: "lass uns streamen", "wir gehen live", "starte den Stream" switch to stream mode,
  "Stream beenden" switches back. Nox then says what actually runs - "Stream-Modus ist an. Twitch
  und OBS laufen." - or which plugin did not start and why. Questions and statements about
  streaming ("Wann wollen wir streamen?", "Ich will heute nicht streamen") still go to the model.
- **A stalled event loop is reported with the code that stalls it.** When the core's loop does not
  answer for 3 s, `core.loop_stalled` names the module, function and line it is stuck in, and
  `core.loop_resumed` says how long it lasted - instead of a silent gap in the log.

### Fixed
- **Every conversation was answered by the weakest model.** The default chat chain started with the
  local 4B model "because it answers fastest": in a real test it took 4-19 s per answer, claimed it
  could not control devices it has tools for, and invented memories. Claude, set as the preferred
  backend, was never asked. Chat now uses the fallback chain like every other role - Claude first,
  the local model only when Claude is not reachable; Claude answered the same twelve questions in
  4-12 s.
- **The memory index was left half written at every start.** The audit log shares the core's
  database connection but kept a lock of its own, so it committed in the middle of the vault
  index's transaction, whose own COMMIT then failed ("cannot commit - no transaction is active").
  It now takes the database's lock; the same start indexes the vault without an error.
- **The pet's mouth did not move when it spoke.** Speaking stretched the whole muzzle by 3 % on a
  fixed beat, which nobody could see. Every creature now has a jaw below the muzzle that drops in
  an uneven syllable rhythm, scaled by how loud Nox speaks: 5-10 px at the strongest syllable in
  the 260 px window, with the nose left where it is. The photographs have closed mouths, so it is
  a chin that moves rather than lips; real lips would need open-mouth art for each creature.
- **Nox said "Ich bin bereit" while answering with its weakest brain.** With Claude Code's login
  expired, every answer came from the small local model and nothing said so. When the preferred AI
  backend is not available at start, the greeting now says which one answers instead, and how to
  fix it where the reason is known ("Die Anmeldung ist abgelaufen. Starte einmal claude in einer
  Konsole und gib slash login ein.").
- **Switching to stream could never start Twitch or OBS.** Three faults stacked up: the plugins'
  network access was checked against the profile Nox started in, which failed them for good under
  `companion`; a preset's mode switch changed the mode but not the security profile; and
  `plugins.enabled` was empty, so neither plugin was allowed to run in any mode. A plugin is now
  checked against the profile it would run under, every mode switch goes through one function that
  sets mode and profile together, and Twitch and OBS are enabled by default - they still run only
  in stream mode.
- **Nox misheard German and answered in English.** Speech recognition guessed the language for every
  sentence and flipped short German ones to English ("Hallo Nox" -> "Hello Nox"), and the answer
  followed in English; stream words came out as "Lass uns striam". It now listens for German by
  default (`voice.stt.language: de`), `auto` stays German unless the audio is clearly English, and
  a vocabulary list (`voice.stt.vocabulary`: Nox, Stream, Twitch, OBS, Szene, Minecraft, ...) tells
  Whisper which words to expect. Measured on eight German commands with the shipped `small`
  model: 4/8 correct in 1.9 s before, 8/8 in 0.86 s after. The Whisper model and the recognition
  language are now picked from a list in the dashboard instead of typed in.
- **The pet window ignored the creature chosen in the dashboard.** The shell read only the shipped
  defaults and never the user's own `user.yaml`, so it always opened the pet as the plain shape -
  and every other setting it reads, such as the push-to-talk key, was the default too. It now lays
  the user layer over the defaults with the same resolver the core and the supervisor use.
- **The PIN was hashed with PBKDF2 instead of Argon2id on every installation.** The code prefers
  Argon2id, but `argon2-cffi` was in no dependency list, so the log reported the downgrade on every
  start. It is a dependency now, and a PIN stored with the weaker scheme - or with older Argon2
  parameters - is re-hashed the next time it is entered correctly; the audit log records it.
- **`uv.lock` was behind `pyproject.toml`** (starlette, ruff and Nox's own version), and the
  third-party license list had been generated from a stale local install. Both are current again.
- **A self-extension proposal could leave a crashed session's edits in your own checkout.** The
  runner only checked that the coding tool answered, not how the session ended: a session that
  failed was reported as "changed nothing", and its half-written files were carried by
  `git checkout` back onto your branch. A session that did not end is now a failed proposal with
  the reason, and anything it wrote is committed on the proposal's branch, marked unfinished,
  before the checkout is restored.
- **Self-extension never worked against the real Claude Code.** Found by a live run, not by the
  tests, whose fake session answers instantly: every tool call had 30 s, a real session takes
  minutes, so each proposal timed out. Tools now carry their own time budget (`timeout_s` on a
  tool, and per tool in a plugin manifest); the coding session has its 30 minutes and
  `extend.propose` room for the session plus the test suite.
- **The coding plugin's `session_timeout_s` was never enforced.** It stood in the manifest and
  nothing read it, so a session could run without end. It now bounds the whole session, repair
  attempts included; past it the CLI is stopped and the session fails with the reason.
- **A coding session depended on the person's own Claude Code setup.** It loaded their personal
  hooks and permissions, which in the live run refused every edit with a question nobody was
  there to answer. Sessions now read only the workspace's own settings (`--setting-sources
  project`). An empty proposal also passes on what the session said, instead of only "changed
  nothing", and the test result is plain text without terminal colour codes.
- **Self-extension asked twice for one change.** After you confirmed `extend.propose`, the coding
  session it starts asked again. The `coding` profile now lets the core's own `extend` agent start
  that session; a model starting one directly is still asked.
- **The core restarted itself over and over on a cold start.** With the data on a spinning drive,
  single install steps of the boot stalled the core's event loop for 13-16 s while the voice worker
  and the language model were loading from the same drive. The supervisor counted that as ten missed
  heartbeats and killed the core - which started the same disk-bound boot again, so the next one
  stalled too. The heartbeat now says whether the core is still booting, and while it is, a silence
  only counts as a hang once it outlasts `supervisor.boot_grace_s`; a core that has started is
  watched as strictly as before. The Rocket League extension also no longer imports its vision and
  callout modules on the event loop.
- **The database health check stalled the core every 30 s on a busy drive.** It ran a full
  `PRAGMA integrity_check` - every page of the database - under the lock the event loop needs for
  its own queries. Warm that costs 40-150 ms; on a cold spinning drive it held the lock long enough
  for the supervisor to kill the core. The periodic check now only asks whether the database
  answers; the full integrity check still runs once, when the database is opened.
- **The audit-chain health check re-hashed the whole audit log on the event loop** every 30 s. It
  still verifies the whole chain - it is how tampering is noticed - but now in a worker thread, with
  30 s instead of 5 s before a slow drive is mistaken for a broken chain.
- **The process list showed a blank name** for protected system processes such as "Registry",
  whose name Windows does not hand out. They are now listed as "(protected system process)".

## [0.4.0] - 2026-10-01

### Added
- **Seven creatures to choose from, each one complete.** Meereswolf, Eulenfuchs, Katzendrache,
  Mottenkatze, Kaninchenkatze, Koala-Flughund and Chamster, each with a deformation rig - it
  breathes, blinks, flicks an ear, turns its head, and moves its tail or wings - and its own art for
  sleeping, lying down and eating. They come from the original creature concepts; the key poses were
  generated against each creature's own picture on a green screen and keyed out.
- **The creature is picked from a list in the dashboard**, with a picture of the one you chose under
  it, instead of a text box that expected you to know to type `sprite:koalaflughund`. It applies
  without a restart. A configuration written before the list existed still loads; an unknown value
  falls back to the plain shape and says so in the log.
- **Tools to add a creature**: `ui/pet/scripts/cut_out.py` keys a generated picture off a
  checkerboard, a flat colour, a lit studio wall or a chroma screen; `make_variant.py` turns the
  cut-out and a landmark file into a whole rigged variant; `ui/pet/scripts/README.md` walks through
  it, including how to ask an image generator for pose art it can actually use.

### Fixed
- **The pet turned into another animal when it fell asleep.** The Meereswolf's base picture was a
  wolf and its three pose pictures were a hamster, so sleeping cross-dissolved one creature into the
  other on screen. Nothing objected: every file was valid. The hamster is now its own creature, the
  Chamster, and a test reads the variants off disk and checks each one is internally consistent.
- **No creature ever lay down when bored.** The pose table looked the mood up under a key that only
  ever held the activity, so the lying-down art was unreachable. The user guide had been promising
  it all along.
- **A pet that started asleep sat up first**, for a quarter of a second, before curling up: the first
  pose was dissolved into from a frame that was never drawn.

## [0.3.0] - 2026-10-01

### Added
- **The default local model could not drive Nox's own tools, and now one that can is the default.**
  `scripts/probe_models.py` asks every model Ollama has the real question - the real offer text, the
  real parser from `nox.ai.tooluse`, four cases including one where the right answer is *not* to
  reach for a tool. `llama3.2:3b`, which was the shipped default, hit 1 of 4 at 14.6 seconds an
  answer: it writes a friendly paragraph where a tool directive belongs, so the local fallback could
  talk and do nothing. `qwen3:4b-instruct` hits 3 of 4 at 2.9. Measured on the development machine
  (reference machine, 6 GB GPU), and the probe ships so anyone can measure their own.
  [`docs/LOCAL_MODELS.md`](docs/LOCAL_MODELS.md) has the table and two findings behind it: a bigger
  model is not the win, because the larger open models reason before answering and an 8B that fits
  this card takes 32.7 seconds; and "does not fit the VRAM" is survivable while "does not fit the
  RAM" is not - a model streamed from an SSD needs every layer for every token.
- **Nox can propose a change to itself, and cannot apply one.** `extend.propose` starts a branch in
  a checkout you named, asks the `coding` plugin to write the change there, runs your test command,
  and hands back the diff and what the tests said (`src/nox/extend/`). There is deliberately no
  `extend.apply`: merging a branch and restarting Nox on the result are things a person does with
  their own git, which is what keeps "Nox can extend itself" from meaning "Nox can change what it is
  while you are not looking". `extend.workspace` ships empty, so out of the box there is no
  repository it may touch and the tool says which setting to fill in. High risk, so every profile
  asks first - and the confirmation shows the *intent*, because "add a tool for reading my calendar"
  is a question a person can answer and an identifier is not. The git surface is four verbs with the
  absences as the point: no merge, no push, no reset, no branch deletion, and the user's checkout is
  put back on *every* path out, including the ones that raised - which is tested against a real
  temporary repository rather than a mock, because a mock would have granted that for free.
- **The installer offers a desktop shortcut**, ticked by default. It made a start menu entry and an
  optional autostart, which means the first thing somebody does after installing - look for it - only
  worked if they already knew it was called Nox.

- **The creature curls up, lies down and eats.** The key-pose path shipped working and the art for
  it did not exist, so `poses` in the Meereswolf rig was an empty object and every pose request
  quietly did nothing. Three cut-outs now fill it: it curls up when it sleeps, lies down when it is
  bored, and eats when you give it a treat - shift-click it, or Shift+Enter while it has focus, so
  the second interaction is keyboard-reachable for the same reason the first one was made to be. A
  treat lifts affection and mood more than a pat does and lifts the one thing a pat cannot, energy.
  Making the art usable took two fixes that were only visible by watching the pet: a key pose is a
  *different picture*, not a deformation of the base one, so it is no longer skinned - the mesh's
  bone weights describe the base photograph, and turning the head bone loose on a curled-up body
  smears the drawing instead of animating it. And the eyes, which are separate layers placed by the
  skeleton, now fade exactly as the base drawing does; at full alpha over a curled body they were
  two eyes hanging in the air above it. `make_pose_frames.py` prepares a cut-out for the rig: it
  downscales in premultiplied alpha, because averaging raw colour across a transparent edge drags
  the colour of nothing into the silhouette and leaves a pale halo, and it shifts each pose down
  until its lowest pixel meets the base drawing's - which is what makes the dissolve read as the
  creature lying down *where it was standing* rather than sinking through the floor.
- **Nox can type into a window, and the boundary around that is narrower than the request.** The
  input-synthesis prohibition was "no file anywhere may import one of these APIs". It is now
  window-scoped, as asked: `desktop.type_text` brings one window to the front, checks it really got
  there, and types - high risk, so every profile asks first. `SendInput` lives in exactly one file
  (`src/nox/desktop/keyboard.py`) and that file refuses to send anything at all while a watched game
  process is running *anywhere on the machine*, which is stricter than refusing the game's window:
  Rocket League's boundary is not only about where input lands, and a process that observes a game
  and synthesises input is the shape an anti-cheat is right to distrust. The cost of being wrong
  there is an account, not a failed request. It types characters (`KEYEVENTF_UNICODE`), which cannot
  express Alt+F4 or Ctrl+A - the damage in synthetic input is almost never in the letters. There is
  no mouse input, and `game.input.send` stays in the immutable hard-prohibition list. The CI guard
  was narrowed rather than relaxed: process-memory calls and the automation libraries are still
  forbidden everywhere, `SendInput` must appear in that one file and nowhere else, *and* that file
  must still contain its game guard - an exemption nobody re-checks is a hole. The guard caught its
  first thing immediately: this module's own docstring, which had named the forbidden APIs in prose.
  A grep cannot tell a mention from a call, so the prose gave way. Not one test sends a keystroke;
  every one of them asserts something that did not happen.
- **Nox can draw, and it draws data rather than code.** `view.show` puts a table, a bar chart, a
  line chart, a list of facts or a plain note on a new Board page in the dashboard
  (`src/nox/views/`, `ui/dashboard/src/pages/Board.tsx`). The tempting design is to let the model
  write HTML or SVG and put that on the page, which is an injection hole with extra steps: the
  dashboard would be rendering whatever a language model was talked into producing. So the model
  never sends markup. It picks one of five shapes, fills in values, and the page owns the rendering -
  a bar is a div with a width, a table is a table, text is text, and React escapes all of it. The
  shapes are a discriminated union with `extra="forbid"`, so a table sent with a `bars` list is
  refused rather than silently stripped, a row that does not fit its columns is named, and a line
  chart with more values than labels is named too. Every list is bounded, because a view is
  something a person looks at and four hundred rows is not that. The wire input is deliberately
  flat: the tool offer in the prompt lists argument names, not nested schemas, so a single `view`
  object would have shown up as `view: object` and the five shapes would have been invisible to the
  model. The strictness lives one layer in, which is also what lets the tool say *which* field was
  wrong - the executor answers a failed input validation with a generic "invalid input" on purpose,
  since it must not put user content in a log. The `view.shown` event carries the title and the kind
  and not the view, and the page re-reads the board, the same way the Settings page re-reads its
  snapshot after `settings.changed`. The charts are plain CSS and one inline SVG: a charting library
  would be a dependency, a bundle and a theme to argue with, for five shapes that are a div and a
  polyline.
- **Nox can see what is running and tidy up after you.** `desktop.windows`,
  `desktop.processes`, `desktop.window_focus/minimize/restore/close` and `desktop.process_stop`
  (`src/nox/desktop/`). A model does not know window handles, so the window tools take a title
  fragment - and the case that matters is several matches: "close Chrome" with four Chrome windows
  open reports the candidates and changes nothing, because a guess closes the wrong window and the
  user cannot tell a guess was made. `window_close` posts `WM_CLOSE`, which is what the X button
  does: the program is *asked* and may still offer to save, and a true answer means asked, not gone.
  `process_stop` is the other half of that pair and says so in its own description, because a model
  picking the wrong one of the two costs the user work; it never escalates to a kill when a
  terminate is ignored, since a program that will not go is either busy saving or stuck and guessing
  which is not the code's job. `window_focus` reports whether the window actually ended up in front
  rather than whether the call returned - Windows refuses `SetForegroundWindow` in cases this
  process cannot influence, and saying it worked would be a lie. Three things are out of bounds
  entirely, in code rather than as a risk level: Windows' own session processes, Nox itself
  (including its workers - a tool that can kill the process holding the audit log is a kill switch
  with no record), and the game, because observation-only is not only about input. The gap rows for
  process and window control came out of the capability table, and the remaining desktop gaps were
  renamed to the names their tools will have, so the staleness check can catch the next one.
- **Nox can work with your files, in the folders you name and nowhere else.** `file.list`,
  `file.read`, `file.write`, `file.move` and `file.delete` (`src/nox/files/`), bounded by a
  `files.roots` list that ships empty: a fresh installation reaches nothing outside its own vault,
  and the tools name the setting to add a folder to instead of failing oddly. An empty list means
  nowhere, never everywhere - the other reading is how a guard meant to restrict ends up permitting.
  Paths are resolved *before* they are checked, so `..`, symlinks and Windows junctions are all
  checked against where they really lead; the junction test creates a real junction rather than a
  mock. Writing asks first in every profile that has not said otherwise, never replaces a file
  unless told to and never invents a missing folder, because a typo would otherwise build a tree
  nobody looks in. Reading refuses anything that is not text and caps the size, since a file read
  becomes part of a prompt. Deleting goes through the Windows shell with `FOF_ALLOWUNDO` - the
  Recycle Bin, where it can be got back - and there is no fallback to `os.unlink`: if the shell
  refuses, the file stays. Turning the setting off removes the delete tool rather than making it
  permanent. Each tool reports its path as the permission target, so *writing is fine under
  Downloads, ask me anywhere else* is a profile rule and not a code change. See
  [`docs/FILES.md`](docs/FILES.md).
- **Nox can say what it cannot do.** A capability catalogue (`src/nox/capabilities/`) joins three
  things that were never asked together: which tools exist, what the active profile would decide
  about each of them, and what is missing entirely. Four answers come out of that and stay apart,
  because collapsing
  them is how an assistant ends up claiming things that are not true: a tool can exist and be
  forbidden here, be allowed and unreachable for want of a token, or simply not be built. Asking
  the permission engine for all of that leaves no audit entry, via a new `preview()`; recording
  dozens of decisions for an action nobody took would bury the log it exists to explain. Boundaries
  are marked `forbidden` rather than `missing`, so a door lock does not read like a backlog item,
  and the build fails if a row outlives the capability it describes. `capabilities.check` has a
  fourth answer most assistants leave out: "I have no entry for that", which is not a no.
- **Saying it now does what clicking it does.** The orchestrator had no idea tools existed: it
  streamed text, and the tools Nox owns were reachable only from the dashboard or a preset, so
  "mach das Licht an" produced a sentence about lights and nothing else. Neither provider can be
  given tools natively - the Claude Code CLI runs with `--tools ""` on purpose, because its own
  file and shell tools would sit outside Nox's permission engine entirely - so the request travels
  as one line of text, with the guards where the risk is. A directive counts only as the whole
  answer, and prose followed by one runs nothing and is logged rather than swallowed. The stream is
  held back at the head of every tool-enabled turn so the protocol is never read aloud, including
  when the model keeps asking after the offer was withdrawn. What is offered comes from the
  capability report under the same agent the call will use, and an integration test walks every
  offered tool through the real permission engine to prove none would be refused. The dashboard's
  chat shows which tools an answer used, because "Nox changed something on your machine" does not
  belong only in a log file. Three rounds, then the offer is taken away and the model has to answer.
- **Work that outlives the conversation it started in.** A plan (`src/nox/plans/`) is a few tool
  steps with a title, written down so a person can read them before they run - no command lines, no
  conditions, no loops, no nesting. `plans.propose` writes nothing and is low risk; `plans.start`
  is high risk, so every profile falls through to a confirmation, and that confirmation shows the
  plan's *title* rather than its identifier, because "Start: Downloads sortieren" is a question a
  person can answer. Approving in the dashboard needs no dialog - the click is the dialog - and
  either way every step still meets the permission engine. Plans run on the existing task queue, so
  they survive a restart and pause while a game is running. The step being attempted is written to
  the checkpoint *before* the call: a step is a tool call with side effects, so a plan that comes
  back from a crash records that step as interrupted and stops rather than repeating it. A visible
  gap beats an invisible repeat.
- **The desktop pet is a creature, not a photograph.** A 2D deformation rig (`ui/pet/src/rig/`)
  runs a triangulated mesh over the pet's artwork, driven by a small bone hierarchy (root, body,
  chest, neck, head, muzzle, ears, gill fins, tail), so it breathes, tilts its head, flicks one ear
  and sways its tail continuously instead of cutting between a handful of near-identical stills.
  WebGL where it exists, Canvas 2D where it does not - and the downgrade is logged, not hidden. A
  variant opts in by shipping `variants/<id>/rig.json`, a hand-editable file validated at load with
  errors that name the field; a variant without one keeps its static frames, and any error loading
  a rig falls back to exactly that path.
- **The Meereswolf blinks for real.** Its eyes are now separate alpha-masked layers cut out of the
  source photograph (`ui/pet/scripts/make_pet_layers.py`), with the sockets behind them filled with
  surrounding fur, so a blink is an eyelid closing over an eyeball rather than a dip in brightness.
  Ten clips are authored against the picture - `breathe`, `sleep_breathe`, `blink`, `ear_flick`,
  `head_tilt`, `head_turn`, `tail_sway`, `perk`, `speak_idle` and a `startle` the kill switch
  triggers - blended additively, so a blink can start mid-breath without the breath skipping. The
  moods in `petState.ts` are carried as held postures: ears back when it is frightened, low and
  heavy when it is sad. `prefers-reduced-motion` leaves breathing and the mood, nothing else, and a
  hidden window stops the loop and rebases its clock rather than replaying the lost time.
- **`rig.json` can name key poses** (`sit`, `lie`, `curl`, `eat_0..n`) that the player
  cross-dissolves to while the mesh keeps running. The path ships working; the art for those poses
  does not exist yet, so they are reported as missing rather than faked.
- **`?animate=1`**, a dev-only companion to `?still=1`: the same state presets with the animation
  loop left running and a fixed ambient schedule, so `ui/pet/scripts/render_rig.py` can film a
  rig's motion and two review renders can be compared frame for frame.
- One palette for both front-ends: the dashboard's Apple-style token block now lives in
  `ui/shared/tokens.css` (same names, same values, same `prefers-color-scheme` +
  `data-theme` guards) and the pet window uses it instead of its own dark-only colours. The pet
  therefore follows the OS between light and dark — chips, notes, the status label and the
  capture indicator all do — while the window itself stays transparent, and each creature variant
  finally uses the body lightness it already shipped for the active background.
- The pet can render a **sprite variant**: `pet.variant: sprite:<id>` loads
  `variants/<id>/sprites.json`, validates it, preloads every frame and plays the frame sequences
  the existing state machine selects (idle / listening / speaking / thinking / sleeping, plus
  optional blink), with a 150 ms crossfade between expressions, idle breathing as a subtle
  scale/translate, `prefers-reduced-motion` honoured, and a logged fallback to the procedural
  variant when a manifest or a single frame fails to load. A generated placeholder set ships under
  `variants/placeholder/` so the path works end to end before the real art exists.
- `config.set pet.variant` takes effect without restarting Nox: the shell reloads the pet page with
  the new variant on `settings.changed`, and the pet page subscribes to the same event so it can
  swap the variant in place once the core exposes the value to the `pet` role.
- Kokoro (`kokoro-onnx`) as a second text-to-speech engine next to Piper, selectable with
  `voice.tts.engine: kokoro` and the new optional extra `voice-kokoro`. Same `TtsEngine`
  contract as Piper: sentence-wise streaming, synthesis off the event loop, reported sample rate
  (24 kHz), prepared clips. Model files are resolved from `<data_dir>/models/kokoro/` and are never
  downloaded automatically - a missing file is an `unavailable` health reason that names the path
  and the command, `python -m nox.worker --download-kokoro` (~354 MB, user-initiated, no core and
  no egress guard involved). The default stays `piper`.
- Wake-word gate in front of Whisper: `voice.stt.wake_word_engine: openwakeword|text`,
  `wake_word_model`, `wake_word_threshold`, `wake_window_s` and `conversation_window_s`. While
  push-to-talk is not held and no conversation window is open, a speech segment only reaches
  Whisper if the cheap always-on detector fired - background audio (a video, a game) is dropped
  before transcription instead of filling the queue with 20-40 s of latency and inventing
  languages in noise. Audio the user did not direct at Nox is now never transcribed at all.
- `voice.stt.listening_mode: continuous|ptt_only`. `ptt_only` never opens the microphone
  unless push-to-talk is held; `continuous` stays the default.
- `voice.stt.kill_phrase_watchdog` / `kill_watchdog_max_ms`: because no acoustic model for
  the kill phrase exists, short segments still reach Whisper so "Nox Notaus" keeps working. Such a
  transcript is checked for the kill phrase and then discarded - it never becomes an event and
  never reaches a language model.
- `voice.models_dir`: one root for the Piper, Kokoro and openWakeWord model files (empty =
  `<paths.data_dir>/models`).
- New read-only `security.pin.status {}` -> `{configured}` (roles `shell`/`dashboard`): presence
  only, never the PIN, its hash, its length or its algorithm. The core has required a `pin` on
  `secrets.set`/`secrets.delete` ever since a PIN is configured, but the Settings page never sent
  one, so every credential change failed with "permission denied". The page now shows an inline
  PIN field next to Speichern/Löschen when a PIN is configured, sends it with exactly that one
  request, clears it afterwards and stores it nowhere, and turns a PIN refusal into the honest
  `pin_required`/`pin_wrong` message instead of a raw error line.
- `nox.proactive.store.NotificationStore` is now backed by a `proactive_notifications`
  table (migration `0010_notifications`) when constructed with a database, so notification history
  survives a restart; `db=None` keeps the original in-memory ring buffer for tests. New
  `NotificationRepository` (`nox.data.repos`). New `proactive.notification.dismiss {id}` tool,
  persisted like everything else in the store; expired/dismissed rows are pruned by
  `NotificationRepository.purge_expired` (module-level `DEFAULT_NOTIFICATIONS_RETENTION_DAYS = 30`
  default; adding a matching `proactive.notifications_retention_days` to `ProactiveConfig` is
  tracked as follow-up work).
- Settings are writable, not just readable. New `nox.settings` package with
  `config.get`/`config.set` over an explicit allow-list of configuration paths whose types,
  options and bounds are derived from the pydantic models in `nox.core.config`; values are
  validated before anything is written, written to the User layer (`user.yaml`) by the same
  writer `nox onboard` uses, live-applied where that is genuinely possible and honestly reported
  as `restart_required` otherwise. New `settings.changed` event (paths only, never values).
- `secrets.status`/`secrets.set`/`secrets.delete`: manage the six known credential names without
  ever showing a value. Rate-limited, PIN-gated when a PIN is configured, audited by name.
  There is deliberately no `secrets.get`.
- Twitch login by OAuth 2.0 Device Code Grant (`twitch.auth.start`/`status`/`disconnect`, event
  `twitch.auth.changed`) instead of pasting an `oauth:` token: a public client, `chat:read
  chat:edit` only, tokens straight into the keyring, every call through the egress guard, with a
  background refresh so a long stream does not die of an expired token. `id.twitch.tv:443` is
  allow-listed in the defaults and in the `companion`/`stream` profiles, and documented in
  `docs/PRIVACY.md`.
- The personality moved out of the source tree: the repository ships a neutral default and each
  installation keeps its own `<data_dir>/personality.md`, created on first start, re-read on
  every change, and editable through `personality.get`/`personality.set`. The operating rules
  stay in code.
- `stream.twitch.channel` in the configuration, so the Twitch channel is an editable setting; the
  plugin still prefers an explicit `channel` in its own manifest.
- License decided: Apache-2.0. `LICENSE` (full text) and `NOTICE` added,
  `LICENSE-PENDING.md` removed; `pyproject.toml` and both `ui/*/package.json` declare
  `Apache-2.0`. The optional `voice` extra pulls in GPL-3.0 components, so a bundled build
  (e.g. the installer) ships under GPL-3.0 terms as a combined work - stated in `NOTICE` and
  accepted as a documented exception in `docs/license_policy.yaml`.
- Governance and community files for a public repository: `CONTRIBUTING.md`,
  `CODE_OF_CONDUCT.md`, root `SECURITY.md`, `.github/CODEOWNERS`, `.github/dependabot.yml`,
  pull-request and issue templates, `docs/PUBLISHING.md`, `scripts/github_branch_protection.ps1`
  and `scripts/check_links.py`.

### Changed
- The core's composition root is a composition root again. `src/nox/app.py` went from 1238 lines
  with a 296-line `start()` to an ordered list of named boot steps; the steps themselves live in
  `nox.core.boot` (`persistence`, `ai`, `workers`, `extensions`), the IPC payload models and
  handlers in `nox.ipc.handlers.core`, and the process entry points in `nox.entrypoints`. Every
  component is declared up front, so shutdown can tell "was never built" from "would not stop" and
  names the component in the log either way.
- The configuration is a package (`nox.core.config`) split by area - installation, security,
  assistant, features, loader - instead of one 1019-line module. Every public name is re-exported,
  so no import site changed.
- Paths below `paths.data_dir` are derived from it. Setting `data_dir` now really does move the
  vault, index, database, cache, backups, runtime directory and logs with it; each can still be
  pinned individually. The `<derived>` placeholder, which contained characters Windows does not
  allow in a path, is gone.
- `security.*` and `ipc.*` settings reach the services that use them as typed models rather than
  as dictionaries, so a renamed key is a type error instead of a silently missing feature.
- Environment references in paths (`${VAR}`, `%VAR%`, `~`) are expanded once, during validation,
  and an unresolvable one is an error naming the variable - never a directory called `${APPDATA}`.
- Configuration layers are validated once each while loading, instead of up to five times.
- `nox onboard` runs in one language. The interface language is the first question and everything
  after it - prompts, hints and the closing summary - follows it. The summary now also says what to
  do next: the start command, where the dashboard is, and that the first vault index takes about a
  minute.
- `nox onboard` recommends connecting Twitch in the dashboard, where the login is a code in the
  browser, and offers typing a token by hand only as the explicit alternative. Both paths store the
  same credential names the Settings page manages.
- `nox doctor` probes the model providers through the egress guard, like the core does, instead of
  opening an unguarded client. Its report goes through one writer, so a caller can capture it.
- `nox --help` and the shipped configuration are written for the person reading them: the internal
  ticket and specification identifiers are gone from `config/defaults.yaml`, `config/profiles/*`
  and the command help, replaced by the constraint each one stood for.

- Every extension (`sensors`, `memory`, `health`, `proactive`, `pm`, `rl`, `clips`, `creative`,
  `remote`) now follows one convention: `install(core)` returns a runtime object with `stop()`,
  which the core holds in its extension registry. Extensions no longer graft attributes onto the
  core object, and the runtimes carry the health checks for what they own.
- `nvidia-smi` is probed in a worker thread with a timeout instead of blocking the event loop for
  up to three seconds on every resource poll, and it is looked up on each call, so a driver
  installed while Nox runs is noticed without a restart.
- Wake-word detection runs in a worker thread over batched frames, and transcription runs one
  utterance at a time, so neither competes with the event loop that carries IPC and heartbeats.
- Output devices are resolved once and cached instead of enumerating PortAudio before every
  utterance; the vault's initial directory walk and the WAV decoding in `stt.transcribe` moved off
  the event loop as well.
- Vector search fetches its mapping rows in one query instead of one per hit, off the loop.
- Shared helpers replace duplicated code: `nox.util.aio` (awaiting a maybe-coroutine, closing an
  async iterator, waiting on a stop event, one poll loop), `nox.util.proc` (process creation flags,
  binary lookup), `nox.worker.heartbeat` (one heartbeat loop for both worker kinds) and
  `nox.plugins.reconnect` (one reconnect backoff with logging for all three transports).
- A worker process exits non-zero after an abnormal end, so the supervisor can tell a requested
  shutdown from a crash, and a worker that cannot measure its own CPU load omits the field instead
  of reporting a perfectly idle process.
- `ProviderInfo` carries a `display_name_de` next to its English `display_name`, so the German
  user interface can show "Regeln (Offline-Notfall)" instead of "Rules (offline fallback)". An
  empty value means the name is the same in both languages (product names are not translated).
- Internal story, spike, requirement and specification identifiers were removed from comments,
  docstrings, manifests and user-visible strings across the voice, worker, plugin, AI, memory,
  sensor, clip, creative, project-management, remote, stream, health and shell packages. Comments
  now state the constraint rather than pointing at a document the reader does not have.
- Voice model and clip directories are derived from the data directory instead of being hard-coded
  absolute paths in the engine modules. **Action required for existing installations whose
  models are not below `paths.data_dir`:** set `voice.models_dir` in `user.yaml` (or move the
  `piper/`, `faster-whisper/` folders), otherwise the engines look under
  `%APPDATA%\Nox\models` and report their models as missing.
- `voice.tts.engine` is validated against `piper|kokoro` instead of being a free-form string.
- `openwakeword` was added to the `voice` extra; clip handling shared by both TTS engines moved to
  `nox.voice.tts.clips`.
- `NOTICE` and `docs/license_policy.yaml` record the honest license status of the Kokoro route:
  Kokoro's own code, ONNX Runtime and the model files are permissive, but `kokoro-onnx`
  still depends on `phonemizer` and espeak-ng (GPL-3.0-or-later), so switching to Kokoro moves the
  GPL obligation on bundled builds rather than removing it. Reaching an Apache-2.0-only installer
  needs a permissively licensed grapheme-to-phoneme front end and remains an open decision.
- `stream.twitch` gained `bot_names`, `relevance_cooldown_s`, `rate_limit_max_messages`,
  `rate_limit_window_s`, `rate_limit_min_gap_s`, `min_backoff_s` and `max_backoff_s` — typed,
  validated, with the manifest's own values as defaults — and all of them are editable on the
  Settings page with de/en labels. The plugin reads them from the configuration
  (`nox_plugin_twitch.settings.resolve_settings`); a manifest that still carries one of the moved
  keys keeps working for one release and logs `twitch.manifest_config_deprecated`.
- Repository prepared for publication: `README.md` rewritten for readers who do not know
  the project, and all personal data (names, e-mail addresses, employer references,
  machine-specific paths) replaced with placeholders across config, plugins, tests, spikes
  and docs.
- Dashboard redesigned for clarity: design system in `styles.css` with light and dark palettes
  (system preference plus System/Hell/Dunkel toggle, `nox.theme`), sidebar navigation, cards,
  status pills, chat bubbles, macOS-Settings-style grouping. ARIA structure, i18n keys and the
  e2e selectors are unchanged.
- Rocket League coach, Stage 1 — observation-only detection, replay pipeline; config scaffolding
  (`rl:` section) landed, feature work ongoing.
- v1.0 public release readiness: license audit, secrets scan, onboarding wizard CLI,
  documentation set, external security review readiness — this changeset.

_No git tags exist yet for the pre-publication history; they will be created at the first tagged
release (`docs/PUBLISHING.md`) - until then the compare links below point at tags that don't exist
yet either._

### Fixed
- **CI has been failing since 2026-09-28, and not for any of the reasons the open dependency PRs
  were blamed for.** Every one of #45-#62 died the same way: `Windows fatal exception: access
  violation` in `security/audit.py`, from the queued audit writer's thread. The writer shares the
  core's SQLite connection under its own lock, and both `NoxCore.stop()` and a unit fixture closed
  that connection while the thread was inside a statement - which `sqlite3` does not raise on, it
  faults. Both are fixed; the suite now runs to completion in CI (1982 passed) instead of dying at
  78%.
- **A supervisor test asserted on a process record the instant the process disappeared**, which are
  two different moments. It passed locally and failed in CI - the same shape as the restart test
  beside it, and fixed the same way.
- **The Meereswolf no longer has a black outline on a light desktop.** Its cut-out had been stored
  with premultiplied colour under a straight-alpha flag, which left every soft fur edge too dark by
  its own alpha - mean border luminance 0.102 against fur at 0.352. The matte is now repaired
  reproducibly (measure the background off the transparent pixels, erode two pixels, divide the
  colour back out, re-feather, lift what a second background grey left behind) and the border sits
  at 0.378, matching the fur it belongs to. The creature also carries a contact shadow in light
  mode, because a white wolf on a white desktop is only 2.7:1 whatever the edge does - that part
  needs a new source image, and `ui/pet/scripts/README.md` states what one has to provide.
- **Eleven of the Meereswolf's twelve frames were the same photograph.** `speaking-0` was
  byte-identical to `idle`, `thinking-0` to `thinking-2`, and `blink` was the idle frame dimmed by
  3 %, so the pet's "blink" read as a monitor flicker and its "sleeping" had both eyes wide open.
  They are gone, and the manifest now advertises only the one drawing that exists; every state it
  used to claim is produced by the rig instead.
- `security.pin_required_for_security_changes` does something. It was declared in the configuration
  and read by no code at all. With a PIN configured, a change that *relaxes* protection now has to
  carry it: leaving a stricter privacy mode, switching a capture device back on, or editing a
  `security.*` / `privacy.*` setting from the dashboard. Making Nox stricter never asks, so a
  forgotten PIN can never trap it in an open state.
- A broken audit chain no longer boots into normal operation. Verification is checked properly - the
  result used to be read with a default of "fine" - and a failure engages the kill switch, so Nox
  starts in safe mode, denies every action with a side effect, says so on screen, and needs the PIN
  to leave. Boot verifies forward from the last verified position instead of rescanning years of
  entries.
- A plugin can no longer take over the voice service. `worker.register` and `worker.ready` are
  restricted to spawned workers, and a worker may only claim the service its one-time token was
  issued for. Plugins also no longer receive raw transcripts, model responses, memory events or
  chat text over the event stream - the event boundary now matches the state boundary that was
  already in place.
- Only the process the supervisor actually spawned can claim the `core` role or send heartbeats.
  The control token is shared with the shell, so any holder of it could previously become the core
  connection, intercept the kill switch and keep the watchdog quiet with forged heartbeats. An
  unauthenticated connection now also has an authentication timeout and a frame budget.
- Permission checks and outbound requests no longer write to the database on the event loop. Audit
  entries are handed to one writer thread that preserves their order; nothing is dropped, and a
  full queue makes the caller write its own entry rather than losing it.
- The dashboard's stream page could never load. `stream.session.status` and `stream.funken.top`
  were registered for the UI roles but missing from a second, parallel allow-list that had to agree
  - so every call was refused before the handler ran. A handler's own roles are the authority now.
- A profile rule scoped to a directory is no longer bypassed by `..` in the target, and a temporary
  grant for project `a` no longer covers every name starting with `a`.
- A PIN lockout survives a restart. The counter lived in memory only, so restarting the core - which
  the supervisor does on request - reset it after five attempts.
- When Argon2 is unavailable, Nox says so at startup and in the PIN error instead of reporting
  "wrong PIN" for a PIN that may well be right.
- `[::1]` and other IPv6 entries in an allow-list are parsed correctly instead of being read as host
  `::` port `1` and silently matching nothing. An unparsable entry is now rejected when the
  configuration is read.
- Nested values in an audit entry are redacted at every level; a secret one level down used to be
  stored verbatim.
- `GET /health` no longer discloses the vault path to unauthenticated local processes, and reports
  whether the session token file could be restricted to the current account.
- The kill switch is audited before its stop hooks run, so a failing hook cannot cost the record
  that the kill happened; the report says when it is incomplete.
- `nox voice` says why it is unavailable instead of vanishing from the command list, and a failing
  Twitch token refresh is logged and reported as limited instead of retrying silently forever.
- `nox rl calibrate` stores its calibration with the rest of Nox's data instead of in a fifth,
  hard-coded location under the home directory.
- The supervisor no longer blocks its own event loop for up to two seconds while terminating a
  process tree, and no longer raises when a process exits while it is being killed.
- A client can no longer grow the hub's inbound event queue without limit, a request that fails
  inside the hub is always answered instead of leaving the caller to time out, and the hub keeps a
  reference to its own background tasks.

- Screenshot capture in creative apps no longer proceeds when the active security profile cannot
  be read: an unreadable security engine is refused, with the reason, instead of being treated as
  "the Work profile is not active". The same rule now applies to the creative-app mode switch.
- Memory embeddings are only requested through the egress guard. Without a guard, the embedding
  provider is not built at all and the `memory.embeddings` health check reports `unavailable` with
  the reason; note text never leaves the process unguarded, and search falls back to full-text.
- A `/privacy` command from a paired phone now reports what actually happened: a privacy switch
  that failed answers "Privatsphäre-Modus nicht geändert" instead of a mode the system never
  entered.
- A failed security-profile switch on entering or leaving Rocket League mode is logged and named
  in the `system.mode_changed` reason ("security profile unchanged") instead of being swallowed
  while the mode change claims the game profile is in force.
- `rl.callout` is published only for a callout the user actually heard. A missing or failing
  speaker is logged; it no longer produces a transparency entry with a measured latency for audio
  nobody played.
- `listening_mode: ptt_only` now keeps the capture device closed until push-to-talk is held, and
  closes it again on release, instead of holding an open microphone stream (and the operating
  system's microphone indicator) for the whole session and discarding the frames.
- A failing microphone capture loop is reported: the voice pipeline's new capture health check
  says `unavailable` with the reason instead of leaving the pipeline permanently deaf while it
  still reports itself as running.
- Every `tts.speak` ends in exactly one terminal event. A playback error outside the expected set,
  and a say-task that dies in the worker, now emit `tts.finished {ok: false, reason}` instead of
  leaving the caller waiting forever.
- Barge-in is no longer lost in the moment between `tts.started` and playback, and a `tts.stop`
  that arrives while an utterance is still queued now cancels that utterance instead of being
  cleared when it finally starts.
- A plugin secret is refused when the access cannot be written to the audit chain, instead of
  being handed out with no trace.
- A plugin worker no longer inherits the core's entire environment; it gets a minimal, explicit
  set (interpreter, home and temp directories, and the two `NOX_*` variables).
- A plugin's own `src` directory is appended to `sys.path` instead of prepended, so a plugin
  shipping `src/asyncio.py` or `src/nox/` can no longer shadow the standard library or the core.
- The Rocket League plugin's kill-switch handler closes the capture gate and requires an explicit
  resume; cancelling the capture loops alone let the next `game.detected` restart capture without
  any new consent.
- A failed coding-session run keeps its captured stderr instead of cancelling the reader, and a
  CLI process that survives `kill()` is logged with its pid rather than leaked silently.
- The OBS, Twitch and Telegram clients log connection failures (the first of a streak and every
  attempt at the backoff ceiling). A wrong password no longer retries forever in complete silence.
- The Telegram client no longer chains the original httpx exception, whose message carries the bot
  token in the request URL, into the error it raises.
- The AI router's fallback event names the providers left *after* the failed one, instead of
  offering the provider that just failed as its own fallback.
- The degraded-mode service ignores its own `system.level` announcement instead of relying on a
  constructor argument not to list it.
- A note written while the first vault scan is still running is no longer deleted from the index
  again: the scan's pruning pass now only removes entries the index already held when it started.
- A sensor whose poll raises keeps polling and reports itself as `limited` through the new
  `sensors` health check, instead of ending its task in silence while the status tool serves a
  frozen sample as if it were live.
- `nox.settings.layers.write_user_config` — the one writer behind `config.set` and `nox onboard` —
  round-trips the User layer through `ruamel.yaml` instead of `yaml.safe_dump`: comments, key
  order and quoting style survive a settings change, and only the keys in the patch change. What
  is written stays plain YAML; every reader still parses it with `yaml.safe_load`. A `user.yaml`
  that does not parse now raises `UserConfigError` and is left untouched instead of being quietly
  replaced by a fresh document. New core dependency `ruamel.yaml` (MIT).
- The Stage 1 HUD `_recognize_loop` (`plugins/rl/src/nox_plugin_rl/plugin.py`) captured the
  screen on a timer regardless of privacy state, unlike Stage 2's `_vision_loop`, which already
  stops while a privacy zone is active or `privacy.capture.screen` is off. New
  `nox.rl.capture_gate.CaptureGate`: a `privacy.capture_changed`-driven gate (reusing
  `PrivacyService.effective_capture()`'s `screen` boolean - already accounts for
  zone/mode/capture-flag/panic/safe-mode) meant to back both loops so neither hand-rolls its own
  privacy check; logs `rl.capture_paused reason=...` / `rl.capture_resumed` exactly once per
  transition and guarantees a paused loop grabs no frame at all via `maybe_capture()`.
- `ProactiveService.__init__` used `store or NotificationStore(...)`; since `NotificationStore`
  defines `__len__`, a caller-supplied store that was still empty (0 records) was falsy and got
  replaced with a fresh, unrelated in-memory store - notably `nox.proactive.install.install`'s
  now-db-backed store right after boot. Fixed to an explicit `is not None` check.
- Supervisor counted missed heartbeats from the moment it spawned the core, so a cold boot
  (20-40 s of imports and index work) looked like a hang: it asked for a graceful restart before
  the core had even connected. Accounting now starts with the core's first heartbeat and never
  before a boot grace of 90 s has passed (`SupervisorSettings.boot_grace_s`, read from
  `supervisor.boot_grace_s` when the config schema carries it); inside the grace only the hard
  liveness check applies (process exited -> restart).
- `sup.kill mode=restart` (the watchdog's graceful restart request) engaged the kill switch: Nox
  ended up in safe mode with the voice worker terminated and nothing restarted. A restart request
  is now a clean shutdown (exit 0) that the supervisor answers by respawning the core; kill switch,
  panic and `mode=safe_mode` are unchanged.
- Boot blocked the event loop for ~10 s (extension modules imported synchronously - `rl` pulls in
  OpenCV - and the `pm` extension indexed the whole vault inline), so no heartbeat could go out.
  Extension modules are imported in a worker thread, the pm index runs as a background task
  (`pm` health reports `limited` until it finishes), the database is opened off the loop, and the
  supervisor client now starts before any of it.
- First vault scan embedded one chunk per Ollama request; chunks are now batched per note (at most
  32 per request) and the sqlite-vec upserts run off the loop.
- `ai.providers` could exceed the UI's 10 s request timeout because it probes every provider live
  (`claude_code` alone may take 15 s), leaving the dashboard's provider card empty while health
  showed the same providers: the probe now has a 3 s budget and falls back to the health service's
  last observation instead of returning nothing.
- Core froze after `core.started`: the memory vault scan blocked the event loop — one fresh SSL
  context (~250 ms of CA-bundle loading) per embedding request, and no yield at all on already
  indexed notes. Heartbeats stopped, the supervisor restart-looped the core into safe mode.
  Now: one shared SSL context per process (`nox.security.egress.shared_ssl_context`), the
  memory extension's Ollama client goes through the egress guard like the chat provider,
  and the indexer yields after every chunk and note.
- Voice worker died on a single 5 s handshake timeout while the core was still booting; it now
  retries with backoff for up to 60 s.
- Shell stayed offline for the whole session once the first bridge connect failed, and could
  not follow a supervisor-restarted core (stale session token). It now re-reads the runtime
  files, retries every 3 s, drops a bridge after two failed pings, and reloads the pet page.
- `pm` extension refused to start because `08 - Epics/Epics Overview.md` (a map note) was
  parsed as a work item; only files matching the project's own work-item naming pattern are
  parsed as work items now.
- `nox doctor` warns when the venv runs on Microsoft-Store Python (virtualized `%APPDATA%`,
  the cause of `token_acl_failed`).
- First voice turn deadlocked the worker connection (hub delivered the worker's event inline,
  the orchestrator awaited LLM + TTS inside the handler, TTS waited for `tts.finished` from the
  blocked socket): voice turns run as a task, the hub pumps inbound events per client off the
  receive loop, the worker reconnects and re-registers, the core forgets a dropped worker, the
  shell reloads the pet page when the session token changed.

## [0.2.0] - 2026-09-14

Stream companion: Twitch chat, OBS scene control, and the Funken viewer-loyalty system, on top of
the v0.1 walking skeleton.

### Added
- Plugin runtime: manifest-validated worker processes, tool registry/executor, per-plugin scoped
  egress guard (ADR-012/ADR-013).
- OBS Studio plugin (obs-websocket v5 over loopback): read-only status/scene inventory, one
  confirmed scene switch, a privacy-scene safety action, a read-only preflight check — no
  scene/source-delete or stream-stop tool exists at all.
- Twitch plugin (IRC over TLS): chat responder, moderation gate, rate limiting.
- Stream data model, event types, and the Funken ledger (viewer currency/loyalty).
- Stream session service, Funken booking, dashboard Stream page.

### Fixed
- IPC client fix (stream bot core changeset).

## [0.1.0] - 2026-09-11

v0.1 walking skeleton: the first end-to-end run of Nox's core process, security layer, IPC hub,
AI router, voice worker, supervisor, and desktop shell/dashboard UIs.

### Added
- Core process lifecycle, config layering (Defaults → User → Profile → Runtime Override,
  ADR-010), event bus, state manager, health checks.
- Security layer: permission engine, privacy modes/zones, kill switch, audit log
  (hash-chained), secrets store (`keyring`-backed), profiles.
- Local IPC hub (WebSocket, versioned, per-client tokens, role-based).
- AI provider abstraction and router (Claude Code, Ollama, a deterministic rules fallback) with a
  degradation chain.
- Voice worker: local speech-to-text (faster-whisper) and text-to-speech (Piper), push-to-talk and
  wake-word support.
- Supervisor: watchdog, kill-switch hotkey path independent of the core process.
- PySide6 desktop shell (pet window, tray, hotkeys) and the initial React/TypeScript pet renderer
  and dashboard.
- `nox` CLI (`core`, `shell`, `supervisor`, `dev`, `doctor`, `secrets set/delete/check`).

### Security
- Graceful supervisor shutdown, speech gate (no unsolicited speech in private modes), typed chat
  stream, pet variant scaffolding.

## [0.0.0] - 2026-09-09

Repository foundation: package layout, `config/defaults.yaml`, and the interface contracts other
modules build against (`events.py`, `state.py`, `model.py`, `protocol.py`, `base.py`). No running
product yet.

No tags exist yet: the links below used to point at commit hashes from the pre-publication
history, which 404 on the public repository (#29) - they now use the tag-based form
`docs/PUBLISHING.md`'s squashed-snapshot publish is expected to create tags for
(item 16 of `docs/RELEASE_CHECKLIST.md`), even though those tags do not exist yet either; both
`v0.1.0` and `v0.2.0` get an actual release tag at first publish, `v0.0.0` ("Repository
foundation") predates the tagging process and never gets one, hence the single-tag link shape
below instead of a compare for it and for `v0.1.0` (nothing tagged to compare either against).

[Unreleased]: https://github.com/Crackxsy/nox/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/Crackxsy/nox/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/Crackxsy/nox/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/Crackxsy/nox/releases/tag/v0.3.0
[0.2.0]: https://github.com/Crackxsy/nox/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Crackxsy/nox/releases/tag/v0.1.0
[0.0.0]: https://github.com/Crackxsy/nox/releases/tag/v0.0.0
