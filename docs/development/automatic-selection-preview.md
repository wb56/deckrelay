# Zustandsneutrale Automatikauswahl-Vorschau

`AutomaticSelectionService.preview()` berechnet bis zu zehn zukünftige
Automatiktitel als unveränderliche Momentaufnahme. Jeder Schritt verwendet die
bestehenden harten Regeln, Relaxationsstufen, Soft-Scores, Quellenauflösung und
`SelectionRationale`. Es existiert kein zweites Entscheidungs- oder
Begründungsmodell.

Die Vorschau lädt den sichtbaren Kandidatenbestand einmal und den für
Abspielzahlen sowie Recent-Track-Schutz benötigten Verlauf mit einer weiteren
Abfrage. Danach simuliert sie Abspielzahl, Recent-Track-Liste sowie Titel- und
Interpretenabstand ausschließlich in kopiertem Speicher. Die Laufzeit ist bei
`n` Kandidaten und Vorschautiefe `d <= 10` grundsätzlich `O(n * d)`.

Regeln mit eigenen Repositories behalten derzeit ihre vorhandenen Lesezugriffe.
Diese Zugriffe werden nicht durch die Vorschau versteckt oder vervielfacht:
Eine Regel wird je Kandidat und Relaxationsstufe höchstens so oft ausgeführt wie
bei einer entsprechenden produktiven Auswahl. Über mehrere Vorschauschritte
kann sie folglich erneut lesen. Die harte Tiefengrenze zehn begrenzt diesen
Aufwand; ein späterer gemeinsamer Policy-Snapshot kann diese verbleibenden
Lesezugriffe bündeln, ohne den Vorschauvertrag zu ändern.

Die produktive Zufallsquelle wird nicht fortgeschaltet. Die Vorschau kopiert
ihren Zustand, kopiert die Regelkette und verändert weder Queue noch Historie,
Session/Audit, Sperren, Operatorausnahmen, Decks, Automatikzustand oder
`last_rationale`/`last_relaxation_stage`. Datei-Revalidierung und sonstiges
Datei-I/O sind nicht Bestandteil des Vorschaupfads.

Das Ergebnis ist vorläufig: Queue-Eingriffe, neue Wünsche, Metadatenänderungen,
Dateifehler und zwischenzeitlich gespielte Titel können die spätere reale Folge
verändern. Titel- und Interpretname sind sichere Anzeigedaten; Dateipfade und
personenbezogene Wunschdaten werden nicht übernommen.
