# Nox on a Raspberry Pi (or any Linux server)

Nox's core can run as a server: always on, quiet, in a corner of the flat, on a Raspberry Pi or any
64-bit Linux machine. Your PC, laptop, phone and tablet then share one Nox - the same memory,
reminders and calendar - instead of each having its own.

> **Deutsch:** [`RASPBERRY_PI.de.md`](RASPBERRY_PI.de.md) ist dieselbe Anleitung auf Deutsch. The
> installer itself talks German.

## What you need

| | |
| --- | --- |
| Hardware | **Raspberry Pi 4 or 5 with 2 GB RAM or more** (4 GB recommended), or any 64-bit Linux machine (ARM64 or x86-64). A Pi 3, 400 or Zero 2 W boots it too, but with 1 GB or less it is tight and untested. Prefer an SSD or a high-endurance SD card: Nox writes its database often. |
| System | **Raspberry Pi OS 64-bit** (Bookworm or newer), Debian 12+, Ubuntu 22.04+. A 32-bit system is refused with that reason. |
| Network | The Pi in the same home network as your other devices. |

Python, the dependencies and the dashboard come with the installer. No Node.js, no compiler.

## Install

On the Pi, in a terminal (or over SSH):

```bash
curl -fsSL https://github.com/Crackxsy/nox/releases/latest/download/install-linux.sh | sudo bash
```

Prefer to read it first? `curl -fsSLO <same address>`, `less install-linux.sh`, then
`sudo bash install-linux.sh`. Each release also carries `install-linux.sh.sha256`.

The script prints each step in German and ends with **"Nox läuft"** once the service answers. From
then on it starts by itself after every reboot. It installs the newest release that runs on Linux
(v0.6.0 or later); once installed, `nox update <version>` picks another one.

What it does, and nothing else:

