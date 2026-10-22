# Nodes: Nox' Hände an deinen anderen Computern

*English: [NODES.md](NODES.md)*

Ein Haushalt hat **einen** Nox - den *Core*, am besten auf einem Raspberry Pi. Jeder andere PC
oder Laptop kann einen kleinen **`nox node`** laufen lassen: Er verbindet sich mit dem Core und
lässt Nox Dinge *auf diesem Gerät* tun - und nur die, die du dort erlaubt hast.

> Stand: Welle C entsteht gerade. Diese Version hat das Koppeln, die Verbindung, die eigene
> Erlaubnisliste und das Protokoll des Geräts und zwei Werkzeuge: `system.info` (Systemwerte
> lesen) und `notify.show` (einen Hinweis zeigen). Befehle, Dateien, Bildschirm und der Rest
> folgen.

## Einen Node installieren

| Gerät | Wie | Startet mit |
| --- | --- | --- |
| Windows | Der Nox-Installer, Modus **„Mit einem Nox im Heimnetz verbinden (nur dieses Gerät)“** (still: `Nox-Setup-<Version>.exe /MODE=node [/PAIR=<Link>]`). Ohne Oberflächen; die Sandbox-Regeln werden wie beim Core angeboten. | Eine Anmelde-Aufgabe „Nox Node“ nur für dich: läuft als du, *geringste Rechte*, nie als Dienst, nie mit Administratorrechten. Startmenü „Nox Node“ startet sie sofort. |
| Linux-Desktop | `curl -fsSL https://github.com/Crackxsy/nox/releases/latest/download/install-linux.sh \| bash -s -- --node` - als du selbst, **ohne** sudo. Braucht `git curl tar python3`. | Eine systemd-**Benutzer**-Unit `nox-node.service`. Befehl: `nox-node ...` (in `~/.local/bin`). |
| Linux ohne Bildschirm (ein Pi) | dasselbe mit `sudo bash -s -- --node --headless` | Eine gehärtete System-Unit für den eigenen Systembenutzer `nox-node`; Daten in `/var/lib/nox-node`. Befehl: `sudo nox-node ...`. |
| macOS (**Beta**) | `bash scripts/install-macos.sh --node` in einer Kopie dieses Repositorys | Ein LaunchAgent bei der Anmeldung, als du. **Dieses Skript prüft Release-Signaturen noch nicht:** sobald Signaturen eingerichtet sind, installiert es nichts aus dem Netz (keinen Tag, Branch oder Commit), nur eine Kopie, die du selbst geprüft hast (`NOX_SOURCE=<Ordner>`). |

`--pair '<Link>'` koppelt gleich mit; den Installer erneut starten aktualisiert; `--uninstall`
entfernt Programm und Autostart (Kennung und Protokoll des Nodes bleiben; das Gerät auch in Nox
entfernen). Unter Windows ist `nox-node.cmd` im Installationsordner der Befehl `nox node`.

## Einen Node koppeln

1. Am Core-Rechner im Terminal `nox pair --node` ausführen (die Geräte-Seite im Dashboard
   bekommt Nodes in einer späteren Version). Der Netzwerkzugriff muss an sein (`ipc.lan.enabled`, siehe
   [NETWORK_ACCESS.de.md](NETWORK_ACCESS.de.md)). Ist eine PIN gesetzt, fragt Nox danach.
2. Am neuen PC (mit installiertem Nox) den angezeigten Befehl ausführen, zum Beispiel

   ```text
   nox node pair "nox-node://pair?h=192.168.1.20&p=47843&c=ABCD2345&fp=…" --name Laptop
   ```

   Der Link enthält den Fingerabdruck der Zertifizierungsstelle deines Nox. Der Node holt sie,
   vergleicht sie mit diesem Fingerabdruck und schickt erst dann den Code - ein Gerät im Netz, das
   sich als Nox ausgibt, bekommt ihn nie zu sehen. Der Code gilt fünf Minuten, einmal.
3. Den Node starten: `nox node run`. Er verbindet sich, verbindet sich von selbst neu (nach 1 s,
   dann bis zu 60 s Abstand) und tut nichts, solange er nicht verbunden ist.

`nox node status` zeigt, ob er gekoppelt und verbunden ist und was Nox hier darf.

Namen: nur `A-Z a-z 0-9 ä ö ü ß`, Leerzeichen und `- _ .`, bis 40 Zeichen. Ein Name, den Nox
schon hat (auch als „LAPTOP“ oder „Kueche“ für „Küche“), wird abgelehnt - dann mit `--name` einen
anderen wählen. Nox zeigt ein Gerät immer mit einer kurzen Kennung: „Laptop · 3f2a“.

## Welches Gerät macht es?

