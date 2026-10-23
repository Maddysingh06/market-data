#!/usr/bin/env python3
"""
market_collector.py v2 -- bade venues ka public BTC data + algo/whale FOOTPRINT. Sirf padhta hai, koi order nahi.

DATA (har 60 sec, SQLite):
  Binance futures : funding, OI, top-trader L/S, retail L/S, taker buy/sell
  Bybit / OKX     : funding, OI, L/S ratio
  Hyperliquid     : funding, OI, premium
  Coinbase        : spot -> Coinbase premium (US paisa)
  Deribit         : options OI, put/call
  Delta India     : tera apna mark/funding/OI

FOOTPRINT (live trades se, andaza hai, saboot nahi):
  SLICE   : ek jaise size ke trade, barabar gap par, ek hi disha -> koi bada khiladi dheere-dheere ghus raha (TWAP jaisa)
  ABSORB  : bhaari bikri/kharid par bhi price nahi hila -> kisi ne bada order chhupa ke le liya (iceberg jaisa)
  WALLET  : Hyperliquid wallets ko behaviour se label: MM-BOT / TREND-BUY / TREND-SELL / SCALP-ARB
  BIAS    : upar sab + Coinbase premium + top-vs-retail milake ek score (weights meri guess, untested)

Chalane ke tareeke:
  python3 market_collector.py            # collector (snapshot + live footprint)
  python3 market_collector.py report     # abhi ka sab ek table me + footprint + bias
  python3 market_collector.py selftest   # nakli data par logic check
Zaroori: pip3 install requests websocket-client --break-system-packages
NOTE: sandbox me exchanges blocked the, isliye asli endpoints/fields yahan test nahi hue.
Pehle run me jo source "ERR" dikhaye wo mujhe batao.
"""
import os, sys, time, json, sqlite3, threading, collections, random
import requests

DB = os.getenv("DB", "market.db")
EVERY = 60
BIG_USD = 300_000
WHALE_MIN_USD = 1_000_000
TOP_N = 20
STATE_EVERY = 60      # wallet position poll (pehle 300)
SLOW_EVERY = 300      # labels/decay
BURST_USD, BURST_WIN, BURST_COOL = 500_000, 60, 180
HL = "https://api.hyperliquid.xyz/info"
# footprint parameters (meri guess, badal sakte ho)
SLICE_MIN_USD, SLICE_MIN_N, SLICE_CV, SLICE_SPAN, WIN_S = 5000, 8, 0.35, 60, 600
ABS_MULT, ABS_MIN_BTC, ABS_MOVE_ATR = 3.0, 15.0, 0.25
MIN_N = 30


def http(method, url, **kw):
    kw.setdefault("timeout", 8)
    r = requests.request(method, url, **kw)
    r.raise_for_status()
    return r.json()


def f(x):
    try:
        return float(x)
    except Exception:
        return None


# ------------- venue collectors -------------
def c_binance(H):
    B, S = "https://fapi.binance.com", "BTCUSDT"
    o = {}
    p = H("GET", B + "/fapi/v1/premiumIndex", params={"symbol": S})
    o["mark"], o["funding"] = f(p["markPrice"]), f(p["lastFundingRate"])
    o["oi_btc"] = f(H("GET", B + "/fapi/v1/openInterest", params={"symbol": S})["openInterest"])
    for k, path, fld in (("top_pos_ls", "topLongShortPositionRatio", "longShortRatio"),
                         ("top_acc_ls", "topLongShortAccountRatio", "longShortRatio"),
                         ("retail_ls", "globalLongShortAccountRatio", "longShortRatio"),
                         ("taker_bs", "takerlongshortRatio", "buySellRatio")):
        try:
            r = H("GET", B + "/futures/data/" + path, params={"symbol": S, "period": "5m", "limit": 1})
            o[k] = f(r[-1][fld])
        except Exception:
            pass
    return o


def c_bybit(H):
    B, S = "https://api.bybit.com", "BTCUSDT"
    t = H("GET", B + "/v5/market/tickers", params={"category": "linear", "symbol": S})["result"]["list"][0]
    o = {"mark": f(t["markPrice"]), "funding": f(t["fundingRate"]), "oi_btc": f(t["openInterest"])}
    try:
        r = H("GET", B + "/v5/market/account-ratio",
              params={"category": "linear", "symbol": S, "period": "5min", "limit": 1})["result"]["list"][0]
        o["retail_ls"] = f(r["buyRatio"]) / f(r["sellRatio"])
    except Exception:
        pass
    return o


