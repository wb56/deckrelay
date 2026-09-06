# Strukturierte Auswahlbegründung

`SelectionRationale` ist das gemeinsame, unveränderliche Diagnosemodell der
Auswahlpipeline. Es übernimmt ausschließlich Ergebnisse des tatsächlich ausgeführten
Pfads; Regeln, Scores, Quellenauflösung und RNG werden für die Erklärung nicht erneut
ausgeführt.

## Aufbau und Reihenfolge

Eine Begründung enthält eine stabile Schema-Version, Kontext-ID, Gesamtergebnis,
Relaxationsstufe, Tie-Break-Verfahren, optionale `SourceResolution` und die geordneten
Kandidatenauswertungen. Pro Kandidat folgen auf die sichere Kandidatenidentität die in
Ausführungsreihenfolge entstandenen harten Regelbewertungen und anschließend – nur für
zulässige Automatikkandidaten – die weichen Scorebeiträge samt Gesamtscore.

Die Kandidatenkategorie unterscheidet `SELECTED`, `EXCLUDED` und
`ELIGIBLE_NOT_SELECTED`. Ein zulässiger, aber niedriger bewerteter oder im Gleichstand
nicht gewählter Titel wird damit niemals als ungeeignet bezeichnet.

Stabile Abschlusscodes sind insbesondere `SELECTED_QUEUE_PRIORITY`,
`SELECTED_HIGHEST_SCORE`, `SELECTED_RNG_TIE_BREAK`, `SELECTED_EMERGENCY_ORDER`,
`LOWER_TOTAL_SCORE`, `STABLE_TIE_BREAK_LOSS` und `RNG_TIE_BREAK_LOSS`. Bei einem
Ausschluss bleibt der bereits bestehende fachliche oder technische Code erhalten, etwa
`BLOCKED_TRACK`, `TRACK_MISSING` oder `FILE_MISSING`. `NO_SAFE_CANDIDATE` bezeichnet
ausschließlich das Gesamtergebnis ohne zulässigen Titel.

`RELAXED` bleibt an die aktive Relaxationsstufe gebunden, `OVERRIDDEN` an eine
ausdrückliche Operatorausnahme. `NOT_APPLICABLE` und `UNKNOWN_METADATA` bleiben neutral.
Nach dem ersten terminalen Ausschluss endet der Regeldurchlauf weiterhin; die
Begründung ergänzt keine hypothetischen Ergebnisse.

## Datenschutz und Serialisierung

Alle Strukturen bestehen aus unveränderlichen Dataclasses, Enums und skalaren Fakten
und können über `dataclasses.asdict` serialisiert werden. Enthalten sind sichere Track-
und Queue-IDs sowie bereits freigegebene Titel-/Interpretdaten. Ausgeschlossen sind
Dateipfade, Gastidentitäten, Freitextwünsche, Domänenobjekte, Repositories,
Datenbankverbindungen und Legacy-Regelobjekte. Kandidatentraces bleiben auf 50 Einträge
begrenzt; Anzahl und Auslassungen werden separat ausgewiesen.

## Grenzen

Dieser Schritt persistiert keine Begründungen und zeigt sie nicht in der GUI an. Er
berechnet weder eine Vorschau noch mehrere zukünftige Titel. Vorschau, persistierte
Konfiguration/GUI-Integration sowie Migration und reale Abnahme bleiben den Schritten
6, 7 und 8 vorbehalten.
