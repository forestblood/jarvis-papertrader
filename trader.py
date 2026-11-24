#!/usr/bin/env python3
"""Jarvis Papertrader – Probehandel (Paper Trading) über die Alpaca-Paper-API.

Kein echtes Geld: Das Programm spricht ausschließlich mit paper-api.alpaca.markets
und bricht ab, sobald ein Konto nicht als Paper-Konto erkennbar ist.

Aufruf (im Ordner tools/trading):
  python3 trader.py            einen Durchlauf ausführen (Signale prüfen, handeln, Bericht schreiben)
  python3 trader.py --check    nur Verbindung und Konten prüfen
  python3 trader.py --dry-run  Signale berechnen und anzeigen, aber nichts handeln
  python3 trader.py --report   nur Berichte neu schreiben
  python3 trader.py --loop     alle 5 Minuten einen Durchlauf, bis Ctrl+C
                               (Tipp: caffeinate -i python3 trader.py --loop hält den Mac wach)

Benötigt nur Python 3.9+ (keine Zusatzpakete). Schlüssel kommen aus Umgebungsvariablen
(GitHub-Secrets) oder lokal aus .env im selben Ordner.
"""
import csv
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

PAPER_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
NY = ZoneInfo("America/New_York")
BERLIN = ZoneInfo("Europe/Berlin")

BASE = Path(__file__).resolve().parent
OUT = BASE / "berichte"          # Berichte (status.json, journal.csv, probehandel.md)
STATE_DIR = BASE / "state"       # Zustand der virtuellen Konten und Protokoll


# ---------------------------------------------------------------- Hilfen
def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def parse_ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def log(msg):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{now_utc().astimezone(BERLIN):%Y-%m-%d %H:%M:%S} {msg}"
    print(line)
    p = STATE_DIR / "log.txt"
    lines = p.read_text().splitlines() if p.exists() else []
    lines = (lines + [line])[-1500:]
    p.write_text("\n".join(lines) + "\n")


def load_env():
    env = {}
    p = BASE / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if k.startswith("ALPACA_")})
    return env


def is_crypto(symbol):
    return "/" in symbol or symbol.endswith("USD") and len(symbol) > 5


def pos_symbol(symbol):
    """Alpaca führt Krypto-Positionen ohne Schrägstrich (BTC/USD -> BTCUSD)."""
    return symbol.replace("/", "")


# ---------------------------------------------------------------- API
class Api:
    def __init__(self, key, secret):
        self.h = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                  "Content-Type": "application/json"}

    def _req(self, method, url, body=None):
        if not (url.startswith(PAPER_URL) or url.startswith(DATA_URL)):
            raise RuntimeError("Sicherheitsstopp: nur Paper- und Daten-API erlaubt")
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url, data=data, headers=self.h, method=method)
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                txt = resp.read().decode()
                return json.loads(txt) if txt else None
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {url.split('?')[0]} -> {e.code}: {e.read().decode()[:300]}")

    def get(self, path, params=None, data=False):
        url = (DATA_URL if data else PAPER_URL) + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return self._req("GET", url)

    def account(self):
        return self.get("/v2/account")

    def clock(self):
        return self.get("/v2/clock")

    def positions(self):
        return self.get("/v2/positions") or []

    def open_orders(self):
        return self.get("/v2/orders", {"status": "open", "limit": 500}) or []

    def filled_orders_after(self, after_iso):
        res = self.get("/v2/orders", {"status": "closed", "after": after_iso,
                                       "direction": "asc", "limit": 500}) or []
        return [o for o in res if o.get("filled_at") and float(o.get("filled_qty") or 0) > 0]

    def buy_notional(self, symbol, usd):
        return self._req("POST", PAPER_URL + "/v2/orders", {
            "symbol": symbol, "notional": f"{usd:.2f}", "side": "buy", "type": "market",
            "time_in_force": "gtc" if is_crypto(symbol) else "day"})

    def close(self, symbol):
        return self._req("DELETE", PAPER_URL + "/v2/positions/" + urllib.parse.quote(pos_symbol(symbol), safe=""))

    def bars(self, symbols, crypto, timeframe, start_iso):
        path = "/v1beta3/crypto/us/bars" if crypto else "/v2/stocks/bars"
        params = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": start_iso, "limit": 10000}
        if not crypto:
            params.update({"feed": "iex", "adjustment": "raw"})
        out, token = {}, None
        for _ in range(20):
            p = dict(params)
            if token:
                p["page_token"] = token
            r = self.get(path, p, data=True) or {}
            for s, b in (r.get("bars") or {}).items():
                out.setdefault(s, []).extend(b)
            token = r.get("next_page_token")
            if not token:
                break
        return out