def c_okx(H):
    B, I = "https://www.okx.com", "BTC-USDT-SWAP"
    o = {}
    o["oi_btc"] = f(H("GET", B + "/api/v5/public/open-interest",
                      params={"instType": "SWAP", "instId": I})["data"][0]["oiCcy"])
    o["funding"] = f(H("GET", B + "/api/v5/public/funding-rate", params={"instId": I})["data"][0]["fundingRate"])
    try:
        r = H("GET", B + "/api/v5/rubik/stat/contracts/long-short-account-ratio",
              params={"ccy": "BTC", "period": "5m"})["data"][0]
        o["retail_ls"] = f(r[1])
    except Exception:
        pass
    return o


def c_hyper(H):
    d = H("POST", HL, json={"type": "metaAndAssetCtxs"})
    names = [u["name"] for u in d[0]["universe"]]
    c = d[1][names.index("BTC")]
    return {"mark": f(c["markPx"]), "funding": f(c["funding"]), "oi_btc": f(c["openInterest"]),
            "premium": f(c.get("premium"))}


def c_coinbase(H):
    return {"price": f(H("GET", "https://api.exchange.coinbase.com/products/BTC-USD/ticker")["price"])}


def c_deribit(H):
    B = "https://www.deribit.com/api/v2/public/"
    o = {"index": f(H("GET", B + "get_index_price", params={"index_name": "btc_usd"})["result"]["index_price"])}
    rows = H("GET", B + "get_book_summary_by_currency", params={"currency": "BTC", "kind": "option"})["result"]
    call = sum(f(r["open_interest"]) or 0 for r in rows if r["instrument_name"].endswith("-C"))
    put = sum(f(r["open_interest"]) or 0 for r in rows if r["instrument_name"].endswith("-P"))
    o["opt_oi_btc"], o["put_call_oi"] = call + put, (put / call if call else None)
    return o


def c_delta(H):
    r = H("GET", "https://api.india.delta.exchange/v2/tickers/BTCUSD")["result"]
    return {"mark": f(r.get("mark_price")), "funding": f(r.get("funding_rate")), "oi": f(r.get("oi"))}


SRC = {"binance": c_binance, "bybit": c_bybit, "okx": c_okx, "hyperliquid": c_hyper,
       "coinbase": c_coinbase, "deribit": c_deribit, "delta": c_delta}


# ------------- DB -------------
def db():
    con = sqlite3.connect(DB, check_same_thread=False)
    con.execute("CREATE TABLE IF NOT EXISTS m(ts INT, src TEXT, k TEXT, v REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS w(ts INT, addr TEXT, szi REAL, entry REAL, liq REAL, upnl REAL, lev REAL, usd REAL, flow REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS e(ts INT, venue TEXT, kind TEXT, side TEXT, a REAL, b REAL, note TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS c(ts INT, addr TEXT, label TEXT, bought REAL, sold REAL, maker_n REAL, taker_n REAL, n REAL)")
    con.execute("CREATE INDEX IF NOT EXISTS im ON m(src,k,ts)")
    con.execute("CREATE INDEX IF NOT EXISTS ie ON e(ts)")
    return con


LOCK = threading.Lock()


def put(con, ts, src, k, v):
    if v is not None:
        with LOCK:
            con.execute("INSERT INTO m VALUES(?,?,?,?)", (int(ts), src, k, v))


def put_event(con, ts, venue, kind, side, a, b, note):
    with LOCK:
        con.execute("INSERT INTO e VALUES(?,?,?,?,?,?,?)", (int(ts), venue, kind, side, a, b, note))
    print("EVENT %s %s %s %s" % (venue, kind, side, note), flush=True)


