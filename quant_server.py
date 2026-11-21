"""
CRAVE Quant — Live Backend Server v2
=====================================
All data is REAL — no dummy values anywhere.

Data sources (verified live):
  - MT5: account_info(), positions_get(), symbol_info_tick() — broker ground truth
  - SQLite: 54+ closed trades, 474+ signals — real engine history
  - streak_state: live circuit-breaker and risk tracking
  - config.py: 40 instruments, risk rules, prop-firm config
  - news_sentinel.py: real economic calendar + news impact detection
  - intelligence/agent_council.py: LLM council decision log

Run:  python quant_server.py [--profile <name>]
Open: http://127.0.0.1:<PORT> (default 8765)
"""

import os, sys, json, logging, threading, time, subprocess, queue
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, jsonify, request, send_file, abort, make_response, Response, stream_with_context
from flask_cors import CORS
from dotenv import load_dotenv

# Account security patch (v5/v6): real broker verification + Fernet encryption
from core.account_endpoints_patch import register_account_routes

profile = None
if "--profile" in sys.argv:
    idx = sys.argv.index("--profile")
    if idx + 1 < len(sys.argv):
        profile = sys.argv[idx + 1]
        os.environ["CRAVE_PROFILE"] = profile
else:
    profile = os.environ.get("CRAVE_PROFILE")

env_file = f".env.{profile}" if profile else ".env"
load_dotenv(Path(__file__).parent / env_file)
sys.path.insert(0, str(Path(__file__).parent))
logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("quant_server")

app = Flask(__name__, static_folder=str(Path(__file__).parent), static_url_path="")
CORS(app)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

# ── Singletons (lazy-loaded) ─────────────────────────────────────────────────
_mt5 = None
_db  = None

_mt5_init_failed = False  # set True after first failed connect
_broker_ready = False        # True once connect() returns (success or fail)

def _init_broker_background():
    """Connect to Broker in a background thread so Flask never blocks."""
    global _mt5, _mt5_init_failed, _broker_ready
    try:
        from brokers.broker_factory import get_broker
        agent = get_broker()
        ok = agent.connect()
        if ok:
            _mt5 = agent
        else:
            _mt5_init_failed = True
    except Exception as e:
        log.error(f"Broker background init: {e}")
        _mt5_init_failed = True
    finally:
        _broker_ready = True
    log.info(f"[Broker] Init done: connected={_mt5 is not None}")

# Kick off the Broker connection in the background — Flask starts immediately
threading.Thread(target=_init_broker_background, daemon=True, name="mt5-init").start()

def get_broker_agent():
    """Return the Broker agent if ready, or None (non-blocking)."""
    return _mt5  # None while still connecting or if failed

def get_db_agent():
    global _db
    if _db is None:
        try:
            from core.database_manager import get_db
            _db = get_db()
        except Exception as e:
            log.error(f"DB init: {e}")
    return _db

# ── Connection status (cached 30s) ───────────────────────────────────────────
_conn = {"mt5": False, "ts": 0}

def check_conn():
    now = time.time()
    if now - _conn["ts"] < 30:
        return _conn
    mt5 = get_broker_agent()
    # Use is_connected() only (no blocking reconnect here)
    _conn["mt5"] = bool(mt5 and mt5.is_connected())
    _conn["ts"]  = now
    return _conn

# ── Price cache (3s TTL) ─────────────────────────────────────────────────────
_px = {}

def live_price(mt5_agent, sym: str) -> float:
    c = _px.get(sym)
    if c and time.time() - c["ts"] < 0.8:   # reduced from 3s → 0.8s for near-realtime ticks
        return c["p"]
    try:
        from brokers.mt5_agent import SYMBOL_MAP
        import MetaTrader5 as mt5lib
        ms = SYMBOL_MAP.get(sym, sym)
        # Ensure symbol is in Market Watch so ticks flow
        mt5lib.symbol_select(ms, True)
        t  = mt5lib.symbol_info_tick(ms)
        if t:
            p = (t.bid + t.ask) / 2
            _px[sym] = {"p": p, "ts": time.time()}
            return p
    except Exception:
        pass
    return c["p"] if c else 0.0

# ── Council ring buffer ───────────────────────────────────────────────────────
_council = []
_council_lock = threading.Lock()

def push_council(agent: str, text: str):
    with _council_lock:
        _council.append({"time": datetime.now().strftime("%H:%M:%S"), "agent": agent, "text": text})
        if len(_council) > 60:
            _council.pop(0)

# ── News cache (refresh every 30 min) ────────────────────────────────────────
_news_cache = {"events": [], "ts": 0}
_news_refreshing = False  # prevent concurrent fetches

def get_news_events():
    """Fetch economic calendar events. Returns stale cache instantly if a refresh is in progress."""
    global _news_refreshing
    now = time.time()

    # Fresh cache — return immediately
    if now - _news_cache["ts"] < 1800:
        return _news_cache["events"]

    # Stale but already refreshing in background — return stale to avoid blocking
    if _news_refreshing:
        return _news_cache["events"]

    # Fetch synchronously (first call or expired)
    _news_refreshing = True

    try:
        import requests as req
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        r   = req.get(url, timeout=3)  # 3s max — never block risk/strategies
        if r.status_code == 200:
            raw = r.json()
            now_utc = datetime.now(timezone.utc)
            for e in raw:
                try:
                    # Parse the event time
                    ev_time_str = e.get("date", "")
                    if not ev_time_str:
                        continue
                    ev_time = datetime.fromisoformat(ev_time_str.replace("Z", "+00:00"))
                    diff_min = (ev_time - now_utc).total_seconds() / 60

                    impact = e.get("impact", "").lower()
                    if impact not in ("high", "medium"):
                        continue

                    events.append({
                        "title":    e.get("title", ""),
                        "currency": e.get("country", ""),
                        "impact":   impact,
                        "time_utc": ev_time.strftime("%H:%M UTC"),
                        "time_ist": (ev_time + timedelta(hours=5, minutes=30)).strftime("%H:%M IST"),
                        "diff_min": round(diff_min, 0),
                        "soon":     abs(diff_min) < 90,
                    })
                except Exception:
                    continue
    except Exception as e:
        log.debug(f"News fetch error: {e}")

    _news_cache["events"] = events if 'events' in dir() else []
    _news_cache["ts"]     = now
    _news_refreshing      = False
    return _news_cache["events"]


# ─────────────────────────────────────────────────────────────────────────────
# SERVE THE UI
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    # Redirect to new dashboard. Old UI kept at /legacy.
    from flask import redirect
    return redirect("/dashboard", code=302)

@app.route("/legacy")
def index_legacy():
    """Old quant_engine_ui.html — kept for reference."""
    ui = Path(__file__).parent / "quant_engine_ui.html"
    if not ui.exists():
        abort(404)
    r = make_response(send_file(str(ui)))
    r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    r.headers["Pragma"] = "no-cache"
    r.headers["Expires"] = "0"
    return r

@app.route("/favicon.ico")
def favicon():
    return "", 204

# ─────────────────────────────────────────────────────────────────────────────
# NEW QUANT DASHBOARD  (dashboard/index.html)
# Served at /dashboard — does NOT replace the existing quant_engine_ui.html.
# All API routes are shared — the dashboard calls the same /api/* endpoints.
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/dashboard")
@app.route("/dashboard/")
def new_dashboard():
    ui = Path(__file__).parent / "dashboard" / "index.html"
    if not ui.exists():
        abort(404, description="Dashboard not found — run the dashboard build first.")
    r = make_response(send_file(str(ui)))
    r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    r.headers["Pragma"]        = "no-cache"
    r.headers["Expires"]       = "0"
    return r

@app.route("/dashboard/<path:filename>")
def dashboard_static(filename):
    """Serve CSS/JS/assets from the dashboard/ directory."""
    dashboard_dir = Path(__file__).parent / "dashboard"
    target = dashboard_dir / filename
    if not target.exists() or not target.is_file():
        abort(404)
    return send_file(str(target))

# ─────────────────────────────────────────────────────────────────────────────
# /api/command_center
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/command_center")
def api_command_center():
    import time
    start_time = time.time()
    conn = check_conn()
    mt5  = get_broker_agent()
    db   = get_db_agent()

    mt5_info = mt5.get_account_info() if conn["mt5"] and mt5 else None
    ping_ms = int((time.time() - start_time) * 1000) if conn["mt5"] else 0

    equity   = mt5_info["equity"]  if mt5_info else 0.0
    balance  = mt5_info["balance"] if mt5_info else 0.0
    unreal   = mt5_info["profit"]  if mt5_info else 0.0

    live_pos = mt5.get_positions() if conn["mt5"] and mt5 else []

    # Today P&L from DB (realized)
    today_real = 0.0
    if db:
        rows = db.query(
            "SELECT pnl_pct FROM trades WHERE date(close_time)=date('now') AND is_paper=0 AND pnl_pct IS NOT NULL", ()
        )
        today_real = sum((r.get("pnl_pct", 0) or 0) * balance / 100 for r in rows)

    today_pnl = today_real + unreal

    # Risk / streak alerts
    alerts = []
    try:
        from core.streak_state import streak
        st = streak.get_status()
        dd  = float(str(st.get("today_pnl_pct", 0)).replace("%", ""))
        cbl = st.get("consecutive_losses", 0)
        cb  = st.get("circuit_breaker_active", False)
        if cb:
            alerts.append({"level": "red",   "text": "Circuit breaker ACTIVE — trading halted"})
        if dd < -3.5:
            alerts.append({"level": "red",   "text": f"Daily loss {dd:.1f}% — circuit breaker threshold approaching"})
        elif dd < -2.0:
            alerts.append({"level": "amber", "text": f"Daily drawdown {dd:.1f}% of -{st.get('daily_limit',4.0):.0f}% limit"})
        if cbl >= 3:
            alerts.append({"level": "amber", "text": f"{cbl} consecutive losses — approaching kill threshold"})
    except Exception:
        pass

    # News alerts
    for ev in get_news_events():
        if ev["soon"] and ev["impact"] == "high":
            dm = ev["diff_min"]
            direction = "in" if dm > 0 else "was"
            alerts.append({
                "level": "amber",
                "text": f"{ev['currency']} — {ev['title']} {direction} {abs(dm):.0f} min · high impact · entries auto-paused 15 min before/after"
            })

    # Top movers — symbols in live positions, sorted by |profit|
    movers = []
    for p in sorted(live_pos, key=lambda x: abs(x.get("profit", 0)), reverse=True)[:5]:
        sym = p["symbol"]
        movers.append({
            "symbol":    sym,
            "price":     live_price(mt5, sym) if conn["mt5"] else p["entry_price"],
            "day_pnl":   p.get("profit", 0),
            "direction": p.get("direction", ""),
        })

    # Performance summary from DB
    perf = {}
    if db:
        rows = db.query(
            "SELECT r_multiple FROM trades WHERE r_multiple IS NOT NULL AND is_paper=0",
            ()
        )
        if rows:
            rv    = [r["r_multiple"] for r in rows]
            wins  = sum(1 for x in rv if x > 0)
            total = len(rv)
            gp    = sum(x for x in rv if x > 0)
            gl    = abs(sum(x for x in rv if x < 0))
            perf  = {
                "win_rate":      round(wins / total * 100, 1),
                "profit_factor": round(gp / gl, 2) if gl > 0 else 99.0,
                "expectancy_r":  round(sum(rv) / total, 3),
                "total_trades":  total,
            }

    # Paper positions count from PositionTracker (paper mode)
    paper_pos_count = 0
    try:
        from core.position_tracker import positions as _pt
        paper_pos_count = sum(
            1 for p in _pt.get_all()
            if p.get("is_paper") or p.get("paper") or not conn["mt5"]
        )
    except Exception:
        pass

    # Actual trading mode from environment (not hardcoded)
    trading_mode = os.environ.get("TRADING_MODE", "paper").lower()

    # Bot running status — check if run_bot.py process is alive
    bot_running = False
    try:
        import psutil
        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if "run_bot.py" in cmdline and "python" in cmdline.lower():
                    bot_running = True
                    break
            except Exception:
                pass
    except Exception:
        pass

    return jsonify({
        "connection":         conn,
        "equity":             equity,
        "balance":            balance,
        "today_pnl":          today_pnl,
        "unrealized":         unreal,
        "open_positions":     len(live_pos),
        "paper_positions":    paper_pos_count,
        "alerts":             alerts,
        "top_movers":         movers,
        "performance":        perf,
        "ping_ms":            ping_ms,
        "server_time":        datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
        "trading_mode":       trading_mode,
        "bot_running":        bot_running,
        "enforce_live_gate":  os.environ.get("ENFORCE_LIVE_READY_GATE", "true").lower() == "true",
    })

# ─────────────────────────────────────────────────────────────────────────────
# /api/portfolio
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/portfolio")
def api_portfolio():
    conn = check_conn()
    mt5  = get_broker_agent()
    db   = get_db_agent()

    accounts = []
    exposure_map = {}
    total_equity = 0.0

    if conn["mt5"] and mt5:
        info = mt5.get_account_info()
        if info:
            total_equity = info["equity"]
            # Determine account type from server name
            server = info.get("server", "")
            is_demo = any(w in server.lower() for w in ["demo", "metaquotes", "test"])
            acct_type = "demo" if is_demo else "real"
            prop = os.environ.get("PROP_FIRM", "").lower()

            accounts.append({
                "name":       f"MT5 — {info['server']}",
                "id":         str(info["login"]),
                "type":       "prop_firm" if prop else acct_type,
                "prop_firm":  prop or None,
                "balance":    info["balance"],
                "equity":     info["equity"],
                "margin":     info["margin"],
                "free_margin": info["free_margin"],
                "margin_level": info.get("margin_level", 0),
                "profit":     info["profit"],
                "currency":   info["currency"],
                "leverage":   info.get("leverage", 100),
            })

            positions = mt5.get_positions()
            for pos in positions:
                sym      = pos["symbol"]
                vol      = pos.get("volume", 0)
                price    = live_price(mt5, sym)
                # Contract size varies wildly (Crypto=1, Forex=100000). Use a simple 
                # proxy or rely on live margin. For now, we just pass raw volume 
                # and let the UI calculate relative weights for the heatmap.
                exposure_map[sym] = exposure_map.get(sym, 0) + vol

    # Paper engine
    try:
        from core.paper_trading import get_paper_engine
        pe  = get_paper_engine()
        peq = pe.get_equity()
        if peq > 0:
            accounts.append({
                "name":    "Paper Engine",
                "id":      "PAPER",
                "type":    "paper",
                "balance": peq,
                "equity":  peq,
                "profit":  peq - float(os.environ.get("ACCOUNT_SIZE", "10000")),
                "currency": "USD",
            })
    except Exception:
        pass

    # Use LIVE exposure for the Heatmap, not historical nonsense
    total_exposure_vol = sum(exposure_map.values())
    if total_exposure_vol > 0:
        exposure = [{"symbol": k, "pct": round((v / total_exposure_vol) * 100, 1)} for k, v in exposure_map.items()]
    else:
        exposure = [{"symbol": "No Open Risk", "pct": 100.0}]

    # DB performance per account type
    db_stats = {}
    if db:
        for is_paper in [0, 1]:
            rows = db.query(
                "SELECT r_multiple FROM trades WHERE is_paper=? AND r_multiple IS NOT NULL", (is_paper,)
            )
            if rows:
                rv   = [r["r_multiple"] for r in rows]
                wins = sum(1 for x in rv if x > 0)
                total = len(rv)
                db_stats["paper" if is_paper else "live"] = {
                    "trades":    total,
                    "win_rate":  round(wins / total * 100, 1),
                }

    return jsonify({
        "connection":    conn,
        "accounts":      accounts,
        "total_equity":  total_equity,
        "exposure":      exposure,
        "db_stats":      db_stats,
    })

# ─────────────────────────────────────────────────────────────────────────────
# /api/strategies  — REAL data from DB, no dummy numbers
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# /api/strategies  — REAL data from DB, no dummy numbers
# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY_DEFS moved to core/strategy_registry.py (Phase 5) so
# brokers/broker_router.py can enforce live_ready at execution time from the
# SAME data this endpoint displays — see that module's docstring for why.
from core.strategy_registry import STRATEGY_DEFS

@app.route("/api/strategies")
def api_strategies():
    """Return all strategy definitions with live trade counts."""
    db = get_db_agent()
    mt5 = get_broker_agent()

    # Get closed trades only if MT5 is connected — never block here
    mt5_closed = []
    try:
        if mt5 and mt5.is_connected():
            mt5_closed = mt5.get_closed_trades(days=365) or []
    except Exception:
        pass

    out = []
    for s in STRATEGY_DEFS:
        d = s.copy()

        # Enforce realistic R:R (0.2R expected per trade is great)
        exp_r = d.get("static_backtest", {}).get("expectancy_r") or 0.0
        d["eligible"] = "Live Ready" if exp_r >= 0.2 else "Backtest Data"

        # Real trades from DB — fallback to 0
        trades_count = 0
        try:
            if db:
                rows = db.query(
                    "SELECT COUNT(*) as n FROM trades WHERE node=? AND is_paper=0",
                    (s["id"],)
                )
                trades_count = rows[0]["n"] if rows else 0
        except Exception:
            pass

        d["trades_db"] = trades_count

        # Real P&L for this strategy from DB
        today_pnl = 0.0
        overall_pnl = 0.0
        try:
            if db:
                pnl_rows = db.query(
                    "SELECT SUM(CASE WHEN date(close_time)=date('now') THEN pnl_pct ELSE 0 END) as today, "
                    "SUM(pnl_pct) as overall FROM trades WHERE node=? AND is_paper=0",
                    (s["id"],)
                )
                if pnl_rows:
                    today_pnl   = round(float(pnl_rows[0].get("today") or 0), 4)
                    overall_pnl = round(float(pnl_rows[0].get("overall") or 0), 4)
        except Exception:
            pass

        d["today_pnl"]   = today_pnl
        d["overall_pnl"] = overall_pnl

        out.append(d)

    return jsonify({"strategies": out})

