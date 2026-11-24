"""Offline-Test des Papertraders mit simulierter Alpaca-API (kein Netz nötig).
Aufruf: python3 test_trader.py
"""
import datetime as dt
import json
import shutil
import tempfile
import uuid
from pathlib import Path

import trader

TMP = Path(tempfile.mkdtemp())
trader.OUT = TMP / "out"
trader.STATE_DIR = TMP / "state"


def bar(t, o, h, l, c, v):
    return {"t": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": o, "h": h, "l": l, "c": c, "v": v}


class FakeApi:
    """Füllt Market-Orders sofort zum letzten Schlusskurs."""

    def __init__(self, market):
        self.market = market          # gemeinsames Dict symbol -> bars
        self.pos = {}                 # psym -> {qty, avg}
        self.orders = []

    def price(self, psym):
        for s, b in self.market["bars"].items():
            if trader.pos_symbol(s) == psym:
                return b[-1]["c"]
        raise KeyError(psym)

    def account(self):
        return {"account_number": "PA123", "cash": "100000", "crypto_status": "ACTIVE"}

    def clock(self):
        return self.market["clock"]

    def positions(self):
        out = []
        for s, p in self.pos.items():
            px = self.price(s)
            out.append({"symbol": s, "qty": str(p["qty"]), "avg_entry_price": str(p["avg"]),
                        "current_price": str(px), "market_value": str(px * p["qty"]),
                        "unrealized_plpc": str(px / p["avg"] - 1),
                        "asset_class": "crypto" if s.endswith("USD") else "us_equity"})
        return out

    def open_orders(self):
        return []

    def filled_orders_after(self, after):
        return [o for o in self.orders if o["filled_at"] > after]

    def _fill(self, sym, side, qty, px):
        o = {"id": str(uuid.uuid4()), "symbol": trader.pos_symbol(sym), "side": side,
             "filled_qty": str(qty), "filled_avg_price": str(px),
             "filled_at": self.market["now"].strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
        self.orders.append(o)
        return o

    def buy_notional(self, sym, usd):
        px = self.market["bars"][sym][-1]["c"]
        qty = usd / px
        ps = trader.pos_symbol(sym)
        self.pos[ps] = {"qty": qty, "avg": px}
        return self._fill(sym, "buy", qty, px)

    def close(self, psym):
        p = self.pos.pop(trader.pos_symbol(psym))
        return self._fill(psym, "sell", p["qty"], self.price(trader.pos_symbol(psym)))

    def bars(self, symbols, crypto, tf, start):
        return {s: self.market["bars"][s] for s in symbols if s in self.market["bars"]}


def make_series(start, n, price, step=0.0, vol=1000):
    out = []
    t = start
    for i in range(n):
        p = price + step * i
        out.append(bar(t, p, p * 1.001, p * 0.999, p, vol))
        t += dt.timedelta(minutes=5)
    return out


def check(cond, msg):
    print(("OK   " if cond else "FEHLER ") + msg)
    if not cond:
        raise SystemExit(1)


def main():
    trader.CFG["aktien"] = ["TSLA"]
    trader.CFG["krypto"] = ["BTC/USD", "SOL/USD"]
    # Dienstag 24.11.2026, 14:30 New York = 19:30 UTC
    now = dt.datetime(2026, 11, 24, 19, 30, tzinfo=dt.timezone.utc)
    open_ny = dt.datetime(2026, 11, 24, 9, 30, tzinfo=trader.NY).astimezone(dt.timezone.utc)

    # TSLA: Vortag + heute ruhig, letzter Balken Ausbruch mit Volumen
    tsla = make_series(open_ny - dt.timedelta(hours=10), 60, 300.0)
    tsla += make_series(open_ny, 60, 300.0)
    tsla[-1] = bar(parse(tsla[-1]["t"]), 300, 306, 300, 305.0, 5000)
    # BTC: Ausbruch über 1-h-Hoch, über EMA50
    btc = make_series(now - dt.timedelta(minutes=5 * 119), 119, 90000.0, step=5)
    btc.append(bar(now, btc[-1]["c"], 92000, btc[-1]["c"], 91500.0, 6000))
    # SOL: Rücksetzer – lange Aufwärtsphase, dann kurzer Abverkauf (RSI tief, Kurs nahe Trend)
    sol = make_series(now - dt.timedelta(minutes=5 * 120), 105, 200.0, step=0.2)
    p = sol[-1]["c"]
    for i in range(15):
        p -= 0.35
        sol.append(bar(now - dt.timedelta(minutes=5 * (14 - i)), p, p, p, p, 1000))

    market = {"now": now, "bars": {"TSLA": tsla, "BTC/USD": btc, "SOL/USD": sol},
              "clock": {"is_open": True, "next_close": "2026-11-24T21:00:00Z", "next_open": "2026-11-25T14:30:00Z"}}
    apis = {"momentum": FakeApi(market), "ruecksetzer": FakeApi(market)}

    # Probelauf ohne Orders
    rep = trader.tick(apis, now, dry=True)
    check(any(a[1] == "TSLA" for a in rep["momentum"]["actions"]), "Momentum erkennt TSLA-Ausbruch (Probe)")
    check(not apis["momentum"].pos, "Probelauf ohne Orders platziert nichts")

    # Echter Durchlauf (gegen Fake-API)
    rep = trader.tick(apis, now)
    m = apis["momentum"]
    check("TSLA" in m.pos and "BTCUSD" in m.pos, "Momentum kauft TSLA und BTC")
    check("SOLUSD" in apis["ruecksetzer"].pos, "Rücksetzer kauft SOL")
    check(all(abs(p["qty"] * p["avg"] - 50 * 1.11) < 0.01 for p in m.pos.values()), "Einsatz je Trade = 50 € in USD")

    # 5 Minuten später: TSLA fällt 3 % -> Stop-Loss; Fills buchen
    now2 = now + dt.timedelta(minutes=5)
    market["now"] = now2
    tsla.append(bar(now2, 305, 305, 295, 295.8, 3000))
    trader.tick(apis, now2)
    check("TSLA" not in m.pos, "Stop-Loss verkauft TSLA bei -3 %")
    st = json.loads((trader.STATE_DIR / "momentum.json").read_text())
    rows = trader.read_journal()
    sell = [r for r in rows if r["symbol"] == "TSLA" and r["seite"] == "Verkauf"]
    check(len(sell) == 1 and float(sell[0]["ergebnis_eur"]) < -2, f"Journal bucht Verlust inkl. Gebühren ({sell[0]['ergebnis_eur']} €)")
    check(abs(st["gebuehren_eur"] - 3.0) < 1e-9, "3 Orders = 3 € simulierte Gebühren")

    # Kurz vor Börsenschluss: keine neuen Aktien-Einstiege, offene Aktien schließen
    now3 = dt.datetime(2026, 11, 24, 20, 55, tzinfo=dt.timezone.utc)
    market["now"] = now3
    tsla.append(bar(now3, 296, 310, 296, 309, 9000))
    rep = trader.tick(apis, now3)
    check("TSLA" not in m.pos, "5 Min vor Schluss kein neuer Aktienkauf")

    # Krypto-Haltedauer
    now4 = now + dt.timedelta(hours=9)
    market["now"] = now4
    market["clock"] = {"is_open": False, "next_close": "2026-11-25T21:00:00Z", "next_open": "2026-11-25T14:30:00Z"}
    trader.tick(apis, now4)
    check("BTCUSD" not in m.pos, "BTC nach 8 h Haltedauer verkauft, kein sofortiger Wiederkauf")

    # Tagesverlust-Limit
    st = json.loads((trader.STATE_DIR / "ruecksetzer.json").read_text())
    st["day_start_equity"] = st["cash_eur"] + 200
    st["day"] = now4.astimezone(trader.BERLIN).date().isoformat()
    (trader.STATE_DIR / "ruecksetzer.json").write_text(json.dumps(st))
    rep = trader.tick(apis, now4)
    check(rep["ruecksetzer"]["blocked"] and "Tagesverlust" in rep["ruecksetzer"]["blocked"], "Tagesverlust-Limit sperrt neue Einstiege")

    status = json.loads((trader.OUT / "status.json").read_text())
    check(set(status["strategien"]) == {"momentum", "ruecksetzer"}, "status.json für beide Strategien")
    check((trader.OUT / "probehandel.md").exists(), "Bericht probehandel.md geschrieben")

    # Sicherheit: Live-URL wird abgelehnt
    try:
        trader.Api("k", "s")._req("GET", "https://api.alpaca.markets/v2/account")
        check(False, "Live-API müsste blockiert sein")
    except RuntimeError as e:
        check("Sicherheitsstopp" in str(e), "Live-API wird blockiert")
    print("\n" + (trader.OUT / "probehandel.md").read_text())
    shutil.rmtree(TMP)


def parse(s):
    return trader.parse_ts(s)


if __name__ == "__main__":
    main()
