#!/usr/bin/env python3
"""
whale_radar.py  --  PAPER ONLY. Koi API key nahi, koi real order nahi.

Kya karta hai:
 1. Binance USDT-M futures ke public trades (aggTrades) + Open Interest 1-minute bars me todta hai.
 2. Whale footprint pakadta hai: taker delta ka z-score + bade trades ka delta + pichle high/low ka breakout.
 3. OI se label lagata hai (Whale Scanner jaisa): GREEN = fresh paisa, RED = purana paisa nikal raha.
 4. Impulse ka size Pig/Deer/Shark/Whale/BlueWhale/BigDaddy me naapta hai.
 5. Breakout ke baad seedha nahi ghusta: pullback ka wait, fir trigger candle par maker (limit) entry.
 6. SL pullback ke bahar, TP class ke target se, fees (maker/taker) ginke. Exit: TP / SL / exhaustion / time.
 7. Har paper trade trades.csv me, aur label-wise stats print hote hain.

Chalane ke tareeke:
   python3 whale_radar.py            # live paper (Ctrl+C se band)
   python3 whale_radar.py --selftest # nakli data par logic check
Optional: TG_TOKEN aur TG_CHAT env set karo to Telegram par alerts aayenge.
"""
import os, sys, time, csv, random, collections
import requests

CFG = dict(
    SYMBOL="BTCUSDT",
    BASE=os.getenv("BASE", "https://fapi.binance.com"),
    POLL=2.0,
    Z_MIN=2.0, BREAK_N=30, SIG_N=5, BASE_N=120, MIN_IMPULSE=100,
    PB_MIN=0.30, PB_MAX=0.60, ARM_BARS=45, FILL_BARS=3,
    SL_ATR=0.5, MIN_RR=1.5, MIN_FEE_X=3.0, TP_FRAC=0.85, HOLD_BARS=90,
    MAKER=0.0002, TAKER=0.0005, SIZE_BTC=0.1,
    SIZES=[250, 550, 1100, 1800, 5100, 15300],
    NAMES=["Pig", "Deer", "Shark", "Whale", "BlueWhale", "BigDaddy"],
    BIG_Q=0.995,
)


def note(msg):
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    tok, chat = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT")
    if tok and chat:
        try:
            requests.post("https://api.telegram.org/bot%s/sendMessage" % tok,
                          data={"chat_id": chat, "text": msg}, timeout=5)
        except Exception:
            pass


class Bar:
    __slots__ = ("t", "o", "h", "l", "c", "buy", "sell", "bbuy", "bsell", "oi")

    def __init__(self, t, p):
        self.t = t
        self.o = self.h = self.l = self.c = p
        self.buy = self.sell = self.bbuy = self.bsell = 0.0
        self.oi = None


