# Changelog

Alle wesentlichen Änderungen an DeckRelay werden in dieser Datei dokumentiert.

## [2.0.0-beta.1] - Unveröffentlicht

> **Planungsstatus:** Dieser Abschnitt beschreibt bereits implementierte Änderungen der
> unveröffentlichten Beta, nicht den vollständigen verbleibenden Umfang der stabilen
> Version 2.0. Die verbindliche Abgrenzung zwischen fertig, noch abzunehmen, noch zu
> entwickeln sowie 2.1/3.0 steht in `docs/feature_list.md`.

### Added

- Eine responsive Oberfläche bietet große und kompakte Darstellungen sowie getrennte
  kompakte Arbeitsbereiche für Livebetrieb und Vorbereitung. Decks, Queue, Automatik,
  Katalog und wichtige Aktionen bleiben auch bei kleineren Arbeitsflächen erreichbar.
- Dialoge für Titeleditor, Backup, Systemdiagnose und externe Programme berücksichtigen
  den verfügbaren Windows-Arbeitsbereich und halten ihre Hauptaktionen sichtbar.
- Der erweiterte Titeleditor verbindet Cue-Bearbeitung und Vorhören, Lautheitsanalyse
  sowie typisierte Metadatenpflege. Speichern übernimmt Änderungen, ohne den Dialog
  automatisch zu schließen.
- Die Katalogpflege unterstützt Filter, Vorschauen, reversible Stapeländerungen und
  nachvollziehbare Entscheidungen über Analysevorschläge.
- Der Titeleditor zeigt schreibgeschützte technische Audiodaten aus dem tatsächlichen
  FFprobe-Dateiinhalt einschließlich Codec, Container, belastbarem MP3-CBR-/VBR-Status,
  Bitrate, Abtastrate, Bittiefe, Kanälen, Layout und technischer Dauer. Die asynchrone
  Ermittlung verwendet einen snapshotgebundenen Cache und verwirft veraltete Ergebnisse.
- Vollständige Vergleichsdiagnose für getrennte Gesamt- und Cue-Tempoanalysen mit
  tatsächlichen Analysefenstern, Rohwerten, Aggregationsbeiträgen und eindeutigen
  Zuständen für wartende, laufende, abgeschlossene, fehlgeschlagene und abgebrochene
  Läufe.
- Titelvarianten werden anhand ihres konkreten Formats und Dateinamens unterscheidbar.
  Beim Entfernen aus dem Katalog und bei der Übernahme in Queue oder Playlist bleibt
  die ausgewählte Version eindeutig.
- Lautheitswerte können direkt im Titeleditor analysiert und anschließend als eigener
  gespeicherter Analysestand angezeigt werden.

### Changed

- Der Titeleditor speichert Cue- und Metadatenänderungen ausdrücklich, bleibt danach
  aber für weitere Bearbeitung geöffnet. Lange Titel und Cue-Aktionen passen sich an
  die verfügbare Dialogbreite an.
- Die produktive Tempoanalyse verwendet den real abgenommenen Stand
  `ffmpeg-onset-acf-v0.5` mit `tempo-profile-v3`. Aggregatkonfidenz und
  Rhythmusstabilität werden getrennt bewertet; nur hinreichend sichere und stabile
  Ergebnisse werden automatisch für die Planung verwendet.
- Vollaufnahme, wirksamer Cue-Bereich und playlistbezogene Ausschnitte sind getrennte
  Analyse- und Planungskontexte. Ein unsicherer Cue-Wert verdrängt keinen verlässlichen
  Vollspurwert.
- Queue-, Playlist- und Katalogdarstellung unterscheiden konkrete Dateivarianten und
  machen die jeweils betroffene Fassung vor einer Aktion sichtbar.
- Hintergrundanalysen laufen in begrenzten Workern beziehungsweise einem isolierten
  Analyseprozess. Abbruch und Programmende schließen Worker und Audioressourcen
  kontrolliert.
- Das Datenbankschema wird beim ersten Start dieser Beta automatisch von Schema 34 auf
  Schema 41 erweitert.

### Fixed

- Responsive Layouts und Dialoge verhindern abgeschnittene Titel, Aktionsleisten und
  Cue-Schaltflächen in den abgenommenen Windows-Auflösungen und Skalierungen.
