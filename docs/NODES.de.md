# Nodes: Nox' Hände an deinen anderen Computern

*English: [NODES.md](NODES.md)*

Ein Haushalt hat **einen** Nox - den *Core*, am besten auf einem Raspberry Pi. Jeder andere PC
oder Laptop kann einen kleinen **`nox node`** laufen lassen: Er verbindet sich mit dem Core und
lässt Nox Dinge *auf diesem Gerät* tun - und nur die, die du dort erlaubt hast.

> Stand: Welle C entsteht gerade. Diese Version hat das Koppeln, die Verbindung, die eigene
> Erlaubnisliste und das Protokoll des Geräts und zwei Werkzeuge: `system.info` (Systemwerte
> lesen) und `notify.show` (einen Hinweis zeigen). Befehle, Dateien, Bildschirm und der Rest
> folgen.

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
   gerade bist, sobald ein Node mit `node.link` mit ihm verbunden ist - ein Gerätename oder
   `auto` für keinen); das Dashboard setzt ihn je Werkzeugfamilie unter Geräte › Computer und
   Server,
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

## Im Dashboard

Geräte › Computer und Server zeigt jeden Node: ob er verbunden ist, was Nox dort darf, wofür er
das Standardgerät ist. Sein Blatt hat einen Schalter je Gruppe:

- **Aus** geht dort sofort (ohne PIN am Rechner, auf dem Nox läuft; vom Handy mit PIN). Der Core
  schickt dann nichts mehr aus dieser Gruppe an das Gerät und sagt es dem Node (`nox/policy`),
  auch nach jedem Neuverbinden.
- **Wieder an** geht dort nur für eine Gruppe, die du dort ausgeschaltet hast, mit der PIN, wenn
  eine gesetzt ist. Was das Gerät selbst nicht erlaubt, schaltest du nur am Gerät ein:
  `nox node allow <Gruppe>` im Terminal dort (das Symbol im Infobereich zeigt nur, was erlaubt
  ist; ein Pi ohne Bildschirm hat keins).

Im selben Blatt stellst du das Standardgerät je Werkzeugfamilie ein und entfernst den Node
(„Wirklich entfernen“); Standards, die einen entfernten Node nannten, stehen danach wieder auf
`auto`. Die Anfragen heißen `node.narrow` und `node.defaults.set` (siehe
[SECURITY.md](SECURITY.md#nodes-nox-node)).

## Stoppen

Der Stopp-Knopf von Nox stoppt jeden Node in unter einer Sekunde; laufende Aktionen werden
abgebrochen. Am Gerät selbst: `nox node stop` stoppt es dort und Nox überall; aufheben kann das nur
`nox node resume` an diesem Gerät. Hört ein Node 3 Sekunden lang nichts von Nox, während er etwas
tut, bricht er das ab und wartet.

Harte Verbote (Anti-Cheat, Spieleingaben, Stream-Schlüssel, ...) prüft auch der Node selbst.

## Was der Node speichert, und wo

| Datei | Was |
| --- | --- |
| `node.key` | Der private Schlüssel des Nodes, auf diesem Gerät erzeugt und nie verschickt. Verschlüsselt, wenn das Betriebssystem einen Schlüsselbund hat (Windows-Anmeldeinformationsverwaltung, macOS-Schlüsselbund, Secret Service unter Desktop-Linux); auf einem Linux-Node ohne Bildschirm schützen ihn nur die Dateirechte (0600). |
| `node.crt`, `ca.crt`, `node.json` | Sein Zertifikat (30 Tage), die angepinnte Stelle, die Adresse des Cores |
| `node.yaml` | Was Nox hier darf |
| `audit.db` | Jeder Aufruf, erlaubt oder abgelehnt, als Hash-Kette: `nox node log` |

Ordner: `%LOCALAPPDATA%\Nox\node` (Windows), `~/.local/share/nox/node` (Linux),
`~/Library/Application Support/Nox/node` (macOS); `NOX_NODE_HOME` ändert ihn. Der Node läuft als
angemeldeter Benutzer, nie als Administrator oder root.

## Einen Node entfernen

Am Core-Rechner: `nox unpair --node Laptop`. Die Verbindung endet sofort, das Zertifikat geht nie
wieder, und der Node löscht Schlüssel und Zertifikat („Von Nox entfernt“). Ein Node, der beim
Entfernen aus war, wird beim nächsten Verbinden abgewiesen.

## Sicherheit

Siehe [SECURITY.md](SECURITY.md#nodes-nox-node). Bekannte Grenzen dieser Version: Der Node findet
den Core nur unter der Adresse aus dem Kopplungslink (Suche im Heimnetz kommt mit Welle D); das
Zertifikat wird noch nicht erneuert, also nach 30 Tagen neu koppeln.
