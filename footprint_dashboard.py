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
    label = "BULLISH" if sc >= 2 else "BEARISH" if sc <= -2 else "NEUTRAL"
    return sc, label, parts


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
    sl_fallback = max((vol or 0) * 1.5, 150)
    tp_fallback = sl_fallback * 2
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
        plan["sl"] = support[0] - 20 if support else px - sl_fallback
        plan["tp"] = resistance[0] - 20 if resistance else px + tp_fallback
    elif direction == "SHORT":
        plan["entry"] = px
        plan["sl"] = resistance[0] + 20 if resistance else px + sl_fallback
        plan["tp"] = support[0] + 20 if support else px - tp_fallback
    else:
        plan["entry"] = plan["sl"] = plan["tp"] = None
    if plan["entry"] is not None:
        plan["sl_pts"] = abs(plan["entry"] - plan["sl"])
        plan["tp_pts"] = abs(plan["entry"] - plan["tp"])
    return plan


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
    rows_html.append("<div class='price'>BTC $%s</div>" % (("%.0f" % mark) if mark else "--"))
    if stale:
        rows_html.append("<div class='warn'>DATA %.0f MIN PURANA -- collector check karo</div>" % (data_age / 60))

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
        rows_html.append("<div class='box' style='border:1px solid %s'>" % confcol[ps["conf"]] + "".join(lines) + "</div>")
    else:
        rows_html.append("<div class='box'><div class='hdr'>SIMPLE SETUP</div><div class='small'>Abhi koi clear setup nahi bun raha -- na whale pressing hai na bias strong hai. Wait karo.</div></div>")

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
        rows_html.append("<div class='box'><div class='hdr'>Pichhle 30 min events</div>" + "".join(lines) + "</div>")
    else:
        rows_html.append("<div class='box small'>Pichhle 30 min me koi event nahi</div>")

    cw = q(con, "SELECT addr,label,bought,sold FROM c WHERE ts=(SELECT MAX(ts) FROM c) ORDER BY (bought+sold) DESC LIMIT 6")
    if cw:
        lines = ["<div class='hdr'>Hyperliquid wallets</div>"]
        for a, lab, b_, s_ in cw:
            lines.append("<div class='wrow'>%s..%s <b>%s</b> B $%.1fM / S $%.1fM</div>" % (a[:6], a[-4:], lab, b_ / 1e6, s_ / 1e6))
        rows_html.append("<div class='box'>" + "".join(lines) + "</div>")

    top = q(con, "SELECT addr,szi,entry,usd FROM w WHERE ts=(SELECT MAX(ts) FROM w) ORDER BY usd DESC LIMIT 5")
    if top:
        lines = ["<div class='hdr'>Top whale positions</div>"]
        for a, szi, e, usd in top:
            side = "LONG" if szi > 0 else "SHORT"
            cls = "up" if szi > 0 else "dn"
            lines.append("<div class='wrow %s'>%s..%s %s %.2f BTC @ %.0f ($%.1fM)</div>" % (cls, a[:6], a[-4:], side, abs(szi), e or 0, usd / 1e6))
        rows_html.append("<div class='box'>" + "".join(lines) + "</div>")

    conv = whale_conviction(con)
    if conv:
        lines = ["<div class='hdr'>Whale conviction (position+flow, dedup)</div>"]
        for a, side, usd, verdict, cls, tier in conv:
            lines.append("<div class='wrow %s'>[%s] %s..%s %s $%.1fM -> <b>%s</b></div>" % (cls, tier, a[:6], a[-4:], side, (usd or 0) / 1e6, verdict))
        uniq = len(set(x[0] for x in conv))
        if uniq < 3:
            lines.append("<div class='small' style='color:#f59e0b'>SAVDHAN: sirf %d alag wallet(s) se, 'market' mat samjho, chhota sample</div>" % uniq)
        rows_html.append("<div class='box'>" + "".join(lines) + "</div>")

    radar = entry_radar(con)
    if radar:
        lines = ["<div class='hdr'>Tier entry radar ($ size se Pig..BigDaddy, meri apni bucket)</div>"]
        for name, n, net, buy, sell in radar:
            cls = "up" if net > 0 else "dn" if net < 0 else ""
            dirn = "GHUS RAHA (buy)" if net > 0 else "NIKAL/SHORT (sell)" if net < 0 else "balanced"
            lines.append("<div class='wrow %s'>%s (%d wallets) net %+.0f -> %s</div>" % (cls, name, n, net, dirn))
        rows_html.append("<div class='box'>" + "".join(lines) + "</div>")

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
        rows_html.append("<div class='box'>" + "".join(lines) + "</div>")

    pw = pig_vs_whale(con)
    if pw:
        pig_net, whale_net, diverge = pw
        lines = ["<div class='hdr'>Chhote players (pig-tier) vs Whale</div>",
                 "<div class='small'>Pig net flow: %+.0f USD</div>" % pig_net]
        if whale_net is not None:
            lines.append("<div class='small'>Whale net flow: %+.0f USD</div>" % whale_net)
        if diverge:
            lines.append("<div class='small' style='color:#f59e0b'>ULTI disha me hain -- dhyan layak, proof nahi</div>")
        rows_html.append("<div class='box'>" + "".join(lines) + "</div>")

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
    rows_html.append("<div class='box'>" + "".join(lines) + "</div>")

    con.close()
    return "\n".join(rows_html), stale


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Footprint Live</title>
<style>
body{background:#0b0b0f;color:#eee;font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:12px;max-width:480px}
.price{font-size:30px;font-weight:700;margin:4px 0}
.warn{background:#b02a2a;padding:6px 10px;border-radius:6px;margin-bottom:8px;font-size:13px}
.bias{padding:10px 12px;border-radius:8px;margin-bottom:10px;line-height:1.5}
.box{background:#17171c;border-radius:8px;padding:10px 12px;margin-bottom:10px}
.hdr{color:#888;font-size:12px;text-transform:uppercase;margin-bottom:6px}
.small{font-size:12px;opacity:.85}
.ev{font-size:13px;padding:3px 0;border-bottom:1px solid #222}
.ev.up{color:#4ade80}
.ev.dn{color:#f87171}
.wrow{font-size:13px;padding:3px 0}
.wrow.up{color:#4ade80}
.wrow.dn{color:#f87171}
.foot{font-size:11px;color:#666;margin-top:12px;text-align:center}
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
