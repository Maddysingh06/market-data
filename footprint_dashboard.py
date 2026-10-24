#!/usr/bin/env python3
"""
footprint_dashboard.py -- market_collector.py ke data ko browser me live dikhata hai.
Alag port (8090) par chalta hai, PivotPulse ya bot se koi lena-dena nahi, sirf market.db PADHTA hai.
Koi order nahi deta, koi state nahi badalta.

Chalane ka tareeka:
  python3 footprint_dashboard.py            # port 8090 par
  PORT=9000 python3 footprint_dashboard.py  # alag port par
"""
import os, time, sqlite3, collections
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB = os.getenv("DB", "market.db")
PORT = int(os.getenv("PORT", "8090"))


def q(con, sql, args=()):
    return con.execute(sql, args).fetchall()


def latest(con):
    rows = q(con, "SELECT src,k,v,MAX(ts) FROM m GROUP BY src,k ORDER BY src,k")
    return {(s, k): (v, ts) for s, k, v, ts in rows}


def bias_calc(con, now):
    L = {k: v for k, (v, ts) in latest(con).items()}
    g = lambda s, k: L.get((s, k))
    sc, parts = 0.0, []
    tb, tsl = g("hyperliquid", "trend_buy_usd"), g("hyperliquid", "trend_sell_usd")
    if tb is not None and tsl is not None:
        x = max(-3, min(3, (tb - tsl) / 1e6))
        sc += x
        parts.append("HL trend wallets %+.1f" % x)
    rows = q(con, "SELECT kind,side,COUNT(*) FROM e WHERE ts>? GROUP BY kind,side", (int(now) - 900,))
    cnt = {(k, s): n for k, s, n in rows}
    for kind, w in (("SLICE", 0.5), ("ABSORB", 1.0), ("BURST", 0.5)):
        x = max(-3, min(3, w * (cnt.get((kind, "BULL"), 0) - cnt.get((kind, "BEAR"), 0))))
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
    bu, au = g("depth", "bid_usd"), g("depth", "ask_usd")
    if bu is not None and au is not None and (bu + au) > 0:
        x = max(-0.5, min(0.5, (bu - au) / 10e6))
        if abs(x) > 0.1:
            sc += x
            parts.append("order-book imbalance %+.1f" % x)
    label = "BULLISH" if sc >= 2 else "BEARISH" if sc <= -2 else "NEUTRAL"
    return sc, label, parts


def wall_watch(con, now):
    rows = con.execute(
        "SELECT ts,k,v FROM m WHERE src='depth' AND k IN "
        "('bid_wall_px','bid_wall_usd','ask_wall_px','ask_wall_usd') AND ts>? ORDER BY ts",
        (int(now) - 1800,)).fetchall()
    by_ts = collections.defaultdict(dict)
    for ts, k, v in rows:
        by_ts[ts][k] = v
    stamps = sorted(by_ts)
    if len(stamps) < 2:
        return None
    first, last = by_ts[stamps[0]], by_ts[stamps[-1]]
    out = {}
    for side in ("bid", "ask"):
        px0, u0 = first.get(side + "_wall_px"), first.get(side + "_wall_usd")
        px1, u1 = last.get(side + "_wall_px"), last.get(side + "_wall_usd")
        if u0 and u1 and px0 and px1:
            same_level = abs(px1 - px0) / px0 < 0.002
            if same_level and u1 < u0 * 0.5:
                out[side] = ("EATEN/CHHOTA HO GAYA", px1, u0, u1)
            elif same_level:
                out[side] = ("ABHI BHI KHADA HAI", px1, u0, u1)
            else:
                out[side] = ("KHISAK GAYA", px1, u0, u1)
    return out or None


def spoof_check(con, now, window=1800):
    """Order-book wall SPOOFING pakadne ki koshish: ek bada wall (>$500K) dikhta hai, phir agle hi
    poll (30sec baad) mein SAME price ke paas achanak bahut chhota/gayab ho jaata hai (>70% drop) --
    agar us 30-60sec window mein us side ka MATCHING trade-volume bahut kam hua (order asal mein
    EXECUTE nahi hua), toh ye strong signal hai ki wo order sirf CANCEL/PULL kiya gaya tha -- classic
    spoof pattern (fake wall dikhake doosri taraf ke traders ko trade karwana, phir apna order hata lena).

    SACH: hum sirf har ~30sec mein ek POLL lete hain (poora order-book snapshot) -- agar koi wall
    isse KAM time mein place+cancel karta hai (jo real high-frequency spoofing mein common hai), hume
    BILKUL nahi dikhega. Ye sirf SLOWER/manual-type spoofing catch kar sakta hai, millisecond-level
    algo-spoofing NAHI -- uske liye tick-by-tick order-book-diff feed chahiye hoga, jo abhi nahi hai.
    Isliye 'spoof detect nahi hua' ka matlab 'spoofing nahi ho rahi' bilkul nahi hai."""
    rows = con.execute(
        "SELECT ts,k,v FROM m WHERE src='depth' AND k IN "
        "('bid_wall_px','bid_wall_usd','ask_wall_px','ask_wall_usd') AND ts>? ORDER BY ts",
        (int(now) - window,)).fetchall()
    by_ts = collections.defaultdict(dict)
    for ts, k, v in rows:
        by_ts[ts][k] = v
    stamps = sorted(by_ts)
    if len(stamps) < 4:
        return None

    zone_of = lambda p: round(p / 100) * 100
    out = []
    for side in ("bid", "ask"):
        pxk, uk = side + "_wall_px", side + "_wall_usd"
        series = [(ts, by_ts[ts].get(pxk), by_ts[ts].get(uk)) for ts in stamps if by_ts[ts].get(pxk) and by_ts[ts].get(uk)]
        for i in range(len(series) - 1):
            ts0, px0, u0 = series[i]
            ts1, px1, u1 = series[i + 1]
            if u0 > 500_000 and zone_of(px0) == zone_of(px1) and u1 < u0 * 0.3:
                vol_key = "big_sell_usd" if side == "bid" else "big_buy_usd"
                vol_rows = con.execute(
                    "SELECT v FROM m WHERE src='hyperliquid' AND k=? AND ts BETWEEN ? AND ?",
                    (vol_key, ts0, ts1 + 60)).fetchall()
                matching_vol = sum(v[0] for v in vol_rows) if vol_rows else 0.0
                drop = u0 - u1
                if matching_vol < drop * 0.3:
                    out.append({"side": side, "px": px0, "was_usd": u0, "now_usd": u1,
                               "matching_vol": matching_vol, "ts": ts1})
    # duplicate-ish events (same side, bahut paas ke ts) ko squash karo, sirf latest-wala rakho
    out.sort(key=lambda e: e["ts"])
    dedup = {}
    for e in out:
        dedup[e["side"]] = e
    return list(dedup.values()) or None


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def track_record(con):
    try:
        rows = con.execute("SELECT eval15,eval30,eval60,eval240 FROM setups").fetchall()
    except Exception:
        return {}, 0
    out = {}
    for i, col in enumerate(("15min", "30min", "60min", "4hr")):
        vals = [r[i] for r in rows if r[i] is not None]
        h, m, fl = vals.count("HIT"), vals.count("MISS"), vals.count("FLAT")
        out[col] = (h, m, fl, len(vals))
    return out, len(rows)


def pig_vs_whale(con):
    g = {}
    rows = con.execute("SELECT src,k,v,MAX(ts) FROM m WHERE src='hyperliquid' GROUP BY src,k").fetchall()
    for s, k, v, ts in rows:
        g[k] = v
    ptb, pts_ = g.get("pig_trend_buy_usd"), g.get("pig_trend_sell_usd")
    wtb, wts_ = g.get("trend_buy_usd"), g.get("trend_sell_usd")
    if ptb is None or pts_ is None:
        return None
    pig_net = ptb - pts_
    whale_net = (wtb - wts_) if (wtb is not None and wts_ is not None) else None
    diverge = whale_net is not None and whale_net != 0 and pig_net != 0 and (whale_net > 0) != (pig_net > 0)
    return pig_net, whale_net, diverge


def tier_name(usd):
    # MERI apni $ size guess -- teacher ke Pig/Deer/Shark... POINT-move class se ALAG cheez
    if usd is None:
        return "?"
    u = abs(usd)
    if u < 100_000:
        return "PIG"
    if u < 300_000:
        return "DEER"
    if u < 1_000_000:
        return "SHARK"
    if u < 5_000_000:
        return "WHALE"
    if u < 20_000_000:
        return "BLUEWHALE"
    return "BIGDADDY"


def whale_conviction(con):
    w = con.execute("SELECT addr, szi, usd, MAX(ts) FROM w GROUP BY addr").fetchall()
    c = con.execute("SELECT addr, label, MAX(ts) FROM c GROUP BY addr").fetchall()
    clab = {a: lab for a, lab, _ in c}
    out = []
    for addr, szi, usd, _ in w:
        lab = clab.get(addr)
        if lab not in ("TREND-BUY", "TREND-SELL") or szi is None:
            continue
        pos_long = szi > 0
        flow_buy = lab == "TREND-BUY"
        if pos_long and flow_buy:
            verdict, cls = "PRESSING LONG", "up"
        elif pos_long and not flow_buy:
            verdict, cls = "LONG covering/exit", "dn"
        elif (not pos_long) and not flow_buy:
            verdict, cls = "PRESSING SHORT", "dn"
        else:
            verdict, cls = "SHORT covering/exit", "up"
        out.append((addr, "LONG" if pos_long else "SHORT", usd, verdict, cls, tier_name(usd)))
    return out


def biggest_player(con, now):
    """Pure BTC (jo bhi wallets hum track kar rahe hain) me se SABSE BADA live position
    (abs $ size se) dhoondo. Fir uske apne LIQ price se cascade-push direction nikalo,
    aur uska APNA position-size history (w table se) dekh ke batao ki wo khilada
    abhi size BADHA raha hai (pressing) ya GHATA raha hai (unwind/profit-booking) --
    ye ek asli, measured trend hai (DB se compute), koi guess ya prediction nahi."""
    row = con.execute(
        "SELECT addr,szi,entry,liq,usd,upnl,lev FROM w WHERE ts=(SELECT MAX(ts) FROM w) "
        "ORDER BY usd DESC LIMIT 1").fetchone()
    if not row:
        return None
    addr, szi, entry, liq, usd, upnl, lev = row
    if szi is None or usd is None:
        return None
    side = "LONG" if szi > 0 else "SHORT"
    cur_px = current_mark(con)

    push_dir, push_dist_pct = None, None
    if liq and cur_px:
        # LONG ka liq hamesha neeche hota hai (price girne se liquidate) -> cascade NICHE
        # SHORT ka liq hamesha upar hota hai (price chadne se liquidate) -> cascade UPAR
        push_dir = "NICHE" if side == "LONG" else "UPAR"
        push_dist_pct = abs(cur_px - liq) / cur_px * 100

    trend = {}
    for label, secs in (("15min", 900), ("1hr", 3600), ("4hr", 14400)):
        r = con.execute(
            "SELECT usd FROM w WHERE addr=? AND ts<=? ORDER BY ts DESC LIMIT 1",
            (addr, now - secs)).fetchone()
        if r and r[0]:
            old_usd = r[0]
            trend[label] = (usd - old_usd) / old_usd * 100
        else:
            trend[label] = None

    return {"addr": addr, "side": side, "szi": szi, "entry": entry, "liq": liq,
            "usd": usd, "upnl": upnl, "lev": lev, "cur_px": cur_px,
            "push_dir": push_dir, "push_dist_pct": push_dist_pct, "trend": trend}


