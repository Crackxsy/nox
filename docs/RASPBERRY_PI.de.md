# Nox auf einem Raspberry Pi (oder einem anderen Linux-Server)

Der Kern von Nox kann als Server laufen: immer an, leise, in einer Ecke der Wohnung, auf einem
Raspberry Pi oder einem anderen 64-Bit-Linux-Rechner. PC, Laptop, Handy und Tablet teilen sich dann
ein Nox – mit demselben Gedächtnis, denselben Erinnerungen und demselben Kalender –, statt dass jedes
Gerät sein eigenes hat.

> **English:** [`RASPBERRY_PI.md`](RASPBERRY_PI.md) is the same guide in English.

## Was du brauchst

| | |
| --- | --- |
| Hardware | **Raspberry Pi 4 oder 5 mit mindestens 2 GB RAM** (4 GB empfohlen) oder ein beliebiger 64-Bit-Linux-Rechner (ARM64 oder x86-64). Ein Pi 3, 400 oder Zero 2 W startet ihn auch, mit 1 GB oder weniger ist es aber knapp und nicht getestet. Besser eine SSD oder eine besonders haltbare SD-Karte: Nox schreibt oft in seine Datenbank. |
| System | **Raspberry Pi OS 64-bit** (Bookworm oder neuer), Debian 12+, Ubuntu 22.04+. Ein 32-Bit-System lehnt der Installer mit Begründung ab. |
| Netzwerk | Der Pi im selben Heimnetz wie deine anderen Geräte. |

Python, die Abhängigkeiten und das Dashboard bringt der Installer mit. Kein Node.js, kein Compiler.

## Installieren

Auf dem Pi in einem Terminal (oder per SSH):

```bash
curl -fsSL https://github.com/Crackxsy/nox/releases/latest/download/install-linux.sh | sudo bash
```

Lieber erst lesen? `curl -fsSLO <dieselbe Adresse>`, dann `less install-linux.sh` und
`sudo bash install-linux.sh`. Jedes Release enthält dazu `install-linux.sh.sha256`. Die Prüfsumme
erkennt einen beschädigten Download; sie liegt aber neben dem Skript und schützt deshalb nicht vor
einem Release, das jemand mit Schreibrechten am Projekt verändert hätte.

Das Skript zeigt jeden Schritt an und endet mit **„Nox läuft“**, sobald der Dienst antwortet. Ab
dann startet Nox nach jedem Neustart von selbst. Installiert wird die neueste Version, die unter
Linux läuft (ab v0.6.0); später wählt `nox update <version>` eine andere.

Was es tut, und sonst nichts:

- legt einen Systembenutzer `nox` ohne Anmelde-Shell an;
- legt das Programm nach `/opt/nox` (für diesen Benutzer nur lesbar) und seine Daten nach
  `/var/lib/nox`;
