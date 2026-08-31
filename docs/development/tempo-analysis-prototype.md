# BPM- und Energieanalyse-Prototyp

> **Historisches Dokument:** Dieses Papier beschreibt die ursprüngliche
> v0.1-Prototypentscheidung und deren damalige offene Punkte. Die produktive reale
> Abnahme wurde mit `ffmpeg-onset-acf-v0.5` und `tempo-profile-v3` abgeschlossen.
> Maßgeblicher Nachweis ist
> [Tempo analysis, planning and diagnostics](tempo-analysis-diagnostics.md).

Stand: 2026-08-22. Dieser Prototyp ist nicht mit GUI, Datenbank, Playback oder dem
Composition Root verbunden. Er verändert weder Audiodateien noch Tags.

## Entscheidung

Bevorzugt wird für Paket 6B-2 zunächst **externes FFmpeg/FFprobe plus DeckRelay-eigene,
begrenzte Onset-Hüllkurven- und Autokorrelationsanalyse**. FFmpeg dekodiert mono mit
11.025 Hz direkt über eine Pipe. Es entstehen keine PCM-Temporärdateien und keine
PCM-Daten überschreiten die Prozessgrenze aus Paket 6A. Drei nicht überlappende,
verteilte Ausschnitte von zusammen höchstens 90 Sekunden werden getrennt geschätzt
und anschließend gewichtet über den Median zusammengeführt.

Diese Lösung fügt keine Laufzeitabhängigkeit und keine Binärdatei zum portablen Paket
hinzu. Das weiterhin externe FFmpeg bleibt von der bestehenden Buildprüfung
ausgeschlossen. Der neue Python-Quellcode erhöht das Paket nur um einige zehn
Kilobyte. Es gibt keine Modelle oder Modelllizenzen. Allgemeine DSP-Verfahren wie
Hüllkurve und Autokorrelation benötigen vor einer Veröffentlichung keine
Modellweitergabe; diese technische Bewertung ist keine Patent- oder Rechtsberatung.

