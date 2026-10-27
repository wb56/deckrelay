# Session-Historie und Absturzwiederaufnahme – erster Schritt

## Ausgangsbefund auf `main` (`2bce0cd`)

DeckRelay speichert Sessions (`party_sessions`), Queue-Einträge einschließlich Status,
Quelle, Reihenfolge, Sperren und Cue-Snapshots (`party_queue`), abgeschlossene und
abgebrochene Wiedergaben (`play_history`) sowie strukturierte Bedien- und
Recovery-Ereignisse (`session_audit_events`). Beim Start wird die jüngste nicht
beendete Session übernommen. Vorbereitete oder geladene Queue-Einträge werden wieder
wartend; Decks werden höchstens stumm vorgeladen. Wiedergabe und Automatik werden nicht
gestartet. Offene Einträge einer sauber beendeten Session werden in eine neue Session
kopiert.

Die vorhandenen Tests in `tests/test_queue_and_session.py`,
`tests/test_main_controller.py`, `tests/test_playback_history_service.py` und
`tests/test_automatic_selection_plan_e2e.py` belegen Persistenz, stabile Reihenfolge,
stummes Wiederherstellen, Historienabschlüsse und die separate Entscheidung über einen
offenen Automatikplan. `docs/queue_ordering.md` beschreibt die stabile Reihenfolge;
`docs/feature_list.md` nennt einen vollständigen Historien- und Recovery-Dialog weiter
als ausstehend.

Die konkrete Lücke dieses Schritts: Ein beim Absturz laufender manueller Titel wurde
zwar als `ABORTED` historisiert, anschließend aber wieder auf `waiting` gesetzt. Damit
konnte er ohne ausdrückliche Bedienentscheidung erneut vorbereitet werden. Außerdem
zeigte die Oberfläche nur den allgemeinen Sessionstatus „WIEDERHERGESTELLT“.

## Erwartetes Verhalten dieses Schritts

- Ein beim Neustart als `playing` gefundener Eintrag wird genau einmal als abgebrochen
  historisiert und als übersprungen mit dem stabilen Code
  `RECOVERY_INTERRUPTED_PLAYBACK` markiert. Er ist damit kein automatischer Kandidat;
  ein vorhandener manueller Wiederholen-/Zurücksetzen-Befehl bleibt möglich.
- `preparing` sowie automatisch vorgeladene `ready`-Einträge (`deck_origin = AUTO` oder
  `NULL`) werden weiterhin sicher nach `waiting` zurückgesetzt;
  Reihenfolge, Quelle, manuelle Sperren und Cue-Snapshots bleiben erhalten.
- Ein vom Benutzer manuell aus der Queue auf Deck A oder B geladener `ready`-Eintrag
  (`deck_origin = MANUAL`) bleibt `ready` und seinem Deck zugeordnet (Vormerkung). Nur
  Recovery-Zähler und Audit-Details weisen ihn als `preserved_deck_reservations` aus; die
  GUI meldet ihn als „manuell geladene Titel bleiben ihrem Deck vorgemerkt (nicht
  gestartet)“. Die Wiedergabe startet nicht; der Benutzer muss sie ausdrücklich starten.
- Die Vormerkung wird über Datenbankspalte `party_queue.deck_origin` (`MANUAL`/`AUTO`/
  `NULL`, Schemaversion 46) gespeichert. `MANUAL` setzt nur `load_queue_track` nach
  erfolgreichem Laden; automatischer Preload und `load_next_into_free_deck` setzen `AUTO`.
  Jeder Wechsel nach `waiting`, `removed`, `failed`, `skipped` oder `played` sowie das
  Freigeben der Deckzuordnung löscht die Herkunft. Beim Rückfall `playing` → `ready`
  bleibt die Herkunft erhalten.
- Beim Neustart lädt `restore_deck_assignments` den vorgemerkten Titel stumm in das Deck.
  Eine frühere VLC-Instanz wird nicht wiederhergestellt. Fehlt die Datei oder schlägt
  das Laden fehl, wird der Eintrag kontrolliert als `failed` markiert und die Herkunft
  gelöscht; es startet nichts.
