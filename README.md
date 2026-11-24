# Jarvis Papertrader

Ein Trading-Agent, der **nur mit virtuellem Geld** handelt (Alpaca Paper Trading). Zwei Daytrading-Strategien laufen parallel mit je 500 € Startkapital. Kein echtes Geld, keine Verbindung zu einem echten Depot.

GitHub Actions startet den Agenten alle 5 Minuten (`.github/workflows/trader.yml`). Nach jedem Durchlauf werden Zustand und Berichte ins Repo zurückgeschrieben.

## Ergebnisse

**Live-Übersicht:** https://forestblood.github.io/jarvis-papertrader/ (Kennzahlen, Wertverlauf, offene Positionen, alle Käufe und Verkäufe; Quellcode im Branch `gh-pages`)


- [`berichte/probehandel.md`](berichte/probehandel.md): Stand beider Strategien, offene Positionen, letzte Ausführungen
- [`berichte/journal.csv`](berichte/journal.csv): jede Ausführung mit Grund und Ergebnis
- [`berichte/status.json`](berichte/status.json): Kennzahlen
- [`berichte/verlauf.csv`](berichte/verlauf.csv): Depotwert je Strategie nach jedem Durchlauf
- [`state/log.txt`](state/log.txt): Protokoll jedes Durchlaufs

## Strategien

| | Momentum / Ausbruch | Rücksetzer kaufen |
|---|---|---|
| Idee | Kaufen, wenn ein Wert mit viel Volumen ausbricht | Kaufen, wenn ein Wert im Aufwärtstrend kurz überverkauft ist |
| Einstieg Aktie | Kurs über der Spanne der ersten 15 Min., über VWAP, Volumen ≥ 1,5× Schnitt | RSI(14) unter 30, Kurs höchstens 3 % unter der Trendlinie (EMA 100) |
| Einstieg Krypto | Kurs über dem 1-Stunden-Hoch, über EMA 50, Volumen ≥ 1,5× | wie Aktie |
| Ausstieg | Stop-Loss −2 %, Gewinnziel +4 % | Stop-Loss −2 %, Gewinnziel +2,5 % oder RSI über 55 |

Für beide gilt: 50 € je Trade, höchstens 3 Positionen gleichzeitig, ab 25 € Tagesverlust keine neuen Käufe. Aktien werden vor Börsenschluss verkauft, Krypto spätestens nach 8 Stunden. Nach einem Verkauf ist dasselbe Symbol 60 Minuten gesperrt. Jede Order kostet in der Buchhaltung 1 € (wie bei Trade Republic). Einstellungen: `config.json`.

## Sicherheit

- Der Code spricht nur mit `paper-api.alpaca.markets` und bricht ab, wenn ein Konto kein Paper-Konto ist.
- Die Schlüssel liegen als GitHub-Secrets (Settings → Secrets and variables → Actions) und stehen nie im Code.

## Lokal ausführen

`.env.example` als `.env` kopieren, Paper-Schlüssel eintragen, dann `python3 trader.py --check`, `python3 trader.py --dry-run` oder `python3 trader.py`. **Nie gleichzeitig lokal und über GitHub laufen lassen**, sonst handelt der Agent doppelt. Test ohne Netz: `python3 test_trader.py`.

Keine Anlageberatung.
