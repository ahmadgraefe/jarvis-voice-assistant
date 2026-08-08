# Jarvis Roadmap

25 Punkte in Tiers, Richtung Tony Starks JARVIS: proaktiv, selbstbewusst, autonom.
Vorgehen: Punkt für Punkt, mit sauberen Tests nach jedem Schritt, nicht alles auf einmal.
Security-Fixes sind bewusst aufgeschoben (siehe UEBERGABE-verlauf.md fuer die eine Ausnahme).

Fortschritt und Belegstellen stehen in `UEBERGABE.md` / `UEBERGABE-verlauf.md`, nicht hier
wiederholt. Diese Datei ist die stabile Referenz fuer WAS die 24 Punkte sind, damit sie nicht
mehr vom Chatverlauf abhaengt.

## Tier 0 — Fundament (ohne das bremst alles andere)

Diese drei Dinge sind keine "Skills", aber sie bestimmen, wie mächtig jede Skill danach sein kann.

1. **Weg vom Text-Tag-Hack, hin zu echtem Tool-Use.** Aktuell: Das Modell schreibt am Ende seiner Antwort `[ACTION:INSTAGRAM] payload`, das wird per Regex geparst, eine Aktion pro Turn, danach zwei weitere sequenzielle Claude-Calls (Antwort → Zusammenfassung). Mit Anthropics nativem tool_use (parallele Tool-Calls, mehrstufiges Reasoning in einem Response) könnte Jarvis in einem Gesprächszug z.B. gleichzeitig den Kalender checken, eine Instagram-Zahl abrufen und eine WhatsApp vorbereiten, statt das über drei Gesprächsrunden zu strecken. Das ist der Unterschied zwischen "Assistent, der einen Befehl abarbeitet" und "Assistent, der ein Problem löst".
2. **Zentrale Tool-Registry statt if/elif-Kette.** Jede neue Fähigkeit brauchte einen manuellen Eintrag in einer 200-Zeilen-Kette. Eine Registry (Name → Funktion → JSON-Schema → Berechtigungsstufe) macht aus "neue Skill bauen" einen 10-Minuten-Job statt einer server.py-Chirurgie, Voraussetzung dafür, dass die Liste unten überhaupt wachsen kann, ohne dass die Datei unwartbar wird.
3. **Event-Bus zwischen background_brain.py und server.py.** Die beiden Prozesse teilten sich nur Dateien, der Hintergrund-Prozess konnte während eines laufenden Gesprächs nichts "reinrufen". Ein simpler lokaler Pub/Sub erlaubt, dass eine Entdeckung im Hintergrund (Instagram-Follower-Sprung) sofort ins laufende Gespräch einfließt, statt erst beim nächsten pending_questions-Poll.

## Tier 1 — Proaktivität (das eigentliche "er lebt, auch wenn ich nicht rede")

4. **Echtes Unterbrechen statt Warteschlange.** Ein "Jarvis spricht zuerst"-Modus: der Server pusht über den WebSocket eine unaufgeforderte TTS-Nachricht, wenn etwas wirklich Dringendes reinkommt (z.B. Follower-Absturz, Fanplace-Einbruch).
5. **Urgency-Scoring statt FIFO.** Ein LLM-Klassifikator bewertet jede offene Frage nach Impact × Dringlichkeit und sortiert danach, Jarvis führt durch das, was wirklich zählt, statt chronologisch abzuarbeiten.
6. **Proaktive Briefings ohne Doppelklatschen.** Morgens/abends eine Zusammenfassung, die sich selbst meldet, statt darauf zu warten, dass gefragt wird.
7. **Autonomie-Stufen ausweiten.** Das Jerome-Muster (classify → routine: auto-antworten, notable: informieren, needs_ahmad: eskalieren) generalisiert auf Gmail (Antwort-Entwürfe für Routine-Mails) und Kalender (Konflikte erkennen), mit einer konfigurierbaren Vertrauensstufe pro Kanal.
8. **Ziel-/Projekt-Tracking als generelles Muster.** Die "Trial Reel Wave"-State-Machine ist im Kern ein Ziel-Tracker, aber fest an ein Feature gebunden. Als generisches Konzept wird daraus eine echte Assistenz-Fähigkeit statt eines Einzelfalls.

## Tier 2 — Gedächtnis (damit er sich wirklich "an alles erinnert")

