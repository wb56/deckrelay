# DeckRelay – verbindlicher Funktionsumfang

Stand: 3. September 2026, Planung für DeckRelay 2.0 nach Bestandsaufnahme

Dieses Dokument ist die verbindliche Abgrenzung des Funktionsumfangs für DeckRelay
2.0, 2.1 und 3.0. Historische Phasen-, Beta- und Abnahmedokumente bleiben als
Zeitaufnahmen erhalten; bei Aussagen zum heutigen Planungsstand hat diese Liste
Vorrang.

Zuverlässige Wiedergabe ist keine aufschiebbare Komfortfunktion. Fehler wie ein nicht
mehr ladendes Deck, eine nicht reaktivierbare Automatik, Audioaussetzer oder unklare
Betriebszustände müssen vor einer stabilen Freigabe behoben sein. Der inzwischen in der
2.0-Entwicklung entstandene Notfall- und Recovery-Unterbau wird deshalb als notwendige
Stabilitätsbasis behandelt und nicht als Grund, bekannte Wiedergabefehler auf eine
spätere Version zu verschieben.

## Verbindliche Versionsgrenze

| Version | Verbindlicher Umfang |
| --- | --- |
| 2.0 | Vorhandenen Wiedergabe-, Notfall-, Cue-, Lautheits- und GUI-Unterbau real abnehmen; verständliche regelbasierte Musik- und Queue-Planung vervollständigen; Session-Historie und belastbare Wiederaufnahme nach Absturz vervollständigen; anschließend Release- und Langzeitfreigabe. |
| 2.1 oder später | Betriebsprofile, Veranstaltungscheck und Konfigurationssicherheit sowie die vollständige erweiterte Wunsch- und Queue-Bedienung. Vorhandene technische Grundlagen dafür dürfen früher bestehen, machen den Produktbereich aber noch nicht fertig. |
| 3.0 | Beatmatching, Beatgrid und tonhöhenerhaltendes Time-Stretching. Ferner nur nach eigener Produktentscheidung: mehr als zwei Decks, Netzwerk-/Web-Fernsteuerung, Aufnahme, Streaming, DMX, Online-Musikdienste, KI-Empfehlungen und eine allgemeine Plugin-Schnittstelle. |

## Die fünf Schwerpunkte von DeckRelay 2.0

### 1. Notfallbetrieb und Wiederherstellung

Vorhanden:

- Deck- und System-Gesundheitszustände, Stall- und Fehlererkennung;
- lokale, vorab validierte Notfallplaylist sowie sichere Übergabe;
- isolierte Reparatur eines Deck-Backends ohne Stopp des gesunden Decks;
- Recovery-Strategien für Fortsetzen, Neustart, Überspringen und Notfalltitel;
- Ein-Deck-Betrieb, begrenzte automatische Ersatzwahl und manueller Rückfall;
- getrennte Sicherheits-Mutes, einschließlich Emergency und Panic;
- verständliche Recovery-Zustände, Fehlercodes, Audit- und Diagnoseinformationen;
- bedienergeführte globale Geräte-/Backend-Recovery als letzte Eskalationsstufe.

Für 2.0 noch erforderlich:

- reale Langzeit- und Veranstaltungsabnahme der automatischen Fehlererkennung,
  isolierten Recovery, Notfallübergabe und Rückkehr zur Automatik;
- reale Abnahme von USB-/Audiogeräteverlust und der ausdrücklich bedienergeführten
  globalen Recovery;
- Freigabe darf keine bekannte reproduzierbare Wiedergabeunterbrechung offenlassen.

### 2. Lautheitsanalyse und Cue-Punkte

Vorhanden:

- ReplayGain-Auswertung, eigene Lautheitsanalyse, Datenbankpersistenz, Zielpegel,
  Headroom, Peak-/True-Peak-Schutz und manuelle Korrektur;
- Hintergrund- und Stapelanalyse sowie Anzeige von Analysequelle und -zustand;
- automatische Signal-/Stillegrenzen, manuelle und persistente Cue In/Cue Out,
  titel- und queuebezogene Fade-Dauer sowie wirksame Cue-Snapshots;
