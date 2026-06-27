"""
CRAVE v10.2 — Position Tracker (Thread-Safe)
=============================================
UPGRADE vs v10.1:

  🔧 PRIORITY 1 — RLock on all mutating methods
     _positions dict is accessed simultaneously from three threads:
       Thread 1: trading_loop      (open, has_open_position, count)
       Thread 2: dynamic_tp_engine (update_sl, update_tp, partial_close)
       Thread 3: event_hedge_manager (apply_event_hedge, restore_after_event)

     Python's GIL protects individual bytecode operations but NOT
     compound operations like:
       pos = self._positions.get(trade_id)    ← read
       pos["current_sl"] = new_sl             ← write
       self._save()                           ← write JSON

     Between the read and write, another thread can modify self._positions.
     This is a real data corruption risk, not theoretical.

     Fix: threading.RLock (re-entrant lock — safe for methods that call
     other locking methods, e.g. partial_close calls update_sl).
     All methods that read-then-modify _positions are wrapped with self._lock.
     Pure read-only methods (get, get_all, count) use the lock for
     consistency but release immediately — negligible overhead.

All v10.1 fixes (paper equity, ML backfill, signal_id, risk_pct) retained.
No logic changes — only lock addition.
"""

import json
import uuid
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict

logger = logging.getLogger("crave.positions")


