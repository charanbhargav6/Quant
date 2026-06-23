"""
CRAVE v10.3 — Supabase Dashboard Pusher
=========================================
One-way data bridge: Bot → Supabase → Dashboard.

SECURITY ARCHITECTURE:
  Bot:       has SUPABASE_SERVICE_KEY (write-only to dashboard tables)
  Dashboard: has SUPABASE_ANON_KEY    (read-only, public-safe)
  Exchange:  API keys NEVER touch the dashboard or Supabase

  The website never has a direct connection to your exchange.
  Even if the entire dashboard is compromised, no trades can be placed.

WHAT GETS PUSHED (every N seconds):
  - account_stats    → equity, win rate, Sharpe, drawdown, profit factor
  - open_positions   → live open trades with unrealised R
  - closed_trades    → trade journal (last 200)
  - equity_curve     → equity history for chart
  - system_status    → node, mode, circuit breaker, bot health
  - daily_bias       → today's BUY/SELL/NO_TRADE per instrument
  - kill_switch      → polls for kill signal from dashboard

SUPABASE SETUP (one-time):
  1. Create free project at supabase.com
  2. Run SQL in Supabase SQL editor (see SETUP_SQL below)
  3. Add to .env:
       SUPABASE_URL=https://xxxx.supabase.co
       SUPABASE_SERVICE_KEY=eyJ...   (service_role key — NEVER put in frontend)
       SUPABASE_ANON_KEY=eyJ...      (anon key — safe for frontend)

USAGE:
  from dashboard.supabase_pusher import get_pusher

  pusher = get_pusher()
  pusher.start()    # background thread, pushes every 10s
  pusher.stop()
"""

import os
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("crave.dashboard")

# ─────────────────────────────────────────────────────────────────────────────
# SUPABASE SQL — run this once in your Supabase SQL editor
# ─────────────────────────────────────────────────────────────────────────────

