# Automatische Wiedergabe-Wiederherstellung

Stand: 2. September 2026.

## Zweck und Abgrenzung

Dieser Ablauf hält die normale Queue-Wiedergabe bei einzelnen nicht ladbaren oder
nicht abspielbaren Titeln stabil. Er ergänzt die vorhandenen Dienste für
Notfallwiedergabe, Deck-Gesundheit und isolierten Backend-Austausch; er ist keine
zweite Notfallsteuerung und führt weder Beatmatching noch Time-Stretching ein.

## Kontrollfluss vor der Vervollständigung

- `QueueService` entscheidet über Kandidaten und persistiert deren Lifecycle.
- `MainController` bereitet Medien vor, startet Decks und koordiniert Queue, History,
  Automatik und GUI.
- `DeckController` kapselt Backendbefehle und meldet deren Erfolg an den
  `DeckHealthMonitor`.
- `DeckHealthMonitor` erkennt wiederholte Befehlsfehler, unerwartete Backendzustände
  und bestätigte Wiedergabestalls.
- `EmergencyController` und `AudioRecoveryService` führen vorhandene Notfallaktionen
  beziehungsweise einen isolierten Austausch genau eines Deck-Backends aus.

Die Lücke lag zwischen Fehlererkennung und Automatik-Orchestrierung: Ein Fehler beim
automatischen Start markierte Queue und History, gab das betroffene Deck aber nicht
in jedem Pfad wieder frei. Bestätigte Laufzeitfehler wurden diagnostiziert, jedoch
nicht automatisch an die bestehende isolierte Recovery weitergegeben. Mehrere
unmittelbar fehlerhafte Kandidaten konnten außerdem zu schnell verbraucht werden.

## Verbindliches Verhalten

### Medien- und Vorbereitungsfehler

Ein fehlerhafter Queueeintrag wird genau einmal `FAILED` gesetzt. Der Eintrag bleibt
mit stabilem Fehlercode sichtbar. History und Audit erhalten Deck, Titel-ID,
Dateityp, Vorgang, bereinigte Ursache, Versuch, Entscheidung sowie vorherigen und
folgenden Zustand; vollständige Dateipfade werden nicht in die Auditdetails kopiert.

Das betroffene Deck wird logisch sofort geleert. Die möglicherweise blockierende
Backendbereinigung läuft außerhalb des GUI-Threads. Erst ein zur aktuellen
Deckgeneration gehörendes Ergebnis darf das Deck wieder freigeben und einen
Ersatztitel anfordern. Das andere Deck wird dabei weder gestoppt noch pausiert.

Nach drei aufeinanderfolgenden automatischen Medien- oder Vorbereitungsfehlern wird
kein weiterer Queueeintrag verbraucht. Die Automatik wechselt in einen stabilen,
fortsetzbaren Pausenzustand und verlangt einen manuellen Eingriff. Dasselbe gilt,
wenn kein sicherer Ersatztitel verfügbar ist oder die Deckbereinigung scheitert.

### Bestätigter Laufzeitfehler

Ein vom `DeckHealthMonitor` bestätigter Stall oder Deckfehler beendet den zugehörigen
Queue- und History-Lifecycle als Fehler. Spielt das andere Deck weiter, wird der
vorhandene Ein-Deck-Betrieb aktiviert. Das fehlerhafte Deck wird anschließend über
den vorhandenen `AudioRecoveryService` mit der Richtlinie `SKIP_TRACK` isoliert
ersetzt. Ein erfolgreicher Austausch macht das Deck wieder für den normalen Preload
verfügbar; ein fehlgeschlagener oder gleichzeitig zweiter Deckausfall führt in den
stabilen manuellen Rückfall.

### Manuelle Eingriffe und Shutdown

Manuelles Laden invalidiert ältere Preload- und Recoverygenerationen. Ein erfolgreich
manuell geladener Titel setzt die Gesundheit des Decks nachvollziehbar zurück; danach
kann die Automatik über den vorhandenen Startpfad erneut aktiviert werden. Beim
Shutdown werden sämtliche Generationen invalidiert. Späte Workerergebnisse werden
verworfen und als solche auditiert.

## Bekannte Grenzen

- Die Wiederherstellung kann defekte Medien überspringen, aber nicht reparieren.
- Ein gemeinsamer Ausfall von Audiogerät oder LibVLC-Ressource benötigt weiterhin
  die vorhandene ausdrücklich bestätigte globale Recovery.
- Ohne spielbares Gegendeck oder sicheren Ersatz wird bewusst nicht automatisch
  weitergeschaltet.
- Die musikalische Auswahl eines Ersatztitels bleibt vollständig bei den bestehenden
  Queue- und Auswahlregeln.

## Reale VLC- und Geräteabnahme

Die Abnahme wurde am 1. und 2. September 2026 mit LibVLC 3.0.23, einem realen
Windows-Audiogerät, einer isolierten Testdatenbank und ausschließlich dafür erzeugten
MP3-Testdateien durchgeführt. Produktive Musikdateien und Tags wurden nicht
verändert. Vollständige lokale Pfade sind nicht Bestandteil dieses Nachweises.

