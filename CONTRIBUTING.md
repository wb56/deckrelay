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

Für kompakte lokale Testläufe steht `scripts\Invoke-DevTests.ps1` zur Verfügung:

```powershell
# T1: explizite Datei/Node-ID und optional ein pytest-Ausdruck
.\scripts\Invoke-DevTests.ps1 quick -Tests tests/test_queue_controller.py -Keyword "enqueue"

# Benannte fachliche Regressionstestgruppe aus scripts/test-groups.psd1
.\scripts\Invoke-DevTests.ps1 regression -Group automatic_selection

# Vollständiger pytest-Lauf
.\scripts\Invoke-DevTests.ps1 full
```

Das Skript verwendet ausschließlich die Projekt-Venv, zeigt eine kurze Zusammenfassung und
speichert die vollständige Ausgabe unter `logs/dev-tests/`. Die Profile ersetzen weder die
übrigen T2-Prüfungen noch die verbindlichen CI-Gates.

Der Pull Request soll Zweck, zugehöriges Issue, Risiken und den Testnachweis nennen.

## Deterministische PR-Automatisierung

`scripts/Invoke-DevPr.ps1` führt Git- und GitHub-Schritte einzeln und fail-closed aus.
Jeder Aufruf schreibt das vollständige lokale Protokoll nach `logs/dev-pr/`; die Konsole
zeigt nur Status, Ergebnis und Logpfad. Das Skript setzt `safe.directory` ausschließlich
für den jeweiligen Git-Aufruf und ändert keine globale Git-Konfiguration.

```powershell
# Übersicht und lokale Vorbedingungen
.\scripts\Invoke-DevPr.ps1 -Action status
.\scripts\Invoke-DevPr.ps1 -Action validate

# Nur die genannten Dateien committen und den Branch veröffentlichen
.\scripts\Invoke-DevPr.ps1 -Action commit -Files scripts/Invoke-DevPr.ps1,tests/test_dev_pr_script.py -Message "Add deterministic PR automation"
.\scripts\Invoke-DevPr.ps1 -Action publish -Repository wb56/deckrelay -PrTitle "Kurzer Zweck" -PrBody "Zweck, Issue, Risiken und Testnachweis"

# Gates immer an den vollständigen, erwarteten Head-SHA binden
.\scripts\Invoke-DevPr.ps1 -Action gates -Repository wb56/deckrelay -PrNumber 54 -ExpectedHeadSha <40-stelliger-sha>
```

Die Zustandsfolge ist `lokal -> validiert -> committed -> published -> gates passed ->
reviewed -> owner-approved -> mergeable -> merged -> cleanup`. Die Aktionen überspringen
keinen Zustand. Wiederholte Leseaktionen sind sicher; `commit` meldet ohne Änderungen
`NOOP`, `publish` erkennt einen vorhandenen offenen PR und `cleanup` meldet bei einem
bereits fehlenden Branch `NOOP`.

`REVIEW-REQUIRED` ist der Standard und erlaubt dem Skript keinen Merge. Für
`OWNER-APPROVED` gelten zusätzlich getrennte technische und eigentümerseitige
Attestierungen für denselben vollständigen Head-SHA:

- für eine abgeschlossene unabhängige technische Prüfung die aktiven Labels
  `technical-reviewed` und `reviewed-head:<40-stelliger-head-sha>`;
- für die ausdrückliche Eigentümerfreigabe die aktiven Labels `owner-approved` und
  `approved-head:<40-stelliger-head-sha>`;
- für jedes Label ein GitHub-Label-Ereignis, das den Repository-Eigentümer als Urheber
  ausweist;
- erfolgreiche Quality Gates, erfüllte Branch-Protection und eindeutige Mergefähigkeit.

Die technischen Labels bestätigen nur, dass ein unabhängiger technischer Bericht für den
genannten SHA geprüft wurde; sie sind ausdrücklich keine GitHub-Approval. Die Owner-Labels
sind davon getrennt die Merge-Freigabe. Kommentare, Codex-Aussagen, Labels für einen
älteren Commit oder nur die Option `-ConfirmMerge` genügen nicht. Der Merge erfordert
dennoch einen eigenen Aufruf mit `-Mode OWNER-APPROVED -ConfirmMerge` und verwendet
GitHubs SHA-Bindung `--match-head-commit`. Verlangt Branch-Protection oder ein wirksames
Ruleset formelle GitHub-Approvals, muss GitHubs `reviewDecision` diese als erfüllt melden;
das Einzelentwicklerverfahren ersetzt oder umgeht sie niemals.

Rulesets werden gegen den tatsächlichen PR-Basisbranch ausgewertet. Include- und
Exclude-Muster müssen eindeutig interpretierbar sein; unbekannte Bedingungen, nicht
lesbare Schutzregeln oder API-Fehler führen zu `BLOCKED`.

Lokale Logs enthalten ausschließlich Aktionsname, freigegebene Werkzeug-/Operationsnamen
und Exit-Codes. Befehlsargumente, API-Antworten sowie Standardausgabe und Fehlerausgabe
externer Werkzeuge werden nicht protokolliert, da sie sensible Daten enthalten können.

`BLOCKED` beendet die Aktion bei schmutzigem Arbeitsbaum, falschem Branch, fehlenden oder
unbekannten Berechtigungen, abweichendem SHA, fehlender Review/Freigabe, laufenden Gates,
unklarem Protection-Zustand, Mergekonflikt oder nicht sicher zuordenbarer Bereinigung.
Fehlgeschlagene Gates ergeben `FAIL`. Es gibt weder Force-Push noch `git add .`, direkte
Main-Commits, Schutzregeländerungen oder eine automatische Freigabe. `cleanup` löscht nur
einen lokalen `feature/*`-Branch, der bereits in `main` enthalten und an keinen Worktree
gebunden ist; Remote-Bereinigung bleibt eine bewusste separate Verwaltungsaktion.

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