# ------------- FOOTPRINT 1: trade tape (slicing + absorption) -------------
class Tape:
    def __init__(self, con, venue):
        self.con, self.venue = con, venue
        self.win = collections.deque()
        self.cur = None
        self.hist = collections.deque(maxlen=90)
        self.seen = {}
        self.lock = threading.Lock()

    def add(self, ts, px, sz, buy):
        with self.lock:
            m = int(ts) // 60
            if self.cur is None or m != self.cur["m"]:
                if self.cur:
                    self._close(self.cur)
                self.cur = dict(m=m, o=px, h=px, l=px, c=px, buy=0.0, sell=0.0)
            c = self.cur
            c["h"], c["l"], c["c"] = max(c["h"], px), min(c["l"], px), px
            c["buy" if buy else "sell"] += sz
            self.win.append((ts, sz, px, buy))
            while self.win and self.win[0][0] < ts - WIN_S:
                self.win.popleft()

    def _close(self, b):
        rng = b["h"] - b["l"]
        h = list(self.hist)
        if len(h) >= 30:
            atr = sum(x["rng"] for x in h[-30:]) / 30
            last = h[-60:]
            avg_s = sum(x["sell"] for x in last) / len(last)
            avg_b = sum(x["buy"] for x in last) / len(last)
            move = b["c"] - b["o"]
            ts = (b["m"] + 1) * 60
            if b["sell"] >= max(ABS_MIN_BTC, ABS_MULT * avg_s) and move > -ABS_MOVE_ATR * atr:
                put_event(self.con, ts, self.venue, "ABSORB", "BULL", b["sell"], move,
                          "bhaari bikri %.0f BTC par price nahi gira (neeche koi le raha)" % b["sell"])
            if b["buy"] >= max(ABS_MIN_BTC, ABS_MULT * avg_b) and move < ABS_MOVE_ATR * atr:
                put_event(self.con, ts, self.venue, "ABSORB", "BEAR", b["buy"], move,
                          "bhaari kharid %.0f BTC par price nahi chadha (upar koi bech raha)" % b["buy"])
        self.hist.append(dict(rng=rng, buy=b["buy"], sell=b["sell"]))

    def scan(self, now):
        with self.lock:
            rows = [r for r in self.win if r[1] * r[2] >= SLICE_MIN_USD]
        groups = collections.defaultdict(list)
        for ts, sz, px, buy in rows:
            groups[(round(sz, 4), buy)].append(ts)
        for (sz, buy), tss in groups.items():
            if len(tss) < SLICE_MIN_N:
                continue
            tss.sort()
            if tss[-1] - tss[0] < SLICE_SPAN:
                continue
            gaps = [b - a for a, b in zip(tss, tss[1:])]
            mean = sum(gaps) / len(gaps)
            if mean <= 0.2:
                continue
            sd = (sum((g - mean) ** 2 for g in gaps) / len(gaps)) ** 0.5
            if sd / mean > SLICE_CV:
                continue
            key = (sz, buy)
            if now - self.seen.get(key, 0) < WIN_S:
                continue
            self.seen[key] = now
            put_event(self.con, now, self.venue, "SLICE", "BULL" if buy else "BEAR", sz * len(tss), mean,
                      "%d trade x %.4f BTC har ~%.1fs (%s)" % (len(tss), sz, mean, "kharid" if buy else "bikri"))


# ------------- FOOTPRINT 2: Hyperliquid wallets -------------
def classify(s):
    tot = s["bought"] + s["sold"]
    n = s["tk"] + s["mk"]
    if n < MIN_N or tot <= 0:
        return None
    d = (s["bought"] - s["sold"]) / tot
    mkr = s["mk"] / n
    if mkr >= 0.7 and abs(d) < 0.25 and n >= 50:
        return "MM-BOT"
    if tot >= 2_000_000 and abs(d) >= 0.6 and mkr < 0.5:
        return "TREND-BUY" if d > 0 else "TREND-SELL"
    if tot >= 2_000_000 and abs(d) < 0.25 and mkr < 0.5:
        return "SCALP-ARB"
    return None


