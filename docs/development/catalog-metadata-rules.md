# Katalogmetadaten: Fachbegriffe und Regeln

Dieses Grundlagenpaket definiert ausschließlich reine Fachmodelle. Es ändert weder das
Datenbankschema noch Import, GUI, Analyseausführung oder Wiedergabe.

## Feldbedeutungen

`year` bezeichnet das Jahr der vorhandenen Ausgabe, Compilation oder Edition.
`original_release_year` bezeichnet das ursprüngliche Veröffentlichungsjahr der konkreten
Aufnahme. Das Erscheinungsjahrzehnt wird daraus berechnet und nicht redaktionell bearbeitet.
Musikalische Dekaden sind dagegen eine unabhängige, mehrwertige redaktionelle Zuordnung.

Plausible Jahreswerte liegen in dieser ersten Ausbaustufe zwischen 1877 und 2100.
Bewertungen sind ganze Zahlen von 1 bis 5; `None` bedeutet „nicht bewertet“. BPM liegt im
dokumentierten plausiblen Bereich von 20 bis 300. BPM-Konfidenzen verwenden einheitlich den
Bereich 0 bis 1. Energie und Tanzbarkeit sind ganze Zahlen von 0 bis 100. Kommentare sind
reine Katalogtexte mit höchstens 2.000 Zeichen und verändern keine Dateitags.

Eine Aufnahme besitzt genau eine primäre Art: Original, Neuaufnahme, Liveaufnahme, Remix,
Radio Edit oder unbekannt. „Remastert“ ist ein unabhängiges Merkmal, weil beispielsweise
auch eine Liveaufnahme oder ein Remix remastert sein kann.

## Quellen und Prüfstatus

Stabile Quellen sind Dateitag, Audioanalyse, externe Musikdatenbank,
Datei-/Ordnerableitung, manuelle Eingabe und manuelle Bestätigung. Die internen Enumwerte
sind sprachneutral stabil und können später von der Oberfläche auf deutsche Texte
abgebildet werden.

Die Prüfstatus unterscheiden fehlend, importiert, analysiert, vorgeschlagen,
prüfungspflichtig, bestätigt mit Wert, bestätigt ohne Wert, widersprüchlich,
fehlgeschlagen und veraltet. „Bestätigt ohne Wert“ ist eine bewusste fachliche Aussage und
wird ebenso geschützt wie ein bestätigter Wert.

## Vorschlags- und Schutzregeln

Manuell bestätigte Werte und bestätigte Leerwerte werden nie automatisch überschrieben.
Abweichende spätere Vorschläge dürfen erhalten bleiben, werden aber nicht wirksam.
Konflikte erfordern immer eine Prüfung. Automatische Übernahme setzt voraus, dass kein
wirksamer oder geschützter Wert existiert, das Feld die Übernahme erlaubt und seine
Mindestkonfidenz erreicht ist. Bewertung, Kommentar und freie Tags sind redaktionell und
erlauben in der ersten Ausbaustufe keine automatischen Vorschläge. Subjektive Klassifikationen
wie Genre, Stimmung und Aufnahmeart können vorgeschlagen, aber nicht automatisch übernommen
werden.

## Kontextgrenzen

- **Katalogwerte** beschreiben die konkrete Aufnahme.
- **Playlistwerte** beschreiben nur ihre Verwendung in einer vorbereiteten Zusammenstellung,
  etwa Pflicht-/Reservestatus, Gewichtung, Abschnitt, Cue-/Fade-Snapshot, EQ-Zuweisung,
  Kommentar oder eine bewusste Regelabweichung. Sie ändern niemals Jahr, BPM, Genre,
  Sprache, Energie oder Bewertung des Katalogtitels.
- **Veranstaltungswerte** beschreiben Abschnitte, Auswahlziele und gewünschte Verläufe.
- **Queuewerte** beschreiben den tatsächlichen Ablauf und sollen später als unveränderlicher
  Entscheidungssnapshot gespeichert werden.

## Persistente Grundlage

Schema 36 speichert häufig benötigte effektive Einzelwerte weiterhin typisiert in `tracks`.
Das bestehende `genre` bleibt das Hauptgenre. Normalisierte Mehrfachwerte, feldbezogene
Herkunft und Prüfstatus sowie validierte Analyseläufe und Vorschläge besitzen getrennte
Tabellen. Effektive Änderungen erhöhen monoton `metadata_revision`; Vorschlagsannahmen
erfolgen transaktional und respektieren den manuellen Schutz.

## Sicherer Dateitagimport

Beim Erstimport werden gültige Dateitagwerte normalisiert als effektive Ausgangswerte mit
Quelle `FILE_TAG` und Status `IMPORTED` gespeichert. Fehlt der Titel, darf allein für den
technisch erforderlichen Katalogtitel der Dateiname als Quelle `FILE_OR_FOLDER_DERIVATION`
dienen. Fehlende Tags werden nicht als bestätigt fehlend markiert. Dateipfad und Dauer sind
technische Dateidaten und keine redaktionellen Feldwerte.