- Laufende oder noch wartende Analysen werden diagnostisch eindeutig dargestellt und
  beim Herunterfahren kontrolliert beendet beziehungsweise erhalten.
- Katalog-, Metadaten- und Persistenzabläufe verwerfen veraltete Hintergrundergebnisse
  und verändern weder Musikdateien noch deren Tags.
- Stabile elektronische Titel und Titel mit natürlichem Schlagzeug bleiben
  automatisch planbar, während Shuffle-/Half-Time-Grenzfälle und Titel mit echten
  Tempowechseln sicher blockiert werden. Die reale Abnahme umfasst 36 Voll-/Cue-Läufe
  über FLAC, VBR-MP3 und MP3 mit 320 kbit/s. Sechs Referenzen decken konstanten
  elektronischen Beat, echtes Schlagzeug, rhythmusarmes Intro, Break/Fade-out,
  Shuffle/Half-Time und echte Tempowechsel ab. Die weiterhin manuell zu bewertende
  automatische Shuffle-Freigabe ist als bekannte Produktgrenze dokumentiert. Für den
  Abschluss wurden weder Musikdateien noch Analysegrenzwerte verändert.
- Formatintegrationstests finden neben PATH auch die gebündelte FFmpeg-Toolchain und
  werden dadurch für FLAC, CBR-MP3 und VBR-MP3 nicht mehr übersprungen.

### Upgrade-Hinweise

- Vor dem ersten Start mit DeckRelay 2.0.0-beta.1 muss das vollständige Verzeichnis
  `data` der bisherigen Installation gesichert werden.
- Die vorhandene Datenbank wird beim Start automatisch von Schema 34 auf Schema 41
  migriert. Musikdateien und eingebettete Tags bleiben unverändert.
- Die reale Upgrade-Abnahme mit einer anonymisierten DeckRelay-1.0.0-Datenkopie
  bestätigte Datenerhalt, Wiederanlauf nach einem kontrollierten Fehler sowie Backup
  und Restore; der Hauptnachweis steht in
  `docs/development/release-2.0.0-beta.1-upgrade-acceptance.md`.
- DeckRelay 1.0.0 kann eine auf Schema 41 migrierte Datenbank nicht öffnen. Eine
  Rückkehr zu 1.0.0 ist nur mit der zuvor angelegten Schema-34-Sicherung möglich.

### Bekannte Grenzen

- Shuffle- und Half-Time-Material wird nicht immer automatisch freigegeben. Schwache
  oder widersprüchliche Ergebnisse bleiben zur manuellen Prüfung gesperrt.
- Titel mit echten Tempowechseln können bewusst ohne automatischen BPM-Planungswert
  bleiben.
- Die automatische Cue-Erkennung bewertet Signalpegel und nicht die musikalische oder
  dramaturgische Bedeutung eines Intros oder Outros. Cue In, Cue Out und Fade können
  im Titeleditor über den separaten Vorschauplayer vorgehört werden.
- Der Notfall- und Recovery-Unterbau ist implementiert. Reale Veranstaltungs-, Geräte-
  und Langzeitabnahmen stehen vor der stabilen 2.0-Freigabe noch aus; die globale
  Geräte-/Backend-Recovery bleibt als letzte Eskalationsstufe bewusst bedienergeführt.
- DeckRelay führt kein Beatmatching und kein tonhöhenerhaltendes Time-Stretching aus.

## [1.0.0] - 2026-08-16

### Added

- Automatische und manuelle Queue-Steuerung für die unabhängige Zwei-Deck-Wiedergabe
  einschließlich Cue-Punkten, Crossfades und vollständiger Playlist-Reihenfolgen.
- Jingles und Audio-Overlays mit Favoriten, Fades und optionalem Musik-Ducking.
- ReplayGain-, Lautheits- und Equalizerfunktionen mit Peak-Schutz und getrennten
  Einstellungen pro Titel, Queue und Deck.
- Diagnoseberichte, Performance-Messwerte und ein automatisiertes Windows-Quality-Gate.
- Portable Windows-Laufzeit mit vollständig gebündeltem Tcl/Tk sowie geprüfter
  Datei-, Produktversions- und Artefaktkonsistenz.

### Changed

- Nicht bestätigte Wiedergabestarts werden kontrolliert übersprungen; bereinigte
  Decks können anschließend für weitere Preloads wiederverwendet werden.
