# Erklärbare Quellen- und Prioritätsauflösung

Die Quellenauflösung beschreibt die bestehende Entscheidung in `QueueService`; sie ist
keine zweite Auswahlpipeline. `SourceResolution` entsteht erst für den tatsächlich
gewählten Queue-Eintrag beziehungsweise für das Ergebnis der bestehenden automatischen
Auswahl.

## Reihenfolge

Wartende persistierte Queue-Einträge gehen jeder Empty-Queue-Strategie vor. Ihre
Grundreihenfolge bleibt:

1. Priorität absteigend;
2. Position aufsteigend;
3. Einfügezeit aufsteigend;
4. Queue-ID aufsteigend.

Gleich priorisierte Gastwünsche behalten ihre vorhandene Round-Robin-Fairness. Innerhalb
einer Fairnessrunde gelten Einfügezeit, Position und Queue-ID. Die Erklärung nennt nur
die Rundennummer, niemals die Identität des Wunschstellers.

Die unveränderten Standardprioritäten sind Notfall 999, manuell 700, Gastwunsch
600/650/690, Playlist beziehungsweise Verzeichnis 300 und Automatik 100.

Erst ohne verwendbaren Queue-Eintrag greift die konfigurierte Empty-Queue-Strategie.
Automatisches Soft-Scoring bleibt vollständig innerhalb der automatischen
Katalogauswahl. Deren lokale Notfall-Playlist bleibt der letzte Fallback, wenn kein
sicherer Automatikkandidat existiert.

## Quelle und Herkunft

`SelectionSourceClass` enthält die normalisierte Verarbeitungsklasse. Die genaue,
sicher anzeigbare Herkunft steht getrennt in `origin_kind` und `origin_label`.
Verzeichnisimporte bleiben persistiert und verarbeitet als `QueueSource.PLAYLIST`, sind
aber als `directory` mit dem letzten Verzeichnisnamen erkennbar. Vollständige Pfade und
Wunschpersonendaten werden nicht in `SourceResolution` übernommen.

Bei automatischer oder Notfallauswahl entspricht die technische Kontext-ID der
`SelectionRationale.context_id`. Queue-Entscheidungen erhalten eine eigene Kontext-ID.
Die Erklärung verwendet ausschließlich bereits geladene Queue-Einträge und das bereits
vorhandene Automatikresultat; sie führt keine zusätzlichen Datenbankabfragen aus und
verändert weder Queue, Session, Verlauf, Audit noch Deckzustand.
