#!/usr/bin/env python3
"""
trendline_accuracy.py -- trendline_log se dekhta hai ki 4 "chota-timeframe-window"
variants (1h/4h/8h/24h) me se KAUNSA genuinely sahi direction predict karta hai.

SACH: ye sirf tabhi kaam ka hai jab trendline_log me kaafi rows ho (kam se kam
kuch sau 'break' events har variant ke liye, ideally 100+). Shuru me 'SAMPLE
BAHUT KAM' dikhega -- usko IGNORE karo, result pe bharosa mat karo jab tak
sample size genuinely bada na ho.

Chalane ka tareeka (server pe):
    cd ~/whale && python3 trendline_accuracy.py
"""
import sqlite3
import sys

DB = "whale.db"
VARIANTS = ["1h", "4h", "8h", "24h"]
HORIZONS = [("px15", "15 min"), ("px30", "30 min"), ("px60", "60 min")]
MIN_SAMPLE_FOR_TRUST = 100


def main():
    con = sqlite3.connect(sys.argv[1] if len(sys.argv) > 1 else DB)
    total = con.execute("SELECT COUNT(*) FROM trendline_log").fetchone()[0]
    print("trendline_log me total rows: %d" % total)
    if total < 40:
        print("Itna kam data hai ki koi analysis karna bhi beimani hogi. Thodi der/din chalne do.")
        return
    print()

    for variant in VARIANTS:
        print("=== chota-window: %s ===" % variant)

        # SUPPORT break -> expect price GIRE (bearish continuation)
        for pxcol, label in HORIZONS:
            rows = con.execute(
                "SELECT price, %s FROM trendline_log WHERE variant=? AND support_broken=1 "
                "AND %s IS NOT NULL" % (pxcol, pxcol), (variant,)
            ).fetchall()
            n = len(rows)
            if n == 0:
                print("  [support break] %s: koi data nahi abhi" % label)
                continue
            hit = sum(1 for px0, px1 in rows if px1 < px0)
            acc = hit / n * 100
            flag = "" if n >= MIN_SAMPLE_FOR_TRUST else "  [SAMPLE BAHUT KAM -- bharosa mat karo]"
            print("  [support break -> expect GIRE] %s: %d/%d sahi (%.1f%%)%s" % (label, hit, n, acc, flag))

        # RESISTANCE break -> expect price CHADHE (bullish continuation)
        for pxcol, label in HORIZONS:
            rows = con.execute(
                "SELECT price, %s FROM trendline_log WHERE variant=? AND resistance_broken=1 "
                "AND %s IS NOT NULL" % (pxcol, pxcol), (variant,)
            ).fetchall()
            n = len(rows)
            if n == 0:
                print("  [resistance break] %s: koi data nahi abhi" % label)
                continue
            hit = sum(1 for px0, px1 in rows if px1 > px0)
            acc = hit / n * 100
            flag = "" if n >= MIN_SAMPLE_FOR_TRUST else "  [SAMPLE BAHUT KAM -- bharosa mat karo]"
            print("  [resistance break -> expect CHADHE] %s: %d/%d sahi (%.1f%%)%s" % (label, hit, n, acc, flag))
        print()

    print("---")
    print("Interpretation: 50%% ke aas-paas matlab us variant ka break genuinely kuch predict")
    print("nahi karta (coin-flip jaisa). Jo variant consistently 55-60%%+ dikhaye (bade sample ke")
    print("saath), wahi genuinely kaam ka hai -- dashboard ka default small_lookback usi pe set")
    print("kar dena (footprint_dashboard.py me trendline_break() ka small_lookback parameter).")


if __name__ == "__main__":
    main()