- Cue- und Übergangsvorschau ohne Eingriff in Queue, History oder On-Air-Decks;
- sichere Behandlung kurzer Titel und Warnungen bei ungeeigneten Grenzen;
- kontextbezogene Tempoanalyse als Planungsmetadatum, ausdrücklich ohne
  Beat-Synchronisierung.

Für 2.0 noch erforderlich:

- reale Hörabnahme von Lautheit, Clip-Schutz, Cue-Grenzen und Crossfades mit
  repräsentativem MP3-/FLAC-Material;
- Langzeitabnahme der Hintergrundanalyse und der unabhängigen Normalisierung beider
  Decks;
- individuelle Fade-Kurven sowie eine ausdrücklich unterschiedliche Lautheitsbehandlung
  von Musik, Jingles und Sprache sind nicht als vollständige Funktionen belegt und für
  2.0 noch zu entwickeln.

### 3. Regelbasierte Musikauswahl und Queue-Planung

Vorhanden:

- deterministische, GUI-unabhängige Auswahlregeln und strukturierte Ablehnungsgründe;
- Titel-/Interpretensperren, Titel- und Interpreten-Wiederholungsschutz;
- Prüfung von Dateiverfügbarkeit, Metadaten, Eignung und Kurztiteln;
- unterschiedliche Queue-Quellen einschließlich manuell, Gastwunsch, automatisch,
  Playlist und Notfall; Verzeichnisimporte werden derzeit als Playlistquelle
  normalisiert;
- Prioritäten, sichere Regelentspannung, Abbruch bei fehlendem Kandidaten sowie
  Berücksichtigung bisheriger Spielhäufigkeit;
- gespeicherte BPM-, Genre-, Stimmungs-, Energie- und Bewertungsmetadaten als Grundlage;
- Queue-Zustände, Skip-/Fehlergründe, Audit-Ereignisse und gespeicherte Queues.

Für 2.0 noch zu entwickeln:

- bedienbar einstellbarer Interpretabstand statt nur technischer Sperrgrundlage;
- verständliche optionale Regeln für Genrefolgen, Tempo, Stimmung und Energie;
- nachvollziehbare Gewichtung nach Bewertung beziehungsweise Beliebtheit;
- eindeutige Produktlogik und Anzeige für Pflicht-, Wunsch- und Automatiktitel;
- Vorschau der nächsten automatisch geplanten Titel;
- sichtbare Auswahlbegründung und verständliche Erklärung übersprungener Kandidaten;
- Regeln müssen optional, deterministisch und erklärbar bleiben; ein
  undurchschaubares KI-System ist ausdrücklich nicht Teil von 2.0.

### 4. Zustandsabhängige Oberfläche

Vorhanden:

- große und kompakte Darstellung sowie getrennte Arbeitsbereiche für Livebetrieb und
  Vorbereitung;
- sichtbare Deck-, On-Air-, Queue-, Automatik-, Recovery- und Notfallzustände;
- responsive, arbeitsbereichsabhängige Fenster- und Dialoggeometrie;
- Hauptquelle, Queue-Herkunft, nächster Titel, Warnungen und Bedienhinweise;
- GUI-Dispatcher, Heartbeat, begrenzte Zeilen-/Workerpfade und inkrementelle
  Aktualisierungen.

Für 2.0 noch erforderlich:

- abschließende reale Prüfung, dass Queue, Playlist, Verzeichnis, automatische Auswahl
  und Notfallbetrieb jederzeit eindeutig und widerspruchsfrei als aktive Quelle
  erscheinen;
- reale Prüfung zustandsabhängiger Bedienbarkeit, Tastaturfokus und konkreter
  Handlungsempfehlungen in Normal-, Ein-Deck-, Recovery- und Fehlerzuständen;
- Freigabematrix für die drei vorgesehenen Windows-Auflösungs-/Skalierungsumgebungen
  einschließlich Wechsel zwischen Monitoren und Arbeitsbereichen.

### 5. Session-Historie und Wiederaufnahme nach Absturz