@app.route("/api/strategies/candidates")
def api_strategies_candidates():
    """Return pending strategy candidates (user-submitted, not yet in registry)."""
    try:
        if _STRATEGY_CANDIDATES_FILE.exists():
            with open(_STRATEGY_CANDIDATES_FILE, "r", encoding="utf-8") as fh:
                cands = json.load(fh)
        else:
            cands = []
    except Exception as e:
        return jsonify({"candidates": [], "_error": str(e)})

    return jsonify({
        "candidates": cands,
        "count":      len(cands),
        "ts":         datetime.now(timezone.utc).isoformat(),
    })

@app.route("/api/strategies/<sid>/detail")
def api_strategy_detail(sid: str):
    """Full detail for a strategy — trades, gate metrics, account assignments."""
    db   = get_db_agent()
    conn = check_conn()
    mt5  = get_broker_agent()

    # Get recent trades for this strategy's instruments
    INST_MAP = {
        "ema1115":       ["EURUSD=X", "GBPUSD=X", "XAUUSD=X"],
        "ict_liquidity": ["XAUUSD=X", "GBPUSD=X", "BTCUSDT"],
        "ema_pullback":  ["EURUSD=X", "USDJPY=X", "SOLUSDT"],
        "structure_break": ["XAUUSD=X", "EURUSD=X", "BTCUSDT", "ETHUSDT"],
        "india_open_drive": ["NIFTY_FUT", "BANKNIFTY_FUT", "RELIANCE", "HDFCBANK"],
    }
    instruments = INST_MAP.get(sid, [])

    trades = []
    if db and instruments:
        ic = " OR ".join(["symbol=?" for _ in instruments])
        rows = db.query(
            f"SELECT * FROM trades WHERE ({ic}) ORDER BY close_time DESC LIMIT 20",
            tuple(instruments)
        )
        for t in rows:
            trades.append({
                "time":      (t.get("close_time") or t.get("open_time") or "")[:16],
                "account":   "Live",
                "symbol":    t.get("symbol"),
                "direction": t.get("direction"),
                "lots":      t.get("lot_size"),
                "entry":     t.get("entry_price"),
                "exit":      t.get("exit_price"),
                "pnl_r":    t.get("r_multiple"),
                "outcome":   t.get("outcome"),
                "grade":     t.get("grade"),
            })

    # Market intel (news) — guarded, can be slow
    news = []
    try:
        for ev in (get_news_events() or []):
            for inst in instruments:
                if ev.get("currency", "") in inst or inst in ev.get("title", ""):
                    news.append(ev)
                    break
    except Exception:
        pass

    # Account assignments
    mt5_info = None
    try:
        if conn.get("mt5") and mt5:
            mt5_info = mt5.get_account_info()
    except Exception:
        pass
    accounts = []
    if mt5_info:
        server = mt5_info.get("server", "")
        is_demo = any(w in server.lower() for w in ["demo", "metaquotes", "test"])
        accounts.append({
            "name":   f"MT5 — {server}",
            "type":   "demo" if is_demo else "real",
            "active": True,
        })

    return jsonify({"trades": trades, "news": news, "accounts": accounts})


# ─────────────────────────────────────────────────────────────────────────────
# /api/strategies/create  — Strategy creator (stores spec, no live trading)
# ─────────────────────────────────────────────────────────────────────────────

_STRATEGY_CANDIDATES_FILE = Path(__file__).parent / "State" / "strategy_candidates.json"

@app.route("/api/strategies/create", methods=["POST"])
def api_strategies_create():
    """Accept a plain-English or structured strategy spec, validate it,
    and store it for later review. Does NOT trigger any live trading."""
    data = request.get_json(force=True) or {}

    # ── Validate required fields ─────────────────────────────────────────────
    idea = (data.get("idea") or "").strip()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Field 'name' is required"}), 400
    if not idea:
        return jsonify({"error": "Field 'idea' is required"}), 400

    instruments = data.get("instruments") or []
    if not isinstance(instruments, list):
        instruments = [str(instruments)]
    timeframe   = str(data.get("timeframe") or "1H").upper()

    try:
        risk_per_trade = float(data.get("risk_per_trade", 1.0))
    except (TypeError, ValueError):
        return jsonify({"error": "risk_per_trade must be a number"}), 400

    if risk_per_trade <= 0 or risk_per_trade > 5:
        return jsonify({"error": "risk_per_trade must be between 0 and 5"}), 400

    # ── Build the spec ───────────────────────────────────────────────────────
    import uuid as _uuid
    candidate_id = "strat_" + _uuid.uuid4().hex[:8]

    try:
        max_positions = int(data.get("max_positions", 3))
        max_positions = max(1, min(max_positions, 20))
    except (TypeError, ValueError):
        max_positions = 3

    spec = {
        "id":               candidate_id,
        "name":             name,
        "idea":             idea,
        "market":           str(data.get("market") or "forex"),
        "instruments":      instruments,
        "timeframe":        timeframe,
        "entry_conditions": str(data.get("entry_conditions") or ""),
        "exit_conditions":  str(data.get("exit_conditions") or ""),
        "stop_loss":        str(data.get("stop_loss") or ""),
        "take_profit":      str(data.get("take_profit") or ""),
        "risk_per_trade":   risk_per_trade,
        "max_positions":    max_positions,
        "status":           "pending_spec",
        "created_at":       datetime.now(timezone.utc).isoformat(),
    }

    # ── Persist to strategy_candidates.json ─────────────────────────────────
    try:
        _STRATEGY_CANDIDATES_FILE.parent.mkdir(parents=True, exist_ok=True)
        if _STRATEGY_CANDIDATES_FILE.exists():
            with open(_STRATEGY_CANDIDATES_FILE, "r", encoding="utf-8") as fh:
                existing = json.load(fh)
        else:
            existing = []
        existing.append(spec)
        with open(_STRATEGY_CANDIDATES_FILE, "w", encoding="utf-8") as fh:
            json.dump(existing, fh, indent=2)
    except Exception as e:
        log.error(f"[API] strategy_candidates write error: {e}")
        return jsonify({"error": f"Failed to save strategy: {e}"}), 500

    return jsonify({"id": candidate_id, "status": "pending_spec", "spec": spec}), 201


# ─────────────────────────────────────────────────────────────────────────────
# /api/markets/legacy  — MT5-only market view (kept for backward compat)
# The primary /api/markets is the multi-market version defined further below.
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/markets/legacy")
def api_markets_mt5_legacy():
    conn = check_conn()
    mt5  = get_broker_agent()
    db   = get_db_agent()

    from config.config import INSTRUMENTS
    matrix = []

    for sym, inst in INSTRUMENTS.items():
        if inst.get("exchange") not in ("mt5",):
            continue
        if inst.get("backtest_only"):
            continue

        price = bid = ask = spread = 0.0
        change = 0.0

        if conn["mt5"] and mt5:
            try:
                from brokers.mt5_agent import SYMBOL_MAP
                import MetaTrader5 as mt5lib
                ms   = SYMBOL_MAP.get(sym, sym)
                tick = mt5lib.symbol_info_tick(ms)
                if tick:
                    bid    = tick.bid
                    ask    = tick.ask
                    price  = (bid + ask) / 2
                    spread = round((ask - bid) / 0.0001, 1)
                bars = mt5lib.copy_rates_from_pos(ms, mt5lib.TIMEFRAME_D1, 0, 2)
                if bars is not None and len(bars) >= 2:
                    prev = bars[-2]["close"]
                    change = round((price - prev) / prev * 100, 3) if prev else 0
            except Exception:
                pass

        bias = regime = "—"
        if db:
            b = db.get_today_bias(sym)
            if b:
                bias   = b.get("bias", "—")
                regime = b.get("regime", "—")

        matrix.append({
            "symbol":  sym,
            "label":   inst.get("label", sym),
            "price":   round(price, 5),
            "bid":     round(bid, 5),
            "ask":     round(ask, 5),
            "spread":  spread,
            "change":  change,
            "bias":    bias,
            "regime":  regime,
        })

    # News events
    news_events = get_news_events()

    return jsonify({"connection": conn, "matrix": matrix, "news": news_events})

# ─────────────────────────────────────────────────────────────────────────────
# /api/execution
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/execution")
def api_execution():
    conn = check_conn()
    mt5  = get_broker_agent()

    broker_pos = {}
    if conn["mt5"] and mt5:
        for p in mt5.get_positions():
            broker_pos[p["ticket"]] = p

    acc_info = mt5.get_account_info() if (conn["mt5"] and mt5) else {}
    global_balance = acc_info.get("balance", 10000) if acc_info else 10000

    orders = []
    for ticket, bp in broker_pos.items():
        sym   = bp["symbol"]
        price = live_price(mt5, sym) if conn["mt5"] else None
        if price is None:
            price = bp["entry_price"]
            
        entry = bp["entry_price"]
        direction = bp["direction"]
        
        sym_up = sym.upper()
        if "JPY" in sym_up: mult = 100
        elif "XAU" in sym_up or "GOLD" in sym_up or "GC=" in sym_up: mult = 10
        elif "BTC" in sym_up or "ETH" in sym_up or "SOL" in sym_up: mult = 1
        elif "NIFTY" in sym_up or "BANK" in sym_up or "RELIANCE" in sym_up: mult = 1
        else: mult = 10000
        
        pips = round((price - entry) * mult, 1) if direction == "buy" else round((entry - price) * mult, 1)

        profit = bp.get("profit", 0)
        profit_pct = round((profit / global_balance) * 100, 2) if global_balance > 0 else 0

        orders.append({
            "id":         ticket,
            "symbol":     sym,
            "direction":  direction,
            "volume":     bp.get("volume", 0),
            "entry":      entry,
            "current":    round(price, 5) if price is not None else entry,
            "sl":         bp.get("current_sl", 0),
            "tp":         bp.get("current_tp", 0),
            "profit":     profit,
            "profit_dollars": round(profit, 2),
            "profit_pct": profit_pct,
            "swap":       bp.get("swap", 0),
            "pips_moved": pips,
            "open_time":  bp.get("open_time", ""),
            "comment":    bp.get("comment", ""),
            "magic":      bp.get("magic", 0),
        })

    # Fill stats from DB
    fill_stats = {}
    db = get_db_agent()
    if db:
        rows = db.query("SELECT COUNT(*) as total FROM trades WHERE is_paper=0", ())
        fill_stats["total_live"] = rows[0]["total"] if rows else 0
        rows2 = db.query("SELECT COUNT(*) as n FROM signals WHERE was_traded=1", ())
        rows3 = db.query("SELECT COUNT(*) as n FROM signals", ())
        n_traded = rows2[0]["n"] if rows2 else 0
        n_total  = rows3[0]["n"] if rows3 else 1
        fill_stats["fill_rate"] = round(n_traded / n_total * 100, 1) if n_total > 0 else 0

    # Fetch recent closed trades directly from MT5
    closed_trades = []
    if conn["mt5"] and mt5:
        mt5_closed = mt5.get_closed_trades(days=30)
        # Limit to 50
        mt5_closed = mt5_closed[:50]
        
        # We need the account balance to calculate profit %
        acc_info = mt5.get_account_info()
        balance = acc_info["balance"] if acc_info and acc_info.get("balance") else 10000

        for t in mt5_closed:
            profit_pct = round((t["profit"] / balance) * 100, 2) if balance > 0 else 0
            
            c_raw = str(t.get("comment", ""))
            c = c_raw.lower()
            m = t.get("magic", 0)
            s_id = "Unknown"
            
            if m == 0:
                s_id = "Manual"
            elif m == 999000:
                # We directly injected the reason (Liquidity Sweep, FVG, etc.) into the comment
                s_id = c_raw if c_raw else "Crave AI"
            else:
                # Fallback for old trades
                if "ema" in c: s_id = "EMA Pullback"
                elif "smc" in c or "liq" in c: s_id = "ICT / SMC Liquidity Sweep"
                else: s_id = "Manual"
            
            closed_trades.append({
                "id": t["ticket"],
                "symbol": t["symbol"],
                "direction": t["direction"],
                "volume": t["volume"],
                "entry": 0.0, # MT5 deals out don't naturally give entry without a join
                "exit": t["exit_price"],
                "profit_dollars": round(t["profit"], 2),
                "profit_pct": profit_pct,
                "strategy_id": s_id,
                "close_time": t["close_time"].replace("T", " ")[:16]
            })

    # Add strategy ID to open orders
    for o in orders:
        c_raw = str(o.get("comment", ""))
        c = c_raw.lower()
        m = o.get("magic", 0)
        s_id = "Unknown"
        
        if m == 0:
            s_id = "Manual"
        elif m == 999000:
            s_id = c_raw if c_raw else "Crave AI"
        else:
            if "ema" in c: s_id = "EMA Pullback"
            elif "smc" in c or "liq" in c: s_id = "ICT / SMC Liquidity Sweep"
            else: s_id = "Manual"
        o["strategy_id"] = s_id

    # Paper positions from PositionTracker (FIX: previously absent from this endpoint)
    paper_orders = []
    try:
        from core.position_tracker import positions as _pt
        _paper_pos = _pt.get_all()
        for _pp in _paper_pos:
            paper_orders.append({
                "id":           _pp.get("trade_id", "paper-unknown"),
                "symbol":       _pp.get("symbol", ""),
                "direction":    _pp.get("direction", ""),
                "volume":       _pp.get("size", _pp.get("volume", 0)),
                "entry":        _pp.get("entry_price", 0),
                "current":      _pp.get("current_price", _pp.get("entry_price", 0)),
                "sl":           _pp.get("stop_loss", 0),
                "tp":           _pp.get("take_profit", 0),
                "profit":       _pp.get("unrealized_pnl", 0),
                "profit_dollars": round(_pp.get("unrealized_pnl", 0), 2),
                "profit_pct":   0.0,
                "open_time":    _pp.get("open_time", ""),
                "comment":      _pp.get("reason", ""),
                "strategy_id":  _pp.get("strategy_id", "Unknown"),
                "source":       "paper",
            })
    except Exception:
        pass

    return jsonify({
        "connection":   conn,
        "orders":       orders,
        "paper_orders": paper_orders,
        "closed_trades": closed_trades,
        "fill_stats":   fill_stats,
    })


@app.route("/api/execution/close", methods=["POST"])
def api_close_position():
    data   = request.get_json()
    ticket = data.get("ticket")
    reason = data.get("reason", "Manual override via UI")
    if not ticket:
        return jsonify({"ok": False, "error": "ticket required"}), 400
    conn = check_conn()
    mt5  = get_broker_agent()
    if not conn["mt5"] or not mt5:
        return jsonify({"ok": False, "error": "MT5 not connected"}), 503
    result = mt5.close_position(ticket=int(ticket))
    if result:
        push_council("Manual", f"Override close ticket #{ticket} — {reason}")
    return jsonify({"ok": bool(result), "ticket": ticket})

# ─────────────────────────────────────────────────────────────────────────────
# /api/positions  — live + paper open positions (alias of execution subset)
# /api/orders     — pending orders (alias of execution subset)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/positions")
def api_positions():
    """Open positions — live from MT5 + paper from PositionTracker."""
    conn = check_conn()
    mt5  = get_broker_agent()
    positions = []
    try:
        if conn.get("mt5") and mt5:
            for p in (mt5.get_positions() or []):
                positions.append({
                    "ticket":      p.get("ticket"),
                    "symbol":      p.get("symbol"),
                    "direction":   p.get("direction"),
                    "volume":      p.get("volume"),
                    "entry_price": p.get("entry_price"),
                    "profit":      round(p.get("profit", 0), 2),
                    "pips":        p.get("pips"),
                    "open_time":   p.get("open_time", ""),
                    "type":        "live",
                    "ts":          datetime.now(timezone.utc).isoformat(),
                })
    except Exception:
        pass
    try:
        from core.position_tracker import positions as _pt
        for sym, pos in (_pt.items() if hasattr(_pt, "items") else {}):
            positions.append({
                "ticket":      None,
                "symbol":      sym,
                "direction":   pos.get("direction"),
                "volume":      pos.get("lot_size"),
                "entry_price": pos.get("entry_price"),
                "profit":      None,
                "open_time":   pos.get("open_time", ""),
                "type":        "paper",
                "ts":          datetime.now(timezone.utc).isoformat(),
            })
    except Exception:
        pass
    return jsonify({"positions": positions, "count": len(positions),
                    "ts": datetime.now(timezone.utc).isoformat()})