class Whales:
    def __init__(self, con, H=http, tape=None):
        self.con, self.H, self.tape = con, H, tape
        self.flow = collections.defaultdict(float)
        self.vol = collections.defaultdict(float)
        self.st = collections.defaultdict(lambda: dict(bought=0.0, sold=0.0, tk=0.0, mk=0.0))
        self.big = {"buy": 0.0, "sell": 0.0}
        self.recent = collections.defaultdict(collections.deque)
        self.alerted = {}
        self.pending = []
        self.last_slow = 0.0
        self.lock = threading.Lock()

    def on_msg(self, raw):
        m = json.loads(raw) if isinstance(raw, str) else raw
        if m.get("channel") != "trades":
            return
        for t in m["data"]:
            px, sz = float(t["px"]), float(t["sz"])
            usd = px * sz
            buy_agg = t["side"] == "B"
            users = t.get("users") or []
            ts = float(t.get("time", time.time() * 1000)) / 1000.0
            if self.tape:
                self.tape.add(ts, px, sz, buy_agg)
            with self.lock:
                if len(users) == 2:
                    buyer, seller = users[0], users[1]
                    taker = buyer if buy_agg else seller
                    self.flow[taker] += usd if buy_agg else -usd
                    self.vol[taker] += usd
                    dq = self.recent[taker]
                    dq.append((ts, usd if buy_agg else -usd))
                    while dq and dq[0][0] < ts - BURST_WIN:
                        dq.popleft()
                    net = sum(x for _, x in dq)
                    if abs(net) >= BURST_USD and ts - self.alerted.get(taker, 0) > BURST_COOL:
                        self.alerted[taker] = ts
                        self.pending.append((taker, net, ts))
                    self.st[buyer]["bought"] += usd
                    self.st[seller]["sold"] += usd
                    self.st[taker]["tk"] += 1
                    self.st[seller if buy_agg else buyer]["mk"] += 1
                if usd >= BIG_USD:
                    self.big["buy" if buy_agg else "sell"] += usd

    def flush_big(self, ts):
        with self.lock:
            b, s = self.big["buy"], self.big["sell"]
            self.big = {"buy": 0.0, "sell": 0.0}
        put(self.con, ts, "hyperliquid", "big_buy_usd", b)
        put(self.con, ts, "hyperliquid", "big_sell_usd", s)

    def classify_all(self, ts):
        tb = ts_ = mm = 0.0
        rows = []
        with self.lock:
            for a, s in list(self.st.items()):
                lab = classify(s)
                if lab:
                    rows.append((int(ts), a, lab, s["bought"], s["sold"], s["mk"], s["tk"], s["mk"] + s["tk"]))
                    net = s["bought"] - s["sold"]
                    if lab == "TREND-BUY":
                        tb += net
                    elif lab == "TREND-SELL":
                        ts_ += -net
                    elif lab == "MM-BOT":
                        mm += 1
                for k in s:
                    s[k] *= 0.92
                if s["bought"] + s["sold"] < 10_000:
                    del self.st[a]
        with LOCK:
            self.con.executemany("INSERT INTO c VALUES(?,?,?,?,?,?,?,?)", rows)
        put(self.con, ts, "hyperliquid", "trend_buy_usd", tb)
        put(self.con, ts, "hyperliquid", "trend_sell_usd", ts_)
        put(self.con, ts, "hyperliquid", "mm_bots", mm)

    def flush_bursts(self):
        with self.lock:
            items, self.pending = self.pending, []
        for addr, net, ts in items:
            side = "BULL" if net > 0 else "BEAR"
            put_event(self.con, ts, "hyperliquid", "BURST", side, abs(net), 0,
                      "wallet %s..%s ne 1 min me $%.2fM %s" % (addr[:6], addr[-4:], abs(net) / 1e6, "kharida" if net > 0 else "becha"))
            try:
                st = self.H("POST", HL, json={"type": "clearinghouseState", "user": addr})
                for ap in st.get("assetPositions", []):
                    p = ap["position"]
                    if p["coin"] == "BTC":
                        with LOCK:
                            self.con.execute("INSERT INTO w VALUES(?,?,?,?,?,?,?,?,?)",
                                             (int(ts), addr, f(p["szi"]), f(p["entryPx"]), f(p.get("liquidationPx")),
                                              f(p["unrealizedPnl"]), f((p.get("leverage") or {}).get("value")), f(p["positionValue"]), net))
            except Exception as e:
                print("burst poll err", e)

    def poll_state(self, ts):
        with self.lock:
            top = sorted(self.vol.items(), key=lambda kv: -kv[1])[:TOP_N]
            top = [(a, v) for a, v in top if v >= WHALE_MIN_USD]
            flows = dict(self.flow)
        longs = shorts = 0.0
        for a, v in top:
            try:
                st = self.H("POST", HL, json={"type": "clearinghouseState", "user": a})
                for ap in st.get("assetPositions", []):
                    p = ap["position"]
                    if p["coin"] != "BTC":
                        continue
                    szi, usd = f(p["szi"]), f(p["positionValue"])
                    liq = f(p.get("liquidationPx"))
                    lev = f((p.get("leverage") or {}).get("value"))
                    with LOCK:
                        self.con.execute("INSERT INTO w VALUES(?,?,?,?,?,?,?,?,?)",
                                         (int(ts), a, szi, f(p["entryPx"]), liq, f(p["unrealizedPnl"]), lev, usd, flows.get(a, 0)))
                    if szi > 0:
                        longs += usd
                    else:
                        shorts += usd
            except Exception as e:
                print("HL state err", a[:8], e)
        put(self.con, ts, "hyperliquid", "whale_long_usd", longs)
        put(self.con, ts, "hyperliquid", "whale_short_usd", shorts)
        put(self.con, ts, "hyperliquid", "whale_n", float(len(top)))
        if time.time() - self.last_slow < SLOW_EVERY:
            self.con.commit()
            return
        self.last_slow = time.time()
        self.classify_all(ts)
        with self.lock:
            for d in (self.flow, self.vol):
                for k in list(d):
                    d[k] *= 0.5
                    if abs(d[k]) < 50_000:
                        del d[k]
        self.con.commit()

    def ws_thread(self):
        import websocket
        while True:
            try:
                ws = websocket.WebSocketApp(
                    "wss://api.hyperliquid.xyz/ws",
                    on_open=lambda w: w.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": "BTC"}})),
                    on_message=lambda w, msg: self.on_msg(msg),
                    on_error=lambda w, e: print("HL ws err", e))
                ws.run_forever(ping_interval=25, ping_timeout=10)
            except Exception as e:
                print("HL ws crash", e)
            time.sleep(5)


