# PartyPlayer – Bestandsaufnahme Performance und Nebenläufigkeit

Stand: 18.07.2026, Version 1.0.0

> **Historische Momentaufnahme:** Dieses Audit beschreibt ausschließlich den Stand von
> DeckRelay 1.0.0 am 18. Juli 2026. Die unten genannten fehlenden Dispatcher, Worker,
> Messpunkte, Begrenzungen und Datenbankwarteschlangen sind keine aktuelle 2.0-Restliste.
> Der heutige Funktions- und Planungsstand steht in `docs/feature_list.md`. Insbesondere
> ist die asynchrone Übergangspersistenz inzwischen implementiert; die bestandene
> 1000-ms-Datenbankverzögerungsabnahme ist in `docs/database_delay_test.md` dokumentiert.
> Der historische Befundtext bleibt zur Nachvollziehbarkeit unverändert.

## Kurzfazit

Die Audio-Crossfade-Berechnung ist bereits von der sichtbaren GUI-Aktualisierung getrennt
und verwendet eine monotone Uhr. Der größte aktuelle Risikobereich ist nicht die reine
Thread-Anzahl, sondern der zentrale GUI-Statuscallback: Er läuft während der Wiedergabe
alle 200 ms und kombiniert VLC-Abfragen, SQLite-Zugriffe, Queue-Statistik, Autoload und
Rendering. Das kann bei langsamen VLC- oder NAS-Antworten die Oberfläche blockieren.

Vor einer Zwei-Prozess-Architektur sollten Messpunkte, ein zentraler GUI-Event-Dispatcher
und begrenzte Worker eingeführt werden. Die öffentliche Grenze zwischen GUI und Playback
sollte dabei bereits nachrichtenorientiert gestaltet werden.

## Thread-Inventar

### Dauerhaft

| Thread | Erzeuger | Anzahl | Lebensdauer | Aufgabe |
|---|---|---:|---|---|
| Tk-Hauptthread | CustomTkinter | 1 | gesamte Laufzeit | GUI und Controllercallbacks |
| `vlc-volume` | `VlcAudioBackend` | bis 2 | ab erster Lautstärkeänderung bis Backend-Ende | Lautstärkewerte je Deck an VLC übertragen |

Typischer stabiler Zustand nach der ersten Lautstärkeänderung: drei Threads einschließlich
GUI. Die beiden VLC-Worker sind benannt und besitzen eine Stop-Logik, sind aber Daemon-Threads.

### Vorübergehend und bei Wiederholung neu erzeugt

| Thread | Erzeuger | Begrenzung | Risiko |
|---|---|---|---|
| `crossfade-A-B` / `crossfade-B-A` | `TransitionController` | logisch höchstens einer | Daemon; VLC-Lautstärkezugriffe laufen parallel zum GUI-Statuscallback |
| `preload-deck-A/B` | `MainController` | durch `_preload_in_progress` auf einen begrenzt | pro Titel neu erzeugt; NAS/VLC-Prepare kann lange dauern |
| `cover-deck-A/B` | `MainController` | keine feste Obergrenze | pro Cover neu; alte Generationen werden verworfen, Arbeit läuft dennoch zu Ende |
| `cue-preview` | `CuePointController` | Generation vorhanden, alte Worker können kurz überlappen | pro Vorschau neu; Backend-Lebenszyklus und Tk-Rückmeldung kritisch |
| `directory-playlist-import` | `MainController` | keine globale Einmal-/Poolbegrenzung | pro Importaktion neu; große NAS-Verzeichnisse können lange laufen |

Es existiert derzeit kein `ThreadPoolExecutor`, kein Workerregister und kein zentraler,
kontrollierter Shutdown für alle vorübergehenden Threads.

## Endlosschleifen und Wartezustände

- `VlcAudioBackend._volume_worker`: dauerhafte Event-Warteschleife pro initialisiertem Deck.
- `TransitionController.audio_fade`: Schleife für die Dauer eines Crossfades, Takt 16 ms.
- `CuePointController`-Vorschau: maximal fünf Sekunden Seek-Bestätigung plus Vorschauzeit.
- `_drain_background_callbacks`: leert pro GUI-Zyklus die komplette unbeschränkte
  `SimpleQueue`; bei einem Rückstau gibt es kein Zeit- oder Mengenlimit.
