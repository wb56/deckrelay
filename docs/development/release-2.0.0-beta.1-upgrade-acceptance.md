# Reale Upgrade-Abnahme für DeckRelay 2.0.0-beta.1

## Zweck und Abgrenzung

Diese Abnahme prüft den produktiven Migrationspfad von DeckRelay 1.0.0 mit
Datenbankschema 34 auf DeckRelay 2.0.0-beta.1 mit Schema 41. Verwendet wurde eine
Kopie einer real genutzten DeckRelay-Datenbank. Alle Migrationen, Sicherungen,
Fehlerproben und Wiederherstellungen liefen ausschließlich auf weiteren temporären
Kopien. Musikdateien und Tags wurden weder geöffnet noch verändert.

Die Quelldatenbank war vor und nach der Prüfung bytegleich. Ihr anonymisierter
Unverändertheitsnachweis lautet:

- Größe: 610.304 Byte
- SHA-256: `8bcd8b4cda90a2174c4deadd77f931dd55cae4d52c12e5999c0488cabdc7353a`
- SQLite `integrity_check`: `ok`
- Fremdschlüsselfehler: 0
- Ausgangsschema: 34

## Anonymisierte Baseline

Die Baseline enthielt reale Daten und mehrere fachliche Bereiche:

| Bereich | Zeilen |
| --- | ---: |
| Titel | 179 |
| Party-Queue | 363 |
| gespeicherte Queues | 1 |
| gespeicherte Queue-Einträge | 275 |
| Wiedergabehistorie | 32 |
| Cue-Datensätze | 6 |
| Lautheitsdatensätze | 179 |
| Sitzungen | 3 |
| Audit-Ereignisse | 453 |
| Einstellungen | 10 |
| Equalizer-Presets / -Bänder | 6 / 20 |

Alle bestehenden Tabellen wurden vor der Migration anhand stabil sortierter,
eindeutig serialisierter Zeilen kanonisch gehasht. Lokale Medienpfade gingen nur in
diese Prüfsummen ein und wurden nicht protokolliert. Es bestanden keine mehrfachen
Titel-/Interpret-Gruppen nach normalisiertem Vergleich; dieser konkrete Datensatz
prüfte daher keine Varianten-Duplikate.

## Sicherung

Vor der Migration wurden drei getrennte Dateikopien angelegt: reguläre Upgradeprobe,
Fehlerprobe und unveränderte temporäre Sicherheitskopie. Zusätzlich erzeugte der
produktive `BackupService` aus der Schema-34-Kopie ein DeckRelay-Backuparchiv. Das
Archiv war lesbar, bestand die Archivvalidierung und wies Schema 34 aus. Alle Ziele
lagen im isolierten Prüfbereich; vorhandene Sicherungen wurden nicht überschrieben.

## Reguläre Migration 34 auf 41

Die reguläre Kopie wurde über denselben Einstieg wie beim Anwendungsstart migriert:
`migrate(Database(...))`. Die Instrumentierung protokollierte lediglich die
aufgerufenen Stufen und veränderte deren Verhalten nicht.

- tatsächlich durchlaufen: 35, 36, 37, 38, 39, 40 und 41
- Endschema: 41
- gemessene Dauer: rund 56 ms
- `integrity_check` danach: `ok`
- Fremdschlüsselfehler danach: 0

Die erwarteten Tabellen, Spalten und Indizes für typisierte Metadaten,
Stapeländerungen, Analyseläufe, Bereichsmetriken, kontextbezogene Tempoergebnisse und
strukturierte Diagnose wurden angelegt. Die 179 bestehenden Titel erhielten
durchgehend die vorgesehenen Ausgangswerte `recording_type = 'UNKNOWN'`,
`is_remastered = 0` und `metadata_revision = 0`; die neuen optionalen Analysefelder
blieben `NULL`. Die neuen Analyse-, Tempo- und Stapeltabellen waren leer. Die
Migration startete somit keine Audioanalyse.

## Datenerhalt

Nach Ausschluss der absichtlich geänderten Zeile in `schema_version` stimmten die
kanonischen Prüfsummen sämtlicher bisherigen Tabellen und Spalten vollständig mit der
Schema-34-Baseline überein. Zeilenzahlen, Primärschlüssel und Beziehungen blieben
erhalten. Insbesondere wurden 0 gespeicherte Medienpfade verändert. Es entstanden
keine unbeabsichtigten Duplikate und keine Verluste bei Titeln, Queue,
gespeicherten Queues, Historie, Cue-Punkten, Lautheit oder Einstellungen.