def binance_tape(tape):
    s, last = requests.Session(), None
    while True:
        try:
            p = {"symbol": "BTCUSDT", "limit": 1000}
            if last:
                p["fromId"] = last + 1
            r = s.get("https://fapi.binance.com/fapi/v1/aggTrades", params=p, timeout=10)
            if r.status_code in (403, 451):
                print("Binance tape block (%d), band" % r.status_code)
                return
            r.raise_for_status()
            for t in r.json():
                tape.add(t["T"] / 1000.0, float(t["p"]), float(t["q"]), not t["m"])
                last = t["a"]
        except Exception as e:
            print("binance tape err", e)
            time.sleep(5)
        time.sleep(2)


# ------------- bias score -------------
def latest(con):
    rows = con.execute("SELECT src,k,v,MAX(ts) FROM m GROUP BY src,k ORDER BY src,k").fetchall()
    return {(s, k): v for s, k, v, _ in rows}


def clamp(x, a):
    return max(-a, min(a, x))


def bias_calc(con, now):
    L = latest(con)
    g = lambda s, k: L.get((s, k))
    sc, parts = 0.0, []
    tb, tsl = g("hyperliquid", "trend_buy_usd"), g("hyperliquid", "trend_sell_usd")
    if tb is not None and tsl is not None:
        x = clamp((tb - tsl) / 1e6, 3)
        sc += x
        parts.append("HL trend wallets %+.1f" % x)
    rows = con.execute("SELECT kind,side,COUNT(*) FROM e WHERE ts>? GROUP BY kind,side", (int(now) - 900,)).fetchall()
    cnt = {(k, s): n for k, s, n in rows}
    for kind, w in (("SLICE", 0.5), ("ABSORB", 1.0), ("BURST", 0.5)):
        x = clamp(w * (cnt.get((kind, "BULL"), 0) - cnt.get((kind, "BEAR"), 0)), 3)
        if x:
            sc += x
            parts.append("%s %+.1f" % (kind, x))
    cp = g("derived", "coinbase_premium_usd")
    if cp is not None and abs(cp) > 20:
        x = 0.5 if cp > 0 else -0.5
        sc += x
        parts.append("Coinbase premium %+.1f" % x)
    tp, rl = g("binance", "top_pos_ls"), g("binance", "retail_ls")
    if tp and rl and (tp > rl * 1.1 or tp < rl * 0.9):
        x = 0.5 if tp > rl else -0.5
        sc += x
        parts.append("top-vs-retail %+.1f" % x)
    label = "BULLISH" if sc >= 2 else "BEARISH" if sc <= -2 else "NEUTRAL"
    return sc, label, parts


# ------------- paisa flow synthesis (price + OI milake) -------------
OI_FIELD = {"binance": "oi_btc", "bybit": "oi_btc", "okx": "oi_btc", "delta": "oi"}
FLOW_WIN = 900   # 15 min


def flow_label(con, src, now):
    kf = OI_FIELD.get(src)
    if not kf:
        return None
    pr = con.execute("SELECT ts,v FROM m WHERE src=? AND k='mark' AND ts>? ORDER BY ts", (src, now - FLOW_WIN)).fetchall()
    oi = con.execute("SELECT ts,v FROM m WHERE src=? AND k=? AND ts>? ORDER BY ts", (src, kf, now - FLOW_WIN)).fetchall()
    if len(pr) < 2 or len(oi) < 2:
        return None
    dp = pr[-1][1] - pr[0][1]
    doi = oi[-1][1] - oi[0][1]
    doi_pct = doi / oi[0][1] * 100 if oi[0][1] else 0
    if abs(doi_pct) < 0.05:   # itna chhota OI change -> shor, kuch mat bolo
        return None
    if dp >= 0 and doi > 0:
        return ("GHUS", "long", dp, doi_pct)
    if dp < 0 and doi > 0:
        return ("GHUS", "short", dp, doi_pct)
    if dp >= 0 and doi < 0:
        return ("NIKAL", "short-covering", dp, doi_pct)
    return ("NIKAL", "long-unwind", dp, doi_pct)