- Verzeichnisimport und Dateisuche iterieren vollständig im jeweiligen Importworker.

Im GUI-Thread wurden keine Aufrufe von `join()`, `Future.result()`, `Event.wait()` oder
`sleep()` gefunden.

## Tkinter-Zugriffe aus Workern

Die normalen Preload-, Cover- und Importworker legen Callbacks in
`_background_callbacks`; Widgets werden anschließend im GUI-Thread verändert.

Ein konkreter Verstoß besteht bei der Cue-Vorschau: Der Worker ruft über
`CuePointController._post_status()` den übergebenen Scheduler auf. Dieser Scheduler ist
`MainWindow.schedule()` und damit unmittelbar `Tk.after()`. Schon das Einplanen eines
Tk-Callbacks aus einem Worker ist nicht zuverlässig thread-sicher. Diese Rückmeldung muss
über den zentralen GUI-Eventkanal erfolgen.

## `after()`-Inventar

### Dauerhaft wiederkehrend

- `MainController._status_tick`: genau eine permanente Anwendungsschleife, 200 ms bei
  Aktivität und 750 ms im Leerlauf.

### Vorübergehende Callback-Ketten

- Crossfade-Rendering: 33 ms während eines Übergangs.
- Warten auf tatsächliche Wiedergabe: 50 ms, maximal 60 Schritte.
- Manueller Deck-Fade: 50 ms für die Fade-Dauer.
- Glättung geänderter Normalisierung: 20 ms für 0,5 Sekunden.
- Tooltips: ein verzögerter Callback pro aktuell überfahrenem Widget.
- Einmaliger Start der Controllerinitialisierung: 150 ms nach GUI-Aufbau.

Damit existiert nur ein permanenter Haupttimer, aber während eines Übergangs können mehrere
temporäre Ketten gleichzeitig aktiv sein. Crossfade-Timing selbst hängt nicht von ihnen ab.

## Wahrscheinliche GUI-Operationen über 50 ms

Ohne Laufzeitmessung kann die Dauer noch nicht bewiesen werden. Folgende Pfade sind
statisch besonders verdächtig:

1. `_status_tick()` alle 200 ms:
   - zwei VLC-Positionsabfragen und Zustandsabfragen,
   - Crossfader-/Lautstärkeanwendung,
   - Queue- und History-Schreibzugriffe bei Zustandswechsel,
   - Queue-Statistik mit Datenbankzugriffen,
   - Autoload-Kandidatensuche mit mehreren SQLite-Abfragen.
2. Queue-Rendering: Die sichtbare Seite wird vollständig zerstört und mit bis zu 50 Zeilen
   und mehreren hundert Widgets/Tooltips neu aufgebaut.
3. Direkte Titel-/Deck-Ladevorgänge: `Path.is_file()`, VLC-Medienanalyse und bei NAS-Pfaden
   Netzwerkzugriffe können aus einem GUI-Callback erfolgen.
4. Katalogimport einzelner Dateien: Dateiprüfung, TinyTag-Metadaten und ReplayGain-Tags
   werden synchron gelesen.
5. Queue-, Cue-, Einstellungs- und Saved-Queue-Aktionen führen synchrone SQLite-Reads und
   -Writes im jeweiligen GUI-Callback aus.
6. `refresh_replaygain()` kann beim Deckladen Tags synchron vom NAS lesen.
7. Synchroner `FileHandler`: Logausgaben aller Threads schreiben unmittelbar in die Datei.
8. Übergangsabschluss: Deck-Stopp/Eject, Queuepersistenz und nächstes Laden werden über
   einen Tk-Callback ausgeführt und können VLC oder SQLite abwarten.

## Datenbankzustand

Positiv:

- pro Operation wird eine eigene SQLite-Verbindung verwendet;
- `foreign_keys=ON`, WAL und `synchronous=NORMAL` sind aktiv;
- Transaktionen sind auf Repositorymethoden begrenzt;
- wichtige Queue-Indizes existieren.