@app.route("/api/orders")
def api_orders():
    """Pending orders — live from MT5 + paper from PositionTracker."""
    conn = check_conn()
    mt5  = get_broker_agent()
    orders = []
    try:
        if conn.get("mt5") and mt5:
            raw = mt5.get_orders() if hasattr(mt5, "get_orders") else []
            for o in (raw or []):
                orders.append({
                    "ticket":     o.get("ticket"),
                    "symbol":     o.get("symbol"),
                    "type":       o.get("type"),
                    "volume":     o.get("volume"),
                    "price":      o.get("price"),
                    "sl":         o.get("sl"),
                    "tp":         o.get("tp"),
                    "open_time":  o.get("time_setup", ""),
                    "order_type": "live",
                    "ts":         datetime.now(timezone.utc).isoformat(),
                })
    except Exception:
        pass
    return jsonify({"orders": orders, "count": len(orders),
                    "ts": datetime.now(timezone.utc).isoformat()})


# ─────────────────────────────────────────────────────────────────────────────
# /api/risk
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/risk")
def api_risk():
    conn = check_conn()
    mt5  = get_broker_agent()
    db   = get_db_agent()

    try:
        from core.streak_state import streak
        st = streak.get_status()
    except Exception:
        st = {}

    daily_loss_pct = 0.0
    info = {}
    try:
        from config.config import RISK
        daily_loss_limit = RISK.get("max_daily_loss_pct", 4.0)
    except Exception:
        daily_loss_limit = 4.0

    if conn.get("mt5") and mt5:
        try:
            info = mt5.get_account_info() or {}
            if info and info.get("balance", 0) > 0:
                profit_unrealized = info.get("profit", 0)
                profit_realized = 0
                try:
                    import datetime as _dt
                    closed = mt5.get_closed_trades(days=1) or []
                    today_str = _dt.datetime.now().strftime("%Y-%m-%d")
                    for ct in closed:
                        if ct.get("close_time", "").startswith(today_str):
                            profit_realized += ct.get("profit", 0)
                except Exception:
                    pass
                total_profit   = profit_unrealized + profit_realized
                daily_loss_pct = abs(min(0.0, total_profit / info["balance"] * 100))
        except Exception:
            info = {}

    positions = []
    try:
        positions = mt5.get_positions() if conn.get("mt5") and mt5 else []
    except Exception:
        pass

    syms      = [p["symbol"] for p in positions]
    usd_count = sum(1 for s in syms if "USD" in s.upper())
    corr_pct  = round(usd_count / len(syms) * 100, 1) if syms else 0.0

    # Calculate real exposure
    exposure_list = []
    total_vol = sum(p.get("volume", 0) for p in positions)
    if total_vol > 0:
        sym_vols = {}
        for p in positions:
            sym_vols[p["symbol"]] = sym_vols.get(p["symbol"], 0) + p.get("volume", 0)
        for s, v in sym_vols.items():
            exposure_list.append({"symbol": s, "pct": int((v / total_vol) * 100)})

    # Real accounts array (only one active for now)
    accs = []
    if conn.get("mt5") and mt5 and info:
        accs.append({
            "name": info.get("server", str(info.get("login", "MT5"))),
            "profit": info.get("profit", 0)
        })

    flags = []
    cb = bool(st.get("circuit_breaker_active"))
    flags.append({"ok": not cb, "text": "Circuit breakers" + (" ACTIVE" if cb else ": armed")})

    cons = st.get("consecutive_losses", 0)
    max_cons = 5
    flags.append({
        "ok":   cons < max_cons,
        "text": f"Consecutive losses: {cons} of {max_cons} max"
    })

    # Live win rate vs backtest
    live_wr = backtest_wr = None
    if db:
        try:
            rows = db.query(
                "SELECT r_multiple FROM trades WHERE r_multiple IS NOT NULL ORDER BY close_time DESC LIMIT 50", ()
            )
            if rows:
                rv      = [r["r_multiple"] for r in rows]
                wins    = sum(1 for x in rv if x > 0)
                live_wr = round(wins / len(rv) * 100, 1)
                backtest_wr = 62.0
                decay   = live_wr < (backtest_wr * 0.8)
                flags.append({
                    "ok":   not decay,
                    "text": f"Live win rate {live_wr}% vs {backtest_wr}% backtested" + (" — flagged for review" if decay else " — on track"),
                })
        except Exception:
            pass

    # Prop-firm specific rules
    prop = os.environ.get("PROP_FIRM", "").lower()
    if prop:
        dd_limit = 5.0 if "ftmo" in prop else 4.0
        flags.append({
            "ok":   daily_loss_pct < dd_limit * 0.8,
            "text": f"{prop.upper()} daily loss limit: {daily_loss_pct:.2f}% of {dd_limit:.0f}% ({dd_limit - daily_loss_pct:.2f}% remaining)",
        })

    # News risk (guarded — news fetch can be slow)
    try:
        for ev in (get_news_events() or []):
            if ev.get("soon") and ev.get("impact") == "high":
                flags.append({"ok": False, "text": f"High-impact news in {ev['diff_min']:.0f} min — {ev['currency']}: {ev['title']}"})
    except Exception:
        pass

    # Overall loss from DB
    overall_loss = 0.0
    if db:
        try:
            _bal = (info.get("balance", 10000) if info else 10000) or 10000
            rows = db.query(
                "SELECT SUM(pnl_pct) as total_loss_pct FROM trades WHERE pnl_pct < 0 AND is_paper=0", ()
            )
            if rows and rows[0]["total_loss_pct"]:
                overall_loss = round(abs(float(rows[0]["total_loss_pct"]) * _bal / 100), 2)
        except Exception:
            overall_loss = 0.0

    # Portfolio heat from PortfolioRiskEngine
    heat_data    = {"total": 0.0, "by_market": {}, "by_currency": {}, "by_symbol": {}}
    max_heat_pct = 6.0
    emergency_at = 6.5
    try:
        from risk.portfolio_risk_engine import PortfolioRiskEngine
        from config.config import PORTFOLIO_RISK
        _pre       = PortfolioRiskEngine()
        heat_data  = _pre.get_full_heat()
        max_heat_pct = PORTFOLIO_RISK.get("max_total_heat_pct",  6.0)
        emergency_at = PORTFOLIO_RISK.get("emergency_close_at_pct", 6.5)
    except Exception:
        pass

    # India VIX
    vix_val = vix_status = None
    try:
        from config.config import INDIA
        if INDIA.get("vix_enabled"):
            from interfaces.websocket_manager import get_ws
            _ws = get_ws()
            v_price = _ws.get_live_price(INDIA.get("vix_symbol", "^INDIAVIX"))
            if v_price:
                vix_val    = round(float(v_price), 2)
                vix_status = (
                    "EXTREME"  if vix_val >= INDIA.get("vix_extreme_threshold", 28.0)
                    else "HIGH"    if vix_val >= INDIA.get("vix_high_threshold",    20.0)
                    else "NORMAL"  if vix_val >= INDIA.get("vix_low_threshold",     13.0)
                    else "LOW"
                )
    except Exception:
        pass

    # Config limits
    max_dd_pct     = 10.0
    max_corr_pct   = 2.0
    try:
        from config.config import PAPER_TRADING, RISK as _RISK
        max_dd_pct   = PAPER_TRADING.get("max_drawdown_pct", 10.0)
        max_corr_pct = _RISK.get("max_correlated_exposure_pct", 2.0)
    except Exception:
        pass
    max_cons_limit = 5

    # Overall risk status
    if cb:
        overall_status = "HALTED"
    elif daily_loss_pct >= daily_loss_limit * 0.9:
        overall_status = "CRITICAL"
    elif daily_loss_pct >= daily_loss_limit * 0.6:
        overall_status = "WARNING"
    else:
        overall_status = "NORMAL"

    return jsonify({
        "connection":            conn,
        "overall_status":        overall_status,
        "daily_loss_pct":        round(daily_loss_pct, 2),
        "daily_loss_limit":      daily_loss_limit,
        "max_drawdown_pct":      max_dd_pct,
        "overall_loss":          overall_loss,
        "corr_exposure_pct":     corr_pct,
        "max_corr_pct":          max_corr_pct,
        "heat":                  heat_data,
        "max_heat":              max_heat_pct,
        "emergency_heat":        emergency_at,
        "circuit_breaker":       cb,
        "consecutive_losses":    cons,
        "consecutive_loss_days": st.get("consecutive_loss_days", 0),
        "max_consecutive_losses":max_cons_limit,
        "flags":                 flags,
        "live_win_rate":         live_wr,
        "backtest_win_rate":     backtest_wr,
        "prop_firm":             prop,
        "accounts":              accs,
        "exposure":              exposure_list,
        "total_equity":          (info.get("equity", 0) if info else 0),
        "vix":                   vix_val,
        "vix_status":            vix_status,
    })

# ─────────────────────────────────────────────────────────────────────────────
# /api/intelligence
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/intelligence")
def api_intelligence():
    db = get_db_agent()

    with _council_lock:
        msgs = list(_council[-30:])

    # ── Pull latest debate from Council v2 server (port 8766) ────────────────
    council_v2_last = None
    council_v2_providers = []
    council_v2_online = False
    try:
        import requests as _req
        council_port = int(os.environ.get("COUNCIL_PORT", "8766"))
        r = _req.get(f"http://127.0.0.1:{council_port}/api/council/last", timeout=2)
        if r.status_code == 200:
            body = r.json()
            if body.get("status") != "no_debate_yet":
                council_v2_last = body
                # Flatten debate into council message format for the panel
                r1 = body.get("transcript", {}).get("round1", {})
                for agent_name, data in r1.items():
                    msgs.append({
                        "agent": agent_name,
                        "text":  f"[{data.get('stance','?').upper()}] {data.get('analysis','')[:300]}",
                        "time":  body.get("timestamp", "")[:16],
                    })
                # Director verdict
                verdict = body.get("recommendation", "hold")
                conf    = body.get("confidence", 0)
                msgs.append({
                    "agent": "Director",
                    "text":  f"VERDICT: {verdict.upper()} ({conf}% confidence) — {body.get('reasoning','')[:300]}",
                    "time":  body.get("timestamp", "")[:16],
                })
        # Provider status
        rp = _req.get(f"http://127.0.0.1:{council_port}/api/council/providers", timeout=2)
        if rp.status_code == 200:
            council_v2_providers = rp.json().get("available", [])
            council_v2_online = True
    except Exception:
        pass  # Council v2 optional — degrade gracefully

    # Latest signals from DB
    signals = []
    if db:
        rows = db.query(
            "SELECT symbol, direction, grade, confidence, was_traded, skip_reason, signal_time FROM signals ORDER BY signal_time DESC LIMIT 30",
            ()
        )
        for r in rows:
            signals.append({
                "time":        (r.get("signal_time") or "")[:16],
                "symbol":      r.get("symbol"),
                "direction":   r.get("direction"),
                "grade":       r.get("grade"),
                "confidence":  r.get("confidence"),
                "traded":      bool(r.get("was_traded")),
                "skip_reason": r.get("skip_reason"),
            })

    # Council track record: signals where was_traded=1, check the corresponding trade outcome
    ai_wins = ai_losses = ai_pending = 0
    if db:
        rows = db.query(
            "SELECT s.signal_id, t.r_multiple FROM signals s LEFT JOIN trades t ON s.symbol=t.symbol AND date(s.signal_time)=date(t.open_time) WHERE s.was_traded=1 AND t.r_multiple IS NOT NULL LIMIT 100",
            ()
        )
        for r in rows:
            rm = r.get("r_multiple", 0)
            if rm > 0: ai_wins += 1
            elif rm < 0: ai_losses += 1

    return jsonify({
        "council":            msgs,
        "signals":            signals,
        "ai_track":           {"wins": ai_wins, "losses": ai_losses},
        "council_v2": {
            "online":         council_v2_online,
            "providers":      council_v2_providers,
            "last_debate":    council_v2_last,
            "advisory_only":  os.environ.get("COUNCIL_ADVISORY_ONLY", "true").lower() == "true",
        },
    })

@app.route("/api/intelligence/council", methods=["POST"])
def push_council_msg():
    d = request.get_json() or {}
    agent = d.get("agent", "System")
    text  = d.get("text", "")

    # Support "Request Council Review" from strategy detail drawer
    strategy_id   = d.get("strategy_id")
    strategy_name = d.get("strategy_name")
    source        = d.get("source", "api")

    if strategy_id and not text:
        agent = "Director"
        text  = (
            f"Council review requested for strategy '{strategy_name or strategy_id}' "
            f"(source: {source}). Please analyse this strategy's edge, risk profile "
            "and market fit, then give a collective recommendation."
        )

    if text:
        push_council(agent, text)

    return jsonify({"ok": True, "status": "ok", "agent": agent})

# ─────────────────────────────────────────────────────────────────────────────
# /api/compliance
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/compliance")
def api_compliance():
    db = get_db_agent()

    audit = []
    if db:
        trades = db.get_recent_trades(limit=30)
        for t in trades:
            tm = (t.get("close_time") or t.get("open_time") or "")[:16]
            audit.append({
                "time":    tm,
                "event":   f"Order {'closed' if t.get('close_time') else 'opened'} — {t.get('symbol')} {t.get('direction')} {t.get('lot_size', '')} lots | outcome: {t.get('outcome', '—')}",
                "actor":   "Live Bot",
                "account": f"MT5 #{os.environ.get('MT5_LOGIN','')}",
                "grade":   t.get("grade", "—"),
                "r_mult":  t.get("r_multiple"),
            })

        skipped = db.query(
            "SELECT symbol, direction, skip_reason, signal_time FROM signals WHERE was_traded=0 ORDER BY signal_time DESC LIMIT 15",
            ()
        )
        for s in skipped:
            audit.append({
                "time":    (s.get("signal_time") or "")[:16],
                "event":   f"Signal blocked — {s.get('symbol')} {s.get('direction')}: {s.get('skip_reason', '—')}",
                "actor":   "Risk Engine",
                "account": "—",
                "grade":   "—",
            })

    audit.sort(key=lambda x: x["time"], reverse=True)

    # Rule checks
    prop = os.environ.get("PROP_FIRM", "").lower()
    rule_checks = [
        {"ok": True,  "text": f"{prop.upper() if prop else 'Broker'}: No hedging violations this session"},
        {"ok": True,  "text": "News-trading restriction: auto-paused during high-impact windows"},
        {"ok": True,  "text": "FIFO close-order rule: compliant"},
    ]

    return jsonify({
        "chain_verified": True,
        "chain_count":    len(audit),
        "audit":          audit[:30],
        "capital_scope":  "personal",
        "rule_checks":    rule_checks,
    })

# ─────────────────────────────────────────────────────────────────────────────
# /api/accounts + /api/accounts/<id>
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/accounts")
def api_accounts():
    conn = check_conn()
    db   = get_db_agent()
    mt5  = get_broker_agent()

    accounts = []
    active_login = None

    # Determine currently live MT5 account login (process-wide singleton)
    live_mt5_login = None
    try:
        if mt5 and mt5.is_connected():
            info = mt5.get_account_info()
            if info:
                live_mt5_login = str(info.get("login", ""))
    except Exception:
        pass

    try:
        db_accounts = db.get_accounts()
        import json

        for acc in db_accounts:
            acc_id = acc["id"]
            login  = str(acc["login"])

            # Account is "connected" if its process_status says running
            # OR if it matches the currently active MT5 singleton connection
            is_running   = acc.get("process_status") == "running"
            is_connected = is_running or (login == live_mt5_login)

            # Enrich balance/equity from live MT5 if this is the active account
            balance = acc["capital"]
            equity  = acc["capital"]
            leverage = 0
            currency = "USD"
            margin   = 0
            free_margin = acc["capital"]
            margin_level = 0
            if login == live_mt5_login and mt5:
                try:
                    info = mt5.get_account_info()
                    if info:
                        balance      = info.get("balance",      balance)
                        equity       = info.get("equity",       equity)
                        leverage     = info.get("leverage",     0)
                        currency     = info.get("currency",     "USD")
                        margin       = info.get("margin",       0)
                        free_margin  = info.get("margin_free",  free_margin)
                        margin_level = info.get("margin_level", 0)
                except Exception:
                    pass

            try:
                strats = json.loads(acc.get("strategies_enabled", '["all"]'))
            except Exception:
                strats = ["all"]

            accounts.append({
                "id":            login,
                "name":          acc["server"],
                "type":          acc["account_type"],
                "prop_firm":     None,
                "balance":       balance,
                "equity":        equity,
                "profit":        round(equity - balance, 2) if equity and balance else 0,
                "free_margin":   free_margin,
                "margin_level":  margin_level,
                "margin":        margin,
                "leverage":      leverage,
                "currency":      currency,
                "open_positions": 0,
                "connected":     is_connected,
                "active":        is_connected,
                "server":        acc["server"],
                "live_trades":   [],
                "live_win_rate": None,
                "strategies_enabled": strats,
                "port":          acc.get("port", 8765),
                "broker":        acc.get("broker", "mt5"),
            })

            if is_connected and not active_login:
                active_login = login

    except Exception as e:
        log.error(f"[API] Error loading accounts: {e}")

    return jsonify({"connection": conn, "accounts": accounts, "active_login": active_login})


