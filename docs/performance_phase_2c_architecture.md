# DeckRelay – Performance-Stabilisierung Phase 2C

## Zweck und Geltungsbereich

Phase 2C dient ausschließlich der Identifikation und Reduzierung verbleibender
Blockaden des Tk-Hauptthreads. Playback-Zustandsmaschine, Crossfade-Kurve,
VLC-Deckstruktur, Queue-Auswahl und SQLite-Architektur bleiben unverändert.

Die Implementierung verfolgt zwei Ziele:

1. Lange GUI-Unterbrechungen erhalten einen eindeutigen Messpunkt oder einen
   Python-Thread-Dump.
2. Aufwendige Widgetarbeit wird in kurze, abbrechbare Pakete zerlegt.

## Threadgrenzen

### Tk-Hauptthread

Nur der Hauptthread darf CustomTkinter- und Tk-Objekte erzeugen oder konfigurieren.
Insbesondere verbleiben dort:

- `CTkImage` erzeugen,
- Cover-Widget konfigurieren,
- Katalog- und Queuezeilen binden,
- Tooltips und Kontextmenüs bedienen,
- Deck-, Mixer- und Statusanzeigen aktualisieren.

### Hintergrundworker

Der Coverworker erledigt vor der Rückgabe an Tk:

- Datei beziehungsweise eingebettete Coverdaten lesen,
- Bild dekodieren,
- nach RGB konvertieren,
- Seitenverhältnis berechnen,
- auf 190 × 160 Pixel skalieren,
- auf den Deckhintergrund setzen.

Das resultierende PIL-Bild wird erst im Hauptthread in ein `CTkImage` umgewandelt.
PIL-Verarbeitung darf deshalb nicht als Tk-Arbeit interpretiert werden.

## Kritischer GUI-Heartbeat und Thread-Dumps

Der Tk-Hauptthread aktualisiert bei jedem erfolgreichen Heartbeat einen monotonen
Zeitstempel in `GuiCallbackState`. Ein eigener Daemon-Thread prüft diesen Zustand alle
100 ms. Ab 750 ms ohne erfolgreichen Heartbeat speichert `ThreadDumpWriter` unter
`diagnostics/thread-dump-YYYYMMDD-HHMMSS.txt`:

- Zeit und gemessene Verzögerung,
- aktiven Testkontext,
- Deck- und Transitionzustand,
- Dispatcherzustand,
- aktiven, zuletzt gestarteten und zuletzt abgeschlossenen GUI-Callback,
- Startzeit des aktiven Callbacks,
- aktiven Katalog- und Queue-Renderauftrag,
- Stack des MainThreads,
- Stacks aller weiteren Python-Threads.

Der Dump ist auf einen Schreibvorgang pro 60 Sekunden begrenzt. Ein Fehler beim
Schreiben beendet weder Wiedergabe noch Anwendung.

Der Watchdog ruft keine Tkinter-Funktion auf. Weil er `sys._current_frames()` aus
seinem eigenen Thread aufruft, enthält der Dump den MainThread-Stack während der
tatsächlichen Blockade. Der bisherige nachträgliche Dump aus `_heartbeat_tick()` wurde
entfernt; `GuiHeartbeat` erfasst nur noch Verzögerungsstatistik und Erholung.

`GuiCallbackState` verwendet ein kleines Lock ausschließlich für Zuweisungen und
Snapshots. Weder Dateizugriff, Logging, Widgetarbeit noch fachliche Callbacks laufen
unter diesem Lock. Verschachtelte Messwrapper werden als Stack geführt, sodass nach
Abschluss eines inneren Rendercallbacks der umgebende Callback wieder sichtbar wird.

## Gemessene GUI-Callbacks

`measured_gui_callback()` ist der gemeinsame Wrapper für anwendungseigene
Tk-Callbacks. Die Warnschwelle beträgt 25 ms. Das Namensschema lautet:

```text
gui_callback.after.<name>
gui_callback.after_idle.<name>
gui_callback.command.<bereich>.<aktion>
gui_callback.binding.<name>
```

Der GUI-Event-Dispatcher misst seine Handler zusätzlich als
`gui_event.dispatch.<event_type>`. Diese Messung ersetzt nicht die fachlichen
Teilmessungen eines Preload- oder Covercallbacks.

## Preload-Abschluss

Die fachliche Titelwahl und Deckzustandsmaschine wurden nicht verändert. Der
GUI-Abschluss eines vorbereiteten Titels ist jedoch in folgende Messbereiche
aufgeteilt:

```text
gui_event.preload.apply_result
gui_event.preload.update_deck_view
gui_event.preload.apply_loudness
gui_event.preload.update_queue_view
gui_event.preload.update_catalog_view
gui_event.preload.schedule_cover
gui_event.preload.schedule_followup
gui_event.preload.total
```