def synthesize(con, now):
    labels = {}
    for src in ("binance", "bybit", "okx"):
        r = flow_label(con, src, now)
        if r:
            labels[src] = r
    if not labels:
        return "15 min me OI itna kam badla ki kuch kehna shor hoga. Chup rehna behtar hai."
    ghus = [s for s, r in labels.items() if r[0] == "GHUS"]
    nikal = [s for s, r in labels.items() if r[0] == "NIKAL"]
    parts = []
    for s, (flow, kind, dp, doi_pct) in labels.items():
        parts.append("%s: %s (%s, price %+.0f, OI %+.2f%%)" % (s, flow, kind, dp, doi_pct))
    detail = " | ".join(parts)
    if len(ghus) > len(nikal):
        head = "PAISA MARKET ME GHUS RAHA HAI (naya position ban raha hai, %d/%d venue)" % (len(ghus), len(labels))
    elif len(nikal) > len(ghus):
        head = "PAISA MARKET SE NIKAL RAHA HAI (log position band kar rahe hain, %d/%d venue)" % (len(nikal), len(labels))
    else:
        head = "VENUE AAPAS ME ULTE HAIN, saaf tasveer nahi (confused signal)"
    return head + "\n   " + detail + "\n   (ye heuristic hai, price-OI se seedha nikla, asli test nahi hua)"


# ------------- main loop / report -------------
def snapshot(con, H=http):
    ts = int(time.time())
    got = {}
    for name, fn in SRC.items():
        try:
            d = fn(H)
            got[name] = d
            for k, v in d.items():
                put(con, ts, name, k, v)
        except Exception as e:
            print("%s ERR %s" % (name, str(e)[:120]))
    try:
        put(con, ts, "derived", "coinbase_premium_usd", got["coinbase"]["price"] - got["binance"]["mark"])
    except Exception:
        pass
    con.commit()
    return ts


def report(con):
    L = latest(con)
    if not L:
        print("abhi data nahi. pehle collector chalao.")
        return
    print("%-12s %-22s %s" % ("SOURCE", "METRIC", "VALUE"))
    for (s, k), v in sorted(L.items()):
        print("%-12s %-22s %.6g" % (s, k, v))
    g = lambda s, k: L.get((s, k))
    print("\n--- BIAS (sirf ishara, signal nahi) ---")
    fund = [(s, g(s, "funding")) for s in ("binance", "bybit", "okx", "hyperliquid", "delta") if g(s, "funding") is not None]
    if fund:
        print("Funding:", ", ".join("%s %.4f%%" % (s, v * 100) for s, v in fund))
    cp = g("derived", "coinbase_premium_usd")
    if cp is not None:
        print("Coinbase premium: %+.1f USD -> %s" % (cp, "US khareed" if cp > 20 else "US bech" if cp < -20 else "neutral"))
    tp, rl = g("binance", "top_pos_ls"), g("binance", "retail_ls")
    if tp and rl:
        print("Top traders L/S %.2f vs retail %.2f -> %s" % (tp, rl,
              "bade LONG, retail kam long" if tp > rl * 1.1 else "bade SHORT-side, retail zyada long" if tp < rl * 0.9 else "dono ek jaise"))
    wl, ws_ = g("hyperliquid", "whale_long_usd"), g("hyperliquid", "whale_short_usd")
    if wl is not None and ws_ is not None and (wl + ws_) > 0:
        print("HL whales (BTC): long $%.1fM / short $%.1fM -> %s" % (wl / 1e6, ws_ / 1e6,
              "long bhaari" if wl > ws_ * 1.3 else "short bhaari" if ws_ > wl * 1.3 else "balanced"))
    pc = g("deribit", "put_call_oi")
    if pc:
        print("Deribit put/call OI: %.2f" % pc)
    now = time.time()
    sc, label, parts = bias_calc(con, now)
    print("\n=== FOOTPRINT BIAS: %s (score %+.1f, confidence LOW, weights untested) ===" % (label, sc))
    print("   " + (", ".join(parts) if parts else "koi footprint nahi mila"))
    print("\n--- PAISA FLOW (15 min, price+OI) ---")
    print(synthesize(con, now))
    ev = con.execute("SELECT ts,venue,kind,side,note FROM e WHERE ts>? ORDER BY ts DESC LIMIT 10", (int(now) - 1800,)).fetchall()
    if ev:
        print("\nPichhle 30 min ke events:")
        for ts, venue, kind, side, note in ev:
            print("  %s %-11s %-6s %-4s %s" % (time.strftime("%H:%M", time.localtime(ts)), venue, kind, side, note))
    cw = con.execute("SELECT addr,label,bought,sold FROM c WHERE ts=(SELECT MAX(ts) FROM c) ORDER BY (bought+sold) DESC LIMIT 8").fetchall()
    if cw:
        print("\nHyperliquid wallets (behaviour label, ~pichhla 1-2 ghanta):")
        for a, lab, b_, s_ in cw:
            print("  %s..%s %-10s bought $%.1fM sold $%.1fM" % (a[:6], a[-4:], lab, b_ / 1e6, s_ / 1e6))
    top = con.execute("SELECT addr,szi,entry,liq,usd FROM w WHERE ts=(SELECT MAX(ts) FROM w) ORDER BY usd DESC LIMIT 5").fetchall()
    if top:
        print("\nTop HL whale wallets (BTC position):")
        for a, szi, e, liq, usd in top:
            print("  %s..%s %s %.2f BTC entry %.0f liq %s ($%.1fM)" % (a[:6], a[-4:], "LONG" if szi > 0 else "SHORT",
                  abs(szi), e or 0, "%.0f" % liq if liq else "-", usd / 1e6))