9. **Semantische Suche statt Kategorien-Budget.** Mit Embeddings kann Jarvis relevanzbasiert genau die Erinnerungen ziehen, die zur aktuellen Frage passen, unabhängig von Kategorie-Grenzen.
10. **Wissensgraph statt Flat-Files.** Personen (Jerome, Geschäftskontakte), Projekte (Luna Vale, Fanplace), Entscheidungen sind aktuell verstreut in Text. Ein einfacher Graph (auch nur eine SQLite-Tabelle mit Relationen: Person→Projekt, Entscheidung→Grund→Ergebnis) erlaubt Fragen wie "was haben wir bei Jerome zuletzt entschieden und warum" ohne dass Jarvis raten muss.
11. **Selbst-Konsolidierung (das "Dreaming").** Ein periodischer Pass in background_brain.py, der Jarvis' eigenes Gedächtnis pflegt (Duplikate mergen, Veraltetes archivieren) — conversation_log.jsonl würde sich so selbst eindampfen, statt nur gelesen, nie bereinigt zu werden.
12. **Outcome-Tracking.** Entscheidungen mit ihren späteren Ergebnissen verknüpfen ("wir haben X versucht, Ergebnis war Y"), macht aus reinem Gedächtnis echtes Lernen.

## Tier 3 — Neue Fähigkeiten (breiter als heute)

13. **Gmail: vom Beobachter zum Akteur.** Antwort-Entwürfe für Routine-Mails, Auto-Kategorisierung, mit Eskalation für alles Wichtige.
14. **Kalender-getriebene Proaktivität.** Konflikterkennung, Vorbereitungs-Reminder ("in 20 Min Meeting X, hier der Kontext dazu").
15. **Finanz-Tracking.** Anbindung an Fanplace-Einnahmen/PayPal/Stripe, automatisch ins Sheet, mit Trend-Alerts.
16. **Breiterer Web-Agent.** Formulare ausfüllen, mehrstufige Recherche-Ketten, mit Bestätigung für Käufe/Sends.
17. **Handy-Companion.** Ein simpler Web-Client mit Push-Benachrichtigung (PWA reicht) macht Jarvis überall erreichbar, nicht nur am Schreibtisch. Vermutlich der größte "fühlt sich nach echtem JARVIS an"-Hebel überhaupt.
18. **Multi-Repo-Bewusstsein für Claude Code.** Projektübergreifender Status, wenn an mehreren Projekten gearbeitet wird.
19. **Kontinuierliches Screen-Bewusstsein (mit klaren Grenzen).** Eine sehr niedrigfrequente Hintergrund-Erfassung würde Kontext ohne Nachfragen liefern, ist aber ein echter Privacy-Trade-off, den Ahmad bewusst entscheiden muss.
20. **Barge-in / Unterbrechbarkeit.** Echtes Dazwischenreden während der TTS-Antwort ist ein spürbarer "er hört wirklich zu"-Moment.
25. **Tiefes Web-Scraping / strukturierte Datenextraktion.** Punkt 16 (Breiterer Web-Agent) reicht für einzelne Seiten und einfache Formulare, aber nicht für echte Vergleichs-Recherchen mit vielen Ergebnissen (z.B. "vergleiche die besten Flüge/Preise/Angebote"). Fehlt aktuell: `read_tab` liefert nur einen auf ~3000 Zeichen gekappten Textauszug statt vollständiger Listen, es wird nicht auf dynamisch nachladende Inhalte gewartet (viele Such-/Vergleichsseiten laden Ergebnisse asynchron nach dem ersten Laden nach), und es gibt keine strukturierte Extraktion (Preis/Anbieter/Zeit/Ort als saubere Felder statt Fließtext zum Selbst-Interpretieren). Besonders wichtig im Hinblick auf Punkt 17 (Handy-Companion): unterwegs am Handy will Ahmad eine fertige, verlässliche Antwort in einem Zug, nicht mehrere Nachfrage-Runden weil eine Seite nur halb gelesen wurde.

## Tier 4 — Selbstheilung (Robustheit, nicht Security)

21. **Aktives statt passives Health-Monitoring.** Ein Watchdog, der bei Hängern (nicht nur Abstürzen) selbst eingreift (Prozess neu starten, Playwright-Session neu aufbauen) — sonst hängt Tier 1-3 in der Luft, wenn der Hintergrund-Prozess mal einfach steckenbleibt.
22. **Sichtbares Self-Improve-Changelog.** self_improve_pass lässt Claude Code unsupervised an sich selbst rumschrauben. Ein Changelog, das morgens sichtbar ist, macht diese sonst unsichtbare Autonomie nachvollziehbar und rückgängig-machbar.

## Über-dimensional (Ahmads eigene Formulierung)

23. **Multi-Agent-Delegation.** Bei komplexen Anfragen mehrere spezialisierte Sub-Agenten parallel lostreten (Instagram-Analyse, Finanzen, Recherche) und die Ergebnisse zusammenführen, statt seriell Modul für Modul abzufragen.
24. **Szenario-Simulation.** Vor wichtigen Jerome-Entscheidungen lässt Jarvis einen Sub-Agenten mehrere Ausgänge durchspielen, bevor er empfiehlt, kein Hellsehen, aber strukturiertes Vorausdenken statt Bauchgefühl.
