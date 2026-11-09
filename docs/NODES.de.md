# Nodes: Nox' Hände an deinen anderen Computern

*English: [NODES.md](NODES.md)*

Ein Haushalt hat **einen** Nox - den *Core*, am besten auf einem Raspberry Pi. Jeder andere PC
oder Laptop kann einen kleinen **`nox node`** laufen lassen: Er verbindet sich mit dem Core und
lässt Nox Dinge *auf diesem Gerät* tun - und nur die, die du dort erlaubt hast.

> Stand: Welle C ist fertig. Ein Node koppelt sich mit einem Code, hat seine eigene
> Erlaubnisliste, sein eigenes Protokoll und Budget, fragt Hohes Risiko am Gerät selbst und bietet
> Systemwerte, Hinweise, Musik, Dateien in Ordnern, die du erlaubst, Befehle in seiner eigenen
> Sandbox, Bildschirm- und Kamerabilder, einen Browser, Fenster und Programme, Maus und Tastatur
> (Windows) sowie die Satelliten für Sprache, OBS und das Pet-Overlay. Programme installieren
> (`package.*`) gibt es auf Nodes **noch nicht** - siehe
> [Programme installieren](#programme-installieren-noch-nicht-verfügbar). Der Node findet den Core
> nur unter der Adresse aus seinem Kopplungslink; die Suche im Heimnetz kommt mit Welle D.

Zwei Regeln gelten überall unten. **Jeder Aufruf geht durch zwei Prüfungen:** die
Berechtigungsprüfung des Cores und die eigene Prüfung des Nodes - jede kann ablehnen, nur beide
zusammen erlauben. **Seine harten Grenzen entscheidet das Gerät selbst:** Dafür behandelt der Node
den Core als nicht vertrauenswürdig. Spielsperre, harte Verbote, eine am Gerät ausgeschaltete
Gruppe und der eigene Dialog des Geräts für Hohes Risiko gelten also auch gegen einen Core, der
übernommen oder so verändert wurde, dass er alles erlaubt.

## Einen Node installieren

| Gerät | Wie | Startet mit |
| --- | --- | --- |
| Windows | Der Nox-Installer, Modus **„Mit einem Nox im Heimnetz verbinden (nur dieses Gerät)“** (still: `Nox-Setup-<Version>.exe /MODE=node [/PAIR=<Link>]`). Ohne Oberflächen; die Sandbox-Regeln werden wie beim Core angeboten. | Eine Anmelde-Aufgabe „Nox Node“ nur für dich: läuft als du, *geringste Rechte*, nie als Dienst, nie mit Administratorrechten. Startmenü „Nox Node“ startet sie sofort. |
| Linux-Desktop | `curl -fsSL https://github.com/Crackxsy/nox/releases/latest/download/install-linux.sh \| bash -s -- --node` - als du selbst, **ohne** sudo. Braucht `git curl tar python3`. | Eine systemd-**Benutzer**-Unit `nox-node.service`. Befehl: `nox-node ...` (in `~/.local/bin`). |
| Linux ohne Bildschirm (ein Pi) | dasselbe mit `sudo bash -s -- --node --headless` | Eine gehärtete System-Unit für den eigenen Systembenutzer `nox-node`; Daten in `/var/lib/nox-node`. Befehl: `sudo nox-node ...`. |
| macOS (**Beta**) | `bash scripts/install-macos.sh --node` in einer Kopie dieses Repositorys | Ein LaunchAgent bei der Anmeldung, als du. **Dieses Skript prüft Release-Signaturen noch nicht:** sobald Signaturen eingerichtet sind, installiert es nichts aus dem Netz (keinen Tag, Branch oder Commit), nur eine Kopie, die du selbst geprüft hast (`NOX_SOURCE=<Ordner>`). |

`--pair '<Link>'` koppelt gleich mit; den Installer erneut starten aktualisiert; `--uninstall`
entfernt Programm und Autostart (Kennung und Protokoll des Nodes bleiben; das Gerät auch in Nox
entfernen). Unter Windows ist `nox-node.cmd` im Installationsordner der Befehl `nox node`, und der
Installer hat eine Seite „Mit Nox verbinden“, auf der du den Kopplungslink einfügst (oder sie leer
lässt und später koppelst).

## Einen Node koppeln

1. **Einen Code bei Nox erzeugen.** Im Dashboard: Geräte & Fähigkeiten › Geräte › Computer und
   Server › **PC oder Laptop hinzufügen** - das System des Geräts wählen, einen Namen geben und
   den Knopf drücken. Oder im Terminal am Core-Rechner: `nox pair --node`. Der Netzwerkzugriff
   muss an sein (`ipc.lan.enabled`, siehe [NETWORK_ACCESS.de.md](NETWORK_ACCESS.de.md)). Einen
   Node koppeln gibt Nox einen PC in die Hand, deshalb fragt Nox nach der Sicherheits-PIN, wenn
   eine gesetzt ist - und von einem gekoppelten Handy oder Tablet immer (ohne gesetzte PIN werden
   Nodes nur an dem Rechner gekoppelt, auf dem Nox läuft).
2. **Am neuen PC koppeln.** Nox zeigt einen einmaligen Link - als Text, als fertigen Befehl und als
   QR-Code mit demselben Link - und den Fingerabdruck seiner Zertifizierungsstelle. Den Link auf
   der Seite „Mit Nox verbinden“ des Windows-Installers einfügen oder den angezeigten Befehl
   ausführen, zum Beispiel

   ```text
   nox node pair "nox-node://pair?h=192.0.2.20&p=47843&c=ABCD2345&fp=…" --name Laptop
   ```

   Der Link enthält den SHA-256-Fingerabdruck der Zertifizierungsstelle deines Nox. Der Node holt
   sie, vergleicht sie mit diesem Fingerabdruck und schickt erst dann den Code - ein Gerät im
   Netz, das sich als Nox ausgibt, bekommt ihn nie zu sehen. Deshalb gibt es für einen Node auch
   **keine Zertifikatswarnung im Browser, die man wegklicken müsste**: Die Stelle ist über den
   Link angepinnt, nicht beim ersten Kontakt geglaubt. Der Code gilt fünf Minuten, einmal. Der
   Node erzeugt seinen eigenen Schlüssel (P-256) und bekommt dafür ein Client-Zertifikat für 30
   Tage; der private Schlüssel verlässt das Gerät nie. Nox nimmt bis zu 10 Nodes an
   (`ipc.lan.max_nodes`).
3. **Den Node starten:** `nox node run` (das erledigen Anmelde-Aufgabe, Benutzer-Unit oder
   LaunchAgent bei der Anmeldung). Er verbindet sich mit dem Node-Port des Cores (47844),
   verbindet sich von selbst neu (nach 1 s, dann bis zu 60 s Abstand) und tut nichts, solange er
   nicht verbunden ist. Das Blatt im Dashboard springt von „Wartet auf das Gerät …“ auf „… ist
   verbunden“, sobald er da ist.

`nox node status` zeigt, ob er gekoppelt und verbunden ist und was Nox hier darf.

Namen: nur `A-Z a-z 0-9 ä ö ü Ä Ö Ü ß`, Leerzeichen und `- _ .`, bis 40 Zeichen. Ein Name, den Nox
schon hat (auch als „LAPTOP“ oder „Kueche“ für „Küche“), wird abgelehnt - dann mit `--name` einen
anderen wählen. Nox zeigt ein Gerät immer mit einer kurzen Kennung, die es nicht selbst wählen
kann: „Laptop · 3f2a“.

## Welches Gerät macht es?

Sag es: „Wie viel Akku hat mein Laptop?“ fragt `system.info` auf dem Laptop, „Zeig mir auf dem
Laptop einen Hinweis: ...“ zeigt ihn dort. In jedem Gespräch steht für das Modell, welche Geräte
es gibt und ob sie erreichbar sind (etwa „Geräte: dieser Core (core), Arbeits-PC (online), Laptop
(offline seit 23.10. 14:02)“). Sagst du nichts:

1. gilt der Standard aus `devices.defaults` (`core`, `origin` - der PC, an dessen Dashboard du
   gerade bist, sobald ein Node mit `node.link` mit ihm verbunden ist - ein Gerätename oder
   `auto` für keinen); das Dashboard setzt ihn je Werkzeugfamilie im Blatt des Nodes,
2. sonst das einzige Gerät, das es kann,
3. sonst darf eine Frage, die nur liest, auf dem Core bleiben; etwas, das etwas verändert, wird
   **nie geraten**: Nox fragt, welches Gerät du meinst.

Ein Gerät, das aus ist, bekommt ein ehrliches „offline seit 14:02“ - Nox nimmt nie ein anderes
Gerät und hebt nichts für später auf. Ein gemerktes „Erlauben“ für den Laptop gilt nur für den
Laptop.

## Was Nox auf diesem Gerät darf

Das entscheidet die Datei `node.yaml` im Node-Ordner, und **nur dieses Gerät kann sie erweitern**:

```text
nox node allow shell     # Nox darf hier Befehle ausführen, in der Sandbox des Geräts; fragt nach
nox node allow files --folder "C:\Users\Alex\Documents\Nox"   # ein Ordner, den Nox öffnen darf
nox node deny files --folder "C:\Users\Alex\Documents\Nox"    # diesen Ordner wieder wegnehmen
nox node deny notify     # keine Hinweise mehr auf diesem Gerät - sofort, auch im laufenden Betrieb
```

| Gruppe | Was Nox darf | Standard | Hinweise |
| --- | --- | --- | --- |
| `system` | `system.info`: Betriebssystem, CPU, Speicher, Platte, Akku, Temperatur, Laufzeit lesen | an | überall |
| `notify` | `notify.show`: einen Hinweis zeigen | an | braucht einen Benachrichtigungsdienst (Windows, macOS, `notify-send` unter Linux) |
| `media` | `media.control`: abspielen, pausieren, springen, Lautstärke, stumm | an | Windows und Linux; noch nicht unter macOS |
| `files` | `file.list`, `file.read`, `file.write` in Ordnern, die du erlaubst | aus | standardmäßig kein Ordner |
| `shell` | `shell.run` in der Sandbox dieses Geräts | aus | Windows (AppContainer), Linux (bubblewrap); unter macOS abgelehnt |
| `packages` | Programme installieren | aus | **auf Nodes noch nicht verfügbar** |
| `screen` | `screen.capture` | aus | jedes Mal Hohes Risiko, braucht eine sichtbare Benachrichtigung |
| `camera` | `camera.snapshot` | aus | wie `screen`; der Kamera-Schalter des Cores ist standardmäßig aus |
| `browser` | `browser.*` in Nox' eigenem unsichtbarem Browser | aus | nur öffentliche Seiten |
| `desktop` | Fenster auflisten, nach vorn holen, minimieren, wiederherstellen, schließen; ein Programm beenden; Text tippen | aus | nur Windows |
| `computer` | Maus und Tastenkombinationen in einer Sitzung | aus | nur Windows; `allow` fragt zweimal |
| `voice` | der Sprach-Satellit | aus | braucht zusätzlich den Schalter des Besitzers im Dashboard |
| `obs` | `obs.request` an das OBS auf diesem PC, das Pet-Overlay-Relay | aus | |

Der Core kann Gruppen abschalten, nie anschalten. `allow` geht nur in einem interaktiven Terminal
mit Bestätigung; wer `node.yaml` von Hand ändert, kann nur etwas wegnehmen - was dazukommt, gilt
erst nach `nox node allow`. `max_risk` senkt, was der Node annimmt (Standard `high`; `critical`
nimmt er nie an). Beim Koppeln werden nur diese Standards bestätigt: Eine `node.yaml`, die schon
da war, wird nicht übernommen.

### Hohes Risiko wird am Gerät gefragt

Ein Systemdialog an diesem Gerät zeigt, was Nox tun will, mit allen Angaben, 60 Sekunden lang -
keine Antwort heißt nein, und ein „Ja“ in den ersten 1,5 Sekunden zählt als nein. Solange er offen
ist, werden andere laufende Aktionen mit Wirkung gestoppt und neue abgelehnt (nur Lesen läuft
weiter). Ein Node ohne Bildschirm lehnt Hohes Risiko ab, außer eine Person hat es an Nox selbst
bestätigt und du hast das am Gerät erlaubt: `nox node allow trust_core_for_high`. Der Node hat
außerdem ein eigenes Budget, unabhängig von dem des Cores: 60 Aktionen und 5 mit Hohem Risiko pro
Stunde (`max_calls_per_hour`, `max_high_per_hour` in `node.yaml`).

## Dateien und Befehle auf einem Node

- **Dateien** (`files`): Nox öffnet nur Pfade in den Ordnern, die du mit `--folder` erlaubt hast
  (standardmäßig keine; nie ein ganzes Laufwerk, dein Benutzerordner, ein System- oder
  App-Daten-Ordner oder ein Ordner mit Schlüsseln oder Passwörtern), und entscheidet das an der
  echten Datei in dem Moment, in dem sie geöffnet wird - ein Link oder eine Verknüpfung
  (Junction) woandershin, `..`, `~`, ein Kurzname wie `PROGRA~1`, ein Netzwerkpfad
  (`\\server\freigabe`) oder ein Gerätepfad wird abgelehnt, ebenso alles im Node-Ordner, im
  Schlüsselbund, in `~/.ssh` und ähnlichen Schlüsselablagen und in Browser-Profilen.
- **Schreiben** legt nur Textdateien an, in zwei Stufen. *Einfacher Text* - `.txt .md .csv .tsv
  .log .ics .vcf .srt`: eine neue Datei ist mittleres Risiko (Frage an Nox). *Strukturierter
  Text* - `.json .yaml .yml`, den ein Programm lesen kann: jedes Schreiben ist hohes Risiko und
  wird an diesem Gerät gefragt, die Datei wird immer ganz geschrieben (kein Anhängen), und der
  Text muss sich schlicht einlesen lassen - YAML mit Tag, Anker oder Alias wird abgelehnt.
  Dokumente, Webseiten, PDFs und Medien (`.html .xml .svg .pdf .docx .xlsx .pptx .odt .png .mp3
  ...`) schreibt Nox auf einem Gerät gar nicht - stattdessen `.md` oder `.txt`. Ersetzen oder
  Anhängen ist immer hohes Risiko. Nie eine versteckte Datei oder etwas in einem versteckten
  Ordner (`.git`, `.vscode`, `.bashrc`, ...), nie etwas in einem Repository (ein Ordner mit
  `.git`, `.hg`, `.svn`, `.jj`, `.bzr`, `_darcs` oder `.sl`) oder einem App-Arbeitsbereich, dessen
  Notizen Code ausführen können (ein Obsidian-Tresor mit `.obsidian`, ein Logseq-Graph, Foam,
  Dendron, ein `.vscode`-/`.idea`-Projekt) - im erlaubten Ordner oder um ihn herum -, nie eine
  Datei ohne Endung, ein Skript, ein Programm oder eine Konfigurationsdatei, nach der Werkzeuge
  handeln (`settings.json`, `package.json`, Compose- und Pipeline-Dateien, `configuration.yaml`,
  alles in `workflows`, ...), nie eine ausführbare Datei oder eine mit zweitem Namen (Hardlink).
  Die Frage zeigt Datei, Art, Größe, Zeilenzahl und einen SHA-256 und den Inhalt - ganz, wenn er
  kurz ist, sonst die ersten und letzten 200 Zeichen mit „content shortened - N characters not
  shown“. Schreiben wird nie gemerkt, und eine Profilregel, die Schreiben an Nox erlaubt, erlaubt
  es auf einem Node nur, wenn sie genau dieses Gerät nennt (`device:` mit seiner vollen Kennung).
  Eine `.md`-Datei wird abgelehnt, wenn sie irgendetwas enthält, das eine Notiz-App ausführen
  könnte - die Wörter `dataviewjs`, `js-engine`, `templater` oder `meta-bind-js`, Templater `<%`,
  inline `` `$= ``, `<script`, `javascript:` -, egal wo es steht. Eine Notiz, die diese
  Plugin-Wörter nur erwähnt (oder einen Link mit `obsidian://` oder `file:`), kann Nox deshalb
  nicht schreiben. Codeblöcke in Notizen, die Nox auf Geräten schreibt, dürfen nur Text- oder
  Datenformate enthalten (`text`, `md`, `json`, `yaml`, `toml`, `ini`, `csv`, `diff`, `log`) oder
  keine Sprache nennen - ein Block in einer Programmiersprache (ein Plugin kann ihn per Klick
  ausführen), für ein Plugin (`dataview`, `jsx`, ...) oder einer, den die App zeichnet
  (`mermaid`, `math`, `html`), wird abgelehnt. Beim Anhängen zählt die ganze Datei, und die
  Ablehnung sagt, was stört. Notizen sind höchstens 256 KB groß, JSON-/YAML-Dateien 64 KB.
- **Dein Notizen-Tresor:** `nox node allow files --folder` lehnt einen Ordner ab, der ein
  Repository oder ein App-Arbeitsbereich ist oder darin liegt - auch einen Tresor, dessen
  Einstellungsordner umbenannt wurde (erkannt an seinem Inhalt). Erlaube einen einfachen Ordner
  daneben - etwa `Nox` neben dem Tresor - und verschiebe, was Nox geschrieben hat, selbst hinein.
  **Verlinke den erlaubten Ordner nicht (Symlink, Junction) in einen Tresor:** Von der Seite des
  Ordners aus lässt sich das nicht erkennen. Die Einstellungsordner von Apps in deinem
  Benutzerordner (`~/.vscode`, `~/.idea`) zählen nicht als Arbeitsbereich - ein Tresor dort
  (`~/.obsidian`) schon.
- **Hingenommen bei einfachem Text:** Eine `.csv` kann eine Tabellenformel enthalten, die eine
  Tabellenkalkulation erst nach eigener Warnung und deinem Klick ausführt; die anderen einfachen
  Formate führen nichts aus.
- **Befehle** (`shell`) laufen nur in der eigenen Sandbox des Geräts (AppContainer unter Windows,
  bubblewrap unter Linux), die den Node-Ordner nicht sieht - unter Linux lehnt der Node Befehle
  ab, wenn sein Ordner unter einem Systemordner liegt, den die Sandbox zeigt. Ohne eine solche
  Sandbox - vorerst macOS, Linux ohne `bwrap` - werden Befehle abgelehnt, nie ohne ausgeführt.
  Hohes Risiko: gefragt an diesem Gerät, bei einem Node ohne Bildschirm an Nox. Ein Stopp beendet
  den ganzen Prozessbaum in der Sandbox.

## Programme installieren: noch nicht verfügbar

`package.install` und `package.remove` gibt es nur auf dem Rechner, auf dem Nox selbst läuft. Auf
einem Node werden sie nicht angeboten, und der Core leitet sie nicht dorthin: Ein Paketmanager
braucht Rechte, die die Sandbox des Nodes nicht gibt (eine systemweite Installation braucht
Administrator oder root; selbst eine Installation nur für den Benutzer schreibt außerhalb jedes
Ordners, den die Sandbox sehen darf). `nox node allow packages` lässt sich setzen, ändert aber noch
nichts.

Das ist eine **offene Entscheidung** des Projekt-Besitzers zwischen zwei Entwürfen:

- **Ein privilegierter Hilfsdienst** auf dem Gerät mit einer festen, kleinen Menge an Anfragen
  (ein Paket aus der offiziellen Quelle installieren oder entfernen, sonst nichts) - wie der
  root-Helfer des Nox-Pi-Images. Er kann alles installieren, was der Paketmanager kann; seine
  Anfragen brauchen deshalb den Dialog des Geräts und eine enge Erlaubnisliste.
- **Ein Sandbox-Profil für Paketmanager**, das nur Installationen für den Benutzer in die eigenen
  Ordner des Paketmanagers erlaubt. Sicherer, kann aber nicht alles installieren (systemweite
  Programme, Treiber).

Was das heute heißt: „Installier VLC auf dem Laptop“ geht auf dem Laptop nicht - Nox sollte das so
sagen, und du installierst es dort selbst. Bietet Nox trotzdem eine Installation an, liefe sie auf
dem Rechner, auf dem Nox läuft, nicht auf dem Laptop: prüfe, was die Frage zeigt, und lehne ab.

## Bildschirm, Kamera, Browser, Fenster, Maus und Tastatur, Musik

Alles aus, außer Musik; jedes wird nur hier eingeschaltet, mit `nox node allow <Gruppe>`.

### Bildschirm und Kamera

Jedes Bild (`screen`, `camera`) ist Hohes Risiko - der Dialog an diesem Gerät fragt jedes Mal, und
es wird nie gemerkt. Bevor das Bild entsteht, sagt eine Benachrichtigung hier „Nox nimmt gerade ein
Bildschirmfoto auf“ (oder „ein Kamerabild“); kann keine Benachrichtigung erscheinen, gibt es kein
Bild (`no_visible_indicator`) - ein Node ohne Desktop macht also keins. Das Bild bleibt nur für
diesen einen Aufruf im Speicher und geht nur an das Nox, das gefragt hat; der Node schreibt
nichts. Auch Nox selbst muss Aufnahmen erlauben: Privatsphäre-Modus FULL oder BALANCED und der
Aufnahme-Schalter für diese Art an (der der Kamera ist standardmäßig aus), geprüft, bevor der Node
gefragt wird, und noch einmal, wenn das Bild ankommt. Nox legt das Bild in seinen eigenen
Aufnahmen-Ordner; Kamerabilder gehen nie an ein Cloud-Modell. Privatsphäre-Zonen des Bildschirms
am Node werden noch nicht angewendet.

### Browser

`browser`: Nox' eigener unsichtbarer Browser mit einem Profil im Node-Ordner, nie deiner; er
erreicht nur öffentliche Seiten (kein Heimnetz, kein Loopback), wie an Nox selbst. Lesen ist
niedriges Risiko, Klicken, Tippen und Absenden mittleres (Frage an Nox, für einen Node nie
gemerkt). Keine Seiten-Screenshots auf einem Node.

### Fenster und Programme

`desktop`, nur Windows: Fenster und Programme auflisten, ein Fenster nach vorn holen, minimieren,
wiederherstellen oder schließen (niedrig/mittel), ein Programm beenden oder Text in ein Fenster
tippen (hoch: Frage hier). Windows selbst, Nox und ein laufendes Spiel werden nie angefasst.
Fenstertitel von einem Node werden verborgen, solange Nox im Panik- oder abgesicherten Modus ist.

### Maus und Tastatur (Computersteuerung)

`computer`, nur Windows (sonst abgelehnt): `nox node allow computer` fragt zweimal. Eine Sitzung
wird jedes Mal an diesem Gerät erfragt (hoch), dauert höchstens 15 Minuten und 200 Aktionen, zeigt
eine Benachrichtigung und endet mit Strg+Alt+Umschalt+K, dem Not-Aus, einer verlorenen Verbindung
oder ihrer Zeit. Darin darf Nox bewegen, klicken, scrollen und Tastenkombinationen drücken - nie
in Nox' eigene Fenster, nie während hier eine Frage offen ist. Dafür muss der Browser auf diesem
PC im Dashboard mit dem Node verknüpft sein (`node.link`), damit Nox erkennt, welcher Browser auf
dem PC läuft, den es steuert. Das Dashboard zeigt währenddessen „Nox steuert gerade Maus und
Tastatur auf <Gerät>“ mit „Stopp“.

### Die Spielsperre

Solange auf diesem Gerät ein Spiel läuft oder vorne ist, wird jeder Maus-, Tasten- und
Tipp-Aufruf und jede Änderung an Fenstern und Programmen abgelehnt - vom Node selbst, vor allem
anderen, egal was Nox schickt. Als Spiel zählt:

- was auf einer beobachteten Liste steht: der des Cores (`sensors.game.process_names`, bei jeder
  Verbindung an den Node geschickt; der Core kann nur hinzufügen) oder der eigenen dieses Geräts
  (`nox node games add StarfallArena.exe`; Entfernen fragt nach: `nox node games remove
  StarfallArena.exe`);
- jede laufende Anti-Cheat-Software - Prozess, Dienst oder Kernel-Treiber;
- etwas, das den Bildschirm belegt: ein Spiel im Vollbild, eine beschäftigte Vollbild-App oder
  der Präsentationsmodus - so werden auch Spiele erkannt, die auf keiner Liste stehen;
- wenn der Node es nicht feststellen kann (ein unlesbares Vordergrundfenster, eine Prüfung, die
  fehlschlägt) - dann lehnt er ebenfalls ab.

Ein Anti-Cheat-Treiber, der mit Windows startet, läuft immer - Maus und Tastatur bleiben auf diesem
PC gesperrt, solange er installiert ist. Das ist Absicht: Nox kann nicht wissen, ob das Spiel
gleich startet. Auch ein Video im Vollbild zählt als „belegt den Bildschirm“. Fenster und Programme
auflisten geht weiterhin. `game.input.send` ist überall ein hartes Verbot und lässt sich nie
erlauben.

### Die Sitzungssperre und die PIN

Während Nox Maus und Tastatur auf einem PC steuert, könnte Nox dort selbst auf „Erlauben“ bei
seiner eigenen Frage klicken. Deshalb gilt, solange die Sitzung läuft (maßgeblich ist, was der Node
meldet, und die Sperre hält auch eine Weile, nachdem ein Node verstummt ist):

- **Der Browser auf diesem PC** kann weder etwas erlauben noch ändern; er zeigt statt eines
  PIN-Felds „von hier geht Erlauben nicht“. Ein gekoppeltes Gerät, das sich von der Adresse dieses
  PCs verbindet, zählt ebenfalls als dieser Browser.
- **Jedes andere gekoppelte Gerät und der Rechner, auf dem Nox läuft,** können nur mit der
  **Sicherheits-PIN** erlauben und ändern. Am Rechner, auf dem Nox läuft, geht das nicht über eine
  Fernsteuerung (ein Pi, auf dem Nox als Dienst läuft, hat keine: dort gilt die PIN).
- **Keine PIN gesetzt:** Bis zum Ende der Sitzung kann nirgends etwas erlaubt oder geändert werden.
- **Ablehnen und Stopp gehen immer**, und mit Nox reden (Chat, Sprechtaste) geht ohne PIN - außer
  vom Browser auf dem gesteuerten PC. Nachrichten anderer gekoppelter Geräte zählen während der
  Sitzung als ungeprüft: Alles, was daraus Daten nach draußen schickt, fragt vorher.
- **Ein PC, der verstummt ist:** Bleibt seine Zeile im Dashboard stehen, löst „Sperre lösen“ mit
  der PIN am Rechner, auf dem Nox läuft, die Sperre; sonst endet sie von selbst spätestens 120
  Minuten nach der letzten Meldung des Nodes. Ein Node, der weiter eine Sitzung meldet, sperrt
  sofort wieder.

Fernsteuerungs-Software, die in die eigene Konsolensitzung des PCs zeichnet, lässt sich nicht von
einer Person am Gerät unterscheiden; dann ist die PIN der Schutz (siehe
[SECURITY.md](SECURITY.md#accepted-limits-nodes)).

### Musik

`media`, standardmäßig an, niedriges Risiko; der Node sagt es einmal per Hinweis: abspielen,
pausieren, weiter, zurück, Lautstärke, lauter, leiser, stumm - unter Windows über die
System-Mediensteuerung, unter Linux über MPRIS (`playerctl` oder D-Bus) und PipeWire (`wpctl`) oder
PulseAudio (`pactl`). Noch nicht unter macOS. „Mach auf dem Arbeits-PC die Musik leiser“ führt dort
`media.control` ohne Rückfrage aus.

## Sprache, OBS und das Pet-Overlay (Satelliten)

Drei Dinge, die ein Node über seine Hände hinaus für Nox tun kann - alle standardmäßig aus, jedes
nur hier eingeschaltet (`nox node allow voice`, `nox node allow obs`). Wie sie arbeiten, steht in
`satellites.yaml` im Node-Ordner (nur Einstellungen, nichts Geheimes, nie *ob*).

### Sprach-Satellit

`voice`: Nox hört und spricht auf diesem Gerät - etwa ein Wohnzimmer-Pi mit einem
USB-Lautsprechermikrofon.

- **Zwei Schlüssel.** `nox node allow voice` hier *und* „Zuhören und sprechen einschalten“ für
  dieses Gerät im Dashboard (im Blatt des Nodes; mit der PIN, wenn eine gesetzt ist, von einem
  gekoppelten Gerät immer). Bis dahin steht die Gruppe dort als ausgeschaltet. Den zweiten
  Schlüssel prüft der Core bei jeder Äußerung erneut.
- **Zuhören** geht standardmäßig per Push-to-Talk (`mode: push_to_talk`): `Strg+Alt+Leertaste`
  halten, wo das Symbol läuft, oder `nox node voice talk` ausführen und Enter drücken, wenn du
  fertig bist. Mit `mode: wake_word` bleibt das Mikrofon offen und Nox reagiert auf seinen Namen
  („Nox, ...“).
- **Text, nie Audio.** Die Spracherkennung läuft hier (faster-whisper, `tiny` auf einem Pi, sonst
  `base` - `stt_model` ändert es); nur der erkannte **Text** geht an Nox, höchstens 500 Zeichen pro
  Äußerung. Audio verlässt das Gerät nie und wird nie gespeichert; die Protokolle halten fest, dass
  etwas gehört wurde und wie lang es war, nie die Worte. Die Antwort kommt als Text zurück und wird
  hier gesprochen (Supertonic 3, Stimme F3; Piper, wenn Supertonic fehlt). Die Sprachmodelle
  einmal mit der Sprach-Einrichtung laden (`python -m nox.worker --download-supertonic
  --models-dir <Node-Ordner>/models`); von selbst wird nichts geladen.
- **Du siehst und hörst, wenn es zuhört:** Das Symbol zeigt „Nox hört zu“, solange das Mikrofon
  offen ist, und ein kurzer Ton erklingt, wenn Push-to-Talk beginnt und wenn Nox deine Frage
  angenommen hat (`earcon: false` schaltet die Töne aus). `nox node voice status` sagt dasselbe im
  Terminal.
- **Stumm und Privatsphäre.** `nox node voice mute` schaltet das Mikrofon hier stumm (auch im Menü
  des Symbols); nur dieses Gerät hebt das auf (`nox node voice unmute`). Auch Nox' Privatsphäre
  schließt es: der Mikrofon- bzw. Aufnahme-Schalter, Panik, abgesicherter Modus, eine
  Privatsphäre-Zone und der Stopp-Knopf. Während Nox Maus und Tastatur auf diesem PC steuert, hört
  es ihm nicht zu.
- **„Nox, Notaus“** (oder „Nox, emergency stop“) stoppt diesen Node - und damit Nox überall - und
  kommt nie als Text bei Nox an.
- **Weniger Vertrauen.** Ein Mikrofon im Raum hört, wer gerade da ist, nicht unbedingt dich. Was es
  hört, wird bei Nox ein eigenes Gespräch (`voice:node:<Kennung>`), gespeichert oder nicht nach
  Nox' Privatsphäre-Modus, und dieses Gespräch gilt vom ersten Wort an als eines mit fremden
  Inhalten: Alles, was Daten nach draußen schickt, fragt vorher, ein anderswo gegebenes „für diese
  Sitzung merken“ gilt dafür nicht, und die Frage zeigt „von Sprach-Satellit <Name>“. Was Nox
  daraus lernt, wartet auf deine Bestätigung. **Erlaubnisse gibt es nie per Stimme.** „Hier“ heißt
  dieses Gerät: „Mach hier die Musik leiser“. Es wartet, bis Nox im Dashboard fertig ist, statt es
  zu unterbrechen (höchstens 90 s). Schlösser, Alarmanlagen, Garagentore und Ventile sind wie für
  jeden Kanal unerreichbar - dafür hat Nox kein Werkzeug.
- **Keine Abkürzungen aus dem Raum.** Presets und Moduswechsel starten von hier nicht - nur ein
  Preset, das du „Auch über Sprach-Satelliten“ markiert hast (mit der PIN, wenn eine gesetzt ist),
  und dann folgt jeder Schritt den normalen Regeln; der Modus wechselt per Stimme von einem
  Satelliten nie, und ein dort gesagtes „neues Gespräch“ verwirft das Gespräch nicht und beendet
  seine Erlaubnisse nicht.
- **Echo-Fenster.** Während Nox hier spricht und 0,3 s danach nimmt es Gehörtes nicht als Frage,
  damit es sich nicht selbst antwortet; auch der Core verwirft eine Äußerung von einem Node, der in
  der letzten Sekunde gesprochen hat. „Nox, Notaus“ geht trotzdem, ebenso Push-to-Talk, das Nox
  unterbricht. Seinen eigenen Namen oder einen Stopp-Satz sagt Nox nie über diesen Lautsprecher,
  und es spricht nur lateinische Buchstaben, Ziffern und übliche Satzzeichen - Wörter in anderen
  Schriften, Symbole und Emoji fallen weg, damit niemand seinen Namen aus ähnlich aussehenden
  Zeichen zusammensetzen kann.
  Eine Frage pro „Nox“: Die nächste braucht den Namen wieder. Der Node spricht nur Antworten auf
  eine Frage, die er in den letzten drei Minuten gestellt hat, höchstens 40 Sätze.
- **Grenzen.** Höchstens 12 Äußerungen pro Minute und Node; weitere werden abgelehnt. Ein Node, der
  30 in einer Minute schickt, gilt als flutend, und seine Verbindung wird getrennt.
- **Gezielt etwas sagen:** `voice.say` (geringes Risiko; gefragt, wenn das Gespräch fremde Inhalte
  enthält): „Sag im Wohnzimmer, dass das Essen fertig ist“. Seinen eigenen Namen oder einen
  Stopp-Satz sagt es nie.

### OBS

`obs`: OBS läuft auf diesem PC, Nox auf einem anderen.

- `nox node obs password` legt das obs-websocket-Passwort im Schlüsselbund dieses Geräts ab; es
  geht nie an Nox' Core. Der Node verbindet sich nur mit dem OBS auf diesem PC (`127.0.0.1`,
  Port `4455` - `obs: port:` ändert ihn).
- In `satellites.yaml` die Privatsphäre-Szene dieses OBS und die Szenen nennen, zu denen Nox
  wechseln darf (`obs: privacy_scene: "Technical Problems"`, `scene_set: ["Live", "Pause"]`).
- `obs.request` geht hierher, wenn du es sagst („auf dem Stream-PC“) oder das Dashboard dieses
  Gerät zum Standard für OBS macht. Die Regeln sind dieselben wie auf dem Core: Lesen läuft sofort,
  Änderungen werden bestätigt, folgenreiche (live gehen, aufnehmen, löschen) jedes Mal - im Dialog
  auf diesem PC -, und den Stream stoppen oder den Stream-Schlüssel lesen geht nie. Ein
  Szenenwechsel über `obs.request` auf einem Node wird immer hier gefragt (Nox' Core kennt die
  Szenenliste dieses PCs nicht). Die Privatsphäre-Szene und alles darin lassen sich nicht ändern.
- Der Panik-Knopf schaltet dieses OBS auf seine Privatsphäre-Szene (auch wenn dieser PC aus war
  und während der Panik wiederkommt); danach ändert Nox hier nichts mehr in OBS, bis du auf diesem
  PC `nox node obs resume` ausführst - das wirkt sofort, auch während der Node läuft.

### Pet-Overlay

Das Pet als OBS-Browserquelle auf diesem PC, vom Nox zuhause.

- Mit erlaubtem `obs` stellt der Node das Pet nur auf diesem PC bereit. `nox node overlay` zeigt
  die Adresse für eine OBS-Browserquelle (oder einen Browser hier), etwa
  `http://127.0.0.1:47845/pet/?overlay=1&device=<id>#overlay=relay`. Die Adresse enthält kein
  Geheimnis: Der Node ergänzt seinen eigenen Nur-Lese-Zugang, erneuert ihn bei jeder Verbindung und
  verliert ihn, wenn der Node entfernt oder OBS für ihn im Dashboard ausgeschaltet wird.
- Das Pet bewegt den Mund zu dem, was dieses Gerät sagt (nur dieses Overlay bekommt das). Ohne
  Verbindung zeigt es „offline“ und verbindet sich von selbst neu.
- Alles, was auf diesem PC läuft, kann diese Adresse auch öffnen und sieht, was das Overlay sieht
  (wie das Pet aussieht und seine Sprechpegel, nie was gesagt wird).

## Im Dashboard

Geräte & Fähigkeiten › Geräte › Computer und Server zeigt jeden Node: ob er verbunden ist
(„Verbunden“, „Offline seit ...“, „Update nötig“), was Nox dort darf, wofür er das Standardgerät
ist, seine Version. Sein Blatt hat einen Schalter je Gruppe:

- **Aus** geht dort sofort (ohne PIN am Rechner, auf dem Nox läuft; vom Handy mit PIN). Der Core
  schickt dann nichts mehr aus dieser Gruppe an das Gerät und sagt es dem Node (`nox/policy`),
  auch nach jedem Neuverbinden.
- **Wieder an** geht dort nur für eine Gruppe, die du dort ausgeschaltet hast, mit der PIN, wenn
  eine gesetzt ist. Was das Gerät selbst nicht erlaubt, schaltest du nur am Gerät ein:
  `nox node allow <Gruppe>` im Terminal dort (das Symbol im Infobereich zeigt nur, was erlaubt
  ist; ein Pi ohne Bildschirm hat keins - dort ein Terminal nutzen, etwa über SSH).

Im selben Blatt stellst du das Standardgerät je Werkzeugfamilie ein, siehst „Zuletzt hier getan“
aus dem Protokoll und entfernst den Node („Wirklich entfernen“, zweimal gedrückt); Standards, die
einen entfernten Node nannten, stehen danach wieder auf `auto`. Die Anfragen heißen `node.narrow`
und `node.defaults.set` (siehe [SECURITY.md](SECURITY.md#nodes-nox-node)).

## Stoppen

- **Der Stopp-Knopf von Nox stoppt jeden Node in unter einer Sekunde:** Der Not-Aus erreicht jeden
  verbundenen Node über seine offene Verbindung, laufende Aktionen werden abgebrochen (bei einem
  Befehl in der Sandbox der ganze Prozessbaum), eine Sitzung mit Maus und Tastatur endet, und der
  Node lehnt alles ab, bis Nox fortsetzt.
- **Am Gerät selbst:** `nox node stop`, **Not-Aus hier** im Symbol-Menü oder
  `Strg+Alt+Umschalt+K` (wo ein globaler Tasten-Hook verfügbar ist) - oder „Nox, Notaus“ an einem
  Sprach-Satelliten - stoppt es dort und Nox überall; den eigenen Stopp des Nodes hebt nur
  `nox node resume` (oder **Hier fortsetzen**) an diesem Gerät auf.
- **Totmannschalter:** Hört ein Node 3 Sekunden lang nichts von Nox, während er etwas tut, bricht
  er das ab und lehnt alles ab, bis Nox wieder da und nicht gestoppt ist.

Auf einem Desktop mit Infobereich-Symbol (Windows; Linux, wo PySide6 installiert ist) zeigt das
Symbol den Zustand - verbunden, „Nicht verbunden mit Nox“, Update nötig, gestoppt - und sein Menü
hat **Not-Aus hier**, **Hier fortsetzen**, **Was Nox hier darf** und, mit Sprache, den
Mikrofon-Schalter. Das Symbol schreibt nur dieselbe Stopp-Datei wie `nox node stop`. Hinweise eines
unter Windows im Node-Modus installierten Nodes tragen Nox' eigenen Namen (AppUserModelID
`Nox.Node`), nicht „Windows PowerShell“.

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

Ein Node, dessen Protokoll der Core nicht annimmt, zeigt „Update nötig“ und bietet bis zum Update
nur `system.info` an.

Das Zertifikat (30 Tage) erneuert sich ab Tag 20 über die Verbindung, für denselben Schlüssel,
höchstens alle 12 Stunden. Das alte bleibt gültig, bis der Node sich zum ersten Mal mit dem neuen
verbindet (oder es von selbst endet): Geht die Antwort unterwegs verloren oder geht die Uhr des
Nodes nach (bis zu einer Stunde wird angenommen), arbeitet er weiter und fragt 10 Minuten später
erneut. Ein Node, der länger als 30 Tage aus war, wird neu gekoppelt.

Die bewusst hingenommenen Restrisiken der Selbst-Updates stehen in
[SECURITY.md](SECURITY.md#accepted-limits-nodes).

## Was der Node speichert, und wo

| Datei | Was |
| --- | --- |
| `node.key` | Der private Schlüssel des Nodes, auf diesem Gerät erzeugt und nie verschickt. Verschlüsselt, wenn das Betriebssystem einen Schlüsselbund hat (Windows-Anmeldeinformationsverwaltung, macOS-Schlüsselbund, Secret Service unter Desktop-Linux); auf einem Linux-Node ohne Bildschirm schützen ihn nur die Dateirechte (0600). |
| `node.crt`, `ca.crt`, `node.json` | Sein Zertifikat (30 Tage), die angepinnte Stelle, die Adresse des Cores und die Kennung des Nodes |
| `node.yaml` | Was Nox hier darf |
| `manifest.key` | Nur ohne Schlüsselbund: der Schlüssel, der die Bestätigungen von `node.yaml` signiert (0600) |
| `audit.db` | Jeder Aufruf, erlaubt oder abgelehnt, als Hash-Kette: `nox node log` |
| `app/` | Versionen, die der Node selbst installiert hat (`releases/<Version>`), und `state.json`, welche läuft |
| `update.json` | Die letzte Update-Prüfung, gezeigt von `nox node status` und dem Symbol |
| `satellites.yaml` | Wie Sprache, OBS und das Overlay hier arbeiten (nichts Geheimes) |
| `voice.json`, `voice-muted`, `voice-ptt` | Was der Sprach-Satellit gerade tut (für das Symbol), der Stummschalter, Push-to-Talk gehalten |
| `obs-panic` | Da nach dem Panik-Knopf: Nox ändert hier nichts in OBS bis `nox node obs resume` |

Ordner: `%LOCALAPPDATA%\Nox\node` (Windows), `~/.local/share/nox/node` (Linux),
`~/Library/Application Support/Nox/node` (macOS); `NOX_NODE_HOME` ändert ihn. Der Node läuft als
angemeldeter Benutzer, nie als Administrator oder root. Bilder, Audio und erkannte Sprache werden
auf dem Node nicht gespeichert; das obs-websocket-Passwort liegt nur im Schlüsselbund.

## Einen Node entfernen

Im Dashboard („Wirklich entfernen“ im Blatt des Nodes) oder am Core-Rechner:
`nox unpair --node Laptop`. Entfernen braucht am Rechner, auf dem Nox läuft, keine PIN (es nimmt
nur Zugriff weg). Die Verbindung endet sofort, jedes Zertifikat des Nodes kommt auf die Sperrliste
und geht nie wieder, seine gemerkten Erlaubnisse enden, Standards, die ihn nannten, stehen wieder
auf `auto`, und ein verbundener Node löscht Schlüssel und Zertifikat („Von Nox entfernt“). Ein
Node, der beim Entfernen aus war, wird beim nächsten Verbinden abgewiesen. Wie man ein solches
Gerät neu koppelt, steht unter
[Fehler bei Zertifikat und Zertifizierungsstelle](#fehler-bei-zertifikat-und-zertifizierungsstelle).

`--uninstall` am Gerät entfernt Programm und Autostart; das Gerät auch in Nox entfernen.

## Fehlersuche

`nox node status` am Gerät und die Zeile des Nodes im Dashboard sind die ersten Anlaufstellen;
`nox node log` zeigt jede Entscheidung des Nodes mit ihrem Grund.

### Der Node zeigt „offline“

- **Läuft er?** `nox node status` sagt `not running`, `disconnected` (er erreicht Nox nicht),
  `connected`, `update_needed`, `pin_mismatch` oder „Removed from Nox“. Mit `nox node run`
  starten oder die Anmelde-Aufgabe „Nox Node“ (Windows), `systemctl --user status nox-node`
  (Linux-Desktop) bzw. `sudo systemctl status nox-node` (ohne Bildschirm) prüfen.
- **Ist der Netzwerkzugriff an?** Der Node-Listener läuft nur, solange `ipc.lan.enabled` an ist;
  sonst sagt das Dashboard „Computer verbindest du, sobald andere Geräte Nox öffnen dürfen.“
- **Erreicht er den Port des Cores?** Siehe [Firewall](#firewall-port-47844) unten.
- **Hat sich die Adresse des Cores geändert?** Der Node verbindet sich nur mit der Adresse aus
  seinem Kopplungslink (die Suche im Heimnetz kommt mit Welle D). Gib dem Core im Router eine
  feste Adresse oder kopple neu.
- **„Update nötig“** heißt, der Core nimmt das Protokoll des Nodes nicht an: den Node (oder den
  Core) aktualisieren; bis dahin liest Nox dort nur Systemwerte.

### Der Kopplungscode ist abgelaufen oder wurde abgelehnt

Ein Code gilt fünf Minuten und nur einmal. Der Node sagt, warum es nicht ging (die Meldungen sind
englisch): „The code has expired“ oder „was already used“ - einen neuen erzeugen; „Too many
attempts. Wait a minute.“ - falsche Codes sind je Adresse und insgesamt begrenzt; „Nox has as many
nodes as it accepts“ - erst einen entfernen; ein Name, der einem vorhandenen Gerät ähnelt - mit
`--name` einen anderen wählen; „This device is already paired“ - siehe den nächsten Abschnitt.

### Fehler bei Zertifikat und Zertifizierungsstelle

- **„The device answering is not the Nox from the link (its fingerprint differs). Nothing was
  sent.“** Unter der Adresse im Link hat eine andere Zertifizierungsstelle geantwortet, als der
  Link nennt. Prüfe, dass der Link von deinem eigenen Nox kommt und du in deinem eigenen Netz bist;
  kopple nie über ein Netz, das du nicht kontrollierst.
- **„Nox: neu koppeln nötig“ / `pin_mismatch`:** Die auf dem Node gespeicherte Stelle passt nicht
  mehr zu der, die er beim Koppeln angepinnt hat; der Node hält an und nimmt keine andere an.
- **Das Zertifikat ist abgelaufen** (der Node war länger als 30 Tage aus) oder **der Node wurde
  entfernt, während er aus war:** Er kann sich nicht mehr verbinden und muss neu gekoppelt werden.
- **Die Zertifizierungsstelle des Cores wurde ersetzt** (etwa eine Neuinstallation mit neuem
  Datenordner): Jeder Node muss neu gekoppelt werden.

Ein Gerät neu koppeln: in Nox entfernen (falls es noch da steht), den Node am Gerät stoppen,
`node.json`, `node.crt` und `node.key` aus dem Node-Ordner löschen und dann `nox node pair` mit
einem neuen Link ausführen. Neu koppeln beginnt mit den Standardgruppen; was du brauchst, mit
`nox node allow` wieder erlauben.

### Firewall: Port 47844

Der Node wählt sich ein; der **Core** muss ihn annehmen. Das Koppeln nutzt den Netzwerk-Port 47843
des Cores, der laufende Node verbindet sich mit dem Node-Port 47844 (`ipc.lan.node_port`). Auf
einem Windows-Core Python in privaten Netzwerken erlauben, wenn die Firewall fragt. Auf einem
Linux-Core mit `ufw`: `sudo ufw allow 47843/tcp` und `sudo ufw allow 47844/tcp`. Das Nox-Pi-Image
öffnet beide selbst, solange der Netzwerkzugriff an ist, nur aus privaten Netzen. Der Node selbst
braucht keinen offenen Port; das Pet-Overlay-Relay (47845) lauscht nur auf seinem Loopback. Siehe
[NETWORK_ACCESS.de.md](NETWORK_ACCESS.de.md#firewall).

### Die Spielsperre lehnt ab

Eine Ablehnung `game_running`, `game_in_foreground` oder `windows_only` kommt vom Node selbst und
lässt sich von Nox aus nicht übergehen. Prüfe:

- ob ein beobachtetes Spiel - oder sein Launcher - noch im Hintergrund läuft;
- ob Anti-Cheat-Software installiert ist und läuft (ein Treiber, der mit Windows startet, hält Maus
  und Tastatur dauerhaft gesperrt);
- ob etwas im Vollbild oder im Präsentationsmodus ist (ein Video zählt);
- `nox node games remove <Programm>` entfernt nur die eigenen Einträge des Geräts; die Liste des
  Cores änderst du in den Einstellungen des Cores (`sensors.game.process_names`), und was der Core
  hinzugefügt hat, lässt sich am Node nicht entfernen;
- Maus, Tastatur und Fenster gibt es nur auf Windows-Nodes (`windows_only`).

### Der Sprach-Satellit hört nicht zu

`nox node voice status` ausführen. Der Reihe nach:

- **„Voice: off“** - `nox node allow voice` am Gerät;
- **„Muted here: yes“** - `nox node voice unmute` am Gerät;
- **„Core says: no (...)“** - der Grund liegt bei Nox: Das Gerät ist im Dashboard nicht
  eingeschaltet (der zweite Schlüssel), der Mikrofon- oder Aufnahme-Schalter ist aus, Nox ist im
  Panik- oder abgesicherten Modus, eine Privatsphäre-Zone ist aktiv, Nox ist gestoppt, oder Nox
  steuert Maus und Tastatur auf diesem PC;
- **„Running: no“** oder eine Zeile **„Problem:“** - die Sprachverarbeitung ist nicht gestartet:
  meist fehlen die Sprachmodelle (einmal laden, siehe oben) oder es wurde kein Mikrofon gefunden;
- mit `mode: push_to_talk` wird ohne die Taste oder `nox node voice talk` nichts gehört; mit
  `mode: wake_word` beginnt jede Frage mit „Nox“;
- während Nox hier spricht und kurz danach, wird Gehörtes nicht als Frage genommen.

### Ein Update ist gescheitert oder wurde zurückgenommen

`nox node status` zeigt die letzte Prüfung in der Zeile „Update:“. „Release signing is not
configured“ heißt, es gibt noch keine Selbst-Updates (Rückgabewert 3 von `nox node update
--check`). Eine gescheiterte Version wird 7 Tage übersprungen; `nox node update --yes` versucht sie
erneut. Eine Version, die zweimal abgestürzt ist, bevor sie sich verbunden hat, oder deren
Protokoll der Core abgelehnt hat, wurde auf die vorige zurückgesetzt - mit der arbeitet der Node
weiter. Ein Release mit geänderten Bibliotheken braucht den Installer; ein Minor- oder
Major-Release wartet auf `nox node update` am Gerät. Eine Warnung, dass seit 30 Tagen kein neueres
signiertes Release gesehen wurde, heißt, dass Updates unterwegs zurückgehalten werden könnten:
das Netz prüfen oder das aktuelle Release mit dem Installer installieren.

## Sicherheit

Siehe [SECURITY.md](SECURITY.md#nodes-nox-node) für das Bedrohungsmodell des Nodes und
[Accepted limits (nodes)](SECURITY.md#accepted-limits-nodes) für alle hingenommenen Grenzen an
einer Stelle. Bekannte Grenzen dieser Version: Der Node findet den Core nur unter der Adresse aus
dem Kopplungslink (Suche im Heimnetz kommt mit Welle D); Programme auf Nodes installieren ist eine
offene Entscheidung. Die Windows-Anmeldeaufgabe prüft die CI mit einem Administratorkonto; dass ein
Standardkonto sie anlegen darf, ist das dokumentierte Verhalten der Aufgabenplanung für eine
Aufgabe, die als dieses Konto läuft, aber noch nicht getestet. Der macOS-Installer ist eine Beta.