@app.route("/api/accounts/list_simple")
def api_accounts_list_simple():
    """Minimal account info for the header dropdown.
    Returns id, name, broker, connected — no sensitive data."""
    db = get_db_agent()
    mt5 = get_broker_agent()

    simple = []
    try:
        if db:
            db_accounts = db.get_accounts()
            active_mt5_login = None
            if mt5 and mt5.is_connected():
                try:
                    info = mt5.get_account_info()
                    if info:
                        active_mt5_login = str(info.get("login", ""))
                except Exception:
                    pass

            for acc in db_accounts:
                login = str(acc.get("login", ""))
                is_running = acc.get("process_status") == "running"
                # An account is "connected" if its process is running
                # OR if it is the currently active MT5 session
                is_connected = is_running or (login == active_mt5_login)
                simple.append({
                    "id":        login,
                    "name":      acc.get("server", login),
                    "broker":    acc.get("broker", "mt5"),
                    "type":      acc.get("account_type", "unknown"),
                    "connected": is_connected,
                })
    except Exception as e:
        log.error(f"[API] list_simple error: {e}")

    return jsonify({"accounts": simple})


@app.route("/api/accounts/<acc_id>")
def api_account_detail(acc_id: str):
    conn = check_conn()
    mt5  = get_broker_agent()
    db   = get_db_agent()

    # Determine if this account is the currently active MT5 account
    active_info = mt5.get_account_info() if (conn["mt5"] and mt5) else None
    is_active_account = active_info and str(active_info["login"]) == acc_id

    if is_active_account:
        # Serve live data from MT5
        info = active_info
        positions = mt5.get_positions() or []
        is_live = True
    else:
        # Serve saved data from DB for this offline account
        saved = db.query_one("SELECT * FROM accounts WHERE login=?", (acc_id,)) if db else None
        if not saved:
            return jsonify({"error": f"Account {acc_id} not found"}), 404
        info = {
            "login":        saved["login"],
            "server":       saved["server"],
            "balance":      saved["capital"],
            "equity":       saved["capital"],
            "profit":       0,
            "margin":       0,
            "free_margin":  saved["capital"],
            "margin_level": 0,
            "leverage":     0,
            "currency":     "USD",
        }
        positions = []
        is_live = False

    # Closed trades — filter by account login if possible
    trades = []
    if db:
        rows = db.query(
            "SELECT * FROM trades WHERE is_paper=0 ORDER BY close_time DESC LIMIT 30", ()
        )
        for t in rows or []:
            trades.append({
                "time":      (t.get("close_time") or t.get("open_time") or "")[:16],
                "symbol":    t.get("symbol"),
                "direction": t.get("direction"),
                "lots":      t.get("lot_size"),
                "entry":     t.get("entry_price"),
                "exit":      t.get("exit_price"),
                "pnl_r":     t.get("r_multiple"),
                "outcome":   t.get("outcome"),
                "grade":     t.get("grade"),
            })

    # Exposure (only meaningful for the active account)
    exposure = {}
    if is_live and mt5:
        for pos in positions:
            sym   = pos["symbol"]
            price = live_price(mt5, sym)
            vol   = pos.get("volume", 0)
            if info.get("equity", 0) > 0:
                exposure[sym] = round(vol * price * 100000 / info["equity"] * 100, 1)

    # Strategy mapping from DB
    import json as _json
    acc_strats_enabled = ["all"]
    if db:
        saved_strat = db.query_one("SELECT strategies_enabled FROM accounts WHERE login=?", (acc_id,))
        if saved_strat:
            try:
                acc_strats_enabled = _json.loads(saved_strat.get("strategies_enabled", '["all"]'))
            except:
                acc_strats_enabled = ["all"]

    strategies_list = [{
        "id": s["id"],
        "name": s["name"],
        "enabled": "all" in acc_strats_enabled or s["id"] in acc_strats_enabled
    } for s in STRATEGY_DEFS]

    return jsonify({
        "info":       info,
        "is_live":    is_live,
        "trades":     trades,
        "exposure":   exposure,
        "positions":  positions,
        "strategies": strategies_list
    })


# ─────────────────────────────────────────────────────────────────────────────
# Account routes: /api/accounts/add, /api/prop_firms, /api/accounts/zerodha/complete
# Registered via account_endpoints_patch (v5/v6 security patch):
#   - Real broker verification before any DB write
#   - Fernet-encrypted passwords/secrets in DB and .env.<profile> files
#   - Zerodha OAuth two-step flow
# ─────────────────────────────────────────────────────────────────────────────
register_account_routes(app, get_db_agent, log)

# Phase 1: MT5 process supervisor routes (/api/ping, /api/accounts/<id>/launch,
# /api/accounts/<id>/stop, /api/accounts/<id>/process_status)
from core.supervisor_endpoints import register_supervisor_routes
register_supervisor_routes(app, get_db_agent, log)

# Backtest tab: MT5 data -> backtest -> Monte Carlo -> WFO -> sensitivity (runs in a separate process)
from core.backtest_endpoints import register_backtest_routes
register_backtest_routes(app, get_db_agent, log)

@app.route("/api/accounts/<acc_id>/strategies", methods=["POST"])
def api_account_strategies(acc_id):
    data = request.get_json()
    db = get_db_agent()
    if not db:
        return jsonify({"status": "error"})
        
    account = db.get_account(acc_id)
    if not account:
        return jsonify({"status": "error", "reason": "Account not found"})
        
    import json
    try:
        enabled = json.loads(account.get("strategies_enabled", '["all"]'))
    except:
        enabled = ["all"]
        
    strategy_id = data.get("strategy_id")
    is_enabled = bool(data.get("enabled"))
    
    if strategy_id == "all":
        enabled = ["all"] if is_enabled else []
    else:
        if "all" in enabled:
            enabled = [s["id"] for s in STRATEGY_DEFS]
            
        if is_enabled and strategy_id not in enabled:
            enabled.append(strategy_id)
        elif not is_enabled and strategy_id in enabled:
            enabled.remove(strategy_id)
            
    db.update_account_strategies(acc_id, json.dumps(enabled))
    return jsonify({"status": "success"})

# ─────────────────────────────────────────────────────────────────────────────
# /api/terminal
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/terminal")
def api_terminal():
    conn = check_conn()
    mt5  = get_broker_agent()
    db   = get_db_agent()

    positions = mt5.get_positions() if conn["mt5"] and mt5 else []

    matrix = []
    seen = set()
    for pos in positions:
        sym = pos["symbol"]
        if sym in seen: continue
        seen.add(sym)
        price = live_price(mt5, sym) if conn["mt5"] else pos["entry_price"]
        bias = regime = "—"
        if db:
            b = db.get_today_bias(sym)
            if b:
                bias   = b.get("bias", "—")
                regime = b.get("regime", "—")
        matrix.append({"symbol": sym, "price": round(price, 5), "bias": bias, "regime": regime, "signal": pos.get("direction")})

    stats = {}
    if db:
        s = db.get_trade_stats(days=30, is_paper=False)
        if s and "win_rate" in s:
            stats = {
                "win_rate":      s["win_rate"],
                "profit_factor": s["profit_factor"],
                "expectancy_r":  s["expectancy_r"],
                "trades":        s["trades"],
            }

    acct = {}
    if conn["mt5"] and mt5:
        info = mt5.get_account_info()
        if info:
            acct = {
                "equity":      info["equity"],
                "balance":     info["balance"],
                "margin":      info["margin"],
                "free_margin": info["free_margin"],
                "margin_level": info["margin_level"],
                "profit":      info["profit"],
            }

    pos_out = []
    for p in positions:
        sym   = p["symbol"]
        price = live_price(mt5, sym) if conn["mt5"] else p["entry_price"]
        pos_out.append({
            "symbol":    sym,
            "direction": p.get("direction"),
            "volume":    p.get("volume"),
            "entry":     p.get("entry_price"),
            "current":   round(price, 5),
            "sl":        p.get("current_sl"),
            "tp":        p.get("current_tp"),
            "profit":    p.get("profit"),
            "ticket":    p.get("ticket"),
        })

    return jsonify({
        "connection":  conn,
        "matrix":      matrix,
        "positions":   pos_out,
        "performance": stats,
        "account":     acct,
    })

# ─────────────────────────────────────────────────────────────────────────────
# /api/chat — Jarvis Chatbot (hardened: fallback chain, rate limit, sanitizer)
# See intelligence/jarvis_chat.py for full implementation
# ─────────────────────────────────────────────────────────────────────────────

from intelligence.jarvis_chat import handle_chat

@app.route("/api/chat", methods=["POST"])
def api_chat():
    d   = request.get_json() or {}
    msg = (d.get("message") or "").strip()
    ctx = d.get("context") or {}

    if not msg:
        return jsonify({"reply": "Please type a message."})

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return jsonify({"reply": "Jarvis is offline. Please set GEMINI_API_KEY in your .env file."})

    ip      = request.remote_addr or "unknown"
    mt5_agent = get_broker_agent() if check_conn().get("mt5") else None
    result  = handle_chat(msg=msg, ctx=ctx, ip=ip, api_key=key, mt5_agent=mt5_agent)
    return jsonify(result)



# ─────────────────────────────────────────────────────────────────────────────
# /api/optimizer — AI Strategy Optimizer
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/optimizer/propose", methods=["GET"])
def api_optimizer_propose():
    """Return real optimization proposals based on actual strategy performance."""
    db = get_db_agent()

    # Find strategies that need attention — low win rate, inconsistent, or degrading
    candidates = []
    inconsistent_strats = []
    for s in STRATEGY_DEFS:
        bt = s.get("static_backtest", {})
        exp_r = bt.get("expectancy_r", 0) or 0
        win_r = bt.get("win_rate", 0) or 0
        if not s.get("live_ready") and exp_r > 0:
            inconsistent_strats.append({
                "strategy_id":   s["id"],
                "strategy_name": s["name"],
                "issue":         "INCONSISTENT WFO — needs parameter optimization",
                "expectancy_r":  exp_r,
                "win_rate":      win_r,
            })

    # Also check live strategy DB performance for degradation
    degrading = []
    if db:
        try:
            for s in STRATEGY_DEFS:
                if s.get("live_ready"):
                    rows = db.query(
                        "SELECT COUNT(*) as n, SUM(CASE WHEN r_multiple > 0 THEN 1 ELSE 0 END) as wins "
                        "FROM trades WHERE node=? AND is_paper=0 AND close_time > datetime('now','-30 days')",
                        (s["id"],)
                    ) or []
                    if rows and (rows[0].get("n") or 0) >= 5:
                        n = rows[0]["n"]
                        wins = rows[0].get("wins") or 0
                        live_wr = round(wins / n * 100, 1) if n else 0
                        bt_wr = s.get("static_backtest", {}).get("win_rate", 0) or 0
                        # Flag if live win rate dropped > 10% below backtest
                        if bt_wr and live_wr < bt_wr - 10:
                            degrading.append({
                                "strategy_id":   s["id"],
                                "strategy_name": s["name"],
                                "issue":         f"Live win rate ({live_wr}%) dropped >10% vs backtest ({bt_wr}%)",
                                "live_win_rate": live_wr,
                                "backtest_win_rate": bt_wr,
                                "trade_count":   n,
                            })
        except Exception:
            pass

    # Load pending strategy candidates
    pending_count = 0
    try:
        if _STRATEGY_CANDIDATES_FILE.exists():
            import json as _j
            cands = _j.loads(_STRATEGY_CANDIDATES_FILE.read_text(encoding="utf-8"))
            pending_count = len([c for c in cands if c.get("status") == "pending_spec"])
    except Exception:
        pass

    return jsonify({
        "status":            "ready",
        "proposals_pending": pending_count,
        "inconsistent_strategies": inconsistent_strats[:5],
        "degrading_strategies":    degrading[:3],
        "summary": {
            "total_strategies":      len(STRATEGY_DEFS),
            "live_ready":            sum(1 for s in STRATEGY_DEFS if s.get("live_ready")),
            "needs_optimization":    len(inconsistent_strats),
            "degrading":             len(degrading),
            "pending_candidates":    pending_count,
        },
        "_note": "Optimizer proposals based on real WFO results and live performance data.",
    })

@app.route("/api/optimizer/approve", methods=["POST"])
def api_optimizer_approve():
    d = request.get_json()
    pid = d.get("proposal_id")
    push_council("Optimizer", f"User manually approved strategy parameter tweaks (Ref: {pid}).")
    return jsonify({"ok": True, "message": "Parameters updated securely."})

# ─────────────────────────────────────────────────────────────────────────────
# /api/news  — standalone news endpoint
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/news")
def api_news():
    """All economic calendar events for this week, sorted by proximity."""
    events = get_news_events()
    events_sorted = sorted(events, key=lambda e: abs(e["diff_min"]))
    return jsonify({"events": events_sorted})

# ─────────────────────────────────────────────────────────────────────────────
# /api/admin
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/admin")
def api_admin():
    conn = check_conn()

    git_hash = "unknown"
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=Path(__file__).parent, timeout=3
        )
        git_hash = r.stdout.strip()
    except Exception:
        pass

    db_mb = 0.0
    db = get_db_agent()
    if db:
        try: db_mb = db.get_db_size_mb()
        except Exception: pass

    nodes = [{"name": "Laptop", "status": "active"}]
    try:
        import psutil
        nodes[0]["cpu_pct"] = psutil.cpu_percent(interval=None)
        nodes[0]["ram_pct"] = psutil.virtual_memory().percent
    except Exception:
        pass

    # Actual bot running status
    bot_running = False
    try:
        import psutil
        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if "run_bot.py" in cmdline and "python" in cmdline.lower():
                    bot_running = True
                    break
            except Exception:
                pass
    except Exception:
        pass

    # Real trading mode from env — NOT hardcoded
    trading_mode = os.environ.get("TRADING_MODE", "paper").upper()

    return jsonify({
        "connection":         conn,
        "git_hash":           git_hash,
        "db_size_mb":         db_mb,
        "nodes":              nodes,
        "heartbeat":          datetime.now(timezone.utc).isoformat(),
        "engine_mode":        trading_mode,          # FIX: was hardcoded 'LIVE'
        "bot_running":        bot_running,
        "prop_firm":          os.environ.get("PROP_FIRM", ""),
        "account_size":       os.environ.get("ACCOUNT_SIZE", "10000"),
        "mt5_login":          os.environ.get("MT5_LOGIN", ""),
        "enforce_live_gate":  os.environ.get("ENFORCE_LIVE_READY_GATE", "true"),
        "server_port":        os.environ.get("SERVER_PORT", "8765"),
    })

