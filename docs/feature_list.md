# DeckRelay – Feature-Liste

Stand: Vorbereitung DeckRelay 2.0.0-beta.1

DeckRelay ist eine eigenständige Windows-Anwendung für Musiksuche,
Veranstaltungsqueues, Zwei-Deck-Wiedergabe, automatische Übergänge sowie Jingles,
Ansagen und Effekte. Musikdateien und ihre Tags werden nicht verändert; eigene
Anwendungsdaten liegen in einer SQLite-Datenbank.

## Musikbibliothek und Suche

- Import und Verwaltung großer MP3- und FLAC-Kataloge
- Suche nach Titel, Interpret, Album, Genre und weiteren Katalogdaten
- Seitennavigation und ressourcenschonendes Lazy Loading
- Detailansicht für Katalogtitel
- Erkennung nicht verfügbarer, beschädigter oder ungeeigneter Dateien
- Gesonderte Behandlung lokaler Dateien und Netzwerkpfade/NAS
- Coveranzeige mit Hintergrundverarbeitung und Cache
- Keine Änderungen an Musikdateien oder eingebetteten Tags

## Party-Queue und Playlists

- Titel aus Katalog und Playlists zur aktuellen Party-Queue hinzufügen
- Einträge entfernen, verschieben, priorisieren, sperren und mischen
- Gastwünsche mit eigener Priorität und Wunschzähler
- Queue ersetzen oder neue Titel kontrolliert anhängen
- Queue beziehungsweise Playlist speichern und später wieder laden
- Vollständige Alben und Playlists exakt in gespeicherter Reihenfolge abspielen
- Eigene Cue-Snapshots pro Veranstaltung oder Playlist
- Effektive Restzeit wahlweise anhand der Cue-Grenzen berechnen
- Wiederhergestellte Einträge sichtbar kennzeichnen
- Zustände wie wartend, vorbereitet, laufend, gespielt, übersprungen und fehlerhaft
- Nachvollziehbare Gründe und Wiederholungsaktionen für übersprungene Titel

## Automatische Titelauswahl

- Start ab erstem wartenden oder bewusst ausgewähltem Queue-Eintrag
- Vorschau von Starttitel, spielbaren Titeln und erwarteten Sperren
- Track- und Interpreten-Wiederholungsschutz
- Übersteuerbarer Wiederholungsschutz für vollständige Album-/Playlist-Wiedergabe
- Berücksichtigung von Priorität, Gastwünschen, Dateiverfügbarkeit und Eignung
- Sichere Behandlung leerer Queues, Kurztitel und nicht spielbarer Kandidaten
- Explizite Zustände für bereit, laufend, Übergang, pausiert und beendet
- Pause und Fortsetzung nach manuellen Eingriffen ohne verlorene Queue-Einträge
- Reguläres Queue-Ende erst nach dem letzten tatsächlich spielbaren Titel
- Strukturierte Audit-Ereignisse für Start, Pause, Skips, Übersteuerungen und Ende

## Zwei Decks und Mixer

- Unabhängige Decks A und B
- Laden, Play, Pause, Fortsetzen, Stop, Seek und Eject
- Decklautstärke, Masterlautstärke und Crossfader
- Automatische und manuelle Crossfades
- Eigene Deckfarben und gut sichtbarer ON-AIR-Status
- Programmatische Crossfaderbewegungen von echten Benutzereingriffen getrennt
- Totzone gegen versehentliche minimale Crossfaderbewegungen
- Gleichzeitige Wiedergabe und getrennte Pegelsteuerung beider Decks
- Ausblendung, Einblendung und sichere Fallbacks am natürlichen Titelende

## Cue-Punkte und Übergänge

- Manuelles Cue In und Cue Out
- Titelbezogene Überblenddauer
- Queue-/veranstaltungsbezogene Cue-Überschreibungen
- Prioritätsauflösung aus Queue-Snapshot, Titelwert, Analyse und Dateigrenze
- Cue-Editor mit aktueller Position, Zurücksetzen und Validierung
- Getrennte Vorschau für Cue In, Cue Out und Fade Out
- Sicherer Vorschauplayer ohne Einfluss auf Queue, History oder On-Air-Decks
- Automatische Stille- und Signalgrenzenerkennung mit FFmpeg/FFprobe
- Analyse von Anfang und Ende statt vollständiger Audiodatei
- Konfidenz, Analysequelle, Version und technische Pegel
- Vorschläge übernehmen, korrigieren oder verwerfen
- Stapelanalyse mit Fortschritt, Abbruch und Fehlerbehandlung
- Sichere Fallbacks bei überschrittenem oder ungeeignetem Cue Out

## Lautheit und Clip-Schutz