Sag es: „Wie viel Akku hat mein Laptop?“ fragt `system.info` auf dem Laptop, „Zeig mir auf dem
Laptop einen Hinweis: ...“ zeigt ihn dort. In jedem Gespräch steht für das Modell, welche Geräte
es gibt und ob sie erreichbar sind („Geräte: dieser Core (core), Arbeits-PC (online), Laptop
(offline seit 14:02)“). Sagst du nichts:

1. gilt der Standard aus `devices.defaults` (`core`, `origin` - der PC, an dessen Dashboard du
   gerade bist, sobald ein Node mit `node.link` mit ihm verbunden ist - oder ein Gerätename),
2. sonst das einzige Gerät, das es kann,
3. sonst darf eine Frage, die nur liest, auf dem Core bleiben; etwas, das etwas verändert, wird
   **nie geraten**: Nox fragt, welches Gerät du meinst.

Ein Gerät, das aus ist, bekommt ein ehrliches „offline seit 14:02“ - Nox nimmt nie ein anderes
Gerät und hebt nichts für später auf. Ein gemerktes „Erlauben“ für den Laptop gilt nur für den
Laptop.

## Was Nox auf diesem Gerät darf

Das entscheidet die Datei `node.yaml` im Node-Ordner, und **nur dieses Gerät kann sie erweitern**:

```text
nox node allow shell     # Nox darf hier Befehle ausführen (sobald es das Werkzeug gibt); fragt nach
nox node deny notify     # keine Hinweise mehr auf diesem Gerät - sofort, auch im laufenden Betrieb
```

Standard: `system` (Systemwerte lesen), `notify` (Hinweise zeigen) und `media` (Musik steuern)
sind an; alles andere - Dateien, Befehle, Programme, Bildschirm, Kamera, Browser, Maus und
Tastatur - ist aus. Der Core kann Gruppen zeitweise abschalten, nie anschalten. `allow` geht nur in einem
interaktiven Terminal mit Bestätigung; wer `node.yaml` von Hand ändert, kann nur etwas
wegnehmen - was dazukommt, gilt erst nach `nox node allow`. `max_risk` senkt,
was der Node annimmt.

**Hohes Risiko wird hier gefragt, an diesem Gerät**: Ein Systemdialog zeigt, was Nox tun will, mit
allen Angaben, 60 Sekunden lang - keine Antwort heißt nein, und ein „Ja“ in den ersten 1,5
Sekunden zählt als nein. Ein Node ohne Bildschirm lehnt hohes Risiko ab, außer eine Person hat es
an Nox selbst bestätigt und du hast das am Gerät erlaubt: `nox node allow trust_core_for_high`. Der Node hat außerdem ein eigenes Budget: 60 Aktionen und 5 mit hohem Risiko pro Stunde
(`max_calls_per_hour`, `max_high_per_hour` in `node.yaml`).

## Stoppen

Der Stopp-Knopf von Nox stoppt jeden Node in unter einer Sekunde; laufende Aktionen werden
abgebrochen. Am Gerät selbst: `nox node stop` stoppt es dort und Nox überall; aufheben kann das nur
`nox node resume` an diesem Gerät. Hört ein Node 3 Sekunden lang nichts von Nox, während er etwas
tut, bricht er das ab und wartet.

Auf einem Desktop mit Infobereich-Symbol (Windows; Linux, wo PySide6 installiert ist) zeigt das
Symbol den Zustand - verbunden, „Nicht verbunden mit Nox“, gestoppt - und sein Menü hat **Not-Aus
hier** (auch `Strg+Alt+Umschalt+K` überall, wo ein globaler Tasten-Hook verfügbar ist), **Hier
fortsetzen** und **Was Nox hier darf**. Das Symbol schreibt nur dieselbe Stopp-Datei wie
`nox node stop`. Hinweise eines unter Windows im Node-Modus installierten Nodes tragen Nox' eigenen
Namen (AppUserModelID `Nox.Node`), nicht „Windows PowerShell“.

Harte Verbote (Anti-Cheat, Spieleingaben, Stream-Schlüssel, ...) prüft auch der Node selbst.

## Updates

Der Node aktualisiert sich **nur aus dem signierten GitHub-Release des Projekts** - der Core darf
sagen, dass es ein Update gibt, schickt aber nie Code. Etwa einmal am Tag holt der Node
`release-manifest.json` und seine Signatur und prüft, in dieser Reihenfolge (jedes „nein“ beendet
es; nichts wird geändert):

1. die installierte Version vertraut einem Release-Schlüssel - bis der Besitzer einen angelegt
   hat, heißt es „Release-Signaturen sind nicht eingerichtet“, und es gibt gar kein Update;
2. die Ed25519-Signatur ist gültig, von einem vertrauten, nicht abgelaufenen Schlüssel;
3. das Manifest ist wohlgeformt und nennt dieses Projekt;
4. keine Rückstufung, keine Wiedergabe: nicht unter der laufenden und nicht unter der höchsten
   hier gelaufenen Version, und nicht älter als das neueste schon gesehene Manifest. Eine
   Version, die hier scheiterte, wird 7 Tage übersprungen (oder mit `nox node update --yes`
   erneut versucht);