Bei Wiederholungsimporten bleiben identische normalisierte Werte unverändert und erhöhen
die Metadatenrevision nicht. Nur ein bereits als ungeschützter Dateitagimport ausgewiesener
Wert darf durch einen geänderten Dateitag aktualisiert werden. Altwerte ohne Feldstatus,
manuell bestätigte Werte und bestätigt fehlende Werte werden erhalten; abweichende Tags
werden als offene, prüfbare Vorschläge gespeichert. Mehrere effektive Änderungen eines
Dateiimports erhöhen `metadata_revision` gemeinsam genau einmal.

Ein Importvorschlag verweist auf einen unveränderlichen Snapshot aus normalisiertem Pfad,
Dateigröße und Änderungszeitpunkt. Identische offene Vorschläge werden nicht dupliziert;
ein unverändert abgelehnter Vorschlag erscheint bei gleichem Snapshot nicht erneut. Ein
neuerer abweichender Vorschlag löst den alten offenen Vorschlag kontrolliert ab. Ändert sich
die Datei zwischen Taglesen und Persistenzprüfung, werden keine Teiländerungen gespeichert.

Der Pfad ist derzeit der einzige belastbare Katalogidentifikator. Ein unbekannter neuer Pfad
wird deshalb auch bei gleichen Titel-, Interpret- oder Albumwerten als neue Datei behandelt.
Bekannte Basisverzeichnisänderungen bleiben Aufgabe des vorhandenen Pfad-Remappings. Eine
automatische Zusammenführung verschobener Dateien ohne Fingerprint ist bewusst nicht
implementiert.

Der Import liest Musikdateien und eingebettete Tags ausschließlich. Er schreibt, verschiebt
oder verändert weder MP3-/FLAC-Dateien noch deren Tags.

## Manuelle Pflege im Titeleditor

Der Reiter **Metadaten** wird beim ersten Öffnen aufgebaut und lädt wirksame Werte,
Feldzustände und offene Vorschläge über den begrenzten Track-Editor-Worker. Bis zum
Speichern bleiben Eingaben und Vorschlagsentscheidungen lokal im Dialog. Abbrechen und
das Schließen über das Fenstersymbol persistieren nichts.

- Eine manuelle Änderung erhält die Quelle `MANUAL_INPUT`, wird als bestätigt mit Wert
  gespeichert und ist damit vor automatischem Überschreiben geschützt.
- **Bestätigen** schützt einen bereits wirksamen Wert, ohne andere Felder zu ändern.
- **Ohne Wert** speichert keinen Ersatzwert, sondern den geschützten Zustand
  `CONFIRMED_WITHOUT_VALUE`.
- Beim Leeren eines vorhandenen Werts muss zwischen „fehlend/ungeprüft“, „bewusst ohne
  Wert“ und Abbrechen gewählt werden.
- Mehrfachwerte werden gemeinsam mit Einzelwerten normalisiert, dedupliziert und in
  einer Transaktion ersetzt.
- Vorschläge werden erst mit **Speichern** angenommen, angenommen und bestätigt oder
  abgelehnt. Geschützte Werte erfordern vor einer Übernahme eine ausdrückliche
  Bestätigung; konkurrierende offene Vorschläge werden kontrolliert abgelöst.

Alle Metadatenänderungen eines Speichervorgangs werden atomar geschrieben und erhöhen
`metadata_revision` höchstens einmal. Bei einem Revisionskonflikt wird nichts
überschrieben. Der Dialog zeigt die betroffenen geöffneten und aktuellen Werte und bietet
an, den aktuellen Stand zu laden, die lokalen Eingaben erneut zu prüfen oder abzubrechen.
Die Musikdatei, ihre MP3-/FLAC-Tags und Cover bleiben unverändert.

## Katalogpflege und Sammelaktionen

Die Katalogpflege verwendet serverseitig gezählte Arbeitsvorräte und paginierte
Treffer. Filter werden kanonisch serialisiert und bilden zusammen mit expliziten
Einschlüssen beziehungsweise Ausschlüssen einen stabilen Auswahlsnapshot. „Alle
Treffer“ hält deshalb keine Trackobjekte in der GUI. Bei einem Filterwechsel kann die
Auswahl erhalten, serverseitig auf die neue Treffermenge beschränkt oder verworfen
werden.

Jede Sammelaktion besitzt eine ausdrückliche Feldmaske. Leere Eingaben löschen keine
Werte; Entfernen und bewusst bestätigter Leerwert sind eigene Aktionen. Mehrfachwerte
werden wahlweise hinzugefügt, entfernt oder vollständig ersetzt. Geschützte Werte
werden nicht still überschrieben. Vorschlagsentscheidungen berücksichtigen Feldmaske
und Mindestkonfidenz; „Später prüfen“ persistiert keine Entscheidung.