- creates a system user `nox` without a login shell;
- puts the program in `/opt/nox` (read-only to that user) and its data in `/var/lib/nox`;
- installs Python 3.13 and the dependencies through [uv](https://docs.astral.sh/uv/) from prebuilt
  packages only, inside `/opt/nox` - the system Python is left alone and nothing is compiled;
- downloads the prebuilt dashboard for that release and refuses it unless its SHA-256 matches and it
  contains nothing but the two dashboard folders;
- installs the systemd service `nox` (`installer/linux/nox.service`), which may only write to
  `/var/lib/nox` and use the network, and the short command `nox`.

## First steps

```bash
nox claude-login   # the AI behind Nox (recommended) - see below
nox onboard        # your name, a PIN, and the services you want to connect
nox doctor         # an honest report of what works and what does not
```

`nox` on the Pi always acts on the service's data, from any account that may use `sudo`.
`nox onboard` restarts the service at the end, so your answers apply at once.

### The AI behind it

Nox answers with the best backend it can reach: Claude Code, then a local Ollama model, then
simple built-in rules. On a Pi, **Claude Code** is the one to use - the Pi only forwards, the
thinking happens at Anthropic. `nox claude-login` installs it for the service user and starts the
login: type `/login`, open the link it shows on your phone or PC, paste the code back, then `/exit`
and `sudo systemctl restart nox`.

`nox claude-login` runs Anthropic's official installer (`claude.ai/install.sh`, over HTTPS) as
the `nox` user, so Claude Code lives next to Nox's data and nowhere else.

A local model through Ollama runs on a Pi 5 with 8 GB, but answers take many seconds. It is
optional and not set up by the installer. If you want it:

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3:4b-instruct   # the model Nox asks for by default (about 2.5 GB)
```

Nox finds a local Ollama by itself (`127.0.0.1:11434`) and uses it when Claude Code is not
available - no setting needed. That model was chosen by measurement: it drives Nox's tools in 3 of
4 tries, where smaller ones tend to answer in prose instead of acting
([`LOCAL_MODELS.md`](LOCAL_MODELS.md)). A smaller model such as `qwen3:1.7b` is faster on a Pi but
acts less reliably; to use it anyway, set `ai.providers.ollama.model: "qwen3:1.7b"` in `user.yaml`.
`nox doctor` says whether the model Nox wants is there.

## Day to day

| | |
| --- | --- |
| Is it running? | `systemctl status nox` |
| What is it doing? | `journalctl -u nox -f` |
| Health check | `nox doctor` |
| Where are its files? | `nox where` |
| Settings | `sudo nano /var/lib/nox/user.yaml`, then `sudo systemctl restart nox` |
| Update | `nox update` |

**Updates can be undone.** Every version is a complete folder of its own under
`/opt/nox/releases`, and `/opt/nox/current` points at the one that runs. `nox update` builds the new
version there while the old one keeps running; only then does it stop Nox briefly, save
`/var/lib/nox` as a snapshot in `/var/backups/nox` (the last five are kept; program caches are left
out), switch to the new version and start it. If the new version does not answer, the installer
switches back and restores the data from that snapshot by itself. The snapshot holds your secrets
too and is readable by root only.

### Undoing an update

| | |
| --- | --- |
| Version **and** data back to before the last update | `nox rollback` |
| Which snapshots exist | `nox snapshots` |
| Only the data of one snapshot back | `nox restore <file name>` |

Restoring never writes over the current data: they are moved aside to
`/var/lib/nox.vor-wiederherstellung-<time>` first, and the command tells you so - delete that
folder once everything works.

**Time zone.** Reminders and the calendar follow the Pi's clock. Raspberry Pi OS Lite often starts
in UTC; the installer warns about it. In Germany: `sudo timedatectl set-timezone Europe/Berlin`.

## Reaching it from your other devices

Network access is the next part of this release: pairing a phone, tablet or PC with a one-time code
shown on the server, after which that device - and only that device - opens the dashboard. Until it
lands, the server listens on the Pi itself only and is not reachable from the network. That is
deliberate: a dashboard that can change settings must not be open to anything that happens to be
in the same Wi-Fi.

## Secrets without a desktop

On Windows, tokens and passwords go into the Windows Credential Manager. A Pi without a desktop has
no such keyring, so Nox keeps them in `/var/lib/nox/secrets.json`, readable by the `nox` user only -
the same protection an SSH key has on that machine. `nox where` and `nox doctor` say which one is
in use. Once an installation has chosen, it stays with that choice: if a keyring it used is missing
one day, Nox reports it and refuses PIN-protected changes instead of quietly starting over with an
empty file. `security.secret_store: keyring | file` in `user.yaml` forces either. Switching moves nothing:
secrets already stored stay where they are, a PIN set before looks unset afterwards, and
`nox where` warns about exactly that.

Plugins run as the same `nox` user and can therefore read the same files - Nox's plugin boundary is
a permission check on every call, not an operating-system sandbox. Install plugins you trust.

## What runs where

The Pi is the shared brain; device-specific things stay on the device they belong to.

| | On the Pi | On your Windows PC |
| --- | --- | --- |
| Conversation, memory, reminders, calendar, smart home, plans | ✓ | ✓ |
| Dashboard from every device in the network | planned - network access (above) | ✓ (on that PC) |
| The pet window and global hotkeys | – (no screen) | ✓ |
| Windows, programs, typing into a window | – | ✓ |
| Speaking and listening | planned (a USB microphone and speaker) | ✓ |
| Twitch, OBS, Rocket League coach | – (they belong on the streaming PC) | ✓ |

## Uninstall

```bash
sudo systemctl disable --now nox
sudo rm /etc/systemd/system/nox.service /usr/local/bin/nox
sudo rm -r /opt/nox            # the program
sudo rm -r /var/lib/nox        # your data: memory, settings, secrets - only if you mean it
sudo rm -r /var/backups/nox    # the update snapshots
sudo userdel nox
```

Only the two lines marked as data delete anything of yours. They are listed separately on purpose.
