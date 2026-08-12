# Luna Vale — Status (aus Claude Desktop App übernommen)

Stand: 2026-08-11 (Grosser Jarvis-Audit — mehrere Praezisierungen: Kernregeln, Jerome-Kommunikation, Feste-Grenzen-Enforcement; siehe jeweilige Abschnitte)

## Worum es geht

Ahmad betreibt fünf Instagram-Personas unter der Marke "Luna Vale" (Stand 2026-08-09 — zwei neue seit der ursprünglichen drei dazugekommen):

| Account | Persona | Status |
|---|---|---|
| lunaxvale | Luna Vale — dark feminine / alt-goth (Hauptaccount) | Aktiv, etabliert |
| cowgirllunavale | Cowgirl / Country-Girl | Aktiv, 46 Posts, 527 Follower |
| lunavalethegoth | Cosplay (Handle irreführend — ist der Cosplay-Account) | Aktiv, 73 Posts, 4.578 Follower |
| lunas.crypt | Neu seit 2026-08-09, laut Ahmad "läuft aktuell fantastisch" — genaue Persona/Follower noch nicht dokumentiert | Aktiv |
| succubuslunavale | Neu seit 2026-08-09, laut Ahmad "läuft aktuell fantastisch" — genaue Persona/Follower noch nicht dokumentiert | Aktiv |

Erfolgsrezept der beiden neuen Accounts: kopieren stark den Content-Stil von @gothgirlenya (getrackter Konkurrent), konkret Outfit-Switch/Outfit-Transition-Content.

**Details zu lunas.crypt/succubuslunavale sind bewusst unvollständig — Ahmad sollte Persona-Beschreibung, Follower-Zahlen und Content-Stimme ergänzen, sobald Zeit ist, damit Jarvis sie genauso gut kennt wie die ersten drei.**

## Rollenverteilung

- **Ahmad**: postet persönlich auf Instagram/TikTok/Facebook, trifft alle finalen Entscheidungen, einzige Person die Accounts/Geräte selbst bedient.
- **Jerome** (Mitarbeiter): erstellt und optimiert Content. Kontakt über WhatsApp.
- **Jarvis**: Tracking, Analyse, Content-Strategie-Empfehlungen, Koordination zwischen Ahmad und Jerome. Kein Posten, keine Gerätesteuerung.

## Tracking-Workbook

Link: https://docs.google.com/spreadsheets/d/1VvQpaYSUF668MQjr-w7VYwiThQozXmR3zdruw5C_V08/edit — natives Google Sheet, komplett auf Englisch (Jerome spricht nur Englisch).

Tabs: Accounts Overview, Instructions, Daily Production List, Winner Tracking, Link Funnel, Scaling Log, Trial Reel Waves, Target Creator List, Insights Eingang. Eine gemeinsame Tab-Struktur für alle Accounts (unterschieden über "Account"-Spalte) — bewusst als "alle Accounts" statt einer festen Zahl formuliert, damit das nicht bei jedem neuen Account veraltet.

**Seit 2026-08-05: direkter Live-Schreibzugriff** über die Sheets API — kein Download/Import/Menschen-Klick mehr nötig, Jarvis schreibt direkt und sofort.

## Kernregeln

- Outlier = Video ≥2x über dem 6-Video-Baseline-Schnitt (6 davor + 6 danach)
- 20/40-Regel: US-Audience-% <20% = Delete, 20–40% = Caution, >40% = Keep
- Trial Reels: von bewährtem Rohmaterial, genau EINE Variable ändern, max. 4/Account/Tag
- Ziel: 2–3 editierte Reels + 2–4 Trial Reels pro Account/Tag
- **Ergänzung (Ahmad, 2026-08-06):** nicht erst auf ein volles KEEP warten — auch ein CAUTION-Video mit starker US-Audience (Ahmads Beispiel: >25%) ist schon einen Trial-Reel-Test wert.

## Wichtigster Fund (bestätigt über alle 3 Accounts)

Debatten-/Meinungs-Hook-Videos (Beziehungs-/Gender-Dynamik, echte Diskussion in Kommentaren) schlagen alles andere klar:
- Debatten-Hook: Ø ~40% US-Audience
- Generischer Wortwitz (kontextfrei): Ø ~14% US-Audience trotz oft mehr Views ("False Dopamine")
- Reine Outfit-Transitions mit US-Signalen: Ø ~26%, solide Mitte

Cowgirl: bester Post (Debatten-Hook "if we girls are always right then why do we always pick the wrong guy") = 40.700 Views, reine Outfit-Posts nur 43–500 Views. Cosplay: Unterschied kleiner, gute Charakter-Reveals laufen schon solide (5.000–6.300 Views). Nächster Test: Debatten-Hook AUF einen Charakter-Reveal legen — noch nicht kombiniert probiert.

