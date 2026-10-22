# stage-nox

The pi-gen stage that turns Raspberry Pi OS Lite into Nox-OS. Sub-stages run in order:

| Sub-stage | Step | What |
| --- | --- | --- |
| `01-nox` | O1 | The packages `scripts/install-linux.sh` needs, then `install-linux.sh --image` in the chroot: Nox laid out exactly as a Pi install has it, service enabled, not started |
| `02-system` | O1 | Baseline: no account can log in, SSH off and masked, per-device SSH host keys at first boot, `/etc/nox-os-release` |

Later steps add their own numbered sub-stages instead of growing these: `03-hardening` (O2:
nftables, SD-card wear, SSH toggle, console status), `04-firstboot` (O3: provisioning file,
hotspot, captive portal), `05-updates` (O6: `nox-update.timer`, security-only apt).