def whale_levels(con):
    """Saare LIVE whale positions (latest w snapshot) ko price-zones (SR_ZONE chaudi) me cluster
    karo -- alag LONG entries alag, alag SHORT entries alag, $usd-weighted. Jahan LONG entries ka
    sabse bada cluster hai wahi un whales ka "defend karne wala" support zone hai (wahan unka average
    cost-basis hai); SHORT entries ka sabse bada cluster resistance. Ye REAL entry-price data hai jo
    exchange khud deta hai, kisi indicator ka guess nahi."""
    rows = con.execute("SELECT szi,entry,usd FROM w WHERE ts=(SELECT MAX(ts) FROM w) AND entry IS NOT NULL").fetchall()
    if not rows:
        return None
    zone_of = lambda p: round(p / SR_ZONE) * SR_ZONE
    longs, shorts = collections.defaultdict(float), collections.defaultdict(float)
    for szi, entry, usd in rows:
        if not usd:
            continue
        z = zone_of(entry)
        (longs if szi > 0 else shorts)[z] += usd
    top_longs = sorted(longs.items(), key=lambda kv: -kv[1])[:3]
    top_shorts = sorted(shorts.items(), key=lambda kv: -kv[1])[:3]
    return {"long_entry_zones": top_longs, "short_entry_zones": top_shorts}


def whale_liq_clusters(con):
    """Saare LIVE whale positions ke LIQ price ko bhi price-zones me cluster karo, $usd-weighted,
    LONG-liq aur SHORT-liq alag alag. Jahan bahut saare LONG liqs ek zone me jama hain, wahan agar
    price pahunchi to EK saath kai forced-sell market-orders fire hone ka risk hai -- cascade zone.
    SACH: ye batata hai KAHAN cascade ho sakta hai agar price wahan pahunche, ye guarantee nahi ki
    price wahan jayegi hi -- sirf 'agar jaaye to kya hoga' ka structural map hai."""
    rows = con.execute("SELECT szi,liq,usd FROM w WHERE ts=(SELECT MAX(ts) FROM w) AND liq IS NOT NULL AND liq>0").fetchall()
    if not rows:
        return None
    zone_of = lambda p: round(p / SR_ZONE) * SR_ZONE
    long_liq, short_liq = collections.defaultdict(float), collections.defaultdict(float)
    for szi, liq, usd in rows:
        if not usd:
            continue
        z = zone_of(liq)
        (long_liq if szi > 0 else short_liq)[z] += usd
    top_long_liq = sorted(long_liq.items(), key=lambda kv: -kv[1])[:3]   # neeche cascade zones
    top_short_liq = sorted(short_liq.items(), key=lambda kv: -kv[1])[:3]  # upar cascade zones
    return {"long_liq_zones": top_long_liq, "short_liq_zones": top_short_liq}


def retail_trap_check(con):
    """Top-traders (bade/smart wallets) ka long/short ratio vs retail ka long/short ratio compare
    karo -- dono Binance ke public ratio-endpoints se seedha aate hain, koi derived guess nahi. Jab
    retail bahut zyada ek taraf jhuke hon (>1.5x ya <0.67x) aur top-traders uससe kaafi alag/opposite
    hon, wahi classic 'retail trapped' zone hoti hai -- retail jis taraf heavy hai, market usi ko
    squeeze karne jaata hai aksar."""
    L = {k: v for k, (v, ts) in latest(con).items()}
    top = L.get(("binance", "top_pos_ls"))
    retail = L.get(("binance", "retail_ls"))
    if top is None or retail is None or retail <= 0:
        return None
    retail_heavy_long = retail > 1.5
    retail_heavy_short = retail < 0.67
    diverge = top is not None and (
        (retail_heavy_long and top < retail * 0.8) or
        (retail_heavy_short and top > retail * 1.25))
    trapped_side = None
    if diverge:
        trapped_side = "LONG" if retail_heavy_long else "SHORT"
    return {"top_ls": top, "retail_ls": retail, "trapped_side": trapped_side}


def exhaustion_check(con, now):
    """'Taaqat khatam ho rahi hai kya' detector. Jab abhi ek taraf (jaise SELLERS BHAARI) pressure
    chal raha hai, ye 4 ALAG real-measured cheezein check karta hai taaki pata chale us pressure ka
    MOMENTUM fade ho raha hai ya nahi -- matlab 'yahan se bina kisi NAYE bade player ke is move mein
    aur dum nahi lag raha':
      1. big_buy/big_sell ka recent flow -- pichhle 2 periods me ghat raha hai kya (period-wise data,
         cumulative nahi, isliye genuinely comparable)
      2. order-book ka defending wall (jis side push ho raha uske OPPOSITE) khada hai ya khisak gaya
      3. sabse bada khiladi: uska 1hr trend abhi bhi badh raha hai, PAR 15min (sabse fresh) me building
         ruk gayi -- matlab uski apni speed abhi slow ho rahi hai (confirm nahi ki ruk gaya, sirf slow)
      4. retail already capitulate kar chuka (retail_trap_check se, isi direction mein heavy)
    SACH: 'exhaustion' ka matlab REVERSAL CONFIRM nahi hai -- sirf itna hai ki jo pressure chal raha
    hai, uska momentum fresh/strong nahi lag raha abhi. Price flat/sideways bhi reh sakti hai, upar
    bhi nahi jaani zaroori. Koi guarantee nahi, sirf ek extra caution-flag hai."""
    bsp = buy_sell_pressure(con)
    if not bsp:
        return None
    buy, sell, pct = bsp
    dominant = "SELL" if pct < 50 else "BUY"
    signals = []

    flow_key = "big_sell_usd" if dominant == "SELL" else "big_buy_usd"
    rows = con.execute("SELECT v FROM m WHERE src='hyperliquid' AND k=? ORDER BY ts DESC LIMIT 3",
                       (flow_key,)).fetchall()
    if len(rows) == 3:
        v0, v1, v2 = rows[0][0], rows[1][0], rows[2][0]
        if v0 < v1 and v1 <= v2:
            signals.append("%s-flow pichhle 2 intervals me fade ho raha hai ($%.0f -> $%.0f -> $%.0f)" % (dominant, v2, v1, v0))

    ww = wall_watch(con, now)
    if ww:
        defend_side = "bid" if dominant == "SELL" else "ask"
        d = ww.get(defend_side)
        if d and d[0] == "ABHI BHI KHADA HAI":
            signals.append("%s wall abhi bhi khada hai (absorb ho raha, khisak nahi raha)" % defend_side.upper())

    bp = biggest_player(con, now)
    if bp:
        bp_matches = (bp["side"] == "SHORT" and dominant == "SELL") or (bp["side"] == "LONG" and dominant == "BUY")
        t15, t1h = bp["trend"].get("15min"), bp["trend"].get("1hr")
        if bp_matches and t15 is not None and t1h is not None and t1h > 3 and t15 <= 1:
            signals.append("sabse bada khiladi (%s) ka 1hr trend abhi bhi +%.1f%% hai, par 15min (sabse fresh) me building ruk gayi (%+.1f%%)" %
                           (bp["side"], t1h, t15))

    trap = retail_trap_check(con)
    if trap:
        retail_dir = "SHORT" if trap["retail_ls"] < 0.67 else "LONG" if trap["retail_ls"] > 1.5 else None
        if retail_dir and ((retail_dir == "SHORT" and dominant == "SELL") or (retail_dir == "LONG" and dominant == "BUY")):
            signals.append("retail bhi already %s-heavy hai (L/S %.2f) -- capitulation jaisa dikh raha" % (retail_dir, trap["retail_ls"]))

    return {"dominant": dominant, "pct": pct, "signals": signals, "exhausted": len(signals) >= 2}


def smart_money_playbook(con, now):
    """Sab signals ek jagah milake 2 alag horizon ke liye verdict banata hai:
      SCALP  (15min-1hr) -- wick-hunt + order-book walls + 1hr trendline + immediate bias
      SWING  (4hr+)       -- biggest-player ka position-trend + 4hr/24hr trendline + weekly whale net flow
    Har horizon me: direction, kitne signals agree kar rahe (confidence), entry-zone (whale entry
    cluster), invalidation (nearest whale-liq cluster opposite side), aur retail-trap warning alag se.
    SACH: ye sab existing real signals ka COMBINATION/confluence-count hai -- naya prediction-engine
    nahi, sirf jo pehle se alag-alag boxes me tha use ek jagah jodta hai taaki clash/confluence saaf dikhe."""
    px = current_mark(con)
    if px is None:
        return None
    sc, bias_label, _ = bias_calc(con, now)
    conv = whale_conviction(con)
    press_long = sum((x[2] or 0) for x in conv if x[3] == "PRESSING LONG")
    press_short = sum((x[2] or 0) for x in conv if x[3] == "PRESSING SHORT")
    wh = wick_hunt_check(con, now)
    bp = biggest_player(con, now)
    lv = whale_levels(con)
    liqc = whale_liq_clusters(con)
    trap = retail_trap_check(con)

    def nearest(zones, above):
        cands = [z for z, u in (zones or []) if (z > px) == above]
        return min(cands, key=lambda z: abs(z - px)) if cands else None

    def zones_for(direction):
        # entry_zone = isi direction ke whales ka entry-cluster (support/resistance, jahan unka cost-basis hai)
        # target_zone = OPPOSITE side ka liq-cluster, trade ki favorable direction me -- agar price wahan
        #   pahunchi to un opposite-wale whales ka forced-close hoga, jo isi direction me price ko aur dhakka dega
        # invalid_zone = ISI direction (apne side) ka liq-cluster -- agar price wahan pahunch gayi, matlab
        #   isi cohort ke whales flush ho rahe hain, thesis fail maano
        if direction == "LONG":
            entry_zone = nearest(lv["long_entry_zones"] if lv else None, False)
            target_zone = nearest(liqc["short_liq_zones"] if liqc else None, True)
            invalid_zone = nearest(liqc["long_liq_zones"] if liqc else None, False)
        else:
            entry_zone = nearest(lv["short_entry_zones"] if lv else None, True)
            target_zone = nearest(liqc["long_liq_zones"] if liqc else None, False)
            invalid_zone = nearest(liqc["short_liq_zones"] if liqc else None, True)
        return entry_zone, target_zone, invalid_zone

    out = {"px": px, "trap": trap}

    # ---------- SCALP (15min-1hr) ----------
    sc_votes, sc_reasons = 0, []
    sc_dir = None
    if wh and wh["aligned"]:
        sc_dir = "LONG" if wh["expect_dir"] == "UP" else "SHORT"
        sc_votes += 1
        sc_reasons.append("wick-hunt (%s, %s taraf)" % (wh["kind"], wh["expect_dir"]))
    if abs(sc) > 1.0:
        d2 = "LONG" if sc > 0 else "SHORT"
        if sc_dir is None:
            sc_dir = d2
        if d2 == sc_dir:
            sc_votes += 1
            sc_reasons.append("immediate bias %s (%+.1f)" % (bias_label, sc))
    if sc_dir:
        entry_zone, target_zone, invalid_zone = zones_for(sc_dir)
        out["scalp"] = {"dir": sc_dir, "votes": sc_votes, "reasons": sc_reasons,
                         "entry_zone": entry_zone, "target_zone": target_zone, "invalid_zone": invalid_zone}
    else:
        out["scalp"] = None

    # ---------- SWING (4hr+) ----------
    sw_votes, sw_reasons = 0, []
    sw_dir = None
    if bp and bp["trend"].get("4hr") is not None:
        if bp["trend"]["4hr"] > 3:
            sw_dir = bp["side"]
            sw_reasons.append("sabse bada khiladi (%s) apna size 4hr me +%.1f%% badha raha" % (bp["side"], bp["trend"]["4hr"]))
            sw_votes += 1
        elif bp["trend"]["4hr"] < -3:
            # badha hua khiladi unwind kar raha hai -- isse koi fresh direction vote nahi milta
            # (uska apna exit hai, naya conviction nahi), sirf context ke liye reason me note karte hain
            sw_reasons.append("SACH context: sabse bada khiladi (%s) apna size 4hr me %.1f%% ghata raha (unwind, iska fresh-direction vote nahi)" %
                              (bp["side"], bp["trend"]["4hr"]))
    if press_long > 0 or press_short > 0:
        d2 = "LONG" if press_long >= press_short * 1.3 else "SHORT" if press_short >= press_long * 1.3 else None
        if d2:
            if sw_dir is None:
                sw_dir = d2
            if d2 == sw_dir:
                sw_votes += 1
                sw_reasons.append("whale conviction overall bhi %s taraf" % d2)
    if sw_dir:
        entry_zone, target_zone, invalid_zone = zones_for(sw_dir)
        out["swing"] = {"dir": sw_dir, "votes": sw_votes, "reasons": sw_reasons,
                         "entry_zone": entry_zone, "target_zone": target_zone, "invalid_zone": invalid_zone}
    else:
        out["swing"] = None

    return out


