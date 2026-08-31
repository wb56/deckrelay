# Produktive Metadatenanalyse (Paket 6B-2)

> **Aktueller Stand:** Die Tempoanalyse ist mit `ffmpeg-onset-acf-v0.5`,
> `tempo-profile-v3` und Datenbankschema 41 real abgenommen. Der vollständige
> technische Stand, die 36 realen Referenzläufe in sechs musikalischen Risikokategorien,
> geschlossene Findings und bekannte
> Grenzen stehen in
> [Tempo analysis, planning and diagnostics](tempo-analysis-diagnostics.md).
> Die nachfolgende Beschreibung von v0.1 bleibt als historischer Produktionsstart
> erhalten und ist keine aktuelle Algorithmusspezifikation.

Das produktive Backend `ffmpeg-onset-autocorrelation` verwendet die versionierte
Konfiguration `ffmpeg-onset-acf-v0.1`: externes FFmpeg/FFprobe, Mono mit 11.025 Hz und
drei getrennte, nicht überlappende Ausschnitte von insgesamt höchstens 90 Sekunden.
Eine Volltitelanalyse ist weder Standard noch automatischer Fallback.

Die zentralen Konfidenzgrenzen sind 0,80 für hohe Konfidenz und 0,55 als
Mindestgrenze für einen regulären Vorschlag. Mittlere Ergebnisse erfordern Prüfung;
unter 0,55 werden keine BPM-, Alternativ-BPM- oder Energievorschläge erzeugt. Ein
erkannter Tempowechsel senkt die Rhythmusstabilität und erzeugt eine Warnung. Stille
beendet den Run erfolgreich ohne BPM-Vorschlag. Kein Vorschlag wird automatisch als
wirksamer Katalogwert übernommen.

Schemaversion 39 ergänzt ausschließlich typisierte, begrenzte Tabellen für bekannte
numerische Run-Metriken und höchstens acht analysierte Zeitbereiche. Vorschläge werden
im Hauptprozess transaktional mit dem Runabschluss gespeichert. Identische offene
Vorschläge werden nicht dupliziert; abweichende ältere offene Audioanalysevorschläge
werden abgelöst. Angenommene und abgelehnte Historie, bestätigte Werte, bestätigte
Leerwerte und `metadata_revision` bleiben unverändert.

Die UI-unabhängige Serviceoberfläche unterstützt explizite Einzelaufträge,
ausgewählte Titel, Titel ohne offenen BPM-Vorschlag und erneute Analyse veralteter
Algorithmusversionen. Stapel bleiben seriell, pausier- und abbrechbar. PENDING-Runs
bleiben beim Shutdown erhalten und werden erst durch einen expliziten Aufruf wieder
eingereiht. Frühere RUNNING-Runs werden beim nächsten Start als unterbrochen markiert,
nicht automatisch gestartet.

Der Composition Root erzeugt den Service mit den validierten aktiven
RuntimeCapabilities und externen Programmpfaden. Ohne FFmpeg bleibt die Anwendung
startfähig; explizite Analyseoperationen enden mit `BACKEND_UNAVAILABLE` ohne
Prozessstart. Produktionsmodus, Audio-Recovery, Restore/Wartung und aktive Automatik
blockieren neue passende Aufträge. Der Service startet beim Programmstart keine
Analyse.

`multiprocessing.freeze_support()` wird vor der Composition-Root-Erzeugung aufgerufen.
Der importierbare Top-Level-Worker lädt kein Tkinter und öffnet keine Datenbank. Der
Supervisor registriert und entfernt den Prozess in der WorkerRegistry, beendet bei
Abbruch oder Timeout den Kindprozess begrenzt und schließt Pipe- und Prozesshandles.
Supportdiagnosen enthalten keine Dateipfade oder PID.

Die damals ausstehende reale BPM-Abnahme ist unter `ffmpeg-onset-acf-v0.5` und
`tempo-profile-v3` abgeschlossen. CBR-MP3, VBR-MP3 und FLAC sowie alle sechs
vorgesehenen Musikkategorien wurden geprüft; positive und negative Referenzen
bestanden. Es bestehen keine offenen produktionsrelevanten BPM-Findings. Der
maßgebliche Nachweis und die bekannte Grenze der automatischen Shuffle-Freigabe sind
in [Tempo analysis, planning and diagnostics](tempo-analysis-diagnostics.md)
dokumentiert. Automatische Planungswerte werden ausschließlich nach den dort
beschriebenen Sicherheitsregeln freigegeben. `energy_experimental` bleibt ein
technischer, versionierter Messindikator, keine Stimmung und keine objektive
musikalische Wahrheit.

## Technische Audiodaten im Titeleditor

Der schreibgeschützte Abschnitt **Technische Audiodaten** verwendet denselben
`FfmpegAudioAnalysisBackend` und denselben beim Start validierten FFprobe-Pfad wie die
vorhandenen Analysefunktionen. Codec, Container, durchschnittliche Bitrate,
Abtastrate, gespeicherte Bittiefe, Kanäle, Kanallayout, technische Dauer, Profil und
Encoder stammen aus dem Dateiinhalt. Dateiname und Endung werden nicht zur technischen
Klassifikation verwendet.

FFprobe liefert alle Audiostreams. Verwendet wird der als Standard gekennzeichnete
Audiostream, andernfalls der erste Audiostream; mehrere Streams und der ausgewählte
Streamindex werden angezeigt. Bei MP3 wird der Bitratenmodus anhand einer begrenzten
Folge tatsächlicher Paketgrößen und -dauern bestimmt. Nachweisbare Streuung wird als
VBR, hinreichend konstante Frames als CBR angezeigt. Bei zu wenigen verwertbaren
Paketen bleibt der Modus ausdrücklich „Nicht zuverlässig bestimmbar“. Eine
durchschnittliche Bitrate allein bestimmt den Modus nicht. FLAC wird nicht in die
MP3-Kategorien CBR/VBR eingeordnet; seine Bitrate wird als Durchschnittswert gezeigt.

Eine Bittiefe wird nur für verlustfreie beziehungsweise PCM-Codecs aus den gespeicherten
Streamangaben übernommen. Verlustbehaftete Formate wie MP3 zeigen „Nicht anwendbar“;
das FFmpeg-Decoderausgabeformat wird nicht als Dateibittiefe missverstanden.

Die Ermittlung läuft über den begrenzten Track-Editor-Hintergrundarbeiter und gelangt
über den vorhandenen GUI-Dispatcher zurück auf den Tk-Hauptthread. Ein prozessweiter,
auf kanonischen Pfad, Dateigröße und Änderungszeit gebundener LRU-Cache vermeidet
wiederholte Probes. Identische gleichzeitige Anfragen teilen einen Lauf. Ändert sich
der Snapshot, wird neu ermittelt; Änderungen während FFprobe verwerfen das Ergebnis.
Späte Ergebnisse werden nur auf einen noch geöffneten Editor mit derselben
Anfragegeneration angewendet.

Fehlendes oder nicht ausführbares FFprobe, Timeout, nicht erreichbare oder während der
Ermittlung geänderte Dateien, beschädigte Dateien und Dateien ohne Audiostream werden
als kontrollierter Status im Editor dargestellt. Nicht gelieferte Einzelwerte bleiben
„Nicht verfügbar“, „Nicht anwendbar“ oder „Nicht zuverlässig bestimmbar“. Der Cache ist
bewusst nur prozesslokal; es wurde keine Datenbank- oder Schemaänderung eingeführt.
