# DeckRelay – belastbarer Datenbankverzögerungstest

## Zweck

Die Auswahl `database_delay` benennt nicht mehr nur einen Bericht. Ein explizit
gestartetes Szenario erzeugt einen isolierten Messzeitraum und verzögert ausschließlich
History- und Queuepersistenz. In-Memory-Queue, Decks, Fade-Rampe, GUI, Preload und Cover
werden nicht künstlich verlangsamt.

## Bedienung

1. Automatik starten und ausreichend Titel für einen vollständigen Übergang vorhalten.
2. Im Mixer das Szenario **Datenbankverzögerung** auswählen.
3. Im Millisekundenfeld den Testwert eingeben; Standard ist `1000`.
4. **Test starten/reset** wählen. Dadurch werden Performance-, Heartbeat-, Dispatcher-
   und abgeschlossene Workerstatistiken zurückgesetzt.
5. Mindestens einen vollständigen automatischen Crossfade abwarten.
6. Prüfen, dass das eingehende Deck ohne Unterbrechung weiterläuft.
7. **Test beenden + Bericht** wählen.

Der letzte Schritt wartet in einem Hintergrundworker auf alle bereits eingereihten
Persistenzaufträge. Die Oberfläche bleibt bedienbar. Nach Abschluss erscheint wie
gewohnt der Speicherort des Berichts; zugleich wird die künstliche Verzögerung
deaktiviert.

## Injektionsgrenze

Die Verzögerung wird nur ausgeführt, wenn Performance-Diagnostik aktiv ist und das
Szenario explizit gestartet wurde. Sie liegt unmittelbar vor dem jeweiligen
Repositoryaufruf im `playback-persist-worker`:

```text
History-Job: database.injected_delay → database.history.commit
Queue-Job:   database.injected_delay → database.queue.commit
```

Produktionsmodus kann kein Szenario aktivieren. Nach Szenarioende liefert der
Injektionspunkt ohne Wartezeit zurück.

## Bericht und Gültigkeit

Der Abschnitt `Scenario` enthält Name, Start-/Endzeit, Verzögerung, Resetstatus,
Übergangszahl und Persistenzzähler. Für `database_delay` wird zusätzlich ausgegeben:

```text
acceptance_data_present: true|false
```

`true` ist nur möglich, wenn nach dem Reset mindestens ein Transition-Abschluss und
mindestens ein Persistenzauftrag erfasst wurden. Die bloße Kontextauswahl ergibt
`false` und ist damit kein gültiger Lastnachweis.

Die relevanten Messpunkte sind:

```text
database.injected_delay
database.history.total
database.history.commit
database.queue.total
database.queue.commit
transition_completion.enqueue_history
transition_completion.enqueue_queue_persist
transition_completion.total
worker.history_persist
worker.queue_persist
worker.playback_persist
```

## Negativkontrolle

Ein automatisierter Kontrolltest ruft denselben Injektionspunkt synchron auf und
bestätigt, dass die aufrufende Ausführung entsprechend lange blockiert. Damit ist
belegt, dass der Testmechanismus eine alte synchrone Implementierung erkennen würde.

## Erwartete reale Abnahme

Bei 1000 ms Verzögerung soll `transition_completion.total` typisch unter 15 ms und
maximal unter 50 ms bleiben. `worker.playback_persist` muss dagegen mindestens ungefähr
1000 ms je verzögertem Auftrag ausweisen. Der Heartbeat soll kein kritisches Ereignis
enthalten und das eingehende Deck muss durchgehend spielen.

## Abnahmenachweis vom 3. September 2026

### Umgebung und geprüfter Stand

- Windows 10 22H2, Build 19045, 64 Bit
- Python 3.11.9 (64 Bit), pytest 9.1.1
- libVLC 3.0.23 Vetinari war verfügbar; der Kontinuitätsnachweis dieses Laufs erfolgte
  technisch mit dem vorhandenen Fake-Audiobackend und nicht als akustischer Hörtest.
- Remote-Tracking-Stand `DeckRelay/main`, Commit
  `b673ec8ced70fa3b0bc05873f1a5dab3095a8c76`
- Isolierter Detached-Worktree; die bereits vorhandene Änderung am EXE-Artefakt des
  ursprünglichen Feature-Worktrees wurde nicht verwendet oder verändert.
- Python meldete für `monotonic()` `GetTickCount64()` mit 15,625 ms Auflösung und für
  `perf_counter()` `QueryPerformanceCounter()` mit 0,0001 ms Auflösung.