def master_verdict(con, now):
    """SAB signals (wick-hunt, exhaustion-check, smart-money-playbook scalp+swing) ko ek jagah
    milake EK top headline banata hai -- taaki alag-alag boxes padhke khud confluence nikaalne ki
    zaroorat na pade.

    FAKE WICK-HUNT SE BACHAV: wick_hunt_check() khud hi sirf confirmed-retrace + bias/whale-alignment
    pe trigger hota hai (raw spike pe nahi) -- par yahan ek extra cross-check bhi karte hain: agar
    wick-hunt 'X taraf move aayega' keh raha hai (matlab opposite side abhi exhaust hona chahiye),
    aur exhaustion_check khud us opposite side ko 'abhi bhi FRESH/strong' bata raha hai (exhausted
    nahi), toh DONO signals CONTRADICT kar rahe hain -- is case me hum CONFIDENT verdict nahi dete,
    'MIXED, wait karo' bolte hain. Jab dono AGREE karte hain, tabhi extra weight milta hai.

    SACH: ye bhi existing signals ka combination hai, naya prediction-engine nahi. 'Confidence' sirf
    kitne signals agree karte hain uska count hai, statistically validated probability nahi."""
    smp = smart_money_playbook(con, now)
    if not smp:
        return None
    wh = wick_hunt_check(con, now)
    exh = exhaustion_check(con, now)

    votes = {"LONG": 0.0, "SHORT": 0.0}
    reasons = {"LONG": [], "SHORT": []}
    conflict = []

    if wh and wh["aligned"]:
        wdir = "LONG" if wh["expect_dir"] == "UP" else "SHORT"
        opp_dominant = "SELL" if wdir == "LONG" else "BUY"  # jis side exhaust hona chahiye isi wick-hunt ko support karne ke liye
        if exh and exh["dominant"] == opp_dominant and exh["exhausted"]:
            votes[wdir] += 1.5
            reasons[wdir].append("wick-hunt + exhaustion-check DONO confirm (%s side momentum khatam lag raha)" % opp_dominant)
        elif exh and exh["dominant"] == opp_dominant and not exh["exhausted"]:
            conflict.append("wick-hunt %s expect kar raha, PAR exhaustion-check kehta hai %s side abhi bhi fresh/strong hai -- CONTRADICT, is wick-hunt pe akela bharosa mat karo" %
                            (wdir, opp_dominant))
        else:
            votes[wdir] += 0.7
            reasons[wdir].append("wick-hunt (%s) -- exhaustion-check se cross-check nahi mil paya, standalone signal" % wh["kind"])

    for key in ("scalp", "swing"):
        d = smp.get(key)
        if d:
            votes[d["dir"]] += d["votes"] * 0.5
            reasons[d["dir"]].append("%s confluence: %s" % (key.upper(), "; ".join(d["reasons"])))

    final_dir = None
    if votes["LONG"] >= 1.2 and votes["LONG"] > votes["SHORT"]:
        final_dir = "LONG"
    elif votes["SHORT"] >= 1.2 and votes["SHORT"] > votes["LONG"]:
        final_dir = "SHORT"

    return {"dir": final_dir, "score_long": votes["LONG"], "score_short": votes["SHORT"],
            "reasons": reasons.get(final_dir, []) if final_dir else [],
            "conflict": conflict, "trap": smp["trap"], "exh": exh}


def entry_radar(con):
    rows = con.execute("SELECT addr,label,bought,sold FROM c WHERE ts=(SELECT MAX(ts) FROM c)").fetchall()
    tiers = collections.defaultdict(lambda: {"buy": 0.0, "sell": 0.0, "n": 0})
    for addr, lab, b_, s_ in rows:
        base = lab[4:] if lab.startswith("PIG-") else lab
        if base not in ("TREND-BUY", "TREND-SELL"):
            continue
        tot = (b_ or 0) + (s_ or 0)
        t = tiers[tier_name(tot)]
        t["buy"] += b_ or 0
        t["sell"] += s_ or 0
        t["n"] += 1
    order = ["PIG", "DEER", "SHARK", "WHALE", "BLUEWHALE", "BIGDADDY"]
    return [(name, tiers[name]["n"], tiers[name]["buy"] - tiers[name]["sell"], tiers[name]["buy"], tiers[name]["sell"])
            for name in order if name in tiers]


SR_ZONE = 150
SR_LOOK_FWD = 300
SR_REV_MOVE = 80


def sr_zones(con, now, src="binance", lookback=SR_LOOK_FWD * 40):
    rows = con.execute("SELECT ts,v FROM m WHERE src=? AND k='mark' AND ts>? ORDER BY ts", (src, now - lookback)).fetchall()
    if len(rows) < 20:
        return None
    ts_arr = [r[0] for r in rows]
    px_arr = [r[1] for r in rows]
    zone_of = lambda p: round(p / SR_ZONE) * SR_ZONE
    stats = collections.defaultdict(lambda: {"touch": 0, "up": 0, "down": 0, "flat": 0})
    n = len(rows)
    i = 0
    last_zone_hit = None
    while i < n:
        z = zone_of(px_arr[i])
        if z != last_zone_hit:
            j = i
            while j < n and ts_arr[j] - ts_arr[i] < SR_LOOK_FWD:
                j += 1
            if j < n:
                move = px_arr[j] - px_arr[i]
                st = stats[z]
                st["touch"] += 1
                if move > SR_REV_MOVE:
                    st["up"] += 1
                elif move < -SR_REV_MOVE:
                    st["down"] += 1
                else:
                    st["flat"] += 1
            last_zone_hit = z
        i += 1
    cur = px_arr[-1]
    return sorted(stats.items(), key=lambda kv: abs(kv[0] - cur))[:4]


def current_mark(con, src="binance"):
    r = con.execute("SELECT v FROM m WHERE src=? AND k='mark' ORDER BY ts DESC LIMIT 1", (src,)).fetchone()
    return r[0] if r else None