- ReplayGain-Auswertung für unterstützte MP3-/FLAC-Metadaten
- Manuelle Gain-Korrektur pro Titel
- Hintergrundanalyse über vorhandene Analysewerkzeuge
- Quellenanzeige für native, analysierte und manuelle Werte
- Peak- und True-Peak-basierte Begrenzung
- Konfigurierbarer Headroom und Zielpegel
- Laufzeit-Clip-Schutz, sofern vom Backend unterstützt
- Geglättete Pegeländerungen ohne sprunghafte Lautstärkewechsel
- Unabhängige Normalisierung beider Decks während Crossfades
- Keine Rückschreibung von Lautheitswerten in Quelldateien

## Equalizer

- Unabhängiger Equalizer pro Deck
- Mitgelieferte Presets wie Neutral, Rock, Pop, Bluesrock und Dance
- Benutzerdefinierte Presets und dynamische VLC-Frequenzbänder
- Preamp-Schutz gegen vorhersehbares Clipping
- Die Presets Rock, Pop, Bluesrock und Dance verwenden einen Sicherheits-Preamp
  von `-3 dB`. Dadurch kann die Wiedergabe trotz unveränderter Lautstärkeregler
  hörbar leiser wirken.
- Auflösungsreihenfolge: Titel, Playlist/Queue, Genre, globaler Standard
- Temporäre Vorschau sowie dauerhafte Zuweisungen
- Presets kopieren, speichern, umbenennen und löschen
- Kompakte EQ-Anzeige direkt am Deck
- Unterschiedliche Presets gleichzeitig auf Deck A und B
- Keine EQ-Neuberechnung innerhalb zeitkritischer Crossfade-Ticks

## Jingles, Ansagen und Effekte

- Dritter, von Deck A und B unabhängiger Overlay-Audiokanal
- Jingles, Ansagen und Effekte getrennt vom Musikkatalog und der Musikhistory
- Start, Ausfaden, Stop und Wechsel eines laufenden Overlays
- Konfigurierbare Lautstärke, Fade In und Fade Out
- Optionales Ducking der Musik mit Attack und Release
- Ducking wirkt konsistent auf beide Decks, auch während Crossfades
- Sechs persistente Soundboard-Favoriten
- Tastenkürzel `Strg+1` bis `Strg+6`
- Overlay-Verwaltung mit Datei, Kategorie, Favorit und Wiedergabeeinstellungen
- Vorbereitung im Hintergrund und begrenzter Favoritencache
- Getrennte Overlay-History und Diagnosewerte
- Fehlende Dateien oder Wiedergabefehler stoppen nicht die Musik

## Titel-Editor

- Zentraler Dialog „Titel bearbeiten“
- Register für Cue, Lautheit und Metadaten
- Vollständig implementierter Cue-Bereich
- Separater Vorschauplayer für Cue In, Cue Out und Fade ohne Einfluss auf Queue,
  History oder On-Air-Decks
- Direkte Lautheitsanalyse mit Status und gespeichertem Ergebnis
- Bearbeitung typisierter Metadaten und bewusste Entscheidungen über Vorschläge
- Schreibgeschützte technische Audiodaten zu Codec, Container, Bitrate, CBR/VBR,
  Abtastrate, Bittiefe, Kanälen, Dauer, Profil und Encoder
- Anzeige von Titel, Interpret, Album, Jahr und Dateipfad
- Lokales Änderungsmodell: Speicherung erst nach ausdrücklichem Speichern
- Asynchrone Persistenz ohne Datenbankarbeit im GUI-Thread
- Schutz gegen Doppelklick und verspätete Worker-Ergebnisse
- Inkrementelle Aktualisierung nur betroffener Katalog- und Queuezeilen
- Speichern hält den Dialog für weitere Bearbeitung geöffnet

Equalizer- und Jingle-Verwaltung bleiben eigenständige Bedienbereiche und werden nicht
als fertige Titeleditor-Register dargestellt.

## Kontextbezogene Tempoanalyse

- Produktiver Stand `ffmpeg-onset-acf-v0.5` mit `tempo-profile-v3`
- Getrennte Ergebnisse für Vollaufnahme, wirksamen Cue-Bereich und Playlistkontext
- Halb-/Doppeltempo als gemeinsame Tempofamilie, ohne unzulässige Drittelpulsumrechnung
- Getrennte Aggregatkonfidenz und Rhythmusstabilität
- Diagnose der tatsächlichen Analysefenster, Rohkandidaten und Aggregationsbeiträge
- Automatische Planung nur bei hinreichend sicherem und stabilem Ergebnis
- Reale Abnahme mit CBR-MP3, VBR-MP3 und FLAC in sechs musikalischen Kategorien

## Session, Einstellungen und Datenhaltung

- SQLite-Datenbank mit versionierten Migrationen
- Wiederherstellung der letzten Session und Queue
- Persistente Mixer-, Queue-, Analyse-, EQ- und Overlay-Einstellungen
- Gespeicherte Queue-/Playlist-Zuordnungen und Cue-Snapshots
- Wiedergabehistorie mit tatsächlicher Laufzeit und Abschlussgrund
- Fehler-, Skip- und Abbruchgründe in History und Auditdaten
- Kontrollierte Behandlung verzögerter oder fehlgeschlagener Datenbankschreibvorgänge
- Serielle, idempotente Hintergrundpersistenz für zeitkritische Übergänge