Vorhanden:

- persistente Veranstaltungssessions mit Status und ausgewählter Playlist;
- Wiederherstellung einer unvollendeten Session beziehungsweise Übernahme ihrer noch
  offenen Queue in eine neue Session;
- persistierte Queue-Zustände und Kennzeichnung wiederhergestellter Einträge;
- Wiedergabeverlauf mit Start, Ende, Laufzeit, Deck und Abschlussstatus;
- persistente Unterscheidung gespielt, teilweise gespielt, übersprungen, fehlgeschlagen
  und abgebrochen; Stopp und Fehler werden auf diese stabilen Abschlussklassen
  abgebildet;
- Audit-Ereignisse für zahlreiche manuelle, automatische und Recovery-Eingriffe.

Für 2.0 noch zu entwickeln:

- zusammenhängende, bedienbare Session-Historie mit vollständiger zeitlicher Sicht auf
  Wiedergaben, Übergänge und relevante manuelle Eingriffe;
- belastbarer Wiederaufnahmedialog und eindeutiger Bedienablauf nach unkontrolliertem
  Programmende;
- nachgewiesene, idempotente Wiederaufnahme von Queue, Quelle, Automatik- und
  Übergangszustand ohne Doppelzählungen;
- CSV-Export der Session-Historie; Excel-Unterstützung nur ohne neue problematische
  Abhängigkeit oder als späterer Zusatz;
- sessionbezogene Auswertung von Titeln, Interpreten, Skips und Abbrüchen;
- reale Absturz-/Neustartabnahme mit dokumentiertem Ergebnis.

## 1. Implementiert und abgeschlossen

Diese Bereiche benötigen keine weitere Funktionsentwicklung für 2.0. Normale
Regressionstests und die abschließenden Release-Gates gelten weiterhin.

- Musikbibliothek, Katalogsuche, MP3-/FLAC-Metadaten, technische Audiodaten,
  Katalogpflege und Stapeländerungen;
- unabhängige Decks A/B, Mixer, manuelle und automatische Crossfades;
- Queue-/Playlist-Grundfunktionen, gespeicherte Queues, Cue-Snapshots und stabile
  Queue-Zustände;
- Jingle-/Overlaykanal, Favoriten, Fades und Musik-Ducking;
- Equalizerauflösung und Presetverwaltung;
- Titeleditor für Cue, Lautheit und typisierte Metadaten;
- Datenbankmigration Schema 34 auf 41 sowie reale Upgrade-, Backup- und Restore-Probe;
- asynchrone, serialisierte Persistenz im zeitkritischen Übergangsabschluss;
- **1000-ms-Datenbankverzögerungsabnahme bestanden**: typischer
  Übergangsabschluss unter 15 ms in hochauflösender Gegenmessung, Maximum unter 50 ms,
  Persistenz mindestens ungefähr 1000 ms, keine kritischen Heartbeats und keine
  technische Unterbrechung von Incoming-Deck, Automatik oder Crossfade. Der
  reproduzierbare Nachweis steht in
  [database_delay_test.md](database_delay_test.md).

## 2. Implementiert, aber noch real abzunehmen

Dies sind technische Freigabeprüfungen vorhandener Funktionen, keine fehlenden
Produktfeatures:

- Notfallbetrieb, isolierte Deck-Recovery, globale bedienergeführte Recovery und
  Panic-/Notfallübergabe unter realen Fehlerbedingungen;
- Lautheit, Cue-Erkennung, Clip-Schutz, EQ und Übergänge als Hör- und Langzeitabnahme;
- Titeleditor und Analyseabläufe mit realem Tk/VLC-Betrieb;
- NAS-Wiedergabe, Quellenverlust und Wiederkehr unter realistischer Last;
- Zustands- und Quellenanzeige sowie responsive Bedienbarkeit in der Windows-Matrix;
- Speicherstabilität und mehrstündiger Veranstaltungsbetrieb;
- Release-Build, Portable-Artefakt, Abhängigkeits-/Lizenzinventar und abschließender
  Release-Candidate-Test.

