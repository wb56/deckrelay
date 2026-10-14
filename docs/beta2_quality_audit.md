# Qualitätsaudit DeckRelay 1.0.0-beta.2

> **Historischer Stand:** Dieses Dokument bewertet die Stabilisierung der
> Version-1-Beta-2. Danach wurden reale FFmpeg-Formattests in das Windows-Gate
> aufgenommen. Die titelbezogene Recovery ist implementiert und automatisiert
> getestet; die durchgeführten realen LibVLC-/Geräteprüfungen sowie ihre Grenzen
> sind in der [Recovery-Abnahme](development/automatic-playback-emergency-recovery.md)
> dokumentiert. Das ist keine Aussage, dass globale Geräte-Recovery oder die
> übrigen Veranstaltungs- und Langzeitabnahmen bestanden sind. Der aktuelle Stand
> steht in der [Feature-Liste](feature_list.md).

Stand: 14.08.2026, unterstützte Zielplattform: Windows, Python 3.11.

## Abgrenzung und Vergleichsbasis

Verglichen wurden der Tag `v1.0.0-beta.1` (`9223ef0`) und der zunächst geprüfte
Beta-2-Stand (`656724b`). Lokale, nach Beta 2 entstandene Änderungen wurden separat
behandelt. Es wurden keine Linux-Kompatibilitätsänderungen vorgenommen.

## Ruff und Black

Mit der für die Qualitätssicherung festgelegten Ruff-Version `0.15.22` liefern sowohl
Beta 1 als auch der aktuelle Stand für `python -m ruff check src tests` null Befunde.
Die gemeldeten 311 beziehungsweise 312 Ruff-Befunde sind mit der eingecheckten
Konfiguration und dem verbindlichen Befehl nicht reproduzierbar. Ein Ruff-Altbestand
liegt unter dieser Prüfkonfiguration daher nicht vor. Die frühere Angabe stammt
offenbar aus einer anderen Werkzeug- oder Regelauswahl; Regeln wurden nicht
deaktiviert und es wurden keine pauschalen Ausnahmen ergänzt.

Beta 1 bestand `black --check`. Im Beta-2-Delta waren
`src/party_player/controllers/main_controller.py` und
`tests/test_transition_controller.py` neu nicht Black-kompatibel. Die spätere lokale
Optimierung der Automatikvorschau machte zusätzlich `src/party_player/queue_service.py`
formatierungsbedürftig. Ausschließlich diese konkret betroffenen Dateien wurden
formatiert; keine repositoryweite automatische Korrektur wurde ausgeführt.

## Recovery-Analyse

Die Behandlung von `INCOMING_PLAYBACK_NOT_CONFIRMED` überspringt den nicht gestarteten
Queueeintrag, beendet dessen aktive History mit `FAILED` und `PLAYBACK_ERROR`, gibt
Deck und Queuezuordnung frei, sperrt das fehlerhafte Deck im Ein-Deck-Betrieb und
lässt die Automatik auf dem hörbaren Deck aktiv. Ein erneuter Fehlercallback ist
idempotent, weil nach der ersten Behandlung keine Deckzuordnung und keine aktive
History mehr vorhanden sind.

Die vorhandene Logik zum Anwenden eines Hintergrund-Preloads prüft Generation,
Queuezustand, Deckbelegung und Wiedergabestatus. Ein nach dem Skip eintreffendes
Ergebnis wird verworfen und darf den Eintrag nicht erneut laden. Die Analyse ergab
keinen nachgewiesenen fachlichen Fehler; geändert wurden deshalb nur Tests.

Ergänzte Testfälle:

- Fehler B bei weiterlaufendem A und spiegelbildlich Fehler A bei weiterlaufendem B,
- Queue-Skipcode, vollständige Deckfreigabe und Sperre des Fehlerdecks,
- exakt ein Historyeintrag mit `FAILED`, Fehlertext und `PLAYBACK_ERROR`,
- wiederholter Fehlercallback ohne doppelten Historyabschluss,
- verspätetes Preload-Ergebnis nach `INCOMING_PLAYBACK_NOT_CONFIRMED`,
- zwei unmittelbar aufeinanderfolgende nicht bestätigte Ein-Deck-Starts ohne Schleife,
- aktive Automatik und weiterhin gesperrtes ausgefallenes Deck.

## Plattform- und Testumfang

DeckRelay ist ausschließlich für Windows vorgesehen. Die vollständige Suite und alle
verbindlichen Gates wurden unter Windows 10 ausgeführt. Die drei Skips betreffen die
parametrisierten realen FFmpeg-Formattests (MP3, FLAC und VBR-MP3), weil FFmpeg und
FFprobe in der Testumgebung nicht über `PATH` verfügbar waren. Tests wurden weder
entfernt noch abgeschwächt.

## Automatische Qualitätssicherung

`.github/workflows/quality.yml` verwendet `windows-latest` und Python 3.11. Der
Workflow installiert das Projekt mit den festgelegten Entwicklungswerkzeugen und
führt Ruff, Black, MyPy und Pytest ohne `continue-on-error` aus. Die Versionen sind in
`pyproject.toml` festgelegt: Black 25.1.0, MyPy 2.3.0, Pytest 9.1.1 und Ruff 0.15.22.

## Releasepaket und Versionen

Die fälschlich im Repository verbliebene Datei
`DeckRelay-portable-1.0.0-beta.1.zip` wurde entfernt. Neu erzeugt wurde lokal
`dist/DeckRelay-portable-1.0.0-beta.2.zip` mit dem vollständigen Ordner `DeckRelay`.
Die enthaltene EXE ist bytegleich mit `dist/DeckRelay/DeckRelay.exe`.

Folgende Werte stimmen überein:

- `pyproject.toml`: `1.0.0-beta.2`,
- `party_player.__version__`: `1.0.0-beta.2`,
- Diagnosebericht/Application-Code: verwendet `party_player.__version__`,
- Windows `FileVersion` und `ProductVersion`: `1.0.0-beta.2`,
- ZIP-Dateiname: `DeckRelay-portable-1.0.0-beta.2.zip`.

Die Releaseprüfung fand keine eingebetteten VLC-/FFmpeg-Laufzeitdateien. Buildordner
und aus ZIP entpackter Ordner bestanden jeweils den begrenzten Starttest. Für künftige
Versionen sollten ZIP und EXE bevorzugt als GitHub-Release-Assets veröffentlicht und
nicht dauerhaft im Git-Repository versioniert werden. Es wurde nichts veröffentlicht,
kein Tag erzeugt und kein vorhandener Tag verändert.

## Ergebnisse der Windows-Quality-Gates

- Ruff: bestanden, 0 Befunde.
- Black: bestanden, 259 Dateien unverändert erwartet.
- MyPy: bestanden, 139 Quelldateien ohne Befund.
- Pytest: 1100 bestanden, 3 übersprungen, Laufzeit 183,60 Sekunden.
- Gezielte Recovery-Auswahl: 7 bestanden.
- Release-Artefaktprüfung: bestanden.
- Starttest des Buildordners: bestanden.
- Starttest des entpackten ZIP-Inhalts: bestanden.

## Risiko und Freigabebewertung

Die fachliche Audio-, Crossfade-, Queue- und Recoverylogik wurde im Rahmen dieser
Bereinigung nicht geändert. Das verbleibende Risiko liegt in den nicht ausgeführten
realen FFmpeg-Formattests und in der weiterhin erforderlichen realen VLC-/Hörabnahme.

Bewertung: **freigabefähig mit dokumentierten Einschränkungen**. Diese Bewertung gilt
für die automatisierten Windows-Gates und das geprüfte Paket; reale Audio- und
Geräteabnahmen bleiben vor einer breiten produktiven Freigabe erforderlich.