5. **von selbst** installiert sich nur ein stabiles Patch-Release ohne geänderte Bibliotheken
   (`deps_changed: false` im Manifest), dessen neuer Node ein Protokoll spricht, das **der
   gekoppelte Core annimmt** (`node_speaks`, gegen den Bereich, den der Core beim letzten
   Verbinden nannte). Minor-, Major- und Vorab-Versionen sowie ein unbekanntes oder abgelehntes
   Protokoll warten auf `nox node update` am Gerät; geänderte Bibliotheken brauchen den Installer
   (ein Hinweis sagt, was gilt);
6. das Paket `nox-app-<Tag>.zip` aus dem Release genau dieses Tags stimmt in Größe und SHA-256
   (aus derselben geöffneten Datei geprüft und entpackt);
7. es enthält nur Nox' Code und Konfiguration, jede Datei mit dem SHA-256 aus `BUNDLE.json`, und
   es wurde aus derselben `uv.lock` gebaut wie der laufende Code;
8. ein Probestart importiert den neuen Code mit diesem Python, isoliert.

Dann ein atomarer Wechsel von `app/state.json` (unter einer Dateisperre), und der Starter startet
die neue Version - nachdem er ihren Ordner bei jedem Start erneut gegen das signierte Manifest
prüft, mit Code und Schlüsseln des *installierten* Programms: eine geänderte, zusätzliche oder
fehlende Datei oder eine gefälschte Zustandsdatei startet das installierte Programm. Die neue
Version gilt als funktionierend, sobald sie verbunden ist und der Core ihr Protokoll annahm. Zwei
echte Abstürze vorher (keine Abmeldung, kein Herunterfahren, kein Stopp) oder ein Core, der ihr
Protokoll ablehnt, gehen auf die vorige Version zurück. Ein erneutes Installieren des Programms
(jeder Installer) verwirft die selbst installierten Versionen. `nox node update --check` schaut
nur; Rückgabewert 3 heißt: Release-Signaturen noch nicht eingerichtet. Wurde länger als 30 Tage
keine neuere signierte Release-Information gesehen, sagen das `nox node status` und ein Hinweis:
Updates könnten zurückgehalten werden.

Das Zertifikat (30 Tage) erneuert sich ab Tag 20 über die Verbindung, für denselben Schlüssel,
höchstens alle 12 Stunden; das alte wird widerrufen. Ein Node, der länger als 30 Tage aus war,
wird neu gekoppelt.

## Was der Node speichert, und wo

| Datei | Was |
| --- | --- |
| `node.key` | Der private Schlüssel des Nodes, auf diesem Gerät erzeugt und nie verschickt. Verschlüsselt, wenn das Betriebssystem einen Schlüsselbund hat (Windows-Anmeldeinformationsverwaltung, macOS-Schlüsselbund, Secret Service unter Desktop-Linux); auf einem Linux-Node ohne Bildschirm schützen ihn nur die Dateirechte (0600). |
| `node.crt`, `ca.crt`, `node.json` | Sein Zertifikat (30 Tage), die angepinnte Stelle, die Adresse des Cores |
| `node.yaml` | Was Nox hier darf |
| `audit.db` | Jeder Aufruf, erlaubt oder abgelehnt, als Hash-Kette: `nox node log` |
| `app/` | Versionen, die der Node selbst installiert hat (`releases/<Version>`), und `state.json`, welche läuft |
| `update.json` | Die letzte Update-Prüfung, gezeigt von `nox node status` und dem Symbol |

Ordner: `%LOCALAPPDATA%\Nox\node` (Windows), `~/.local/share/nox/node` (Linux),
`~/Library/Application Support/Nox/node` (macOS); `NOX_NODE_HOME` ändert ihn. Der Node läuft als
angemeldeter Benutzer, nie als Administrator oder root.

## Einen Node entfernen

Am Core-Rechner: `nox unpair --node Laptop`. Die Verbindung endet sofort, das Zertifikat geht nie
wieder, und der Node löscht Schlüssel und Zertifikat („Von Nox entfernt“). Ein Node, der beim
Entfernen aus war, wird beim nächsten Verbinden abgewiesen.

## Sicherheit

Siehe [SECURITY.md](SECURITY.md#nodes-nox-node). Bekannte Grenzen dieser Version: Der Node findet
den Core nur unter der Adresse aus dem Kopplungslink (Suche im Heimnetz kommt mit Welle D). Die
Windows-Anmeldeaufgabe prüft die CI mit einem Administratorkonto; dass ein Standardkonto sie
anlegen darf, ist das dokumentierte Verhalten der Aufgabenplanung für eine Aufgabe, die als dieses
Konto läuft, aber noch nicht getestet. Der macOS-Installer ist eine Beta.