Offen:

- `busy_timeout` und Connection-Timeout betragen fünf Sekunden; ein GUI-Callback könnte
  entsprechend lange warten;
- Schreibzugriffe sind nicht zentral serialisiert;
- der Statuszyklus löst mehrere getrennte Verbindungen und Abfragen aus;
- es gibt noch keine Zeitmessung für Reads/Writes und keine Schreibwarteschlange.

## GUI-Rendering

- Katalog und Queue verwenden Seitengrößen, wodurch die Datenmenge begrenzt ist.
- Signaturen verhindern ein Neuzeichnen, wenn sich die Daten nicht geändert haben.
- Bei jeder tatsächlichen Queueänderung wird dennoch die komplette sichtbare Seite mit
  allen Buttons und Tooltips neu erzeugt.
- Cover werden im Hintergrund gelesen, aber für jeden Ladevorgang wird ein eigener Thread
  erzeugt; ein fester Cache und ein begrenzter Executor fehlen.

## Logging

Es wird ein synchroner `logging.FileHandler` verwendet. Crossfade-Schritte selbst werden
nicht einzeln protokolliert, was positiv ist. Eine `QueueHandler`/`QueueListener`-Struktur,
begrenzte Logwarteschlange und Rate-Limits für wiederholte Warnungen fehlen.

## Empfohlene Reihenfolge

1. GUI-Heartbeat und allgemeine Slow-Operation-Messung einführen (50-ms-GUI-Schwelle).
2. Tk-Aufruf aus dem Cue-Preview-Worker entfernen und einen zentralen, begrenzten
   `GuiEventDispatcher` einführen.
3. `_status_tick()` instrumentieren und teure SQLite-/VLC-Teile einzeln messen.
4. `_background_callbacks` begrenzen, pro Zyklus nur ein definiertes Budget abarbeiten und
   veraltete/coalescierbare Ereignisse ersetzen.
5. Cover, Preload, Import und Vorschau auf wenige langlebige, benannte und begrenzte Worker
   beziehungsweise passende Executor-Klassen umstellen.
6. Queue-Rendering inkrementell oder virtualisiert ausführen.
7. VLC-Befehle je Deck serialisieren und einen PlaybackCoordinator mit Prioritätsqueue
   definieren.
8. Datenbankschreibzugriffe serialisieren; Reads bündeln und Laufzeiten protokollieren.
9. Logging auf QueueHandler/QueueListener umstellen.
10. NAS-Cache und danach CPU-Analyse als eigener Prozess/FFmpeg-Manager ergänzen.
11. Mehrstündigen Lasttest durchführen und erst anhand der Messwerte über den separaten
    Playback-Prozess entscheiden.

## Bewertung der Zwei-Prozess-Idee

Die Trennung in GUI-Prozess und Playback-Engine ist für PartyPlayer langfristig sinnvoll.
Sie isoliert die Audiowiedergabe gegen teures Widget-Rendering und vereinfacht VLC-Besitz,
Notfallbetrieb und Engine-Neustart. Sie sollte jedoch nach einer klaren Command/Event-Grenze
erfolgen. Als Vorbereitung sollten GUI-Aktionen schon jetzt nur deklarative, serialisierbare
Playback-Kommandos erzeugen und immutable Zustandsereignisse empfangen. Dann kann dieselbe
Schnittstelle zunächst innerhalb eines Prozesses und später über Named Pipes, lokale Sockets
oder `multiprocessing.Queue` betrieben werden.

## Noch nicht gemessen

- tatsächliche Anzahl gleichzeitig lebender Threads während Import, Vorschau und Crossfade;
- maximale und durchschnittliche Dauer des GUI-Statuscallbacks;
- einzelne VLC-, SQLite-, NAS- und Renderzeiten;
- Wachstum der Callback-/Eventwarteschlange;
- Verhalten unter langsamem NAS und SQLite-Lock;
- mehrstündiger Dauerbetrieb und sauberer Worker-Shutdown.

Diese Werte benötigen gezielte Instrumentierung; aus statischer Codeanalyse allein wären
konkrete Millisekundenangaben nicht belastbar.
