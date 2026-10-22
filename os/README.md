# Nox-OS

Nox-OS is a flashable image for the Raspberry Pi: **Raspberry Pi OS Lite (64-bit) with Nox
preinstalled**, as a headless home server. It is built in CI with
[pi-gen](https://github.com/RPi-Distro/pi-gen), the tool Raspberry Pi OS itself is built with, and
works on every 64-bit Pi the base supports (3B/3B+/Zero 2 W, 4B/400/CM4, 5/CM5).

> **Status: step O1 of the Nox-OS plan** - the image is built, checked and boot-tested. The
> first-boot setup without a screen (Wi-Fi from a provisioning file, setup hotspot, claiming the
> device from a phone) comes with O3/O4. Until then a flashed card runs Nox, but there is no way to
> sign in to it yet: use it for testing, and [`docs/RASPBERRY_PI.md`](../docs/RASPBERRY_PI.md)
> (the installer on a stock Raspberry Pi OS) for a real home server.

## What is in the image, and what the first boot does

Nox is installed by the same installer a Pi uses, `scripts/install-linux.sh`, in its image mode
(`--image`): same layout (`/opt/nox/releases/<commit>`, `/opt/nox/current`, data in
`/var/lib/nox`), same signed-release check, but nothing is started at build time.

| Baked into the image (identical on every card) | Made on the device at first boot (unique per Pi) |
| --- | --- |
| Raspberry Pi OS Lite (pinned pi-gen release), hostname `nox` | `/etc/machine-id` |
| Nox: the program, its Python environment (uv, Python 3.13), the dashboard | SSH host keys (`nox-ssh-hostkeys.service`; SSH stays off) |
| `nox.service` (hardened, `installer/linux/nox.service`), enabled | Nox's CA and server certificate, its database, every pairing - when `nox.service` first starts |
| `/var/lib/nox/user.yaml` (server defaults, no secrets) | |
| `/etc/nox-os-release` (version, base, build date) | |

Never in the image: a password (the first user `nox-admin` is locked, root is locked), an SSH key
or `authorized_keys`, SSH switched on (`ssh.service` is masked, so an `ssh` file on the boot
partition does nothing), Wi-Fi credentials, cloud-init seeds, Nox certificates, tokens or pairings.
`os/tools/check-image.sh` refuses an image that has any of them, and the boot test checks the
first boot made the per-device parts. Raspberry Pi Imager's own customisation (user, password, SSH
key, Wi-Fi) is not offered for Nox-OS - the Imager entry has no `init_format` - because Nox-OS sets
itself up (O3).

## Getting it

Each release carries:

| File | What |
| --- | --- |
| `nox-os-<tag>-arm64.img.xz` | The image (xz). Budget: 1.2 GB. |
| `nox-os-<tag>-arm64.img.xz.sha256` | Its SHA-256 |
| `nox-os.rpi-imager.json` | Raspberry Pi Imager custom repository entry (size and SHA-256 of the download and of the unpacked image; Imager verifies the card after writing) |
| `nox-os-<tag>-arm64.licenses.json` | What the image contains and under which licences (below) |
| `nox-os-manifest.json` + `.sig` | The signed list of the files above - once the release key is set up ([Signed releases](../docs/RELEASES.md)) |

In Raspberry Pi Imager: *App options* -> *Content repository* -> *Use custom URL*:
`https://github.com/Crackxsy/nox/releases/latest/download/nox-os.rpi-imager.json`, then pick
"Nox-OS (based on Raspberry Pi OS)". Or write the `.img.xz` with any tool that writes images.

To check a download by hand, on a machine that runs Nox:
`nox release verify nox-os-manifest.json --file nox-os-<tag>-arm64.img.xz`. While the release key
is not configured, releases carry no signed manifest and the image is checked by checksum only;
the workflow says so and marks the run as failed ("Release not signed"), as for every release.

## How it is built

`.github/workflows/os-image.yml`:

- **Every pull request** touching `os/`, `scripts/install-linux.sh` or `installer/linux/`:
  shellcheck, and the installer's image mode in a small Debian chroot (Nox built and enabled, not
  started, nothing of its own created; `--image` refused on a running system).
- **Full build** - on a published release, by hand (*Run workflow*), nightly on `develop` when the
  image's parts changed, and on a pull request with the label `os-image`:
  1. pi-gen (pinned tag and commit in `os/pi-gen/build.sh`) in privileged Docker on a native arm64
     runner: stages 0-2 (Lite) and `stage-nox` ([`pi-gen/stage-nox/README.md`](pi-gen/stage-nox/README.md)).
     A release installs that release from GitHub, signature checked; any other build installs the
     checked-out commit and a dashboard built from it.
  2. `os/tools/check-image.sh`: the image is mounted read-only and checked (list above).
  3. `os/tools/qemu-smoke.sh`: a copy of the image boots headless in QEMU (an emulated Raspberry
     Pi 3, the image's own kernel) with one CI-only unit added, until `nox.service` is active and
     `/health` answers, SSH is not running and the host keys and machine id were made.
  4. xz, SHA-256, the Imager entry, the licence manifest; for a release the OS manifest is signed
     in the protected `release-signing` environment and everything is attached.

Locally (Linux with Docker that may run `--privileged` containers, about 25 GB free; an arm64
machine avoids emulation):

```bash
os/pi-gen/build.sh --ref v0.7.0 --out out/                         # a release, from GitHub
os/pi-gen/build.sh --ref "$(git rev-parse HEAD)" --source . \
  --ui-bundle nox-ui.tar.gz --out out/                              # this checkout
sudo os/tools/check-image.sh out/nox-os-*-arm64.img out/boot
sudo os/tools/qemu-smoke.sh out/nox-os-*-arm64.img out/boot
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
| `pi-gen/stage-nox/` | Nox's stage; later steps add numbered sub-stages there (O2 hardening, O3 first boot, O6 updates) |
| `tools/check-image.sh` | Read-only checks of a built image; takes out the boot files and the licence manifest |
| `tools/qemu-smoke.sh` | Headless QEMU boot until Nox answers |
| `tools/imager_json.py` | The Raspberry Pi Imager entry |
| `tools/source_manifest.py` | The licence and source manifest |