class Engine:
    def __init__(self, cfg, quiet=False):
        self.c = cfg
        self.quiet = quiet
        self.bars = collections.deque(maxlen=400)
        self.arm = None
        self.pend = None
        self.pos = None
        self.trades = []
        self.csv = None

    def say(self, m):
        if not self.quiet:
            note(m)

    # ---------- helpers ----------
    def atr(self, n=14):
        b = list(self.bars)[-n:]
        return sum(x.h - x.l for x in b) / len(b) if b else 0.0

    def zscore(self):
        c = self.c
        b = list(self.bars)[-(c["BASE_N"] + c["SIG_N"]):]
        d = [x.buy - x.sell for x in b]
        k = c["SIG_N"]
        if len(d) < k + 40:
            return 0.0
        sums = [sum(d[i - k + 1:i + 1]) for i in range(k - 1, len(d))]
        cur, hist = sums[-1], sums[:-1]
        m = sum(hist) / len(hist)
        var = sum((x - m) ** 2 for x in hist) / len(hist)
        sd = var ** 0.5
        return (cur - m) / sd if sd > 0 else 0.0

    def big_delta(self):
        b = list(self.bars)[-self.c["SIG_N"]:]
        return sum(x.bbuy - x.bsell for x in b)

    def label(self, d):
        k = self.c["SIG_N"]
        b = list(self.bars)
        o0, o1 = b[-k - 1].oi, b[-1].oi
        if o0 is None or o1 is None:
            return "NA"
        up = d == 1
        oi_up = o1 > o0
        if up and oi_up:
            return "GREEN long-buildup"
        if up and not oi_up:
            return "RED short-covering"
        if (not up) and oi_up:
            return "GREEN short-buildup"
        return "RED long-unwinding"

    def klass(self, size):
        s = self.c["SIZES"]
        for i, v in enumerate(s):
            if size <= v * 1.1:
                return self.c["NAMES"][i]
        return self.c["NAMES"][-1]

    # ---------- main ----------
    def on_bar(self, bar):
        c = self.c
        self.bars.append(bar)
        if len(self.bars) < c["BREAK_N"] + c["SIG_N"] + 45:
            return
        z = self.zscore()
        if self.pend:
            self.try_fill(bar)
        if self.pos:
            self.manage(bar, z)
        if (not self.pos) and (not self.pend):
            if self.arm:
                self.update_arm(bar)
            else:
                self.look_for_signal(bar, z)

    def look_for_signal(self, bar, z):
        c = self.c
        b = list(self.bars)
        win = b[-(c["BREAK_N"] + c["SIG_N"]):-c["SIG_N"]]
        hi_prev = max(x.h for x in win)
        lo_prev = min(x.l for x in win)
        bd = self.big_delta()
        d = 0
        if bar.c > hi_prev and z >= c["Z_MIN"] and bd > 0:
            d = 1
        elif bar.c < lo_prev and z <= -c["Z_MIN"] and bd < 0:
            d = -1
        if not d:
            return
        recent = b[-(c["SIG_N"] + 5):]
        start = min(x.l for x in recent) if d == 1 else max(x.h for x in recent)
        ext = bar.h if d == 1 else bar.l
        size = (ext - start) * d
        if size < c["MIN_IMPULSE"]:
            return
        lab = self.label(d)
        self.arm = dict(d=d, start=start, ext=ext, size=size, pb=bar.l if d == 1 else bar.h,
                        age=0, label=lab, z=z, cls=self.klass(size))
        self.say("ARM %s %s impulse=%d pts (%s) z=%.1f %s" %
                 ("LONG" if d == 1 else "SHORT", self.arm["cls"], size, "", z, lab))

    def update_arm(self, bar):
        c, a = self.c, self.arm
        d = a["d"]
        prev = list(self.bars)[-2]
        a["age"] += 1
        fav = bar.h if d == 1 else bar.l
        adv = bar.l if d == 1 else bar.h
        if fav * d > a["ext"] * d:
            a["ext"] = fav
            a["pb"] = adv
            a["size"] = (a["ext"] - a["start"]) * d
            a["cls"] = self.klass(a["size"])
            return
        if (adv * d) < (a["pb"] * d):
            a["pb"] = adv
        depth = ((a["ext"] - a["pb"]) * d) / a["size"] if a["size"] > 0 else 9
        if depth > c["PB_MAX"] or a["age"] > c["ARM_BARS"]:
            self.say("ARM cancel (pullback %.0f%% / age %d)" % (depth * 100, a["age"]))
            self.arm = None
            return
        pf = prev.h if d == 1 else prev.l
        if depth >= c["PB_MIN"] and bar.c * d > pf * d:
            entry = bar.c
            atr = self.atr()
            sl = a["pb"] - d * c["SL_ATR"] * atr
            sizes = c["SIZES"]
            tgt = next((s for s in sizes if s > a["size"] * 1.2), a["size"] * 1.3)
            tp = a["start"] + d * tgt * c["TP_FRAC"]
            risk, reward = (entry - sl) * d, (tp - entry) * d
            fee_pts = entry * (c["MAKER"] + c["TAKER"])
            if risk <= 0 or reward <= 0 or reward / risk < c["MIN_RR"] or reward < c["MIN_FEE_X"] * fee_pts:
                self.say("SKIP rr=%.2f reward=%.0f fee=%.0f" % (reward / risk if risk > 0 else 0, reward, fee_pts))
                self.arm = None
                return
            self.pend = dict(d=d, limit=entry, sl=sl, tp=tp, age=0, label=a["label"], cls=a["cls"], z=a["z"])
            self.say("ORDER %s limit=%.1f SL=%.1f TP=%.1f RR=%.2f %s %s" %
                     ("BUY" if d == 1 else "SELL", entry, sl, tp, reward / risk, a["cls"], a["label"]))
            self.arm = None

    def try_fill(self, bar):
        p = self.pend
        p["age"] += 1
        d = p["d"]
        hit = bar.l <= p["limit"] if d == 1 else bar.h >= p["limit"]
        if hit:
            self.pos = dict(p, entry=p["limit"], bars=0)
            self.pend = None
            self.say("FILL %s @ %.1f" % ("LONG" if d == 1 else "SHORT", self.pos["entry"]))
            self.manage(bar, 0.0, first=True)
        elif p["age"] > self.c["FILL_BARS"]:
            self.say("ORDER expire (nahi bhara)")
            self.pend = None

    def manage(self, bar, z, first=False):
        c, p = self.c, self.pos
        if not first:
            p["bars"] += 1
        d, e = p["d"], p["entry"]
        stop_hit = bar.l <= p["sl"] if d == 1 else bar.h >= p["sl"]
        tp_hit = bar.h >= p["tp"] if d == 1 else bar.l <= p["tp"]
        if stop_hit:
            self.close(p["sl"], "SL", c["TAKER"], bar)
        elif tp_hit:
            self.close(p["tp"], "TP", c["MAKER"], bar)
        elif (not first) and (bar.c - e) * d > 0 and z * d <= -c["Z_MIN"]:
            self.close(bar.c, "EXHAUST", c["TAKER"], bar)
        elif p["bars"] >= c["HOLD_BARS"]:
            self.close(bar.c, "TIME", c["TAKER"], bar)

    def close(self, px, why, fee_rate, bar):
        c, p = self.c, self.pos
        d, e = p["d"], p["entry"]
        gross = (px - e) * d
        fees = e * c["MAKER"] + px * fee_rate
        net = gross - fees
        row = dict(time=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(bar.t * 60)),
                   dir="LONG" if d == 1 else "SHORT", cls=p["cls"], label=p["label"],
                   entry=round(e, 1), exit=round(px, 1), why=why,
                   gross_pts=round(gross, 1), fee_pts=round(fees, 1), net_pts=round(net, 1),
                   net_usd=round(net * c["SIZE_BTC"], 2))
        self.trades.append(row)
        self.say("EXIT %s %s net=%.0f pts ($%.2f)" % (row["dir"], why, net, row["net_usd"]))
        if self.csv:
            new = not os.path.exists(self.csv)
            with open(self.csv, "a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(row.keys()))
                if new:
                    w.writeheader()
                w.writerow(row)
        self.pos = None

    def summary(self):
        t = self.trades
        if not t:
            return "abhi tak koi paper trade nahi."
        wins = [x for x in t if x["net_pts"] > 0]
        tot = sum(x["net_pts"] for x in t)
        out = ["TRADES=%d  WIN=%.0f%%  NET=%.0f pts  ($%.2f)" %
               (len(t), 100 * len(wins) / len(t), tot, sum(x["net_usd"] for x in t))]
        by = collections.defaultdict(list)
        for x in t:
            by[x["label"]].append(x["net_pts"])
        for k, v in sorted(by.items()):
            out.append("  %-22s n=%-3d win=%.0f%% net=%.0f" %
                       (k, len(v), 100 * sum(1 for q in v if q > 0) / len(v), sum(v)))
        return "\n".join(out)


# ---------------- live feed ----------------
def live(cfg):
    eng = Engine(cfg)
    eng.csv = "trades.csv"
    s = requests.Session()
    sizes = collections.deque(maxlen=20000)
    thr = 1.0
    cur, last_id, last_oi, last_sum = None, None, None, time.time()
    note("whale_radar PAPER chal raha hai: %s @ %s" % (cfg["SYMBOL"], cfg["BASE"]))
    while True:
        try:
            params = {"symbol": cfg["SYMBOL"], "limit": 1000}
            if last_id:
                params["fromId"] = last_id + 1
            r = s.get(cfg["BASE"] + "/fapi/v1/aggTrades", params=params, timeout=10)
            if r.status_code in (403, 451):
                note("Exchange ne block kiya (%d). BASE badal ke dekho ya Madhav ko batao." % r.status_code)
                return
            r.raise_for_status()
            for t in r.json():
                p, q, ms, m, tid = float(t["p"]), float(t["q"]), t["T"], t["m"], t["a"]
                minute = ms // 60000
                if cur is None:
                    cur = Bar(minute, p)
                if minute > cur.t:
                    try:
                        o = s.get(cfg["BASE"] + "/fapi/v1/openInterest",
                                  params={"symbol": cfg["SYMBOL"]}, timeout=10).json()
                        last_oi = float(o["openInterest"])
                    except Exception:
                        pass
                    cur.oi = last_oi
                    eng.on_bar(cur)
                    if len(sizes) > 500:
                        srt = sorted(sizes)
                        thr = max(0.2, srt[int(len(srt) * cfg["BIG_Q"])])
                    cur = Bar(minute, p)
                cur.h, cur.l, cur.c = max(cur.h, p), min(cur.l, p), p
                if m:
                    cur.sell += q
                    cur.bsell += q if q >= thr else 0
                else:
                    cur.buy += q
                    cur.bbuy += q if q >= thr else 0
                sizes.append(q)
                last_id = tid
        except KeyboardInterrupt:
            note("band.\n" + eng.summary())
            return
        except Exception as e:
            note("error: %s (retry)" % e)
            time.sleep(5)
        if time.time() - last_sum > 3600:
            note("HOURLY\n" + eng.summary())
            last_sum = time.time()
        time.sleep(cfg["POLL"])


# ---------------- self test (nakli data) ----------------
def synth(n, events, seed):
    rnd = random.Random(seed)
    price, oi, t0 = 81000.0, 80000.0, 28000000
    bars = []
    ev_at = set(range(120, n, 400)) if events else set()
    script = []
    for i in range(n):
        if i in ev_at:
            up = rnd.random() < 0.5
            d = 1 if up else -1
            for _ in range(6):
                script.append((d * 70, 40, 8, 20, 0, 0.0012 * d * d))
            for _ in range(3):
                script.append((-d * 55, 12, 12, 0, 0, 0))
            script.append((d * 45, 30, 8, 10, 0, 0.0005))
            for _ in range(14):
                script.append((d * 40, 14, 9, 2, 0, 0.0003))
        if script:
            mv, b, s, bb, bs, doi = script.pop(0)
            mv += rnd.gauss(0, 8)
        else:
            mv, b, s, bb, bs, doi = rnd.gauss(0, 22), max(0.5, rnd.gauss(10, 3)), max(0.5, rnd.gauss(10, 3)), 0, 0, 0
            bb = rnd.random() * 1.5 if rnd.random() < .1 else 0
            bs = rnd.random() * 1.5 if rnd.random() < .1 else 0
        o = price
        price += mv
        bar = Bar(t0 + i, o)
        bar.c = price
        bar.h = max(o, price) + abs(rnd.gauss(0, 6))
        bar.l = min(o, price) - abs(rnd.gauss(0, 6))
        bar.buy, bar.sell, bar.bbuy, bar.bsell = b, s, bb, bs
        oi *= 1 + doi
        bar.oi = oi
        bars.append(bar)
    return bars


def selftest(cfg):
    for events in (True, False):
        eng = Engine(cfg, quiet=True)
        for b in synth(20000, events, 7):
            eng.on_bar(b)
        print("\n== %s ==" % ("nakli whale events ke saath" if events else "sirf shor (koi whale nahi)"))
        print(eng.summary())
        print("(sirf logic check: nakli data ka result asli market ka saboot nahi hai)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest(CFG)
    else:
        live(CFG)
