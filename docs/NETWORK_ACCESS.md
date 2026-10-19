# Network access: Nox on every device in your home

Phones, tablets and other PCs in your home network can open the Nox dashboard - the same one the PC
shows - once they are **paired**. A paired device talks, sees the day, switches lights, answers
Nox's questions ("may I delete this file?") and can pull the kill switch. It cannot change how Nox
is set up; that stays with the machine Nox runs on.

| | |
| --- | --- |
| Off or on | `ipc.lan.enabled` in `user.yaml` (dashboard: Settings, "Other devices in the home network may open Nox"). Off by default on a PC, **on** on a Raspberry Pi installed with `scripts/install-linux.sh`. Turning it on needs the PIN when one is set. |
| Address | `https://<this machine>:47843/dashboard/` - the dashboard's "Devices" tab and `nox pair` show the exact links. |
| Pairing | A one-time code, valid for five minutes, shown as a QR code and a link. |

## Pairing a device

1. **Make a code.** On the PC: dashboard, tab **Devices**, **Create a code**. On a server without a
   screen (a Raspberry Pi): `nox pair` in a terminal or over SSH - it prints a QR code made of block
   characters, the link and the code.
2. **Scan it** with the new device's camera, or open the link. Nothing to install.
3. The browser warns once, because Nox has a certificate of its own (see below). Compare the
   fingerprint it shows with the one next to the QR code; if they match, continue.
4. Give the device a name and tap **Pair**. Done - the dashboard opens, and stays paired.

A device without a camera: open `https://<this machine>:47843/dashboard/`, tap **I have a code** and
type it.

## No warning, and Nox on the home screen

Nox makes a **certificate authority of its own** on first start. Install it on a device once and the
browser stops warning - also after the server got a new address - and the dashboard can be added to
the home screen as an app. On a paired device: tab **Devices**, **Download the certificate**, then:

- **Android:** Settings, Security, More security settings, Encryption & credentials, Install a
  certificate, CA certificate.
- **iPhone and iPad:** open the file, Settings, Profile Downloaded, Install; then Settings, General,
  About, Certificate Trust Settings, turn on "Nox".
- **Windows:** open the file, Install Certificate, Local Machine, Trusted Root Certification
  Authorities.
- **Mac:** open the file in Keychain Access, double-click it, Trust, Always Trust.

This authority **may only vouch for your home network**: `localhost`, names ending in `.local`, the
machine's own name, and private addresses (192.168.x.x, 10.x.x.x, 172.16-31.x.x). It is
technically limited to those (X.509 name constraints, enforced by the browser and the phone), so
even a stolen key could not be used to impersonate a website on the internet. Its key never leaves
the machine Nox runs on.

## What a paired device may do

| A paired device may | It stays on the machine Nox runs on |
| --- | --- |
| Talk to Nox, see the status, the day, reminders, the board, plans, presets | Changing settings, secrets and accounts |
| Switch lights and scenes, run presets | Relaxing privacy, resuming after the kill switch |
| Answer Nox's questions - **once**: "always allow" is not offered | Adding facts to Nox's memory, editing clips |
| Pull the kill switch and panic mode | Pairing or removing other devices without the PIN |
| Sign itself out | |

Every question Nox asks appears on **every** dashboard at once - the PC, the phone, the tablet. The
first answer counts; the others see "Decided on another device". Nobody answers within a minute:
the answer is no. The audit trail records which device answered.

## Removing a device

Dashboard, tab **Devices**, **Remove** next to the device, and once more to confirm. Its open
sessions end immediately and its token never works again. A lost phone: remove it from the PC or
another paired device (with the PIN).

## How it is protected

- **Only paired devices.** Without pairing, the network gets the pairing page and nothing else.
  Each device has a 256-bit token, shown to it once; the server keeps only a salted hash.
- **HTTPS only** on the network. The plain-HTTP dashboard and the per-start session token stay on
  the machine itself (loopback) and are not reachable from the network.
- **No impersonation.** A connection from the network can only ever be a paired device - never the
  desktop shell, the pet or a worker - and a device token is useless on the machine itself.
- **No foreign web pages.** Requests from another website open in the same browser are refused
  (Origin check), as are requests that reach the machine under a foreign name (Host check, against
  DNS rebinding). Every page carries a strict Content-Security-Policy.
- **No guessing.** A pairing code works once, for five minutes; wrong codes are limited per
  address and in total.
- **Only your network.** Only devices in your home network can reach the listener, and the
  certificate is only valid there. Nothing about this leaves your home: no account, no cloud, no
  telemetry.

## Firewall

On Windows, the first start asks whether Python may accept connections in **private** networks -
allow it. On a Linux server with `ufw`: `sudo ufw allow 47843/tcp`.

## Settings

```yaml
ipc:
  lan:
    enabled: false                 # true: paired devices in the home network open the dashboard
    host: 0.0.0.0                  # every local address
    port: 47843
    max_devices: 20
    pair_code_ttl_s: 300           # a pairing code is valid this long, and only once
    pair_attempts_per_minute: 5    # per source address
```

Changes take effect after a restart. `nox doctor` and the dashboard's Status page show whether the
listener runs (health check `lan`).
