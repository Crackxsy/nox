# Network access: Nox on every device in your home

> German translation: [`NETWORK_ACCESS.de.md`](NETWORK_ACCESS.de.md). This English version is
> authoritative.

Phones, tablets and other PCs in your home network can open the Nox dashboard - the same one the PC
shows - once they are **paired**. A paired device talks, sees the day, switches lights, answers
Nox's questions ("may I delete this file?") and can pull the kill switch. It cannot change how Nox
is set up; that stays with the computer Nox runs on.

| | |
| --- | --- |
| Off or on | `ipc.lan.enabled` in `user.yaml` (dashboard: Settings, "Devices", "Other devices in the home network may open Nox"). Off by default on a PC, **on** on a Raspberry Pi installed with `scripts/install-linux.sh`. Turning it on needs the PIN when one is set. |
| Address | `https://<this computer>:47843/dashboard/` - the dashboard's "Devices" tab and `nox pair` show the exact links. |
| Pairing | A one-time code, valid for five minutes, shown as a QR code and a link. |

## Before you pair: what every paired device can see

A paired device sees **everything** the dashboard shows: the chat with Nox, the audit trail (what
Nox did and was asked to do), clips, the day, reminders and what Nox remembers about you. It is a
second screen of the same Nox, not a restricted view.

So pair only devices of people who may see all of that. **In a household with children, keep
pairing to the adults' devices for now** - there are no per-person views yet.

## Pairing a device

1. **Make a code.** On the computer Nox runs on: dashboard, tab **Devices**, **Create a code**. When
   a security PIN is set, it is asked for here. Optionally give the new device a name. On a server
   without a screen (a Raspberry Pi): `nox pair` in a terminal or over SSH - it prints a QR code made
   of block characters, the link and the code.
2. **Scan it** with the new device's camera, or open the link. Nothing to install.
3. **The browser warns once.** That is expected: Nox has a certificate of its own, not one from a
   public authority. To be sure it is your Nox, show the certificate in the browser (the warning
   page has a "details" or "view certificate" link) and compare the first characters of its
   **SHA-256 fingerprint** with the one shown next to the QR code (or printed by `nox pair`). They
   match: continue. This is the fingerprint of the *server* certificate, which is what the browser
   shows; it changes when Nox issues a new one (for example after a new address).
4. On the new device, a name is optional: left empty, it gets the name chosen when the code was
   made, or else its device type ("iPhone"). Tap **Pair**. Done - the dashboard opens, and stays
   paired.

A device without a camera: open `https://<this computer>:47843/dashboard/`, tap **I have a code** and
type it. If a code did not work, the pairing screen offers a field for a new one.

A paired device can pair further devices only with the security PIN - and only when one is set;
otherwise it says to do it on the computer Nox runs on or with `nox pair`.

## No warning, and Nox on the home screen

Nox makes a **certificate authority of its own** on first start. Install it on a device once and the
browser stops warning - also after the server got a new address - and the dashboard can be added to
the home screen as an app. On a paired device: tab **Devices**, **Download the certificate**. The
tile also shows the authority's own SHA-256 fingerprint: compare it with the one your device shows
for the file before you install it.

- **Android:** Settings, Security, More security settings, Encryption & credentials, Install a
  certificate, CA certificate.
- **iPhone and iPad:** open the file, Settings, Profile Downloaded, Install; then Settings, General,
  About, Certificate Trust Settings, turn on **"Nox \<computer name\> home network CA"**.
- **Windows:** open the file, Install Certificate, **Current User**, "Place all certificates in the
  following store", Trusted Root Certification Authorities.
- **Mac:** open the file in Keychain Access, double-click it, Trust, Always Trust.

**iPhone and iPad, in this order:** install the certificate, then in Safari **Share, Add to Home
Screen**, then open Nox from the home screen and **pair it there with a new code**. Safari and the
home-screen app keep separate storage, so a pairing made in Safari does not carry over; the app
opens straight to the code entry on its first start.

This authority **may only vouch for your home network**: `localhost`, names ending in `.local`, the
computer's own name, and private addresses (192.168.x.x, 10.x.x.x, 172.16-31.x.x). It is
technically limited to those (X.509 name constraints, enforced by the browser and the phone). Its
key never leaves the computer Nox runs on. If that key were stolen, it could be used to impersonate
home-network names and addresses to the devices that installed the authority - not websites on the
internet.

### Why there is a warning at all, and what could replace it

Browsers only trust certificates from public authorities, and a public authority only certifies
names it can check from the internet. A computer in your home has no such name - `192.168.1.20` and
`noxpi.local` belong to nobody - so Nox has to be its own authority, and every device either
installs that authority once or accepts the warning. This is the price of a setup that needs no
account, no domain and no connection to anyone.

A **possible later option** (not built; planned at the earliest with remote access) is a real
certificate: a free subdomain from a dynamic-DNS service plus a Let's Encrypt certificate obtained
with the DNS-01 challenge, which works without opening any port. Every browser would trust it
without a warning or an installed authority. The trade-off is privacy: the name is published in
the public Certificate Transparency logs, the DNS provider learns it and holds an API key for it,
and renewing every 90 days needs internet access and that third party. Nox would keep its own
authority as the default and offer this only as an explicit choice.

