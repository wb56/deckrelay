# Nachvollziehbare weiche Auswahlbewertung

Die automatische Katalogauswahl bewertet nur Kandidaten, die Quellenpriorität und alle
harten Ausschlussregeln bereits passiert haben. Ein Score kann deshalb weder eine
Queue-Priorität verändern noch einen ausgeschlossenen Titel wieder zulassen.

## Reihenfolge

1. Vorhandene Queue- und Quellenpriorität; die Automatik wird nur bei leerer Queue aktiv.
2. Harte Regeln in der bestehenden Reihenfolge und mit den bestehenden Relaxationsstufen.
3. Je zulässigem Automatikkandidaten genau ein Durchlauf aller weichen Regeln.
4. Höchster Gesamtscore.
5. Kanonische Sortierung nach Track-ID, damit die RNG-Eingabe nicht von Datenbank- oder
   Containerreihenfolgen abhängt.
6. Der vorhandene injizierbare RNG nur zwischen weiterhin vollständig gleich bewerteten
   Kandidaten.

## Standardregeln und Gewichtung

`selection.play_count` liefert `-10` Punkte je abgeschlossener Wiedergabe. Ein Titel ohne
Historieneintrag wird wie bisher mit Abspielzahl null behandelt und erhält keinen Abzug.

`selection.rating` bildet die redaktionelle Bewertung 1 bis 5 auf `-2` bis `+2` Punkte
ab; Bewertung 3 ist neutral. Eine fehlende oder ungültige Bewertung liefert
`UNKNOWN_METADATA` und null Punkte.

Der Abstand von zehn Punkten pro Wiedergabe ist bewusst größer als die maximale
Bewertungsdifferenz von vier Punkten. Damit bleibt die bisherige strikte Bevorzugung der
geringsten Abspielzahl erhalten. Neu ist ausschließlich, dass bei gleicher Abspielzahl
eine vorhandene Bewertung vor dem RNG entscheidet. Fehlt die Bewertung bei allen
gleich häufig gespielten Kandidaten, bleibt das bisherige RNG-Verhalten erhalten.

Jeder Einzelbeitrag wird mit stabiler Regel-ID, Regelversion, maschinenlesbarem
Reason-Code, sicheren Fakten und numerischem Beitrag in `SelectionRationale` abgelegt.
Pfade, Queue-Objekte und Wunschstellerdaten gehören nicht zu diesen Fakten.

Nicht Bestandteil dieses Schritts sind konfigurierbare Gewichtungen, Persistenz,
Bedienoberfläche, Vorschau sowie BPM-, Genre-, Energie-, Stimmungs- oder
Tanzbarkeitsregeln.