- Die Recovery bleibt idempotent: Ein zweiter Lauf erzeugt weder Duplikate noch weitere
  Historieneinträge.
- Die GUI meldet Anzahl übernommener offener Einträge, zurückgesetzter Vorbereitungen
  und unterbrochener Titel. Bei unterbrochenen Titeln nennt sie die notwendige manuelle
  Entscheidung ausdrücklich.
- Start und Recovery starten weder Wiedergabe noch Automatik. Auswahlregeln und
  Automatikplan-Recovery bleiben unverändert.
- Bereits terminale Einträge (`played`, `skipped`, `failed`, `removed`) werden nicht
  wieder aktiviert. Wiederholte Recovery erzeugt keinen zweiten Abbruchdatensatz.
- Für diesen Schritt ist eine vorwärtskompatible Datenmigration nötig. Die Migration auf Schemaversion 46
  ergänzt nur die nullable Spalte `deck_origin`; bestehende Zeilen bleiben `NULL` und
  verhalten sich wie bisher (Rücksetzen nach `waiting`), da ihre Herkunft nicht
  rekonstruierbar ist.

## Grenzen und weitere Schritte

Direkt aus Katalog oder Datei geladene Titel (ohne Queue-Eintrag) werden nicht
vorgemerkt. Ein sauberes Beenden ohne `finish_session` hinterlässt `ready`-Einträge,
die sich in der Datenbank nicht von einem Absturz unterscheiden; manuelle Vormerkungen
bleiben daher auch dann erhalten.

Fehlende Mediendateien werden weiterhin unmittelbar vor einer Vorbereitung erneut
geprüft, als `FILE_MISSING` markiert und nicht geladen. Fehler beim Öffnen oder
Migrieren der Datenbank bleiben Startfehler; eine Wiederaufnahme gegen einen nicht
erreichbaren Speicher kann nicht sicher angeboten werden. Ein vollständiger
Historien-/Recovery-Dialog und CSV-Auswertung sind ausdrücklich nicht Teil dieses
Schritts.

Eine reale Prüfung von stummem Deck-Preload, VLC-Ausgabe und Audiogeräten bleibt bis
zur tatsächlichen Durchführung **offen**. Automatisierte Tests mit dem Fake-Backend
ersetzen diese Prüfung nicht.

## Manueller Abnahmeversuch vom 26. September 2026

Geprüfter Prüfstand war ausschließlich der Branch
`feature/2.0-session-crash-recovery` auf Commit
`9237ff5f7bbb08453c4d83351d05977eafcc3c34`. Der isolierte Recovery-Datensatz
wurde mit einem als `playing` markierten Eintrag auf Deck A und einem als `ready`
markierten Eintrag auf Deck B vorbereitet. DeckRelay wurde aus genau diesem
Worktree mit VLC `3.0.23 Vetinari` gestartet.

Die am Arbeitsplatz bereitgestellte Windows-Automationsschnittstelle lieferte
jedoch keine Anwendungsfenster (`apps: []`), obwohl der DeckRelay-Prozess lief.
Dadurch waren weder die GUI-Recovery-Meldung noch die sichtbaren Wiedergabe- und
Automatikzustände tatsächlich beobachtbar. Ein physischer Audio-Stream stand der
Prüfinstanz ebenfalls nicht zur Verfügung; es wurde daher kein Audiogerät als
gehörter Ausgang bestätigt. Backend-, Log- und Datenbankzustände werden
ausdrücklich nicht als Ersatz für diese Sicht- und Hörnachweise gewertet.

Damit bleiben die folgenden manuellen Nachweise **offen**:

- sichtbare Recovery-Meldung und Übereinstimmung mit dem wiederhergestellten Zustand;
- sichtbare Bestätigung, dass Wiedergabe und Automatik nicht selbsttätig starten;
- hörbare Stille am gewählten physischen Ausgang bis zu einer bewussten Wiedergabeaktion.

PR #42 bleibt bis zu einer tatsächlich beobachteten Wiederholung dieses Laufs Draft.