## What a paired device may do

| A paired device may | Only with the security PIN | It stays on the computer Nox runs on |
| --- | --- | --- |
| Talk to Nox, see the status, the day, reminders, the board, plans, presets, the audit trail, clips | Allow something Nox asks about at **high or critical risk** | Changing settings, secrets and accounts |
| Switch lights and scenes | Approve a plan | Relaxing privacy, resuming after the kill switch, the mode, presets, forgetting facts |
| Answer Nox's questions at lower risk - **once**: a lasting "allow" is not offered | Pair a new device | |
| Deny any question | Remove another device | |
| Pull the kill switch and panic mode, sign itself out | | |

Without a PIN set, a paired device cannot do the middle column at all; it says where that works
instead of showing a form that cannot. After **three wrong PINs** from one device, that device is
removed - before the PIN's own lockout would lock out its owner too.

## Nox's questions on every device

Every question Nox asks appears on **every** dashboard at once - the PC, the phone, the tablet - with
how many seconds are left. The first answer counts; the others see "Decided on another device".
Nobody answers within a minute: the answer is no, and every dashboard says "No answer - Nox did not
do it". A dashboard that reconnects (a phone waking up) asks which questions are still open, so it
neither misses one nor shows one that is already decided. The audit trail records which device
answered.

On the computer Nox runs on, a question can also be allowed **for 8 hours**: Nox then does the same
kind of thing again without asking, until that time is up. There is no "always".

## Removing a device

Dashboard on the computer Nox runs on, tab **Devices**, **Remove** next to the device, then **Really
remove** to confirm - no PIN needed there: removing only ever narrows access. Its open sessions end
immediately and its token never works again. A paired device can sign itself out the same way; it
removes *other* devices only with the PIN.

A **lost or stolen phone**: remove it from the computer Nox runs on, or on a server without a
screen with `nox devices` (lists them) and `nox unpair <name or id>`.

## Troubleshooting

- **The browser warns on first opening.** Expected - see "Pairing a device", step 3. Installing the
  certificate makes it go away for good.
- **Paired devices stop working after a router restart.** The computer Nox runs on probably got a
  new IP address from the router (DHCP), and the devices still try the old one. Give it a fixed
  address: in the router, reserve its address (a "DHCP reservation"; on a FRITZ!Box: Home Network,
  Network, the computer, **"Always assign this network device the same IPv4 address"**). Then pair
  the devices again.
- **Nothing on the network can reach Nox (Windows).** Windows blocks incoming connections in a
  network marked **Public**. Settings, Network & internet, your Wi-Fi or Ethernet, Network profile
  type: **Private**. Also allow Python in private networks when the firewall asks.
- **Firefox on Android keeps warning after installing the certificate.** Firefox for Android
  ignores certificates installed by the user. Use Chrome, or accept the warning.
- **Paired in Safari, but the home-screen app is not paired (iPhone, iPad).** They keep separate
  storage: pair the home-screen app with a new code.
- **"Nox is not letting devices in right now".** Network access was switched off on the computer
  Nox runs on. The pairing stays; the page reconnects by itself once it is on again.

## How it is protected

- **Only paired devices.** Without pairing, the network gets the pairing page and nothing else.
  Each device has a 256-bit token, shown to it once; the server keeps only a salted hash.
- **HTTPS only** on the network. The plain-HTTP dashboard and the per-start session token stay on
  the computer itself (loopback) and are not reachable from the network.
- **No impersonation.** A connection from the network can only ever be a paired device - never the
  desktop shell, the pet or a worker - and a device token is useless on the computer itself.
- **No foreign web pages.** Requests from another website open in the same browser are refused
  (Origin check), as are requests that reach the computer under a foreign name (Host check, against
  DNS rebinding). Every page carries a strict Content-Security-Policy.
- **No guessing.** A pairing code works once, for five minutes; wrong codes are limited per
  address and in total.
- **Only your network.** Only clients from loopback and private networks are answered
  (`ipc.lan.allow_public_clients: false`), and the certificate is only valid there. Nothing about
  this leaves your home: no account, no cloud, no telemetry.
- **Known limits** - pairing trusts the first contact, a flood of wrong codes can make pairing wait
  a minute, and several devices in the wrong hands can lock the PIN for 15 minutes - are listed in [`SECURITY.md`](SECURITY.md#known-limits-of-network-access).

## Firewall

On Windows, the first start asks whether Python may accept connections in **private** networks -
allow it. On a Linux server with `ufw`: `sudo ufw allow 47843/tcp`. Nodes (`nox node` on another
PC) connect to a second port: `sudo ufw allow 47844/tcp`, and only if you use nodes.

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
    allow_public_clients: false    # only loopback and private networks are answered
    node_port: 47844               # `nox node` on other PCs (client certificates, TLS 1.3)
    max_nodes: 10
```

Changes take effect after a restart. `nox doctor` and the dashboard's Status page show whether the
listener runs (health check `lan`).