def _resample_ohlc(rows, bucket_sec):
    """rows = [(ts,price), ...] sorted by ts -> {bucket_ts: [o,h,l,c]}"""
    buckets = {}
    for ts, px in rows:
        b = int(ts // bucket_sec) * bucket_sec
        if b not in buckets:
            buckets[b] = [px, px, px, px]
        else:
            o, h, l, c = buckets[b]
            buckets[b] = [o, max(h, px), min(l, px), px]
    return sorted(buckets.items())


def _project_line(tA, pA, tB, pB, t_now):
    if tA == tB:
        return None
    t1, p1 = (tA, pA) if tA < tB else (tB, pB)
    t2, p2 = (tB, pB) if tA < tB else (tA, pA)
    slope = (p2 - p1) / (t2 - t1)
    return p1 + slope * (t_now - t1)


def trendline_break(con, now, src="binance", big_bucket=3600, small_bucket=300, small_lookback=14400):
    """Admin/retail log manually jo trendline khinchte hain, uska automated version:
    1 PURANA/BADA extreme point (bade timeframe -- jitna data hai utna -- ka ekdum LOWEST/HIGHEST)
    + 1 RECENT/CHOTA extreme point (chote timeframe, last 4hr ka lowest/highest) ko jodo.
    SACH: swing-point selection ka koi ek 'sahi' tareeka nahi hota -- ye ek reasonable automated
    version hai, haath se khinchi chart-line se thoda alag ho sakta hai. Jitna kam history hai utna
    hi weak/short ye trendline hogi -- naye deploy ke turant baad "bada" wala point bhi recent hi hoga."""
    rows = con.execute("SELECT ts,v FROM m WHERE src=? AND k='mark' ORDER BY ts", (src,)).fetchall()
    if len(rows) < 50:
        return None
    cur_px = rows[-1][1]

    big_candles = _resample_ohlc(rows, big_bucket)          # jitna data hai utna -- "bada" timeframe
    small_rows = [(ts, v) for ts, v in rows if ts >= now - small_lookback]
    small_candles = _resample_ohlc(small_rows, small_bucket)
    if len(big_candles) < 3 or len(small_candles) < 3:
        return None

    big_low_ts, big_low = min(((t, c[2]) for t, c in big_candles), key=lambda x: x[1])
    small_low_ts, small_low = min(((t, c[2]) for t, c in small_candles), key=lambda x: x[1])
    support = _project_line(big_low_ts, big_low, small_low_ts, small_low, now)

    big_high_ts, big_high = max(((t, c[1]) for t, c in big_candles), key=lambda x: x[1])
    small_high_ts, small_high = max(((t, c[1]) for t, c in small_candles), key=lambda x: x[1])
    resistance = _project_line(big_high_ts, big_high, small_high_ts, small_high, now)

    out = {"cur": cur_px, "support_line": support, "resistance_line": resistance,
           "support_pts": ((big_low_ts, big_low), (small_low_ts, small_low)),
           "resistance_pts": ((big_high_ts, big_high), (small_high_ts, small_high)),
           "hist_hours": (now - rows[0][0]) / 3600.0}
    out["support_broken"] = support is not None and cur_px < support
    out["resistance_broken"] = resistance is not None and cur_px > resistance
    return out


# ------------- wick-hunt check -- recent sharp spike+reversal + abhi ka bias/whale alignment -------------
WH_WINDOW = 900          # pichhle 15 min me dekho
WH_MIN_SPIKE = 150       # itna chhota spike wick-hunt nahi maanenge
WH_RETRACE_FRAC = 0.5    # spike ka kam se kam itna % wapas aa chuka ho, tabhi "hunt ho chuki" maano


def wick_hunt_check(con, now):
    """Pichhle WH_WINDOW (15 min) me koi sharp spike (wick, SL cluster hunt jaisa) hua aur price
    usse kaafi (WH_RETRACE_FRAC) wapas aa chuka hai kya -- agar haan, AUR abhi ka bias/whale-flow
    us 'wapas' direction ko support kare, tabhi 'tez move aa sakta hai' flag hota hai.
    SACH: heuristic hai -- wick hui iska matlab move GUARANTEED nahi, sirf ek extra confirming
    signal hai jo hunt+alignment dono mile tabhi trigger karta hai."""
    rows = con.execute("SELECT ts,v FROM m WHERE src='binance' AND k='mark' AND ts>? ORDER BY ts",
                       (now - WH_WINDOW,)).fetchall()
    if len(rows) < 10:
        return None
    baseline = rows[0][1]
    cur_ts, cur = rows[-1]
    low_ts, low_val = min(rows, key=lambda r: r[1])
    high_ts, high_val = max(rows, key=lambda r: r[1])

    candidates = []
    if low_ts != cur_ts:
        down_spike = baseline - low_val
        retrace = cur - low_val
        if down_spike >= WH_MIN_SPIKE and retrace >= down_spike * WH_RETRACE_FRAC:
            candidates.append(("NEECHE WICK-HUNT HUI", low_val, down_spike, retrace, "UP"))
    if high_ts != cur_ts:
        up_spike = high_val - baseline
        retrace = high_val - cur
        if up_spike >= WH_MIN_SPIKE and retrace >= up_spike * WH_RETRACE_FRAC:
            candidates.append(("UPAR WICK-HUNT HUI", high_val, up_spike, retrace, "DOWN"))
    if not candidates:
        return None
    kind, extreme, spike, retrace, expect_dir = max(candidates, key=lambda c: c[2])  # bada spike wala lo

    sc, label, _ = bias_calc(con, now)
    conv = whale_conviction(con)
    press_long = sum((x[2] or 0) for x in conv if x[3] == "PRESSING LONG")
    press_short = sum((x[2] or 0) for x in conv if x[3] == "PRESSING SHORT")

    supports = []
    if expect_dir == "UP":
        if sc > 0.5:
            supports.append("bias bullish (%+.1f)" % sc)
        if press_long > 0 and press_long >= press_short * 1.3:
            supports.append("whales long press kar rahe hain")
    else:
        if sc < -0.5:
            supports.append("bias bearish (%+.1f)" % sc)
        if press_short > 0 and press_short >= press_long * 1.3:
            supports.append("whales short press kar rahe hain")

    return {"kind": kind, "extreme": extreme, "spike": spike, "retrace": retrace,
            "expect_dir": expect_dir, "aligned": len(supports) > 0, "supports": supports, "cur": cur}


def trade_plan(con, now):
    px = current_mark(con)
    if px is None:
        return None
    sc, label, _ = bias_calc(con, now)
    near = sr_zones(con, now)
    direction = "LONG" if sc > 0.5 else "SHORT" if sc < -0.5 else None
    rows = con.execute("SELECT v FROM m WHERE src='binance' AND k='mark' AND ts>?", (now - 3600,)).fetchall()
    vol = None
    if len(rows) > 10:
        vals = [r[0] for r in rows]
        mean = sum(vals) / len(vals)
        vol = (sum((x - mean) ** 2 for x in vals) / len(vals)) ** 0.5
    # SL ko pehle se zyada breathing room -- taaki normal wick/noise me pre-time stop na lage
    sl_fallback = max((vol or 0) * 2.2, 220)
    tp_fallback = sl_fallback * 1.3   # pehle 2x tha -- ab chhota, zyada achievable target (win% priority)
    SL_BUFFER = 40  # pehle 20 -- zone ke thoda aage SL, taaki zone ka normal wick SL ko na choo le
    TP_REACH = 0.7  # zone tak pura jaane ke bajaye 70% distance pe TP -- target hit chance zyada
    reliable = [(z, st) for z, st in (near or []) if st["touch"] >= 5]
    support = resistance = None
    for z, st in reliable:
        if z < px and st["up"] / st["touch"] >= 0.5 and (support is None or z > support[0]):
            support = (z, st)
        if z > px and st["down"] / st["touch"] >= 0.5 and (resistance is None or z < resistance[0]):
            resistance = (z, st)
    wick_zones = [(z, st) for z, st in (near or []) if st["touch"] >= 5 and max(st["up"], st["down"]) / st["touch"] >= 0.5]
    plan = {"price": px, "direction": direction, "score": sc, "label": label,
            "reliable": len(reliable), "wick_zones": wick_zones}
    if direction == "LONG":
        plan["entry"] = px
        plan["sl"] = support[0] - SL_BUFFER if support else px - sl_fallback
        if resistance:
            raw_target = resistance[0] - SL_BUFFER
            plan["tp"] = px + (raw_target - px) * TP_REACH
        else:
            plan["tp"] = px + tp_fallback
    elif direction == "SHORT":
        plan["entry"] = px
        plan["sl"] = resistance[0] + SL_BUFFER if resistance else px + sl_fallback
        if support:
            raw_target = support[0] + SL_BUFFER
            plan["tp"] = px - (px - raw_target) * TP_REACH
        else:
            plan["tp"] = px - tp_fallback
    else:
        plan["entry"] = plan["sl"] = plan["tp"] = None
    if plan["entry"] is not None:
        plan["sl_pts"] = abs(plan["entry"] - plan["sl"])
        plan["tp_pts"] = abs(plan["entry"] - plan["tp"])
    return plan


def buy_sell_pressure(con):
    """'Fighting bar' -- abhi ke real BUY flow vs SELL flow ko milake ek bar banata hai.
    Sirf FLOW wala data use karta hai (kitna abhi khareeda/becha ja raha hai), POSITION/OI wala nahi
    (jaise GMX/HL whale long-short) -- wo 'kitna bada stock hai' batata hai, 'abhi kaun jeet raha hai' nahi."""
    L = latest(con)
    g = lambda s, k: (L[(s, k)][0] if (s, k) in L else None)
    buy = sell = 0.0
    got = False
    hb, hs = g("hyperliquid", "big_buy_usd"), g("hyperliquid", "big_sell_usd")
    if hb is not None and hs is not None:
        buy += hb; sell += hs; got = True
    tb, ts_ = g("hyperliquid", "trend_buy_usd"), g("hyperliquid", "trend_sell_usd")
    if tb is not None and ts_ is not None:
        buy += tb; sell += ts_; got = True
    db_, ds_ = g("dydx", "recent_buy_usd"), g("dydx", "recent_sell_usd")
    if db_ is not None and ds_ is not None:
        buy += db_; sell += ds_; got = True
    if not got or (buy + sell) <= 0:
        return None
    pct = buy / (buy + sell) * 100
    return buy, sell, pct


def breakout_check(con, now):
    """Jab price kisi resistance (ASK wall ya SR zone) ke bilkul paas ho, abhi ke flow/wall/whale
    data ko milake ek 'kitna dum hai' composite check deta hai -- SACH: ye koi proven probability
    nahi hai, sirf heuristic hai. Agar price resistance ke paas nahi hai to None deta hai."""
    px = current_mark(con)
    if px is None:
        return None
    L = latest(con)
    g = lambda s, k: (L[(s, k)][0] if (s, k) in L else None)

    aw_px = g("depth", "ask_wall_px")
    near_wall = aw_px is not None and 0 < (aw_px - px) / px < 0.004   # wall 0.4% ke andar upar

    near = sr_zones(con, now)
    zone_res = None
    if near:
        for z, st in near:
            if z >= px and (z - px) / px < 0.006 and st["touch"] >= 5 and st["down"] / st["touch"] >= 0.5:
                zone_res = (z, st)
                break

    if not near_wall and not zone_res:
        return None

    bsp = buy_sell_pressure(con)
    buy_pct = bsp[2] if bsp else None
    ww = wall_watch(con, now)
    wall_status = ww.get("ask", (None,))[0] if ww else None
    conv = whale_conviction(con)
    press_long = sum((x[2] or 0) for x in conv if x[3] == "PRESSING LONG")
    press_short = sum((x[2] or 0) for x in conv if x[3] == "PRESSING SHORT")

    score, reasons = 0, []
    if buy_pct is not None:
        if buy_pct > 55:
            score += 1; reasons.append("buy flow bhaari hai (%.0f%%)" % buy_pct)
        elif buy_pct < 45:
            score -= 1; reasons.append("sell flow bhaari hai (%.0f%%)" % buy_pct)
    if wall_status == "EATEN/CHHOTA HO GAYA":
        score += 1; reasons.append("ask wall chhota ho raha hai")
    elif wall_status == "ABHI BHI KHADA HAI":
        score -= 1; reasons.append("ask wall abhi poora khada hai")
    if press_long > 0 and press_long >= press_short * 1.3:
        score += 1; reasons.append("whales long press kar rahe hain")
    elif press_short > 0 and press_short >= press_long * 1.3:
        score -= 1; reasons.append("whales short press kar rahe hain -- bounce zyada likely")

    if score >= 2:
        verdict, col = "DUM DIKH RAHA HAI -- tod sakta hai", "#4ade80"
    elif score <= -2:
        verdict, col = "KAM DUM -- reject/bounce zyada likely", "#f87171"
    else:
        verdict, col = "MIXED -- abhi 50-50, clear nahi", "#999"

    level = aw_px if near_wall else zone_res[0]
    return {"level": level, "price": px, "verdict": verdict, "col": col, "reasons": reasons}


def realized_vol_pct(con, now, window=3600):
    """SACH: ye rigorous realized-vol formula NAHI hai (log-returns ka proper annualized stdev nahi
    use kiya) -- sirf price-level ka % stdev annualize karke ek 'IV se roughly compare karne layak'
    number banaya hai. Real quant-grade realized vol se alag number aa sakta hai, direction/scale
    samajhne ke liye hi use karo, exact % pe bharosa mat karo."""
    rows = con.execute("SELECT v FROM m WHERE src='binance' AND k='mark' AND ts>?", (now - window,)).fetchall()
    if len(rows) < 10:
        return None
    vals = [r[0] for r in rows]
    mean = sum(vals) / len(vals)
    if mean == 0:
        return None
    sd = (sum((x - mean) ** 2 for x in vals) / len(vals)) ** 0.5
    frac = sd / mean
    periods_per_year = (365 * 24 * 3600) / window
    return frac * (periods_per_year ** 0.5) * 100


def option_sell_view(con, now):
    """EXPERIMENTAL -- option-selling ke liye directional trade_plan() se ALAG nazariya hai:
    yahan dekhna hai (1) range-bound hai ya bada move aane wala hai, (2) agar direction hai to
    kaunsi taraf (CALL ya PUT) bechna 'safer' hai.
    Ab Delta ke options-chain se ATM implied-vol (IV) bhi use karta hai, realized-vol ke against
    compare karne ke liye, aur bias ka pichhle 2hr ka oscillation bhi (factor_log se) -- taaki
    'range tootega ya nahi' ka read thoda zyada data-driven ho.
    SACH -- phir bhi: theta/time-decay, bid-ask spread, aur actual premium pricing IGNORE ho raha
    hai. Isko options-pricing ka substitute mat samjho, sirf ek extra input hai."""
    px = current_mark(con)
    if px is None:
        return None
    sc, label, _ = bias_calc(con, now)
    near = sr_zones(con, now)
    ww = wall_watch(con, now) or {}
    L = latest(con)
    g = lambda s, k: (L[(s, k)][0] if (s, k) in L else None)

    rows = con.execute("SELECT v FROM m WHERE src='binance' AND k='mark' AND ts>?", (now - 3600,)).fetchall()
    vol = None
    if len(rows) > 10:
        vals = [r[0] for r in rows]
        mean = sum(vals) / len(vals)
        vol = (sum((x - mean) ** 2 for x in vals) / len(vals)) ** 0.5

    hist = con.execute("SELECT bias_score FROM factor_log WHERE ts>? AND bias_score IS NOT NULL", (now - 7200,)).fetchall()
    bias_osc = None
    if len(hist) >= 5:
        vals = [h[0] for h in hist]
        m = sum(vals) / len(vals)
        bias_osc = (sum((x - m) ** 2 for x in vals) / len(vals)) ** 0.5

    atm_iv = g("options", "atm_iv")
    days_exp = g("options", "days_to_expiry")
    rv = realized_vol_pct(con, now)

    rb_score, rb_reasons = 0, []
    if abs(sc) < 2:
        rb_score += 1; rb_reasons.append("bias weak/neutral hai (%+.1f)" % sc)
    else:
        rb_score -= 1; rb_reasons.append("bias strong hai (%+.1f) -- directional move risk" % sc)
    bid_status = ww.get("bid", (None,))[0]
    ask_status = ww.get("ask", (None,))[0]
    if bid_status == "ABHI BHI KHADA HAI" and ask_status == "ABHI BHI KHADA HAI":
        rb_score += 1; rb_reasons.append("dono walls (bid+ask) abhi solid khade hain")
    elif bid_status and "EATEN" in bid_status or (ask_status and "EATEN" in ask_status):
        rb_score -= 1; rb_reasons.append("ek wall eaten ho rahi hai -- range toot sakta hai")
    if vol is not None:
        if vol < 150:
            rb_score += 1; rb_reasons.append("pichhle 1hr volatility kam hai (%.0f)" % vol)
        elif vol > 350:
            rb_score -= 1; rb_reasons.append("pichhle 1hr volatility zyada hai (%.0f)" % vol)
    if bias_osc is not None:
        if bias_osc > 1.5:
            rb_score += 1; rb_reasons.append("bias pichhle 2hr me bahut oscillate kar raha hai (%.1f) -- indecision" % bias_osc)
        elif bias_osc < 0.5 and abs(sc) >= 2:
            rb_score -= 1; rb_reasons.append("bias stable + strong hai (osc %.1f) -- trending ho sakta hai" % bias_osc)
    if atm_iv is not None and rv is not None and rv > 0:
        if atm_iv > rv * 1.2:
            rb_score += 1; rb_reasons.append("IV (%.1f%%) realized-vol (%.1f%%) se zyada hai -- premium rich lag raha hai, selling favorable" % (atm_iv, rv))
        elif atm_iv < rv * 0.8:
            rb_score -= 1; rb_reasons.append("IV (%.1f%%) realized-vol (%.1f%%) se kam hai -- market jitna hil raha hai utna priced in nahi, risky" % (atm_iv, rv))

    if rb_score >= 2:
        rb_verdict, rb_col = "RANGE-BOUND zyada likely -- selling ke liye thoda favorable", "#4ade80"
    elif rb_score <= -1:
        rb_verdict, rb_col = "MOVE-RISK zyada -- naked selling abhi risky", "#f87171"
    else:
        rb_verdict, rb_col = "MIXED -- clear nahi", "#999"

    safer_side = ref_level = None
    if sc > 0.5:
        safer_side = "PUT bechna (support ke support pe, price support se neeche na jaaye ye bet)"
        for z, st in (near or []):
            if z < px and st["touch"] >= 5 and st["up"] / st["touch"] >= 0.5 and (ref_level is None or z > ref_level):
                ref_level = z
    elif sc < -0.5:
        safer_side = "CALL bechna (resistance ke upar, price resistance se upar na jaaye ye bet)"
        for z, st in (near or []):
            if z > px and st["touch"] >= 5 and st["down"] / st["touch"] >= 0.5 and (ref_level is None or z < ref_level):
                ref_level = z
    else:
        safer_side = None

    return {"price": px, "bias_score": sc, "rb_score": rb_score, "rb_verdict": rb_verdict,
            "rb_col": rb_col, "rb_reasons": rb_reasons, "safer_side": safer_side, "ref_level": ref_level,
            "vol": vol, "atm_iv": atm_iv, "days_exp": days_exp, "rv": rv, "bias_osc": bias_osc}


def plain_setup(con, now):
    """Sabse upar dikhne wala, EASY-SAMAJH wala summary: whale pressing + bias + SR/volatility
    milake ek seedha-saadha 'setup bun raha hai ya nahi' paragraph banata hai, Entry/SL/TP/wait-time ke saath.
    Ye koi alag calculation nahi hai -- jo niche ke boxes me detail me hai, wahi yahan plain bhasha me hai."""
    conv = whale_conviction(con)
    press_long = sum((x[2] or 0) for x in conv if x[3] == "PRESSING LONG")
    press_short = sum((x[2] or 0) for x in conv if x[3] == "PRESSING SHORT")
    uniq_long = len(set(x[0] for x in conv if x[3] == "PRESSING LONG"))
    uniq_short = len(set(x[0] for x in conv if x[3] == "PRESSING SHORT"))

    sc, label, _ = bias_calc(con, now)

    direction, why, uniq, conf = None, [], 0, "halki"
    if press_short > 0 and press_short >= press_long * 1.3:
        direction, uniq = "SHORT", uniq_short
        why.append("%d whale(s) short badha rahe hain (~$%.1fM combined, PRESSING SHORT)" % (uniq, press_short / 1e6))
    elif press_long > 0 and press_long >= press_short * 1.3:
        direction, uniq = "LONG", uniq_long
        why.append("%d whale(s) long badha rahe hain (~$%.1fM combined, PRESSING LONG)" % (uniq, press_long / 1e6))

    if direction:
        bias_strongly_opposes = (direction == "SHORT" and sc >= 3) or (direction == "LONG" and sc <= -3)
        if bias_strongly_opposes:
            # Ek-do chhote whale ke akele pressing karne se "setup" mat bolo jab poora footprint
            # (ABSORB/BURST/etc, jo bias score banate hain) usse ULTA strongly keh raha ho.
            why.append("%s whale pressing hai, PAR overall footprint bias strongly ulta (%s, score %+.1f) hai -- contradict karta hai, isliye clean setup NAHI maan raha" %
                       (direction, label, sc))
            direction = None
        else:
            bias_agrees = (direction == "SHORT" and sc < -0.5) or (direction == "LONG" and sc > 0.5)
            conf = "zyada" if (bias_agrees and uniq >= 3) else "medium" if (bias_agrees or uniq >= 3) else "halki"
            if bias_agrees:
                why.append("overall bias bhi isi (%s) taraf hai (score %+.1f)" % (direction, sc))
            else:
                why.append("par overall bias (%s, score %+.1f) isse match nahi karta -- mixed signal" % (label, sc))
    if not direction and (sc > 0.5 or sc < -0.5):
        direction = "LONG" if sc > 0 else "SHORT"
        conf = "sirf-bias"
        why.append("koi whale clearly press nahi kar raha, sirf overall footprint bias (score %+.1f) se" % sc)

    plan = trade_plan(con, now) if direction else None
    # trade_plan() apne bias-direction se banta hai -- agar whale-direction alag nikla, to
    # yahan sirf LABEL/entry-price dikhayenge, SL/TP ka number tabhi denge jab dono match karte hon
    # (taaki galat direction ka SL/TP na dikhe).
    sl = tp = sl_pts = tp_pts = None
    if plan and plan.get("direction") == direction:
        sl, tp, sl_pts, tp_pts = plan.get("sl"), plan.get("tp"), plan.get("sl_pts"), plan.get("tp_pts")

    tr, total_setups = track_record(con)

    return {"direction": direction, "conf": conf, "why": why, "price": current_mark(con),
            "sl": sl, "tp": tp, "sl_pts": sl_pts, "tp_pts": tp_pts, "total_setups": total_setups}


def plain_verdict(con, now):
    """SABSE simple, EK-LINE jawab -- 'abhi ye karo' ya 'wait karo', plain bhasha mein, seedhe
    points ke saath. Ye do ALAG system -- plain_setup() (jo whale-pressing+bias se SL/TP banata hai)
    aur smart_money_playbook() (whale entry/liq-cluster wala) -- ko compare karta hai. DONO agree
    karein tabhi confident number deta hai. Disagree karein toh 'mixed' jaisa vague nahi bolta --
    EXACTLY batata hai kaun system kya bol raha hai, taaki samajh aaye confusion kyun hai."""
    ps = plain_setup(con, now)
    smp = smart_money_playbook(con, now)
    ps_dir = ps.get("direction") if ps else None
    smp_votes = {"LONG": 0, "SHORT": 0}
    smp_which = {"LONG": [], "SHORT": []}
    if smp:
        for key in ("scalp", "swing"):
            d = smp.get(key)
            if d:
                smp_votes[d["dir"]] += 1
                smp_which[d["dir"]].append(key.upper())
    smp_dir = "LONG" if smp_votes["LONG"] > smp_votes["SHORT"] else "SHORT" if smp_votes["SHORT"] > smp_votes["LONG"] else None

    if ps_dir and smp_dir and ps_dir == smp_dir and ps.get("sl") is not None:
        return {"state": "GO", "dir": ps_dir, "entry": ps["price"], "sl": ps["sl"], "sl_pts": ps["sl_pts"],
                "tp": ps["tp"], "tp_pts": ps["tp_pts"], "conf": ps["conf"]}
    if ps_dir and smp_dir and ps_dir == smp_dir and ps.get("sl") is None:
        return {"state": "WAIT", "why": "dono system <b>%s</b> pe agree karte hain, PAR exact SL/TP abhi calculate nahi ho paya "
                "(overall bias isse match nahi kar raha) -- direction pe confidence hai, par exact number nahi, thoda wait karo" % ps_dir}
    if ps_dir and smp_dir and ps_dir != smp_dir:
        return {"state": "WAIT", "why": "ek system (SIMPLE SETUP) <b>%s</b> bol raha hai, doosra (SMART MONEY, %s) <b>%s</b> bol raha hai -- dono alag disha, abhi confuse hai, trade mat lo" %
                (ps_dir, "/".join(smp_which[smp_dir]), smp_dir)}
    if ps_dir and not smp_dir:
        return {"state": "WAIT", "why": "SIMPLE SETUP <b>%s</b> bol raha hai, par whale entry/liq-cluster system se koi confirm nahi mila -- akele signal pe bharosa risky hai" % ps_dir}
    if not ps_dir and smp_dir:
        return {"state": "WAIT", "why": "SMART MONEY PLAYBOOK <b>%s</b> (%s) bol raha hai, par overall bias/whale-pressing abhi utna clear nahi -- confirm nahi" % (smp_dir, "/".join(smp_which[smp_dir]))}
    return {"state": "WAIT", "why": "abhi koi clean signal nahi bann raha kisi bhi system se"}


def render():
    con = sqlite3.connect(DB)
    now = time.time()
    L = latest(con)
    g = lambda s, k: (L[(s, k)][0] if (s, k) in L else None)
    age = lambda s, k: (now - L[(s, k)][1]) if (s, k) in L else None

    mark = g("binance", "mark") or g("hyperliquid", "mark") or g("delta", "mark")
    data_age = age("binance", "mark")
    stale = data_age is not None and data_age > 180

    sc, label, parts = bias_calc(con, now)
    colmap = {"BULLISH": "#1b7a3a", "BEARISH": "#b02a2a", "NEUTRAL": "#555"}

    rows_html = []
    detail_html = []   # raw/deep-detail boxes -- collapsible section, bottom of page
    rows_html.append("<div class='price'>BTC $%s</div>" % (("%.0f" % mark) if mark else "--"))
    if stale:
        rows_html.append("<div class='warn'>DATA %.0f MIN PURANA -- collector check karo</div>" % (data_age / 60))

    pv = plain_verdict(con, now)
    if pv:
        if pv["state"] == "GO":
            dcol = "#4ade80" if pv["dir"] == "LONG" else "#f87171"
            lines = ["<div class='hdr' style='color:#ccc'>🎯 YE KARO</div>",
                     "<div style='font-size:24px;font-weight:800;color:%s'>%s lo abhi</div>" % (dcol, pv["dir"]),
                     "<div style='font-size:15px;margin-top:6px'>Entry: <b>$%.0f</b> (abhi ka price)</div>" % pv["entry"],
                     "<div style='font-size:15px;color:#f87171'>SL: $%.0f (%.0f points) -- yahan pahunche to nikal jao, loss accept karo</div>" % (pv["sl"], pv["sl_pts"]),
                     "<div style='font-size:15px;color:#4ade80'>TP: $%.0f (%.0f points) -- yahan pahunche to profit book karo, nikal jao</div>" % (pv["tp"], pv["tp_pts"]),
                     "<div class='small' style='margin-top:8px;color:#aaa'>Confidence: %s. Dono system (Simple Setup + Smart Money Playbook) agree karte hain.</div>" % pv["conf"],
                     "<div class='small' style='color:#666;margin-top:4px'>SACH: ye guarantee nahi hai, sirf 2 system ka agreement hai. Har 15-30 min wapas check karo -- agar signal palat jaaye, SL hit hone se pehle bhi nikal sakte ho.</div>"]
            rows_html.append("<div class='box' style='border:3px solid %s;background:#15201a'>" % dcol + "".join(lines) + "</div>")
        else:
            lines = ["<div class='hdr' style='color:#ccc'>🎯 YE KARO</div>",
                     "<div style='font-size:22px;font-weight:800;color:#999'>ABHI WAIT KARO</div>",
                     "<div class='small' style='margin-top:6px'>" + pv["why"] + "</div>"]
            rows_html.append("<div class='box' style='border:3px solid #444;background:#17171a'>" + "".join(lines) + "</div>")

    mv = master_verdict(con, now)
    if mv:
        lines = ["<div class='hdr' style='color:#ccc'>📋 MASTER VERDICT (detail reasoning -- upar wale 'YE KARO' box ka backend)</div>"]
        if mv["conflict"]:
            for cftext in mv["conflict"]:
                lines.append("<div class='small' style='color:#fbbf24'>⚠ %s</div>" % esc(cftext))
        if mv["dir"]:
            dcol = "#4ade80" if mv["dir"] == "LONG" else "#f87171"
            lines.append("<div style='font-size:19px;font-weight:700;color:%s;margin-top:4px'>%s (score %.1f vs %.1f)</div>" %
                         (dcol, mv["dir"], mv["score_long"] if mv["dir"] == "LONG" else mv["score_short"],
                          mv["score_short"] if mv["dir"] == "LONG" else mv["score_long"]))
            lines.append("<div class='small'>Kyun: " + esc("; ".join(mv["reasons"])) + "</div>")
        else:
            lines.append("<div class='small' style='color:#888;margin-top:4px'>Abhi koi clean consolidated direction nahi -- "
                         "signals mixed/weak hain ya contradict kar rahe. Wait karo.</div>")
        if mv["trap"] and mv["trap"]["trapped_side"]:
            lines.append("<div class='small' style='color:#fbbf24;margin-top:4px'>⚠ Retail %s-heavy hai, top-traders alag hain -- trap-risk.</div>" % mv["trap"]["trapped_side"])
        lines.append("<div class='small' style='color:#666;margin-top:6px'>SACH: ye wick-hunt, exhaustion, smart-money-playbook ka COMBINATION hai, "
                     "alag se koi naya prediction nahi. Confidence = kitne signal agree kiye, statistically-proven probability nahi. Backtest abhi nahi hua.</div>")
        detail_html.append("<div class='box' style='border:1px solid #8b5cf6'>" + "".join(lines) + "</div>")

    wh = wick_hunt_check(con, now)
    if wh and wh["aligned"]:
        wcol = "#4ade80" if wh["expect_dir"] == "UP" else "#f87171"
        rows_html.append(
            "<div class='box' style='border:2px solid %s;background:#1a1a1a'>"
            "<div class='hdr' style='color:%s'>⚡ WICK HUNT ALERT</div>"
            "<div style='font-weight:700;color:%s'>%s (~%.0f pts spike, %.0f pts wapas aa chuka) -- %s taraf tez move ka chance</div>"
            "<div class='small' style='margin-top:4px'>%s</div>"
            "<div class='small' style='color:#666;margin-top:4px'>SACH: heuristic hai, guarantee nahi -- wick hui iska matlab move pakka nahi.</div>"
            "</div>" % (wcol, wcol, wcol, wh["kind"], wh["spike"], wh["retrace"], wh["expect_dir"], esc("; ".join(wh["supports"]))))

    smp = smart_money_playbook(con, now)
    if smp:
        lines = ["<div class='hdr'>🧠 SMART MONEY PLAYBOOK (whale entry/liq-cluster + confluence, SCALP vs SWING)</div>"]
        if smp["trap"] and smp["trap"]["trapped_side"]:
            t = smp["trap"]
            lines.append("<div class='small' style='color:#fbbf24'>⚠ RETAIL TRAP RISK: retail %s-heavy hai (L/S %.2f) jabki top-traders utne nahi (%.2f) -- "
                          "agar iss trade me retail ki crowd follow kar rahe ho, careful raho.</div>" %
                         (t["trapped_side"], t["retail_ls"], t["top_ls"]))
        for horizon, key, lbl in (("scalp", "scalp", "SCALP (15min-1hr)"), ("swing", "swing", "SWING (4hr+)")):
            d = smp.get(key)
            if not d:
                lines.append("<div class='small' style='margin-top:6px;color:#666'>%s: abhi koi clean signal nahi (confluence nahi mila)</div>" % lbl)
                continue
            dcol = "#4ade80" if d["dir"] == "LONG" else "#f87171"
            lines.append("<div style='margin-top:8px'><b>%s</b> -- <span style='color:%s;font-weight:700'>%s</span> (%d signal(s) agree)</div>" %
                         (lbl, dcol, d["dir"], d["votes"]))
            lines.append("<div class='small'>Kyun: " + esc("; ".join(d["reasons"])) + "</div>")
            zparts = []
            if d["entry_zone"]:
                zparts.append("entry-zone (whale cost-basis) ~$%.0f" % d["entry_zone"])
            if d["target_zone"]:
                zparts.append("<span style='color:#4ade80'>target/squeeze-zone ~$%.0f</span>" % d["target_zone"])
            if d["invalid_zone"]:
                zparts.append("<span style='color:#f87171'>invalidation-zone ~$%.0f</span>" % d["invalid_zone"])
            if zparts:
                lines.append("<div class='small' style='margin-top:2px'>" + " | ".join(zparts) + "</div>")
        lines.append("<div class='small' style='color:#666;margin-top:6px'>SACH: entry/target/invalid zones whale entry-price aur LIQ-price ke REAL clusters hain (exchange data se) -- "
                     "'target' ka matlab guarantee nahi, sirf structural zone hai jahan opposite-side whales flush ho sakte hain aur move accelerate ho sakta hai. "
                     "'invalid' zone pe pahunchna matlab isi cohort ke whales flush ho rahe, thesis fail maano. Backtested nahi hai abhi.</div>")
        rows_html.append("<div class='box' style='border:1px solid #8b5cf6'>" + "".join(lines) + "</div>")

    bsp = buy_sell_pressure(con)
    if bsp:
        buy, sell, pct = bsp
        heavier = "BUYERS BHAARI" if pct > 55 else "SELLERS BHAARI" if pct < 45 else "BARABAR (koi heavy nahi)"
        hcol = "#4ade80" if pct > 55 else "#f87171" if pct < 45 else "#999"
        rows_html.append(
            "<div class='box' style='padding:10px 12px'>"
            "<div class='hdr'>FIGHTING BAR (abhi ka real buy vs sell flow)</div>"
            "<div style='display:flex;height:22px;border-radius:5px;overflow:hidden;margin:4px 0'>"
            "<div style='width:%.1f%%;background:#1b7a3a'></div>"
            "<div style='width:%.1f%%;background:#b02a2a'></div>"
            "</div>"
            "<div class='small'>BUY $%.1fM (%.0f%%) &nbsp;vs&nbsp; SELL $%.1fM (%.0f%%)</div>"
            "<div style='font-weight:700;color:%s;margin-top:2px'>%s</div>"
            "</div>" % (pct, 100 - pct, buy / 1e6, pct, sell / 1e6, 100 - pct, hcol, heavier))

        exh = exhaustion_check(con, now)
        if exh and len(exh["signals"]) > 0:
            ecol = "#fbbf24" if exh["exhausted"] else "#666"
            etitle = ("⚖ %s PRESSURE EXHAUST HO RAHA LAG RAHA HAI (%d/4 signals)" % (exh["dominant"], len(exh["signals"]))) if exh["exhausted"] \
                     else ("⚖ EXHAUSTION CHECK -- abhi sirf %d/4 signal, confirm nahi" % len(exh["signals"]))
            lines = ["<div class='hdr' style='color:%s'>%s</div>" % (ecol, etitle)]
            for s in exh["signals"]:
                lines.append("<div class='small'>- %s</div>" % esc(s))
            if exh["exhausted"]:
                lines.append("<div class='small' style='margin-top:4px'>Matlab: %s side ka momentum abhi FRESH/STRONG nahi lag raha -- "
                              "agar isi taraf trade mein ho, yahan se bina kisi NAYE bade player/confirmation ke zyada aggressive mat ho, "
                              "existing trade pe risk tighten karna soch sakte ho.</div>" % exh["dominant"])
            lines.append("<div class='small' style='color:#666;margin-top:4px'>SACH: ye REVERSAL CONFIRM nahi karta -- sirf itna batata hai ki "
                         "momentum fresh nahi lag raha. Price flat bhi reh sakti hai, ulti taraf jaana zaroori nahi.</div>")
            rows_html.append("<div class='box' style='border:1px solid %s'>" % ecol + "".join(lines) + "</div>")

    L2 = latest(con)
    g2 = lambda s, k: (L2[(s, k)][0] if (s, k) in L2 else None)
    bw_px, bw_usd = g2("depth", "bid_wall_px"), g2("depth", "bid_wall_usd")
    aw_px, aw_usd = g2("depth", "ask_wall_px"), g2("depth", "ask_wall_usd")
    if bw_px or aw_px:
        lines = ["<div class='hdr'>ORDER BOOK WALLS (Binance, public -- chhupa bada order kahan khada hai)</div>"]
        if bw_px:
            lines.append("<div class='small' style='color:#4ade80'>BID wall: $%.0f pe $%.2fM (niche support)</div>" % (bw_px, bw_usd / 1e6))
        if aw_px:
            lines.append("<div class='small' style='color:#f87171'>ASK wall: $%.0f pe $%.2fM (upar resistance)</div>" % (aw_px, aw_usd / 1e6))
        ww = wall_watch(con, now)
        if ww:
            for side, (status, px, u0, u1) in ww.items():
                lines.append("<div class='small' style='margin-top:2px'>%s wall (30 min me): <b>%s</b> -- tha $%.2fM, ab $%.2fM</div>" %
                             (side.upper(), status, u0 / 1e6, u1 / 1e6))
        else:
            lines.append("<div class='small' style='color:#666'>(history ke liye abhi kam data hai)</div>")
        sp = spoof_check(con, now)
        if sp:
            for e in sp:
                lines.append("<div class='small' style='color:#fbbf24;margin-top:4px'>⚠ POSSIBLE SPOOF (%s): $%.0f pe $%.2fM wala wall "
                             "achanak $%.2fM reh gaya, bina matching trade-volume ke ($%.0f hi hua) -- lagta hai order EXECUTE nahi hua, "
                             "CANCEL kiya gaya tha</div>" % (e["side"].upper(), e["px"], e["was_usd"] / 1e6, e["now_usd"] / 1e6, e["matching_vol"]))
            lines.append("<div class='small' style='color:#666;margin-top:2px'>SACH: sirf har ~30sec ka poll hai -- isse fast (ms-level) spoofing bilkul nahi dikhegi, sirf slower-type.</div>")
        rows_html.append("<div class='box'>" + "".join(lines) + "</div>")

    bc = breakout_check(con, now)
    if bc:
        lines = ["<div class='hdr'>BREAKOUT CHECK (price resistance $%.0f ke paas hai)</div>" % bc["level"],
                 "<div style='font-weight:700;color:%s'>%s</div>" % (bc["col"], bc["verdict"])]
        if bc["reasons"]:
            lines.append("<div class='small' style='margin-top:4px'>" + esc("; ".join(bc["reasons"])) + "</div>")
        else:
            lines.append("<div class='small' style='color:#666'>abhi koi clear supporting data nahi, MIXED hi hai</div>")
        lines.append("<div class='small' style='color:#666;margin-top:4px'>SACH: ye proven probability NAHI hai, sirf abhi ke flow+wall+whale ko milake ek quick-check hai.</div>")
        rows_html.append("<div class='box' style='border:1px solid #666'>" + "".join(lines) + "</div>")

    tl = trendline_break(con, now)
    if tl:
        near_zones = sr_zones(con, now) or []
        reliable_zones = [(z, st) for z, st in near_zones if st["touch"] >= 5]
        lines = ["<div class='hdr'>TRENDLINE CHECK (auto -- purana bada low/high + recent chota low/high jodke)</div>"]
        if tl["support_broken"]:
            nearest_sup = max((z for z, st in reliable_zones if z < tl["cur"]), default=None)
            extra = " aur ek SR zone $%.0f bhi neeche hai -- dono confirm" % nearest_sup if nearest_sup else " (koi SR zone abhi confirm nahi kar raha, akela trendline break hai)"
            lines.append("<div style='font-weight:700;color:#f87171'>Support trendline (~$%.0f) ke NEECHE close -- bearish break%s</div>" % (tl["support_line"], extra))
        elif tl["resistance_broken"]:
            nearest_res = min((z for z, st in reliable_zones if z > tl["cur"]), default=None)
            extra = " aur ek SR zone $%.0f bhi upar hai -- dono confirm" % nearest_res if nearest_res else " (koi SR zone abhi confirm nahi kar raha, akela trendline break hai)"
            lines.append("<div style='font-weight:700;color:#4ade80'>Resistance trendline (~$%.0f) ke UPAR close -- bullish break%s</div>" % (tl["resistance_line"], extra))
        else:
            lines.append("<div style='color:#999'>Abhi dono trendline (support ~$%.0f, resistance ~$%.0f) ke beech hai -- koi break nahi</div>" %
                         ((tl["support_line"] or 0), (tl["resistance_line"] or 0)))
        if tl["hist_hours"] < 24:
            lines.append("<div class='small' style='color:#666;margin-top:4px'>SACH: abhi sirf %.1f ghante ka data hai -- 'bada timeframe' wala point bhi recent hi hai, trendline weak/short-term hai. Jaise-jaise data badhega, zyada reliable hoga.</div>" % tl["hist_hours"])
        lines.append("<div class='small' style='color:#666;margin-top:4px'>SACH: ye ek automated swing-point choice hai, haath se khinchi trendline se thoda alag ho sakti hai -- koi ek 'sahi' trendline nahi hoti.</div>")
        rows_html.append("<div class='box' style='border:1px solid #666'>" + "".join(lines) + "</div>")

    ov = option_sell_view(con, now)
    if ov:
        lines = ["<div class='hdr'>OPTION SELLING VIEW (EXPERIMENTAL)</div>",
                 "<div style='font-weight:700;color:%s'>%s</div>" % (ov["rb_col"], ov["rb_verdict"])]
        if ov["rb_reasons"]:
            lines.append("<div class='small' style='margin-top:4px'>" + esc("; ".join(ov["rb_reasons"])) + "</div>")
        if ov["safer_side"]:
            lines.append("<div class='small' style='margin-top:6px'><b>Safer side (bias ke hisab se):</b> %s</div>" % esc(ov["safer_side"]))
            if ov["ref_level"]:
                lines.append("<div class='small'>Reference level (SR zone, exact strike nahi): $%.0f</div>" % ov["ref_level"])
        else:
            lines.append("<div class='small' style='margin-top:6px'>Bias bilkul neutral hai -- ek taraf favor karne layak signal nahi, dono taraf risk barabar lag raha hai.</div>")
        if ov["atm_iv"] is not None:
            lines.append("<div class='small' style='margin-top:6px'>ATM IV (nearest expiry%s): %.1f%%</div>" %
                         ((", %.1f din baaki" % ov["days_exp"]) if ov["days_exp"] is not None else "", ov["atm_iv"]))
        if ov["rv"] is not None:
            lines.append("<div class='small'>Realized vol (1hr se andaza, annualized): %.1f%%</div>" % ov["rv"])
        if ov["atm_iv"] is None:
            lines.append("<div class='small' style='color:#666'>(options-chain data abhi nahi mila -- naya venue hai, deploy ke baad check karna)</div>")
        lines.append("<div class='small' style='color:#666;margin-top:6px'>SACH: theta/time-decay, bid-ask spread, actual premium pricing IGNORE ho raha hai -- ye sirf directional+range ka heuristic hai, options-pricing ka substitute nahi. Blindly mat bechna.</div>")
        rows_html.append("<div class='box' style='border:1px solid #666'>" + "".join(lines) + "</div>")

    ps = plain_setup(con, now)
    confcol = {"zyada": "#1b7a3a", "medium": "#8a6d1b", "halki": "#555", "sirf-bias": "#555"}
    dircol = {"LONG": "#4ade80", "SHORT": "#f87171"}
    if ps["direction"]:
        lines = ["<div class='hdr' style='color:#ccc'>SIMPLE SETUP (easy-samajh)</div>",
                 "<div style='font-size:19px;font-weight:700;color:%s'>%s setup banta dikh raha hai -- confidence: %s</div>" %
                 (dircol[ps["direction"]], ps["direction"], ps["conf"])]
        lines.append("<div class='small' style='margin-top:4px'>Kyun: " + esc("; ".join(ps["why"])) + "</div>")
        if ps["price"]:
            lines.append("<div class='small' style='margin-top:6px'>Entry: abhi ka price ~$%.0f</div>" % ps["price"])
        if ps["sl"] is not None:
            lines.append("<div class='small' style='color:#f87171'>SL: $%.0f (%.0f points)</div>" % (ps["sl"], ps["sl_pts"]))
            lines.append("<div class='small' style='color:#4ade80'>TP: $%.0f (%.0f points)</div>" % (ps["tp"], ps["tp_pts"]))
        else:
            lines.append("<div class='small' style='color:#888'>SL/TP abhi nahi dikha raha -- whale-direction aur overall-bias match nahi kar rahe, mixed hai</div>")
        lines.append("<div class='small' style='margin-top:6px'><b>Entry ABHI ka price hai</b> -- entry ke liye wait mat karo, is box ka matlab hi hai ki ye ABHI ho raha hai.</div>")
        lines.append("<div class='small'>Entry ke BAAD: har 15-30 min isi page ko wapas check karte raho. Agar wahi whale(s) covering/exit dikhne lage ya naya PRESSING ulti taraf aa jaye, thesis khatam samjho -- SL hit hone se pehle bhi nikal sakte ho.</div>")
        warn = " -- isse bahut kam hai, abhi risk mat lena sirf isi pe" if ps["total_setups"] < 20 else ""
        lines.append("<div class='small' style='color:#666;margin-top:4px'>Humara apna track-record system abhi tak %d setups ne test kiya hai%s.</div>" % (ps["total_setups"], warn))
        detail_html.append("<div class='box' style='border:1px solid %s'>" % confcol[ps["conf"]] + "".join(lines) + "</div>")
    else:
        detail_html.append("<div class='box'><div class='hdr'>SIMPLE SETUP</div><div class='small'>Abhi koi clear setup nahi bun raha -- na whale pressing hai na bias strong hai. Wait karo.</div></div>")

    rows_html.append("<div class='bias' style='background:%s'>FOOTPRINT BIAS: %s (score %+.1f)<br><span class='small'>%s</span><br><span class='small'>confidence LOW, weights untested</span></div>" %
                     (colmap.get(label, "#555"), label, sc, esc(", ".join(parts) if parts else "koi footprint nahi")))

    cp = g("derived", "coinbase_premium_usd")
    tp, rl = g("binance", "top_pos_ls"), g("binance", "retail_ls")
    wl, ws_ = g("hyperliquid", "whale_long_usd"), g("hyperliquid", "whale_short_usd")
    extra = []
    if cp is not None:
        extra.append("Coinbase premium: %+.0f USD" % cp)
    if tp and rl:
        extra.append("Top L/S %.2f vs retail %.2f" % (tp, rl))
    if wl is not None and ws_ is not None and (wl + ws_) > 0:
        extra.append("HL whales: long $%.1fM / short $%.1fM" % (wl / 1e6, ws_ / 1e6))
    if extra:
        rows_html.append("<div class='box'>" + "<br>".join(esc(x) for x in extra) + "</div>")

    ev = q(con, "SELECT ts,venue,kind,side,note FROM e WHERE ts>? ORDER BY ts DESC LIMIT 12", (int(now) - 1800,))
    if ev:
        lines = []
        for ts, venue, kind, side, note in ev:
            cls = "up" if side == "BULL" else "dn"
            lines.append("<div class='ev %s'>%s <b>%s</b> %s %s -- %s</div>" %
                         (cls, time.strftime("%H:%M", time.localtime(ts)), venue, kind, side, esc(note)))
        detail_html.append("<div class='box'><div class='hdr'>Pichhle 30 min events</div>" + "".join(lines) + "</div>")
    else:
        detail_html.append("<div class='box small'>Pichhle 30 min me koi event nahi</div>")

    cw = q(con, "SELECT addr,label,bought,sold FROM c WHERE ts=(SELECT MAX(ts) FROM c) ORDER BY (bought+sold) DESC LIMIT 6")
    if cw:
        lines = ["<div class='hdr'>Hyperliquid wallets</div>"]
        for a, lab, b_, s_ in cw:
            lines.append("<div class='wrow'>%s..%s <b>%s</b> B $%.1fM / S $%.1fM</div>" % (a[:6], a[-4:], lab, b_ / 1e6, s_ / 1e6))
        detail_html.append("<div class='box'>" + "".join(lines) + "</div>")

    bp = biggest_player(con, now)
    if bp:
        a, side, szi, entry, liq, usd, upnl = bp["addr"], bp["side"], bp["szi"], bp["entry"], bp["liq"], bp["usd"], bp["upnl"]
        lines = ["<div class='hdr'>SABSE TAGDA KHILADI (single biggest live position)</div>"]
        pnl_txt = ""
        if upnl is not None:
            pnl_cls = "up" if upnl >= 0 else "dn"
            pnl_txt = " -- <span class='%s'>uPNL $%.0f</span>" % (pnl_cls, upnl)
        lines.append("<div class='wrow'>%s..%s <b>%s</b> %.2f BTC @ %.0f ($%.1fM)%s</div>" %
                     (a[:6], a[-4:], side, abs(szi), entry or 0, usd / 1e6, pnl_txt))
        if bp["liq"] and bp["push_dir"]:
            lines.append("<div class='small'>Agar ye liquidate hua, price <b>%s</b> ki taraf dhakka khayega (cascade) -- "
                          "LIQ $%.0f (abhi se %.1f%% door)</div>" % (bp["push_dir"], bp["liq"], bp["push_dist_pct"]))
        tparts = []
        for label in ("15min", "1hr", "4hr"):
            d = bp["trend"].get(label)
            if d is None:
                tparts.append("%s: data nahi" % label)
            elif d > 3:
                tparts.append("%s: <span class='up'>+%.1f%% BADH raha (pressing)</span>" % (label, d))
            elif d < -3:
                tparts.append("%s: <span class='dn'>%.1f%% GHAT raha (unwind/profit-booking)</span>" % (label, d))
            else:
                tparts.append("%s: ~flat" % label)
        lines.append("<div class='small' style='margin-top:4px'>Size trend: " + " | ".join(tparts) + "</div>")
        lines.append("<div class='small' style='color:#666;margin-top:4px'>SACH: 'kab exact profit-booking hogi' koi predict "
                      "nahi kar sakta -- ye sirf ABHI TAK size badh raha hai ya ghat raha hai wahi dikha raha hai (real DB data se, "
                      "guess nahi). Agar GHAT raha hai, matlab wo khilada already nikalna shuru kar chuka hai.</div>")
        rows_html.append("<div class='box'>" + "".join(lines) + "</div>")

    top = q(con, "SELECT addr,szi,entry,usd,liq FROM w WHERE ts=(SELECT MAX(ts) FROM w) ORDER BY usd DESC LIMIT 5")
    if top:
        cur_px = current_mark(con)
        lines = ["<div class='hdr'>Top whale positions</div>"]
        for a, szi, e, usd, liq in top:
            side = "LONG" if szi > 0 else "SHORT"
            cls = "up" if szi > 0 else "dn"
            base = "<div class='wrow %s'>%s..%s %s %.2f BTC @ %.0f ($%.1fM)" % (cls, a[:6], a[-4:], side, abs(szi), e or 0, usd / 1e6)
            if liq:
                dist_txt = ""
                if cur_px:
                    dist_pct = abs(cur_px - liq) / cur_px * 100
                    dist_txt = " -- abhi se %.1f%% door" % dist_pct
                base += " -- <span style='color:#f87171'>LIQ $%.0f</span>%s" % (liq, dist_txt)
            lines.append(base + "</div>")
        lines.append("<div class='small' style='color:#666;margin-top:4px'>LIQ = jahan ye position forced-close hogi -- us point pe exchange khud market order fire karta hai, jo price ko us hi disha me aur dhakka de sakta hai (cascade ka risk).</div>")
        detail_html.append("<div class='box'>" + "".join(lines) + "</div>")

    conv = whale_conviction(con)
    if conv:
        lines = ["<div class='hdr'>Whale conviction (position+flow, dedup)</div>"]
        for a, side, usd, verdict, cls, tier in conv:
            lines.append("<div class='wrow %s'>[%s] %s..%s %s $%.1fM -> <b>%s</b></div>" % (cls, tier, a[:6], a[-4:], side, (usd or 0) / 1e6, verdict))
        uniq = len(set(x[0] for x in conv))
        if uniq < 3:
            lines.append("<div class='small' style='color:#f59e0b'>SAVDHAN: sirf %d alag wallet(s) se, 'market' mat samjho, chhota sample</div>" % uniq)
        detail_html.append("<div class='box'>" + "".join(lines) + "</div>")

    radar = entry_radar(con)
    if radar:
        lines = ["<div class='hdr'>Tier entry radar ($ size se Pig..BigDaddy, meri apni bucket)</div>"]
        for name, n, net, buy, sell in radar:
            cls = "up" if net > 0 else "dn" if net < 0 else ""
            dirn = "GHUS RAHA (buy)" if net > 0 else "NIKAL/SHORT (sell)" if net < 0 else "balanced"
            lines.append("<div class='wrow %s'>%s (%d wallets) net %+.0f -> %s</div>" % (cls, name, n, net, dirn))
        detail_html.append("<div class='box'>" + "".join(lines) + "</div>")

    plan = trade_plan(con, now)
    if plan:
        lines = ["<div class='hdr'>Trade plan (DRAFT, khud-banaya, backtested nahi)</div>"]
        if plan["direction"] is None:
            lines.append("<div class='small'>Bias neutral/weak (score %+.1f) -- koi clean plan nahi</div>" % plan["score"])
        else:
            lines.append("<div class='small'>Bias: %s (score %+.1f) -> %s</div>" % (plan["label"], plan["score"], plan["direction"]))
            lines.append("<div class='small'>Entry: $%.0f</div>" % plan["entry"])
            lines.append("<div class='small' style='color:#f87171'>SL: $%.0f (%.0f pts) %s</div>" %
                         (plan["sl"], plan["sl_pts"], "" if plan["reliable"] else "-- volatility fallback"))
            lines.append("<div class='small' style='color:#4ade80'>TP: $%.0f (%.0f pts)</div>" % (plan["tp"], plan["tp_pts"]))
            if plan["wick_zones"]:
                lines.append("<div class='small' style='margin-top:4px'>Wick/reversal zones:</div>")
                for z, st in sorted(plan["wick_zones"], key=lambda kv: abs(kv[0] - plan["price"])):
                    lines.append("<div class='small'>$%.0f : up %.0f%% / down %.0f%% (%d touch)</div>" %
                                 (z, st["up"] / st["touch"] * 100, st["down"] / st["touch"] * 100, st["touch"]))
            lines.append("<div class='small' style='color:#666;margin-top:4px'>SACH: 1-2 din ka data, backtest nahi hua. Blindly order mat daalo.</div>")
        detail_html.append("<div class='box'>" + "".join(lines) + "</div>")

    pw = pig_vs_whale(con)
    if pw:
        pig_net, whale_net, diverge = pw
        lines = ["<div class='hdr'>Sab tiers net flow (PIG se BIGDADDY tak)</div>"]
        radar_all = entry_radar(con)
        if radar_all:
            for name, n, net, b_, s_ in radar_all:
                color = "#4ade80" if net > 0 else ("#f87171" if net < 0 else "#999")
                lines.append("<div class='small' style='color:%s'>%s (%d wallets) net %+.0f USD</div>" %
                             (color, name, n, net))
        else:
            lines.append("<div class='small'>Pig net flow: %+.0f USD</div>" % pig_net)
            if whale_net is not None:
                lines.append("<div class='small'>Whale net flow: %+.0f USD</div>" % whale_net)
        if diverge:
            lines.append("<div class='small' style='color:#f59e0b;margin-top:4px'>PIG aur WHALE ulti disha me hain -- dhyan layak, proof nahi</div>")
        detail_html.append("<div class='box'>" + "".join(lines) + "</div>")

    tr, total_setups = track_record(con)
    lines = ["<div class='hdr'>Track record (3+ wallet confluence, khud test)</div>"]
    if total_setups == 0:
        lines.append("<div class='small'>abhi koi setup log nahi hua</div>")
    else:
        for win, (h, m, fl, n) in tr.items():
            if n == 0:
                lines.append("<div class='small'>%s: abhi evaluate hone wala koi nahi</div>" % win)
            else:
                warn = " -- bahut kam sample" if n < 20 else ""
                lines.append("<div class='small'>%s: %d HIT / %d MISS / %d FLAT (%d eval, %.0f%% hit)%s</div>" % (win, h, m, fl, n, h / n * 100, warn))
        lines.append("<div class='small' style='color:#666'>total setups log hue: %d</div>" % total_setups)
    detail_html.append("<div class='box'>" + "".join(lines) + "</div>")

    if detail_html:
        rows_html.append("<details class='detail'><summary>Zyada detail / raw data (%d boxes) -- click karo</summary>"
                         "<div class='detail-inner'>" % len(detail_html) + "".join(detail_html) + "</div></details>")

    con.close()
    return "\n".join(rows_html), stale


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Footprint Live</title>
<style>
*{box-sizing:border-box}
body{background:#0a0a0d;color:#e5e5e5;font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:16px;max-width:1400px}
#app{column-width:420px;column-gap:16px}
.price{font-size:28px;font-weight:700;margin:2px 0 12px 0;letter-spacing:-.5px;break-inside:avoid;-webkit-column-break-inside:avoid}
.warn{background:#7f1d1d;padding:8px 12px;border-radius:8px;margin-bottom:10px;font-size:13px;break-inside:avoid;-webkit-column-break-inside:avoid}
.bias{padding:10px 12px;border-radius:8px;margin-bottom:10px;line-height:1.5;break-inside:avoid;-webkit-column-break-inside:avoid;font-size:13px}
.box{background:#151518;border:1px solid #232328;border-radius:10px;padding:11px 13px;margin-bottom:12px;break-inside:avoid;-webkit-column-break-inside:avoid}
.box.primary{background:#17171d;padding:13px 15px}
.hdr{color:#8a8a93;font-size:11px;text-transform:uppercase;letter-spacing:.4px;margin-bottom:7px;font-weight:600}
.small{font-size:12px;opacity:.82;line-height:1.45}
.ev{font-size:12.5px;padding:3px 0;border-bottom:1px solid #1e1e22}
.ev.up{color:#4ade80}
.ev.dn{color:#f87171}
.wrow{font-size:12.5px;padding:2.5px 0}
.wrow.up{color:#4ade80}
.wrow.dn{color:#f87171}
.up{color:#4ade80}
.dn{color:#f87171}
.foot{font-size:11px;color:#555;margin-top:16px;text-align:center}
details.detail{column-span:all;margin-top:6px;padding-top:14px;border-top:1px solid #1e1e22}
details.detail>summary{cursor:pointer;color:#777;font-size:12px;text-transform:uppercase;letter-spacing:.5px;padding:6px 0;list-style:none;user-select:none}
details.detail>summary::-webkit-details-marker{display:none}
details.detail>summary:before{content:"▸ ";color:#555}
details.detail[open]>summary:before{content:"▾ "}
details.detail>summary:hover{color:#aaa}
.detail-inner{column-width:420px;column-gap:16px;margin-top:10px}
</style></head>
<body>
<div id="app">Loading...</div>
<div class="foot">Sirf public data ka andaza hai, saboot nahi. Khud order mat lagao isi par.<br>Har 4s refresh</div>
<script>
async function tick(){
  try{
    const r = await fetch('/data');
    document.getElementById('app').innerHTML = await r.text();
  }catch(e){
    document.getElementById('app').innerHTML = "<div class='warn'>Server se connect nahi ho raha</div>";
  }
}
tick();
setInterval(tick, 4000);
</script>
</body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/data"):
            try:
                body, _ = render()
            except Exception as e:
                body = "<div class='warn'>Error: %s</div>" % esc(e)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(PAGE.encode())


if __name__ == "__main__":
    print("Footprint dashboard http://0.0.0.0:%d (market.db=%s)" % (PORT, DB))
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