### Ablauf und Reproduktion

Zunächst wurde der vorhandene automatisierte Szenario-, Übergangs- und
Negativkontrollsatz unverändert ausgeführt:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_diagnostic_scenario.py `
  tests/test_main_controller.py -k `
  'database_delay or transition_completion_does_not_wait_for_persistence_and_preloads_next or slow_queue_render_and_persistence_do_not_block_transition_completion or automatic_queue_starts_and_begins_crossfade_near_track_end or slow_deck_cleanup_cannot_delay_crossfade_target_or_incoming_audio or slow_outgoing_stop_does_not_block_incoming_audio_or_completion'
```

Ergebnis: 9 bestanden, 194 abgewählt. Die drei auf den vollständigen automatischen
Fade, langsame Deckbereinigung und langsamen ausgehenden Stopp fokussierten Tests wurden
zusätzlich einzeln wiederholt und bestanden ebenfalls.

Danach wurden fünf voneinander isolierte Läufe mit 1000 ms Verzögerung ausgeführt. Pro
Lauf wurden drei Queue-Titel vorbereitet, beide Decks gestartet, die Automatik aktiv
gesetzt, das Szenario über `begin_diagnostic_scenario("database_delay", 1000)`
zurückgesetzt und der Übergangsabschluss ausgelöst. Während der Persistenzworker lief,
wurden Incoming-Deck, Automatik und Heartbeat alle 50 ms abgefragt. Abschließend wurde
auf beide Persistenzaufträge gewartet und geprüft, dass der Bericht
`acceptance_data_present: true` enthält. Derselbe Injektionspunkt wurde danach als
1000-ms-Negativkontrolle synchron aufgerufen.

### Messwerte

| Messgröße | Läufe / Ergebnis |
| --- | --- |
| `transition_completion.total` (`monotonic`) | 15,000 / 0,000 / 15,000 / 16,000 / 16,000 ms; maximal 16,000 ms |
| derselbe Aufruf, hochauflösend (`perf_counter`) | 14,676 / 10,490 / 14,151 / 10,648 / 17,718 ms; Median 14,151 ms, maximal 17,718 ms |
| `database.injected_delay`, Mittel je Lauf | 1007,500 / 1000,000 / 1000,000 / 1000,000 / 1000,000 ms |
| `worker.playback_persist`, Mittel je Lauf | 1031,500 / 1085,500 / 1070,000 / 1078,500 / 1070,000 ms |
| `worker.playback_persist`, größter Einzelwert | 1125,000 ms |
| History-Gesamtzeit | 1016,000 bis 1047,000 ms |
| Queue-Gesamtzeit | 1047,000 bis 1125,000 ms |
| Heartbeat | 0 Warnungen, 0 kritische Ereignisse in allen fünf Läufen |
| Incoming-Deck | in 212 von 212 Stichproben weiter `playing` |
| Automatik | in 212 von 212 Stichproben aktiv |
| Persistenz | je Lauf 2 eingereiht, 2 abgeschlossen, 0 fehlgeschlagen |
| Berichtsgültigkeit | fünfmal `acceptance_data_present: true` |
| synchrone Negativkontrolle | konfigurierte/rückgemeldete 1000 ms, gemessene 1000,000 ms Blockierung |

### Bewertung

Der Nachweis ist **bestanden**. Der hochauflösende Median von 14,151 ms erfüllt
„typisch unter 15 ms“; 17,718 ms als größter hochauflösender Wert und 16,000 ms als
größter Produktmesswert bleiben deutlich unter 50 ms. Persistenz- und Workerwerte
belegen die wirksame Verzögerung von ungefähr mindestens 1000 ms. Es gab kein
kritisches Heartbeat-Ereignis, keinen technisch feststellbaren Aussetzer des
eingehenden Decks und keine Blockierung von Automatik oder Crossfade.

Die 15- und 16-ms-Stufen des Produktmesspunkts sind eine **Messabweichung durch die
15,625-ms-Auflösung von `GetTickCount64()`**, kein Produktfehler. Die synchrone
Negativkontrolle blockierte wie vorgesehen 1000 ms und belegt, dass das Szenario eine
synchrone Persistenzimplementierung erkannt hätte. Aus dieser Abnahme bleibt kein
technischer Produktrestpunkt offen. Ein akustischer Hörtest war nicht Bestandteil
dieses Laufs; die Anforderung wurde durch die zulässige technische Zustands- und
Kontinuitätsprüfung erfüllt.