## 3. Noch für Version 2.0 zu entwickeln

In empfohlener Reihenfolge:

1. Regelbasierte Planung vervollständigen: konfigurierbare, optionale Regeln,
   Gewichtung, Quellentypen, Planungsvorschau und Erklärungen.
2. Titelbezogene Übergangsvorbereitung um individuelle Fade-Kurven und die noch
   fehlende medientypbezogene Lautheitsbehandlung ergänzen.
3. Session-Historie und Absturzwiederaufnahme als vollständigen Bedienablauf samt
   Export und Auswertung vervollständigen.
4. Quellen- und Automatikzustände gegen die Produktanforderung schließen; nur dabei
   gefundene echte Funktionslücken entwickeln.
5. Die unter „implementiert, aber noch real abzunehmen“ genannten Freigabeprüfungen
   abschließen und anschließend den Release Candidate abnehmen.

## 4. Bewusst auf Version 2.1 oder später verschoben

### Betriebsprofile

Profile für Party, Hintergrundmusik, Tanz-/Vereinsveranstaltung, Empfang und manuellen
DJ-Betrieb. Ein Profil soll Quelle, Auswahlregeln, Übergänge, Lautheit,
Wiederholungsschutz, Interpretabstand, Notfallplaylist, Audioausgänge und GUI-Ansicht
gemeinsam verwalten. Persistente Einzeleinstellungen sind vorhanden; ein geschlossenes
Profilprodukt ist nicht belegt.

### Veranstaltungscheck und Konfigurationssicherheit

- Vorabprüfung von Audioausgängen, Musikquelle, Notfalltiteln und Speicherplatz;
- exportierbare Profile, automatische Konfigurationssicherung und gezielte
  Wiederherstellung einer funktionierenden Konfiguration;
- sperrbarer Veranstaltungsmodus und reduzierte Bestätigungen bei unkritischen Aktionen;
- weiterführender Soundkartenassistent, Testsignal je Ausgang, getrennte Haupt-/Preview-
  Ausgänge, Mono/Stereo und gerätebezogene Ausgangspegel.

Der vorhandene Ersteinrichtungs-, Diagnose-, Backup-/Restore- und Geräte-Recovery-Unterbau
ist eine Grundlage, erfüllt diesen Produktbereich aber noch nicht vollständig.

### Erweiterte Wunsch- und Queue-Bedienung

- eigene Bedienoberfläche für Wunschgeber, Wunschpriorität und Rücknahme;
- Drag-and-drop, Mehrfachauswahl/-verschiebung und allgemeines Queue-Undo;
- geschätzte Wartezeit und geplante Spielzeit;
- kontrollierter Abbruch einer laufenden Titelvorbereitung;
- zeitgesteuerte Jingles, Pausenmodus, geplantes Veranstaltungsende und
  Abschlussjingle/-playlist.

Service- und Datenbankgrundlagen für Gastwünsche, Duplikatschutz, Wunschgeber,
Prioritätsgrenzen, Fairness und gespeicherte Queues sind bereits vorhanden. Ohne
vollständige GUI und End-to-End-Abnahme gilt die erweiterte Wunschverwaltung dennoch
nicht als fertige 2.0-Funktion.

## 5. Für Version 3 vorgesehen

- Beatmatching;
- Beatgrid-Ermittlung und -Bearbeitung;
- tonhöhenerhaltendes Time-Stretching;
- darauf aufbauende takt-/phasenbezogene Übergangsplanung.

Kontextbezogene BPM-Analyse in 2.0 ist nur ein Planungsmerkmal und darf nicht als
Beatmatching, Beatgrid oder Time-Stretching bezeichnet werden.

Weitere mögliche Zukunftsthemen wie Web-/Smartphone-Fernsteuerung, mehrere
Bedienplätze, Aufnahme, Streaming, DMX, Online-Musikdienste, KI-Empfehlungen, mehr als
zwei Decks und eine allgemeine Plugin-Schnittstelle sind nicht Teil des verbindlichen
2.x-Umfangs und benötigen jeweils eine eigene Produkt- und Architekturentscheidung.
