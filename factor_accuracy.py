#!/usr/bin/env python3
"""
factor_accuracy.py -- har bias_calc() factor ki ALAG-ALAG accuracy nikalta hai,
'factor_log' table se (jo market_collector.py ab har ~2 min me ek row daalta hai).

SACH: ye sirf tabhi kaam ka hai jab factor_log me kaafi rows ho (kam se kam
kuch sau, ideally 1000+, matlab kai din ka chalna). Shuru me 'SAMPLE BAHUT
KAM' dikhega -- usko IGNORE karo, result pe bharosa mat karo jab tak
sample size genuinely bada na ho.

Chalane ka tareeka (server pe):
    cd ~/whale && python3 factor_accuracy.py
"""
import sqlite3
import sys

DB = "whale.db"

FACTORS = [
    ("hl_trend", "Hyperliquid trend-wallet net ($)"),
    ("burst_cnt", "BURST events net (15min)"),
    ("absorb_cnt", "ABSORB events net (15min)"),
    ("slice_cnt", "SLICE events net (15min)"),
    ("coinbase_premium", "Coinbase premium ($)"),
    ("dydx_net", "dYdX recent buy-sell net ($)"),
    ("gmx_net", "GMX long-short OI net ($)"),
    ("orderbook_imb", "Order-book bid-ask imbalance ($)"),
    ("bias_score", "Combined bias score (sab mila ke)"),
]

HORIZONS = [("px15", "15 min"), ("px30", "30 min"), ("px60", "60 min")]

MIN_SAMPLE_FOR_TRUST = 100   # isse kam ho to result mat maano


def main():
    con = sqlite3.connect(sys.argv[1] if len(sys.argv) > 1 else DB)
    total = con.execute("SELECT COUNT(*) FROM factor_log").fetchone()[0]
    print("factor_log me total rows: %d" % total)
    if total < 30:
        print("Itna kam data hai ki koi analysis karna bhi beimani hogi. Thodi der/din chalne do.")
        return
    print()

    for col, desc in FACTORS:
        print("=== %s (%s) ===" % (desc, col))
        for pxcol, label in HORIZONS:
            rows = con.execute(
                "SELECT %s, price, %s FROM factor_log WHERE %s IS NOT NULL AND %s IS NOT NULL AND %s != 0"
                % (col, pxcol, col, pxcol, col)
            ).fetchall()
            n = len(rows)
            if n == 0:
                print("  %s: koi data nahi abhi" % label)
                continue
            hit = 0
            for fval, px0, px1 in rows:
                moved_up = (px1 - px0) > 0
                factor_up = fval > 0
                if moved_up == factor_up:
                    hit += 1
            acc = hit / n * 100
            flag = "" if n >= MIN_SAMPLE_FOR_TRUST else "  [SAMPLE BAHUT KAM -- bharosa mat karo]"
            print("  %s: %d/%d sahi direction match (%.1f%%)%s" % (label, hit, n, acc, flag))
        print()

    print("---")
    print("Interpretation: 50%% ke aas-paas matlab factor ka koi real predictive value nahi (coin-flip jaisa).")
    print("Jo factor consistently 55-60%%+ dikhaye (bade sample ke saath), wahi genuinely kaam ka hai --")
    print("baaki ko bias_calc() me kam weight de do ya hata do.")


if __name__ == "__main__":
    main()
