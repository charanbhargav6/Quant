"""
CRAVE QUANT — Full System Test Suite
=====================================
Tests: DB, API endpoints, SSE stream, markets, trade gate, stress, in/out-of-sample.
Usage: python test_system.py
       python test_system.py --stress   (adds 50-request concurrency test)
       python test_system.py --backtest (adds in/out-of-sample backtest check)
"""
import sys, os, json, time, threading, urllib.request, urllib.error
from datetime import datetime, timezone

BASE = "http://127.0.0.1:8765"
PASS = "\033[92m[PASS]\033[0m"
FAIL = "\033[91m[FAIL]\033[0m"
WARN = "\033[93m[WARN]\033[0m"
INFO = "\033[94m[INFO]\033[0m"

results = {"pass": 0, "fail": 0, "warn": 0}


def ok(msg):
    results["pass"] += 1
    print(f"  {PASS} {msg}")

def fail(msg):
    results["fail"] += 1
    print(f"  {FAIL} {msg}")

def warn(msg):
    results["warn"] += 1
    print(f"  {WARN} {msg}")

def info(msg):
    print(f"  {INFO} {msg}")

def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def get(path, timeout=10):
    url = BASE + path
    start = time.time()
    try:
        r = urllib.request.urlopen(url, timeout=timeout)
        data = json.loads(r.read())
        elapsed = round(time.time() - start, 2)
        return data, elapsed, None
    except urllib.error.HTTPError as e:
        elapsed = round(time.time() - start, 2)
        return None, elapsed, f"HTTP {e.code}"
    except Exception as e:
        elapsed = round(time.time() - start, 2)
        return None, elapsed, str(e)[:80]


# ─────────────────────────────────────────────────────────────────────────────
# 1. SERVER REACHABILITY
# ─────────────────────────────────────────────────────────────────────────────
section("1. SERVER REACHABILITY")

data, t, err = get("/api/ping", timeout=5)
if data:
    ok(f"Server reachable — ping {t}s | mt5={data.get('mt5')} | ts={data.get('ts','?')[:19]}")
    mt5_up = bool(data.get("mt5"))
else:
    fail(f"Server not reachable: {err}")
    print("\n  Start the server with: Crave Quant.bat\n")
    sys.exit(1)

data, t, err = get("/api/bot_status", timeout=5)
if data:
    ok(f"Bot status — running={data.get('running')} | uptime={data.get('uptime_sec','?')}s")
else:
    warn(f"Bot status unreachable: {err}")


# ─────────────────────────────────────────────────────────────────────────────
# 2. DATABASE CONNECTIVITY
# ─────────────────────────────────────────────────────────────────────────────
section("2. DATABASE CONNECTIVITY")

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from core.database_manager import get_db
    db = get_db()
    if db:
        rows = db.query("SELECT COUNT(*) as n FROM trades", ())
        n = rows[0]["n"] if rows else 0
        ok(f"DB connected — trades table: {n} rows")

        cols = db.query("PRAGMA table_info(trades)", ())
        col_names = [c["name"] for c in (cols or [])]
        required = ["symbol", "direction", "entry_price", "pnl_pct", "close_time", "node", "is_paper"]
        missing = [c for c in required if c not in col_names]
        if missing:
            fail(f"DB schema missing columns: {missing}")
        else:
            ok(f"DB schema complete — {len(col_names)} columns present")

        recent = db.query(
            "SELECT symbol, direction, pnl_pct, close_time FROM trades "
            "ORDER BY close_time DESC LIMIT 5", ()
        ) or []
        info(f"Last {len(recent)} trades: {[r['symbol'] for r in recent]}")
    else:
        fail("DB returned None")
