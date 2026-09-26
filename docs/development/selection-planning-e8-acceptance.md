# E8-Abnahme: Auswahl und Vorausplanung

Stand: 25. September 2026. Geprüfter Ausgangsstand ist der unveränderte
`main`-Merge `5d9a814718a2614d66a4e9372b9726d9e515f199` (PR #40). Das zugehörige
Main-Gate `36122564425` bestand alle fünf Jobs. E8 ergänzt keine Auswahlregel
und ändert weder Auswahlverhalten noch Datenformat.

## Nachweismatrix vor den ergänzenden E8-Prüfungen

| Bereich | Konkreter Nachweis | Geprüfter Stand | Ergebnis | Aussagegrenze |
| --- | --- | --- | --- | --- |
| Integration | `tests/test_automatic_selection.py`, `tests/test_selection_rule_settings.py`, `tests/test_automatic_selection_plan_e2e.py`, `tests/test_automatic_selection_plan_execution.py`, `tests/test_queue_and_session.py`, `tests/test_main_controller.py` | Main-Gate `36122564425`, Commit `5d9a814` | Bestanden | Automatisierte Service-/Repository-/Controller-Integration; kein akustischer Nachweis. |
| Integration/GUI | `tests/test_selection_rule_settings_dialog.py`, `tests/test_automatic_selection_preview_dialog.py`, `tests/test_automatic_selection_plan_ui.py` sowie manuelle E7b-Sichtprüfung | PR #40 / `5d9a814` | Bestanden | GUI-Zustände und Bedienfluss; keine reale Wiedergabe während Regeländerung. |
| Migration | `tests/test_database.py`, `tests/test_selection_rule_settings.py`; reale Upgrade-Abnahme in `release-2.0.0-beta.1-upgrade-acceptance.md` | Automatisiert bis Schema 45; reale Datenkopie nur Schema 34 → 41 auf `e20fa29` | Teilweise belegt | Reale Datenkopie belegt nicht die späteren Stufen 42–45; großer Bestand und heutiger Gesamtpfad sind noch offen. |
| Last/DB-Verzögerung | `docs/database_delay_test.md`, insbesondere fünf isolierte Läufe mit 1.000 ms Verzögerung | `b673ec8` | Bestanden: Übergangsabschluss Median 14,151 ms, Maximum 17,718 ms; 0 Heartbeat-Warnungen; eingehendes Deck und Automatik 212/212 Stichproben aktiv | Fake-Audio; technische Zustandskontinuität, keine Hörabnahme; nicht mit großem E7-Katalog/Plan kombiniert. |
| Last/Planung | `test_real_repository_plan_uses_four_reads_independent_of_depth`, Queue-Konkurrenz- und Buffer-Tests in `test_automatic_selection_plan_e2e.py` | `5d9a814` | Bestanden | Kleine Testkataloge; keine Messung mit großem Katalog und gefüllter Queue. |
| Recovery/Neustart | `test_restart_preserves_materialized_step_and_resume_keeps_identity`, Plan-Recovery-, Queue-/Session-Neustart- und Main-Controller-Recoverytests | `5d9a814` | Bestanden | Simulierte Fehler und Prozessneuerzeugung; kein absichtlich hart abgebrochener Prozess im Lastlauf. |
| Recovery/VLC/Gerät | `docs/development/automatic-playback-emergency-recovery.md`, reales Windows-Gerät und LibVLC 3.0.23 | `36913dc` | Bestanden, zwei dokumentierte Einschränkungen | Dateiverschiebung nach VLC-Pufferung und gleichzeitiger Geräte-/Doppeldeckausfall wurden nicht destruktiv real erzwungen. Auswahlplanung E6/E7 war nicht Gegenstand. |
| Reale Hörabnahme | Hörbarer unterbrechungsfreier Gegendeckbetrieb in der Emergency-Abnahme | `36913dc` | Für Emergency-Recovery bestanden | Keine vollständige reale Hörabnahme der heutigen Auswahl-, Plan-, Queue- und Regelkonfiguration auf `5d9a814`; für E8 offen. |

Die automatisierten Nachweise des Main-Gates werden ohne konkreten Anlass nicht
wiederholt. Insbesondere ersetzt die bestandene 1.000-ms-Prüfung keine reale
Hörprüfung, und die ältere reale VLC-Abnahme wird nur für ihre tatsächlich
ausgeführten Recoveryfälle angerechnet.

## Vorab definierte ergänzende Prüfungen

### E8-A: Migration und Wiederanlauf eines großen Bestands

**Ausgangszustand:** Isolierte SQLite-Datenbank auf Schema 41 mit 10.000
Katalogtiteln, einer aktiven Sitzung, 500 geordneten Queue-Einträgen und
bestehenden Einstellungen. Die Quelle bleibt eine synthetische Kopie; die reale
Schema-34-Datenkopie wird nicht erneut angefasst.

**Schritte:** Auf Schema 45 migrieren, Integrität und Fremdschlüssel prüfen,
Katalog-, Queue- und Einstellungsdaten mit der Baseline vergleichen, Migration
ein zweites Mal ausführen und den Zustand erneut vergleichen. Zusätzlich eine
Datenbank mit Schemaversion 46 öffnen.

**Erwartung und Kriterien:** Endschema 45; 10.000 Titel und 500 Queue-Einträge
unverändert und geordnet; vorhandene Einstellungen unverändert; alle 14
Regelregistereinträge und die Plantabellen vorhanden; `integrity_check = ok`;
null Fremdschlüsselfehler; zweiter Lauf ändert die kanonischen Nutzdaten nicht;
Schema 46 wird mit einer Meldung abgelehnt, die tatsächliche und unterstützte
Version nennt. Richtwert für jeden Migrationslauf: höchstens 5 s auf dem
Abnahmearbeitsplatz.

### E8-B: Große Planung und gefüllte Queue

**Ausgangszustand:** 1.000 eindeutige Kandidaten und 250 aktive
Queue-Einträge; Planungstiefe 10; deterministischer Seed. Die separat
protokollierte 1.000-ms-Prüfung bleibt der Nachweis für verzögerte
Datenbankpersistenz während laufender Wiedergabe.

**Schritte:** Einen vollständigen Entwurf mit der aus der gefüllten Queue
gebildeten Ausschlussmenge erzeugen, Folge und Persistenz prüfen und die
Ende-zu-Ende-Dauer messen.

**Erwartung und Kriterien:** Die Planung liefert zehn eindeutige Titel und
keinen der 250 aktiven Queue-Titel; genau ein Entwurf wird persistiert; vor der
ausdrücklichen Aktivierung entsteht keine zusätzliche Queue-Zeile;
Ende-zu-Ende-Dauer höchstens 10 s. Der Test belegt Planungskorrektheit und
Lastverhalten, nicht GUI-Reaktionsfähigkeit oder Audioqualität.

## Ergebnisse der ergänzenden E8-Prüfungen

Die neuen Prüfungen stehen in
`tests/test_e8_selection_planning_acceptance.py`. Ein direkt beendender Lauf
aller Assertions auf dem Abnahmearbeitsplatz ergab:

| Prüfung | Messwert | Ergebnis |
| --- | ---: | --- |
| Schema 41 → 45, 10.000 Titel, 500 Queue-Einträge, Idempotenz und Integrität | 0,557 s für den vollständigen Testablauf | Bestanden |
| Verständliche Ablehnung von Schema 46 durch Anwendung mit Schema 45 | 0,104 s | Bestanden |
| Planungstiefe 10, 1.000 Titel, 250 aktive Queue-Einträge | 0,775 s für Aufbau, Migration, Planung und Assertions | Bestanden |

Die Migration endete auf Schema 45, `integrity_check` war `ok`, der
Fremdschlüsselcheck leer, alle 10.000 Titel und 500 Queue-Einträge sowie die
vorhandene Einstellung blieben kanonisch unverändert. Der zweite
Migrationsaufruf änderte diese Nutzdaten nicht. Das Regelregister enthielt 14
Einträge und beide Plantabellen waren vorhanden. Die große Planung persistierte
genau einen Entwurf mit zehn eindeutigen, nicht in der aktiven Queue vorhandenen
Titeln; die 250 Queue-Zeilen blieben unverändert.

Der erste eingeschränkte Sandboxlauf konnte den lokalen Pytest-Cache im
separaten Worktree nicht schreiben. Mit freigegebenem Worktree-Schreibzugriff
endete derselbe Lauf regulär mit `3 passed in 2.98s`. Der anschließende gezielte
Regressionslauf aus Datenbank-, Regelkonfigurations-, Plan-E2E-, Plan-Recovery-
und E8-Tests endete mit `64 passed in 12.91s`. Zusätzlich bestanden Black und
Ruff für den neuen Test, MyPy für den Test und die berührten fachlichen Module
sowie `git diff --check`. Das bereits grüne Main-Gate bleibt der maßgebliche
vollständige Suite- und Realformatnachweis für `5d9a814`; eine Wiederholung der
gesamten unveränderten Suite war ohne Produktivcodeänderung nicht begründet.

## Noch offene reale E8-Hörprüfung

Status: **offen**, bis sie auf dem finalen Stand tatsächlich durchgeführt und
protokolliert ist. Simulationen und stummgeschaltete VLC-Läufe schließen diesen
Punkt nicht.

### Vorbereiteter Lauf vom 25. September 2026

Der final zu prüfende Produktionscode war unverändert Commit
`5d9a814718a2614d66a4e9372b9726d9e515f199`. Uncommittet vorhanden waren vor
Beginn nur dieses E8-Protokoll und die E8-Abnahmetests; es gab keine spätere
Produktivcodeänderung, die eine Wiederholung der technischen Nachweise verlangt.

Für den Hörlauf wurde eine isolierte Schema-45-Datenbank mit 30 eigens erzeugten
12-sekündigen MP3-Dateien, bestätigter Eignung und fünf Queue-Einträgen aus
manueller, Playlist- und Gastwunschquelle aufgebaut. `integrity_check` ergab
`ok`; Katalog, Eignung und Queue enthielten 30, 30 beziehungsweise 5 Einträge.
Die Anwendung startete erfolgreich mit LibVLC `3.0.23 Vetinari` und meldete
beide Deck-Backends im erwarteten leeren VLC-Zustand.

Als aktive Windows-Ausgabeendpunkte wurden erkannt:

- Audials Sound Capturing;
- Realtek Digital Output;
- Realtek Digital Output (Optical);
- TX-NR525;
- VE248.

Für die isolierte Probe war bewusst `WINDOWS_DEFAULT` konfiguriert. Welcher
physische Endpunkt tatsächlich Standard war, ließ sich über die verwendete
schreibgeschützte Endpoint-Erkennung nicht zuverlässig bestimmen.

Der eigentliche Hörlauf wurde **nicht durchgeführt**: Die verfügbare
Windows-Automationsschnittstelle stellte kein steuerbares Anwendungsfenster
bereit, und der ausführende Agent besitzt keine Möglichkeit, reale
Audioausgabe wahrzunehmen. Daher existieren keine beobachteten Hörwerte und
keine Aussage zu Knacken, Stille, Übergangsqualität oder physischem Gerät. Die
Anwendungsvorbereitung und ein erfolgreicher technischer Start sind ausdrücklich
kein Ersatz für die Hörabnahme.

1. Isolierte Kopie einer bestehenden Schema-41-oder-älteren Datenbank sichern,
   starten, Migration auf 45 bestätigen und `integrity_check` protokollieren.
2. Reales VLC und das vorgesehene Ausgabegerät wählen; zwei bekannte echte
   Audiodateien manuell gegenhören (Kanal, Lautstärke, Start/Stop).
3. Mindestens 30 unterschiedliche echte Titel mit gepflegten Genre-, BPM-,
   Energie-, Stimmungs- und Bewertungsdaten bereitstellen. Queue mit mindestens
   fünf manuellen/Playlist-/Wunscheinträgen füllen.
4. Automatikvorschau Tiefe 10 erzeugen, übernehmen und starten. Prüfen, dass
   fremde Queue-Quellen Vorrang behalten und anschließend exakt die sichtbare
   Planfolge nachgefüllt wird.
5. Während laufender Wiedergabe Regelwerte speichern, eine neue Vorschau öffnen
   und bestätigen, dass nur neue Entscheidungen die neuen Werte verwenden.
6. Einen noch nicht geladenen geplanten Titel kontrolliert unzugänglich machen;
   sichtbare Invalidierung/Pause prüfen, Datei wiederherstellen und den
   angebotenen Recoverypfad ausführen.
7. Anwendung während eines vorbereiteten, noch nicht gespielten Plans beenden,
   neu starten und explizit fortsetzen. Identität und Reihenfolge des bereits
   materialisierten Queue-Eintrags vergleichen.
8. Mindestens drei vollständige Übergänge mit beiden Decks hören; während eines
   freien-Deck-Fehlers muss das Gegendeck ohne hörbare Unterbrechung weiterlaufen.

Die isolierte Probe kann mit `scripts/e8_hearing_acceptance_setup.py` erneut
vorbereitet werden. Sie verweigert das Überschreiben einer vorhandenen
Zieldatenbank und erwartet genau 30 Dateien namens `e8-track-01.mp3` bis
`e8-track-30.mp3`. Danach wird DeckRelay mit dem erzeugten Verzeichnis als
Arbeitsverzeichnis gestartet. Vor dem ersten Play ist der tatsächlich hörbare
Ausgabeendpunkt in der GUI auszuwählen und im Protokoll zu notieren.

### Unmittelbar ausführbares Hörprotokoll

Vor Beginn eintragen: Datum/Uhrzeit `__________`, Commit `__________`, Windows
`__________`, VLC `__________`, gewählter physischer Ausgabeendpunkt
`__________`, Dateiformat `MP3/44,1 kHz/192 kbit/s`.

| Szenario | Ausgangszustand | Schritte | Erwartetes Verhalten / Akzeptanz | Beobachtet | Hörbare Auffälligkeiten | Ergebnis |
| --- | --- | --- | --- | --- | --- | --- |
| Grundwiedergabe und Quellenpriorität | Isolierte DB, 30 geeignete Titel, fünf vorbereitete Einträge aus MANUAL, PLAYLIST und GUEST_REQUEST, beide Decks leer | Gerät auswählen; Queue prüfen; Automatikvorschau Tiefe 10 erzeugen, übernehmen und starten | Manuell vor Gastwünschen vor Playlist; danach exakt sichtbare Planfolge; GUI bleibt bedienbar | `__________` | `__________` | `offen` |
| Regeländerung während Wiedergabe | Mindestens ein Titel spielt, alter Plan ist sichtbar | Gewicht oder Aktivstatus ändern und speichern; neue Vorschau öffnen | Laufende Wiedergabe/alte Entscheidung unverändert; neue Vorschau verwendet neue Konfiguration; kein Audioaussetzer beim Speichern | `__________` | `__________` | `offen` |
| Fehlende geplante Datei | Ein geplanter, noch nicht geladener Titel ist bekannt; Gegendeck spielt | Datei außerhalb der Anwendung vorübergehend umbenennen; Vorbereitung abwarten; Datei wiederherstellen; angebotenen Recoverypfad ausführen | Verständliche Meldung ohne vollständigen Pfad; Schritt invalidiert oder Plan pausiert; Gegendeck läuft hörbar weiter; kontrollierte Fortsetzung | `__________` | `__________` | `offen` |
| Neustart und Wiederaufnahme | Plan aktiv; mindestens ein Planschritt materialisiert, aber noch nicht gespielt | Plan-ID und Queue-ID notieren; Anwendung normal schließen und neu starten; Wiederaufnahme bestätigen | Keine automatische Wiedergabe beim Start; explizite Recovery; materialisierte Queue-ID und Reihenfolge bleiben erhalten | `__________` | `__________` | `offen` |
| Drei reale Übergänge | Beide Decks betriebsbereit; mindestens vier spielbare Titel verbleiben | Drei automatische Übergänge vollständig anhören; Start-/Endzeit notieren | Keine unerwartete Stille, kein Knacken, kein vorzeitiger Abbruch; Folge stimmt mit Plan/Queue überein | `__________` | `__________` | `offen` |

Jedes Ergebnis darf erst nach tatsächlichem Hören auf `bestanden` gesetzt werden.
Bei einem Fehler sind Zeitpunkt, Deck, Plan-ID, Queue-ID, sichtbare Meldung und
reproduzierbare Schritte festzuhalten; erst nach Korrektur und erneutem Hören
darf das betreffende Szenario geschlossen werden.

### Erneuter manueller Abnahmeversuch vom 26. September 2026

Der getrennte E8-Prüfstand wurde auf Branch
`feature/2.0-selection-e8-acceptance` und Commit
`2c20a0916df1f883ff889904b3ffe15fb5944136` verifiziert. Ergebnisse aus dem
PR-#42-Recovery-Prüfstand wurden nicht übernommen.

Die bereitgestellte Windows-Automationsschnittstelle meldete keine steuerbaren
Anwendungsfenster (`apps: []`), obwohl sich Anwendungen per Prozessstart auf dem
Windows-Rechner ausführen ließen. Zusätzlich stand der Prüfinstanz kein hörbarer
Audio-Stream des physischen Ausgangs zur Verfügung. Deshalb wurden die fünf
Hörszenarien nicht als durchgeführt protokolliert; insbesondere gibt es keine
belastbare Beobachtung zu Stille, Knacken, Aussetzern, Abbrüchen oder dem Verhalten
des Gegendecks. Alle fünf Tabellenzeilen bleiben unverändert **offen**.

Eine Wiederholung muss auf demselben oder einem neu exakt protokollierten Commit
mit sichtbarer GUI, realem VLC, echten Audiodateien und einem benannten, tatsächlich
gehörten Audiogerät erfolgen.

Akzeptiert ist der Lauf nur bei bedienbarer GUI ohne Aussetzer, unveränderter
Quellenpriorität, exakter Plan-/Queue-Identität nach Neustart, verständlichen
Fehlermeldungen ohne technische Pfade, erfolgreicher Recovery und ohne hörbares
Knacken, Stille oder vorzeitigen Abbruch in den drei Übergängen. Zu protokollieren
sind Datum, Commit, Betriebssystem, VLC-Version, Gerätebezeichnung, verwendete
Dateiformate, Plan-IDs, beobachtete Übergangszeiten und Ergebnis jedes Schritts.

## E8-Entscheidung

Integration, Migration, automatisierte Lastprüfung und die vorhandenen
Recoverynachweise sind für den geprüften Stand ohne neuen Produktivcodebefund
belegt. Es wurden ausschließlich Dokumentation und reproduzierbare
Abnahmetests ergänzt; Auswahlverhalten und Datenformat blieben unverändert.

E8 ist dennoch **teilweise offen**. Blockierend für die vollständige Abnahme ist
die noch nicht tatsächlich ausgeführte reale Hörprüfung der heutigen
Auswahl-/Planfolge auf `5d9a814`. Außerdem bleiben die bereits in der realen
Emergency-Abnahme ausdrücklich begrenzten destruktiven Fälle (gleichzeitiger
Geräte-/Doppeldeckausfall und eine erst nach VLC-Pufferung verschobene Datei)
Grenzen, keine voll real reproduzierten Nachweise. Das vorstehende Protokoll
definiert den noch auszuführenden Abschlusslauf und seine messbaren Kriterien.
