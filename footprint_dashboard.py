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
        rows = con.execute("SELECT eval15,eval30,eval60 FROM setups").fetchall()
    except Exception:
        return {}, 0
    out = {}
    for i, col in enumerate(("15min", "30min", "60min")):
        vals = [r[i] for r in rows if r[i] is not None]
        h, m, fl = vals.count("HIT"), vals.count("MISS"), vals.count("FLAT")
        out[col] = (h, m, fl, len(vals))
    return out, len(rows)


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
        out.append((addr, "LONG" if pos_long else "SHORT", usd, verdict, cls))
    return out


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
        for a, side, usd, verdict, cls in conv:
            lines.append("<div class='wrow %s'>%s..%s %s $%.1fM -> <b>%s</b></div>" % (cls, a[:6], a[-4:], side, (usd or 0) / 1e6, verdict))
        uniq = len(set(x[0] for x in conv))
        if uniq < 3:
            lines.append("<div class='small' style='color:#f59e0b'>SAVDHAN: sirf %d alag wallet(s) se, 'market' mat samjho, chhota sample</div>" % uniq)
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
<div class="foot">Sirf public data ka andaza hai, saboot nahi. Khud order mat lagao isi par.<br>Har 10s refresh</div>
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
setInterval(tick, 10000);
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