class PositionTracker:

    def __init__(self, positions_file: Optional[str] = None):
        from config.config import POSITIONS_FILE
        self.positions_file = Path(positions_file or POSITIONS_FILE)
        self._positions: Dict[str, dict] = {}

        # PRIORITY 1: RLock — re-entrant so partial_close can call update_sl
        # without deadlock. All methods that mutate _positions acquire this lock.
        self._lock = threading.RLock()

        self._load()
        logger.info(
            f"[Positions] Loaded {len(self._positions)} open position(s) "
            f"(thread-safe v10.2)."
        )
        if self._positions:
            for tid, p in self._positions.items():
                logger.info(
                    f"  → {p['symbol']} {p['direction'].upper()} "
                    f"entry={p['entry_price']} sl={p['current_sl']} "
                    f"tp={p.get('current_tp','?')} "
                    f"remaining={p.get('remaining_pct',100)}%"
                )

    # ─────────────────────────────────────────────────────────────────────────
    # PERSISTENCE (called under lock — no separate locking needed)
    # ─────────────────────────────────────────────────────────────────────────

    def _load(self):
        if self.positions_file.exists():
            try:
                with open(self.positions_file) as f:
                    self._positions = json.load(f)
            except Exception as e:
                logger.warning(f"[Positions] Load failed: {e}. Starting empty.")
                self._positions = {}
        else:
            self._positions = {}

    def _save(self):
        """Always called while holding self._lock."""
        try:
            self.positions_file.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: write to temp then rename to prevent partial writes
            tmp = self.positions_file.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(self._positions, f, indent=2)
            tmp.replace(self.positions_file)
        except Exception as e:
            logger.error(f"[Positions] Save failed: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # OPEN
    # ─────────────────────────────────────────────────────────────────────────

    def open(self, trade: dict) -> str:
        trade_id = trade.get("trade_id") or str(uuid.uuid4())[:8].upper()

        position = {
            "trade_id":             trade_id,
            "symbol":               trade["symbol"],
            "direction":            trade["direction"],
            "entry_price":          trade.get("entry") or trade.get("entry_price"),
            "lot_size":             trade["lot_size"],
            "current_sl":           trade["stop_loss"],
            "original_sl":          trade["stop_loss"],
            "tp1_price":            trade.get("take_profit_1"),
            "current_tp":           trade.get("take_profit_2") or trade.get("take_profit"),
            "original_tp2":         trade.get("take_profit_2") or trade.get("take_profit"),
            "tp1_hit":              False,
            "remaining_pct":        100.0,
            "bookings":             [],
            "tp_extensions":        [],
            "grade":                trade.get("grade", "A"),
            "confidence":           trade.get("confidence_pct", 50),
            "atr_at_open":          trade.get("atr_value") or trade.get("atr"),
            "sl_multiplier":        trade.get("sl_multiplier", 1.5),
            "exchange":             trade.get("exchange", "paper"),
            "is_paper":             trade.get("is_paper", True),
            "node":                 trade.get("node", "unknown"),
            "open_time":            datetime.now(timezone.utc).isoformat(),
            "last_updated":         datetime.now(timezone.utc).isoformat(),
            "risk_pct":             trade.get("risk_pct", 1.0),
            "signal_id":            trade.get("signal_id"),
            "event_hedged":         False,
            "event_hedge_pct":      0.0,
            "pre_event_lot_size":   trade["lot_size"],
            "hours_since_tp1":      0.0,
            "sl_compressed_count":  0,
        }

        with self._lock:
            self._positions[trade_id] = position
            self._save()

        # DB mirror outside lock — I/O doesn't need to hold the position lock
        try:
            from core.database_manager import db
            db.upsert_position(position)
        except Exception as e:
            logger.warning(f"[Positions] DB mirror failed: {e}")

        logger.info(
            f"[Positions] Opened: {position['symbol']} "
            f"{position['direction'].upper()} @ {position['entry_price']} "
            f"| SL={position['current_sl']} TP={position['current_tp']} "
            f"| Grade={position['grade']} Risk={position['risk_pct']}% "
            f"| ID={trade_id}"
        )
        return trade_id

    # ─────────────────────────────────────────────────────────────────────────
    # UPDATES
    # ─────────────────────────────────────────────────────────────────────────

    def update_sl(self, trade_id: str, new_sl: float,
                   reason: str = "") -> bool:
        with self._lock:
            pos = self._positions.get(trade_id)
            if not pos:
                logger.warning(f"[Positions] update_sl: {trade_id} not found.")
                return False

            old_sl    = pos["current_sl"]
            direction = pos["direction"]

            if direction in ("buy", "long") and new_sl <= old_sl:
                return False
            if direction in ("sell", "short") and new_sl >= old_sl:
                return False

            pos["current_sl"]   = round(new_sl, 5)
            pos["last_updated"] = datetime.now(timezone.utc).isoformat()
            self._save()

        logger.info(
            f"[Positions] SL updated: {pos['symbol']} {old_sl}→{new_sl} ({reason})"
        )

        # Sync SL to MT5 if this is an MT5 position
        if pos.get("exchange") == "mt5" and pos.get("mt5_ticket"):
            try:
                from brokers.mt5_agent import get_mt5
                mt5 = get_mt5()
                mt5.modify_sl(pos["mt5_ticket"], new_sl)
            except Exception as e:
                logger.warning(f"[Positions] MT5 SL sync failed: {e}")

        return True

    def update_tp(self, trade_id: str, new_tp: float,
                   reason: str = "") -> bool:
        with self._lock:
            pos = self._positions.get(trade_id)
            if not pos:
                logger.warning(f"[Positions] update_tp: {trade_id} not found.")
                return False

            old_tp    = pos["current_tp"]
            direction = pos["direction"]

            if direction in ("buy", "long") and new_tp <= old_tp:
                return False
            if direction in ("sell", "short") and new_tp >= old_tp:
                return False

            pos["current_tp"] = round(new_tp, 5)
            pos["tp_extensions"].append({
                "time":   datetime.now(timezone.utc).isoformat(),
                "old_tp": old_tp,
                "new_tp": round(new_tp, 5),
                "reason": reason,
            })
            pos["last_updated"] = datetime.now(timezone.utc).isoformat()
            symbol = pos["symbol"]
            self._save()

        logger.info(
            f"[Positions] TP EXTENDED: {symbol} {old_tp}→{new_tp} ({reason})"
        )
        self._notify(
            f"📈 TP EXTENDED: {symbol}\n"
            f"   {old_tp} → {new_tp}\n"
            f"   Reason: {reason}"
        )
        return True

    def mark_tp1_hit(self, trade_id: str):
        with self._lock:
            pos = self._positions.get(trade_id)
            if pos:
                pos["tp1_hit"]      = True
                pos["last_updated"] = datetime.now(timezone.utc).isoformat()
                self._save()

    # ─────────────────────────────────────────────────────────────────────────
    # PARTIAL CLOSE (calls update_sl → RLock re-entrant handles this)
    # ─────────────────────────────────────────────────────────────────────────

    def partial_close(self, trade_id: str, close_pct: float,
                       at_price: float, r_level: float,
                       new_sl: Optional[float] = None) -> dict:
        with self._lock:
            pos = self._positions.get(trade_id)
            if not pos:
                return {"error": f"Trade {trade_id} not found"}

            entry         = pos["entry_price"]
            old_remaining = pos["remaining_pct"]
            closed_this   = old_remaining * (close_pct / 100)
            new_remaining = old_remaining - closed_this

            if entry == 0:
                return {"error": f"Trade {trade_id} has entry_price=0 — cannot compute PnL"}
            if pos["direction"] in ("buy", "long"):
                pnl_pct = (at_price - entry) / entry * 100
            else:
                pnl_pct = (entry - at_price) / entry * 100

            booking = {
                "time":                   datetime.now(timezone.utc).isoformat(),
                "r_level":                r_level,
                "at_price":               round(at_price, 5),
                "close_pct":              round(close_pct, 2),
                "closed_pct_of_original": round(closed_this, 2),
                "pnl_pct":                round(pnl_pct, 4),
            }

            pos["remaining_pct"] = round(new_remaining, 2)
            pos["bookings"].append(booking)
            pos["last_updated"]  = datetime.now(timezone.utc).isoformat()
            symbol = pos["symbol"]
            self._save()

        # update_sl acquires the lock itself — RLock allows re-entry
        if new_sl is not None:
            self.update_sl(trade_id, new_sl,
                           reason=f"partial book at {r_level}R")

        logger.info(
            f"[Positions] Partial close: {symbol} "
            f"{close_pct}% at {at_price} ({r_level}R) | "
            f"Remaining: {new_remaining:.1f}%"
        )
        self._notify(
            f"📊 PARTIAL CLOSE: {symbol}\n"
            f"   Closed {close_pct:.0f}% at {at_price}\n"
            f"   R level: +{r_level:.1f}R\n"
            f"   Remaining: {new_remaining:.1f}%"
        )
        return booking

    # ─────────────────────────────────────────────────────────────────────────
    # EVENT HEDGING
    # ─────────────────────────────────────────────────────────────────────────

    def apply_event_hedge(self, trade_id: str,
                           reduce_pct: float = 50.0,
                           event_name: str = "") -> bool:
        with self._lock:
            pos = self._positions.get(trade_id)
            if not pos or pos.get("event_hedged"):
                return False

            reduce_pct = min(max(reduce_pct, 0.0), 99.0)  # guard against ≥100% → negative lot
            pos["pre_event_lot_size"] = pos["lot_size"]
            pos["lot_size"]           = round(
                pos["lot_size"] * (1 - reduce_pct / 100), 4
            )
            pos["event_hedged"]    = True
            pos["event_hedge_pct"] = reduce_pct
            pos["last_updated"]    = datetime.now(timezone.utc).isoformat()
            symbol = pos["symbol"]
            self._save()

        logger.info(
            f"[Positions] Event hedge: {symbol} "
            f"{pos['pre_event_lot_size']}→{pos['lot_size']} "
            f"({reduce_pct}% for {event_name})"
        )
        return True

    def restore_after_event(self, trade_id: str) -> bool:
        with self._lock:
            pos = self._positions.get(trade_id)
            if not pos or not pos.get("event_hedged"):
                return False

            pos["lot_size"]        = pos["pre_event_lot_size"]
            pos["event_hedged"]    = False
            pos["event_hedge_pct"] = 0.0
            pos["last_updated"]    = datetime.now(timezone.utc).isoformat()
            symbol = pos["symbol"]
            self._save()

        logger.info(
            f"[Positions] Event restored: {symbol} →{pos['lot_size']}"
        )
        return True

    # ─────────────────────────────────────────────────────────────────────────
    # CLOSE
    # ─────────────────────────────────────────────────────────────────────────

    def close(self, trade_id: str, exit_price: float,
               r_multiple: float, outcome: str) -> Optional[dict]:
        """
        Fully close a position.
        Lock held only for the mutation. All downstream calls (DB, streak,
        paper equity, Telegram) happen outside the lock to avoid
        holding it during potentially slow I/O operations.
        """
        with self._lock:
            pos = self._positions.get(trade_id)
            if not pos:
                logger.warning(f"[Positions] close: {trade_id} not found.")
                return None

            open_time  = datetime.fromisoformat(pos["open_time"])
            close_time = datetime.now(timezone.utc)
            hold_h     = (close_time - open_time).total_seconds() / 3600
            risk_pct   = pos.get("risk_pct", 1.0)

            closed_trade = {
                **pos,
                "exit_price":      round(exit_price, 5),
                "r_multiple":      round(r_multiple, 3),
                "outcome":         outcome,
                "close_time":      close_time.isoformat(),
                "hold_duration_h": round(hold_h, 2),
                "pnl_pct":         round(r_multiple * risk_pct, 4),
            }

            del self._positions[trade_id]
            self._save()

        # ── All downstream work outside the lock ──────────────────────────
        # This prevents holding the lock during DB writes, HTTP calls, etc.

        try:
            from core.database_manager import db
            db.save_trade(closed_trade)
            db.delete_position(trade_id)
        except Exception as e:
            logger.warning(f"[Positions] DB save failed: {e}")

        try:
            signal_id = pos.get("signal_id")
            if signal_id:
                from core.database_manager import db
                db.update_ml_outcome(signal_id, outcome, r_multiple)
        except Exception as e:
            logger.warning(f"[Positions] ML outcome backfill failed: {e}")

        try:
            from core.streak_state import streak
            streak.record_trade_result(r_multiple)
        except Exception as e:
            logger.warning(f"[Positions] Streak update failed: {e}")

        if pos.get("is_paper", True):
            try:
                from core.paper_trading import get_paper_engine
                pe = get_paper_engine()
                pe.record_trade_result(r_multiple, risk_pct)
                logger.debug(
                    f"[Positions] Paper equity: "
                    f"R={r_multiple:+.2f} → ${pe.get_equity():,.2f}"
                )
            except Exception as e:
                logger.warning(f"[Positions] Paper equity update failed: {e}")

        try:
            from interfaces.telegram_interface import tg
            tg.send_trade_close(closed_trade)
        except Exception:
            pass

        logger.info(
            f"[Positions] Closed: {pos['symbol']} "
            f"| {outcome} | {r_multiple:+.2f}R "
            f"| held {hold_h:.1f}h"
        )

        # ── Zone 2: Jarvis post-mortem (async, non-blocking) ──────────────
        try:
            from intelligence.jarvis_llm import get_jarvis
            get_jarvis().write_trade_postmortem(closed_trade, run_async=True)
        except Exception:
            pass

        # ── Zone 4: Content factory (async, A+ trades only) ───────────────
        try:
            from content.trade_recap import get_content_factory
            get_content_factory().generate_trade_recap(
                pos["trade_id"], run_async=True
            )
        except Exception:
            pass

        return closed_trade

    # ─────────────────────────────────────────────────────────────────────────
    # QUERIES (read-only — lock for consistency, released immediately)
    # ─────────────────────────────────────────────────────────────────────────

    def get_all(self) -> List[dict]:
        with self._lock:
            return list(self._positions.values())

    def get(self, trade_id: str) -> Optional[dict]:
        with self._lock:
            return self._positions.get(trade_id)

    def get_by_symbol(self, symbol: str) -> Optional[dict]:
        with self._lock:
            for p in self._positions.values():
                if p["symbol"] == symbol:
                    return p
            return None

    def has_open_position(self, symbol: str) -> bool:
        with self._lock:
            return any(
                p["symbol"] == symbol for p in self._positions.values()
            )

    def count(self) -> int:
        with self._lock:
            return len(self._positions)

    def get_summary_message(self) -> str:
        with self._lock:
            positions_snapshot = list(self._positions.values())

        try:
            from infra.node_orchestrator import orchestrator
            my_node = orchestrator.get_active_node()
        except:
            my_node = "unknown"
            
        try:
            from data.market_data_router import get_data_router
            router = get_data_router()
        except:
            router = None
            
        if not positions_snapshot:
            return f"🟢 No open positions. (Node: {my_node})"

        lines = [
            f"📊 OPEN POSITIONS ({len(positions_snapshot)}) - Node: {my_node}",
            "───────────────────────────"
        ]
        for pos in positions_snapshot:
            direction = (
                "🟢 LONG" if pos["direction"] in ("buy", "long")
                else "🔴 SHORT"
            )
            tp_ext = len(pos.get("tp_extensions", []))
            
            floating_r_str = ""
            if router:
                try:
                    price = router.get_live_price(pos["symbol"])
                    if price:
                        entry = pos["entry_price"]
                        original_sl = pos.get("original_sl", pos["current_sl"])
                        sl_dist = abs(entry - original_sl)
                        if sl_dist <= 0: sl_dist = entry * 0.001
                        
                        if pos["direction"] in ("buy", "long"):
                            r = (price - entry) / sl_dist
                        else:
                            r = (entry - price) / sl_dist
                        floating_r_str = f"  Float : {r:+.2f}R\n"
                except Exception:
                    pass

            lines.append(
                f"\n{direction} {pos['symbol']}\n"
                f"  Entry : {pos['entry_price']}\n"
                f"  SL    : {pos['current_sl']}\n"
                f"  TP    : {pos['current_tp']}\n"
                + (f"  Ext   : +{tp_ext}\n" if tp_ext else "") +
                floating_r_str +
                f"  Remain: {pos.get('remaining_pct',100):.0f}%\n"
                f"  Grade : {pos.get('grade','?')} | "
                f"Risk: {pos.get('risk_pct',1.0):.2f}%\n"
                f"  Opened: {pos.get('open_time','?')[:10]}"
            )
        return "\n".join(lines)

    def _notify(self, msg: str):
        try:
            from interfaces.telegram_interface import tg
            tg.send(msg)
        except Exception:
            pass


# ── Singleton ─────────────────────────────────────────────────────────────────
positions = PositionTracker()
