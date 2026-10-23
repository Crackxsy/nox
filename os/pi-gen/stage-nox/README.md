# stage-nox

The pi-gen stage that turns Raspberry Pi OS Lite into Nox-OS. Sub-stages run in order:

| Sub-stage | Step | What |
| --- | --- | --- |
| `01-nox` | O1 | The packages `scripts/install-linux.sh` needs, then `install-linux.sh --image` in the chroot: Nox laid out exactly as a Pi install has it, service enabled, not started |
| `02-system` | O1 | Baseline: no account can log in (no passwordless sudo), SSH off and masked, per-device SSH host keys at first boot, Raspberry Pi Connect removed, no shipped system seeds or build leftovers, `/etc/nox-os-release` |

| `03-hardening` | O2 | nftables (default deny, Nox's ports from its settings), SSH off with the dashboard switch through a root helper, SD-card wear profile, kernel settings, unused services masked, console status on tty1 ([os/README.md](../../README.md#hardening-o2)) |
| `04-firstboot` | O3 | First boot without a screen: the provisioning file, the 30-minute setup window, the setup hotspot (NetworkManager shared mode, captive DNS) and its portal (own user), the claim's root side; `systemd-firstboot` masked ([os/README.md](../../README.md#first-boot-without-a-screen-o3)) |

Later steps add their own numbered sub-stages instead of growing these: `05-updates` (O6:
`nox-update.timer`, security-only apt).
