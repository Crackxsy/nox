# Netzwerkzugriff: Nox auf jedem Gerät zu Hause

> **Maßgeblich ist die englische Fassung:** [`NETWORK_ACCESS.md`](NETWORK_ACCESS.md). Diese
> Übersetzung wird im selben Pull Request mitgepflegt; weichen beide einmal voneinander ab, gilt die
> englische.

Handys, Tablets und andere PCs in deinem Heimnetz können das Nox-Dashboard öffnen – dasselbe, das
der PC zeigt –, sobald sie **gekoppelt** sind. Ein gekoppeltes Gerät redet mit Nox, sieht den Tag,
schaltet Licht, beantwortet Nox' Fragen („Darf ich diese Datei löschen?“) und kann den Not-Aus
ziehen. Ändern, wie Nox eingerichtet ist, kann es nicht; das bleibt beim Rechner, auf dem Nox läuft.

| | |
| --- | --- |
| Aus oder an | `ipc.lan.enabled` in `user.yaml` (Dashboard: Einstellungen, „Geräte“, „Andere Geräte im Heimnetz dürfen Nox öffnen“). Auf einem PC anfangs aus, auf einem Raspberry Pi, der mit `scripts/install-linux.sh` installiert wurde, **an**. Einschalten braucht die PIN, wenn eine eingerichtet ist. |
| Adresse | `https://<dieser Rechner>:47843/dashboard/` – der Reiter „Geräte“ im Dashboard und `nox pair` zeigen die genauen Links. |
| Koppeln | Ein Einmal-Code, fünf Minuten gültig, als QR-Code und als Link. |

## Bevor du koppelst: was jedes gekoppelte Gerät sieht

Ein gekoppeltes Gerät sieht **alles**, was das Dashboard zeigt: den Chat mit Nox, das Protokoll (was
Nox getan hat und was es tun sollte), Clips, den Tag, Erinnerungen und was Nox über dich weiß. Es ist
ein zweiter Bildschirm desselben Nox, keine eingeschränkte Ansicht.

Koppel also nur Geräte von Menschen, die all das sehen dürfen. **In einem Haushalt mit Kindern
koppelst du vorerst nur die Geräte der Erwachsenen** – Ansichten pro Person gibt es noch nicht.

## Ein Gerät koppeln

1. **Code erzeugen.** Am Rechner, auf dem Nox läuft: Dashboard, Reiter **Geräte**, **Code
   erzeugen**. Ist eine Sicherheits-PIN eingerichtet, wird sie hier abgefragt. Dem neuen Gerät
   kannst du dabei einen Namen geben. Auf einem Server ohne Bildschirm (einem Raspberry Pi):
   `nox pair` im Terminal oder über SSH – das zeigt einen QR-Code aus Blockzeichen, den Link und den
   Code.
2. **Scannen** mit der Kamera des neuen Geräts, oder den Link öffnen. Nichts zu installieren.
3. **Der Browser warnt einmal.** Das ist erwartet: Nox hat ein eigenes Zertifikat, keins von einer
   öffentlichen Stelle. Wer sichergehen will, dass es das eigene Nox ist: im Browser das Zertifikat
   anzeigen (die Warnseite hat dafür „Details“ oder „Zertifikat anzeigen“) und die ersten Zeichen
   des **SHA-256-Fingerabdrucks** mit dem neben dem QR-Code (oder dem von `nox pair`) vergleichen.
   Stimmen sie, fortfahren. Das ist der Fingerabdruck des *Server*-Zertifikats, also genau der, den
   der Browser zeigt; er ändert sich, wenn Nox ein neues ausstellt (zum Beispiel nach einer neuen
   Adresse).
4. Auf dem neuen Gerät ist der Name freiwillig: Bleibt das Feld leer, gilt der Name, der beim
   Erzeugen des Codes vergeben wurde, sonst der Gerätetyp („iPhone“). **Koppeln** tippen. Fertig –
   das Dashboard öffnet sich und bleibt gekoppelt.

Ein Gerät ohne Kamera: `https://<dieser Rechner>:47843/dashboard/` öffnen, **Ich habe einen Code**
tippen und ihn eingeben. Hat ein Code nicht geklappt, bietet die Seite ein Feld für einen neuen an.