Nach Übernahme des vorbereiteten Mediums werden Queue-/Deckdarstellung und weitere
Automatikarbeit als eigener Dispatcher-Callback ausgeführt. Dadurch ist der erste
Callback kürzer und jede verbleibende Verzögerung einem Teilschritt zuzuordnen.

## Covermessung

Der Tk-Anteil ist gegliedert in:

```text
gui.cover_apply.prepare_result
gui.cover_apply.create_tk_image
gui.cover_apply.configure_widget
gui.cover_apply.layout
gui.cover_apply.release_old_reference
gui.cover_apply.total
```

Der Covercache besitzt höchstens eine Referenz pro Deck. Beim Wechsel wird die alte
Referenz nach erfolgreicher Widgetkonfiguration freigegeben. Beim Leeren eines Decks
werden sowohl die CustomTkinter- als auch die native Tk-Bildreferenz entfernt.

## Chunked und Dirty-Row-Rendering

Katalog und Queue verwenden je einen `DirtyRowScheduler`. Anfangswerte:

```text
maximal 3 Zeilen pro Chunk
maximal 8 ms aktive Arbeit
mindestens 10 ms bis zum nächsten Chunk
```

`mark()` vereinigt weitere schmutzige Zeilen innerhalb desselben Auftrags.
`replace()` beginnt eine neue Generation, leert die alte Dirty-Menge und macht bereits
in Tk eingeplante alte Callbacks zu No-ops. Dadurch kann ein schneller Such- oder
Seitenwechsel keine veralteten Inhalte nachträglich anzeigen.

Gemessen werden:

```text
gui.catalog_render.schedule
gui.catalog_render.chunk
gui.catalog_render.wall_clock
gui.queue_render.schedule
gui.queue_render.chunk
gui.queue_render.wall_clock
```

`schedule` misst nur ViewModel-Aufbereitung und Einplanung. `chunk` misst aktive
Widgetarbeit. `wall_clock` beginnt mit dem ersten Chunk, endet mit dem letzten und
enthält bewusst die Pausen zwischen den Chunks. Zusätzlicher Kontext nennt Anzahl,
Maximum und Durchschnitt von Chunks, Zeilen und Chunkabständen.

## Diagnosebericht

Der Bericht trennt drei Wertarten:

- **Timings:** Anzahl, Durchschnitt, Maximum und Slow-Count einer Laufzeitmessung.
- **Counters:** monoton steigende Gesamtmengen, etwa erzeugte Widgets oder Chunks.
- **Gauges:** aktueller Zustand, etwa RowViews, Tooltips, Dirty Rows und Tk-Widgets.

Mengenwerte werden nicht mit der Einheit Millisekunden ausgegeben. Die rekursive
Tk-Widgetzählung erfolgt nur auf ausdrücklichen Diagnosebericht, niemals im normalen
Statuszyklus.

## Widget- und Tooltip-Lebenszyklus

Katalog- und Queuezeilen besitzen dauerhafte Widgetbäume. Rebinding verändert nur
abweichende Werte. Tooltips werden einmal pro dauerhaftem Button erzeugt. Beim
Rebinding wird ein sichtbares Tooltipfenster geschlossen und ein geplanter Callback
abgebrochen; die Tooltipinstanz selbst bleibt erhalten.

Seltene Aktionen liegen in kurzlebigen Kontextmenüs. Dadurch benötigt eine Zeile nur
vier dauerhafte Aktionsbuttons. Die Katalogseitengröße wurde auf 50 reduziert, um
Widget- und Tooltipbestand ohne Einschränkung von Suche und Pagination zu halbieren.

## Produktionsbetrieb

Performanceaggregation, Heartbeatdiagnose, Thread-Dumps und Workerhistorie sind an
die bestehende Performance-Diagnostikeinstellung gebunden. Im Produktionsmodus ist
die Diagnose nach einem Neustart deaktiviert. Playbackfunktionen bleiben davon
unabhängig.

## Noch erforderliche reale Abnahme

Automatisierte Tests prüfen Funktion, Rate Limit, Generationenabbruch, Zeit-/
Zeilenbudget, Messpunktstruktur und Wiederverwendung. Folgende Aussagen benötigen
reale Tk-/VLC-Messungen:

- Heartbeat im Normalbetrieb unter 250 ms,
- Cover-Tk-Anteil typisch unter 50 ms und maximal unter 100 ms,
- GUI-Handler üblicherweise unter 10 ms,
- `status_tick.render` durchschnittlich unter 6 ms und maximal unter 20 ms,
- stabiler Widget-, Tooltip-, RSS- und optionaler Heapbestand im Langzeittest.

Diese realen Abnahmen sind in der
[Feature-Liste](feature_list.md) unter **Implementiert, aber noch real abzunehmen**
geführt und dürfen nicht allein aus Unit-Tests als bestanden abgeleitet werden.