# ---------------------------------------------------------------- Indikatoren
def ema(vals, n):
    if len(vals) < n:
        return None
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    g = l = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        g += max(d, 0)
        l += max(-d, 0)
    ag, al = g / n, l / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def vwap(bars):
    v = sum(b["v"] for b in bars)
    return sum(b["c"] * b["v"] for b in bars) / v if v else None


def avg(vals):
    return sum(vals) / len(vals) if vals else 0


def session_bars(bars, ref_utc):
    """Balken der heutigen regulären US-Sitzung (ab 9:30 New York)."""
    d = ref_utc.astimezone(NY).date()
    open_ny = dt.datetime(d.year, d.month, d.day, 9, 30, tzinfo=NY)
    return [b for b in bars if parse_ts(b["t"]) >= open_ny]


# ---------------------------------------------------------------- Signale
def signal_momentum(sym, bars, crypto, sc, ref_utc):
    if len(bars) < 25:
        return None
    last, prev = bars[-1], bars[-2]
    vol_avg = avg([b["v"] for b in bars[-21:-1]])
    vol_ok = vol_avg > 0 and last["v"] > sc["volumen_faktor"] * vol_avg
    if crypto:
        n = sc["krypto_ausbruch_bars"]
        hi = max(b["h"] for b in bars[-n - 1:-1])
        e50 = ema([b["c"] for b in bars], 50)
        if last["c"] > hi and prev["c"] <= hi and vol_ok and e50 and last["c"] > e50:
            return f"Ausbruch über {n * 5}-Min-Hoch {hi:.2f}, Volumen {last['v'] / vol_avg:.1f}x"
        return None
    today = session_bars(bars, ref_utc)
    k = sc["opening_range_bars"]
    if len(today) < k + 1:
        return None
    or_high = max(b["h"] for b in today[:k])
    vw = vwap(today)
    if last["c"] > or_high and prev["c"] <= or_high and vol_ok and vw and last["c"] > vw:
        return f"Ausbruch über Eröffnungsspanne {or_high:.2f}, über VWAP, Volumen {last['v'] / vol_avg:.1f}x"
    return None


def signal_ruecksetzer(sym, bars, crypto, sc, ref_utc):
    closes = [b["c"] for b in bars]
    if len(closes) < sc["trend_ema"] + 1:
        return None
    if not crypto and len(session_bars(bars, ref_utc)) < 3:
        return None
    r = rsi(closes)
    trend = ema(closes, sc["trend_ema"])
    last = closes[-1]
    floor = trend * (1 - sc["max_abstand_unter_trend_prozent"] / 100)
    if r is not None and r < sc["rsi_kaufen_unter"] and last >= floor:
        return f"RSI {r:.0f} (überverkauft), Kurs nahe Trend-EMA{sc['trend_ema']}"
    return None


SIGNALS = {"momentum": signal_momentum, "ruecksetzer": signal_ruecksetzer}
NAMES = {"momentum": "Momentum / Ausbruch", "ruecksetzer": "Rücksetzer kaufen"}


def exit_reason(name, sc, pos, bars, crypto, entry_time, ref_utc, minutes_to_close):
    entry = float(pos["avg_entry_price"])
    cur = float(pos["current_price"])
    pl = (cur - entry) / entry * 100
    if pl <= -sc["stop_loss_prozent"]:
        return f"Stop-Loss bei {pl:.1f} %"
    if pl >= sc["take_profit_prozent"]:
        return f"Gewinnziel bei {pl:.1f} %"
    if name == "ruecksetzer" and bars:
        r = rsi([b["c"] for b in bars])
        if r is not None and r > sc["rsi_verkaufen_ueber"]:
            return f"RSI {r:.0f} erholt ({pl:+.1f} %)"
    if crypto and entry_time:
        hours = (ref_utc - parse_ts(entry_time)).total_seconds() / 3600
        if hours >= sc["krypto_max_haltedauer_stunden"]:
            return f"Haltedauer {hours:.0f} h erreicht ({pl:+.1f} %)"
    if not crypto and minutes_to_close is not None and minutes_to_close <= CFG["aktien_alles_schliessen_minuten_vor_schluss"]:
        return f"Börsenschluss naht ({pl:+.1f} %)"
    return None


