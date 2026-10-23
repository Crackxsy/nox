# Nox-OS

Nox-OS is a flashable image for the Raspberry Pi: **Raspberry Pi OS Lite (64-bit) with Nox
preinstalled**, as a headless home server. It is built in CI with
[pi-gen](https://github.com/RPi-Distro/pi-gen), the tool Raspberry Pi OS itself is built with, and
works on every 64-bit Pi the base supports (3B/3B+/Zero 2 W, 4B/400/CM4, 5/CM5).

> **Status: steps O1-O3 of the Nox-OS plan** - the image is built, hardened (firewall, SSH off
> with a switch in the dashboard, SD-card care, console status), sets itself up without a screen
> (Wi-Fi from a provisioning file, a setup hotspot with a captive portal, claiming the device from
> a phone - [First boot](#first-boot-without-a-screen-o3)), and is checked and boot-tested. The
> onboarding wizard after the claim (AI login, PC node) is O4, the LED O5, the PC assistant that
> writes the provisioning file O7.
> Until then use a screen or a hand-written provisioning file, and
> [`docs/RASPBERRY_PI.md`](../docs/RASPBERRY_PI.md) (the installer on a stock Raspberry Pi OS) for a
> real home server.

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
| First boot, setup mode and the setup portal (O3) | Provisioning imported, this boot's claim code and hotspot password, the claim mark |
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

The image ships `user.yaml` with Nox's LAN listener on (`ipc.lan.enabled: true`, port 47843), so
Nox listens in the home network from its first start - **while unclaimed it serves nothing there
but its claim**, and the firewall keeps the node port closed ([The claim](#the-claim)).

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
| Nox's dashboard (`ipc.lan.port`, 47843) and node listener (`ipc.lan.node_port`, 47844) | only while `ipc.lan.enabled`; **the node port only once the device is claimed** (root's `/var/lib/nox-os-claim/claimed`) - before, the dashboard port only, where Nox serves its claim | private networks - the ones Nox's listener answers: `10/8`, `172.16/12`, `192.168/16`, `169.254/16`, `fc00::/7`, `fe80::/10`; anywhere only with `ipc.lan.allow_public_clients`. Over 60 new connections a minute (burst 40) per address are dropped |
| SSH (tcp 22) | only while remote maintenance is on | private networks; over 10 a minute (burst 5) per address dropped |
| HTTP 80, DNS 53, DHCP server 67 | only in setup mode (`/run/nox-os/setup-mode`, its end not reached) **and** only on the setup hotspot's interface while it is up - which gets nothing else (dropped before Nox's ports and SSH) | `10.42.0.0/24` (DHCP: the hotspot) |

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
It is set once during the first-boot setup - by the claim (O3), from the setup portal or the claim
page - or with `maint set`, which the helper accepts `maint set` only while the setup window is open (root's `/run/nox-os/setup-mode`,
this boot's, its end not reached) and no passphrase is set yet - and changed later only with the
old one. A forgotten passphrase is reset by root only:
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

`systemd-firstboot.service` (which failed on the first boot of O1/O2 images: it had nobody to ask)
is masked since O3; `nox-firstboot.service` takes over what it would set.

During setup mode the console shows the setup hotspot's name and password and the claim code made
on the device (the unit carries the group `nox-os-setup`, which may read them); a screen is
physical access, and they stop working when the window closes.

## First boot without a screen (O3)

Sub-stage `stage-nox/04-firstboot`: `nox_setup.py` (root, short steps only - never a daemon), the
setup portal `nox_setup_portal.py` (its own user), their units, and the claim's root side in the
root helper. `os/tools/check-image.sh` and `os/tools/qemu-smoke.sh` check it.

### What happens at power-on

1. **`nox-firstboot.service`** (every boot, before the network and before Nox):
   - imports `nox-provision.json` from the boot partition if there is one (below), then overwrites
     and deletes it;
   - **unclaimed:** opens the **setup window** and makes this boot's setup secrets (claim code
     unless the setup card brought one, hotspot password unless the card brought one);
   - claimed: nothing - unless root asked for a maintenance-passphrase reset
     (`/var/lib/nox-os/reset-maintenance`), which opens the window for that alone.
2. **`nox-setup.timer`** (every 15 s while the window is open) runs one step: closes the window when
   its time is up, starts the **setup hotspot** when Nox needs it, keeps the portal running while
   the hotspot is up. It stops itself once the window is closed.
3. The phone joins the hotspot, the portal opens by itself, the phone goes back to the home network
   and **claims** the device at `https://nox.local:47843/claim/`.

`systemd-firstboot.service` is masked: it would ask for locale, keymap, time zone and a root
password on a console a headless Pi does not have (and failed there in O1/O2). Its answers come from
the image (pi-gen's defaults) and the provisioning file; root stays locked.

### The setup window

Root's `/run/nox-os/setup-mode` (JSON: this boot's id and the end in **seconds since boot**,
`CLOCK_BOOTTIME`, which no clock change moves - a Pi has no real-time clock and gets the time from
the network later). It is open **at most 30 minutes after power-on**, only while the device has no
owner, and nothing reopens it but another boot: **a power cycle is the physical act** that proves
someone is at the device (owner decision D2: setup without a PC is allowed, with a 30-minute window
and the blink code). Everything that grants something during setup checks the file itself - the
setup hotspot's firewall ports, the portal, the root helper's `maint set` and `claim` - so an
expired window grants nothing even in the seconds before `nox-setup.timer` removes it. When it
closes (time up, or the claim), the hotspot, the portal and every setup secret go with it and the
firewall is regenerated. The spec's "30 minutes on, 30 minutes off, repeating" is deliberately not
done: without a power cycle the window stays shut.

Test images only (the QEMU smoke): `/etc/nox-os/testing/setup-window-s` shortens it (60 s at
least); `check-image.sh` refuses an image that has `/etc/nox-os/testing`.

### The provisioning file

`/boot/firmware/nox-provision.json` - written by the PC dashboard's "Raspberry Pi einrichten"
assistant (O7) right after flashing, or by hand from
[`provisioning/nox-provision.example.json`](provisioning/nox-provision.example.json). Schema:
[`provisioning/nox-provision.schema.json`](provisioning/nox-provision.schema.json) (version 1);
`nox_setup.py validate` refuses anything it does not allow - unknown or duplicate fields, a wrong
type, a value out of range - because a typo must not be silently ignored on a device without a
screen.

| Field | What | Goes to |
| --- | --- | --- |
| `v` | `1` | - |
| `hostname` | RFC 1123 label, lower case | `/etc/hostname`, `/etc/hosts` |
| `timezone` | IANA name the image has | `/etc/localtime`, `/etc/timezone` |
| `locale` | `de_DE`, `en_GB` or `en_US` (generated in the image) | `/etc/default/locale` |
| `country` | Wi-Fi regulatory country, ISO 3166-1 alpha-2 (required with `wifi`) | `cfg80211` (modprobe.d), root's `/var/lib/nox-os/country` |
| `wifi` | `{ssid, psk, hidden}` or `null` - `psk` 8-63 printable ASCII or 64 hex, `null` for an open network | NetworkManager keyfile `/etc/NetworkManager/system-connections/nox-wifi.nmconnection`, **root 0600** |
| `claim_hash` | `sha256:` + hex of `sha256("nox-claim-v1:" + code)`, the code in upper case without spaces or dashes (Crockford base32, 8-32 characters) - the code itself is printed on the setup card | root's `/var/lib/nox-os/claim.json` (0600); ignored on a claimed device |
| `hotspot_psk` | the setup hotspot's WPA2 password printed on the card, **16**-63 printable ASCII without spaces (a random one: 80 bits or more) | root's `/var/lib/nox-os/hotspot-psk` (0600) |
| `reclaim` | `true`: forget the owner at this boot ([Reclaiming](#reclaiming-the-device)) - the same as a file `nox-reclaim` | - |

The example's code is `ABCD-EFGH-JKMN` (fictitious). The file is read **every boot** it is there -
placing a new one re-provisions the Wi-Fi of a claimed device too (physical access to the card is
full access anyway). A refused file is deleted as well (it may hold a Wi-Fi password); the reason
(`invalid_wifi_psk`, never a value) is shown on the screen and in the portal. Nothing of its
contents is ever logged - only which fields were set.

**Secure delete, and what FAT does not guarantee.** The file is overwritten with random bytes of
its own length, flushed, truncated, deleted, and the folder flushed (a macOS `._nox-provision.json`
twin is deleted too). That replaces the clusters the file has *now*. It does not reach: copies an
editor left in other clusters when it saved more than once; the deleted directory entry (FAT keeps
the name, with its first character replaced); and the SD card's flash controller, which writes the
new bytes to fresh flash pages and may keep the old ones until it reuses them - nothing the
operating system can overwrite. Treat the Wi-Fi password as "has been on the card": a card that
leaves the house should be wiped as a whole. The claim code on it is one-time and worthless after
the claim; the hotspot password only works in a setup window.

### The setup hotspot

When the window is open and the device is unclaimed, `nox-setup.timer` starts it:

- **when Nox found no network in time** (no Ethernet and the provisioned Wi-Fi not connected, 90 s
  after boot), or
- **right away when the claim code was made on the device** - the portal is the one place without a
  screen to learn it (an Ethernet cable does not need the radio);
- never while the radio is connected to the home network.

It scans for Wi-Fi networks first (one radio: it cannot scan while it is the hotspot), then brings
up a NetworkManager profile written to `/run/NetworkManager/system-connections` (RAM, root 0600):
SSID **`Nox-Setup-XXXX`** (four hex digits of a hash of the machine id: per device, stable, nothing
of the id itself), **WPA2 (CCMP) only, never open**, channel 6, NetworkManager's shared mode on
`10.42.0.1/24` (DHCP and DNS), IPv6 off. `dnsmasq-shared.d/50-nox-setup.conf` answers every name
with the portal and tells the phone where it is (DHCP option 114, RFC 8910), so Android and iPhone
open it by themselves.

**Its password, per device, never a default:** the setup card's `hotspot_psk` when there is one
(16 characters at least); otherwise **16 random characters of the claim-code alphabet made at
every boot** - shown as `ABCD-EFGH-JKMN-PQRS`, the dashes part of it (what is shown is what is
typed): **80 bits**, so a recorded WPA2 handshake cannot be cracked offline later to read what the
portal's plain HTTP carried (the home Wi-Fi's password). A screen plugged into the Pi shows it as
text **and as a Wi-Fi QR code** (`WIFI:T:WPA;S:Nox-Setup-XXXX;P:<password>;;`, drawn with Unicode
half blocks on the console; the phone's camera joins with it). The QR matrix comes from segno -
already a dependency of Nox (the pairing QR codes), run by Nox's own Python with the payload on
stdin - so nothing new is installed. Without a card or a screen the hotspot cannot be joined yet:
use the setup card (O7) or a screen. **Blink code (O5):** an LED can carry about 12 digits (40
bits), which is too little for the hotspot password; `nox_setup.new_blink_digits` is kept for O5,
which has to give a blink code a role of its own (e.g. comparing a claim), never the WPA2
password.

**Firewall:** while setup mode is on, the hotspot interface gets the portal (TCP 80), DHCP (UDP
67) and DNS (53) from `10.42.0.0/24` - and **nothing else**: an explicit drop before Nox's ports
and SSH, so a phone on the hotspot never reaches the dashboard, the node port or remote
maintenance. That drop follows the hotspot itself (root's `hotspot.json`), not the window: in the
moments after the window closed and before the hotspot is down, the interface is still isolated -
only the portal ports are gone. Forwarding stays dropped: the hotspot is no way into the home
network or the internet.

### The setup portal

`nox-setup-portal.service`, started only while the hotspot is up (no `[Install]`, at most 31
minutes), as the user **`nox-setup`**: no login, no root, no capability but binding port 80,
`ProtectSystem=strict`, writes only its own request folder, network only `10.42.0.0/24`
(systemd's IP filter - not even loopback). It changes nothing itself: it writes one request
(`/run/nox-os/setup/portal/request.json`, 0600) that root's `nox_setup.py apply`
(`nox-setup-apply.path`) checks again the same way and carries out.

- Any address that is not the portal's - every captive-portal probe (`generate_204`,
  `hotspot-detect.html`, `connecttest.txt` ...) - is redirected to `http://10.42.0.1/`.
- **One form, no JavaScript at all**, German and English (from the phone's language, with a
  switch): the **claim code** (filled in when the device made it - it is on the Pi's screen too;
  typed from the setup card otherwise), the home Wi-Fi from the scan or typed (or "Nox is on a
  cable") and its password - **nothing else**. The security PIN and the maintenance passphrase
  are never asked here: the portal is plain HTTP over a Wi-Fi others may record; they are set on
  the claim page, over TLS. Labels, legends, hints and errors are tied to their fields (`aria-describedby`,
  `aria-invalid`), an error summary with links is announced (`role="alert"`), targets are at least
  44 px, light and dark follow the phone.
- **Hardening:** Content-Security-Policy `default-src 'none'; style-src 'self'; img-src 'self';
  form-action 'self'; base-uri 'none'; frame-ancestors 'none'` (no scripts, nothing from
  elsewhere), `no-store`, `no-referrer`, `nosniff`, no framing. **CSRF:** the form carries a token
  bound to the phone's address and the time (HMAC with a key made at start, 30 minutes), and a
  POST must come with `Origin: http://10.42.0.1` (and, if sent, `Sec-Fetch-Site: same-origin`) as
  `application/x-www-form-urlencoded`. **Rate limits:** 6 POSTs a minute per address and 30 in
  total, 120 page views per address; bodies at most 4 KB; at most 16 connections at once, 10 s
  each. Nothing a phone sends is logged, and a password is never sent back in a page.
- **Root's side - nothing without the claim code:** root passes the claim code to the root
  helper's `stage` first. A wrong one applies **nothing** (no Wi-Fi change, nothing kept) and slows
  that phone's address down. With the right one the helper keeps only the hash of the portal's
  one-time ticket (`/run/nox-os/setup/pending.json`, root 0600, RAM), and the home Wi-Fi becomes
  the NetworkManager keyfile (root 0600) - the hotspot goes down and Nox joins within 60 s. **Wrong
  password or network not found:** the hotspot comes back and the portal says why.
- **The next step,** on the portal's last page: back to the home Wi-Fi, open
  `https://nox.local:47843/claim/` (the link carries the claim code made on the device, in the
  fragment, so it never reaches a server log), with the claim code shown and the certificate
  authority's fingerprint to compare with the browser warning; or "the code on your setup card".
  That fingerprint is root's: `nox_setup.py` reads Nox's CA file (`/var/lib/nox/tls/ca.crt`) and
  writes its SHA-256 to `/run/nox-os/setup/ca_fingerprint` (root:nox-os-setup 0640) - the portal
  never asks a port on loopback, which any local process could have taken first.
  The IP address cannot be shown before Nox joined the network: the router's device list is the
  fallback.

### The claim

An **unclaimed** Nox-OS core serves on the LAN only its claim: `/claim/` (a page of its own, no
inline script, German and English, with its own Content-Security-Policy `default-src 'self';
script-src 'self'; style-src 'self'; ... frame-ancestors 'none'` plus `nosniff` and
`no-referrer`), `GET`/`POST /api/claim`, `/api/lan/info`, `/nox-ca.crt`.
The dashboard, pairing, nodes and the WebSocket are refused (`403 unclaimed`; `/` and pages under
`/dashboard/` are redirected to the claim page) - checked on every request, fail closed (an
unreadable state counts as unclaimed). **The firewall opens the dashboard port only (for the
claim), the node port stays closed until claimed.** Decision: closing the dashboard port too would
leave a headless Pi without any way to be claimed from the home network; the port is open, but
the core behind it serves nothing but the claim.

**Claiming** needs the one-time **claim code** (setup card, or the screen/portal) **during the
setup window**. `POST /api/claim` (same origin, at most 2 KB, 5 a minute per address, 20 in total)
pairs the claiming browser as the first device, then asks the root helper's **`claim`**: the code
against its hash, and on success the helper - only root - writes the claim mark
`/var/lib/nox-os-claim/claimed` (root 0644: Nox, the firewall and the console read it), sets the
maintenance passphrase the claim page sent, and **ends setup mode** - hotspot and portal down,
waited for, before the firewall is regenerated. Nox then sets the security PIN the claim page
sent (the claim page asks for both, over TLS; nothing comes from the portal), audits the claim (`noxos.claim`, never a code or secret), and the browser stores its device token
like a paired device. The page then says that **this browser is now the only key** and offers
to pair a second device at once (`/dashboard/#/devices/devices`), before "Continue" to
`/dashboard/#onboarding` (O4's wizard). A refused claim removes the device it paired. After the claim: `nox-setup-close.service` stops the hotspot and the portal and
removes the setup secrets, the firewall opens the node port.

**Wrong claim codes** are counted root-side on **two independent sides**: `lan` (the claim page)
and `hotspot` (the setup portal). Per side and per source - an IPv4 address, an IPv6 /64 - every
wrong code makes that source wait (1 s, doubling, at most 5 minutes); a waiting source is never
forgotten to make room, and when a side's table of 256 sources is full of waiting ones the whole
side waits 5 minutes (fail closed). **200 wrong codes per side and boot lock that side until the
next boot** (`claim_locked_lan` / `claim_locked_hotspot`); the other side is unaffected. A right
code (compared in constant time) succeeds unless its source waits or its side is locked; nothing
staged exempts anything. The portal's done page links to the claim page with a **one-time ticket**
(its hash kept by the helper only with the right code): a claim from the home network presenting
it counts on the hotspot side - so whoever locked the LAN side cannot lock out the owner who joined
the hotspot, whose WPA2 password is only on the Pi's screen, the setup card or the LED. The math is
in [`docs/SECURITY.md`](../docs/SECURITY.md#nox-os-the-raspberry-pi-image).

The core has one read-only request for O4, `noxos.claim.status` (owner or not, setup window, its
time left).

### Reclaiming the device

The way back when the only key is lost - **physical access is full access**: put a file named
`nox-reclaim` (any content) on the boot partition, or `"reclaim": true` in a provisioning file,
and power the Pi on. At that boot root overwrites and deletes the marker, removes the claim mark,
forgets the remote-maintenance keys, lets the next claim set a fresh maintenance passphrase
(`reset-maintenance`) and leaves Nox a note (`/var/lib/nox-os-claim/reclaim`, an id). When Nox
starts - before its listener - it revokes **every paired device, node and token** (and the nodes'
certificates) and clears the security PIN, once per note (`nox.noxos.claim.apply_reclaim`,
audited). Everything else Nox knows - memory, settings, history - stays: whoever holds the card
could read it anyway. The setup window then opens as on a new device, with a new claim code.

**Residual risks (D2):** without a setup card, whoever reads the screen within the 30 minutes - a
neighbour at the window at the same moment - can join the hotspot and learn the claim code. The
hotspot's password has 80 bits, so a recorded handshake does not reveal the home Wi-Fi password
the portal carried; PIN and passphrase never cross the hotspot. A claimed device never opens the
hotspot again (unless reclaimed).

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
  3. `os/tools/qemu-smoke.sh`: copies of the image boot headless in QEMU (an emulated Raspberry
     Pi 3, the image's own kernel, no network and no Wi-Fi radio) with one CI-only unit added.
     **Twice (O3):** once *provisioned* - a provisioning file (fictitious Wi-Fi, the card's claim
     code and hotspot password) is imported and deleted, its password in no log, the setup window
     is open at boot, the hotspot profile is written, the firewall opens the claim port only;
     then the portal as a phone in a network namespace on a virtual hotspot interface sees it
     (captive redirect, CSP, Origin check, claim code and Wi-Fi only, a wrong claim code keeps
     nothing, root's CA fingerprint, Nox's port closed to the hotspot); a wrong claim code is
     refused, a claim without PIN too, the card's code with PIN and passphrase claims the device
     (setup closed, secrets and hotspot gone, node port open, a second device offered, the claim
     page's own CSP) - then the O2 checks below run on the claimed device, and a reclaim marker
     forgets the owner again (pairings and PIN gone, fresh window, a new owner claims); once
     *fresh* - no provisioning file and a test-only 600-second window: claim code and an 80-bit
     hotspot password made on the device, shown on the console (text and QR code) as its
     throwaway user sees it, then the window closes by itself and takes them along, and a claim and `maint
     set` after it are refused. Each boot runs until `nox.service` is active and `/health`
     answers, SSH is not running and the host keys and machine id were made; then O2:
     the firewall is loaded and default deny with the dashboard port opened from Nox's settings,
     journal, `/`, `/tmp` and swap as above, sysctl applied, the console status renders, the root
     helper is out of reach of the user `nox` and refuses another process with its group, the core
     is non-dumpable, `noxos.ssh.status` answers through the core and the sandboxed helper, the
     maintenance passphrase (set by the claim) is refused again outside setup mode and stored only
     as root's scrypt hash,
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
| `pi-gen/stage-nox/04-firstboot/files/` | First boot and setup mode (`nox_setup.py`), the setup portal, their units, captive DNS |
| `provisioning/` | The provisioning file's JSON schema and a fictitious example |
| `pi-gen/export-nox/` | One step in pi-gen's export (removes `userconf-pi` again, which the export installs) |
| `tools/check-image.sh` | Read-only checks of a built image (or of a tree: `--root`/`--boot`); takes out the boot files and the licence manifest |
| `tools/qemu-smoke.sh` | Headless QEMU boots (provisioned and fresh) until Nox answers |
| `tools/imager_json.py` | The Raspberry Pi Imager entry |
| `tools/source_manifest.py` | The licence and source manifest |