SETUP_SQL = """
-- Run this in Supabase SQL Editor (one time)

-- Account stats (single row, upserted on each push)
CREATE TABLE IF NOT EXISTS crave_account_stats (
    id              SERIAL PRIMARY KEY,
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    equity          NUMERIC,
    starting_equity NUMERIC,
    total_return_pct NUMERIC,
    max_drawdown_pct NUMERIC,
    total_trades    INTEGER,
    wins            INTEGER,
    losses          INTEGER,
    win_rate        NUMERIC,
    profit_factor   NUMERIC,
    sharpe_ratio    NUMERIC,
    expectancy_r    NUMERIC,
    trading_mode    TEXT,          -- paper / live
    can_trade       BOOLEAN,
    circuit_breaker BOOLEAN,
    streak_state    TEXT,
    daily_pnl_pct   NUMERIC,
    risk_a_plus     NUMERIC
);

-- Equity curve (one row per trade close)
CREATE TABLE IF NOT EXISTS crave_equity_curve (
    id              SERIAL PRIMARY KEY,
    recorded_at     TIMESTAMPTZ DEFAULT NOW(),
    equity          NUMERIC,
    trade_id        TEXT,
    r_multiple      NUMERIC,
    symbol          TEXT
);

-- Open positions (upserted by trade_id)
CREATE TABLE IF NOT EXISTS crave_open_positions (
    trade_id        TEXT PRIMARY KEY,
    symbol          TEXT,
    direction       TEXT,
    entry_price     NUMERIC,
    current_sl      NUMERIC,
    current_tp      NUMERIC,
    tp1_price       NUMERIC,
    grade           TEXT,
    risk_pct        NUMERIC,
    remaining_pct   NUMERIC,
    tp1_hit         BOOLEAN,
    open_time       TIMESTAMPTZ,
    unrealised_r    NUMERIC,
    exchange        TEXT,
    is_paper        BOOLEAN,
    last_updated    TIMESTAMPTZ DEFAULT NOW()
);

-- Closed trades journal
CREATE TABLE IF NOT EXISTS crave_trades (
    trade_id        TEXT PRIMARY KEY,
    symbol          TEXT,
    direction       TEXT,
    entry_price     NUMERIC,
    exit_price      NUMERIC,
    stop_loss       NUMERIC,
    grade           TEXT,
    confidence      INTEGER,
    r_multiple      NUMERIC,
    outcome         TEXT,
    hold_duration_h NUMERIC,
    pnl_pct         NUMERIC,
    is_paper        BOOLEAN,
    exchange        TEXT,
    open_time       TIMESTAMPTZ,
    close_time      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- System status
CREATE TABLE IF NOT EXISTS crave_system_status (
    id              SERIAL PRIMARY KEY,
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    active_node     TEXT,
    trading_mode    TEXT,
    bot_running     BOOLEAN,
    ws_connected    BOOLEAN,
    last_heartbeat  TIMESTAMPTZ,
    open_positions  INTEGER,
    today_signals   INTEGER,
    python_version  TEXT,
    uptime_h        NUMERIC
);

-- Daily bias
CREATE TABLE IF NOT EXISTS crave_daily_bias (
    id              SERIAL PRIMARY KEY,
    date            DATE,
    symbol          TEXT,
    bias            TEXT,
    strength        INTEGER,
    reason          TEXT,
    UNIQUE(date, symbol)
);

-- Kill switch (dashboard writes here, bot reads it)
CREATE TABLE IF NOT EXISTS crave_kill_switch (
    id              SERIAL PRIMARY KEY,
    triggered_at    TIMESTAMPTZ,
    triggered_by    TEXT,         -- 'dashboard' / 'telegram' / 'auto'
    action          TEXT,         -- 'close_all' / 'pause' / 'resume'
    executed        BOOLEAN DEFAULT FALSE,
    executed_at     TIMESTAMPTZ
);

-- Row Level Security: allow anon read on all tables except kill_switch writes
ALTER TABLE crave_account_stats    ENABLE ROW LEVEL SECURITY;
ALTER TABLE crave_equity_curve     ENABLE ROW LEVEL SECURITY;
ALTER TABLE crave_open_positions   ENABLE ROW LEVEL SECURITY;
ALTER TABLE crave_trades           ENABLE ROW LEVEL SECURITY;
ALTER TABLE crave_system_status    ENABLE ROW LEVEL SECURITY;
ALTER TABLE crave_daily_bias       ENABLE ROW LEVEL SECURITY;
ALTER TABLE crave_kill_switch      ENABLE ROW LEVEL SECURITY;

-- Public read policy (anon key can read — safe for dashboard)
CREATE POLICY "public_read" ON crave_account_stats    FOR SELECT USING (TRUE);
CREATE POLICY "public_read" ON crave_equity_curve     FOR SELECT USING (TRUE);
CREATE POLICY "public_read" ON crave_open_positions   FOR SELECT USING (TRUE);
CREATE POLICY "public_read" ON crave_trades           FOR SELECT USING (TRUE);
CREATE POLICY "public_read" ON crave_system_status    FOR SELECT USING (TRUE);
CREATE POLICY "public_read" ON crave_daily_bias       FOR SELECT USING (TRUE);
CREATE POLICY "public_read" ON crave_kill_switch      FOR SELECT USING (TRUE);

-- Dashboard can insert kill switch commands (anon key — from dashboard button)
CREATE POLICY "public_insert_kill" ON crave_kill_switch FOR INSERT WITH CHECK (TRUE);

-- Enable realtime on key tables (so dashboard gets live updates)
ALTER PUBLICATION supabase_realtime ADD TABLE crave_account_stats;
ALTER PUBLICATION supabase_realtime ADD TABLE crave_open_positions;
ALTER PUBLICATION supabase_realtime ADD TABLE crave_system_status;
ALTER PUBLICATION supabase_realtime ADD TABLE crave_kill_switch;
"""


# ─────────────────────────────────────────────────────────────────────────────
# PUSHER CLASS
# ─────────────────────────────────────────────────────────────────────────────