# ---------------------------------------------------------------- Zustand / Buchhaltung
def load_state(name, budget_eur):
    p = STATE_DIR / f"{name}.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"start_eur": budget_eur, "cash_eur": budget_eur, "gebuehren_eur": 0.0,
            "last_fill": (now_utc() - dt.timedelta(minutes=1)).isoformat(),
            "seen_orders": [], "entries": {}, "reasons": {}, "day": None, "day_start_equity": None,
            "lots": {}}


def save_state(name, st):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    st["seen_orders"] = st["seen_orders"][-1000:]
    (STATE_DIR / f"{name}.json").write_text(json.dumps(st, indent=1))


JOURNAL_FIELDS = ["zeit", "strategie", "symbol", "seite", "menge", "preis_usd", "betrag_eur",
                  "gebuehr_eur", "ergebnis_eur", "grund"]


def journal_append(row):
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / "journal.csv"
    new = not p.exists()
    with p.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def book_fills(name, api, st, fee):
    """Ausgeführte Orders seit dem letzten Lauf ins virtuelle Konto buchen."""
    fx = CFG["eur_usd"]
    after = (parse_ts(st["last_fill"]) - dt.timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for o in api.filled_orders_after(after):
        if o["id"] in st["seen_orders"]:
            continue
        st["seen_orders"].append(o["id"])
        qty = float(o["filled_qty"])
        px = float(o["filled_avg_price"])
        sym = pos_symbol(o["symbol"])   # einheitlich ohne Schrägstrich (ETH/USD -> ETHUSD)
        eur = qty * px / fx
        result = ""
        if o["side"] == "buy":
            st["cash_eur"] -= eur + fee
            lot = st["lots"].setdefault(sym, {"qty": 0.0, "cost_eur": 0.0})
            lot["qty"] += qty
            lot["cost_eur"] += eur + fee
            st["entries"].setdefault(sym, o["filled_at"])
        else:
            st["cash_eur"] += eur - fee
            lot = st["lots"].get(sym, {"qty": qty, "cost_eur": eur})
            share = min(1.0, qty / lot["qty"]) if lot["qty"] else 1.0
            cost = lot["cost_eur"] * share
            result = round(eur - fee - cost, 2)
            lot["qty"] -= qty
            lot["cost_eur"] -= cost
            if lot["qty"] <= 1e-9:
                st["lots"].pop(sym, None)
                st["entries"].pop(sym, None)
        st["gebuehren_eur"] += fee
        journal_append({"zeit": parse_ts(o["filled_at"]).astimezone(BERLIN).strftime("%Y-%m-%d %H:%M"),
                        "strategie": name, "symbol": sym, "seite": "Kauf" if o["side"] == "buy" else "Verkauf",
                        "menge": f"{qty:.6g}", "preis_usd": f"{px:.4f}", "betrag_eur": f"{eur:.2f}",
                        "gebuehr_eur": f"{fee:.2f}", "ergebnis_eur": result,
                        "grund": st["reasons"].pop(o["id"], "")})
        if o["filled_at"] > st["last_fill"]:
            st["last_fill"] = o["filled_at"]


def equity_eur(st, positions):
    return st["cash_eur"] + sum(float(p["market_value"]) for p in positions) / CFG["eur_usd"]


# ---------------------------------------------------------------- Ein Durchlauf
def run_strategy(name, sc, api, clock, bars_by_symbol, ref_utc, dry, report):
    fee = CFG["simulierte_gebuehr_eur_je_order"]
    st = load_state(name, sc["budget_eur"])
    book_fills(name, api, st, fee)
    positions = api.positions()
    held = {p["symbol"]: p for p in positions}
    open_syms = {o["symbol"] for o in api.open_orders()}

    eq = equity_eur(st, positions)
    today = ref_utc.astimezone(BERLIN).date().isoformat()
    if st["day"] != today:
        st["day"], st["day_start_equity"] = today, eq
    tagesergebnis = eq - st["day_start_equity"]

    market_open = bool(clock.get("is_open"))
    mtc = None
    if market_open:
        mtc = (parse_ts(clock["next_close"]) - ref_utc).total_seconds() / 60
    actions = []

    # Ausstiege
    for psym, pos in held.items():
        sym = next((s for s in bars_by_symbol if pos_symbol(s) == psym), psym)
        crypto = pos.get("asset_class") == "crypto" or is_crypto(sym)
        if not crypto and not market_open:
            continue
        reason = exit_reason(name, sc, pos, bars_by_symbol.get(sym, []), crypto,
                             st["entries"].get(psym), ref_utc, mtc)
        if reason and psym not in open_syms:
            actions.append(("Verkauf", sym, reason))
            if not dry:
                try:
                    o = api.close(psym)
                    if o and o.get("id"):
                        st["reasons"][o["id"]] = reason
                    st.setdefault("cooldown", {})[psym] = ref_utc.isoformat()
                except RuntimeError as e:
                    log(f"[{name}] Verkauf {sym} fehlgeschlagen: {e}")

    # Einstiege
    blocked = None
    if tagesergebnis <= -sc["tagesverlust_limit_eur"]:
        blocked = f"Tagesverlust-Limit erreicht ({tagesergebnis:.2f} €)"
    einsatz = sc["einsatz_eur_je_trade"]
    slots = sc["max_offene_positionen"] - len(held)
    for sym, bars in bars_by_symbol.items():
        if blocked or slots <= 0:
            break
        crypto = is_crypto(sym)
        if pos_symbol(sym) in held or sym in open_syms or pos_symbol(sym) in open_syms:
            continue
        cd = st.get("cooldown", {}).get(pos_symbol(sym))
        if cd and (ref_utc - parse_ts(cd)).total_seconds() < CFG["sperre_nach_verkauf_minuten"] * 60:
            continue
        if not crypto:
            if not market_open or (mtc is not None and mtc <= CFG["aktien_keine_neuen_einstiege_minuten_vor_schluss"]):
                continue
        reason = SIGNALS[name](sym, bars, crypto, sc, ref_utc)
        if not reason:
            continue
        if st["cash_eur"] < einsatz + 2 * fee:
            blocked = "Budget aufgebraucht"
            break
        actions.append(("Kauf", sym, reason))
        slots -= 1
        if not dry:
            try:
                o = api.buy_notional(sym, einsatz * CFG["eur_usd"])
                if o and o.get("id"):
                    st["reasons"][o["id"]] = reason
                    st["entries"][pos_symbol(sym)] = ref_utc.isoformat()
            except RuntimeError as e:
                log(f"[{name}] Kauf {sym} fehlgeschlagen: {e}")

    for a in actions:
        log(f"[{name}]{' (Probe ohne Order)' if dry else ''} {a[0]} {a[1]}: {a[2]}")
    log(f"[{name}] Durchlauf fertig: Wert {eq:.2f} € (Start {st['start_eur']:.0f} €), "
        f"{len(held)} offene Position(en), {len(actions)} Aktion(en)"
        f"{'' if market_open else ', US-Börse zu – nur Krypto'}")
    if dry:
        for sym, bars in bars_by_symbol.items():
            if not bars:
                continue
            closes = [b["c"] for b in bars]
            r = rsi(closes)
            print(f"   {sym:9} Kurs {closes[-1]:>12.4f}  RSI {r if r is None else round(r):>4}  "
                  f"{'Signal' if any(a[1] == sym and a[0] == 'Kauf' for a in actions) else 'kein Signal'}")
    if blocked:
        log(f"[{name}] keine neuen Einstiege: {blocked}")
    if not dry:
        book_fills(name, api, st, fee)
        positions = api.positions()
        eq = equity_eur(st, positions)
        save_state(name, st)
    report[name] = {"st": st, "positions": positions, "equity": eq, "tagesergebnis": tagesergebnis,
                    "blocked": blocked, "actions": actions}


def fetch_bars(api, ref_utc):
    start = (ref_utc - dt.timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = CFG["lookback_bars"]
    out = {}
    if CFG["aktien"]:
        for s, b in api.bars(CFG["aktien"], False, CFG["timeframe"], start).items():
            out[s] = b[-n:]
    if CFG["krypto"]:
        for s, b in api.bars(CFG["krypto"], True, CFG["timeframe"], start).items():
            out[s] = b[-n:]
    return out


def connect(env):
    apis = {}
    for name, sc in CFG["strategien"].items():
        if not sc.get("aktiv"):
            continue
        k = env.get(f"ALPACA_{sc['konto']}_KEY")
        s = env.get(f"ALPACA_{sc['konto']}_SECRET")
        if not k or not s:
            log(f"[{name}] keine Schlüssel in .env (ALPACA_{sc['konto']}_KEY/_SECRET) – übersprungen")
            continue
        api = Api(k, s)
        acct = api.account()
        if not str(acct.get("account_number", "")).startswith("PA"):
            raise SystemExit(f"Sicherheitsstopp: Konto {name} ist kein Paper-Konto.")
        apis[name] = api
    keys = [(env.get(f"ALPACA_{sc['konto']}_KEY")) for sc in CFG["strategien"].values()]
    if len([k for k in keys if k]) != len({k for k in keys if k}):
        log("Warnung: zwei Strategien nutzen dasselbe Konto – Positionen vermischen sich.")
    return apis


def tick(apis, ref_utc=None, dry=False):
    ref_utc = ref_utc or now_utc()
    if dry:
        log("Probelauf: Signale werden nur angezeigt, es wird nichts gehandelt.")
    if not apis:
        log("Keine Konten verbunden.")
        return {}
    any_api = next(iter(apis.values()))
    clock = any_api.clock()
    bars = fetch_bars(any_api, ref_utc)
    report = {}
    for name, api in apis.items():
        run_strategy(name, CFG["strategien"][name], api, clock, bars, ref_utc, dry, report)
    if not dry:
        write_reports(report, ref_utc)
    return report


# ---------------------------------------------------------------- Berichte
def read_journal():
    p = OUT / "journal.csv"
    if not p.exists():
        return []
    with p.open() as f:
        return list(csv.DictReader(f))


def write_reports(report, ref_utc):
    OUT.mkdir(parents=True, exist_ok=True)
    rows = read_journal()
    status = {"stand": ref_utc.astimezone(BERLIN).strftime("%Y-%m-%d %H:%M"),
              "hinweis": "Probehandel bei Alpaca (Paper). Kein echtes Geld. Gebühren wie Trade Republic simuliert (1 € je Order).",
              "strategien": {}}
    today = ref_utc.astimezone(BERLIN).date().isoformat()
    md = [f"# Probehandel – Stand {status['stand']}", "",
          "Simulation mit virtuellem Geld bei Alpaca (Paper Trading), kein echtes Geld. Jede Order kostet hier wie bei Trade Republic 1 €.", ""]
    for name, r in report.items():
        st = r["st"]
        closed = [x for x in rows if x["strategie"] == name and x["seite"] == "Verkauf" and x["ergebnis_eur"] != ""]
        results = [float(x["ergebnis_eur"]) for x in closed]
        wins = [x for x in results if x > 0]
        today_res = [float(x["ergebnis_eur"]) for x in closed if x["zeit"].startswith(today)]
        pos = [{"symbol": p["symbol"], "menge": float(p["qty"]), "seit": st["entries"].get(p["symbol"]),
                "einstand_usd": round(float(p["avg_entry_price"]), 4),
                "kurs_usd": round(float(p["current_price"]), 4),
                "wert_eur": round(float(p["market_value"]) / CFG["eur_usd"], 2),
                "ergebnis_prozent": round(float(p["unrealized_plpc"]) * 100, 2)} for p in r["positions"]]
        s = {"start_eur": st["start_eur"], "wert_eur": round(r["equity"], 2),
             "rendite_prozent": round((r["equity"] / st["start_eur"] - 1) * 100, 2),
             "bargeld_eur": round(st["cash_eur"], 2), "gebuehren_eur": round(st["gebuehren_eur"], 2),
             "trades_abgeschlossen": len(results),
             "gewinnquote_prozent": round(len(wins) / len(results) * 100, 1) if results else None,
             "ergebnis_heute_eur": round(sum(today_res), 2),
             "bester_trade_eur": max(results) if results else None,
             "schlechtester_trade_eur": min(results) if results else None,
             "positionen": pos, "gesperrt": r["blocked"], "name": NAMES.get(name, name),
             "regeln": {k: CFG["strategien"][name][k] for k in ("einsatz_eur_je_trade", "stop_loss_prozent", "take_profit_prozent", "max_offene_positionen", "tagesverlust_limit_eur")}}
        status["strategien"][name] = s
        md += [f"## {NAMES.get(name, name)}", "",
               f"| Startkapital | Wert jetzt | Rendite | Trades | Gewinnquote | Gebühren |",
               "|---|---|---|---|---|---|",
               f"| {s['start_eur']:.0f} € | {s['wert_eur']:.2f} € | {s['rendite_prozent']:+.2f} % | {s['trades_abgeschlossen']} | "
               f"{(str(s['gewinnquote_prozent']) + ' %') if s['gewinnquote_prozent'] is not None else '–'} | {s['gebuehren_eur']:.2f} € |", ""]
        if pos:
            md += ["Offene Positionen:", "", "| Symbol | Wert | Ergebnis |", "|---|---|---|"]
            md += [f"| {p['symbol']} | {p['wert_eur']:.2f} € | {p['ergebnis_prozent']:+.2f} % |" for p in pos]
            md.append("")
        if s["gesperrt"]:
            md += [f"Keine neuen Einstiege: {s['gesperrt']}", ""]
    last = rows[-15:][::-1]
    if last:
        md += ["## Letzte Ausführungen", "", "| Zeit | Strategie | Symbol | Seite | Betrag | Ergebnis | Grund |", "|---|---|---|---|---|---|---|"]
        md += [f"| {x['zeit']} | {x['strategie']} | {x['symbol']} | {x['seite']} | {x['betrag_eur']} € | "
               f"{(x['ergebnis_eur'] + ' €') if x['ergebnis_eur'] else ''} | {x['grund']} |" for x in last]
    (OUT / "status.json").write_text(json.dumps(status, indent=1, ensure_ascii=False))
    # Wertverlauf für das Diagramm: eine Zeile je Strategie und Durchlauf
    vp = OUT / "verlauf.csv"
    new = not vp.exists()
    with vp.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["zeit", "strategie", "wert_eur", "bargeld_eur", "positionen"])
        for name, s in status["strategien"].items():
            w.writerow([ref_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), name, s["wert_eur"], s["bargeld_eur"], len(s["positionen"])])
    (OUT / "probehandel.md").write_text("---\ntype: topic\ntags: [finanzen, trading]\n---\n\n" + "\n".join(md) + "\n")


