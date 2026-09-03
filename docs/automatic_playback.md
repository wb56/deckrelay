# Automatikmodus und CD-/Playlist-Queues

## Eine CD oder gespeicherte Playlist laden

Beim Laden in eine bereits belegte Queue fragt DeckRelay, wie mit den vorhandenen
Titeln verfahren werden soll:

- **Ja – ersetzen:** Wartende und nur vorbereitete Titel der bisherigen Queue werden
  entfernt. Ein bereits laufender Titel darf zu Ende spielen. Für eine CD, die
  vollständig und in ihrer Reihenfolge laufen soll, ist diese Auswahl empfohlen.
- **Nein – anhängen:** Vorhandene Titel bleiben vor den neu geladenen Titeln erhalten.
- **Abbrechen:** Die Queue bleibt unverändert.

Ein nur auf dem inaktiven Deck vorbereiteter Titel gehört noch zur bisherigen Queue
und wird beim Ersetzen ebenfalls entfernt. Die Anzeige **CD/Playlist** bezeichnet die
Herkunft des Eintrags; sie ist keine Fehlermeldung.

## Vollständig und in Reihenfolge abspielen

Die Rückfrage **Vollständig abspielen?** bestimmt die Wiedergaberegel:

- **Ja:** Die bewusst geladene CD-/Playlist-Reihenfolge bleibt erhalten. Der
  Wiederholungsschutz wird nur für diese geladenen Einträge übersteuert.
- **Nein:** Die normalen Party-Regeln gelten. Titel können beispielsweise wegen des
  Titel- oder Interpreten-Wiederholungsschutzes übersprungen werden.

## Automatik starten

- **Ab erstem wartenden Titel** ist der sichere Standard. Eine zufällig markierte
  Queue-Zeile verändert den Startpunkt nicht.
- **Ab ausgewähltem Titel** beginnt bewusst bei der markierten Zeile und zeigt vorab,
  was mit früheren Einträgen geschieht.

Die Startzusammenfassung nennt Starttitel, wartende und voraussichtlich spielbare
Titel sowie erkennbare Regelblockaden. Nach dem Start lädt DeckRelay den Folgetitel
auf das freie Deck und wechselt abwechselnd zwischen Deck A und Deck B.

## Pause, Fortsetzen und manuelle Eingriffe

- Die Pause eines spielenden Decks pausiert auch die Automatik.
- Das Fortsetzen desselben Decks setzt die Automatik wieder fort; der vorbereitete
  Folgetitel bleibt erhalten.
- Eine echte Crossfader-Bewegung pausiert die Automatik und zeigt den Grund an.
- Stoppen, Auswerfen oder manuelles Ersetzen eines Titels ist ein bewusster manueller
  Eingriff. Die Statusanzeige zeigt, ob die Automatik läuft, pausiert oder beendet ist.

## Cue-Werte und sicherer Fallback

Wenn Cue Out bereits überschritten ist oder nicht genug Vorbereitungszeit für einen
sicheren Crossfade bleibt, wird der Folgetitel nicht verworfen. Der laufende Titel
endet natürlich; danach startet der vorbereitete Folgetitel direkt. Eine entsprechende
Warnung erklärt den verwendeten Fallback.

## Fehler und Notfallfortsetzung

Kann ein einzelner Titel nicht vorbereitet oder gestartet werden, bleibt er mit
Fehlergrund in der Queue sichtbar. DeckRelay gibt das betroffene Deck kontrolliert
frei und bereitet nach den bestehenden Queue-Regeln einen Ersatz vor, ohne ein auf
dem anderen Deck laufendes Stück zu unterbrechen.

Nach mehreren unmittelbar aufeinanderfolgenden Fehlern oder ohne sicheren Ersatz
pausiert die Automatik. Die Statusmeldung nennt Deck und erforderlichen manuellen
Eingriff; mit dem normalen Automatik-Start kann nach erfolgreichem Laden oder einer
Recovery wieder fortgesetzt werden. Bestätigte Laufzeitfehler verwenden den
vorhandenen Ein-Deck-Betrieb und die isolierte Deck-Recovery.

## Nach einem Neustart

Bei aktivierter Sitzungswiederherstellung bleiben offene Queue-Einträge erhalten.
Vorbereitungs- und Deckzustände werden aus Sicherheitsgründen wieder auf wartend
gesetzt; die Wiedergabe startet niemals allein durch den Programmstart. Beim Laden
einer neuen CD anschließend bewusst **Ersetzen** oder **Anhängen** wählen.