## Oberfläche und Bedienung

- Moderne Dark-DJ-Oberfläche mit getrennten Akzenten für Deck A und B
- Vollbildmodus und optionaler Vollbildstart
- Anpassung an kleinere Fenster und unterschiedliche Windows-Skalierungen
- Tastaturbedienung, Fokusführung und sichtbarer Tastaturfokus
- Tooltips für kompakte Symbolschaltflächen
- Klare Leerzustände für Katalog, Queue und Decks
- Deutsche Deck-, Automatik-, Cue-, EQ- und Overlayzustände
- Kontextbezogene Hilfe für Queue, Automatik und Übergangsfälle
- Produktionsmodus mit reduzierter Diagnostik

## Diagnose und Performance

- Speichervorgang für strukturierte Diagnoseberichte
- Szenarien für Leerlauf, Wiedergabe, Crossfade, Queue-Stress, NAS und Datenbanklast
- GUI-Heartbeat und unabhängiger Watchdog
- Automatische Thread-Dumps bei kritischen GUI-Verzögerungen
- Messwerte für Rendering, Preload, Crossfade, Datenbank und Worker
- Speicherüberwachung mit RSS und optionalem `tracemalloc`
- Begrenzte Worker, Callback-Queues, Caches, Logs und Diagnosehistorien
- Virtualisierte beziehungsweise wiederverwendete Katalog- und Queuezeilen
- Change Detection zur Vermeidung unveränderter Widgetaktualisierungen
- Hintergrundverarbeitung für Cover, Analyse, Preload und Persistenz
- Belastungstest mit künstlich verzögerter SQLite-Persistenz

## Audio-Robustheit und Notfallbetrieb

- Getrennte System- und Deck-Gesundheitszustände
- Stall-Erkennung anhand echten Positionsfortschritts
- Unterschiedliche Grenzwerte für lokale und Netzwerkdateien
- Erkennung wiederholter Backendbefehlsfehler und fehlender Audiogeräte
- Deck-, Master-, Emergency- und Panic-Mute als getrennte Sicherheitsfaktoren
- Lokale, vorab validierte Notfallplaylist
- `SAFE_HANDOVER`: Notfalltitel erst stumm starten und bestätigen, dann übergeben
- Notfallwiedergabe auf einem gesunden Deck vor isolierter Reparatur
- Isolierter Austausch nur des defekten Deck-Backends
- Wiederherstellung von Titel, Cue, Position und Lautheitsfaktor
- Recovery-Richtlinien: Position fortsetzen, Titel neu starten, überspringen oder
  lokalen Notfalltitel laden
- Ein-Deck-Betrieb mit gesperrtem Crossfade und sequenziellen kurzen Blenden
- Rückkehr zum Zwei-Deck-Betrieb nur mit zwei gesunden Decks und ohne aktive Recovery
- Begrenzte Recovery- und Notfallstartversuche
- Timeouts für Erzeugung, Laden, Seek, Start, Bestätigung und Backend-Freigabe
- Stabile, maschinenlesbare Recovery- und Timeout-Fehlercodes
- Automatische, begrenzte Behandlung einzelner Lade-, Vorbereitungs- und
  Wiedergabefehler ohne Unterbrechung des gesunden Decks
- Konsistente Queue-/History-Abschlüsse, generationensichere Deckfreigabe und
  automatisches Ersatzladen nach den bestehenden Auswahlregeln
- Manueller Rückfall nach drei aufeinanderfolgenden Medienfehlern, bei fehlendem
  Ersatz oder gleichzeitigem Ausfall beider Decks

Der titelbezogene Notfallbetrieb und die isolierte Deck-Recovery sind umgesetzt.
Weiterhin ausdrücklich bedienergeführt bleiben die globale Geräte-/Backend-Recovery
als letzte Eskalationsstufe sowie die Rückkehr aus einem Zustand ohne spielbares Deck.

## Unterstützte Laufzeitumgebung

- Windows-Desktopanwendung
- VLC als Wiedergabebackend
- FFmpeg und FFprobe für automatische Cue- und Lautheitsanalyse
- MP3 und FLAC als primäre Musikformate
- Lokale Laufwerke und kontrolliert gepufferte Netzwerkpfade

## Qualitätsstand

- Automatisierte Tests für Services, Controller, Datenbank, Audio-Doubles und UI-nahe
  Abläufe
- Das aktuelle Main-Quality-Gate umfasst Ruff, Black, MyPy, reale FFmpeg-Formattests
  und die vollständige Pytest-Suite.
- Responsive Windows-Darstellungen wurden bei 1920 x 1080 mit 100 und 125 Prozent
  sowie bei 1366 x 768 mit 125 Prozent praktisch geprüft.
- Release-Build, Upgradeprobe einer realen 1.0.0-Datenbank und Portable-ZIP-Prüfung
  bleiben eigene Freigabeschritte für 2.0.0-beta.1.
