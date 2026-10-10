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
- Vor dem Pull Request die zum Risiko passende Testtiefe ausführen: gezielte T1-Tests,
  relevante T2-Bereichsprüfungen und statische Prüfungen; T3 und T4 liefern anschließend
  die PR- beziehungsweise Main-Evidenz. Ein vollständiger lokaler Pytest-Lauf ist nur
  erforderlich, wenn Änderungsrisiko oder Workflow ihn sachlich verlangen.

Die statischen T2-Prüfungen bleiben:

```powershell
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m black --check src tests
.\.venv\Scripts\python.exe -m mypy src\party_player
```

Für lokale Testläufe ist `scripts\Invoke-DevTests.ps1` der bevorzugte Einstieg:

```powershell
# T1: explizite Datei/Node-ID und optional ein pytest-Ausdruck
.\scripts\Invoke-DevTests.ps1 quick -Tests tests/test_queue_controller.py -Keyword "enqueue"

# Aktuelle Gruppen zuerst aus scripts/test-groups.psd1 lesen
.\scripts\Invoke-DevTests.ps1 regression -Group <gruppe-aus-test-groups.psd1>

# Vollständiger pytest-Lauf
.\scripts\Invoke-DevTests.ps1 full
```

Das Skript verwendet ausschließlich die Projekt-Venv, zeigt eine kurze Zusammenfassung und
speichert die vollständige Ausgabe unter `logs/dev-tests/`. Die Profile ersetzen weder die
übrigen T2-Prüfungen noch die verbindlichen CI-Gates.

Direkte Pytest-Aufrufe sind nur für Fälle vorgesehen, die der Runner nicht passend
abbildet, und verwenden immer `--tb=short -x --no-header --no-summary` sowie die kleinste
sinnvolle Testauswahl. Für gezielte Fehlerdiagnostik dient die vollständige Ausgabe im
Testlog; erfolgreiche Läufe, Vollsuite und CI werden nicht ohne technischen Anlass
wiederholt.

Der Pull Request soll Zweck, zugehöriges Issue, Risiken und den Testnachweis nennen.

## Deterministische PR-Automatisierung

