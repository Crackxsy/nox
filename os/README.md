# Nox-OS

Nox-OS is a flashable image for the Raspberry Pi: **Raspberry Pi OS Lite (64-bit) with Nox
preinstalled**, as a headless home server. It is built in CI with
[pi-gen](https://github.com/RPi-Distro/pi-gen), the tool Raspberry Pi OS itself is built with, and
works on every 64-bit Pi the base supports (3B/3B+/Zero 2 W, 4B/400/CM4, 5/CM5).

> **Status: steps O1 and O2 of the Nox-OS plan** - the image is built, hardened (firewall, SSH off
> with a switch in the dashboard, SD-card care, console status), checked and boot-tested. The
> first-boot setup without a screen (Wi-Fi from a provisioning file, setup hotspot, claiming the
> device from a phone) comes with O3/O4. Until then a flashed card runs Nox, but there is no way to
> sign in to it yet: use it for testing, and [`docs/RASPBERRY_PI.md`](../docs/RASPBERRY_PI.md)
> (the installer on a stock Raspberry Pi OS) for a real home server.

## What is in the image, and what the first boot does

Nox is installed by the same installer a Pi uses, `scripts/install-linux.sh`, in its image mode
(`--image`): same layout (`/opt/nox/releases/<commit>`, `/opt/nox/current`, data in
`/var/lib/nox`), same signed-release check, but nothing is started at build time.

> **How far the signed-release check reaches today:** while `install-linux.sh` trusts no release
> key (`TRUSTED_RELEASE_KEYS` is empty until the owner sets the key up, [Signed
> releases](../docs/RELEASES.md)), the Nox inside the image is pinned by its tag and commit and the
> dashboard by its SHA-256 - not by a signature. Once a key is trusted, a release image is built
> only from a release whose signed manifest verifies.

| Baked into the image (identical on every card) | Made on the device at first boot (unique per Pi) |
| --- | --- |
| Raspberry Pi OS Lite (pinned pi-gen release), hostname `nox` | `/etc/machine-id` |
| Nox: the program, its Python environment (uv, Python 3.13), the dashboard | SSH host keys (`nox-ssh-hostkeys.service`; SSH stays off) |
| `nox.service` (hardened, `installer/linux/nox.service`), enabled | Nox's CA and server certificate, its database, every pairing - when `nox.service` first starts |
| `/var/lib/nox/user.yaml` (server defaults, no secrets) | |
| `/etc/nox-os-release` (version, base, build date) | |
| The firewall's boot rules (`/etc/nftables.conf`: default deny, no port of Nox's open) | The firewall rules from Nox's settings (`nox-firewall.service`, at every boot and every change) |
| Hardening, SD-card wear profile, the root helper, the console status ([Hardening](#hardening-o2)) | |

Never in the image: a password (the first user `nox-admin` is locked, root is locked), passwordless
sudo, an SSH key or `authorized_keys`, SSH switched on (`ssh.service`, `ssh.socket` and
`sshswitch.service` are masked, so an `ssh` file on the boot partition does nothing), Wi-Fi
credentials, cloud-init seeds, Nox certificates, tokens or pairings, the system's own seeds and
secrets (random seed, credential secret, NetworkManager key, D-Bus machine id), build leftovers
(shell history, caches, apt credentials or proxy). Raspberry Pi Connect and `userconf-pi` are
removed, cloud-init is not installed. `os/tools/check-image.sh` refuses an image that has any of
these, and the boot test checks the first boot made the per-device parts. SSH host keys come from
`nox-ssh-hostkeys.service` (only when none exist; Raspberry Pi OS's own
`regenerate_ssh_host_keys` may do the same - either way they are the device's own).

Raspberry Pi Imager's own customisation (user, password, SSH key, Wi-Fi) is not offered for
Nox-OS - the Imager entry has no `init_format` - because Nox-OS sets itself up (O3). That is not a
lock against the owner of the card: the boot partition is plain FAT, and whoever holds the card can
still use what Raspberry Pi OS reads from it (`custom.toml`, a `systemd.run=` in `cmdline.txt`) or
change anything else. Physical access to the card is full access, as on every Pi.

**For O3:** the image ships `user.yaml` with Nox's LAN listener on (`ipc.lan.enabled: true`, port
47843), so an unclaimed Nox-OS listens in the home network from its first start. Pairing still
needs a code nobody can create on a headless O1 image; O3 defines what an unclaimed core exposes
(claim page only) before the image is meant for real use.

## Hardening (O2)

Sub-stage `stage-nox/03-hardening`. `os/tools/check-image.sh` checks every point below on the
finished image, `os/tools/qemu-smoke.sh` on the booted one.

### Firewall

nftables, inbound **default deny**, IPv4 and IPv6, in a table of its own (`inet nox_os`).
`nftables.service` loads the boot rules (`/etc/nftables.conf`) before the network comes up:
nothing of Nox's open. `nox-firewall.service` then generates the rules from Nox's own settings
(`config/defaults.yaml` of the installed release and `/var/lib/nox/user.yaml`; `nox-firewall.path`
regenerates them whenever the settings file changes) and replaces the table atomically. If that
fails, the boot rules stay in force: the firewall fails closed.

| Open inbound | When | From |
| --- | --- | --- |
| Loopback; answers to connections the Pi made | always | - |
| The ICMP IPv4 and IPv6 need (errors, neighbour discovery, router adverts, multicast queries); ping at most 10/s | always | ICMP: anywhere; IGMP: private networks |
| DHCP client (udp 67 -> 68), DHCPv6 client | always | the network; DHCPv6 from `fe80::/10` |
| mDNS (udp 5353, `nox.local`) | always | private networks |
| Nox's dashboard (`ipc.lan.port`, 47843) and node listener (`ipc.lan.node_port`, 47844) | only while `ipc.lan.enabled` | private networks - the ones Nox's listener answers: `10/8`, `172.16/12`, `192.168/16`, `169.254/16`, `fc00::/7`, `fe80::/10`; anywhere only with `ipc.lan.allow_public_clients`. Over 60 new connections a minute (burst 40) per address are dropped |
| SSH (tcp 22) | only while remote maintenance is on | private networks; over 10 a minute (burst 5) per address dropped |
| HTTP 80, DNS 53, DHCP server 67 | only in setup mode (`/run/nox-os/setup-mode`, step O3) | - |

Forwarding is dropped. Outbound is open - Nox needs the internet - except the cloud metadata
addresses (`169.254.169.254`, `fd00:ec2::254`). A port from the settings is opened only when it is
1024-65535 and none of the system's own (22, 53, 67, 68, 80, 546, 547, 5353, and the core's
loopback ports 47800/47801): the settings file belongs to the service user, which must not be able
to open SSH or a privileged port through it.

**IPv6 in the home network:** "private" means unique-local (`fc00::/7`) and link-local
(`fe80::/10`) addresses only. A phone that reaches the Pi from its global IPv6 address (the home's
prefix from the provider) is not let in - the firewall fails closed, as Nox's own listener does.
Browsers fall back to IPv4 or to the link-local address; allowing the Pi's own on-link global
prefix is left for later.

If `nftables.service` cannot load its rules, `nox.service` does not start either (it requires
the firewall, a drop-in): never a Nox listening without a firewall.

**No fail2ban:** there is no password login to guess (key only), SSH is off unless switched on for
a limited time, reachable from private networks only and rate-limited per address. A ban list
would have nothing left to do, and one more daemon writing logs is what the SD card does not need.

### Remote maintenance (SSH)

Off. `ssh.service`, `ssh.socket` and `sshswitch.service` stay masked (O1), and no account has a
password. Dashboard -> Einstellungen -> System -> **Fernwartung (SSH)**, in two steps, each with
two secrets - the **security PIN** (always, also on a paired phone, and refused while no PIN is
set) **and the maintenance passphrase**:

1. **Keys** (`noxos.ssh.keys.set`): public keys pasted there (ed25519 or ECDSA, optionally on a
   security key). The root helper stores them - not Nox - and answers with their fingerprints,
   which the dashboard shows.
2. **On** (`noxos.ssh.enable`): the owner confirms that those fingerprints are theirs and picks a
   time (15 minutes to 4 hours, 1 hour by default). The request names exactly the confirmed
   fingerprints; the helper refuses unless its stored keys are exactly those.

Both, and their outcome, are in Nox's audit chain (never the passphrase); switching off and
forgetting the keys (`noxos.ssh.disable`) need nothing. While SSH is on, the dashboard and the
console status say so, with the end time.

**Why a second secret.** The security PIN is checked by Nox, and Nox's files can be read by its
service user - code running as that user could guess a short PIN's hash offline and would then
be one step from root (keys, SSH as `nox-admin`, sudo). The **maintenance passphrase** closes that
step: at least 12 characters, checked only by the root helper (scrypt, `/var/lib/nox-os/maintenance`,
root 0600), never written to a file the service user can read and never logged - the core only
passes it through in the one request. Five wrong passphrases lock it for 15 minutes, every five
more for twice as long, at most a day (counted root-side, `/var/lib/nox-os/maintenance-lockout`).
It is set once during the first-boot setup - the helper accepts `maint set` only while the
root-owned `/run/nox-os/setup-mode` exists (O3 creates it during the claim) and no passphrase is
set yet - and changed later only with the old one. A forgotten passphrase is reset by root only:
create `/var/lib/nox-os/reset-maintenance` (physical access to the card, or a maintenance session
still open), and the next `maint set` in setup mode replaces it and removes the flag. Every check
of the passphrase holds an exclusive lock (`flock` on `/run/nox-os/helper/passphrase.lock`) from
reading the lockout count to writing it, so parallel guesses - one helper instance per
connection, at most 4 at once - are counted one by one. Without it, setting keys and switching on are refused ("set a maintenance
passphrase during setup").

The unprivileged `nox.service` (`NoNewPrivileges`, no capabilities) cannot change the system
itself. It asks the **root helper**, `/usr/local/lib/nox-os/nox_os_helper.py`:

- socket-activated (`nox-os-helper.socket`: `/run/nox-os/helper/helper.sock`,
  `root:nox-os-helper 0660`, in a folder only root and that group may enter). The group has no
  members; only `nox.service` carries it (`SupplementaryGroups=`, a drop-in in
  `nox.service.d/`) - the user `nox` itself cannot even connect. At most 20 connections in 10 s:
  a flood fails the socket, and `nox-os-helper-recover.service` starts it again after 30 s. One
  sandboxed instance per request (`ProtectSystem=strict`, writes only `/run/nox-os` and
  `/var/lib/nox-os`, no network, no capabilities);
- answers **root and the main process of `nox.service` only**, bound to the process, not to its
  number: the service user's uid, `nox.service`'s `MainPID`, in `nox.service`'s cgroup (its own
  or the sandbox's `nox-core` leaf), and still the same process at the end of the check - by the
  pidfd of the peer (`SO_PEERPIDFD`, Linux 6.5+) or, without one, by its start time read before and
  after. Commands Nox runs for someone run as that user and inherit the group, but are never the
  main process; and they cannot take it over: `nox core` makes itself non-dumpable as its very
  first step on Nox-OS (`PR_SET_DUMPABLE`, before the event loop, the extensions or any
  subprocess), so no other process of the user may trace it or touch its memory - Raspberry Pi
  OS's kernel has no Yama that would;
- knows exactly these requests, without options: `status`, `keys set` (the passphrase and 1-8
  public keys, checked again itself: type, base64, the blob's own type and curve, no
  `authorized_keys` options, a printable comment), `keys clear`, `ssh on <minutes> <fingerprints>`
  (5-240, with the passphrase), `ssh off`, `maint set` (setup mode only), `maint change` (with the
  old passphrase). Anything else is refused.

The keys are the helper's alone: `/var/lib/nox-os/ssh_keys`, root, 0600 (on the card, so they
survive a reboot until forgotten). Nothing the service user can write is ever read as a key - a
planted file in Nox's folder grants nothing.

`ssh on` copies the stored keys to `/run/nox-os/ssh/authorized_keys` and starts
`nox-ssh.service` - sshd with a complete configuration of its own (`/etc/nox-os/ssh/sshd_config`:
`AuthenticationMethods publickey`, no passwords, no root, `AllowUsers nox-admin@<private
networks>`, no forwarding or tunnels, three tries). Only once sshd runs does it write a sudo rule
for `nox-admin` (`NOPASSWD` - the account has no password) to `/run/nox-os/sudoers.d/`, which
`/etc/sudoers.d/50-nox-os` includes; then it opens port 22 for private networks and starts a
timer. Keys and sudo rule live in memory, gone at the next boot; a request that finds the state
"on" while sshd does not run cleans up first. SSH from the device itself is always dropped by the
firewall (`iif "lo" tcp dport 22 drop`, before anything else): a local process must not reach
`nox-admin`'s root that way. When the time is up, on `ssh off`, or whenever sshd stops for any
other reason (`ExecStopPost=`), sshd **and its open sessions** end (`loginctl terminate-user
nox-admin`), and the copied keys, the sudo rule and port 22 go with it. `nox-ssh.service` has no
`[Install]` section and a `RuntimeMaxSec=4h15min` of its own as the last stop. Keys are never baked
in: they come only from the dashboard.

**What switching off does not undo:** it ends the access, not its effects. Whatever was changed
as root during maintenance - packages, files, settings - stays changed.

### SD-card wear

| Measure | Setting | What a power cut (or reboot) costs |
| --- | --- | --- |
| System journal | `Storage=volatile`, `RuntimeMaxUse=32M` (in RAM) | the system log of that boot. Nox's own audit chain is in its database, on the card |
| Root file system | `noatime`, `commit=60` | file changes of up to the last 60 s (ext4 stays consistent; Nox's database syncs on its own) |
| `/tmp`, `/var/tmp` | tmpfs (at most 50 % / 25 % of RAM, used only as filled) | their contents, which nothing may rely on |
| Swap | zram only (`/etc/rpi/swap.conf.d/50-nox-os.conf`: `Mechanism=zram`), no swap file; `dphys-swapfile` and `rsyslog` removed where present | nothing |
| Nox's database | WAL, `synchronous=NORMAL`, `wal_autocheckpoint=4000` pages, `PRAGMA optimize` on close | the last transactions before the cut; the database itself stays consistent |
| man-db | no index rebuild on package changes | nothing |

Nothing has been measured on a device yet: the write counter (`/sys/block/mmcblk0/stat`) over
24 hours is on the release hardware checklist. A USB SSD remains the better home for a server.

### Kernel, services, console

- `sysctl` (`/etc/sysctl.d/90-nox-os.conf`): kernel pointers and log for root only, no
  unprivileged eBPF, hardened BPF JIT, no kexec, no SysRq, `ptrace` of own children only (where
  the kernel has Yama),
  protected links, FIFOs and files in sticky folders, no core dumps of set-uid programs;
  reverse-path filter (loose - Ethernet and Wi-Fi may be in the same network), no ICMP redirects
  sent or accepted, no source routing, SYN cookies. Unprivileged user namespaces stay on: Nox's
  command sandbox (bubblewrap) needs them.
- Masked: Bluetooth (`bluetooth`, `hciuart`), `triggerhappy`, `ModemManager`. avahi stays: it
  answers for `nox.local`. `nox.service` is unchanged and as hardened as `installer/linux/`
  ships it (user `nox`, `ProtectSystem=strict`, no capabilities - checked by `check-image.sh`).
- Updates: `unattended-upgrades` is step O6 (sub-stage `05-updates`), not here.
- **Console status** on the first screen (tty1) instead of a login prompt: Nox-OS and Nox
  version, addresses, the dashboard's address, network access and paired devices, health, SSH on
  or off. No secrets, no login, refreshed every 10 s, run as a throwaway user without rights
  (`nox-console.service`, `DynamicUser=`). The other consoles keep a login prompt at which nobody
  can log in (no account has a password).

Known, for O3: `systemd-firstboot.service` reports a failure on the first boot (on a headless
device it has nobody to ask); O3's own first-boot unit takes over what it would set.

## Getting it

Each release carries:

| File | What |
| --- | --- |
| `nox-os-<tag>-arm64.img.xz` | The image (xz). Budget: 1.2 GB. |
| `nox-os-<tag>-arm64.img.xz.sha256` | Its SHA-256 |
| `nox-os.rpi-imager.json` | Raspberry Pi Imager custom repository entry (size and SHA-256 of the download and of the unpacked image) |
| `nox-os-<tag>-arm64.licenses.json` | What the image contains and under which licences (below) |
| `nox-os-manifest.json` + `.sig` | The signed list of the files above (`"kind": "os-image"`, [Signed releases](../docs/RELEASES.md)) |

A release whose manifest cannot be signed - the release key is not set up yet - gets **none** of
these files: the workflow stops at the signing job ("Release not signed") before it uploads
anything.

In Raspberry Pi Imager: *App options* -> *Content repository* -> *Use custom URL*:
`https://github.com/Crackxsy/nox/releases/latest/download/nox-os.rpi-imager.json`, then pick
"Nox-OS (based on Raspberry Pi OS)". Or write the `.img.xz` with any tool that writes images.

Imager checks **integrity**, not authenticity: it compares the download and the written card with
the SHA-256 in the JSON, which comes from the same release page as the image. Whoever could replace
the image there could replace the JSON too. Authenticity is the signed manifest's job - check it by
hand, on a machine that runs Nox:
`nox release verify nox-os-manifest.json --kind os-image --file nox-os-<tag>-arm64.img.xz`.

## How it is built

`.github/workflows/os-image.yml`:

- **Every pull request** touching `os/`, `scripts/install-linux.sh` or `installer/linux/`:
  shellcheck, and the installer's image mode in a small Debian chroot (Nox built and enabled, not
  started, nothing of its own created; `--image` refused on a running system).
- **Full build** - on a published release, by hand (*Run workflow*), nightly when `develop`
  changed during the day, and on a pull request from this repository with the label `os-image`
  (never for a fork's pull request):
  1. pi-gen (pinned tag and commit in `os/pi-gen/build.sh`) in privileged Docker on a native arm64
     runner: stages 0-2 (Lite) and `stage-nox` ([`pi-gen/stage-nox/README.md`](pi-gen/stage-nox/README.md)).
     A release installs that release from GitHub, signature checked; any other build installs the
     checked-out commit and a dashboard built from it.
  2. `os/tools/check-image.sh`: the image is mounted read-only and checked (list above).
  3. `os/tools/qemu-smoke.sh`: a copy of the image boots headless in QEMU (an emulated Raspberry
     Pi 3, the image's own kernel) with one CI-only unit added, until `nox.service` is active and
     `/health` answers, SSH is not running and the host keys and machine id were made; then O2:
     the firewall is loaded and default deny with the dashboard port opened from Nox's settings,
     journal, `/`, `/tmp` and swap as above, sysctl applied, the console status renders, the root
     helper is out of reach of the user `nox` and refuses another process with its group, the core
     is non-dumpable, `noxos.ssh.status` answers through the core and the sandboxed helper, the
     maintenance passphrase is refused outside setup mode and stored only as root's scrypt hash,
     and SSH goes on (keys stored root-only and answered by fingerprint, refused for unconfirmed
     ones or a wrong passphrase; sshd up, port 22 for private networks, off timer, TCP to port 22
     from the device itself dropped by the firewall alone). In a network namespace with a private
     and a documentation address: a key login as `nox-admin` with `sudo -n` works from the private
     one and is refused from the other; the expiry then ends sshd and an open session, and `ssh
     off` leaves nothing behind. Test-only
     boot settings, because of the emulator: no hardware watchdog (QEMU resets on it), Bluetooth
     off the serial port, no first-boot resize. The verdict is read from the copy's disk.
  4. The SHA-256 taken right after the build is checked again, then xz, SHA-256, the Imager entry,
     the licence manifest. For a release the OS manifest is signed in the `release-signing`
     environment and only then is everything attached - without the key, nothing is.

pi-gen is pinned by tag and commit; its `build-docker.sh` builds its build container from
`debian:trixie` by tag, not digest. That container is not shipped - the image's packages come from
the Debian and Raspberry Pi archives, checked by their apt keys - but it does run the build.

Locally (Linux with Docker that may run `--privileged` containers, about 25 GB free; an arm64
machine avoids emulation):

```bash
os/pi-gen/build.sh --ref v0.7.0 --out out/                         # a release, from GitHub
os/pi-gen/build.sh --ref "$(git rev-parse HEAD)" --source . \
  --ui-bundle nox-ui.tar.gz --out out/                              # this checkout
sudo os/tools/check-image.sh out/nox-os-*-arm64.img out/boot
sudo os/tools/qemu-smoke.sh out/nox-os-*-arm64.img out/boot     # qemu-system-arm, qemu-utils, device-tree-compiler
```

## Licences

The image is called **"Nox-OS (based on Raspberry Pi OS)"** and uses no Raspberry Pi logo. It
contains Raspberry Pi OS (Debian and Raspberry Pi packages under their own free licences, plus
redistributable firmware) and Nox (Apache-2.0) with its Python dependencies
([`docs/THIRD_PARTY_LICENSES.md`](../docs/THIRD_PARTY_LICENSES.md)). pi-gen (BSD-3-Clause) builds
it and is not shipped. `nox-os-<tag>-arm64.licenses.json` lists every package with its version,
its source package (where its corresponding source code is in the Debian and Raspberry Pi
archives) and the licences it declares, and every Python package with its licence. Mirroring the
GPL source packages as a release asset is step O8.

Not in the image, ever installed only by the user's own action or downloaded on demand after
asking: Claude Code, Ollama and models, Playwright's Chromium, voice models and Whisper weights.

## Layout

| Path | What |
| --- | --- |
| `pi-gen/config` | pi-gen settings: Lite stages + `stage-nox`, hostname, locked first user, SSH and cloud-init off |
| `pi-gen/build.sh` | Fetches the pinned pi-gen, adds `stage-nox` and the Nox inputs, runs pi-gen in Docker |
| `pi-gen/stage-nox/` | Nox's stage; numbered sub-stages per step (`03-hardening` O2, then O3 first boot, O6 updates) |
| `pi-gen/stage-nox/03-hardening/files/` | Firewall generator, root helper, console status, their units, sshd and system settings |
| `pi-gen/export-nox/` | One step in pi-gen's export (removes `userconf-pi` again, which the export installs) |
| `tools/check-image.sh` | Read-only checks of a built image (or of a tree: `--root`/`--boot`); takes out the boot files and the licence manifest |
| `tools/qemu-smoke.sh` | Headless QEMU boot until Nox answers |
| `tools/imager_json.py` | The Raspberry Pi Imager entry |
| `tools/source_manifest.py` | The licence and source manifest |