Ein gekoppeltes Gerät kann weitere Geräte nur mit der Sicherheits-PIN koppeln – und nur, wenn eine
eingerichtet ist; sonst sagt es, dass das am Rechner, auf dem Nox läuft, oder mit `nox pair` geht.

## Ohne Warnung, und Nox auf dem Startbildschirm

Nox legt beim ersten Start eine **eigene Zertifizierungsstelle** an. Einmal auf einem Gerät
installiert, warnt der Browser nicht mehr – auch nicht, nachdem der Server eine neue Adresse bekommen
hat –, und das Dashboard lässt sich als App auf den Startbildschirm legen. Auf einem gekoppelten
Gerät: Reiter **Geräte**, **Zertifikat herunterladen**. Die Kachel zeigt auch den SHA-256-Fingerabdruck
der Zertifizierungsstelle selbst: Vergleiche ihn vor dem Installieren mit dem, den dein Gerät für die
Datei anzeigt.

- **Android:** Einstellungen, Sicherheit, Weitere Sicherheitseinstellungen, Verschlüsselung und
  Anmeldedaten, Zertifikat installieren, CA-Zertifikat.
- **iPhone und iPad:** Datei öffnen, Einstellungen, Profil geladen, Installieren; dann
  Einstellungen, Allgemein, Info, Zertifikatsvertrauenseinstellungen, **„Nox \<Name des Rechners\>
  home network CA“** einschalten.
- **Windows:** Datei öffnen, Zertifikat installieren, **Aktueller Benutzer**, „Alle Zertifikate in
  folgendem Speicher speichern“, Vertrauenswürdige Stammzertifizierungsstellen.
- **Mac:** Datei in der Schlüsselbundverwaltung öffnen, doppelklicken, Vertrauen, Immer vertrauen.

**iPhone und iPad, in dieser Reihenfolge:** das Zertifikat installieren, dann in Safari **Teilen,
Zum Home-Bildschirm**, dann Nox vom Home-Bildschirm aus öffnen und **dort mit einem neuen Code
koppeln**. Safari und die App auf dem Home-Bildschirm speichern getrennt, eine Kopplung in Safari
gilt dort also nicht; beim ersten Start öffnet die App direkt die Code-Eingabe.

Diese Zertifizierungsstelle **darf nur für dein Heimnetz bürgen**: `localhost`, Namen auf `.local`,
den Namen des Rechners selbst und private Adressen (192.168.x.x, 10.x.x.x, 172.16–31.x.x). Das ist
technisch festgelegt (X.509 Name Constraints, die Browser und Handy durchsetzen). Ihr Schlüssel
verlässt den Rechner, auf dem Nox läuft, nie. Würde er gestohlen, ließen sich damit gegenüber den
Geräten, die die Stelle installiert haben, Namen und Adressen im Heimnetz vortäuschen – keine
Websites im Internet.

### Warum es überhaupt eine Warnung gibt, und was sie ersetzen könnte

Browser vertrauen nur Zertifikaten öffentlicher Stellen, und eine öffentliche Stelle bescheinigt
nur Namen, die sie aus dem Internet prüfen kann. Ein Rechner bei dir zu Hause hat keinen solchen
Namen – `192.168.1.20` und `noxpi.local` gehören niemandem –, also muss Nox seine eigene Stelle
sein, und jedes Gerät installiert diese Stelle einmal oder nimmt die Warnung hin. Das ist der Preis
dafür, dass Nox kein Konto, keine Domain und keine Verbindung zu irgendwem braucht.

Eine **mögliche spätere Option** (nicht gebaut; frühestens zusammen mit dem Fernzugriff geplant) ist
ein echtes Zertifikat: eine kostenlose Subdomain bei einem Dynamic-DNS-Dienst und dazu ein
Let's-Encrypt-Zertifikat über die DNS-01-Prüfung, für die kein Port geöffnet werden muss. Jeder
Browser würde ihm ohne Warnung und ohne installierte Stelle vertrauen. Der Preis ist Privatsphäre:
Der Name steht öffentlich in den Certificate-Transparency-Logs, der DNS-Anbieter kennt ihn und hält
einen API-Schlüssel dafür, und die Erneuerung alle 90 Tage braucht Internet und diesen Dritten. Nox
würde die eigene Stelle als Standard behalten und das nur als bewusste Wahl anbieten.