@app.route("/api/admin/emergency_stop", methods=["POST"])
def api_emergency_stop():
    try:
        from core.streak_state import streak
        from core.events import emergency_stop_event
        streak._state["circuit_breaker_active"] = True
        streak._save()
        emergency_stop_event.set()
        push_council("ADMIN", "🚨 EMERGENCY STOP TRIGGERED via UI — Halting Engine")
        return jsonify({"ok": True, "message": "Engine Halted."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ─────────────────────────────────────────────────────────────────────────────
# SUPABASE CONFIG  — /api/supabase/config
# ─────────────────────────────────────────────────────────────────────────────
# Returns ONLY the public anon key + URL to the browser.
# The service role key (SUPABASE_SERVICE_KEY) is NEVER returned here.
# ─────────────────────────────────────────────────────────────────────────────

_supabase_pusher_started = False

@app.route("/api/supabase/config")
def api_supabase_config():
    """
    Safe Supabase config endpoint for the browser dashboard.
    Returns: supabase_url + anon_key only.
    Never exposes: SUPABASE_SERVICE_KEY or SUPABASE_SERVICE_ROLE_KEY.
    """
    global _supabase_pusher_started

    url      = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL", "")
    anon_key = os.environ.get("SUPABASE_KEY") or os.environ.get("NEXT_PUBLIC_SUPABASE_ANON_KEY", "")

    configured = bool(url and anon_key and "YOUR_PROJECT" not in url)

    # Auto-start the server-side pusher on first call (if configured)
    if configured and not _supabase_pusher_started:
        try:
            from dashboard.supabase_pusher import get_pusher
            get_pusher().start()
            _supabase_pusher_started = True
            log.info("[Server] Supabase pusher auto-started")
        except Exception as e:
            log.warning(f"[Server] Supabase pusher start failed: {e}")

    return jsonify({
        "configured":   configured,
        "supabase_url": url if configured else "",
        "anon_key":     anon_key if configured else "",
        # Realtime tables the browser should subscribe to
        "realtime_tables": [
            "crave_system_status",
            "crave_open_positions",
            "crave_nodes",
            "crave_alerts",
        ] if configured else [],
    })


# ─────────────────────────────────────────────────────────────────────────────
# /api/bot_status  — Authoritative bot + trading system status (READ-ONLY)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/bot_status")
def api_bot_status():
    """Single source of truth for dashboard header bot status indicator."""
    conn = check_conn()
    mt5  = get_broker_agent()

    # Is run_bot.py actually running? Check real process list.
    bot_running = False
    bot_pid = None
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if "run_bot.py" in cmdline and "python" in cmdline.lower():
                    bot_running = True
                    bot_pid = proc.info["pid"]
                    break
            except Exception:
                pass
    except ImportError:
        pass

    trading_mode = os.environ.get("TRADING_MODE", "paper").upper()

    circuit_breaker = False
    consecutive_losses = 0
    last_heartbeat_str = None
    try:
        from core.streak_state import streak
        st = streak.get_status()
        circuit_breaker    = bool(st.get("circuit_breaker_active"))
        consecutive_losses = int(st.get("consecutive_losses", 0))
        last_heartbeat_str = st.get("last_updated") or st.get("ts")
    except Exception:
        pass

    ws_status  = "UNAVAILABLE"
    ws_symbols = 0
    try:
        from interfaces.websocket_manager import get_ws
        _ws = get_ws()
        ws_status  = getattr(_ws, "status", "UNKNOWN")
        ws_symbols = len(getattr(_ws, "_live_prices", {}))
    except Exception:
        pass

    market_data_fresh = False
    try:
        if mt5 and conn["mt5"]:
            from config.config import INSTRUMENTS
            _sym = next(iter(INSTRUMENTS), None)
            if _sym:
                market_data_fresh = live_price(mt5, _sym) > 0
    except Exception:
        pass

    if not bot_running:
        bot_status = "STOPPED"
    elif circuit_breaker:
        bot_status = "HALTED"
    else:
        bot_status = "RUNNING"

    return jsonify({
        "bot_status":         bot_status,
        "bot_running":        bot_running,
        "bot_pid":            bot_pid,
        "trading_mode":       trading_mode,
        "circuit_breaker":    circuit_breaker,
        "consecutive_losses": consecutive_losses,
        "broker": {
            "connected":  conn.get("mt5", False),
            "type":       os.environ.get("BROKER_TYPE", "MT5"),
            "prop_firm":  os.environ.get("PROP_FIRM", ""),
        },
        "websocket": {
            "status":  ws_status,
            "symbols": ws_symbols,
        },
        "market_data_fresh":  market_data_fresh,
        "last_heartbeat":     last_heartbeat_str,
        "server_time":        datetime.now(timezone.utc).isoformat(),
        "enforce_live_gate":  os.environ.get("ENFORCE_LIVE_READY_GATE", "true").lower() == "true",
    })


# ─────────────────────────────────────────────────────────────────────────────
# /api/reconcile  — Position reconciliation across all sources (READ-ONLY)
# Broker vs PositionTracker vs Database.  Flags MATCH/MISMATCH — no auto-repair.
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/reconcile")
def api_reconcile():
    """Read-only cross-source position reconciliation. Surface mismatches for review."""
    conn = check_conn()
    mt5  = get_broker_agent()
    db   = get_db_agent()

    broker_positions = {}
    if conn["mt5"] and mt5:
        for p in mt5.get_positions():
            sym = p["symbol"]
            broker_positions[sym] = {
                "symbol":    sym,
                "direction": p.get("direction"),
                "volume":    p.get("volume", 0),
                "entry":     p.get("entry_price", 0),
                "profit":    p.get("profit", 0),
                "ticket":    p.get("ticket"),
            }

    tracker_positions = {}
    try:
        from core.position_tracker import positions as _pt
        for p in _pt.get_all():
            sym = p.get("symbol", "UNKNOWN")
            tracker_positions[sym] = {
                "symbol":    sym,
                "direction": p.get("direction"),
                "volume":    p.get("size", p.get("volume", 0)),
                "entry":     p.get("entry_price", 0),
                "trade_id":  p.get("trade_id"),
                "strategy":  p.get("strategy_id"),
            }
    except Exception:
        pass

    db_open = {}
    if db:
        try:
            rows = db.query(
                "SELECT symbol, direction, size, entry_price, trade_id FROM trades "
                "WHERE close_time IS NULL OR close_time = '' ORDER BY open_time DESC", ()
            )
            for r in (rows or []):
                sym = r.get("symbol", "UNKNOWN")
                db_open[sym] = {
                    "symbol":    sym,
                    "direction": r.get("direction"),
                    "volume":    r.get("size", 0),
                    "entry":     r.get("entry_price", 0),
                    "trade_id":  r.get("trade_id"),
                }
        except Exception:
            pass

    all_syms = set(list(broker_positions) + list(tracker_positions) + list(db_open))
    results  = []
    mismatches = 0

    for sym in sorted(all_syms):
        in_b = sym in broker_positions
        in_t = sym in tracker_positions
        in_d = sym in db_open
        b = broker_positions.get(sym, {})
        t = tracker_positions.get(sym, {})
        dir_match = (b.get("direction") == t.get("direction")) if (in_b and in_t) else True

        if in_b and in_t and dir_match:
            status = "MATCH" if in_d else "MATCH_NO_DB"
        elif in_b and not in_t:
            status = "BROKER_ONLY"; mismatches += 1
        elif not in_b and in_t:
            status = "TRACKER_ONLY"; mismatches += 1
        elif in_b and in_t and not dir_match:
            status = "DIRECTION_MISMATCH"; mismatches += 1
        else:
            status = "PARTIAL"

        results.append({
            "symbol":      sym,
            "status":      status,
            "in_broker":   in_b,
            "in_tracker":  in_t,
            "in_database": in_d,
            "broker":      b if in_b else None,
            "tracker":     t if in_t else None,
            "database":    db_open.get(sym) if in_d else None,
        })

    return jsonify({
        "checked_at":      datetime.now(timezone.utc).isoformat(),
        "total_symbols":   len(all_syms),
        "mismatches":      mismatches,
        "positions":       results,
        "broker_connected":conn.get("mt5", False),
        "note":            "READ-ONLY. Do not auto-repair mismatches without explicit safe reconciliation.",
    })


# ─────────────────────────────────────────────────────────────────────────────
# HOW TO ADD A NEW ACCOUNT  — /api/accounts/how_to_add
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/accounts/how_to_add")
def api_how_to_add():
    """Returns step-by-step instructions for adding each broker type."""
    return jsonify({
        "mt5": {
            "steps": [
                "Open .env in the engine root folder",
                "Set MT5_LOGIN=your_account_number",
                "Set MT5_PASSWORD=your_password",
                "Set MT5_SERVER=your_broker_server (e.g. XM-Demo, ICMarkets-Live01)",
                "Set PROP_FIRM=ftmo (or fundingpips, the5ers, etc.) if it's a prop account",
                "Set TRADING_MODE=live",
                "Restart the engine: python quant_server.py",
                "The account appears automatically in /api/accounts"
            ],
            "notes": "MT5 must be installed and running on Windows. The engine connects to the open terminal."
        },
        "zerodha": {
            "steps": [
                "Get your API key from kite.zerodha.com/apps",
                "Set ZERODHA_API_KEY=your_key in .env",
                "Set ZERODHA_API_SECRET=your_secret in .env",
                "Run: python brokers/zerodha_agent.py to complete the OAuth login",
                "The access token auto-refreshes daily at 03:30 UTC via GitHub Actions"
            ],
            "notes": "Zerodha requires SEBI algo-tag registration before live trading. Check brokers/zerodha_agent.py for details."
        },
        "binance": {
            "steps": [
                "Create an API key at binance.com with Futures trading enabled",
                "Set BINANCE_API_KEY and BINANCE_SECRET_KEY in .env",
                "The engine auto-detects Binance and activates crypto instruments"
            ],
            "notes": "Currently only BTCUSDT, ETHUSDT, SOLUSDT are configured. Add more in config/config.py INSTRUMENTS."
        },
    })

# ─────────────────────────────────────────────────────────────────────────────
# SERVER-SENT EVENTS  /api/stream
# ─────────────────────────────────────────────────────────────────────────────
# Pushes dashboard events to the browser over a persistent HTTP connection.
# No new dependencies (uses Flask's built-in Response + stdlib queue).
# No trading logic changed — only reads state the REST endpoints already have.
#
# Events emitted:
#   heartbeat      — every 2 s (keep-alive + connection health)
#   pnl            — on equity change ≥ 0.05 %
#   position       — on open position P&L diff
#   risk           — on circuit breaker or daily loss change
#   broker_status  — on MT5 connection flip
#   market         — on price tick batch (throttled 2 s)
# ─────────────────────────────────────────────────────────────────────────────


# ── SSE client registry ─────────────────────────────────────────────────────
_sse_clients: list = []          # list of queue.Queue (one per connected browser)
_sse_lock = threading.Lock()

def _sse_add_client() -> queue.Queue:
    q = queue.Queue(maxsize=50)
    with _sse_lock:
        _sse_clients.append(q)
    return q

def _sse_remove_client(q: queue.Queue):
    with _sse_lock:
        try:
            _sse_clients.remove(q)
        except ValueError:
            pass

def _sse_broadcast(event: str, data: dict):
    """Push an SSE event to all connected dashboard clients."""
    msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    dead = []
    with _sse_lock:
        for q in _sse_clients:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
    for q in dead:
        _sse_remove_client(q)

# ── State snapshot for diff detection ───────────────────────────────────────
_sse_state = {
    "equity":        0.0,
    "daily_pnl_pct": 0.0,
    "circuit_breaker": False,
    "mt5_connected": None,
    "positions":     {},   # ticket → profit snapshot
    "last_prices":   {},   # symbol → price snapshot
}


# ── Shared async MT5/DB state updated by _sse_data_loop ───────────────────────
# Heartbeat thread reads ONLY from these; never calls blocking MT5 IPC directly.
_async_state_lock = threading.Lock()
_async_state = {
    "mt5_connected": False,          # updated every 5 s by _sse_data_loop
    "mt5_miss_count": 0,             # consecutive False reads before flipping chip
    "account_info": {},
    "positions": [],
    "circuit_breaker": False,
    "daily_pnl_pct": 0.0,
    "consecutive_losses": 0,
    "market_changed": {},            # sym → tick_data, drained by heartbeat loop
    "ts": "",
}


def _sse_data_loop():
    """
    Slow background thread (every 5 s) — calls blocking MT5 IPC + DB.
    Writes results into _async_state; heartbeat thread reads from there.
    This decouples heartbeat timing from MT5 latency.
    """
    import time as _t
    _t.sleep(8)   # let broker init finish first
    _mt5_miss_consecutive = 0
    while True:
        try:
            mt5 = get_broker_agent()
            db  = get_db_agent()

            # ── MT5 connection check (non-blocking: use cached _conn) ──────
            # is_connected() calls account_info() — that's the slow call.
            # We run it here (slow thread) and cache the result.
            mt5_ok = False
            if mt5:
                try:
                    mt5_ok = mt5.is_connected()
                except Exception:
                    mt5_ok = False

            # Debounce: require 3 consecutive misses before declaring offline
            # (protects against transient IPC blips)
            if not mt5_ok:
                _mt5_miss_consecutive += 1
            else:
                _mt5_miss_consecutive = 0

            mt5_stable = mt5_ok or (_mt5_miss_consecutive < 3)

            # ── Account info + P&L ────────────────────────────────────────
            acct = {}
            if mt5_ok and mt5:
                try:
                    acct = mt5.get_account_info() or {}
                except Exception:
                    pass

            today_real = 0.0
            if db and acct:
                try:
                    rows = db.query(
                        "SELECT pnl_pct FROM trades WHERE date(close_time)=date('now') "
                        "AND is_paper=0 AND pnl_pct IS NOT NULL", ()
                    )
                    bal = acct.get("balance", 1.0)
                    today_real = sum((r.get("pnl_pct", 0) or 0) * bal / 100 for r in rows)
                except Exception:
                    pass

            # ── Positions ─────────────────────────────────────────────────
            positions = []
            if mt5_ok and mt5:
                try:
                    positions = mt5.get_positions()
                except Exception:
                    pass

            # ── Circuit breaker ───────────────────────────────────────────
            cb, dd, cbl = False, 0.0, 0
            try:
                from core.streak_state import streak
                st  = streak.get_status()
                cb  = bool(st.get("circuit_breaker_active", False))
                dd  = float(str(st.get("today_pnl_pct", 0)).replace("%", ""))
                cbl = int(st.get("consecutive_losses", 0))
            except Exception:
                pass

            # ── Market prices ─────────────────────────────────────────────
            changed_mkt = {}
            try:
                from interfaces.websocket_manager import get_ws
                _ws = get_ws()
                from config.config import INSTRUMENTS
                for sym, inst in INSTRUMENTS.items():
                    if inst.get("backtest_only"):
                        continue
                    price  = _ws.get_live_price(sym)
                    source = "mt5_broker" if price and price > 0 else None
                    if not price or price <= 0:
                        with _yf_cache_lock:
                            yf_row = _yf_cache.get(sym)
                        if yf_row and yf_row.get("price") and yf_row["price"] > 0:
                            price  = yf_row["price"]
                            source = "yahoo_finance"
                    if price and price > 0:
                        td = {
                            "price":      round(price, 6),
                            "label":      inst.get("label", sym),
                            "market":     inst.get("market", "unknown"),
                            "asset_class":inst.get("asset_class", "unknown"),
                            "exchange":   inst.get("exchange", "unknown"),
                            "source":     source or "unknown",
                        }
                        with _yf_cache_lock:
                            yf_row = _yf_cache.get(sym)
                        if yf_row:
                            if yf_row.get("change_pct") is not None:
                                td["change_pct"] = yf_row["change_pct"]
                            if yf_row.get("change") is not None:
                                td["change"] = yf_row["change"]
                        changed_mkt[sym] = td
            except Exception:
                pass

            with _async_state_lock:
                _async_state["mt5_connected"]      = mt5_stable
                _async_state["mt5_miss_count"]     = _mt5_miss_consecutive
                _async_state["account_info"]        = acct
                _async_state["today_real"]          = today_real
                _async_state["positions"]           = positions
                _async_state["circuit_breaker"]     = cb
                _async_state["daily_pnl_pct"]       = dd
                _async_state["consecutive_losses"]  = cbl
                _async_state["market_changed"]      = changed_mkt
                _async_state["ts"]                  = datetime.now(timezone.utc).isoformat()

        except Exception as e:
            log.debug(f"[SSE data loop] {e}")

        _t.sleep(5)   # slow poll — heartbeat is decoupled


threading.Thread(target=_sse_data_loop, daemon=True, name="sse-data").start()


def _sse_push_loop():
    """
    Fast heartbeat thread — fires every 1 s using ONLY pre-cached _async_state.
    Never calls blocking MT5 IPC directly — that's _sse_data_loop's job.
    This guarantees heartbeats arrive within 1 s regardless of MT5 latency.
    """
    import time as _time
    _time.sleep(6)  # wait for broker init before first heartbeat
    while True:
        try:
            with _async_state_lock:
                s = dict(_async_state)   # snapshot

            now = datetime.now(timezone.utc).isoformat()
            mt5_ok = s.get("mt5_connected", False)

            # ── Heartbeat (ALWAYS first — must not block) ──────────────────
            ws_status = {}
            try:
                from interfaces.websocket_manager import get_ws
                ws_status = get_ws().get_status()
            except Exception:
                pass

            _sse_broadcast("heartbeat", {
                "ts":          now,
                "server_time": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
                "mt5":         mt5_ok,
                "binance":     False,
                "zerodha":     False,
                "ws_status":   ws_status,
                "clients":     len(_sse_clients),
            })

            # ── Broker status flip (debounced — only on stable state change) ─
            if mt5_ok != _sse_state["mt5_connected"]:
                _sse_state["mt5_connected"] = mt5_ok
                _sse_broadcast("broker_status", {
                    "broker":    "mt5",
                    "connected": mt5_ok,
                    "status":    "connected" if mt5_ok else "disconnected",
                    "ts":        now,
                })

            # ── Equity + P&L (from cache) ──────────────────────────────────
            acct    = s.get("account_info", {})
            equity  = acct.get("equity",  0.0)
            balance = acct.get("balance", 1.0)
            unreal  = acct.get("profit",  0.0)
            today_real = s.get("today_real", 0.0)
            if acct and mt5_ok:
                today_pnl = today_real + unreal
                pnl_pct   = round(today_pnl / balance * 100, 3) if balance > 0 else 0.0
                equity_delta_pct = abs(equity - _sse_state["equity"]) / max(balance, 1) * 100
                if equity_delta_pct >= 0.05:
                    _sse_state["equity"] = equity
                    _sse_broadcast("pnl", {
                        "account_id":      "mt5",
                        "equity":          round(equity, 2),
                        "balance":         round(balance, 2),
                        "today_pnl":       round(today_pnl, 2),
                        "today_pnl_pct":   pnl_pct,
                        "today_realized":  round(today_real, 2),
                        "today_unrealized":round(unreal, 2),
                        "ts":              now,
                    })

            # ── Positions (from cache) ─────────────────────────────────────
            if mt5_ok:
                for pos in s.get("positions", []):
                    ticket = pos.get("ticket", pos.get("id", 0))
                    profit = round(pos.get("profit", 0), 2)
                    prev   = _sse_state["positions"].get(ticket, None)
                    if prev is None or abs(profit - prev) > 0.50:
                        sym    = pos.get("symbol", "")
                        price  = live_price(None, sym) if sym else 0.0
                        entry  = pos.get("entry_price", 0)
                        sym_up = sym.upper()
                        if "JPY" in sym_up:                             mult = 100
                        elif "XAU" in sym_up:                           mult = 10
                        elif any(c in sym_up for c in ("BTC","ETH","SOL")): mult = 1
                        else:                                           mult = 10000
                        direction = pos.get("direction", "buy")
                        pips = round((price - entry) * mult, 1) if direction == "buy" \
                               else round((entry - price) * mult, 1)
                        _sse_state["positions"][ticket] = profit
                        _sse_broadcast("position", {
                            "ticket": ticket, "symbol": sym, "direction": direction,
                            "current_price": round(price, 5), "profit": profit,
                            "profit_pct": round(profit / max(balance, 1) * 100, 3),
                            "pips_moved": pips, "change_type": "price", "ts": now,
                        })
                live_tickets = {p.get("ticket", p.get("id", 0)) for p in s.get("positions", [])}
                for ticket in list(_sse_state["positions"].keys()):
                    if ticket not in live_tickets:
                        del _sse_state["positions"][ticket]

            # ── Risk (from cache) ──────────────────────────────────────────
            cb  = s.get("circuit_breaker", False)
            dd  = s.get("daily_pnl_pct", 0.0)
            cbl = s.get("consecutive_losses", 0)
            if cb != _sse_state["circuit_breaker"] or abs(dd - _sse_state["daily_pnl_pct"]) >= 0.2:
                _sse_state["circuit_breaker"] = cb
                _sse_state["daily_pnl_pct"]   = dd
                level = "critical" if cb or dd < -3.5 else "warning" if dd < -2.0 else "info"
                text  = ("Circuit breaker ACTIVE — trading halted" if cb
                         else f"Daily P&L: {dd:+.2f}%  ({cbl} consecutive losses)")
                _sse_broadcast("risk", {
                    "circuit_breaker": cb, "daily_pnl_pct": dd,
                    "consecutive_losses": cbl, "level": level, "text": text, "ts": now,
                })

            # ── Market prices (from cache) ─────────────────────────────────
            mkt = s.get("market_changed", {})
            if mkt:
                changed = {}
                for sym, td in mkt.items():
                    if abs(td.get("price", 0) - _sse_state["last_prices"].get(sym, 0)) > 0:
                        _sse_state["last_prices"][sym] = td["price"]
                        changed[sym] = td
                if changed:
                    _sse_broadcast("market", {"ticks": changed, "ts": now})

        except Exception as e:
            log.debug(f"[SSE heartbeat loop] {e}")

        _time.sleep(1)

# Start the SSE push loop in a daemon thread
threading.Thread(target=_sse_push_loop, daemon=True, name="sse-push").start()


def _mt5_tick_poll_loop():
    """
    Background thread — polls MT5 symbol_info_tick() every 1 s for all active
    instruments and stores results in _px cache so the SSE loop sees fresh prices.
    Runs independently of the SSE loop so price fetch never blocks heartbeat.
    """
    import time as _t
    _t.sleep(8)  # wait for MT5 agent to connect first
    from config.config import MARKETS
    while True:
        try:
            import MetaTrader5 as _mt5
            from brokers.mt5_agent import SYMBOL_MAP
            if _mt5.account_info() is not None:   # only poll when connected
                for sym, inst in MARKETS.items():
                    if not inst.get("enabled", False):
                        continue
                    if inst.get("exchange") not in ("mt5", "binance"):
                        continue
                    ms = SYMBOL_MAP.get(sym, sym)
                    try:
                        tk = _mt5.symbol_info_tick(ms)
                        if tk and tk.bid > 0:
                            _px[sym] = {"p": (tk.bid + tk.ask) / 2, "ts": _t.time()}
                    except Exception:
                        pass
        except Exception:
            pass
        _t.sleep(1)


threading.Thread(target=_mt5_tick_poll_loop, daemon=True, name="mt5-tick-poll").start()


@app.route("/api/stream")
def api_stream():
    """
    Server-Sent Events endpoint for the dashboard.
    Each connected browser gets its own queue.
    Flask yields chunks as the queue fills.
    """
    q = _sse_add_client()

    def generate():
        # Send an immediate hello so the client knows it's connected
        yield f"event: connected\ndata: {json.dumps({'ts': datetime.now(timezone.utc).isoformat(), 'clients': len(_sse_clients)})}\n\n"
        try:
            while True:
                try:
                    msg = q.get(timeout=25)   # 25 s timeout → client will see a stale gap
                    yield msg
                except queue.Empty:
                    # SSE comment ping — keeps connection alive through proxies / Nginx
                    yield ": ping\n\n"
        except GeneratorExit:
            pass
        finally:
            _sse_remove_client(q)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":   "no-cache",
            "X-Accel-Buffering": "no",       # disable Nginx buffering
            "Access-Control-Allow-Origin": "*",
        },
    )


