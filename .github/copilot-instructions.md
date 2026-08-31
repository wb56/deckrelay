# Copilot Instructions — DeckRelay

`AGENTS.md` und die dort verlinkten GUI- und Lizenzrichtlinien sind für die
DeckRelay-2.0-Entwicklung verbindlich.

- Technologie: Python 3.11+, CustomTkinter, tkinter/ttk, SQLite und die vorhandenen
  Audioabstraktionen. Kein eigenständiger Frameworkwechsel.
- Architektur strikt einhalten: UI → Controller → Service → Repository → SQLite.
- UI enthält weder SQL noch Geschäftslogik oder Dateimanipulationen und greift nicht
  direkt auf ein konkretes Audio-Backend zu.
- Musikdateien sind schreibgeschützt zu behandeln: nicht automatisch umbenennen, verschieben, löschen oder Metadaten überschreiben.
- Datenbankänderungen erfolgen über Migrationen, SQL ist parametrisiert und `SELECT *` ist verboten.
- Auf mindestens 100.000 Titel auslegen: Pagination, Indizes, Lazy Loading und Hintergrundarbeit verwenden.
- Tkinter nur im Hauptthread aktualisieren; Worker kommunizieren über Queue, Callbacks oder `after()`.
- Projekt-Logging verwenden, niemals `print()`.
- Code und Bezeichner Englisch, UI-Texte Deutsch; Type Hints und Docstrings sind Pflicht.
- Tests verwenden temporäre SQLite-Datenbanken, gemockte Player und keine echte Musiksammlung.
- Party-Oberflächen zeigen keine Tag-Editoren, Datenbankwartung oder Dateioperationen.