## Idempotenz, Neustart und Diagnose

Ein zweiter Aufruf des produktiven Migrationspfads beließ die Datenbank unverändert
auf Schema 41. Schema, Tabellen, Indizes, Spalten, Zeilenzahlen und kanonische
Prüfsummen blieben identisch. Anschließend ließen sich Datenbank, Track-Repository,
zentrales Repository und Systemdiagnose erneut initialisieren. Das Repository meldete
179 Titel; die Diagnose meldete tatsächliches und erwartetes Schema 41 mit Status
`available`. Für die Probe wurden keine Analyseworker gestartet, und es blieben keine
Worker zurück.

## Kontrollierte Fehlersimulation

Auf der zweiten Kopie wurde unmittelbar vor Stufe 38 kontrolliert eine
`RuntimeError` ausgelöst. Die Stufen 35, 36 und 37 waren zuvor aufgerufen worden. Nach
dem Fehler galt:

- konsistenter, lesbarer Zwischenstand mit ausgewiesener Schemaversion 36;
- `integrity_check = ok` und 0 Fremdschlüsselfehler;
- keine Änderung bisheriger fachlicher Tabellen oder Altfelder;
- durch `executescript` bereits angelegte, leere Schemaobjekte aus Stufe 37 waren
  vorhanden, während die Versionsmarke 37 zurückgerollt war.

Der Zustand war sicher fortsetzbar: Ein normaler erneuter Migrationsaufruf führte die
idempotente Stufe 37 erneut aus und migrierte danach erfolgreich bis Schema 41.
Integrität, Fremdschlüssel und sämtliche Baseline-Prüfsummen stimmten anschließend
mit der regulären Upgradeprobe überein. Die Fehlermeldung blieb als kontrollierte
`RuntimeError` am Migrationsaufrufer sichtbar. Es trat weder Datenverlust noch ein
nicht fortsetzbarer Zustand auf.

## Restore und erneute Migration

Das validierte Schema-34-Backuparchiv wurde über die produktive atomare
Restore-Pipeline ausschließlich gegen eine temporäre aktive Datenbank
wiederhergestellt. Die Pipeline materialisierte den Kandidaten, migrierte ihn auf
Schema 41, erzeugte ihr obligatorisches validiertes Sicherheitsbackup und tauschte
die temporäre aktive Datenbank atomar aus. Das Ergebnis verlangte erwartungsgemäß
einen Neustart.

Die wiederhergestellte Datenbank endete auf Schema 41 mit `integrity_check = ok` und
0 Fremdschlüsselfehlern. Alle Altfeld-Prüfsummen entsprachen sowohl der
Schema-34-Baseline als auch der regulären Upgradeprobe. Die ursprüngliche Quelle und
das bestehende Datenverzeichnis waren an keinem Restore beteiligt.

## Bekannte Grenzen

- Die Probe initialisierte den Anwendungskern und die Diagnose ohne vollständige GUI,
  Audioausgabe oder Wiedergabe, wie für diesen Auftrag vorgesehen.
- Der reale Quelldatensatz enthielt keine normalisierten Titel-/Interpret-Duplikate;
  Varianten-Duplikate werden durch andere Tests abgedeckt, aber nicht durch diese
  reale Datenkopie.
- Eine kontrollierte Ausnahme kann aufgrund der SQLite-`executescript`-Semantik leere
  Schemaobjekte einer Stufe dauerhaft anlegen, bevor deren Versionsmarke geschrieben
  wird. Der geprüfte Zwischenstand blieb konsistent und der normale Wiederanlauf war
  erfolgreich.

## Freigabeentscheidung

Die reale Upgradeprobe von Schema 34 auf Schema 41 ist bestanden. Originalquelle,
Nutzdaten und Medienpfade blieben unverändert; Migration, Idempotenz, Diagnose,
kontrollierter Fehlerwiederanlauf sowie Backup und Restore erfüllten die
Abnahmekriterien. Für DeckRelay 2.0.0-beta.1 besteht aus dieser Upgradeprobe kein
verbleibender Beta-Blocker.