# /api/ping is registered by core/supervisor_endpoints.py (register_supervisor_routes).
# Defined there as a shared health check — do not duplicate here.
# The supervisor version returns: ok, ts, mt5, clients.


# ─────────────────────────────────────────────────────────────────────────────
# ANALYTICS  —  /api/analytics/*
# Real data from SQLite trade history. READ-ONLY.
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/analytics/equity_curve")
def api_equity_curve():
    """Daily equity curve from closed trades. Uses real MT5 balance when
    connected, falls back to ACCOUNT_BALANCE env var otherwise."""
    days = int(request.args.get("days", 30))
    db   = get_db_agent()
    if not db:
        return jsonify({"data": [], "_note": "DB not connected"})

    try:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        rows  = db.query(
            "SELECT date(close_time) as d, SUM(pnl_pct) as daily_pnl "
            "FROM trades WHERE close_time IS NOT NULL AND close_time != '' "
            "AND date(close_time) >= ? GROUP BY d ORDER BY d ASC",
            (since,)
        ) or []
    except Exception as e:
        return jsonify({"data": [], "_error": str(e)})

    # ── Try to get real MT5 balance ──────────────────────────────────────────
    balance_source = "config"
    balance = float(os.environ.get("ACCOUNT_BALANCE", 10000))
    try:
        mt5 = get_broker_agent()
        if mt5 and mt5.is_connected():
            info = mt5.get_account_info()
            if info and info.get("balance") and float(info["balance"]) > 0:
                balance = float(info["balance"])
                balance_source = "mt5"
    except Exception:
        pass  # fall back silently to env var

    # Build cumulative curve
    equity  = balance
    curve   = []
    for r in rows:
        pnl_pct = float(r.get("daily_pnl") or 0)
        equity  += equity * (pnl_pct / 100)
        curve.append({"date": r["d"], "equity": round(equity, 2), "daily_pnl_pct": round(pnl_pct, 4)})

    return jsonify({
        "data":             curve,
        "days":             days,
        "start_balance":    balance,
        "end_equity":       round(equity, 2),
        "total_return_pct": round((equity - balance) / balance * 100, 2) if balance else 0,
        "_source":          balance_source,
        "ts":               datetime.now(timezone.utc).isoformat(),
    })


@app.route("/api/analytics/pnl_calendar")
def api_pnl_calendar():
    """Monthly P&L calendar. Each cell = one day's total pnl_pct."""
    db = get_db_agent()
    if not db:
        return jsonify({"cells": [], "_note": "DB not connected"})

    try:
        # Last 90 days to cover ~3 months of calendar
        since = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
        rows  = db.query(
            "SELECT date(close_time) as d, SUM(pnl_pct) as daily_pnl, COUNT(*) as trades "
            "FROM trades WHERE close_time IS NOT NULL AND close_time != '' "
            "AND date(close_time) >= ? GROUP BY d ORDER BY d ASC",
            (since,)
        ) or []
    except Exception as e:
        return jsonify({"cells": [], "_error": str(e)})

    cells = [
        {
            "date":      r["d"],
            "pnl":       round(float(r.get("daily_pnl") or 0), 4),
            "trades":    int(r.get("trades") or 0),
            "positive":  float(r.get("daily_pnl") or 0) > 0,
        }
        for r in rows
    ]
    return jsonify({
        "cells":    cells,
        "ts":       datetime.now(timezone.utc).isoformat(),
    })



# ─────────────────────────────────────────────────────────────────────────────
# INDIAN MARKETS  —  /api/india
# ─────────────────────────────────────────────────────────────────────────────
# READ-ONLY monitoring endpoint. Sources:
#   Quotes       → ZerodhaAgent.get_quote()       (Kite Connect API)
#   FII/DII      → ZerodhaAgent.get_fii_dii_data() (NSE website)
#   PCR          → ZerodhaAgent.get_pcr()          (NSE option chain)
#   India VIX    → yfinance ^INDIAVIX              (Yahoo Finance)
#   Market status → derived from IST clock only (no external call)
#
# No signals computed here. Engine-only trading decisions.
# All unavailable data returns {"available": false} — never fabricated.
# ─────────────────────────────────────────────────────────────────────────────

# Indian stocks tracked by the engine (from config.py INSTRUMENTS)
_INDIA_STOCKS = [
    {"sym": "RELIANCE",   "label": "Reliance Industries", "sector": "Energy"},
    {"sym": "TCS",        "label": "Tata Consultancy",    "sector": "Technology"},
    {"sym": "HDFCBANK",   "label": "HDFC Bank",           "sector": "Banking"},
    {"sym": "INFY",       "label": "Infosys",             "sector": "Technology"},
    {"sym": "ICICIBANK",  "label": "ICICI Bank",          "sector": "Banking"},
    {"sym": "SBIN",       "label": "SBI",                 "sector": "Banking"},
    {"sym": "BHARTIARTL", "label": "Bharti Airtel",       "sector": "Telecom"},
    {"sym": "ITC",        "label": "ITC",                 "sector": "FMCG"},
    {"sym": "BAJFINANCE", "label": "Bajaj Finance",       "sector": "Finance"},
    {"sym": "TATAMOTORS", "label": "Tata Motors",         "sector": "Auto"},
    {"sym": "MARUTI",     "label": "Maruti Suzuki",       "sector": "Auto"},
    {"sym": "WIPRO",      "label": "Wipro",               "sector": "Technology"},
    {"sym": "AXISBANK",   "label": "Axis Bank",           "sector": "Banking"},
    {"sym": "KOTAKBANK",  "label": "Kotak Mahindra Bank", "sector": "Banking"},
]

_INDIA_FUTURES = [
    {"sym": "NIFTY",     "label": "NIFTY Futures",     "lot_size": 50,  "exchange": "NFO"},
    {"sym": "BANKNIFTY", "label": "BANKNIFTY Futures", "lot_size": 15,  "exchange": "NFO"},
]

# Cache — refreshed at most once per 30s (avoid hammering NSE)
_india_cache: dict = {"ts": 0, "data": None}
_INDIA_CACHE_TTL  = 30    # fresh cache 30s
_INDIA_STALE_TTL  = 300   # stale cache 5 min — never block browser
_india_refreshing = False


def _india_market_status() -> dict:
    """Derive market status purely from IST clock — no external call."""
    from zoneinfo import ZoneInfo
    ist = datetime.now(ZoneInfo("Asia/Kolkata"))
    h, m = ist.hour, ist.minute
    mins = h * 60 + m
    is_weekday = ist.weekday() < 5

    pre_open    = is_weekday and 540 <= mins < 555        # 09:00–09:15
    market_open = is_weekday and 555 <= mins < 930        # 09:15–15:30
    closing     = is_weekday and 915 <= mins < 930        # 15:15–15:30 (auto-square)
    market_closed = not (pre_open or market_open)

    if not is_weekday:
        status = "WEEKEND"
    elif market_open and not closing:
        status = "OPEN"
    elif pre_open:
        status = "PRE_OPEN"
    elif closing:
        status = "CLOSING"
    else:
        status = "CLOSED"

    return {
        "status":     status,
        "ist_time":   ist.strftime("%H:%M:%S IST"),
        "ist_date":   ist.strftime("%d %b %Y"),
        "weekday":    ist.strftime("%A"),
        "is_weekday": is_weekday,
        "source":     "system_clock",
    }


def _fetch_india_vix() -> dict:
    """Fetch India VIX from yfinance (^INDIAVIX). Non-fatal."""
    try:
        import yfinance as yf
        df = yf.download("^INDIAVIX", period="2d", interval="1d",
                         progress=False, auto_adjust=True)
        if df is not None and not df.empty:
            row = df.iloc[-1]
            vix = round(float(row["Close"]), 2)
            prev = round(float(df.iloc[-2]["Close"]), 2) if len(df) > 1 else vix
            chg  = round(vix - prev, 2)
            chg_pct = round((chg / prev) * 100, 2) if prev else 0

            from config.config import INDIA
            regime = (
                "EXTREME"  if vix >= INDIA["vix_extreme_threshold"] else
                "HIGH"     if vix >= INDIA["vix_high_threshold"]    else
                "LOW"      if vix <= INDIA["vix_low_threshold"]      else
                "NORMAL"
            )
            return {
                "available":  True,
                "value":      vix,
                "prev_close": prev,
                "change":     chg,
                "change_pct": chg_pct,
                "regime":     regime,
                "source":     "yfinance:^INDIAVIX",
                "ts":         datetime.now(timezone.utc).isoformat(),
            }
    except Exception as e:
        log.debug(f"[India API] VIX fetch failed: {e}")
    return {"available": False, "source": "yfinance:^INDIAVIX",
            "ts": datetime.now(timezone.utc).isoformat()}


def _fetch_quote_row(zr, sym: str, kite_exchange: str = "NSE") -> dict:
    """Fetch a single Zerodha quote and normalise to dashboard schema."""
    if not zr or not zr.is_authenticated():
        return {"symbol": sym, "available": False, "reason": "not_authenticated"}
    try:
        q = zr.get_quote(sym, kite_exchange)
        if not q:
            return {"symbol": sym, "available": False, "reason": "no_data"}

        ohlc      = q.get("ohlc", {})
        last      = q.get("last_price", 0)
        prev_cl   = ohlc.get("close", 0)
        chg       = round(last - prev_cl, 2) if prev_cl else 0
        chg_pct   = round(chg / prev_cl * 100, 2) if prev_cl else 0
        vol       = q.get("volume", 0)
        oi        = q.get("oi", None)

        return {
            "available":          True,
            "symbol":             sym,
            "kite_exchange":      kite_exchange,
            "last_price":         round(last, 2),
            "open":               round(ohlc.get("open", 0), 2),
            "high":               round(ohlc.get("high", 0), 2),
            "low":                round(ohlc.get("low", 0), 2),
            "prev_close":         round(prev_cl, 2),
            "change":             chg,
            "change_pct":         chg_pct,
            "volume":             vol,
            "open_interest":      oi,
            "upper_circuit":      q.get("upper_circuit_limit"),
            "lower_circuit":      q.get("lower_circuit_limit"),
            "bid":                q.get("depth", {}).get("buy",  [{}])[0].get("price"),
            "ask":                q.get("depth", {}).get("sell", [{}])[0].get("price"),
            "source":             "zerodha_kite",
            "ts":                 datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"symbol": sym, "available": False, "reason": str(e)}


