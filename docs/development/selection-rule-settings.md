# Persistierbare Soft-Regelkonfiguration

Schema 42 speichert die Konfiguration der bestehenden Soft-Regeln in
`selection_rule_settings`. Der Primärschlüssel ist die stabile Regel-ID; jede
Zeile enthält Konfigurationsversion, Aktivstatus und eine begrenzte Gewichtung.
Es wird weder ausführbare Konfiguration noch eine Kandidatenentscheidung
persistiert. Unbekannte zukünftige Regel-IDs werden ignoriert.

Die Standardwerte bilden das bisherige Verhalten exakt ab:

- `selection.play_count`, Version 1: aktiv, 10 Punkte Abzug je abgeschlossener
  Wiedergabe;
- `selection.rating`, Version 1: aktiv, Faktor 1 und damit Beiträge von -2 bis
  +2; fehlende oder ungültige Bewertungen bleiben neutral.

Der Abspielabzug ist auf 5 bis 100 Punkte begrenzt. Der Bewertungsfaktor liegt
zwischen 0 und 1. Damit beträgt die größtmögliche Bewertungsdifferenz vier
Punkte und eine einzige zusätzliche Wiedergabe bleibt stets ausschlaggebend.

Fehlende, beschädigte, nicht-endliche, außerhalb der Grenzen liegende oder in
einer unbekannten Version gespeicherte Werte fallen zeilenweise auf die
sicheren Standardwerte zurück. Schreibzugriffe lehnen solche Werte ab.

Eine reale Automatikauswahl lädt die gesamte Konfiguration mit genau einer
zusätzlichen Datenbankabfrage. Eine Mehrschritt-Vorschau lädt ebenfalls genau
einen unveränderlichen Konfigurationssnapshot, den alle Vorschauschritte
verwenden. Queue-, Wunsch-, Playlist- und Notfallprioritäten sowie harte Regeln
werden von der Soft-Konfiguration nicht verändert.
