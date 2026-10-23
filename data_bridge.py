#!/usr/bin/env python3
"""
data_bridge.py -- tere server ka jama kiya market data GitHub repo par bhejta hai, taaki Claude wahan se padh sake.

market_collector.py ke saath chalta hai (wo market.db banata hai, ye usko CSV me badal ke git push karta hai).

Zaroori env vars:
   REPO_URL = https://github.com/<user>/<repo>.git     (repo PUBLIC rakho taaki Claude padh sake)
   GH_TOKEN = fine-grained token (sirf us ek repo ka "Contents: Read and write")
Token chat me MAT bhejna. Sirf server par env me rakho.

Chalane ke tareeke:
   python3 data_bridge.py            # har 10 minute me push
   python3 data_bridge.py once       # ek baar push karke ruk jao
   python3 data_bridge.py selftest   # local nakli repo par push/pull ka asli test
"""
import os, sys, csv, time, sqlite3, subprocess, shutil, tempfile

DB = os.getenv("DB", "market.db")
WORK = os.path.expanduser(os.getenv("BRIDGE_DIR", "~/whale/bridge_repo"))
EVERY = 600
STATE = ".bridge_state"   # repo ke andar nahi, WORK ke bahar nahi: last pushed ts


def sh(*a, cwd=None, check=True):
    r = subprocess.run(a, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError("%s -> %s" % (" ".join(a[:3]), (r.stderr or r.stdout).strip()[:300]))
    return r.stdout.strip()


def remote_url():
    url, tok = os.getenv("REPO_URL"), os.getenv("GH_TOKEN")
    if not url:
        raise SystemExit("REPO_URL set karo")
    if tok and url.startswith("https://"):
        url = url.replace("https://", "https://x-access-token:%s@" % tok, 1)
    return url


def ensure_repo(url):
    if not os.path.isdir(os.path.join(WORK, ".git")):
        os.makedirs(os.path.dirname(WORK), exist_ok=True)
        sh("git", "clone", url, WORK)
    sh("git", "config", "user.name", "data-bridge", cwd=WORK)
    sh("git", "config", "user.email", "bridge@localhost", cwd=WORK)
    sh("git", "remote", "set-url", "origin", url, cwd=WORK)
    sh("git", "pull", "--rebase", "--autostash", "-q", cwd=WORK, check=False)


def export(db_path, repo):
    sp = os.path.join(repo, ".state")
    last = {"m": 0, "w": 0, "e": 0, "c": 0}
    if os.path.exists(sp):
        for line in open(sp):
            k, v = line.split()
            last[k] = int(v)
    con = sqlite3.connect(db_path)
    os.makedirs(os.path.join(repo, "data"), exist_ok=True)
    n = 0
    for tbl, cols in (("m", "ts,src,k,v"), ("w", "ts,addr,szi,entry,liq,upnl,lev,usd,flow"),
                      ("e", "ts,venue,kind,side,a,b,note"), ("c", "ts,addr,label,bought,sold,maker_n,taker_n,n")):
        try:
            rows = con.execute("SELECT %s FROM %s WHERE ts>? ORDER BY ts" % (cols, tbl), (last[tbl],)).fetchall()
        except sqlite3.OperationalError:
            continue
        by_day = {}
        for r in rows:
            by_day.setdefault(time.strftime("%Y%m%d", time.gmtime(r[0])), []).append(r)
            last[tbl] = max(last[tbl], r[0])
        for day, rs in by_day.items():
            p = os.path.join(repo, "data", "%s_%s.csv" % (tbl, day))
            new = not os.path.exists(p)
            with open(p, "a", newline="") as fh:
                w = csv.writer(fh)
                if new:
                    w.writerow(cols.split(","))
                w.writerows(rs)
        n += len(rows)
    con.close()
    with open(sp, "w") as fh:
        for k, v in last.items():
            fh.write("%s %d\n" % (k, v))
    return n


def push_once(db_path=DB, work=None):
    repo = work or WORK
    n = export(db_path, repo)
    if not n:
        return "naya data nahi"
    sh("git", "add", "-A", cwd=repo)
    if not sh("git", "status", "--porcelain", cwd=repo):
        return "kuch badla nahi"
    sh("git", "commit", "-q", "-m", "data %s (+%d rows)" % (time.strftime("%F %T", time.gmtime()), n), cwd=repo)
    try:
        sh("git", "push", "-q", "origin", "HEAD", cwd=repo)
    except RuntimeError:
        sh("git", "pull", "--rebase", "-q", cwd=repo)
        sh("git", "push", "-q", "origin", "HEAD", cwd=repo)
    return "push ho gaya (+%d rows)" % n


def run(once=False):
    ensure_repo(remote_url())
    while True:
        try:
            print(time.strftime("%H:%M:%S"), push_once(), flush=True)
        except Exception as e:
            print("bridge error:", e, flush=True)
        if once:
            return
        time.sleep(EVERY)


def selftest():
    tmp = tempfile.mkdtemp()
    try:
        bare = os.path.join(tmp, "remote.git")
        sh("git", "init", "-q", "--bare", "-b", "main", bare)
        dbp = os.path.join(tmp, "m.db")
        con = sqlite3.connect(dbp)
        con.execute("CREATE TABLE m(ts INT, src TEXT, k TEXT, v REAL)")
        con.execute("CREATE TABLE w(ts INT, addr TEXT, szi REAL, entry REAL, liq REAL, upnl REAL, lev REAL, usd REAL, flow REAL)")
        now = int(time.time())
        con.executemany("INSERT INTO m VALUES(?,?,?,?)", [(now + i, "binance", "mark", 81000 + i) for i in range(5)])
        con.execute("INSERT INTO w VALUES(?,?,?,?,?,?,?,?,?)", (now, "0xA", 1.5, 80000, 70000, 10, 5, 120000, 1e6))
        con.commit()
        work = os.path.join(tmp, "work")
        sh("git", "clone", "-q", bare, work)
        sh("git", "config", "user.name", "t", cwd=work)
        sh("git", "config", "user.email", "t@t", cwd=work)
        print("1) pehla push:", push_once(dbp, work))
        con.execute("INSERT INTO m VALUES(?,?,?,?)", (now + 100, "bybit", "mark", 81234))
        con.commit()
        print("2) dusra push (sirf naya):", push_once(dbp, work))
        print("3) bina naye data ke:", push_once(dbp, work))
        reader = os.path.join(tmp, "reader")
        sh("git", "clone", "-q", bare, reader)
        files = sorted(os.listdir(os.path.join(reader, "data")))
        rows = sum(len(open(os.path.join(reader, "data", x)).read().strip().splitlines()) - 1 for x in files if x.startswith("m_"))
        print("4) alag clone se padha: files=%s, m rows=%d (expected 6)" % (files, rows))
        assert rows == 6
        print("selftest OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    a = sys.argv[1] if len(sys.argv) > 1 else "run"
    if a == "selftest":
        selftest()
    else:
        run(once=(a == "once"))
