# Titel-Editor – Phase A: automatisierter Abnahmestand

Stand: 30. Juli 2026

> **Historische Momentaufnahme:** Dieses Dokument hält den automatisierten Phase-A-Stand
> vom 30. Juli 2026 fest. Die damalige Aussage, Phase B sei noch gesperrt, ist kein
> aktueller Planungsstatus. Der erweiterte Titeleditor ist inzwischen implementiert;
> verbleibende reale Freigabeprüfungen sind in [der Feature-Liste](feature_list.md)
> eingeordnet. Der historische Abnahmetext bleibt unverändert.

## Ergebnis

Die automatisierbaren Anforderungen der Phase A sind umgesetzt. Der bisherige
Cue-Dialog heißt `Titel bearbeiten`, verwendet ein unveränderliches ViewModel
und Änderungsmodell und behält den vorhandenen Cue-, Analyse- und Previewpfad
bei. Phase B bleibt bis zur realen Tk-/VLC-Abnahme gesperrt.

## Architektur und Threadgrenzen

- `TrackEditorController` komponiert `CuePointController`,
  `LoudnessController` und die vorhandene Equalizerauflösung.
- Das initiale `TrackEditorViewModel` wird im begrenzten
  `track-editor-load`-Worker aufgebaut und erst danach über den
  `GuiEventDispatcher` an Tk übergeben.
- Cue-Persistenz läuft seriell im begrenzten `cue-persist`-Worker.
- Nur tatsächlich geänderte Cue-Spalten werden geschrieben. Das Verwerfen
  eines Analysevorschlags wird zusammen mit den manuellen Änderungen
  atomar persistiert.
- Preview und Cue-Analyse verwenden ihre bestehenden Worker- und
  Backendpfade. Queue, History, Automatik und On-Air-Decks werden durch die
  Vorschau nicht verändert.
- Späte Workerergebnisse prüfen den Dialog- beziehungsweise Tk-Lebenszyklus
  und greifen nach dem Schließen nicht mehr auf Widgets zu.

## Umgesetzte Oberfläche

- Dauerhafter Kopf mit Interpret, Titel, Album, Originaljahr und
  ressourcensicherem Dateipfad-Tooltip.
- Register `Cue`, `Lautheit`, `Equalizer`, `Jingles` und `Metadaten`.
  Nur `Cue` wird sofort aufgebaut; die übrigen schreibgeschützten
  Platzhalter entstehen lazy.
- Stabile globale Aktionen `Speichern` und `Abbrechen`.
- Vollständige Cue-Aktionen einschließlich aktueller Position, einzelner
  Rücksetzungen, Preview, Fade-Out-Test, Analyse sowie Vorschlag
  übernehmen/verwerfen.
- Sichtbare NULL-Semantik: leer bedeutet Dateianfang, Dateiende oder globale
  Überblenddauer. Ein expliziter Fade von `0` bleibt erhalten und deaktiviert
  automatisches Crossfading.
- Nach dem Speichern werden nur betroffene Katalog- und Queuezeilen dirty
  markiert.

## Diagnose

Vorhanden sind unter anderem:

- Timings: `track_editor.open`, `track_editor.load`,
  `track_editor.build_view_model`, `track_editor.save`,
  `track_editor.persist`, `track_editor.close`,
  `track_editor.cue_preview_start`, `track_editor.cue_preview_stop`,
  `track_editor.analysis_start`, `track_editor.analysis_complete` und
  `track_editor.equalizer_resolve`.
- Counters: Öffnen, Speichern, Abbrechen, Validierungsfehler,
  Persistenzfehler sowie Preview-Starts und -Stopps.
- Diagnosekontexte enthalten Titel-IDs, aber keine Dateipfade.

## Automatisierte Belege

- Vollständiger Pytest-Lauf: 630 bestanden, 3 übersprungen.
- Projektweites Ruff: bestanden.
- MyPy für alle 94 Quellmodule: bestanden.
- Bearbeitete Dateien sind mit Black formatiert.
- Projektweites `black --check src tests` meldet 23 bereits zuvor
  formatierungsbedürftige, nicht zum Track-Editor gehörende Dateien.
- Spezifische Tests belegen unter anderem NULL-Semantik, Grenzvalidierung,
  atomare und partielle Persistenz, Doppelklickschutz, Fehlererhalt,
  Spätcallbackschutz, gezielte Dirty Rows, Berichtsklassifizierung sowie
  wiederholte Previewzyklen ohne wachsende Workerzahl.

## Noch erforderliche reale Abnahme

1. Anwendung mit echtem Tk und VLC starten und einen Katalogtitel öffnen.
2. Cue In, Cue Out und Fade jeweils mit Komma, Punkt, leerem Wert und `0`
   prüfen; einmal speichern und einmal abbrechen.
3. Preview ab Cue In und Fade-Out-Test starten, stoppen und den Dialog während
   beziehungsweise nach einem Auftrag schließen.
4. Eine automatische Cue-Analyse durchführen, Vorschlag zunächst verwerfen
   und abbrechen, danach erneut verwerfen und speichern.
5. Parallel Deck A/B, Automatik, Queue und einen Crossfade weiterlaufen lassen.
6. Diagnosebericht speichern und auf neue kritische Heartbeats sowie die
   `track_editor.*`-Timings und `track_editor_*_total`-Counter prüfen.
7. Dialog mindestens zehnmal öffnen/schließen und bestätigen, dass Threads,
   Previewplayer, Widgets und Tooltips nach der Aufwärmphase nicht wachsen.

Erst wenn diese reale Prüfung ohne neue kritische Heartbeats, Audiostörung
oder Ressourcenwachstum abgeschlossen ist, darf Phase A als vollständig
abgenommen gelten und Phase B beginnen.