## Was ein gekoppeltes Gerät darf

| Ein gekoppeltes Gerät darf | Nur mit der Sicherheits-PIN | Bleibt beim Rechner, auf dem Nox läuft |
| --- | --- | --- |
| Mit Nox reden, den Status, den Tag, Erinnerungen, das Board, Pläne, Presets, das Protokoll und Clips sehen | Erlauben, was Nox bei **hohem oder kritischem Risiko** fragt | Einstellungen, Zugangsdaten und Konten ändern |
| Licht und Szenen schalten | Einen Plan freigeben | Privatsphäre lockern, nach dem Not-Aus fortsetzen, den Modus, Presets, Fakten vergessen |
| Nox' Fragen bei geringerem Risiko beantworten – **einmal**: ein dauerhaftes „Erlauben“ gibt es nicht | Ein neues Gerät koppeln | |
| Jede Frage ablehnen | Ein anderes Gerät entfernen | |
| Not-Aus und Panikmodus auslösen, sich selbst abmelden | | |

Ohne eingerichtete PIN kann ein gekoppeltes Gerät nichts aus der mittleren Spalte; es sagt dann, wo
das geht, statt ein Formular zu zeigen, das nicht funktionieren kann. Nach **drei falschen PINs** von
einem Gerät wird dieses Gerät entfernt – bevor die Sperre der PIN selbst greift, die auch ihren
Besitzer aussperren würde.

## Nox' Fragen auf jedem Gerät

Jede Frage von Nox erscheint auf **allen** Dashboards gleichzeitig – PC, Handy, Tablet –, mit den
verbleibenden Sekunden. Die erste Antwort zählt; die anderen sehen „Auf einem anderen Gerät
entschieden.“ Antwortet niemand innerhalb einer Minute, ist die Antwort Nein, und jedes Dashboard
sagt „Keine Antwort – Nox hat es nicht getan.“ Ein Dashboard, das sich neu verbindet (ein Handy, das
aufwacht), fragt nach den noch offenen Fragen; so verpasst es keine und zeigt keine, die schon
entschieden ist. Das Protokoll hält fest, welches Gerät geantwortet hat.

Am Rechner, auf dem Nox läuft, lässt sich eine Frage auch **für 8 Stunden erlauben**: Nox tut dann
dasselbe noch einmal, ohne zu fragen, bis die Zeit um ist. Ein „immer“ gibt es nicht.

## Ein Gerät entfernen

Dashboard am Rechner, auf dem Nox läuft, Reiter **Geräte**, neben dem Gerät **Entfernen**, dann zum
Bestätigen **Wirklich entfernen** – dort ohne PIN: Entfernen schränkt den Zugang immer nur ein. Seine
offenen Verbindungen enden sofort, und sein Token funktioniert nie wieder. Ein gekoppeltes Gerät
kann sich genauso selbst abmelden; *andere* Geräte entfernt es nur mit der PIN.

Ein **verlorenes oder gestohlenes Handy**: am Rechner, auf dem Nox läuft, entfernen, oder auf einem
Server ohne Bildschirm mit `nox devices` (listet sie auf) und `nox unpair <Name oder ID>`.

## Wenn es nicht klappt

- **Der Browser warnt beim ersten Öffnen.** Erwartet – siehe „Ein Gerät koppeln“, Schritt 3. Mit
  installiertem Zertifikat verschwindet die Warnung für immer.
- **Nach einem Neustart des Routers gehen gekoppelte Geräte nicht mehr.** Der Rechner, auf dem Nox
  läuft, hat vermutlich vom Router (DHCP) eine neue IP-Adresse bekommen, und die Geräte versuchen
  es noch unter der alten. Gib ihm eine feste Adresse: im Router seine Adresse reservieren (eine
  „DHCP-Reservierung“; auf einer FRITZ!Box: Heimnetz, Netzwerk, der Rechner, **„Diesem
  Netzwerkgerät immer die gleiche IPv4-Adresse zuweisen“**). Danach die Geräte neu koppeln.