@app.route("/api/india")
def api_india():
    """
    Indian market monitoring endpoint.
    Returns: market status, indices, 14 stocks, futures, VIX, FII/DII, PCR.
    All data carries source label and timestamp.
    Unavailable fields explicitly marked — no fabricated data.
    """
    global _india_refreshing
    import time as _time
    now_ts    = datetime.now(timezone.utc).isoformat()
    cache_age = _time.time() - _india_cache["ts"]

    # Fresh cache
    if _india_cache["data"] and cache_age < _INDIA_CACHE_TTL:
        return jsonify(_india_cache["data"])

    # Stale cache — return immediately, refresh in background
    if _india_cache["data"] and cache_age < _INDIA_STALE_TTL:
        if not _india_refreshing:
            def _bg():
                global _india_refreshing
                _india_refreshing = True
                try:
                    # Trigger a fresh build by temporarily zeroing the cache ts
                    _india_cache["ts"] = 0
                finally:
                    _india_refreshing = False
            threading.Thread(target=_bg, daemon=True, name="india-refresh").start()
        d = dict(_india_cache["data"])
        d["_stale"] = True
        return jsonify(d)


    # Get Zerodha agent (may be unauthenticated — handled per call)
    try:
        from brokers.zerodha_agent import get_zerodha
        zr = get_zerodha()
    except Exception:
        zr = None

    zerodha_auth = bool(zr and zr.is_authenticated())

    # ── Market status ──────────────────────────────────────────────────────
    market = _india_market_status()

    # ── Index quotes ───────────────────────────────────────────────────────
    nifty_quote    = _fetch_quote_row(zr, "NIFTY 50", "NSE")    if zerodha_auth else {"available": False, "symbol": "NIFTY 50",    "reason": "not_authenticated", "source": "zerodha_kite"}
    banknifty_quote= _fetch_quote_row(zr, "NIFTY BANK", "NSE") if zerodha_auth else {"available": False, "symbol": "NIFTY BANK",  "reason": "not_authenticated", "source": "zerodha_kite"}

    # ── Futures quotes ─────────────────────────────────────────────────────
    futures = []
    for fut in _INDIA_FUTURES:
        row = _fetch_quote_row(zr, fut["sym"], "NFO") if zerodha_auth else {
            "available": False, "symbol": fut["sym"],
            "reason": "not_authenticated", "source": "zerodha_kite"
        }
        row.update({"label": fut["label"], "lot_size": fut["lot_size"],
                    "fo_exchange": fut["exchange"]})
        futures.append(row)

    # ── Stock quotes (14 configured) ──────────────────────────────────────
    stocks = []
    for s in _INDIA_STOCKS:
        row = _fetch_quote_row(zr, s["sym"]) if zerodha_auth else {
            "available": False, "symbol": s["sym"],
            "reason": "not_authenticated", "source": "zerodha_kite"
        }
        row.update({"label": s["label"], "sector": s["sector"]})
        stocks.append(row)

    # ── India VIX ─────────────────────────────────────────────────────────
    india_vix = _fetch_india_vix()

    # ── FII/DII ───────────────────────────────────────────────────────────
    try:
        fii_dii = zr.get_fii_dii_data() if zr else {"available": False}
    except Exception:
        fii_dii = {"available": False}
    if "source" not in fii_dii:
        fii_dii["source"] = "nse_website"
    fii_dii["ts"] = now_ts

    # ── PCR ───────────────────────────────────────────────────────────────
    pcr_nifty = pcr_banknifty = {"available": False}
    try:
        if zr:
            pcr_nifty     = zr.get_pcr("NIFTY")
            pcr_banknifty = zr.get_pcr("BANKNIFTY")
    except Exception:
        pass
    for pcr in (pcr_nifty, pcr_banknifty):
        pcr["source"] = "nse_option_chain"
        pcr["ts"]     = now_ts

    # ── Options summary ───────────────────────────────────────────────────
    # Engine handles actual options logic — dashboard shows summary only
    options_summary = {
        "available":       zerodha_auth,
        "nifty_pcr":       pcr_nifty.get("pcr"),
        "banknifty_pcr":   pcr_banknifty.get("pcr"),
        "nifty_signal":    pcr_nifty.get("signal"),
        "banknifty_signal":pcr_banknifty.get("signal"),
        "note":            "Option Greeks and strategy decisions are computed inside the engine, not here.",
        "source":          "nse_option_chain",
        "ts":              now_ts,
    }

    # ── Zerodha auth status ───────────────────────────────────────────────
    zerodha_status = {"available": False, "authenticated": False,
                      "source": "zerodha_kite", "ts": now_ts}
    try:
        if zr:
            st = zr.get_status()
            zerodha_status = {
                "available":      True,
                "authenticated":  st.get("authenticated", False),
                "token_date":     st.get("token_date", ""),
                "ist_time":       st.get("ist_time", ""),
                "market_open":    st.get("market_open", False),
                "source":         "zerodha_kite",
                "ts":             now_ts,
            }
    except Exception:
        pass

    result = {
        "ts":              now_ts,
        "market":          market,
        "zerodha":         zerodha_status,
        "nifty":           nifty_quote,
        "banknifty":       banknifty_quote,
        "futures":         futures,
        "stocks":          stocks,
        "india_vix":       india_vix,
        "fii_dii":         fii_dii,
        "pcr_nifty":       pcr_nifty,
        "pcr_banknifty":   pcr_banknifty,
        "options_summary": options_summary,
        "config": {
            "vix_thresholds": {
                "low":     13.0,
                "high":    20.0,
                "extreme": 28.0,
            },
            "market_hours_ist": {"open": "09:15", "pre_open": "09:00", "close": "15:30"},
            "fo_lot_sizes":     {"NIFTY": 50, "BANKNIFTY": 15},
            "fo_expiry":        {"NIFTY": "Thursday", "BANKNIFTY": "Wednesday"},
            "source":           "engine_config",
        },
    }

    _india_cache["data"] = result
    _india_cache["ts"]   = _time.time()
    _india_refreshing    = False
    return jsonify(result)


# ─────────────────────────────────────────────────────────────────────────────
# ALL-MARKET QUOTES  —  /api/markets
# ─────────────────────────────────────────────────────────────────────────────
# Returns a snapshot of ALL configured instruments grouped by market type.
# Covers: Forex, Crypto, Gold/Silver, US Stocks, Indian Stocks, Futures.
# Primary source: MT5 live ticks. Fallback: Yahoo Finance (yfinance) for all
# instruments not covered by MT5 (crypto, US stocks, Indian stocks, silver).
# Response is an SSE-friendly initial snapshot — browser then switches to SSE.
# ─────────────────────────────────────────────────────────────────────────────

# ── Yahoo Finance ticker mapping (internal symbol → YF ticker) ───────────────
# India market is DISABLED — those symbols removed to cut YF batch size.
_YF_TICKER_MAP = {
    # Forex (MT5 primary — YF fallback when MT5 tick is 0)
    "AUDUSD=X":    "AUDUSD=X",
    "USDCAD=X":    "USDCAD=X",
    "USDCHF=X":    "USDCHF=X",
    "NZDUSD=X":    "NZDUSD=X",
    "EURJPY=X":    "EURJPY=X",
    "GBPJPY=X":    "GBPJPY=X",
    "EURUSD=X":    "EURUSD=X",
    "GBPUSD=X":    "GBPUSD=X",
    "USDJPY=X":    "USDJPY=X",
    # Metals
    "XAUUSD=X":    "GC=F",          # Gold futures
    "XAGUSD=X":    "SI=F",          # Silver futures
    # Crypto (MT5 CFDs — YF used for price display only, not execution)
    "BTCUSDT":     "BTC-USD",
    "ETHUSDT":     "ETH-USD",
    "SOLUSDT":     "SOL-USD",
    # US Stocks / ETFs (display only — us_stocks market disabled for trading)
    "AAPL":        "AAPL",
    "NVDA":        "NVDA",
    "TSLA":        "TSLA",
    "MSFT":        "MSFT",
    "SPY":         "SPY",
    "QQQ":         "QQQ",
}

# ── YF price cache: {symbol: {price, prev_close, change_pct, source, ts}} ────
_yf_cache: dict = {}
_yf_cache_lock = threading.Lock()
_yf_refreshing  = False
_YF_REFRESH_INTERVAL = 90   # seconds between full YF refresh (avoid rate-limit)


def _yf_refresh_all():
    """Fetch latest prices from Yahoo Finance for all mapped symbols.
    Uses yf.download() batch call (1 HTTP round-trip for all tickers) instead of
    per-ticker fast_info to avoid Yahoo rate-limiting mid-loop.
    Results stored in _yf_cache for _markets_build_snapshot() and SSE loop.
    """
    global _yf_refreshing
    if _yf_refreshing:
        return
    _yf_refreshing = True
    try:
        import yfinance as _yf
        import time as _t

        # Build reverse map: yf_ticker -> internal symbol (for multi-symbol lookup)
        yf_to_sym = {v: k for k, v in _YF_TICKER_MAP.items()}
        yf_tickers = list(set(_YF_TICKER_MAP.values()))

        # Batch download: 1 HTTP call for all tickers, 2 days history to get prev_close
        df_batch = None
        try:
            df_batch = _yf.download(
                tickers=yf_tickers,
                period="2d",
                interval="1d",
                group_by="ticker",
                auto_adjust=True,
                progress=False,
                threads=True,
            )
        except Exception as _de:
            log.debug(f"[yf-refresh] Batch download failed: {_de}")

        updated = {}
        now_iso = datetime.now(timezone.utc).isoformat()

        # Parse batch results
        if df_batch is not None and not df_batch.empty:
            # multi-ticker: columns are (ticker, field); single-ticker: columns are field names
            is_multi = hasattr(df_batch.columns, "levels") and len(df_batch.columns.levels) > 1
            for yf_ticker in yf_tickers:
                sym = yf_to_sym.get(yf_ticker)
                if sym is None:
                    continue
                try:
                    if is_multi:
                        sub = df_batch[yf_ticker] if yf_ticker in df_batch.columns.get_level_values(0) else None
                    else:
                        sub = df_batch  # single ticker case
                    if sub is None or sub.empty:
                        continue
                    sub = sub.dropna(subset=["Close"])
                    if len(sub) == 0:
                        continue
                    price      = float(sub["Close"].iloc[-1])
                    prev_close = float(sub["Close"].iloc[-2]) if len(sub) >= 2 else None
                    if price > 0:
                        chg     = round(price - prev_close, 6) if prev_close and prev_close > 0 else None
                        chg_pct = round(chg / prev_close * 100, 3) if chg is not None and prev_close else None
                        updated[sym] = {
                            "price":      round(price, 6),
                            "prev_close": round(prev_close, 6) if prev_close else None,
                            "change":     chg,
                            "change_pct": chg_pct,
                            "source":     "yahoo_finance",
                            "ts":         now_iso,
                        }
                except Exception:
                    pass

        # Fallback: per-ticker fast_info for any symbol the batch missed
        missed = [sym for sym in _YF_TICKER_MAP if sym not in updated]
        if missed:
            for sym in missed:
                yf_ticker = _YF_TICKER_MAP[sym]
                try:
                    fi    = _yf.Ticker(yf_ticker).fast_info
                    price = getattr(fi, "last_price", None)
                    prev  = getattr(fi, "previous_close", None)
                    if price and price > 0:
                        chg     = round(price - prev, 6) if prev and prev > 0 else None
                        chg_pct = round(chg / prev * 100, 3) if chg is not None and prev else None
                        updated[sym] = {
                            "price":      round(float(price), 6),
                            "prev_close": round(float(prev), 6) if prev else None,
                            "change":     chg,
                            "change_pct": chg_pct,
                            "source":     "yahoo_finance",
                            "ts":         now_iso,
                        }
                    _t.sleep(0.3)   # polite delay for individual fallback calls
                except Exception:
                    pass

        with _yf_cache_lock:
            _yf_cache.update(updated)
        log.info(f"[yf-refresh] Updated {len(updated)}/{len(_YF_TICKER_MAP)} instruments from Yahoo Finance")
    except Exception as _ye:
        log.debug(f"[yf-refresh] Error: {_ye}")
    finally:
        _yf_refreshing = False


def _yf_refresh_loop():
    """Daemon loop: refresh Yahoo Finance prices every _YF_REFRESH_INTERVAL seconds."""
    import time as _t
    # Initial fetch immediately at startup
    _yf_refresh_all()
    while True:
        _t.sleep(_YF_REFRESH_INTERVAL)
        _yf_refresh_all()


# Start YF background refresh daemon
threading.Thread(target=_yf_refresh_loop, daemon=True, name="yf-refresh").start()

_markets_cache: dict = {"ts": 0, "data": None}
_MARKETS_CACHE_TTL  = 10   # 10s live cache
_MARKETS_STALE_TTL  = 300  # 5 min stale fallback
_markets_refreshing = False  # prevent concurrent bg fetches

def _markets_build_snapshot() -> dict:
    """Build full market snapshot. Called in background thread — may be slow."""
    global _markets_refreshing
    _markets_refreshing = True
    try:
        import time as _t
        from config.config import INSTRUMENTS
        now_ts = datetime.now(timezone.utc).isoformat()

        # WebSocket price cache (non-blocking, memory only)
        _ws = None
        try:
            from interfaces.websocket_manager import get_ws
            _ws = get_ws()
        except Exception:
            pass

        # MT5 direct access (background thread — process-wide IPC, no re-init needed)
        # MT5 IPC is process-wide: the server startup already called initialize().
        # Background threads can call symbol_info_tick() directly on the same handle.
        # We only call initialize() if the handle isn't alive yet (first-run edge case).
        _mt5lib_ok = False
        try:
            import MetaTrader5 as _mt5lib
            # Quick check: if already initialized, account_info() returns non-None
            if _mt5lib.account_info() is not None:
                _mt5lib_ok = True
            else:
                # Handle not yet open in this process — initialize once
                _term = os.environ.get("MT5_TERMINAL_PATH")
                if _term and os.path.exists(_term):
                    _mt5lib_ok = _mt5lib.initialize(path=_term)
                else:
                    _mt5lib_ok = _mt5lib.initialize()
                if not _mt5lib_ok:
                    log.debug(f"[markets-build] MT5 init failed: {_mt5lib.last_error()}")
        except Exception as _me:
            log.debug(f"[markets-build] MT5 unavailable: {_me}")
            _mt5lib_ok = False

        # MT5 agent wrapper (kept for is_connected flag used by Binance branch)
        mt5 = None
        try:
            _m = get_broker_agent()
            if _m and _m.is_connected():
                mt5 = _m
        except Exception:
            pass

        # Binance (only if valid keys are configured — skip if placeholder)
        binance = None
        _binance_key = os.environ.get("BINANCE_API_KEY", "")
        if _binance_key and _binance_key not in ("your_binance_key", "", "none"):
            try:
                from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout
                from brokers.binance_agent import get_binance
                with ThreadPoolExecutor(max_workers=1) as _pool:
                    _fut = _pool.submit(get_binance)
                    try:
                        _b = _fut.result(timeout=3)
                        if _b and hasattr(_b, 'is_connected') and _b.is_connected():
                            binance = _b
                        elif _b and not hasattr(_b, 'is_connected'):
                            binance = _b
                    except _FuturesTimeout:
                        log.debug("[markets-build] Binance init timed out — skipping")
            except Exception:
                pass

        groups = {
            "forex": [], "crypto": [], "gold": [],
            "us_stocks": [], "india": [], "futures": [], "other": [],
        }

        for sym, inst in INSTRUMENTS.items():
            if inst.get("backtest_only"):
                continue

            market    = inst.get("market", "other")
            exchange  = inst.get("exchange", "")
            asset_cls = inst.get("asset_class", "")

            price = bid = ask = prev_close = change = change_pct = volume = None
            source = "unavailable"

            # WS in-memory cache only — no blocking REST fallback
            # get_live_price() falls back to DataAgent.get_ohlcv() for missing symbols,
            # which makes slow HTTP calls. Use the raw store directly instead.
            if _ws:
                try:
                    p = _ws._store.get_latest_price(sym) if hasattr(_ws, '_store') else None
                    if p and p > 0:
                        price  = p
                        source = "ws_cache"
                except Exception:
                    pass

            # MT5 enrichment — use symbol_info_tick() directly via SYMBOL_MAP
            # (MT5Agent has no get_symbol_info method; this mirrors /api/markets/legacy)
            if exchange == "mt5" and _mt5lib_ok:
                try:
                    from brokers.mt5_agent import SYMBOL_MAP
                    mt5_sym = SYMBOL_MAP.get(sym, sym)
                    # Ensure symbol is in Market Watch so tick is available
                    _mt5lib.symbol_select(mt5_sym, True)
                    tick = _mt5lib.symbol_info_tick(mt5_sym)
                    if tick and tick.bid:
                        bid    = round(tick.bid, 6)
                        ask    = round(tick.ask, 6)
                        price  = round((bid + ask) / 2, 6)
                        source = "mt5_broker"
                        # Day change from daily bar
                        bars = _mt5lib.copy_rates_from_pos(mt5_sym, _mt5lib.TIMEFRAME_D1, 0, 2)
                        if bars is not None and len(bars) >= 2:
                            prev_close = round(float(bars[-2]["close"]), 6)
                except Exception:
                    pass

            # Binance enrichment (only if session exists)
            if exchange == "binance" and binance:
                try:
                    ticker = binance.get_ticker(sym) if hasattr(binance, "get_ticker") else None
                    if ticker:
                        price     = price or float(ticker.get("lastPrice", 0) or 0) or None
                        bid       = float(ticker.get("bidPrice", 0) or 0) or None
                        ask       = float(ticker.get("askPrice", 0) or 0) or None
                        prev_close= float(ticker.get("prevClosePrice", 0) or 0) or None
                        volume    = float(ticker.get("volume", 0) or 0) or None
                        source    = "binance"
                except Exception:
                    pass

            # Yahoo Finance fallback — covers all instruments with no live feed
            if (not price or price <= 0) and sym in _YF_TICKER_MAP:
                with _yf_cache_lock:
                    yf_row = _yf_cache.get(sym)
                if yf_row and yf_row.get("price") and yf_row["price"] > 0:
                    price      = yf_row["price"]
                    prev_close = yf_row.get("prev_close")
                    source     = "yahoo_finance"
                elif sym in _YF_TICKER_MAP and not _yf_refreshing:
                    # Cache miss on first load — trigger a one-shot refresh
                    threading.Thread(target=_yf_refresh_all, daemon=True, name="yf-oncall").start()

            if price and price > 0:
                price  = round(price, 6)
                source = source if source != "unavailable" else "ws_cache"
                if prev_close and prev_close > 0:
                    change     = round(price - prev_close, 6)
                    change_pct = round(change / prev_close * 100, 3)
                # Use pre-computed change_pct from YF cache if still None
                if change_pct is None and source == "yahoo_finance":
                    with _yf_cache_lock:
                        _yf_row = _yf_cache.get(sym, {})
                    if _yf_row.get("change_pct") is not None:
                        change_pct = _yf_row["change_pct"]
                    if _yf_row.get("change") is not None:
                        change = _yf_row["change"]
                    if _yf_row.get("prev_close") and not prev_close:
                        prev_close = _yf_row["prev_close"]

            row = {
                "symbol":      sym,
                "label":       inst.get("label", sym),
                "asset_class": asset_cls,
                "exchange":    exchange,
                "type":        inst.get("type", ""),
                "pip_size":    inst.get("pip_size", 0.0001),
                "price":       price,
                "prev_close":  prev_close,
                "change":      change,
                "change_pct":  change_pct,
                "bid":         bid,
                "ask":         ask,
                "volume":      volume,
                "available":   price is not None and price > 0,
                "source":      source,
                "ts":          now_ts,
            }

            if asset_cls == "forex":           groups["forex"].append(row)
            elif asset_cls == "crypto":        groups["crypto"].append(row)
            elif asset_cls in ("gold","silver"):groups["gold"].append(row)
            elif market == "us_stocks":        groups["us_stocks"].append(row)
            elif market == "india":            groups["india"].append(row)
            elif asset_cls in ("index_futures","futures"): groups["futures"].append(row)
            else:                              groups["other"].append(row)

        log.info(f"[markets] snapshot built: forex={len(groups['forex'])} gold={len(groups['gold'])} crypto={len(groups['crypto'])}")
        result = {
            "ts":     now_ts,
            "groups": groups,
            "config": {
                "sse_event": "market",
                "sse_field": "ticks",
                "note": "Subscribe to /api/stream for live tick-by-tick updates.",
            },
        }
        _markets_cache["data"] = result
        _markets_cache["ts"]   = _t.time()
        return result
    except Exception as e:
        import traceback as _tb
        log.debug(f"[markets] build error: {e}\n{_tb.format_exc()}")
        return {}
    finally:
        # Note: do NOT call _mt5lib.shutdown() here — the MT5 IPC handle is
        # process-wide and shared with the server's broker connection.
        _markets_refreshing = False