def run():
    con = db()
    tape_hl, tape_bn = Tape(con, "hyperliquid"), Tape(con, "binance")
    wh = Whales(con, tape=tape_hl)
    for fn, args in ((wh.ws_thread, ()), (binance_tape, (tape_bn,))):
        try:
            threading.Thread(target=fn, args=args, daemon=True).start()
        except Exception as e:
            print("thread start fail", e)
    last_state = 0
    last_bias_alert = [0.0]
    print("collector chalu. DB=%s (Ctrl+C band)" % DB)
    if TG_TOKEN and TG_CHAT:
        tg_send("Collector chalu ho gaya, bhai. BURST/ABSORB events yahan aayenge.")
    else:
        print("TG_TOKEN/TG_CHAT set nahi, Telegram alerts OFF")
    while True:
        t0 = time.time()
        ts = snapshot(con)
        wh.flush_big(ts)
        wh.flush_bursts()
        tape_hl.scan(time.time())
        tape_bn.scan(time.time())
        if time.time() - last_state > STATE_EVERY:
            wh.poll_state(ts)
            last_state = time.time()
        sc, label, parts = bias_calc(con, time.time())
        put(con, ts, "footprint", "bias_score", sc)
        if label != "NEUTRAL" and (time.time() - last_bias_alert[0]) > 900:
            last_bias_alert[0] = time.time()
            tg_send("FOOTPRINT BIAS: %s (score %+.1f)\n%s\n(confidence LOW, weights untested)" % (label, sc, ", ".join(parts) if parts else "-"))
        con.commit()
        time.sleep(max(1, EVERY - (time.time() - t0)))


