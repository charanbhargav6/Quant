"""
CRAVE v10.4 — Supabase Pusher
==============================
Server-side writer that syncs engine state → Supabase.
Uses the SERVICE ROLE KEY — never exposed to the browser.

WHAT IT PUSHES
──────────────
  Realtime (every 30 s):  crave_system_status, crave_nodes
  On change (every 5 s):  crave_open_positions (diff-detected)
  On trade close:         crave_trades
  On session close:       crave_daily_pnl
  Periodic (60 s):        crave_paper_equity, crave_portfolio_heat

SECURITY
────────
  Uses SUPABASE_SERVICE_KEY from .env — bypasses RLS for writes.
  Browser only receives SUPABASE_URL + SUPABASE_KEY (anon) via
  GET /api/supabase/config — service key is never sent to browser.

USAGE
─────
  from dashboard.supabase_pusher import get_pusher

  pusher = get_pusher()
  pusher.start()              # background thread
  pusher.push_trade(trade)    # call on trade close
  pusher.push_alert(alert)    # call on alert change
  pusher.stop()
"""

import os
import json
import logging
import threading
import time
import psutil
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("crave.supabase_pusher")


class SupabasePusher:
    """
    Manages all Supabase writes from the engine.
    Thread-safe. Non-blocking (uses background thread).
    All writes are fire-and-forget with error logging.
    """

    HEARTBEAT_INTERVAL_S   = 30   # crave_system_status + crave_nodes
    POSITION_INTERVAL_S    = 5    # crave_open_positions (diff-detected)
    PAPER_EQUITY_INTERVAL_S = 60  # crave_paper_equity
    HEAT_INTERVAL_S        = 60   # crave_portfolio_heat

    def __init__(self):
        self._sb          = None        # supabase client
        self._running     = False
        self._thread: Optional[threading.Thread] = None
        self._pos_snapshot: dict = {}   # trade_id → profit snapshot for diff
        self._start_time  = time.time()

    # ─────────────────────────────────────────────────────────────────────────
    # CLIENT
    # ─────────────────────────────────────────────────────────────────────────

    def _get_client(self):
        """Lazy-init Supabase client using SERVICE role key."""
        if self._sb:
            return self._sb
        try:
            from supabase import create_client
            url = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL", "")
            # Service key first — bypasses RLS for writes
            key = (
                os.environ.get("SUPABASE_SERVICE_KEY")
                or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
                or os.environ.get("SUPABASE_KEY", "")
            )
            if not url or not key or "YOUR_PROJECT" in url:
                return None
            self._sb = create_client(url, key)
            logger.info("[Pusher] Supabase client initialised")
            return self._sb
        except ImportError:
            logger.warning("[Pusher] supabase-py not installed — run: pip install supabase")
        except Exception as e:
            logger.warning(f"[Pusher] Client init failed: {e}")
        return None

    def _upsert(self, table: str, row: dict, on_conflict: str = "id"):
        """Upsert a single row. Non-fatal on error."""
        sb = self._get_client()
        if not sb:
            return
        try:
            sb.table(table).upsert(row, on_conflict=on_conflict).execute()
        except Exception as e:
            logger.debug(f"[Pusher] upsert {table} failed: {e}")

    def _insert(self, table: str, row: dict):
        """Insert a single row. Non-fatal on error."""
        sb = self._get_client()
        if not sb:
            return
        try:
            sb.table(table).insert(row).execute()
        except Exception as e:
            logger.debug(f"[Pusher] insert {table} failed: {e}")

    def _delete_where(self, table: str, column: str, values: list):
        """Delete rows no longer needed (e.g., closed positions)."""
        sb = self._get_client()
        if not sb or not values:
            return
        try:
            sb.table(table).delete().in_(column, values).execute()
        except Exception as e:
            logger.debug(f"[Pusher] delete {table} failed: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # SYSTEM STATUS / HEARTBEAT
    # ─────────────────────────────────────────────────────────────────────────

    def _push_system_status(self):
        """Push bot health row — one row per node, upserted on node_name."""
        try:
            from quant_server import check_conn, get_broker_agent
            conn   = check_conn()
            mt5    = get_broker_agent()
            info   = mt5.get_account_info() if (mt5 and conn.get("mt5")) else {}
            equity = info.get("equity", 0.0) if info else 0.0
            profit = info.get("profit",  0.0) if info else 0.0
        except Exception:
            conn = {}; equity = 0.0; profit = 0.0

        try:
            from core.streak_state import streak
            st  = streak.get_status()
            cb  = bool(st.get("circuit_breaker_active", False))
            dd  = float(str(st.get("today_pnl_pct", 0)).replace("%", ""))
        except Exception:
            cb = False; dd = 0.0

        try:
            pos_count = len(get_broker_agent().get_positions()) if get_broker_agent() else 0
        except Exception:
            pos_count = 0

        uptime = int(time.time() - self._start_time)

        row = {
            "node_name":        os.environ.get("NODE_NAME", "laptop"),
            "active_node":      os.environ.get("NODE_NAME", "laptop"),
            "bot_running":      True,
            "engine_mode":      os.environ.get("TRADING_MODE", "paper").upper(),
            "engine_version":   "v10.4",
            "trading_mode":     os.environ.get("TRADING_MODE", "paper"),
            "mt5_connected":    bool(conn.get("mt5", False)),
            "binance_connected":bool(conn.get("binance", False)),
            "zerodha_connected":bool(conn.get("zerodha", False)),
            "open_positions":   pos_count,
            "today_pnl":        round(profit, 2),
            "today_pnl_pct":    round(dd, 4),
            "total_equity":     round(equity, 2),
            "circuit_breaker":  cb,
            "cpu_pct":          round(psutil.cpu_percent(interval=None), 2),
            "ram_pct":          round(psutil.virtual_memory().percent, 2),
            "uptime_seconds":   uptime,
            "last_heartbeat":   datetime.now(timezone.utc).isoformat(),
            "updated_at":       datetime.now(timezone.utc).isoformat(),
        }
        self._upsert("crave_system_status", row, on_conflict="node_name")

    # ─────────────────────────────────────────────────────────────────────────
    # NODE HEALTH
    # ─────────────────────────────────────────────────────────────────────────

    def _push_node_health(self):
        """Push this node's health metrics."""
        try:
            disk = psutil.disk_usage("/").percent
        except Exception:
            disk = 0.0
        row = {
            "node_name":       os.environ.get("NODE_NAME", "laptop"),
            "status":          "active",
            "cpu_pct":         round(psutil.cpu_percent(interval=None), 2),
            "ram_pct":         round(psutil.virtual_memory().percent, 2),
            "disk_pct":        round(disk, 2),
            "uptime_seconds":  int(time.time() - self._start_time),
            "last_heartbeat":  datetime.now(timezone.utc).isoformat(),
        }
        self._upsert("crave_nodes", row, on_conflict="node_name")

    # ─────────────────────────────────────────────────────────────────────────
    # OPEN POSITIONS  (diff-detected)
    # ─────────────────────────────────────────────────────────────────────────

    def _push_positions(self):
        """
        Push open positions to Supabase.
        Only upserts rows where profit changed ≥ $0.50.
        Deletes rows for positions that closed since last poll.
        """
        try:
            from quant_server import get_broker_agent, check_conn, live_price
            conn = check_conn()
            mt5  = get_broker_agent()
            if not conn.get("mt5") or not mt5:
                return

            info    = mt5.get_account_info() or {}
            balance = info.get("balance", 1.0)
            positions = mt5.get_positions()

            live_ids = set()
            for pos in positions:
                trade_id = str(pos.get("ticket") or pos.get("id", ""))
                if not trade_id:
                    continue
                live_ids.add(trade_id)

                profit = round(pos.get("profit", 0), 2)
                prev   = self._pos_snapshot.get(trade_id)
                if prev is not None and abs(profit - prev) < 0.50:
                    continue  # no meaningful change — skip

                self._pos_snapshot[trade_id] = profit

                sym = pos.get("symbol", "")
                price = live_price(mt5, sym) if sym else 0.0
                entry = pos.get("entry_price", 0)
                sym_up = sym.upper()
                if "JPY" in sym_up:                        mult = 100
                elif "XAU" in sym_up:                     mult = 10
                elif any(c in sym_up for c in ("BTC","ETH","SOL")): mult = 1
                else:                                     mult = 10000
                direction = pos.get("direction", "buy")
                pips = round((price - entry) * mult, 1) if direction == "buy" \
                       else round((entry - price) * mult, 1)

                row = {
                    "trade_id":     trade_id,
                    "ticket":       pos.get("ticket"),
                    "account_id":   "mt5",
                    "symbol":       sym,
                    "direction":    direction,
                    "volume":       pos.get("volume", 0),
                    "entry_price":  entry,
                    "current_price":round(price, 6),
                    "stop_loss":    pos.get("current_sl") or pos.get("stop_loss") or 0,
                    "take_profit":  pos.get("current_tp") or pos.get("take_profit") or 0,
                    "tp1_price":    pos.get("tp1_price"),
                    "tp1_hit":      bool(pos.get("tp1_hit", False)),
                    "remaining_pct":float(pos.get("remaining_pct", 100)),
                    "profit":       profit,
                    "profit_pct":   round(profit / max(balance, 1) * 100, 4),
                    "pips_moved":   pips,
                    "swap":         pos.get("swap", 0),
                    "strategy_id":  pos.get("strategy_id") or pos.get("comment", ""),
                    "grade":        pos.get("grade"),
                    "confidence":   pos.get("confidence"),
                    "exchange":     "mt5",
                    "is_paper":     bool(pos.get("is_paper", False)),
                    "node":         os.environ.get("NODE_NAME", "laptop"),
                    "open_time":    pos.get("open_time", datetime.now(timezone.utc).isoformat()),
                    "atr_at_open":  pos.get("atr_at_open"),
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                }
                self._upsert("crave_open_positions", row, on_conflict="trade_id")

            # Clean up rows for positions that closed
            closed_ids = set(self._pos_snapshot.keys()) - live_ids
            if closed_ids:
                self._delete_where("crave_open_positions", "trade_id", list(closed_ids))
                for cid in closed_ids:
                    del self._pos_snapshot[cid]

        except Exception as e:
            logger.debug(f"[Pusher] _push_positions: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # PAPER EQUITY CURVE
    # ─────────────────────────────────────────────────────────────────────────

    def _push_paper_equity(self):
        """Snapshot paper engine equity → crave_paper_equity."""
        try:
            from core.paper_trading import get_paper_engine
            pe  = get_paper_engine()
            eq  = pe.get_equity()
            if eq <= 0:
                return
            bal     = float(os.environ.get("ACCOUNT_SIZE", "10000"))
            pos_cnt = len(pe.get_positions()) if hasattr(pe, "get_positions") else 0
            fl_pnl  = round(eq - bal, 2)
            dd_pct  = round((bal - eq) / bal * 100, 4) if eq < bal else 0.0

            row = {
                "ts":             datetime.now(timezone.utc).isoformat(),
                "equity":         round(eq, 2),
                "balance":        bal,
                "open_positions": pos_cnt,
                "floating_pnl":   fl_pnl,
                "drawdown_pct":   dd_pct,
                "trades_today":   0,  # filled by DB query if needed
            }
            self._insert("crave_paper_equity", row)
        except Exception as e:
            logger.debug(f"[Pusher] _push_paper_equity: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # PORTFOLIO HEAT
    # ─────────────────────────────────────────────────────────────────────────

    def _push_portfolio_heat(self):
        """Snapshot exposure heatmap → crave_portfolio_heat."""
        try:
            from quant_server import get_broker_agent, check_conn
            conn = check_conn()
            mt5  = get_broker_agent()
            if not conn.get("mt5") or not mt5:
                return
            info    = mt5.get_account_info() or {}
            equity  = info.get("equity", 0.0)
            positions = mt5.get_positions()

            exposure_map: dict = {}
            for pos in positions:
                sym = pos.get("symbol", "")
                vol = pos.get("volume", 0)
                exposure_map[sym] = exposure_map.get(sym, 0) + vol

            total_vol = sum(exposure_map.values())
            items = [
                {
                    "symbol": k,
                    "direction": "mixed",
                    "volume": round(v, 4),
                    "pct_of_equity": round(v / total_vol * 100, 2) if total_vol else 0,
                    "floating_pnl": 0,
                }
                for k, v in exposure_map.items()
            ] if exposure_map else [{"symbol": "No Open Risk", "pct_of_equity": 100}]

            row = {
                "account_id":   "mt5",
                "snapshot_ts":  datetime.now(timezone.utc).isoformat(),
                "total_equity": round(equity, 2),
                "items":        json.dumps(items),
                "corr_groups":  "[]",
            }
            self._insert("crave_portfolio_heat", row)
        except Exception as e:
            logger.debug(f"[Pusher] _push_portfolio_heat: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # PUBLIC: PUSH TRADE ON CLOSE
    # Call this from the trade close path (does not touch execution logic)
    # ─────────────────────────────────────────────────────────────────────────

    def push_trade(self, trade: dict):
        """
        Push a closed trade to crave_trades.
        Call after recording to SQLite — this is an async mirror, not the source.
        """
        try:
            row = {
                "trade_id":      str(trade.get("trade_id") or trade.get("ticket", "")),
                "ticket":        trade.get("ticket"),
                "account_id":    str(trade.get("account_id", "mt5")),
                "symbol":        trade.get("symbol", ""),
                "direction":     trade.get("direction", ""),
                "volume":        float(trade.get("volume") or trade.get("lot_size") or 0),
                "entry_price":   trade.get("entry_price"),
                "exit_price":    trade.get("exit_price"),
                "stop_loss":     trade.get("stop_loss"),
                "tp1_price":     trade.get("tp1_price"),
                "tp2_price":     trade.get("tp2_price"),
                "profit_dollars":trade.get("profit_dollars") or trade.get("profit"),
                "profit_pct":    trade.get("profit_pct") or trade.get("pnl_pct"),
                "r_multiple":    trade.get("r_multiple"),
                "outcome":       trade.get("outcome"),
                "grade":         trade.get("grade"),
                "confidence":    trade.get("confidence"),
                "strategy_id":   trade.get("strategy_id") or trade.get("node"),
                "node":          trade.get("node") or os.environ.get("NODE_NAME", "laptop"),
                "exchange":      trade.get("exchange", "mt5"),
                "is_paper":      bool(trade.get("is_paper", False)),
                "open_time":     trade.get("open_time", ""),
                "close_time":    trade.get("close_time", datetime.now(timezone.utc).isoformat()),
                "hold_duration_h": trade.get("hold_duration_h"),
                "notes":         json.dumps(trade.get("notes", {})) if trade.get("notes") else None,
                "created_at":    datetime.now(timezone.utc).isoformat(),
            }
            # Run in thread so it doesn't block the trading loop
            threading.Thread(
                target=self._upsert,
                args=("crave_trades", row, "trade_id"),
                daemon=True
            ).start()
        except Exception as e:
            logger.debug(f"[Pusher] push_trade failed: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # PUBLIC: PUSH ALERT
    # ─────────────────────────────────────────────────────────────────────────

    def push_alert(self, alert: dict):
        """Push or update a risk/news alert."""
        try:
            row = {
                "alert_id":       alert.get("alert_id", f"alrt_{int(time.time())}"),
                "level":          alert.get("level", "info"),
                "category":       alert.get("category", "system"),
                "text":           alert.get("text", ""),
                "symbol":         alert.get("symbol"),
                "account_id":     alert.get("account_id"),
                "action_required":bool(alert.get("action_required", False)),
                "auto_action":    alert.get("auto_action"),
                "is_active":      bool(alert.get("is_active", True)),
                "ts":             alert.get("ts", datetime.now(timezone.utc).isoformat()),
                "expires_at":     alert.get("expires_at"),
            }
            threading.Thread(
                target=self._upsert,
                args=("crave_alerts", row, "alert_id"),
                daemon=True
            ).start()
        except Exception as e:
            logger.debug(f"[Pusher] push_alert failed: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # PUBLIC: PUSH DAILY P&L  (call at session close)
    # ─────────────────────────────────────────────────────────────────────────

    def push_daily_pnl(self, account_id: str = "mt5"):
        """Snapshot today's session P&L into crave_daily_pnl."""
        try:
            from quant_server import check_conn, get_broker_agent, get_db_agent
            conn = check_conn()
            mt5  = get_broker_agent()
            db   = get_db_agent()

            info    = mt5.get_account_info() if (mt5 and conn.get("mt5")) else {}
            equity  = info.get("equity", 0.0) if info else 0.0
            balance = info.get("balance", 0.0) if info else 0.0

            today_real = 0.0; wins = 0; losses = 0
            if db:
                rows = db.query(
                    "SELECT pnl_pct, r_multiple FROM trades "
                    "WHERE date(close_time)=date('now') AND is_paper=0", ()
                )
                for r in rows:
                    pnl = (r.get("pnl_pct") or 0) * balance / 100
                    today_real += pnl
                    rm = r.get("r_multiple") or 0
                    if rm > 0: wins += 1
                    else: losses += 1

            from core.streak_state import streak
            st  = streak.get_status()
            cb  = bool(st.get("circuit_breaker_active", False))
            cld = int(st.get("consecutive_loss_days", 0))
            dd  = float(str(st.get("today_pnl_pct", 0)).replace("%", ""))

            from datetime import date as date_cls
            session_date = date_cls.today().isoformat()

            row = {
                "account_id":             account_id,
                "session_date":           session_date,
                "close_equity":           round(equity, 2),
                "pnl_dollars":            round(today_real, 2),
                "pnl_pct":                round(dd, 4),
                "realized_pnl":           round(today_real, 2),
                "unrealized_pnl":         round(info.get("profit", 0), 2) if info else 0,
                "trades_count":           wins + losses,
                "wins":                   wins,
                "losses":                 losses,
                "max_drawdown_pct":       round(min(dd, 0), 4),
                "circuit_breaker_fired":  cb,
                "consecutive_loss_days":  cld,
                "created_at":             datetime.now(timezone.utc).isoformat(),
            }
            self._upsert("crave_daily_pnl", row, on_conflict="account_id, session_date")
        except Exception as e:
            logger.debug(f"[Pusher] push_daily_pnl failed: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # BACKGROUND LOOP
    # ─────────────────────────────────────────────────────────────────────────

    def _push_all(self):
        """Force-push all data. Called on graceful shutdown / sleep."""
        self._push_system_status()
        self._push_node_health()
        self._push_positions()
        self._push_paper_equity()
        self._push_portfolio_heat()

    def _loop(self):
        """Main background thread — interval-based push."""
        hb_last   = 0
        pos_last  = 0
        paper_last = 0
        heat_last  = 0

        time.sleep(5)  # brief startup delay

        while self._running:
            now = time.time()

            if now - hb_last >= self.HEARTBEAT_INTERVAL_S:
                self._push_system_status()
                self._push_node_health()
                hb_last = now

            if now - pos_last >= self.POSITION_INTERVAL_S:
                self._push_positions()
                pos_last = now

            if now - paper_last >= self.PAPER_EQUITY_INTERVAL_S:
                self._push_paper_equity()
                paper_last = now

            if now - heat_last >= self.HEAT_INTERVAL_S:
                self._push_portfolio_heat()
                heat_last = now

            time.sleep(1)

    def start(self):
        """Start background push thread."""
        if self._running:
            return
        if not self._get_client():
            logger.warning("[Pusher] Supabase not configured — pusher disabled. "
                           "Set SUPABASE_URL + SUPABASE_SERVICE_KEY in .env")
            return
        self._start_time = time.time()
        self._running    = True
        self._thread     = threading.Thread(
            target=self._loop,
            daemon=True,
            name="supabase-pusher"
        )
        self._thread.start()
        logger.info("[Pusher] Started — pushing to Supabase every 30s")

    def stop(self):
        """Stop background thread and do one final push."""
        self._running = False
        self._push_all()
        logger.info("[Pusher] Stopped. Final state pushed.")


# ─────────────────────────────────────────────────────────────────────────────
# SINGLETON
# ─────────────────────────────────────────────────────────────────────────────

_pusher_instance: Optional[SupabasePusher] = None
_pusher_lock = threading.Lock()

def get_pusher() -> SupabasePusher:
    global _pusher_instance
    with _pusher_lock:
        if _pusher_instance is None:
            _pusher_instance = SupabasePusher()
    return _pusher_instance