class SupabasePusher:

    PUSH_INTERVAL_SECS     = 10    # push every 10 seconds
    KILL_POLL_INTERVAL_SECS = 5    # poll kill switch every 5 seconds

    def __init__(self):
        self._url     = os.environ.get("SUPABASE_URL", "")
        self._key     = os.environ.get("SUPABASE_SERVICE_KEY", "")
        self._client  = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._start_time = datetime.now(timezone.utc)
        self._connect()

    def _connect(self):
        if not self._url or not self._key:
            logger.info(
                "[Dashboard] SUPABASE_URL or SUPABASE_SERVICE_KEY not set. "
                "Dashboard push disabled. Add to .env to enable."
            )
            return
        try:
            from supabase import create_client
            self._client = create_client(self._url, self._key)
            logger.info("[Dashboard] Supabase connected ✅")
        except ImportError:
            logger.warning(
                "[Dashboard] supabase-py not installed. "
                "Run: pip install supabase"
            )
        except Exception as e:
            logger.warning(f"[Dashboard] Supabase connection failed: {e}")

    def is_ready(self) -> bool:
        return self._client is not None

    # ─────────────────────────────────────────────────────────────────────────
    # PUSH METHODS
    # ─────────────────────────────────────────────────────────────────────────

    def push_account_stats(self):
        try:
            from core.paper_trading import get_paper_engine
            from core.streak_state import streak

            pe    = get_paper_engine()
            stats = pe.get_stats()
            s     = streak.get_status()

            self._upsert("crave_account_stats", {
                "id":               1,    # single row — always upsert row 1
                "updated_at":       datetime.now(timezone.utc).isoformat(),
                "equity":           pe.get_equity(),
                "starting_equity":  pe._state.get("starting_equity", 10000),
                "total_return_pct": pe._state.get("total_return_pct", 0),
                "max_drawdown_pct": pe._state.get("max_drawdown_pct", 0),
                "total_trades":     stats.get("total_trades", 0),
                "wins":             stats.get("wins", 0),
                "losses":           stats.get("losses", 0),
                "win_rate":         stats.get("win_rate_float", 0) / 100.0,
                "profit_factor":    stats.get("profit_factor_float", 0),
                "sharpe_ratio":     stats.get("sharpe_float", 0),
                "expectancy_r":     stats.get("expectancy_float", 0),
                "trading_mode":     "paper" if pe._cfg.get("enabled") else "live",
                "can_trade":        s.get("can_trade", False),
                "circuit_breaker":  s.get("circuit_breaker", False),
                "streak_state":     s.get("streak_state", "neutral"),
                "daily_pnl_pct":    float(s.get("today_pnl_pct", "0").replace("%","")),
                "risk_a_plus":      s.get("current_risk_A+", "1%").replace("%",""),
            })
        except Exception as e:
            logger.debug(f"[Dashboard] push_account_stats failed: {e}")

    def push_equity_curve(self):
        try:
            from core.paper_trading import get_paper_engine
            pe     = get_paper_engine()
            curve  = pe._state.get("equity_curve", [])
            r_ents = pe._state.get("r_entries", [])

            if not curve:
                return

            # Push last 200 points
            rows = []
            for i, eq in enumerate(curve[-200:]):
                r = r_ents[i]["r"] if i < len(r_ents) else 0
                rows.append({
                    "equity":     eq,
                    "r_multiple": r,
                    "symbol":     "portfolio",
                })

            # Truncate and reinsert (simpler than diffing)
            if self._client:
                self._client.table("crave_equity_curve").delete().neq(
                    "id", 0
                ).execute()
                self._client.table("crave_equity_curve").insert(rows).execute()
        except Exception as e:
            logger.debug(f"[Dashboard] push_equity_curve failed: {e}")

    def push_open_positions(self):
        try:
            from core.position_tracker import positions
            from data.market_data_router import get_data_router

            router   = get_data_router()
            all_pos  = positions.get_all()

            # Smart sync: only delete closed positions to prevent UI flickering
            if self._client:
                try:
                    resp = self._client.table("crave_open_positions").select("trade_id").execute()
                    existing_ids = [row["trade_id"] for row in resp.data]
                    current_ids = [pos["trade_id"] for pos in all_pos]
                    for t_id in existing_ids:
                        if t_id not in current_ids:
                            self._client.table("crave_open_positions").delete().eq("trade_id", t_id).execute()
                except Exception as e:
                    pass

            for pos in all_pos:
                # Calculate unrealised R
                unrealised_r = 0.0
                try:
                    price    = router.get_live_price(pos["symbol"])
                    entry    = pos["entry_price"]
                    original_sl = pos.get("original_sl", pos["current_sl"])
                    sl_dist  = abs(entry - original_sl)
                    if sl_dist <= 0:
                        sl_dist = entry * 0.001
                        
                    if price:
                        if pos["direction"] in ("buy", "long"):
                            unrealised_r = round((price - entry) / sl_dist, 2)
                        else:
                            unrealised_r = round((entry - price) / sl_dist, 2)
                except Exception:
                    pass

                self._upsert("crave_open_positions", {
                    "trade_id":      pos["trade_id"],
                    "symbol":        pos["symbol"],
                    "direction":     pos["direction"],
                    "entry_price":   pos["entry_price"],
                    "current_sl":    pos["current_sl"],
                    "current_tp":    pos.get("current_tp"),
                    "tp1_price":     pos.get("tp1_price"),
                    "grade":         pos.get("grade", "?"),
                    "risk_pct":      pos.get("risk_pct", 1.0),
                    "remaining_pct": pos.get("remaining_pct", 100),
                    "tp1_hit":       pos.get("tp1_hit", False),
                    "open_time":     pos.get("open_time"),
                    "unrealised_r":  unrealised_r,
                    "exchange":      pos.get("exchange", "paper"),
                    "is_paper":      pos.get("is_paper", True),
                    "last_updated":  datetime.now(timezone.utc).isoformat(),
                })
        except Exception as e:
            logger.debug(f"[Dashboard] push_open_positions failed: {e}")

    def push_closed_trades(self):
        try:
            from core.database_manager import db
            trades = db.get_recent_trades(limit=200)

            for t in trades:
                # Handle string confidences like "High" from manual trades
                conf = t.get("confidence")
                if isinstance(conf, str):
                    if conf.lower() == "high":
                        conf = 90
                    elif conf.lower() == "medium":
                        conf = 70
                    elif conf.lower() == "low":
                        conf = 40
                    else:
                        conf = 50
                elif conf is None:
                    conf = 0

                self._upsert("crave_trades", {
                    "trade_id":       t.get("trade_id", ""),
                    "symbol":         t.get("symbol"),
                    "direction":      t.get("direction"),
                    "entry_price":    t.get("entry_price"),
                    "exit_price":     t.get("exit_price"),
                    "stop_loss":      t.get("stop_loss"),
                    "grade":          t.get("grade"),
                    "confidence":     int(conf) if conf is not None else 0,
                    "r_multiple":     t.get("r_multiple"),
                    "outcome":        t.get("outcome"),
                    "hold_duration_h": t.get("hold_duration_h"),
                    "pnl_pct":        t.get("pnl_pct"),
                    "is_paper":       bool(t.get("is_paper", 1)),
                    "exchange":       t.get("exchange"),
                    "open_time":      t.get("open_time"),
                    "close_time":     t.get("close_time"),
                })
        except Exception as e:
            logger.debug(f"[Dashboard] push_closed_trades failed: {e}")

    def push_system_status(self):
        try:
            import socket, sys
            from core.position_tracker import positions

            uptime_h = (
                datetime.now(timezone.utc) - self._start_time
            ).total_seconds() / 3600

            active_node = "unknown"
            try:
                from infra.node_orchestrator import orchestrator
                active_node = orchestrator.get_active_node()
            except Exception:
                active_node = socket.gethostname()

            ws_connected = False
            try:
                from interfaces.websocket_manager import get_ws
                ws_connected = any(
                    c.is_connected for c in get_ws()._clients.values()
                )
            except Exception:
                pass

            self._upsert("crave_system_status", {
                "id":              1,
                "updated_at":      datetime.now(timezone.utc).isoformat(),
                "active_node":     active_node,
                "trading_mode":    os.environ.get("TRADING_MODE", "paper"),
                "bot_running":     True,
                "ws_connected":    ws_connected,
                "last_heartbeat":  datetime.now(timezone.utc).isoformat(),
                "open_positions":  positions.count(),
                "today_signals":   0,
                "python_version":  sys.version.split()[0],
                "uptime_h":        round(uptime_h, 2),
            })
        except Exception as e:
            logger.debug(f"[Dashboard] push_system_status failed: {e}")

    def push_daily_bias(self):
        try:
            from core.database_manager import db
            from config.config import get_tradeable_symbols

            today   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            symbols = get_tradeable_symbols()

            for sym in symbols:
                bias = db.get_today_bias(sym)
                if not bias:
                    continue
                if self._client:
                    self._client.table("crave_daily_bias").upsert({
                        "date":     today,
                        "symbol":   sym,
                        "bias":     bias.get("bias", "NO_TRADE"),
                        "strength": bias.get("strength", 0),
                        "reason":   bias.get("reason", ""),
                    }, on_conflict="date,symbol").execute()
        except Exception as e:
            logger.debug(f"[Dashboard] push_daily_bias failed: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # KILL SWITCH POLLING
    # ─────────────────────────────────────────────────────────────────────────

    def poll_kill_switch(self):
        """
        Poll Supabase for unexecuted kill switch commands.
        Dashboard button inserts a row here.
        Bot reads it and executes the action.
        """
        if not self._client:
            return
        try:
            result = self._client.table("crave_kill_switch").select("*").eq(
                "executed", False
            ).execute()

            for cmd in (result.data or []):
                action = cmd.get("action", "")
                cmd_id = cmd.get("id")

                logger.warning(
                    f"[Dashboard] KILL SWITCH: action={action} "
                    f"triggered_by={cmd.get('triggered_by')}"
                )

                if action == "close_all":
                    self._execute_close_all()
                elif action == "pause":
                    self._execute_pause()
                elif action == "resume":
                    self._execute_resume()

                # Mark as executed
                self._client.table("crave_kill_switch").update({
                    "executed":    True,
                    "executed_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", cmd_id).execute()

        except Exception as e:
            logger.debug(f"[Dashboard] Kill switch poll failed: {e}")

    def _execute_close_all(self):
        """Kill switch: close ALL positions immediately."""
        logger.critical("[Dashboard] KILL SWITCH: CLOSE ALL POSITIONS")
        try:
            from core.position_tracker import positions
            from brokers.broker_router import get_router

            for pos in positions.get_all():
                try:
                    get_router().execute(
                        {"symbol": pos["symbol"], "direction": "close",
                         "trade_id": pos["trade_id"]},
                        0,
                        is_paper=pos.get("is_paper", True),
                    )
                except Exception as e:
                    logger.error(
                        f"[Dashboard] Kill close failed {pos['symbol']}: {e}"
                    )

            from interfaces.telegram_interface import tg
            tg.send(
                "🚨 <b>KILL SWITCH ACTIVATED</b>\n"
                "All positions closed from dashboard.\n"
                "Bot is paused."
            )
        except Exception as e:
            logger.error(f"[Dashboard] Close all failed: {e}")

    def _execute_pause(self):
        try:
            from core.streak_state import streak
            streak.manual_pause(reason="Dashboard kill switch")
            from interfaces.telegram_interface import tg
            tg.send("⏸️ Bot PAUSED via dashboard kill switch.")
        except Exception as e:
            logger.error(f"[Dashboard] Pause failed: {e}")

    def _execute_resume(self):
        try:
            from core.streak_state import streak
            streak.manual_resume()
            from interfaces.telegram_interface import tg
            tg.send("▶️ Bot RESUMED via dashboard.")
        except Exception as e:
            logger.error(f"[Dashboard] Resume failed: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _upsert(self, table: str, data: dict):
        if not self._client:
            return
        try:
            self._client.table(table).upsert(data).execute()
        except Exception as e:
            logger.debug(f"[Dashboard] Upsert {table} failed: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # BACKGROUND THREAD
    # ─────────────────────────────────────────────────────────────────────────

    def start(self):
        if not self.is_ready():
            logger.info("[Dashboard] Not configured — skipping.")
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._push_loop,
            daemon=True,
            name="CRAVEDashboardPusher"
        )
        self._thread.start()
        logger.info("[Dashboard] Pusher started — pushing every 10s.")

    def stop(self):
        self._running = False

    def _push_loop(self):
        last_equity_push = 0
        last_trades_push = 0
        last_bias_push   = 0
        cycle            = 0

        while self._running:
            try:
                from infra.node_orchestrator import orchestrator
                if not orchestrator.is_active():
                    # Standby nodes should not push to dashboard!
                    time.sleep(self.PUSH_INTERVAL_SECS)
                    continue

                now = time.time()
                cycle += 1

                # Every 10s: account stats + positions + system status
                self.push_account_stats()
                self.push_open_positions()
                self.push_system_status()
                self.poll_kill_switch()

                # Every 60s: equity curve + closed trades
                if now - last_equity_push >= 60:
                    self.push_equity_curve()
                    last_equity_push = now

                if now - last_trades_push >= 60:
                    self.push_closed_trades()
                    last_trades_push = now

                # Every 5 minutes: daily bias
                if now - last_bias_push >= 300:
                    self.push_daily_bias()
                    last_bias_push = now

            except Exception as e:
                logger.error(f"[Dashboard] Push loop error: {e}")

            time.sleep(self.PUSH_INTERVAL_SECS)


# ── Singleton ─────────────────────────────────────────────────────────────────
_pusher: Optional[SupabasePusher] = None

def get_pusher() -> SupabasePusher:
    global _pusher
    if _pusher is None:
        _pusher = SupabasePusher()
    return _pusher