except Exception as e:
    fail(f"DB import/connect error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# 3. MT5 BROKER CONNECTION
# ─────────────────────────────────────────────────────────────────────────────
section("3. MT5 BROKER CONNECTION")

if mt5_up:
    ok("MT5 connected (from ping response)")
    data, t, err = get("/api/accounts", timeout=8)
    if data and data.get("accounts"):
        a = data["accounts"][0]
        ok(f"Account info — balance={a.get('balance')} equity={a.get('equity')} server={a.get('server','?')}")
    else:
        warn(f"Accounts endpoint: {err or 'empty response'}")
else:
    warn("MT5 NOT connected — open MT5 terminal and log in, then restart via Crave Quant.bat")
    info("Dashboard will show STALE until MT5 is connected — this is expected behaviour")


# ─────────────────────────────────────────────────────────────────────────────
# 4. MARKET DATA ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────
section("4. MARKET DATA ENDPOINTS")

data, t, err = get("/api/markets", timeout=6)
if data is not None:
    loading    = data.get("_loading", False)
    stale      = data.get("_stale", False)
    groups     = data.get("groups", {})
    total      = sum(len(v) for v in groups.values())
    with_price = sum(1 for g in groups.values() for r in g if r.get("available"))

    if loading:
        ok(f"Markets responded in {t}s (_loading — background refresh triggered)")
        info("Prices will appear on next poll in ~5s (subscribe to SSE /api/stream)")
    elif stale:
        ok(f"Markets stale cache ({t}s) — {total} instruments, {with_price} with prices")
    else:
        ok(f"Markets live ({t}s) — {total} instruments, {with_price} with prices")

    for grp, rows in groups.items():
        live = [r["symbol"] for r in rows if r.get("available")]
        if live:
            info(f"  {grp}: {len(live)} live — {live[:3]}")
        elif rows:
            warn(f"  {grp}: {len(rows)} instruments, 0 prices (broker/feed offline)")
else:
    fail(f"Markets endpoint: {err}")

# Test SSE stream
try:
    req = urllib.request.Request(BASE + "/api/stream")
    req.add_header("Accept", "text/event-stream")
    r = urllib.request.urlopen(req, timeout=8)
    chunk = r.read(120).decode(errors="replace")
    if "connected" in chunk or "heartbeat" in chunk:
        ok("SSE /api/stream — streaming OK")
    else:
        warn(f"SSE stream opened but unexpected content: {chunk[:60]}")
except Exception as e:
    fail(f"SSE /api/stream: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# 5. ALL API ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────
section("5. ALL API ENDPOINTS")

endpoints = [
    ("/api/ping",                   5,  "server health"),
    ("/api/risk",                   15, "risk engine"),
    ("/api/strategies",             8,  "strategy registry"),
    ("/api/analytics/equity_curve", 8,  "equity curve"),
    ("/api/analytics/pnl_calendar", 8,  "pnl calendar"),
    ("/api/execution",              8,  "execution log"),
    ("/api/positions",              8,  "positions"),
    ("/api/orders",                 8,  "orders"),
    ("/api/reconcile",              10, "reconciliation"),
    ("/api/intelligence",           8,  "AI council"),
    ("/api/india",                  8,  "india markets"),
    ("/api/bot_status",             5,  "bot status"),
]

for path, timeout, label in endpoints:
    data, t, err = get(path, timeout=timeout)
    if data is not None:
        ok(f"{label:25} {path:38} ({t}s)")
    else:
        if err and ("timed out" in err or "Timeout" in err):
            warn(f"{label:25} {path:38} TIMEOUT after {t}s")
        else:
            fail(f"{label:25} {path:38} {err}")


# ─────────────────────────────────────────────────────────────────────────────
# 6. TRADE CAPABILITY CHECK
# ─────────────────────────────────────────────────────────────────────────────
section("6. TRADE CAPABILITY CHECK")

try:
    from config.config import PAPER_TRADING, RISK, INSTRUMENTS
    paper_enabled = PAPER_TRADING.get("enabled", False)
    max_risk_pct  = RISK.get("risk_per_trade_pct", 1.0)
    max_daily     = RISK.get("max_daily_loss_pct", 4.0)

    ok(f"Config loaded — paper_trading={paper_enabled}")
    ok(f"Risk limits — {max_risk_pct}% per trade, {max_daily}% daily loss limit")
    info(f"Instruments in config: {len(INSTRUMENTS)}")

    try:
        from core.streak_state import streak
        st = streak.get_status()
        cb = st.get("circuit_breaker_active", False)
        cons = st.get("consecutive_losses", 0)
        daily = st.get("today_pnl_pct", 0)
        if cb:
            warn(f"Circuit breaker ACTIVE — {cons} consecutive losses, {daily}% P&L today")
        else:
            ok(f"Circuit breaker armed (not triggered) — {cons} losses, {daily}% today")
    except Exception as e:
        warn(f"Circuit breaker read error: {e}")

    try:
        from core.strategy_registry import STRATEGY_DEFS, ENFORCE_LIVE_READY_GATE
        live_ready = [s for s in STRATEGY_DEFS if s.get("live_ready")]
        paper_only = [s for s in STRATEGY_DEFS if not s.get("live_ready")]
        ok(f"Strategies: {len(live_ready)} live-ready, {len(paper_only)} paper-only")
        info(f"Live gate enforced: {ENFORCE_LIVE_READY_GATE}")
        for s in live_ready:
            exp_r = s.get("static_backtest", {}).get("expectancy_r", 0)
            info(f"  LIVE: {s['name']} (+{exp_r}R expectancy)")
    except Exception as e:
        warn(f"Strategy registry error: {e}")

except Exception as e:
    fail(f"Config load error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# 7. IN-SAMPLE vs OUT-OF-SAMPLE VALIDATION
# ─────────────────────────────────────────────────────────────────────────────
section("7. IN-SAMPLE vs OUT-OF-SAMPLE VALIDATION")

try:
    db = get_db()
    if not db:
        warn("DB unavailable — skipping IS/OOS")
    else:
        rows = db.query(
            "SELECT node, pnl_pct, r_multiple, outcome, close_time FROM trades "
            "WHERE close_time IS NOT NULL AND close_time != '' "
            "ORDER BY close_time ASC", ()
        ) or []

        if len(rows) < 5:
            warn(f"Only {len(rows)} trades in DB — need >= 5 for IS/OOS split")
            in_sample, out_sample = rows, []
        else:
            split = len(rows) * 2 // 3
            in_sample, out_sample = rows[:split], rows[split:]

        def stats(trades):
            if not trades: return {"n": 0, "win_rate": 0, "avg_r": 0, "total_pnl": 0}
            r_vals = [t.get("r_multiple") or 0 for t in trades]
            pnl    = [t.get("pnl_pct")    or 0 for t in trades]
            wins   = sum(1 for x in r_vals if x > 0)
            return {
                "n":        len(trades),
                "win_rate": round(wins / len(trades) * 100, 1),
                "avg_r":    round(sum(r_vals) / len(r_vals), 3),
                "total_pnl":round(sum(pnl), 2),
            }

        is_s  = stats(in_sample)
        oos_s = stats(out_sample)

        info(f"In-sample  ({is_s['n']} trades): win={is_s['win_rate']}% avg_R={is_s['avg_r']} pnl={is_s['total_pnl']}%")

        if out_sample:
            info(f"Out-sample ({oos_s['n']} trades): win={oos_s['win_rate']}% avg_R={oos_s['avg_r']} pnl={oos_s['total_pnl']}%")
            win_decay = is_s["win_rate"] - oos_s["win_rate"]
            r_decay   = is_s["avg_r"]   - oos_s["avg_r"]

            if win_decay > 20:
                fail(f"OOS win-rate decay {win_decay:.1f}% — curve-fit risk!")
            elif win_decay > 10:
                warn(f"OOS win-rate decay {win_decay:.1f}% — monitor closely")
            else:
                ok(f"OOS win-rate stable (decay={win_decay:.1f}%)")

            if r_decay > 0.2:
                warn(f"OOS expectancy decay {r_decay:.3f}R")
            else:
                ok(f"OOS expectancy stable (decay={r_decay:.3f}R)")
        else:
            info("Not enough trades for OOS split — run more live/paper trades")

        # Per-strategy
        try:
            from core.strategy_registry import STRATEGY_DEFS
            info("\nPer-strategy live vs backtest:")
            for strat in STRATEGY_DEFS:
                sid = strat["id"]
                s_rows = [r for r in rows if r.get("node") == sid]
                bt_exp = strat.get("static_backtest", {}).get("expectancy_r") or 0
                if s_rows:
                    s = stats(s_rows)
                    flag = "⚠ DECAY" if bt_exp > 0 and s["avg_r"] < bt_exp * 0.7 else "OK"
                    info(f"  {strat['name'][:40]:40} n={s['n']:3} win={s['win_rate']:5.1f}% "
                         f"live_R={s['avg_r']:+.3f} bt_R={bt_exp:+.3f} {flag}")
                else:
                    info(f"  {strat['name'][:40]:40} no live trades yet (bt_R={bt_exp:+.3f})")
        except Exception as e:
            warn(f"Per-strategy stats error: {e}")

except Exception as e:
    fail(f"IS/OOS test error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# 8. STRESS TEST
# ─────────────────────────────────────────────────────────────────────────────
if "--stress" in sys.argv:
    section("8. STRESS TEST — 50 concurrent requests")

    STRESS_URLS = ["/api/ping", "/api/strategies", "/api/bot_status",
                   "/api/analytics/equity_curve", "/api/markets"]
    N = 50
    errors, times = [], []
    lock = threading.Lock()

    def hit(i):
        d, t, e = get(STRESS_URLS[i % len(STRESS_URLS)], timeout=15)
        with lock:
            if e: errors.append(e)
            else: times.append(t)

    threads = [threading.Thread(target=hit, args=(i,)) for i in range(N)]
    t0 = time.time()
    for th in threads: th.start()
    for th in threads: th.join()
    total_t = round(time.time() - t0, 2)

    ok(f"{N} requests in {total_t}s")
    if times:
        avg_t = round(sum(times)/len(times), 2)
        max_t = round(max(times), 2)
        ok(f"avg={avg_t}s  max={max_t}s  success={len(times)}/{N}")
    if errors:
        fail(f"{len(errors)} errors — {errors[:2]}")
    else:
        ok("0 errors — server stable under load")
else:
    section("8. STRESS TEST")
    info("Skipped — run with --stress to enable 50-request concurrency test")


# ─────────────────────────────────────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────────────────────────────────────
section("SUMMARY")
total = results["pass"] + results["fail"] + results["warn"]
print(f"  Passed:  {results['pass']}")
print(f"  Warned:  {results['warn']}")
print(f"  Failed:  {results['fail']}")
print(f"  Total:   {total}")

if results["fail"] == 0:
    print(f"\n  \033[92m All checks passed!\033[0m\n")
elif results["fail"] <= 2:
    print(f"\n  \033[93m {results['fail']} check(s) failed — review above\033[0m\n")
else:
    print(f"\n  \033[91m {results['fail']} checks failed — system needs attention\033[0m\n")