# ---------------------------------------------------------------- Start
CFG = json.loads((BASE / "config.json").read_text())


def main(argv):
    env = load_env()
    if "--check" in argv:
        apis = connect(env)
        for name, api in apis.items():
            a = api.account()
            print(f"{name}: Paper-Konto {a['account_number']}, Barmittel {a['cash']} USD, Krypto {a.get('crypto_status')}")
        if apis:
            c = next(iter(apis.values())).clock()
            print(f"US-Börse offen: {c['is_open']} (nächste Öffnung {c['next_open']})")
            b = fetch_bars(next(iter(apis.values())), now_utc())
            print(f"Kursdaten: {len(b)} Symbole, z. B. " + ", ".join(f"{s}={v[-1]['c']}" for s, v in list(b.items())[:3] if v))
        return
    if "--report" in argv:
        apis = connect(env)
        tick(apis, dry=True)
        report = {}
        for name, api in apis.items():
            st = load_state(name, CFG["strategien"][name]["budget_eur"])
            pos = api.positions()
            eq = equity_eur(st, pos)
            report[name] = {"st": st, "positions": pos, "equity": eq, "tagesergebnis": 0, "blocked": None, "actions": []}
        write_reports(report, now_utc())
        return
    if "--loop" in argv:
        import time
        apis = connect(env)
        log("Dauerbetrieb gestartet (alle 5 Minuten, Ende mit Ctrl+C).")
        while True:
            try:
                tick(apis)
            except Exception as e:  # weiterlaufen, nur protokollieren
                log(f"Fehler im Durchlauf: {e}")
            t = time.time()
            time.sleep(300 - t % 300 + 20)   # kurz nach jedem vollen 5-Minuten-Takt
        return
    tick(connect(env), dry="--dry-run" in argv)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (SystemExit, KeyboardInterrupt):
        log("Beendet.")
        raise SystemExit(0)
    except Exception as e:  # nie still abstürzen
        log(f"Fehler: {e}")
        sys.exit(1)