## Weitere aktuell starke Muster (Ahmad, 2026-08-06 — genaue Zahlen folgen noch, Pattern schon bestätigt)

- **Outfit-Transitions performen aktuell ebenfalls extrem stark** bei unseren eigenen Accounts — nicht mehr nur "solide Mitte" wie oben, sondern klar stark. Vorbild: wie gothgirlenya (getrackter Konkurrent) das macht — nachschauen/nachbauen. Also: Debatten-Hooks UND Outfit-Transitions sind beides aktuell starke Formate, nicht nur eines bevorzugen.
- **Comedy-Content mit Luna "auf der Bühne"** funktioniert gut — WICHTIG dabei: sie muss ein auffaelliges, starkes Outfit tragen (z.B. richtig heisses Goth-Outfit, Daemon-Outfit) — das Outfit ist Teil davon warum es funktioniert, nicht nebensaechlich.
- **Cowgirl: Country-Outfit** funktioniert nach demselben Muster aehnlich stark wie bei Goth.

## Datenquellen

| Was | Wo | Login? |
|---|---|---|
| Views pro Video | Instagram-Profil → Reels-Tab | Nein, öffentlich |
| Likes, Kommentare, Caption | Video einzeln öffnen | Nein, öffentlich |
| US-Audience-%, Reach, Views | Sheet-Tab **"Insights Eingang"** (Spalten: Link, US Audience %, Reach, Views, Status) | Ahmad braucht Insights-Zugriff, Jarvis nicht |
| Link-Klicks | https://slt.bio/dashboard/analytics | Ja, meist eingeloggt |

**Aktueller Insights-Workflow (seit 2026-08-07, `insights_inbox_pass` in background_brain.py):** Ahmad liest US-Audience-%/Reach/Views selbst von Instagram Insights ab und trägt sie zusammen mit dem Link DIREKT in den Sheet-Tab "Insights Eingang" ein (eine Zeile pro Video). Jarvis prüft diesen Tab auf eigenem, autonomem Takt und verarbeitet neue Zeilen automatisch — Ahmad muss nichts mehr sagen wie "ich hab's geschickt". Ersetzt vollständig den alten WhatsApp-Screenshot-Workflow.

**Veralteter Workflow (bis 2026-08-06, NICHT MEHR VERWENDEN):** Ahmad schickte Link + Insights-Screenshots in den WhatsApp-Chat mit sich selbst, Jarvis las die Zahlen per Vision aus dem Screenshot (`READINSIGHTS`/`_read_insights_from_self_chat`). Wurde ersetzt, weil das Auslesen von WhatsApp-Link-Vorschaukarten und Screenshots unzuverlässig war (Ahmad, 2026-08-07). Falls Jarvis diesen Workflow noch einmal nennt, ist das ein Zeichen für veraltetes Gedächtnis — korrigieren, nicht wiederholen.

Fehlende Zahlen: zuerst im Sheet nachschauen, sonst Ahmad fragen — nicht raten.

## Insights-Timing

Trial Reels: exakt 3h nach Posten. Hauptfeed-Posts: ~3h (früher Indikator) UND ~24h (zählt für Winner-Tracking-Entscheidung).

## Jerome-Kommunikation (WhatsApp)

- **Update ggü. altem Stand: Jarvis darf an alle WhatsApp-Chats SENDEN, nicht nur an Jeromes** — die frühere Jerome-only-Einschränkung fürs Senden ist aufgehoben (Ahmads Entscheidung). **Aber:** eigenstständiges/im-Hintergrund-LESEN fremder Chats bleibt weiterhin NUR auf Jerome (geschäftlich) und den eigenen Insights-Selbst-Chat beschränkt — private Chats (Familie, Freunde, Partnerin) werden nie von selbst durchsucht (Sicherheitsregel in `server.py`, unverändert).
- Nachrichten an Jerome signiert der Code automatisch mit "— Jarvis (Ahmad's AI assistant)" (`jerome_comm.py`).
- Sehr detailliert und explizit sein — Jerome braucht Dinge ausformuliert.
- **Korrigiert (2026-08-11, war zu pauschal formuliert):** `content_brief_pass` schickt Jerome tatsächlich EINMAL TÄGLICH automatisch einen Content-Vorschlag mit konkreten Video-Links (`background_brain.py`, ab 9 Uhr, nur falls wirklich etwas Brauchbares gefunden wurde) — kein reiner "Status-Report", aber sehr wohl ein täglicher automatischer Versand. Zusätzlich gilt weiter: kein separater taeglicher Status-Report ohne konkreten Inhalt.
- (Die frühere Notiz zu WhatsApp-Tippproblemen ist überholt — lief bei Jarvis bisher einwandfrei, öffnen und Senden funktionieren zuverlässig.)
## Feste Grenzen (unveraendert wichtig — gilt fuer Jarvis genauso)

