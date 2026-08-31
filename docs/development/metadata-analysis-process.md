# Metadaten-Analyseprozess (Paket 6A)

Paket 6A definiert ausschließlich die technische Prozessgrenze. Es berechnet noch
keine BPM-, Energie-, Tanzbarkeits- oder Stimmungswerte.

## Prozessgrenze

`MetadataAnalysisJob` und `MetadataAnalysisResult` sind unveränderliche, begrenzte
und mit dem Windows-`spawn`-Verfahren übertragbare Datenverträge. Der Kindprozess
erhält nur IDs, den Dateisnapshot, stabile Enums, primitive Optionen und
Analyseparameter. Er importiert weder Tkinter noch Datenbank-, Repository-, Player-,
Queue- oder Sessioncode. PCM-Daten überschreiten die Prozessgrenze nicht.

Der Supervisor startet pro Auftrag genau einen nicht-dämonischen Spawn-Prozess und
erlaubt höchstens einen aktiven Prozess. Ein Prozess pro Auftrag wurde gegenüber
einem dauerhaft wiederverwendeten Worker gewählt, weil Timeouts, Abbrüche,
FFmpeg-Kindprozesse und spätere Abstürze nativer Backends dadurch klar isoliert und
freigegeben werden können. Die öffentliche Schnittstelle legt diese Strategie nicht
fest. Ein Lebenszeichen ist Prozesszustand plus `STARTED`-Nachricht.

Unter Windows und in einer PyInstaller-EXE ist der Worker-Einstieg eine importierbare
Top-Level-Funktion. Der Supervisor verwendet ausdrücklich `spawn`; rekursive Starts
entstehen erst bei einer späteren Composition-Root-Anbindung, für die vor der
Anwendungserzeugung `multiprocessing.freeze_support()` ergänzt werden muss. Paket 6A
ändert den Startcode bewusst noch nicht.

## Koordinator und Persistenz

Der UI-unabhängige Koordinator serialisiert Jobs, prüft den Dateisnapshot vor und
nach der Analyse und übergibt nur gültige Erfolgs- oder geprüfte Teilergebnisse an
den Ergebnis-Persistenzport. Run-Erzeugung und alle Status-/Vorschlagsschreibvorgänge
bleiben im Hauptprozess. `metadata_analysis_runs` bildet seit Schemaversion 35 die
persistente Grundlage; eine zweite SQLite-Schreibspur wird nicht eingeführt.

Ports kapseln Run-Persistenz, Ergebnis-/Vorschlagspersistenz, Betriebszustand und
Fortschrittsereignisse. Die konkrete serielle Persistenzanbindung folgt zusammen mit
dem realen Backend. Ein Hauptprozessadapter legt bereits Runs über das vorhandene
`AnalysisRunRepository` an und führt deren Status; der Ergebnisport bleibt bis zum
realen Backend die absichtliche serielle Integrationsgrenze. Beim Start können zuvor laufende Runs über den Port kontrolliert
als unterbrochen behandelt werden; sie gelten niemals automatisch als erfolgreich.

## Priorität, Pause, Abbruch und Shutdown

Neue Jobs werden während Audio-Recovery, Datenbankwartung, Restore/Migration und im
Produktionsmodus blockiert. Stapeljobs pausieren standardmäßig während Automatik.
Pause lässt einen laufenden Job fertiglaufen und verhindert nur den nächsten Start.
Abbruch und Timeout terminieren nach kurzer begrenzter Wartezeit den gesamten
Analyseprozess; so können spätere FFmpeg-Kindprozesse nicht als bewusst verwaltete
DeckRelay-Worker zurückbleiben. `close()` ist idempotent und nimmt keine neuen Jobs
mehr an.

Die manuelle Windows-Abnahme der Pakete 4 und 5 bleibt vor Commit/PR ausdrücklich
offen. Reale Analysealgorithmen, Backendauswahl, GUI-Anbindung und das persistente
Einspielen musikalischer Vorschläge folgen in Paket 6B beziehungsweise 7.