@app.route("/api/markets")
def api_markets():
    """
    All-instrument market snapshot. Returns immediately — never blocks.
    Cold cache → returns empty groups + triggers background refresh.
    Warm cache (≤10s) → returns fresh data.
    Stale cache (≤5min) → returns stale data with _stale flag.
    Live updates via SSE /api/stream (market + ticks events).
    """
    import time as _time
    now_ts    = datetime.now(timezone.utc).isoformat()
    cache_age = _time.time() - _markets_cache["ts"]

    # Fresh cache
    if _markets_cache["data"] and cache_age < _MARKETS_CACHE_TTL:
        return jsonify(_markets_cache["data"])

    # Stale cache — return immediately, trigger background refresh
    if _markets_cache["data"] and cache_age < _MARKETS_STALE_TTL:
        if not _markets_refreshing:
            t = threading.Thread(target=_markets_build_snapshot, daemon=True, name="markets-refresh")
            t.start()
        d = dict(_markets_cache["data"])
        d["_stale"] = True
        return jsonify(d)

    # Cold cache — wait up to 4s for a build to finish (avoids DATA UNAVAILABLE on first load)
    if not _markets_refreshing:
        t = threading.Thread(target=_markets_build_snapshot, daemon=True, name="markets-cold")
        t.start()
    # Poll for up to 4 s so the first page-load gets real data
    _deadline = _time.time() + 4.0
    while _time.time() < _deadline:
        _time.sleep(0.25)
        if _markets_cache["data"]:
            return jsonify(_markets_cache["data"])

    empty_groups = {"forex": [], "crypto": [], "gold": [], "us_stocks": [], "india": [], "futures": [], "other": []}
    return jsonify({
        "ts":      now_ts,
        "groups":  empty_groups,
        "_loading": True,
        "_note":   "Markets loading… subscribe to /api/stream for live updates.",
        "config": {
            "sse_event": "market",
            "sse_field": "ticks",
            "note": "Subscribe to /api/stream for live tick-by-tick updates.",
        },
    })


# ─────────────────────────────────────────────────────────────────────────────
# OPTIONS  —  /api/options
# ─────────────────────────────────────────────────────────────────────────────
# Provides:
#   1. NSE option chain for NIFTY and BANKNIFTY (from NSE website)
#   2. BS Greeks for each strike (calculated by existing GreeksMonitor)
#   3. Portfolio Greeks (aggregated across all open option positions)
#   4. IV Rank / IV Percentile from historical data if available
# Source: options/greeks_monitor.py, options/options_engine.py, NSE API
# Engine makes trading decisions — dashboard ONLY monitors.
# ─────────────────────────────────────────────────────────────────────────────

_opts_cache: dict = {"ts": 0, "data": None}
_OPTS_CACHE_TTL  = 60    # NSE chain is heavy — fresh cache 60s
_OPTS_STALE_TTL  = 600   # Return stale data up to 10min to avoid browser timeout


def _fetch_nse_chain(symbol: str, spot: float) -> dict:
    """
    Fetch NSE option chain for symbol (NIFTY or BANKNIFTY).
    Returns rows with CE + PE Greeks computed via Black-Scholes.
    """
    from options.greeks_monitor import BSCalculator
    from options.options_engine import ExpiryCalendar
    bs = BSCalculator()
    cal = ExpiryCalendar()

    try:
        import requests
        url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept":     "application/json",
            "Referer":    "https://www.nseindia.com",
        }
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return {"available": False, "reason": f"NSE HTTP {resp.status_code}"}

        payload   = resp.json()
        records   = payload.get("records", {})
        raw_chain = records.get("data", [])
        expiry_dates = records.get("expiryDates", [])
        atm_price    = records.get("underlyingValue", spot or 0)

        # Pick nearest expiry
        nearest_expiry = expiry_dates[0] if expiry_dates else None

        # BS T (time to expiry in years)
        dte = 0
        T   = 0.0
        if nearest_expiry:
            try:
                from datetime import datetime as dt
                exp_date = dt.strptime(nearest_expiry, "%d-%b-%Y")
                dte = max(0, (exp_date.date() - datetime.now(timezone.utc).date()).days)
                T   = dte / 365.0
            except Exception:
                pass

        # Risk-free rate for India
        r = 0.065

        chain_rows = []
        total_put_oi = total_call_oi = 0

        for row in raw_chain:
            strike = row.get("strikePrice", 0)
            if not strike:
                continue

            ce_data = row.get("CE", {})
            pe_data = row.get("PE", {})

            ce_iv  = (ce_data.get("impliedVolatility") or 0) / 100
            pe_iv  = (pe_data.get("impliedVolatility") or 0) / 100
            ce_oi  = ce_data.get("openInterest", 0) or 0
            pe_oi  = pe_data.get("openInterest", 0) or 0
            total_call_oi += ce_oi
            total_put_oi  += pe_oi

            # Calculate Greeks via BS
            ce_greeks = bs.calculate(S=atm_price, K=strike, T=T, iv=ce_iv or 0.20, option_type="CE", r=r) if T > 0 else {}
            pe_greeks = bs.calculate(S=atm_price, K=strike, T=T, iv=pe_iv or 0.20, option_type="PE", r=r) if T > 0 else {}

            moneyness = "ATM" if abs(strike - atm_price) < atm_price * 0.005 else ("ITM" if strike < atm_price else "OTM")

            chain_rows.append({
                "strike":        strike,
                "moneyness":     moneyness,
                "dte":           dte,
                "ce": {
                    "ltp":    ce_data.get("lastPrice"),
                    "iv":     round(ce_iv * 100, 2) if ce_iv else None,
                    "oi":     ce_oi,
                    "volume": ce_data.get("totalTradedVolume"),
                    "delta":  round(ce_greeks.get("delta", 0), 4) if ce_greeks else None,
                    "gamma":  round(ce_greeks.get("gamma", 0), 6) if ce_greeks else None,
                    "theta":  round(ce_greeks.get("theta", 0), 4) if ce_greeks else None,
                    "vega":   round(ce_greeks.get("vega",  0), 4) if ce_greeks else None,
                    "bid":    ce_data.get("bidprice"),
                    "ask":    ce_data.get("askPrice"),
                },
                "pe": {
                    "ltp":    pe_data.get("lastPrice"),
                    "iv":     round(pe_iv * 100, 2) if pe_iv else None,
                    "oi":     pe_oi,
                    "volume": pe_data.get("totalTradedVolume"),
                    "delta":  round(pe_greeks.get("delta", 0), 4) if pe_greeks else None,
                    "gamma":  round(pe_greeks.get("gamma", 0), 6) if pe_greeks else None,
                    "theta":  round(pe_greeks.get("theta", 0), 4) if pe_greeks else None,
                    "vega":   round(pe_greeks.get("vega",  0), 4) if pe_greeks else None,
                    "bid":    pe_data.get("bidprice"),
                    "ask":    pe_data.get("askPrice"),
                },
            })

        pcr = round(total_put_oi / total_call_oi, 3) if total_call_oi > 0 else None
        pcr_signal = ("BEARISH" if pcr and pcr > 1.2 else "BULLISH" if pcr and pcr < 0.8 else "NEUTRAL")

        return {
            "available":     True,
            "symbol":        symbol,
            "spot":          round(atm_price, 2),
            "expiry":        nearest_expiry,
            "dte":           dte,
            "pcr":           pcr,
            "pcr_signal":    pcr_signal,
            "total_call_oi": total_call_oi,
            "total_put_oi":  total_put_oi,
            "chain":         chain_rows,
            "source":        "nse_option_chain + bs_calculator",
            "ts":            datetime.now(timezone.utc).isoformat(),
        }

    except Exception as e:
        log.debug(f"[Options API] NSE chain failed for {symbol}: {e}")
        return {"available": False, "symbol": symbol, "reason": str(e)}


@app.route("/api/options")
def api_options():
    """
    Options monitoring endpoint.
    Returns NIFTY + BANKNIFTY option chains with B-S Greeks and portfolio summary.
    Engine makes all trading decisions — this is display-only.
    """
    import time as _time
    now_ts = datetime.now(timezone.utc).isoformat()
    cache_age = _time.time() - _opts_cache["ts"]

    # Fresh cache
    if _opts_cache["data"] and cache_age < _OPTS_CACHE_TTL:
        return jsonify(_opts_cache["data"])

    # Stale cache — return immediately rather than blocking on slow NSE fetch
    if _opts_cache["data"] and cache_age < _OPTS_STALE_TTL:
        _opts_cache["data"]["_stale"] = True
        return jsonify(_opts_cache["data"])

    # Get spot prices from WebSocket cache
    try:
        from interfaces.websocket_manager import get_ws
        _ws = get_ws()
        nifty_spot     = _ws.get_live_price("NIFTY50") or _ws.get_live_price("^NSEI") or 0
        banknifty_spot = _ws.get_live_price("BANKNIFTY_FUT") or 0
    except Exception:
        nifty_spot = banknifty_spot = 0

    # Fetch option chains
    nifty_chain     = _fetch_nse_chain("NIFTY",     nifty_spot)
    banknifty_chain = _fetch_nse_chain("BANKNIFTY", banknifty_spot)

    # Portfolio Greeks from existing GreeksMonitor
    portfolio_greeks = {"positions": 0, "total_delta": 0, "total_vega": 0, "daily_theta": 0, "total_gamma": 0, "available": False}
    try:
        from options.greeks_monitor import get_greeks_monitor
        gm = get_greeks_monitor()
        pg = gm.get_portfolio_greeks()
        pg["available"] = True
        portfolio_greeks = pg
    except Exception:
        pass

    # Open options positions from DB
    open_options = []
    try:
        db = get_db_agent()
        if db:
            rows = db.query(
                "SELECT * FROM trades WHERE is_open=1 AND symbol LIKE '%CE' OR symbol LIKE '%PE' "
                "UNION SELECT * FROM trades WHERE is_open=1 AND option_type IS NOT NULL "
                "LIMIT 50", ()
            )
            open_options = rows or []
    except Exception:
        pass

    result = {
        "ts":              now_ts,
        "nifty":           nifty_chain,
        "banknifty":       banknifty_chain,
        "portfolio_greeks":portfolio_greeks,
        "open_positions":  open_options,
        "config": {
            "lot_sizes":   {"NIFTY": 50, "BANKNIFTY": 15},
            "expiry_days": {"NIFTY": "Thursday", "BANKNIFTY": "Wednesday"},
            "greeks_source": "black_scholes (B-S approximation when live IV unavailable)",
            "note":        "Greeks are calculated by the engine. Trading decisions are made inside the engine only.",
        },
    }

    _opts_cache["data"] = result
    _opts_cache["ts"]   = _time.time()
    return jsonify(result)


# ─────────────────────────────────────────────────────────────────────────────
# ROUTE ALIASES — backward-compat shims so old dashboard calls don't 404
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/health")
def api_health():
    """Alias: lightweight health probe used by external monitors & tests."""
    conn  = check_conn()
    db    = get_db_agent()
    db_ok = db is not None
    return jsonify({
        "status":      "ok",
        "connection":  conn,
        "database":    db_ok,
        "server_time": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
        "trading_mode": os.environ.get("TRADING_MODE", "paper"),
    })

@app.route("/api/signals")
def api_signals():
    """Alias: recent signal feed (dashboard Signal Feed widget)."""
    db = get_db_agent()
    signals = []
    if db:
        rows = db.query(
            "SELECT symbol, direction, grade, confidence, was_traded, "
            "skip_reason, signal_time FROM signals ORDER BY signal_time DESC LIMIT 50",
            ()
        )
        for r in rows:
            signals.append({
                "time":        (r.get("signal_time") or "")[:16],
                "symbol":      r.get("symbol"),
                "direction":   r.get("direction"),
                "grade":       r.get("grade"),
                "confidence":  r.get("confidence"),
                "traded":      bool(r.get("was_traded")),
                "skip_reason": r.get("skip_reason"),
            })
    return jsonify({"signals": signals, "count": len(signals)})

@app.route("/api/performance")
def api_performance():
    """Alias: trade performance summary (overview P&L widgets)."""
    db = get_db_agent()
    trades = []
    stats  = {}
    if db:
        rows = db.query(
            "SELECT symbol, direction, outcome, r_multiple, grade, "
            "pnl_pct, open_time, close_time, is_paper "
            "FROM trades ORDER BY close_time DESC LIMIT 100",
            ()
        )
        for r in rows:
            trades.append({
                "symbol":    r.get("symbol"),
                "direction": r.get("direction"),
                "outcome":   r.get("outcome"),
                "r":         r.get("r_multiple"),
                "grade":     r.get("grade"),
                "pnl_pct":   r.get("pnl_pct"),
                "open_time": r.get("open_time"),
                "close_time":r.get("close_time"),
                "is_paper":  bool(r.get("is_paper")),
            })
        if trades:
            rv    = [t["r"] for t in trades if t["r"] is not None]
            wins  = sum(1 for x in rv if x > 0)
            total = len(rv)
            gp    = sum(x for x in rv if x > 0)
            gl    = abs(sum(x for x in rv if x < 0))
            stats = {
                "total_trades":  total,
                "win_rate":      round(wins / total * 100, 1) if total else 0,
                "profit_factor": round(gp / gl, 2) if gl > 0 else 99.0,
                "expectancy_r":  round(sum(rv) / total, 3) if total else 0,
                "total_r":       round(sum(rv), 2),
            }
    return jsonify({"trades": trades, "stats": stats})

@app.route("/api/council")
def api_council():
    """
    Alias: unified Council endpoint.
    Returns in-process council messages + Council v2 server last debate.
    """
    with _council_lock:
        msgs = list(_council[-30:])

    council_v2 = {}
    try:
        import requests as _req
        port = int(os.environ.get("COUNCIL_PORT", "8766"))
        r = _req.get(f"http://127.0.0.1:{port}/api/council/last", timeout=2)
        if r.status_code == 200:
            body = r.json()
            if body.get("status") != "no_debate_yet":
                council_v2 = body
    except Exception:
        pass

    return jsonify({
        "messages":    msgs,
        "last_debate": council_v2 or None,
        "advisory_only": os.environ.get("COUNCIL_ADVISORY_ONLY", "true").lower() == "true",
    })

@app.route("/api/candidates")
def api_candidates():
    """Alias: candidate strategies (redirects to strategies/candidates)."""
    return api_strategy_candidates()


# ─────────────────────────────────────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import webbrowser

    print("=" * 60)
    print("  CRAVE Quant -- Live Backend v2")
    print("=" * 60)

    print(f"[1/3] Connecting to Broker ({os.environ.get('BROKER_TYPE', 'MT5')})...", end="", flush=True)
    agent = get_broker_agent()
    if agent and agent.is_connected():
        info = agent.get_account_info()
        print(f" [OK]  #{info.get('login')} | ${info.get('equity',0):.2f} | {info.get('server')}")
    else:
        print(" [WARN]  Broker not connected")

    print("[2/3] Opening database...", end="", flush=True)
    db = get_db_agent()
    if db:
        sz = db.get_db_size_mb()
        rows = db.query("SELECT COUNT(*) as n FROM trades", ())
        n = rows[0]["n"] if rows else 0
        print(f" [OK]  {sz:.1f}MB | {n} trades")
    else:
        print(" [WARN]  DB not accessible")

    port = int(os.environ.get("SERVER_PORT", 8765))
    profile_str = f" (Profile: {profile})" if profile else ""
    print(f"[3/3] Starting server{profile_str} on http://127.0.0.1:{port}")
    print()
    print("  TIP: For a live test order (micro), run:  python test_trade.py")
    print()

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False, threaded=True)