| Fall | Tatsächliches Verhalten | Ergebnis |
| --- | --- | --- |
| Nicht ladbare Datei | Der Eintrag blieb als `FAILED` sichtbar. Das Deck wurde freigegeben und vorhandene spielbare Ersatztitel wurden vorbereitet. | Bestanden |
| Nicht dekodierbare Datei | LibVLC lehnte die kontrolliert ungültige Datei ab. Der Eintrag wurde `FAILED`; GUI und übrige Wiedergabe blieben reaktionsfähig. | Bestanden |
| Fehler bei laufender Wiedergabe | Das Verschieben einer bereits vorbereiteten Datei unterbrach die Wiedergabe nicht, da LibVLC sie bereits geöffnet beziehungsweise gepuffert hatte. Der isolierte `SKIP_TRACK`-Austausch wurde zusätzlich mit dem realen Backend bestätigt. | Eingeschränkt bestanden |
| Mehrere Medienfehler | Wiederholte reale Fehlversuche pausierten kontrolliert und erzeugten keine schnelle Verbrauchsschleife. Die feste Grenze von drei aufeinanderfolgenden Fehlern ist zusätzlich automatisiert abgesichert. | Bestanden |
| Kein Ersatztitel | Der fehlerhafte Eintrag blieb erhalten, die Queue wuchs nicht weiter und die Automatik pausierte mit dem Hinweis auf den erforderlichen manuellen Eingriff. | Bestanden |
| Manueller Eingriff | Ein gültiger Titel ließ sich nach der Recovery manuell laden. Ältere Generationen blieben wirkungslos; der geladene Titel konnte anschließend einen neuen Automatiklauf starten. | Bestanden |
| Gleichzeitiger Ausfall beider Decks | Ein absichtlicher gleichzeitiger Geräte- oder Backendausfall wurde wegen des Risikos für das aktive Audiogerät nicht real erzwungen. Der stabile manuelle Rückfall ist automatisiert abgesichert. | Eingeschränkt bestanden |
| Shutdown während Recovery | Das Schließen während wiederholter Medienvorbereitung beendete Anwendung, Backend-Kindprozesse und Worker vollständig. | Bestanden |

Während eines Fehlers auf dem freien Deck spielte das andere Deck hörbar ohne
Unterbrechung weiter. Die Oberfläche blieb bedienbar. Nach erfolgreichem manuellen
Eingriff wurde der normale Deckzustand wiederhergestellt.

### Findings aus der realen Abnahme

Die Abnahme deckte drei zusammenhängende Orchestrierungsfehler auf, die im selben
Arbeitsblock korrigiert und mit gezielten Regressionstests abgesichert wurden:

- Leere Platzhalter im festen Queue-Widgetpool durften die Erzeugungsphase des
  Render-Schedulers nicht dauerhaft blockieren.
- Eine Recovery-Ersatzsuche darf keine neuen, nicht ausdrücklich geeigneten
  Katalogtitel erzeugen. `SUITABILITY_APPROVAL_REQUIRED` ist deshalb keine zulässige
  automatische Regelentspannung. Ohne vorhandenen sicheren Ersatz pausiert die
  Automatik auch dann, wenn bereits zu Beginn der Suche kein wartender Eintrag
  vorhanden ist.
- Ein gültig manuell geladenes Deck ohne Queue-Zuordnung wird als möglicher
  Starttitel eines neuen Automatiklaufs berücksichtigt und in der Startvorschau
  entsprechend ausgewiesen.

Damit sind die real sicher durchführbaren VLC-/Gerätefälle abgenommen. Die beiden
eingeschränkt bestandenen Fälle bleiben als bewusst nicht destruktiv reproduzierte
Grenzen dokumentiert; ihre Steuerungs- und Rückfallpfade sind automatisiert geprüft.

## Abschlussgate

Nach den drei Korrekturen aus der realen Abnahme wurde der vollständige Stand am
2. September 2026 erneut geprüft:

- 48 gezielte Emergency-/Recovery-Regressionstests bestanden;
- 458 angrenzende Tests für Controller, Queue, Automatik, Decksteuerung,
  Audio-Recovery, Emergency-Verhalten und Shutdown bestanden;
- vollständige Testsuite: 1.434 Tests bestanden, 6 erwartungsgemäß übersprungen;
- Ruff für `src` und `tests` bestanden;
- Black-Prüfung für `src` und `tests`: 306 Dateien unverändert;
- MyPy für alle 164 Produktionsmodule ohne Befund;
- `git diff --check` bestanden.

Die sechs lokalen Skips betrafen die drei realen FFmpeg-Formattests und die drei
entsprechenden Spawn-Prozesstests für CBR-MP3, VBR-MP3 und FLAC. Im ersten Gate des
Draft-PR liefen die drei realen Formattests mit den über ``PATH`` bereitgestellten
Werkzeugen erfolgreich. Die Spawn-Prozesstests blieben dagegen übersprungen, weil
ihre Werkzeugerkennung ausschließlich das lokale ``.tools/ffmpeg``-Bundle prüfte.
Diese Testinfrastrukturlücke wird in einem Folgecommit durch eine portable Auflösung
aus explizitem Testpfad, lokalem Bundle oder ``PATH`` geschlossen. Produktionslogik,
Analysealgorithmus und Emergency-Recovery-Verhalten bleiben davon unberührt.

Für dieses Abschlussgate wurde kein Build erzeugt. Der Arbeitsblock ist damit auf
Code-, Test-, Dokumentations- und realer VLC-/Geräteebene abgeschlossen.