- installiert Python 3.13 und die Abhängigkeiten über [uv](https://docs.astral.sh/uv/), nur aus
  fertigen Paketen und nur in `/opt/nox` – das System-Python bleibt unberührt, nichts wird
  kompiliert;
- lädt das fertige Dashboard der Version herunter und lehnt es ab, wenn seine SHA-256-Prüfsumme
  nicht stimmt oder es mehr als die beiden Dashboard-Ordner enthält;
- richtet den systemd-Dienst `nox` ein (`installer/linux/nox.service`), der nur in `/var/lib/nox`
  schreiben und ins Netzwerk darf, und den Kurzbefehl `nox`.

## Erste Schritte

```bash
nox claude-login   # die KI hinter Nox (empfohlen) – siehe unten
nox onboard        # dein Name, eine PIN und die Dienste, die du verbinden willst
nox doctor         # ein ehrlicher Bericht, was geht und was nicht
```

`nox` arbeitet auf dem Pi immer mit den Daten des Dienstes, von jedem Konto aus, das `sudo` darf.
`nox onboard` startet den Dienst am Ende neu, damit deine Antworten sofort gelten.

### Die KI dahinter

Nox antwortet mit der besten KI, die er erreicht: Claude Code, dann ein lokales Modell über
Ollama, dann einfache eingebaute Regeln. Auf einem Pi ist **Claude Code** die richtige Wahl – der Pi
leitet nur weiter, gedacht wird bei Anthropic. `nox claude-login` installiert es für den Dienst und
startet die Anmeldung: `/login` eintippen, den angezeigten Link auf dem Handy oder PC öffnen, den
Code zurück ins Terminal kopieren, dann `/exit` und `sudo systemctl restart nox`.

`nox claude-login` führt den offiziellen Installer von Anthropic aus (`claude.ai/install.sh`, über
HTTPS), und zwar als Benutzer `nox` – Claude Code liegt dann neben den Daten von Nox und nirgends
sonst.

Ein lokales Modell über Ollama läuft auf einem Pi 5 mit 8 GB, braucht für eine Antwort aber viele
Sekunden. Es ist optional, der Installer richtet es nicht ein. Wenn du es willst:

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3:4b-instruct   # das Modell, das Nox von sich aus anfragt (etwa 2,5 GB)
```

Nox findet ein lokales Ollama von selbst (`127.0.0.1:11434`) und nutzt es, wenn Claude Code nicht
erreichbar ist – ohne weitere Einstellung. Das Modell wurde durch Messung ausgewählt: Es bedient die
Werkzeuge von Nox in 3 von 4 Versuchen, kleinere antworten eher mit Text, statt zu handeln
([`LOCAL_MODELS.md`](LOCAL_MODELS.md)). Ein kleineres wie `qwen3:1.7b` ist auf dem Pi schneller,
handelt aber unzuverlässiger; wer es trotzdem will, setzt `ai.providers.ollama.model: "qwen3:1.7b"`
in `user.yaml`. `nox doctor` sagt, ob das gewünschte Modell da ist.

## Im Alltag

| | |
| --- | --- |
| Läuft er? | `systemctl status nox` |
| Was tut er? | `journalctl -u nox -f` |
| Gesundheitsbericht | `nox doctor` |
| Wo liegen seine Dateien? | `nox where` |
| Einstellungen | `sudo nano /var/lib/nox/user.yaml`, danach `sudo systemctl restart nox` |
| Gibt es eine neue Version? | `nox doctor` (letzte Zeile) |
| Handy, Tablet oder PC koppeln | `nox pair` |
| Update | `nox update` |

**Updates lassen sich zurücknehmen.** Jede Version ist ein vollständiger eigener Ordner unter
`/opt/nox/releases`, und `/opt/nox/current` zeigt auf die, die läuft. `nox update` baut die neue
Version dort, während die alte weiterläuft; erst dann hält es Nox kurz an, sichert `/var/lib/nox`
als Schnappschuss nach `/var/backups/nox` (die letzten fünf bleiben, Programm-Zwischenspeicher
bleiben draußen), schaltet auf die neue Version um und startet sie. Antwortet die neue Version
nicht, schaltet der Installer von selbst zurück und holt die Daten aus diesem Schnappschuss. Der
Schnappschuss enthält auch deine Zugangsdaten und ist nur für root lesbar.

### Ein Update zurücknehmen

| | |
| --- | --- |
| Version **und** Daten zurück auf den Stand vor dem letzten Update | `nox rollback` |
| Welche Schnappschüsse es gibt | `nox snapshots` |
| Nur die Daten eines Schnappschusses zurück | `nox restore <dateiname>` |

Beim Wiederherstellen wird nie über die aktuellen Daten geschrieben: Sie werden vorher nach
`/var/lib/nox.vor-wiederherstellung-<zeit>` verschoben – was nach dem Update dazukam, ist also noch
da –, und der Befehl sagt dir das. Lösche den Ordner, sobald alles läuft; `nox update` zeigt an,
welche noch liegen. `nox rollback` geht genau einen Schritt zurück; danach gibt es keinen weiteren,
damit nie zweimal dieselben alten Daten zurückgespielt werden. Aus demselben Grund gibt es nach
einem `nox restore` kein `nox rollback` mehr. `nox restore` nimmt nur Schnappschüsse an, die Nox
selbst in `/var/backups/nox` angelegt hat, und lehnt jede andere Datei ab.

`nox update` benutzt den Installer der Version, die es installiert – aus deren Git-Stand, also aus
genau dem Code, der gleich installiert wird –, damit eine Korrektur am Installer schon bei dem
Update greift, das sie mitbringt. Wer von einem Branch installiert hat (`nox update develop`),
bleibt mit `nox update` auf diesem Branch, statt auf das ältere letzte Release zurückzufallen.

**Zeitzone.** Erinnerungen und Kalender richten sich nach der Uhr des Pi. Raspberry Pi OS Lite
startet oft in UTC; der Installer weist darauf hin. In Deutschland:
`sudo timedatectl set-timezone Europe/Berlin`.

## Von deinen anderen Geräten aus

Der Installer schaltet den Netzwerkzugriff ein: Handy, Tablet und PC im Heimnetz öffnen das
Dashboard, sobald sie gekoppelt sind – und erst dann.

```bash
nox pair            # zeigt einen QR-Code und einen Link; mit der Kamera des neuen Geräts scannen
```

Der Browser warnt beim ersten Mal, weil Nox ein eigenes Zertifikat hat – den Fingerabdruck
vergleichen, den `nox pair` zeigt, dann fortfahren. Wer das Zertifikat auf dem Gerät installiert,
sieht keine Warnung mehr und kann Nox auf den Startbildschirm legen; der Reiter **Geräte** im
Dashboard zeigt, wie, und dort koppelst du weitere Geräte oder entfernst eins. Ein gekoppeltes Gerät
darf alles im Alltag, nur nicht ändern, wie Nox eingerichtet ist. Mehr:
[`NETWORK_ACCESS.md`](NETWORK_ACCESS.md).

Firewall: Ist `ufw` aktiv, `sudo ufw allow 47843/tcp`.

## Zugangsdaten ohne Desktop

Unter Windows landen Tokens und Passwörter in der Windows-Anmeldeinformationsverwaltung. Ein Pi
ohne Desktop hat keinen solchen Schlüsselbund. Nox hält sie dort in `/var/lib/nox/secrets.json`,
lesbar nur für den Benutzer `nox` – derselbe Schutz, den ein SSH-Schlüssel auf diesem Rechner hat.
`nox where` und `nox doctor` sagen, was in Gebrauch ist. Hat eine Installation einmal gewählt,
bleibt sie dabei: Fehlt ein Schlüsselbund, den sie benutzt hat, eines Tages, meldet Nox das und
lehnt PIN-geschützte Änderungen ab, statt still mit einer leeren Datei neu anzufangen.
`security.secret_store: keyring | file` in `user.yaml` erzwingt eines von beiden. Ein Wechsel
verschiebt nichts: Bereits gespeicherte Zugangsdaten bleiben, wo sie sind, eine vorher gesetzte PIN
wirkt danach ungesetzt, und `nox where` warnt genau davor.

Plugins laufen als derselbe Benutzer `nox` und können deshalb dieselben Dateien lesen – die
Plugin-Grenze von Nox ist eine Rechteprüfung bei jedem Aufruf, keine Sandbox des Betriebssystems.
Installiere nur Plugins, denen du vertraust.

## Was läuft wo

Der Pi ist das gemeinsame Gehirn; Gerätespezifisches bleibt auf dem Gerät, zu dem es gehört.

| | Auf dem Pi | Auf deinem Windows-PC |
| --- | --- | --- |
| Gespräch, Gedächtnis, Erinnerungen, Kalender, Smart Home, Pläne | ✓ | ✓ |
| Dashboard von jedem Gerät im Netzwerk | ✓ (gekoppelte Geräte, oben) | ✓ (gekoppelte Geräte; anfangs aus) |
| Das Pet-Fenster und globale Tastenkürzel | – (kein Bildschirm) | ✓ |
| Fenster, Programme, Tippen in ein Fenster | – | ✓ |
| Sprechen und Zuhören | geplant (USB-Mikrofon und -Lautsprecher) | ✓ |
| Twitch, OBS, Rocket-League-Coach | – (gehören auf den Stream-PC) | ✓ |

## Deinstallieren

```bash
sudo systemctl disable --now nox
sudo rm /etc/systemd/system/nox.service /usr/local/bin/nox
sudo rm -r /opt/nox            # das Programm
sudo rm -r /var/lib/nox        # deine Daten: Gedächtnis, Einstellungen, Zugangsdaten – nur wenn du es wirklich willst
sudo rm -r /var/backups/nox    # die Update-Schnappschüsse
sudo userdel nox
```

Nur die beiden als Daten markierten Zeilen löschen etwas von dir. Sie stehen absichtlich einzeln da.