**Wichtige Klarstellung (2026-08-11):** diese Grenzen sind rein auf Prompt-/Gedaechtnis-Ebene durchgesetzt, NICHT technisch im Code blockiert. `screen_click`/`screen_type` pruefen nicht, ob das Ziel einer der 5 echten Accounts ist — nichts im Code wuerde einen Klick/eine Eingabe dort technisch verhindern. Der Schutz besteht ausschliesslich darin, dass Jarvis diese Regeln befolgt.

1. Nie Smartphones/virtuelle Geräte (z.B. GeeLark) konfigurieren, einrichten oder verwalten — bleibt immer bei Ahmad, auch für ein künftiges Research-Gerät (siehe unten). Jarvis richtet nichts selbst ein.
2. Auf den ECHTEN Posting-Accounts (lunaxvale, cowgirllunavale, lunavalethegoth, lunas.crypt, succubuslunavale — ALLE fuenf, siehe Tabelle oben, diese Liste MUSS bei jedem neuen/entfernten Account mitgepflegt werden) nie selbst tippen, wischen oder navigieren — nur Screenshots lesen. Grund: automatisierte Eingabemuster auf echten Accounts sind ein Bann-Risiko bei Instagram.
   - **Ausnahme (Zukunft, siehe unten):** auf einem separaten, von Ahmad eingerichteten GeeLark-Research-Gerät mit einem reinen Content-Research-Account (kein wichtiger/echter Posting-Account) darf Jarvis selbst tippen und scrollen.
3. Nie Zugangsdaten/Passwörter für Instagram oder andere Dienste eingeben oder verwalten.
   - **Präzisierung (2026-08-11, nach mehreren Facebook-Sicherheitswarnungen bei Ahmad):** bei Verdacht auf einen gehackten Account ist Jarvis nicht mehr komplett hilflos — das Werkzeug `facebook_secure` in `server.py` prüft die Warnmails anhand von Googles SPF/DKIM/DMARC-Headern auf Echtheit (echte Meta-Mail vs. Phishing) und öffnet auf Ansage die offiziellen Meta-Seiten (aktive Anmeldungen/Geräte, Passwort ändern, 2FA, notfalls facebook.com/hacked) im eingeloggten Browser auf Ahmads Mac, mit selbst getippten URLs statt eines Links aus der verdächtigen Mail. Regel 3 gilt dabei unverändert: Jarvis loggt sich nicht ein, tippt kein Passwort und keinen 2FA-Code, meldet selbst kein Gerät ab und sieht den Kontostand nicht — das Abarbeiten der geöffneten Seiten bleibt bei Ahmad. Das Öffnen der Seiten braucht beim ersten Mal seine ausdrückliche Bestätigung.
4. Nie bei Aufbau von Multi-Account-Ban-Evasion-Infrastruktur helfen (Proxies, SIM/Geräte-Warmup, Ban-Recovery, Detection-Evasion-Link-Anbieter) — nur Analyse und Content-Strategie.
   - **Praezisierung (2026-08-12):** eine reine, read-only Uebersicht ueber vorhandene Proxies (Status, Konto-Label) ist erlaubt ("Analyse") — das tatsaechliche Routing von Account-Traffic ueber einen fest zugeordneten Proxy ist es nicht, auch nicht wenn es als "Sicherheit"/"kein Bann-Risiko" statt als "Umgehung" formuliert wird. Gleicher Mechanismus, andere Worte.
5. Rollentrennung einhalten: Ahmad postet persönlich, Jerome erstellt, Jarvis analysiert/koordiniert.

**Zukunftsidee (noch nicht umgesetzt, kein Geraet vorhanden):** Ahmad ueberlegt, selbst ein separates GeeLark-Research-Geraet mit einem reinen Content-Research-Account einzurichten (Einrichtung bleibt bei ihm, Regel 1 gilt weiter — Jarvis richtet das Geraet nicht selbst ein). Sobald das Geraet existiert, darf Jarvis dort selbst aktiv werden: For-You-Feed durchscrollen, tippen/navigieren, anhand von Metriken gute/inspirierende Videos erkennen und in die Target-Creator-Liste aufnehmen. Das gilt NUR fuer dieses eine dedizierte Research-Geraet/-Konto, niemals fuer die echten Posting-Accounts (Regel 2 oben gilt fuer die weiterhin uneingeschraenkt).

## Referenz-Muster

- Wiederverwendeter Hook auf Cowgirl: "that's not gonna fit… it did" (Trial-Wave, eine Variable geändert)
- Bester Cowgirl-Post: Beziehungs-Debatten-Interview, 40.700 Views
- Beste Cosplay-Posts: "Scarlet Cosplay" Charakter-Reveal (6.337 Views), lila Rüstung (5.769 Views)
- Persona-Stimmen: Goth = trocken/geheimnisvoll/kurz; Cowgirl = warm/Südstaaten-Slang/verspielt; Cosplay = nerdy-selbstbewusst/Fandom-Humor