- **Aus dem Netzwerk erreicht nichts Nox (Windows).** Windows blockiert eingehende Verbindungen in
  einem Netzwerk, das als **Öffentlich** eingestuft ist. Einstellungen, Netzwerk und Internet, dein
  WLAN oder Ethernet, Netzwerkprofiltyp: **Privat**. Und Python in privaten Netzwerken erlauben,
  wenn die Firewall fragt.
- **Firefox auf Android warnt trotz installiertem Zertifikat.** Firefox für Android beachtet keine
  vom Nutzer installierten Zertifikate. Chrome nehmen oder die Warnung bestätigen.
- **In Safari gekoppelt, aber die App auf dem Home-Bildschirm ist es nicht (iPhone, iPad).** Beide
  speichern getrennt: die App auf dem Home-Bildschirm mit einem neuen Code koppeln.
- **„Nox lässt gerade keine Geräte herein.“** Am Rechner, auf dem Nox läuft, wurde der
  Netzwerkzugriff ausgeschaltet. Die Kopplung bleibt; die Seite verbindet sich von selbst wieder,
  sobald er an ist.

## Wie es geschützt ist

- **Nur gekoppelte Geräte.** Ohne Kopplung bekommt das Netzwerk die Koppel-Seite und sonst nichts.
  Jedes Gerät hat einen 256-Bit-Token, den es einmal gezeigt bekommt; der Server speichert nur einen
  gesalzenen Hash.
- **Nur HTTPS** im Netzwerk. Das Dashboard über einfaches HTTP und der Sitzungs-Token, der bei jedem
  Start neu entsteht, bleiben auf dem Rechner selbst (Loopback) und sind aus dem Netzwerk nicht
  erreichbar.
- **Kein Verkleiden.** Eine Verbindung aus dem Netzwerk kann immer nur ein gekoppeltes Gerät sein –
  nie die Desktop-Shell, das Nox-Fenster oder ein Worker –, und ein Geräte-Token nützt auf dem
  Rechner selbst nichts.
- **Keine fremden Webseiten.** Anfragen einer anderen Website im selben Browser werden abgelehnt
  (Origin-Prüfung), ebenso Anfragen, die den Rechner unter einem fremden Namen erreichen
  (Host-Prüfung, gegen DNS-Rebinding). Jede Seite hat eine strenge Content-Security-Policy.
- **Kein Raten.** Ein Koppel-Code gilt einmal, fünf Minuten lang; falsche Codes sind pro Adresse und
  insgesamt begrenzt.
- **Nur dein Netzwerk.** Geantwortet wird nur Geräten aus Loopback und privaten Netzen
  (`ipc.lan.allow_public_clients: false`), und das Zertifikat gilt nur dort. Nichts davon verlässt
  dein Zuhause: kein Konto, keine Cloud, keine Telemetrie.
- **Bekannte Grenzen** – das Koppeln vertraut dem ersten Kontakt, eine Flut falscher Codes kann das
  Koppeln eine Minute warten lassen, und mehrere Geräte in falschen Händen können die PIN 15 Minuten
  sperren – stehen in
  [`SECURITY.md`](SECURITY.md#known-limits-of-network-access).

## Firewall

Unter Windows fragt der erste Start, ob Python in **privaten** Netzwerken Verbindungen annehmen darf –
erlauben. Auf einem Linux-Server mit `ufw`: `sudo ufw allow 47843/tcp`.

## Einstellungen

```yaml
ipc:
  lan:
    enabled: false                 # true: gekoppelte Geräte im Heimnetz öffnen das Dashboard
    host: 0.0.0.0                  # jede eigene Adresse
    port: 47843
    max_devices: 20
    pair_code_ttl_s: 300           # so lange gilt ein Koppel-Code, und nur einmal
    pair_attempts_per_minute: 5    # pro Absenderadresse
    allow_public_clients: false    # nur Loopback und private Netze bekommen eine Antwort
```

Änderungen wirken nach einem Neustart. `nox doctor` und die Statusseite des Dashboards zeigen, ob der
Netzwerkzugriff läuft (Prüfung `lan`).