- Queue-Pause und Fortsetzung steuern die betroffenen Decks konsistent, ohne
  sichtbare Mixerwerte zu verändern.
- Große Queues verursachen durch gebündelte Datenbankabfragen, inkrementelle
  Statistikaktualisierung und begrenzte Hintergrundarbeit weniger GUI-Last.
- Die vollständige Sicherung wird verständlich als Übertragung einer kompletten
  Veranstaltung beschrieben und nach erfolgreicher Erstellung eindeutig bestätigt.
- Hinweise zu deaktivierten Laufzeitanalysen erklären Produktionsmodus, Neustart und
  beschreibbare portable Installationsordner ohne unnötige Administratorrechte.
- Der FFmpeg-Verweis führt zur von FFmpeg verlinkten Windows-Build-Seite von gyan.dev.
- Die Dokumentation erklärt den Einfluss des Equalizer-Sicherheits-Preamps auf die
  wahrgenommene Lautstärke und seine Trennung von Deck-, Master- und ReplayGain-Werten.

## [1.0.0-beta.3] - 2026-08-15

### Fixed

- Beim erstmaligen Öffnen der Jingle- und Effektverwaltung wird ein neues Overlay
  vollständig mit gültigen Standardwerten vorbelegt: 75 % Lautstärke, 300 ms
  Fade-in, 500 ms Fade-out, Cue-In `00:00`, kein Cue-Out, 0 dB Musikabsenkung,
  200 ms Attack und 500 ms Release.
- Leere oder ungültige Zahlenfelder melden jetzt das betroffene Feld mit einem
  verständlichen Eingabehinweis, statt einen internen Python-Fehler wie
  `invalid literal for int()` anzuzeigen.
- Die Vorschau vor dem Automatikstart prüft alle Queue-Kandidaten über eine
  gemeinsame SQLite-Verbindung. Dadurch entfallen Verbindungsaufbau und
  -abbau pro Titel, die den GUI-Thread bei größeren Queues mehrere Sekunden
  blockieren konnten.
- Die Beta-3-Windows-EXE enthält eine konsistente Datei- und Produktversion;
  das irrtümlich weitergeführte portable Beta-1-Paket wurde entfernt.
- Ein nicht bestätigter Wiedergabestart des eingehenden Decks wird nicht mehr
  fälschlich als dauerhafter Deck- oder Backendausfall behandelt. Der betroffene
  Titel wird übersprungen, das Deck vollständig bereinigt und anschließend wieder
  automatisch mit dem nächsten geeigneten Titel vorgeladen.
- Nach einem manuellen Deck-Ladevorgang lässt sich die zuvor unterbrochene
  Automatik wieder aktivieren. Veraltete Übergangs- und Preload-Zustände sowie
  Sperren des inaktiven Decks blockieren den Neustart nicht mehr.
- Neu angelegte Jingles verwenden standardmäßig 0 dB Musikabsenkung statt -8 dB.
- Ein erneuter Klick auf denselben Jingle-Favoriten startet ihn zuverlässig von
  vorn. Das bisherige implizite Ausblenden bei jedem zweiten Klick entfällt;
  dafür bleibt die separate Ausblenden-Schaltfläche verfügbar.
- Beim Deckwechsel gilt jetzt auch ein eindeutig fortschreitender VLC-Medienzeitgeber
  als bestätigte Wiedergabe. Dadurch bleibt ein bereits laufendes eingehendes Deck
  nicht mehrere Sekunden stumm, nur weil VLC seinen Wiedergabestatus verspätet meldet.
- Die Party-Queue zeigt ihre Herkunft aus den persistierten Queue-Einträgen an,
  etwa Verzeichnis, gespeicherte Playlist, Katalog oder gemischte Queue. Die zuletzt
  ausgewählte Playlist kann die Herkunftsanzeige nicht mehr verfälschen.
- Der kompakte Automatikstatus verwendet kurze Zustände und integriert nächsten
  Titel und verbleibende Titel, ohne vom Stoppknopf überlagert zu werden.
- Die automatische Kandidatensuche startet nur noch, wenn tatsächlich ein leeres,
  unzugeordnetes Deck einen Titel benötigt. Erfolglose Suchen verwenden einen
  exponentiellen Backoff bis 30 Sekunden.