# ------------- selftest -------------
def selftest():
    global DB
    DB = ":memory:"

    def fake(method, url, **kw):
        if "premiumIndex" in url:
            return {"markPrice": "81500", "lastFundingRate": "0.0001"}
        if "fapi/v1/openInterest" in url:
            return {"openInterest": "85000"}
        if "futures/data" in url:
            return [{"longShortRatio": "1.3", "buySellRatio": "1.1"}]
        if "bybit" in url and "tickers" in url:
            return {"result": {"list": [{"markPrice": "81480", "fundingRate": "0.00008", "openInterest": "52000"}]}}
        if "account-ratio" in url:
            return {"result": {"list": [{"buyRatio": "0.55", "sellRatio": "0.45"}]}}
        if "open-interest" in url and "okx" in url:
            return {"data": [{"oiCcy": "30000"}]}
        if "funding-rate" in url:
            return {"data": [{"fundingRate": "0.00005"}]}
        if "long-short-account-ratio" in url:
            return {"data": [["1", "1.4"]]}
        if url == HL and kw["json"]["type"] == "metaAndAssetCtxs":
            return [{"universe": [{"name": "ETH"}, {"name": "BTC"}]},
                    [{"markPx": "3000", "funding": "0", "openInterest": "1"},
                     {"markPx": "81490", "funding": "0.0000125", "openInterest": "30000", "premium": "0.0002"}]]
        if url == HL and kw["json"]["type"] == "clearinghouseState":
            return {"assetPositions": [{"position": {"coin": "BTC", "szi": "12.5", "entryPx": "80000", "liquidationPx": "70000",
                                                      "unrealizedPnl": "5000", "positionValue": "1000000", "leverage": {"value": 10}}}]}
        if "coinbase" in url:
            return {"price": "81540"}
        if "get_index_price" in url:
            return {"result": {"index_price": 81500}}
        if "get_book_summary" in url:
            return {"result": [{"instrument_name": "BTC-1-C", "open_interest": 100}, {"instrument_name": "BTC-1-P", "open_interest": 80}]}
        if "delta.exchange" in url:
            return {"result": {"mark_price": "81495", "funding_rate": "0.0001", "oi": "999"}}
        raise RuntimeError("unmocked " + url)

    con = db()
    snapshot(con, fake)
    rnd = random.Random(3)
    t0 = 1_800_000_000.0

    # --- 1) shor wala tape: koi footprint nahi aana chahiye ---
    noise = Tape(con, "noise")
    px = 81000.0
    for i in range(60 * 60):
        px += rnd.gauss(0, 1.2)
        for _ in range(rnd.randint(1, 3)):
            noise.add(t0 + i + rnd.random(), px, round(rnd.lognormvariate(-3.0, 1.0), 5), rnd.random() < 0.5)
    noise.scan(t0 + 3600)
    n_noise = con.execute("SELECT COUNT(*) FROM e WHERE venue='noise'").fetchone()[0]
    print("1) shor tape par events: %d (kam hona chahiye)" % n_noise)

    # --- 2) slicing: 12 x 0.5 BTC kharid har 10s ---
    sl = Tape(con, "slicetest")
    px = 81000.0
    for i in range(300):
        px += rnd.gauss(0, 1.0)
        sl.add(t0 + i + rnd.random() * .5, px, round(rnd.lognormvariate(-3.0, 1.0), 5), rnd.random() < 0.5)
        if i % 10 == 0 and i >= 100:
            sl.add(t0 + i + 0.3, px, 0.5, True)
    sl.scan(t0 + 300)
    r = con.execute("SELECT kind,side,note FROM e WHERE venue='slicetest'").fetchall()
    print("2) slicing test:", r)
    assert any(k == "SLICE" and s == "BULL" for k, s, _ in r)

    # --- 3) absorption: 40 min normal, fir 1 min me 80 BTC bikri par price flat ---
    ab = Tape(con, "abstest")
    px = 81000.0
    for m in range(45):
        for j in range(20):
            px += rnd.gauss(0, 4)
            ab.add(t0 + m * 60 + j * 3, px, round(rnd.uniform(0.2, 1.0), 4), rnd.random() < 0.5)
    base = px
    for j in range(40):
        ab.add(t0 + 45 * 60 + j, base + rnd.gauss(0, 1), 2.0, False)
    ab.add(t0 + 46 * 60 + 1, base, 0.1, True)
    r = con.execute("SELECT kind,side,note FROM e WHERE venue='abstest'").fetchall()
    print("3) absorption test:", r)
    assert any(k == "ABSORB" and s == "BULL" for k, s, _ in r)

    # --- 4) wallets: MM-BOT aur TREND-BUY ---
    w = Whales(con, fake)
    for i in range(80):
        w.on_msg({"channel": "trades", "data": [
            {"coin": "BTC", "side": "A" if i % 2 == 0 else "B", "px": "81500", "sz": "0.5", "time": (t0 + i) * 1000,
             "users": ["0xMM", "0xX%d" % (i % 7)] if i % 2 == 0 else ["0xX%d" % (i % 7), "0xMM"]}]})
    for i in range(60):
        w.on_msg({"channel": "trades", "data": [
            {"coin": "BTC", "side": "B", "px": "81500", "sz": "1.0", "time": (t0 + i) * 1000, "users": ["0xTREND", "0xMAKER%d" % (i % 9)]}]})
    w.flush_big(int(t0))
    w.poll_state(int(t0) + 1)
    labs = {a: l for a, l in con.execute("SELECT addr,label FROM c").fetchall()}
    print("4) wallet labels:", labs)
    assert labs.get("0xMM") == "MM-BOT" and labs.get("0xTREND") == "TREND-BUY"

    wb = Whales(con, fake)
    for i in range(10):
        wb.on_msg({"channel": "trades", "data": [{"coin": "BTC", "side": "B", "px": "81500", "sz": "1.0", "time": (t0 + i) * 1000,
                                                   "users": ["0xBURST", "0xM%d" % i]}]})
    wb.flush_bursts()
    r = con.execute("SELECT kind,side,note FROM e WHERE kind='BURST'").fetchall()
    print("4b) burst test:", r)
    assert len(r) == 1 and r[0][1] == "BULL"
    wb.on_msg({"channel": "trades", "data": [{"coin": "BTC", "side": "B", "px": "81500", "sz": "1.0", "time": (t0 + 20) * 1000, "users": ["0xBURST", "0xMZ"]}]})
    wb.flush_bursts()
    assert con.execute("SELECT COUNT(*) FROM e WHERE kind='BURST'").fetchone()[0] == 1, "cooldown fail"
    print("4c) cooldown ok")
    sc, label, parts = bias_calc(con, t0 + 400)
    print("5) bias:", label, round(sc, 1), parts)
    print("\n----- report (nakli) -----")
    report(con)
    print("\nselftest OK (ye sirf logic check hai, asli market ka saboot nahi)")


if __name__ == "__main__":
    a = sys.argv[1] if len(sys.argv) > 1 else "run"
    if a == "selftest":
        selftest()
    elif a == "report":
        report(db())
    else:
        run()