Vor der Ausführung wird eine nicht verändernde Vorschau mit einmaligem Prüftoken und
den erwarteten Trackrevisionen erzeugt. Die Ausführung prüft diesen Snapshot erneut,
arbeitet seriell und verwendet höchstens 250 Titel je Teiltransaktion. Dieser Wert
begrenzt SQLite-Schreibsperren und erzeugt zugleich nur vier Fortschrittsereignisse je
1.000 Titel. Abbruch wird zwischen Teiltransaktionen wirksam. Ein Savepoint verhindert
Teiländerungen eines einzelnen fehlerhaften Tracks.

Schema 37 speichert begrenzte Batchköpfe und feldbezogene kanonische Vorher-/Nachher-
Werte. Schema 38 ergänzt typisierte Vorschlagsänderungen mit Vorschlags-ID, Status,
Entscheidungszeitpunkt und Entscheidungsgrund jeweils vor und nach der Sammelaktion.
Auch konkurrierende offene Vorschläge, die bei einer Annahme kontrolliert auf
`SUPERSEDED` gesetzt werden, werden dabei einzeln als abgelöst gekennzeichnet. Die
separate Migration ist notwendig, damit bereits erzeugte Entwicklungsdatenbanken mit
Schema 37 kontrolliert aktualisiert und nicht still neu interpretiert werden.

Die letzte vollständig oder teilweise ausgeführte Aktion kann nach einer Undo-Vorschau
zurückgenommen werden. Undo prüft Revision und aktuellen Feldwert sowie jeden aktuellen
Vorschlagsstatus einschließlich Entscheidungszeit und -grund. Angenommene Werte und
Feldstatus werden zurückgesetzt, zuvor offene angenommene oder abgelehnte Vorschläge
werden wieder geöffnet und durch die Annahme abgelöste Konkurrenten werden kontrolliert
wiederhergestellt. Sobald ein Feld oder Vorschlag später verändert wurde, wird der
gesamte betroffene Titel übersprungen und verständlich als Konflikt ausgewiesen. Eine
Zurückstellung persistiert nichts und ist deshalb nicht rückgängig zu machen. Eine
tatsächliche Rücknahme erhöht die Metadatenrevision erneut, wird selbst protokolliert
und kann nicht wiederholt werden.

Die Ergebnisstatus unterscheiden `COMPLETED` und `PARTIAL`. Geschützte Werte,
Revisionskonflikte, Fehler und nach einer Abbruchanforderung nicht mehr begonnene Titel
werden getrennt gezählt. Ein Abbruch beendet die aktuell laufende Teiltransaktion
geordnet; danach beginnt keine weitere Teiltransaktion.

Für „Wert setzen“ auf Titel, Interpret oder Album ist bei mehr als einem tatsächlich
änderbaren Titel zusätzlich zur normalen Sammelbestätigung eine eigene ausdrückliche
Bestätigung erforderlich. Sie zeigt Feld, gemeinsamen Zielwert, ausgewählte und
änderbare Anzahl sowie Beispielzeilen und weist darauf hin, dass alle betroffenen
Katalogeinträge denselben Wert erhalten. Diese Hürde gilt nicht für harmlose Aktionen
wie Bewertung setzen oder musikalische Dekade hinzufügen.

Die Oberfläche formatiert Text, Jahre, BPM, Konfidenzen, Energie, Tanzbarkeit,
Bewertung, Aufnahmeart einschließlich Remastermerkmal, bestätigte Leerwerte,
Mehrfachwerte und aktuelle Werte gegenüber Vorschlägen fachlich lesbar. Interne Enums,
Python-/JSON-Repräsentationen und `None` werden nicht angezeigt. Lange Werte werden in
der Zeile gekürzt und über den wiederverwendeten Tooltip vollständig zugänglich.

Diagnosen umfassen Abfrage-, Vorschau-, Batch- und Renderdauer, Treffermenge,
Teiltransaktionen, geänderte und übersprungene Titel, Revisionskonflikte sowie maximale
sichtbare Zeilen und Tooltips. Die Oberfläche verwendet zwölf wiederverwendete Zeilen.
Musikdateien, eingebettete Tags, Cue-, Lautheits-, EQ-, Queue-, Playlist-, Session- und
Historydaten bleiben unverändert.

## Bewusst noch nicht umgesetzt

Die eigentliche Audioanalyse in einem begrenzten separaten Prozess und ein späterer
Veranstaltungsplan sind nicht Teil von Paket 5. Playlist-, Veranstaltung- und Queuewerte
werden durch die Katalogpflege nicht erweitert. Die Metadatenmodelle bleiben bewusst von
Cue-, Lautheits- und Equalizerwerten getrennt; effektive Werte werden nicht über ein
generisches EAV-Modell gespeichert.