`scripts/Invoke-DevPr.ps1` ist der bevorzugte Einstieg für die unterstützten Git- und
GitHub-Schritte `status`, `validate`, `commit`, `publish`, `gates`, `merge` und `cleanup`.
Es führt sie einzeln und fail-closed aus. Jeder Aufruf schreibt ein kompaktes
Sicherheitsprotokoll nach `logs/dev-pr/`; dieses enthält nur Aktions-/Operationsnamen und
Exit-Codes, keine vollständige Prozessausgabe. Die Konsole zeigt nur Status, Ergebnis und
Logpfad. Das Skript setzt `safe.directory` ausschließlich für den jeweiligen Git-Aufruf
und ändert keine globale Git-Konfiguration.

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
reviewed -> owner-approved -> mergeable -> merged -> main gate -> cleanup`. Technische
Review, Eigentümerfreigabe, Merge und Cleanup sind getrennte Schritte. Wiederholte
Leseaktionen sind sicher; `commit` meldet ohne Änderungen `NOOP`, `publish` erkennt einen
vorhandenen offenen PR und `cleanup` meldet bei einem bereits fehlenden Branch `NOOP`.

`REVIEW-REQUIRED` ist der Standard und erlaubt dem Skript keinen Merge. Für
`OWNER-APPROVED` gelten zusätzlich getrennte technische und eigentümerseitige
Attestierungen für denselben vollständigen Head-SHA:

GitHub begrenzt Labelnamen auf 100 Zeichen. Die Nachweise verwenden deshalb die kurzen,
stabilen Labels und speichern den SHA getrennt in den PR-Attestierungen:

- für eine abgeschlossene unabhängige technische Prüfung das aktive Label
  `technical-reviewed` und einen PR-Kommentar exakt im Format
  `DECKRELAY-ATTESTATION-V1 type=technical-review pr=<nr> head=<40-stelliger-head-sha> decision=approved`;
- für die ausdrückliche Eigentümerfreigabe das aktive Label `owner-approved` und einen
  getrennten PR-Kommentar mit `type=owner-approval` im selben Format;
- für jeden Nachweis den Repository-Eigentümer als serverseitig ausgewiesenen Urheber,
  unveränderte GitHub-Zeitstempel nach dem attestierten Commit und ein nachfolgendes
  Label-Ereignis desselben Eigentümers;
- erfolgreiche Quality Gates, erfüllte Branch-Protection und eindeutige Mergefähigkeit.

Die technische Attestierung bestätigt nur, dass ein unabhängiger technischer Bericht für
den genannten SHA geprüft wurde; sie ist ausdrücklich keine GitHub-Approval. Die
Owner-Attestierung ist davon getrennt die Merge-Freigabe. Das Skript liest diese
Nachweise ausschließlich und besitzt keinen Pfad, um Kommentare oder Labels anzulegen;
Codex darf sie weder erzeugen noch als menschliche Nachweise ausgeben. Fehlende,
veraltete, bearbeitete oder widerrufene Nachweise sowie Labels für einen älteren Commit
führen zu `BLOCKED`. Jede Änderung des PR-Heads erfordert eine erneute Prüfung beider
Nachweise für denselben PR und den neuen vollständigen SHA. Der Merge erfordert
dennoch einen eigenen Aufruf mit `-Mode OWNER-APPROVED -ConfirmMerge` und verwendet
GitHubs SHA-Bindung `--match-head-commit`. Verlangt Branch-Protection oder ein wirksames
Ruleset formelle GitHub-Approvals, muss GitHubs `reviewDecision` diese als erfüllt melden;
das Einzelentwicklerverfahren ersetzt oder umgeht sie niemals.

Klassische Branch-Protection wird mit einem separaten HTTP-Status-Probe geprüft. Nur ein
eindeutiger HTTP-404 bei fehlgeschlagenem API-Aufruf bedeutet „nicht vorhanden“; 401, 403,
5xx, Netzwerkfehler und widersprüchliche Zustände führen zu `BLOCKED`. Ruleset-Muster
werden nicht lokal nachgebildet. Stattdessen fragt das Skript über GitHubs
branch-spezifischen Endpoint `rules/branches/<basisbranch>` ausschließlich die von GitHub
selbst als wirksam ermittelten Regeln ab. Ist diese Ermittlung nicht möglich, folgt
ebenfalls `BLOCKED`.

Lokale Logs enthalten ausschließlich Aktionsname, freigegebene Werkzeug-/Operationsnamen
und Exit-Codes. Befehlsargumente, API-Antworten sowie Standardausgabe und Fehlerausgabe
externer Werkzeuge werden nicht protokolliert, da sie sensible Daten enthalten können.

`BLOCKED` beendet die Aktion bei schmutzigem Arbeitsbaum, falschem Branch, fehlenden oder
unbekannten Berechtigungen, abweichendem SHA, fehlender Review/Freigabe, laufenden Gates,
unklarem Protection-Zustand, Mergekonflikt oder nicht sicher zuordenbarer Bereinigung.
Fehlgeschlagene Gates ergeben `FAIL`. Es gibt weder Force-Push noch `git add .`, direkte
Main-Commits, Schutzregeländerungen oder eine automatische Freigabe. `cleanup` löscht nur
einen lokalen `feature/*`-Branch, der bereits in `main` enthalten und an keinen Worktree
gebunden ist. Es entfernt weder Worktrees noch Remote-Branches. Diese Schritte erfolgen
bei Bedarf bewusst und manuell, ausschließlich nach erfolgreichem Main-Gate für den
Merge-Commit oder ausdrücklich dokumentiertem `n/a`, sauberer Zuordnung und ohne Force.

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
