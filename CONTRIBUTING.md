# Contributing

Danke fur dein Interesse an DeckRelay.

## Aktueller Projektmodus

DeckRelay 1.0.0 ist stabil veröffentlicht. Die nächste reguläre Entwicklungslinie ist
DeckRelay 2.0. Fehlerberichte, Funktionsvorschläge und externe Code-Beiträge sind
willkommen.

## Was wir aktuell gerne annehmen

- Bug Reports
- Verstandnisfragen und Missverstandnisse in der Bedienung
- Funktionswunsche (Wunsche)

Bitte nutze dafur die Issue-Templates im Reiter "Issues".

## Pull Requests

- Vor größeren Änderungen bitte zuerst ein Issue eröffnen und den Lösungsansatz
  abstimmen.
- Einen thematisch fokussierten Branch verwenden und keine unabhängigen Änderungen
  in demselben Pull Request bündeln.
- Architektur `UI → Controller → Service → Repository → SQLite` einhalten.
- Datenbankänderungen ausschließlich über vorwärtskompatible Migrationen umsetzen.
- Musik- und andere Mediendateien niemals verändern, verschieben oder mitliefern.
- Blockierende Datei-, Datenbank- und Audioarbeit gehört nicht in den GUI-Thread.
- Neue Audiofunktionen müssen mit einem Fake-Backend testbar sein.
- Code und Bezeichner sind englisch, sichtbare UI-Texte deutsch.
- Vor dem Pull Request folgende Prüfungen ausführen:

```powershell
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m black --check src tests
.\.venv\Scripts\python.exe -m mypy src\party_player
.\.venv\Scripts\python.exe -m pytest -q
```

Der Pull Request soll Zweck, zugehöriges Issue, Risiken und den Testnachweis nennen.

## Lizenz der Beiträge

Mit dem Einreichen eines Beitrags erklärst du, dass du ihn unter derselben Lizenz
wie DeckRelay bereitstellst: GNU General Public License v3.0 oder später
(`GPL-3.0-or-later`). Reiche ausschließlich Code und Ressourcen ein, für die du die
erforderlichen Rechte besitzt und deren Lizenz mit dem Projekt vereinbar ist.

## Hinweise fur gute Meldungen

- Beschreibe das beobachtete Verhalten klar und kurz.
- Notiere die verwendete Version (Tag/Release) und dein Betriebssystem.
- Fur Fehler: Schritte zur Reproduktion, erwartetes Verhalten, tatsachliches Verhalten.
- Wenn vorhanden, relevante Auszuge aus Diagnoseberichten aus `diagnostics/`.

## Sicherheit

Keine sensiblen Daten (z. B. private Dateipfade, Zugangsdaten, personenbezogene Daten) in Issues posten.
