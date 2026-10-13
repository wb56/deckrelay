# DeckRelay – Performance-Stabilisierung Phase 2F

## Ziel

Der Crossfade-Abschluss aktualisiert synchron nur Audio-, Deck- und den unmittelbar
benötigten In-Memory-Queuezustand. SQLite-Latenz darf den bereits laufenden Folgetitel
nicht verzögern. Fadeformel, Deckzahl, VLC-Volume-Worker, Preload, Cover,
Auswahlregeln und Transition-Zustandsautomat bleiben fachlich unverändert.

## Definierte Abschlussreihenfolge

1. Ein Guard lässt den Abschluss nur einmal zu.
2. Das ausgehende Deck wird ausgeworfen und seine Zuordnung im Speicher entfernt.
3. Der aktive History-Lebenszyklus wird ohne I/O in einen unveränderlichen Auftrag
   überführt.
4. Der betroffene Queueeintrag wird im Controllercache als `PLAYED` markiert.
5. Nur die betroffene sichtbare Queuezeile wird dirty gesetzt.
6. Autoload und Queue-Statistik werden als eigene GUI-Aufträge eingeplant.
7. History- und Queuepersistenz werden in dieser Reihenfolge an einen seriellen
   Executor übergeben.

Der eingehende Titel läuft während aller Schritte weiter. Ein Persistenzfehler ändert
weder Deck- noch Transitionzustand.

## History-Auftrag und Idempotenz

`HistoryPersistRequest` enthält Transition-ID, Track, Deck, Start-/Abschlusszeit,
gemessene Spieldauer, Abschlussgrund und optionale Queue-ID beziehungsweise
Fehlerangaben. `prepare_finish()` entfernt den aktiven Lebenszyklus atomar aus dem
Speicher, führt aber kein Repository-I/O aus.

`persist()` akzeptiert jede Transition-ID innerhalb des Prozesses nur einmal. Erst
nach einem erfolgreichen Commit wird sie als persistiert markiert; ein fehlgeschlagener
Versuch bleibt retryfähig. Der bestehende synchrone `finish()`-Pfad bleibt für
manuelle Aktionen kompatibel und verwendet intern dieselben beiden Schritte.

## Serieller Persistenzpfad

Ein `ThreadPoolExecutor(max_workers=1, thread_name_prefix="playback-persist-worker")`
erhält History- und Queueaufträge. Dadurch entstehen keine Crossfade-spezifischen
Dauerthreads und die fachliche Reihenfolge bleibt erhalten. Bei einer transienten
`sqlite3.OperationalError` erfolgen höchstens drei Versuche mit kurzer, ansteigender
Pause. Danach wird der Fehler durch die vorhandene Workerdiagnostik protokolliert;
Playback läuft weiter.

Beim geordneten Schließen werden noch aktive History-Lebenszyklen zuerst in Aufträge
überführt. Der eigene Persistenzexecutor wird anschließend geordnet geleert. Von außen
injizierte Executor-Instanzen bleiben in der Verantwortung ihres Besitzers.

## Inkrementelle Queue

Ein normaler Abschluss ändert keine Reihenfolge: Der Eintrag bleibt als `PLAYED`
sichtbar. Deshalb sind weder Entfernen noch Positionsnormalisierung erforderlich.
`MainWindow.show_queue_entry()` baut nur das ViewModel der betroffenen sichtbaren
Zeile und markiert genau deren Index im vorhandenen Dirty-Row-Scheduler. Vollständige
Neuberechnungen bleiben den strukturellen Operationen wie Reset, Neuordnung und
Recovery vorbehalten.

## Status-Tick

Ein natürlich beendetes Deck wird im Status-Tick erkannt und höchstens einmal über
`after(0)` eingeplant. History- und Queue-Repositoryzugriffe laufen weder im Status-Tick
noch im nachfolgenden GUI-Abschluss, sondern im seriellen Persistenzworker.

## Messpunkte

Der Abschluss unterscheidet Queue-In-Memory-Änderung, Dirty-Row-Veröffentlichung,
Statistik und Persistenz. History misst Einreihen und Repositorylaufzeit separat.
Zusätzlich werden folgende Crossfade-Zeiten ausgewiesen:

```text
crossfade.configured_duration_ms
crossfade.actual_ramp_duration_ms
crossfade.start_delay_ms
crossfade.completion_detection_delay_ms
crossfade.completion_processing_ms
crossfade.total_transition_ms
crossfade.duration_deviation_ms
```

Die bestehende `crossfade.timing`-Messung bleibt für den Vergleich älterer Berichte
erhalten.

## Verbleibende reale Abnahme

Automatisierte Tests belegen Nichtblockieren, Reihenfolge, Idempotenz, Fehlerisolation
und Guards. Die im damaligen Stand noch offene reale Messung mit verzögerter SQLite-
Persistenz wurde inzwischen bestanden; der aktuelle Nachweis steht in der
[Feature-Liste](feature_list.md) unter **Implementiert und abgeschlossen**. Das
Ergebnis gilt für diese Datenbankverzögerungsabnahme und ersetzt keine anderen realen
VLC-/CustomTkinter- oder Langzeitprüfungen.
