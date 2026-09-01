# Lautheitsnormalisierung

DeckRelay berechnet die Wiedergabelautstärke zur Laufzeit. Musikdateien,
ReplayGain-Tags und andere Metadaten in den Quelldateien werden weder geändert noch
zurückgeschrieben. Eigene Analyse- und Korrekturwerte liegen ausschließlich in der
DeckRelay-Datenbank.

## Auswahl des Gain-Werts

Die Auflösung erfolgt pro Titel in dieser Reihenfolge:

1. Ist die Normalisierung deaktiviert, wird `0 dB` verwendet.
2. Eine manuelle DeckRelay-Korrektur hat immer Vorrang.
3. Im Modus `ALBUM` wird ein gültiger Album-Gain verwendet.
4. Fehlt im Album-Modus ein gültiger Album-Gain, folgt Track-Gain.
5. Im Modus `TRACK` wird ausschließlich Track-Gain berücksichtigt.
6. Fehlt ein gültiger Wert, bleibt der Titel bei `0 dB`.

Ungültige, nicht endliche Tagwerte werden ignoriert. Ein späterer Lesefehler
überschreibt bereits gespeicherte gültige Daten nicht.

## Sicherheitsbegrenzung

Der angeforderte Gain wird nacheinander durch folgende Grenzen eingeschränkt:

- konfigurierte maximale Absenkung;
- konfigurierte maximale positive Verstärkung;
- tatsächlich vom Audio-Backend unterstützter Verstärkungsbereich;
- Peak-Schutz.

Für einen gültigen ReplayGain-Peak berechnet DeckRelay die höchstens sichere
Verstärkung aus:

```text
sicherer Ziel-Peak = maximale Ausgangsspitze - Headroom
sicherer Gain      = sicherer Ziel-Peak - gemessener Peak in dBFS
```

`maximum_output_peak_db` beschreibt somit die technische Peak-Obergrenze.
`headroom_db` ist ein zusätzlicher Sicherheitsabstand und wird genau einmal
abgezogen.

Fehlt bei positiver Verstärkung ein gültiger Peak, greift der konservative
Fallback. Der effektive Gain wird dann höchstens auf
`fallback_positive_gain_db` angehoben. Negative Gains benötigen keinen
Peak-Fallback.

Ein Audio-Backend kann zusätzlich die optionale Schnittstelle
`RuntimeClipProtectionBackend` implementieren. Sie meldet ausdrücklich, ob ein
samplegenauer Laufzeit-Limiter verfügbar ist, und übernimmt Aktivzustand sowie
sicheren Ziel-Peak. Das derzeitige VLC-Backend meldet diese Fähigkeit als nicht
unterstützt: LibVLC stellt in der verwendeten portablen Ausgabekette keine
verlässliche True-Peak-Limiter-Steuerung bereit. Deshalb bleibt die oben
beschriebene, vorab berechnete Gain-Begrenzung der sichere VLC-Fallback.

## Anwendung bei der Wiedergabe

Vor dem ersten hörbaren Sample werden angeforderter und effektiver Gain, Quelle,
Peak-Begrenzung und linearer Faktor auf dem Zieldeck gespeichert. Die effektive
Ausgabe berechnet sich unabhängig für jedes Deck:

```text
Deckregler × Normalisierungsfaktor × Fade × Crossfader × Master
```

Der Equalizer ist eine zusätzliche, davon getrennte Verarbeitungsstufe. Bei
Presets mit angehobenen Frequenzbändern senkt sein Sicherheits-Preamp das Signal
ab, ohne Deckregler, Crossfader oder Master zu verändern. Deshalb kann ein
Equalizerwechsel trotz unveränderter Regler hörbar lauter oder leiser wirken.
Weitere Bedienhinweise stehen unter
[Equalizer und wahrgenommene Lautstärke](equalizer.md).

Änderungen an Gain oder Sicherheitsgrenzen während der Wiedergabe laufen über
eine zeitlich begrenzte Gain-Rampe. Diese Rampe verändert weder Deckregler,
Crossfaderposition noch Masterlautstärke. Trackwechsel, Eject, Stop und Fehler
brechen eine alte Rampe kontrolliert ab.

## Datenhaltung

DeckRelay speichert eigene Werte in SQLite, insbesondere:

- manuelle Gain-Korrektur;
- gelesene ReplayGain-Werte und Peaks;
- Analyse- und Metadatenstatus;
- Analyseversion und Zeitpunkte, soweit verfügbar.

Die Audioquelldatei wird nur gelesen. DeckRelay schreibt keine MP3-, FLAC-,
ReplayGain- oder sonstigen Dateimetadaten.

## Analyse im Titeleditor

Das Register **Lautheit** zeigt gespeicherte integrierte Lautheit, Lautheitsbereich,
True Peak, Analyseversion und Zeitpunkt. Eine neue Analyse kann dort ausdrücklich
gestartet werden, sofern FFmpeg und FFprobe für die Sitzung verfügbar sind. Der Auftrag
läuft im begrenzten Hintergrundworker; Status und Fehler werden im Dialog angezeigt.

Das Analyseergebnis ist zunächst ein eigener gespeicherter Messstand. Manuelle
Verstärkungswerte und die oben beschriebene sichere Auflösung bleiben davon getrennt.
Die Analyse verändert weder die Audiodatei noch deren Tags.