- Queue-Statistiken werden nur noch neu aufgebaut, wenn sich laufzeitrelevante
  Queue-Daten ändern; reine Metadaten- und Anzeigeänderungen lösen keinen
  vollständigen Lauf über große Queues mehr aus.
- Das Queue-Aktionsmenü wiederholt die bereits sichtbare Mischfunktion nicht mehr.
  Bei Duplikaten und Cue-Restlaufzeit zeigt der Menütext unmittelbar an, ob die
  jeweilige Einstellung aktiv oder inaktiv ist.
- Eine ausdrücklich vom Benutzer ausgelöste Queue-Pause pausiert jetzt auch die
  laufende Audiowiedergabe. Beim Fortsetzen werden ausschließlich die Decks wieder
  gestartet, die durch diese Queue-Pause angehalten wurden.

### Changed

- Ein erfolgreich ausgeführter VLC-Startbefehl wird im Log als
  `Wiedergabestart angefordert` bezeichnet und nicht mehr vorzeitig als bestätigte
  Wiedergabe dargestellt.
- Abgelehnte Starts und Reaktivierungen der Automatik protokollieren jetzt den
  konkreten internen Fehlercode sowie Übergangs-, Preload-, Runner- und Pausezustand.
- Der Kopf der Party-Queue ist auf zwei dauerhaft sichtbare Zeilen verdichtet:
  Übersicht und tatsächliche Queue-Quelle oben, Wiedergaberegeln und Automatikstatus
  darunter. Seltene Befehle und Playlist-Werkzeuge liegen im Drei-Punkte-Menü.
- Das frühere Feld `Playlist` heißt in den optionalen Werkzeugen nun eindeutig
  `Titel hinzufügen aus Playlist`; `Queue-EQ` und `Playlist-Vorlage-EQ` benennen
  die zuvor missverständlich gleich bezeichneten, unterschiedlichen Einstellungen.

### Added

- Die portable Windows-Laufzeit bündelt die benötigten Tcl/Tk-Dateien und setzt
  deren Pfade beim Start über einen eigenen Runtime-Hook.
- Der Messpunkt `automatic_start.summary` erfasst die vollständige Dauer der
  Automatikvorschau und warnt ab 50 ms.
- Ein Windows-GitHub-Actions-Workflow führt Ruff, Black, MyPy und die vollständige
  Pytest-Suite mit festgelegten Werkzeugversionen aus.
- Recovery-Regressionstests decken beide Deckrichtungen, idempotente History,
  verspätete Preload-Ergebnisse und aufeinanderfolgende nicht startende Titel ab.
- Zusätzliche Regressionstests prüfen den erneuten Preload des bereinigten Decks
  in beiden Deckrichtungen sowie manuelles Laden und die anschließende erfolgreiche
  Reaktivierung der Automatik ohne veraltete Preload- oder Übergangssperren.
- Ein Mehrfachstart-Test prüft fünf unmittelbar aufeinanderfolgende Starts
  desselben Jingles einschließlich sauberer Generations- und Pending-Zustände.
- Die Audioaussetzer-Diagnose protokolliert VLC-Zustandswechsel, unerwartete
  Buffering-, Paused-, Stopped- und Error-Zustände, Positionssprünge sowie Beginn,
  Dauer und Erholung vermuteter Stalls. Die Meldungen enthalten zeitgleich GUI-Lag,
  aktive Worker, Statistik-/Preload-Status und den gecachten Quelldateistatus.

## [1.0.0-beta.2] - 2026-08-13

### Fixed

- Titel werden automatisch übersprungen, wenn VLC auf dem eingehenden Deck trotz
  Startbefehl keine tatsächliche Wiedergabe bestätigt.
- Nach einem nicht bestätigten Deckwechsel bleibt die Automatik aktiv und setzt die
  Wiedergabe sicher im Ein-Deck-Betrieb auf dem funktionsfähigen Deck fort.
- Das fehlerhafte Deck und seine Queue-Zuordnung werden freigegeben, damit der
  nächste spielbare Titel wieder vorbereitet werden kann.

### Changed

- Versionsnummer und Windows-Laufzeit wurden auf `1.0.0-beta.2` aktualisiert.

## [1.0.0-beta.1] - 2026-08-11

### Changed

- Der öffentliche Produktname wurde von PartyPlayer in DeckRelay geändert.

### Added

- Erste öffentliche Beta-Version von DeckRelay.
