# Persistente Auswahlregel-Konfiguration

Schema 45 erweitert die seit Schema 42 vorhandene Tabelle
`selection_rule_settings` zu einem vollständigen typisierten Regelregister. Der
Primärschlüssel ist die stabile Regel-ID; jede Zeile enthält Regelart,
Konfigurationsversion, Aktivstatus, Änderbarkeit, Gewicht, Standardwerte,
Geltungsbereich sowie Erstellungs- und Änderungszeitpunkt. Es wird weder
ausführbare Konfiguration noch eine Kandidatenentscheidung persistiert.
Unbekannte zukünftige Regel-IDs werden beim Lesen ignoriert und beim Schreiben
abgewiesen.

Unveränderlich aktive Hard Rules sind `core.track_exists`,
`core.required_metadata`, `selection.track_policy`, `selection.artist_policy`,
`selection.track_suitability`, `selection.repetition`,
`selection.short_track` und `selection.automatic_recent_track`. Die ersten
sieben gelten für alle Auswahlwege, die letzte nur für die automatische
Auswahl. Dateiverfügbarkeit bei Planausführung sowie Notfall- und Recoverypfade
bleiben technische Schutzbedingungen außerhalb konfigurierbarer Regeln.

Die Soft-Rule-Standards bilden das bisherige Verhalten exakt ab:

- `selection.play_count`: aktiv, Gewicht 10, gültig von 5 bis 100;
- `selection.rating`: aktiv, Gewicht 1, gültig von 0 bis 1;
- `selection.genre_diversity`, `selection.bpm_continuity`,
  `selection.energy_continuity` und `selection.mood_continuity`: deaktiviert,
  Gewicht 1, jeweils gültig von 0 bis 2.

Fehlende Zeilen werden durch sichere Standards ergänzt. Ist eine bekannte Zeile
beschädigt, nicht endlich, außerhalb der Grenzen oder in einer unbekannten
Version gespeichert, fällt der gesamte Snapshot auf die Standards zurück und
wird nicht teilweise angewendet. Die Servicegrenze schützt Hard Rules, speichert vollständige
Änderungen atomar, stellt Standards wieder her und übersetzt Datenbankfehler in
eine stabile anwendungsinterne Fehlermeldung.

Eine reale Automatikauswahl lädt die gesamte wirksame Soft-Konfiguration mit
genau einer Datenbankabfrage. Eine Mehrschritt-Vorschau und die E6-Planung
verwenden jeweils denselben unveränderlichen Snapshot für alle Schritte.
Queue-, Wunsch-, Playlist- und Notfallprioritäten, Hard-Rule-Reihenfolge und
Tie-Break-Verhalten bleiben unverändert.

Die Migration übernimmt vorhandene Soft-Rule-Werte unverändert, ergänzt
fehlende Standardzeilen und registriert die Schutzregeln in derselben
Transaktion. Ältere Backups bleiben migrationspflichtig; das Backupformat
selbst ändert sich nicht.

Die Einstellungsoberfläche lädt beim Öffnen genau einen vollständigen Snapshot
über den Service. Bearbeitbar sind Abspielhäufigkeit (5 bis 100), Bewertung (0
bis 1) sowie die bereits wirksamen Regeln für Genre, BPM, Energie und Stimmung
(jeweils 0 bis 2). Schutzregeln erscheinen nicht als Schalter; ein kompakter
Hinweis erklärt ihre unveränderliche Wirkung. Eingaben, Aktivierungen und das
Wiederherstellen der Standardwerte bleiben bis zum ausdrücklichen Speichern im
lokalen Dialogzustand. Abbrechen und Fensterschließen verwerfen sie. Speichern
übergibt alle sechs Werte gemeinsam an den Service und lädt anschließend den
bestätigten effektiven Snapshot. Neue Auswahl- und Planungsvorgänge verwenden
die gespeicherte Konfiguration; laufende Wiedergaben und bereits getroffene
Entscheidungen werden nicht rückwirkend verändert.