Als einzige Rückfalllösung wird **librosa 0.11 mit FFmpeg-Dekodierung vor der
Prozessgrenze** vorgemerkt, jedoch nicht installiert oder ausgewählt. Librosa ist ISC
und unterstützt laut Release-Metadaten Python 3.8 oder neuer, bringt aber NumPy,
SciPy, Numba, scikit-learn, SoundFile, soxr und weitere Laufzeitabhängigkeiten mit.
Das bedeutet mehrere native Binärkomponenten, deutlich höheren PyInstaller-Aufwand,
umfangreiche zusätzliche Lizenzhinweise und voraussichtlich eine Paketvergrößerung
im hohen zweistelligen bis dreistelligen MiB-Bereich. Diese Größe wurde mangels
Installation bewusst nur geschätzt. Quellen: [librosa 0.11 PyPI-Metadaten](https://pypi.org/pypi/librosa/0.11.0/json),
[ISC-Lizenz](https://github.com/librosa/librosa/blob/main/LICENSE.md).

## Ausgeschiedene Kandidaten

| Kandidat | Lizenz und Abhängigkeiten | Entscheidung |
| --- | --- | --- |
| aubio 0.4.9 | GPL-3.0-or-later; Pythonbindung benötigt NumPy und eine native C-Erweiterung. Die letzte offizielle Ausgabe ist von 2019 und PyPI bietet nur ein Source-Archiv, keinen aktuellen CPython-3.11-Windows-Wheel. | Nicht übernehmen: explizite GPL-Freigabe nach Projektpolicy nötig, native Windows-/PyInstaller-Pflege und alter Releasezustand. [Offizielles Repository](https://github.com/aubio/aubio), [PyPI-Metadaten](https://pypi.org/pypi/aubio/0.4.9/json). |
| Essentia | AGPLv3 für die offene Bibliothek; Modelle CC BY-NC-ND oder proprietär; verschiedene Drittbibliotheken. | Lizenz- und Modellbedingungen für diesen Auftrag ungeeignet, daher nicht installiert oder getestet. [Offizielle Lizenzangaben](https://essentia.upf.edu/licensing_information.html). |
| madmom 0.16.1 | Quellcode überwiegend BSD, Modelle/Daten CC BY-NC-SA; NumPy, SciPy, Cython und mido. Letzte PyPI-Ausgabe 2018, 20-MB-Sourcepaket, alte Pythonklassifizierungen. | Modelle sind für eine allgemein weitergebbare portable Anwendung problematisch; Wartungs- und Windowsrisiko zu hoch. [Offizielles Repository](https://github.com/CPJKU/madmom), [PyPI-Metadaten](https://pypi.org/project/madmom/). |

Keine Kandidatenbibliothek wurde installiert oder in den Abhängigkeiten hinterlassen.

## Ergebnisvertrag und Rohmerkmale

Backend/Algorithmus im damaligen Messlauf, inzwischen produktiv benannt:
`ffmpeg-onset-autocorrelation`, Version
`ffmpeg-onset-acf-v0.1`.

Der Prozess liefert:

- primäre BPM zwischen 20 und 300;
- explizite Halb-/Doppeltempoalternative;
- Konfidenz und Rhythmusstabilität von 0 bis 1;
- analysierte Zeitbereiche;
- RMS-Mittelwert und RMS-Variabilität;
- Peak und Crest-Faktor;
- robuste Transientendichte mit Refraktärzeit;
- Warnungen sowie stabile Fehlercodes;
- einen experimentellen Energieindikator aus RMS, Dynamik und Transientendichte.

Der Energieindikator ist nur ein versionierter technischer Vorschlag. Er ist weder mit
Lautheit gleichgesetzt noch fachlich kalibriert und darf nicht automatisch zum
Katalogwert werden. Stimmung wird nicht analysiert.

## Messungen

Testsystem: Windows, Python 3.11.9, lokales externes FFmpeg/FFprobe 8.1.2. Die
Formatmedien wurden temporär aus synthetischen Klicksignalen erzeugt und nicht ins
Repository aufgenommen.

| Fall | Ergebnis | Konfidenz | Bemerkung |
| --- | ---: | ---: | --- |
| 60-BPM-VBR-MP3 | 60 / Alternative 120 | 0,87 | exakt |
| 120-BPM-CBR-MP3 | 120 / Alternative 240 | 0,90 | exakt |
| 120-BPM-FLAC | 120 / Alternative 240 | 0,89 | exakt |
| 120-BPM-VBR-MP3 | 120 / Alternative 240 | 0,90 | exakt |
| 200-BPM-VBR-MP3 | 200 / Alternative 100 | 0,85 | exakt; Alternative bleibt sichtbar |
| Stille | erfolgreicher Lauf ohne BPM | — | Hinweis `Kein belastbarer Rhythmus`, keine Vorschläge |
| ungültige MP3-Datei | kein Ergebnis | — | stabiler Analysefehler, Pfad im Fehlertext bereinigt |

Bei 180 Sekunden mit langem Intro, 30-Sekunden-Break beziehungsweise Fade-out wurden
jeweils 120/240 BPM erkannt. Bei einem synthetischen Wechsel 100→120→150 BPM liefert
die empfohlene verteilte Strategie 120/240 BPM, Konfidenz 0,70, Stabilität 0,50 und
eine Tempowechselwarnung. Eine globale Volltitelanalyse fiel hier auf 50/100 BPM
zurück; sie ist deshalb keine geeignete Standardstrategie.

Für einen konstanten 60-Sekunden-120-BPM-Titel lagen alle vier Strategien bei 120 BPM.
Gemessene Laufzeiten waren 1,21 bis 1,45 Sekunden. Für längere 180-Sekunden-Fälle
benötigte die verteilte 90-Sekunden-Auswertung etwa 1,6 bis 1,9 Sekunden gegenüber
etwa 2,4 bis 2,8 Sekunden für den vollständigen Titel.

Eine beobachtende Windows-Messung der verteilten Tempowechselanalyse ergab ungefähr
53,1 MiB kombinierten Spitzen-Working-Set und maximal vier sichtbare Prozesse
einschließlich Hauptinterpreter, Spawn-Helfer/Analyseworker und genau eines
FFmpeg-Kindprozesses. Die temporäre PCM-Datenmenge beträgt null; Ergebnis-JSON im
Messlauf hatte 2.255 Byte. Die Messung ist eine Arbeitsmessung, kein garantierter
Speicherhöchstwert.

## Bekannte Grenzen und nächster Schritt

Die nachstehenden Punkte dokumentieren den damaligen Prototypstand. Die offene reale
BPM-Korpusabnahme, die Formatintegration und die Sicherheitskalibrierung wurden im
v0.5-Arbeitsblock abgeschlossen; geschlossene Findings und weiterhin gültige Grenzen
sind im aktuellen Diagnosepapier eingeordnet.

- Es standen keine lizenzklaren realen Musiktitel mit verifiziertem Referenztempo im
  Repository zur Verfügung. Genauigkeit bei Liveaufnahmen, schwachem Beat, komplexer
  Synkopierung und echten Tempowechseln ist daher noch nicht hinreichend belegt.
- Ein realer NAS-Pfad wurde ohne ausdrücklich benannte Testdatei nicht geöffnet.
  Snapshotprüfung und FFmpeg-Prozessgrenze unterstützen UNC-Pfade technisch, aber
  Latenz, Abbruch während Netzstörung und Wiederanlauf müssen real abgenommen werden.
- Spektrale Verteilung ist nicht enthalten. `energy_experimental` bleibt bewusst ein
  begrenzter technischer Indikator aus RMS, Dynamik und Transientendichte; daraus
  werden keine Stimmungswerte abgeleitet.
- Vor Produktivanbindung wird ein kleiner, lizenzklarer Referenzsatz echter Musik mit
  manuell verifiziertem Tempo benötigt. Akzeptanzgrenzen für BPM-Abweichung,
  Halb-/Doppeltempo und Mindestkonfidenz müssen fachlich festgelegt werden.
- Ergebnis-Persistenzadapter, Composition-Root-Anbindung, `freeze_support()` und
  WorkerRegistry-Anbindung sind in Paket 6B-2 umgesetzt. Ein PyInstaller-Probelauf
  beziehungsweise eine Release-EXE war nicht Bestandteil dieses Auftrags.

Das Abnahmeskript `scripts/probe_metadata_analysis.py` nimmt Dateien entgegen, nutzt
den Spawn-Supervisor aus Paket 6A, unterstützt Timeout und `Ctrl+C` und gibt
strukturiertes JSON aus. Es öffnet keine Datenbank und schreibt weder Tags noch
Katalogwerte.
