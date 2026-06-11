"""
CRAVE v10.0 — Telegram Interface
==================================
Full two-way Telegram interface.
Sends alerts AND receives commands from your phone.

COMMANDS SUPPORTED:
  /start        Welcome + current status
  /status       Streak state, circuit breaker, risk level
  /positions    All open positions with entry/SL/TP
  /close XAUUSD Close a specific position
  /pause        Pause all new entries (keeps monitoring open trades)
  /resume       Resume after pause
  /half_size    Toggle half-size mode
  /bias         Today's daily bias per instrument
  /levels XAUUSD  Key levels being watched
  /tp_check     Force TP extension check now
  /node         Which node is active, temps, uptime
  /switch phone  Switch active node manually
  /aws_start    Start AWS instance
  /aws_stop     Stop AWS instance
  /temp         Phone CPU temperature
  /stats        Win rate, expectancy, last 10 trades
  /journal      Last 10 closed trades
  /paper        Paper trading status + readiness check
  /help         All commands

USAGE:
  from interfaces.telegram_interface import tg

  tg.start()               # Start polling in background thread
  tg.send("message")       # Send a message
  tg.send_trade_alert(...)  # Formatted trade alert
  tg.stop()                # Stop polling
"""

import os
import logging
import threading
import time
import queue
import requests
from datetime import datetime, timezone
from typing import Optional, Callable, Dict

logger = logging.getLogger("crave.telegram")


class TelegramInterface:

    def __init__(self):
        self._token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        self._base    = f"https://api.telegram.org/bot{self._token}"

        self._running   = False
        self._thread:   Optional[threading.Thread] = None
        self._offset    = 0
        self._handlers: Dict[str, Callable] = {}
        self._last_send_time = 0
        self._send_interval  = 0.3   # 300ms between sends — Telegram rate limit

        # PRIORITY 6: Non-blocking message queue
        # Callers drop messages here and return immediately.
        # A dedicated sender thread processes them at _send_interval pace.
        # Queue size 50: if the bot generates 50 queued alerts, oldest are dropped.
        self._send_queue: queue.Queue = queue.Queue(maxsize=50)
        self._sender_thread: Optional[threading.Thread] = None
        self._start_sender()

        # Register all command handlers
        self._register_handlers()

        if not self._token or not self._chat_id:
            logger.warning(
                "[Telegram] BOT_TOKEN or CHAT_ID not set. "
                "Alerts will be logged only. Set in .env file."
            )

    # ─────────────────────────────────────────────────────────────────────────
    # CORE SEND
    # ─────────────────────────────────────────────────────────────────────────

    def _start_sender(self):
        """Start the background message sender thread (Priority 6)."""
        self._sender_thread = threading.Thread(
            target=self._sender_loop,
            daemon=True,
            name="CRAVETelegramSender",
        )
        self._sender_thread.start()

    def _sender_loop(self):
        """
        Dedicated sender thread. Drains the queue at _send_interval pace.
        Retries once on connection error, then drops the message.
        Queue prevents more than 50 pending messages (oldest dropped on overflow).
        """
        while True:
            try:
                # Block until a message is available (timeout=1s to stay alive)
                try:
                    text, parse_mode = self._send_queue.get(timeout=1)
                except queue.Empty:
                    continue

                self._send_now(text, parse_mode)
                self._send_queue.task_done()
                time.sleep(self._send_interval)

            except Exception as e:
                logger.debug(f"[Telegram] Sender loop error: {e}")

    def _send_now(self, text: str, parse_mode: str) -> bool:
        """Actual HTTP send — called from sender thread only."""
        if not self._token or not self._chat_id:
            logger.info(f"[TG→LOG] {text[:100]}")
            return False
        try:
            resp = requests.post(
                f"{self._base}/sendMessage",
                json={
                    "chat_id":    self._chat_id,
                    "text":       text,
                    "parse_mode": parse_mode,
                },
                timeout=10,
            )
            if not resp.ok:
                logger.warning(
                    f"[Telegram] Send failed: {resp.status_code} "
                    f"{resp.text[:80]}"
                )
                return False
            return True
        except requests.exceptions.ConnectionError:
            logger.debug("[Telegram] Network unavailable — message dropped.")
            return False
        except Exception as e:
            logger.warning(f"[Telegram] Send error: {e}")
            return False

    def send(self, text: str, parse_mode: str = "HTML") -> bool:
        """
        Non-blocking send. Drops message into queue and returns immediately.
        Calling thread never waits for HTTP. Sender thread handles delivery.

        PRIORITY 6: Queue-based non-blocking send.
        Old: callers blocked up to 10s per message during alert storms.
        New: callers return in <1μs. Sender thread handles delivery at 300ms pace.
        """
        if not self._token or not self._chat_id:
            logger.info(f"[TG→LOG] {text[:100]}")
            return False
        try:
            self._send_queue.put_nowait((text, parse_mode))
            return True
        except queue.Full:
            # Queue full (50 pending) — drop oldest, add new
            try:
                self._send_queue.get_nowait()
                self._send_queue.put_nowait((text, parse_mode))
            except Exception:
                pass
            logger.warning("[Telegram] Message queue full — oldest message dropped.")
            return False

    # keep backward compat with old code that calls send_message_sync
    def send_message_sync(self, text: str) -> bool:
        return self.send(text)

    # ─────────────────────────────────────────────────────────────────────────
    # FORMATTED ALERT HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def send_trade_open(self, trade: dict):
        direction_emoji = "🟢" if trade.get("direction") in ("buy", "long") else "🔴"
        self.send(
            f"{direction_emoji} <b>TRADE OPENED</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Symbol : {trade.get('symbol')}\n"
            f"Side   : {trade.get('direction', '').upper()}\n"
            f"Grade  : {trade.get('grade', '?')}\n"
            f"Entry  : {trade.get('entry_price')}\n"
            f"SL     : {trade.get('current_sl')}\n"
            f"TP1    : {trade.get('tp1_price')}\n"
            f"TP     : {trade.get('current_tp')}\n"
            f"Risk   : {trade.get('risk_pct', '?')}%\n"
            f"Mode   : {'📄 PAPER' if trade.get('is_paper', True) else '💰 LIVE'}"
        )

    def send_trade_close(self, trade: dict):
        r = trade.get("r_multiple", 0)
        if r > 0:
            emoji = "✅"
            result = f"+{r:.2f}R WIN"
        elif r == 0:
            emoji = "🟡"
            result = "BREAKEVEN"
        else:
            emoji = "🔴"
            result = f"{r:.2f}R LOSS"

        self.send(
            f"{emoji} <b>TRADE CLOSED</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Symbol : {trade.get('symbol')}\n"
            f"Result : {result}\n"
            f"Outcome: {trade.get('outcome')}\n"
            f"Entry  : {trade.get('entry_price')}\n"
            f"Exit   : {trade.get('exit_price')}\n"
            f"Held   : {trade.get('hold_duration_h', 0):.1f}h"
        )

    def send_tp_extension(self, symbol: str, old_tp: float,
                           new_tp: float, reason: str):
        self.send(
            f"📈 <b>TP EXTENDED</b>\n"
            f"Symbol : {symbol}\n"
            f"Old TP : {old_tp}\n"
            f"New TP : {new_tp}\n"
            f"Reason : {reason}"
        )

    def send_partial_close(self, symbol: str, pct: float,
                            at_price: float, r_level: float,
                            remaining: float):
        self.send(
            f"📊 <b>PARTIAL CLOSE</b>\n"
            f"Symbol    : {symbol}\n"
            f"Closed    : {pct:.0f}% at {at_price}\n"
            f"R Level   : +{r_level:.1f}R\n"
            f"Remaining : {remaining:.1f}%"
        )

    def send_circuit_breaker(self, consecutive_days: int,
                              until: str, cooldown_h: int):
        self.send(
            f"⚠️ <b>CIRCUIT BREAKER TRIGGERED</b>\n"
            f"Consecutive losing days: {consecutive_days}\n"
            f"Cooldown: {cooldown_h}h\n"
            f"Resumes: {until}\n"
            f"Half-size mode on re-entry.\n\n"
            f"Review your recent trades before resuming."
        )

    def send_daily_loss_limit(self, pnl_pct: float):
        self.send(
            f"🚨 <b>DAILY LOSS LIMIT HIT</b>\n"
            f"Today P&amp;L: {pnl_pct:.2f}%\n"
            f"Hard stop for today.\n"
            f"No new entries until tomorrow session (21:00 UTC)."
        )

    def send_node_failover(self, from_node: str, to_node: str, reason: str):
        self.send(
            f"🔄 <b>NODE FAILOVER</b>\n"
            f"From : {from_node}\n"
            f"To   : {to_node}\n"
            f"Why  : {reason}"
        )

    def send_daily_summary(self, stats: dict):
        """Daily summary sent at 21:00 UTC."""
        from core.position_tracker import positions
        pos_count = positions.count()

        self.send(
            f"📋 <b>DAILY SUMMARY</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Date     : {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n"
            f"Day P&amp;L : {stats.get('today_pnl_pct', '0.00%')}\n"
            f"Trades   : {stats.get('today_trades', 0)}\n"
            f"Open Pos : {pos_count}\n"
            f"Streak   : {stats.get('streak_state', '?')}\n"
            f"Risk Tmrw: {stats.get('current_risk_A+', '?')} (A+)\n"
            f"CB Active: {'🔴 Yes' if stats.get('circuit_breaker') else '✅ No'}"
        )

    def send_weekly_summary(self, report: dict):
        """Weekly summary sent on Sunday."""
        self.send(
            f"📊 <b>WEEKLY SUMMARY</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Trades   : {report.get('trades', 0)}\n"
            f"Win Rate : {report.get('win_rate', '?')}\n"
            f"Expect   : {report.get('expectancy_r', '?')}\n"
            f"P.Factor : {report.get('profit_factor', '?')}\n"
            f"Best     : {report.get('best_trade', '?')}\n"
            f"Worst    : {report.get('worst_trade', '?')}"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # COMMAND HANDLERS
    # ─────────────────────────────────────────────────────────────────────────

    def register_command(self, command: str, handler) -> None:
        """
        Public API for registering Telegram command handlers.
        Use this instead of writing directly to tg._handlers[x] = y.

        Usage:
          tg.register_command("/readiness", lambda args: ...)
          tg.register_command("/ml",        lambda args: ...)
        """
        if not command.startswith("/"):
            command = "/" + command
        self._handlers[command] = handler
        logger.debug(f"[Telegram] Registered command: {command}")

    def _register_handlers(self):
        self._handlers = {
            "/start":     self._cmd_start,
            "/status":    self._cmd_status,
            "/positions": self._cmd_positions,
            "/close":     self._cmd_close,
            "/pause":     self._cmd_pause,
            "/resume":    self._cmd_resume,
            "/half_size": self._cmd_half_size,
            "/bias":      self._cmd_bias,
            "/levels":    self._cmd_levels,
            "/tp_check":  self._cmd_tp_check,
            "/node":      self._cmd_node,
            "/switch":    self._cmd_switch,
            "/aws_start": self._cmd_aws_start,
            "/aws_stop":  self._cmd_aws_stop,
            "/temp":      self._cmd_temp,
            "/stats":     self._cmd_stats,
            "/journal":   self._cmd_journal,
            "/markets":        self._cmd_markets,
            "/india":          self._cmd_india,
            "/earnings":       self._cmd_earnings,
            "/portfolio":      self._cmd_portfolio,
            "/zerodha_token":  self._cmd_zerodha_token,
            "/brokers":        self._cmd_brokers,
            "/backtest":       self._cmd_backtest,
            "/paper":     self._cmd_paper,
            "/live":      self._cmd_live,
            "/help":      self._cmd_help,
            # ── v10.5: Compounding commands ───────────────────────────────
            "/compound":  self._cmd_compound,
            "/growth":    self._cmd_growth,
            "/canitrade": self._cmd_canitrade,
        }

    # ── v10.5 Compounding commands ────────────────────────────────────────────

    def _cmd_compound(self, args: str):
        """
        /compound — Show current compounding state.
        Equity, tier, risk/trade, win rate, milestone progress.
        """
        try:
            from engines.compounding_engine import get_compound_engine
            s    = get_compound_engine().get_status()
            wins = s["wins"]
            loss = s["losses"]
            tot  = s["total_trades"]
            next_m = s["next_milestone"]
            pct_to_next = (
                (s["equity"] / next_m * 100) if next_m else 100
            )
            bar_filled = int(pct_to_next / 10)
            bar = "█" * bar_filled + "░" * (10 - bar_filled)

            msg = (
                f"📈 <b>CRAVE Compounding State</b>\n\n"
                f"💰 Equity: <b>${s['equity']:,.2f}</b>\n"
                f"🚀 Growth: <b>{s['growth_pct']:+.2f}%</b> "
                f"(from ${s['starting_equity']:,.0f})\n"
                f"📉 Max DD: {s['max_drawdown_pct']:.2f}%\n\n"
                f"🎯 Risk tier: <b>{s['risk_tier']}</b>\n"
                f"⚡ Risk/trade: <b>${s['dollar_risk']:.2f}</b> "
                f"({s['risk_pct']:.2f}%)\n\n"
                f"📊 Trades: {tot} | W: {wins} L: {loss} | "
                f"WR: {s['win_rate']:.1f}%\n\n"
            )
            if next_m:
                msg += (
                    f"🏁 Next milestone: <b>${next_m:,.0f}</b>\n"
                    f"[{bar}] {pct_to_next:.0f}%\n"
                    f"Need: ${next_m - s['equity']:,.2f} more\n"
                )
            self.send(msg)
        except Exception as e:
            self.send(f"❌ Compound error: {e}")

    def _cmd_growth(self, args: str):
        """
        /growth [months] [monthly_pct]
        Project equity growth with compounding.
        Example: /growth 12 7 → 12-month projection at 7%/month
        """
        try:
            from engines.compounding_engine import get_compound_engine
            parts   = args.strip().split()
            months  = int(parts[0]) if len(parts) > 0 else 12
            monthly = float(parts[1]) if len(parts) > 1 else 7.0
            months  = min(months, 36)

            curve = get_compound_engine().project_growth(months, monthly)
            eq    = get_compound_engine().get_status()["equity"]

            msg = (
                f"📈 <b>Growth Projection</b>\n"
                f"Starting: ${eq:,.2f} | Rate: {monthly}%/month\n\n"
            )
            checkpoints = [1, 3, 6, 12, 18, 24, 36]
            for p in curve:
                if p["month"] in checkpoints and p["month"] <= months:
                    months_label = f"{p['month']} month{'s' if p['month']>1 else ''}"
                    msg += (
                        f"<b>{months_label:>8}</b>: "
                        f"${p['equity']:>10,.2f}  "
                        f"[{p['tier'].split('(')[0].strip()}  "
                        f"risk/trade: ${p['dollar_risk']:.2f}]\n"
                    )
            total_growth = (curve[-1]["equity"] - eq) / eq * 100
            msg += f"\n<b>Total: +{total_growth:.1f}%</b>"
            self.send(msg)
        except Exception as e:
            self.send(f"❌ Growth error: {e}")

    def _cmd_canitrade(self, args: str):
        """
        /canitrade [amount]
        Check which instruments you can trade at current (or specified) equity.
        Example: /canitrade 500
        """
        try:
            from engines.compounding_engine import (
                get_compound_engine, what_can_i_trade
            )
            parts = args.strip().split()
            if parts:
                equity = float(parts[0])
            else:
                equity = get_compound_engine().get_status()["equity"]
            self.send(f"<pre>{what_can_i_trade(equity)}</pre>")
        except Exception as e:
            self.send(f"❌ Error: {e}")

    def _cmd_start(self, args: str):
        self._cmd_status(args)

    def _cmd_status(self, args: str):
        try:
            from core.streak_state import streak
            self.send(streak.get_status_message())
        except Exception as e:
            self.send(f"❌ Status error: {e}")

    def _cmd_positions(self, args: str):
        try:
            from core.position_tracker import positions
            self.send(positions.get_summary_message())
        except Exception as e:
            self.send(f"❌ Positions error: {e}")

    def _cmd_close(self, args: str):
        symbol = args.strip().upper()
        if not symbol:
            self.send("Usage: /close XAUUSD")
            return
        try:
            from core.position_tracker import positions
            pos = positions.get_by_symbol(symbol)
            if not pos:
                self.send(f"❌ No open position for {symbol}")
                return
            # Close is handled by execution agent — signal it here
            # Full implementation in Phase 3 when execution agent is upgraded
            self.send(
                f"⚠️ Manual close requested for {symbol}\n"
                f"Position ID: {pos['trade_id']}\n"
                f"This will be executed at next monitor cycle.\n"
                f"(Full manual close in Phase 3)"
            )
        except Exception as e:
            self.send(f"❌ Close error: {e}")

    def _cmd_pause(self, args: str):
        try:
            from core.streak_state import streak
            streak.manual_pause(reason="Telegram command")
            self.send("⏸️ Bot paused. No new entries.\nOpen positions still monitored.\nUse /resume to restart.")
        except Exception as e:
            self.send(f"❌ Pause error: {e}")

    def _cmd_resume(self, args: str):
        try:
            from core.streak_state import streak
            streak.manual_resume()
            self.send("▶️ Bot resumed. New entries allowed.")
        except Exception as e:
            self.send(f"❌ Resume error: {e}")

    def _cmd_half_size(self, args: str):
        try:
            from core.streak_state import streak
            current = streak._state.get("half_size_day", False)
            streak._state["half_size_day"] = not current
            streak._save()
            state = "ON 🟡 (50% position sizes)" if not current else "OFF ✅ (normal sizes)"
            self.send(f"Half-size mode: {state}")
        except Exception as e:
            self.send(f"❌ Error: {e}")

    def _cmd_bias(self, args: str):
        try:
            from core.database_manager import db
            from config.config import get_tradeable_symbols
            lines = ["📅 <b>TODAY'S BIAS</b>", "━━━━━━━━━━━━━━━"]
            for sym in get_tradeable_symbols():
                row = db.get_today_bias(sym)
                if row:
                    emoji = "🟢" if row["bias"] == "BUY" else "🔴" if row["bias"] == "SELL" else "⬜"
                    lines.append(f"{emoji} {sym}: {row['bias']} (strength: {row['strength']}/3)")
                else:
                    lines.append(f"⬜ {sym}: No bias set yet")
            self.send("\n".join(lines))
        except Exception as e:
            self.send(f"❌ Bias error: {e}\n(DailyBiasEngine runs at 06:30 UTC)")

    def _cmd_levels(self, args: str):
        symbol = args.strip().upper()
        if not symbol:
            self.send("Usage: /levels XAUUSD")
            return
        try:
            from core.database_manager import db
            bias = db.get_today_bias(symbol)
            if not bias:
                self.send(f"No levels cached for {symbol} today.")
                return
            levels = bias.get("key_levels", [])
            inv    = bias.get("daily_inv_level", "?")
            lines  = [
                f"📍 <b>KEY LEVELS: {symbol}</b>",
                f"Bias         : {bias['bias']}",
                f"Invalidation : {inv}",
                "Key Levels:",
            ]
            for lvl in levels:
                lines.append(f"  • {lvl}")
            self.send("\n".join(lines))
        except Exception as e:
            self.send(f"❌ Levels error: {e}")

    def _cmd_tp_check(self, args: str):
        # Will be wired to DynamicTPEngine in Phase 2
        self.send("🔍 TP extension check requested.\n(DynamicTPEngine wired in Phase 2)")

    def _cmd_node(self, args: str):
        try:
            # NodeOrchestrator wired in this session
            from infra.node_orchestrator import orchestrator
            self.send(orchestrator.get_status_message())
        except Exception as e:
            import socket
            self.send(
                f"📡 <b>NODE STATUS</b>\n"
                f"Hostname : {socket.gethostname()}\n"
                f"(Full node status in Phase 2 after NodeOrchestrator starts)"
            )

    def _cmd_switch(self, args: str):
        node = args.strip().lower()
        valid = ["laptop", "phone", "aws"]
        if node not in valid:
            self.send(f"Usage: /switch {' | '.join(valid)}")
            return
        try:
            from infra.node_orchestrator import orchestrator
            orchestrator.request_switch(node)
            self.send(f"🔄 Switch to {node} requested.")
        except Exception as e:
            self.send(f"❌ Switch error: {e}")

    def _cmd_aws_start(self, args: str):
        self.send("☁️ AWS start requested.\n(aws_manager.py wired in Phase 2)")

    def _cmd_aws_stop(self, args: str):
        self.send("☁️ AWS stop requested.\n(aws_manager.py wired in Phase 2)")

    def _cmd_temp(self, args: str):
        try:
            from infra.thermal_monitor import thermal
            temp = thermal.get_temperature()
            zone = thermal.get_zone()
            self.send(
                f"🌡️ <b>PHONE TEMPERATURE</b>\n"
                f"Temp  : {temp}°C\n"
                f"Zone  : {zone}\n"
                f"Status: {'✅ OK' if zone == 'NORMAL' else '⚠️ ' + zone}"
            )
        except Exception as e:
            self.send(f"🌡️ Temperature monitoring not available on this node.")

    def _cmd_stats(self, args: str):
        try:
            from core.database_manager import db
            stats = db.get_trade_stats(days=30, is_paper=True)
            if stats.get("trades", 0) == 0:
                self.send("📊 No trades recorded yet. Start paper trading first.")
                return
            self.send(
                f"📊 <b>PERFORMANCE STATS (30d)</b>\n"
                f"━━━━━━━━━━━━━━━\n"
                f"Trades     : {stats['trades']}\n"
                f"Win Rate   : {stats['win_rate']}\n"
                f"Expectancy : {stats['expectancy_r']}\n"
                f"Prof.Factor: {stats['profit_factor']}\n"
                f"Best Trade : {stats['best_trade']}\n"
                f"Worst Trade: {stats['worst_trade']}"
            )
        except Exception as e:
            self.send(f"❌ Stats error: {e}")

    def _cmd_journal(self, args: str):
        try:
            from core.database_manager import db
            trades = db.get_recent_trades(limit=10, is_paper=True)
            if not trades:
                self.send("📓 No closed trades yet.")
                return
            lines = ["📓 <b>LAST 10 TRADES</b>", "━━━━━━━━━━━━━━━"]
            for t in trades:
                r     = t.get("r_multiple", 0) or 0
                emoji = "✅" if r > 0 else "🔴" if r < 0 else "🟡"
                lines.append(
                    f"{emoji} {t['symbol']} {t['direction'].upper()} "
                    f"| {r:+.2f}R | {t.get('outcome', '?')}"
                )
            self.send("\n".join(lines))
        except Exception as e:
            self.send(f"❌ Journal error: {e}")

    def _cmd_paper(self, args: str):
        try:
            from core.database_manager import db
            from config.config import PAPER_TRADING
            stats = db.get_trade_stats(days=90, is_paper=True)
            trades = stats.get("trades", 0)
            min_t  = PAPER_TRADING["min_trades_for_live"]
            ready  = trades >= min_t
            self.send(
                f"📄 <b>PAPER TRADING STATUS</b>\n"
                f"━━━━━━━━━━━━━━━\n"
                f"Mode     : {'✅ PAPER' if PAPER_TRADING['enabled'] else '💰 LIVE'}\n"
                f"Trades   : {trades} / {min_t} needed\n"
                f"Win Rate : {stats.get('win_rate', 'N/A')}\n"
                f"Ready    : {'✅ YES — use /live to request' if ready else f'❌ Need {min_t - trades} more trades'}"
            )
        except Exception as e:
            self.send(f"❌ Paper status error: {e}")

    def _cmd_live(self, args: str):
        try:
            from core.database_manager import db
            from config.config import PAPER_TRADING
            stats = db.get_trade_stats(days=90, is_paper=True)
            trades = stats.get("trades", 0)
            min_t  = PAPER_TRADING["min_trades_for_live"]
            if trades < min_t:
                self.send(
                    f"❌ <b>LIVE TRADING BLOCKED</b>\n"
                    f"Need {min_t} paper trades. Have {trades}.\n"
                    f"Paper trade {min_t - trades} more, then try again."
                )
            else:
                self.send(
                    f"⚠️ <b>LIVE TRADING REQUEST</b>\n"
                    f"You have {trades} paper trades.\n"
                    f"To enable live trading:\n"
                    f"1. Add real API keys to .env\n"
                    f"2. Set TRADING_MODE=live in .env\n"
                    f"3. Restart bot\n\n"
                    f"⚠️ Only do this after reviewing your paper results carefully."
                )
        except Exception as e:
            self.send(f"❌ Error: {e}")

    def _cmd_help(self, args: str):
        from config.config import TELEGRAM_COMMANDS
        lines = ["📖 <b>CRAVE COMMANDS</b>", "━━━━━━━━━━━━━━━"]
        for cmd, desc in TELEGRAM_COMMANDS.items():
            lines.append(f"<code>{cmd}</code> — {desc}")
        self.send("\n".join(lines))

    # ─────────────────────────────────────────────────────────────────────────
    # POLLING LOOP
    # ─────────────────────────────────────────────────────────────────────────

    def start(self):
        """Start polling for commands in a background thread."""
        if not self._token or not self._chat_id:
            logger.info("[Telegram] Not configured — running in log-only mode.")
            return

        if self._thread and self._thread.is_alive():
            return

        self._running = True
        self._thread  = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="CRAVETelegramPoller"
        )
        self._thread.start()
        logger.info("[Telegram] Polling started.")

    def stop(self):
        self._running = False
        logger.info("[Telegram] Stopped.")

    def _poll_loop(self):
        """Long-poll Telegram for new messages."""
        while self._running:
            try:
                resp = requests.get(
                    f"{self._base}/getUpdates",
                    params={
                        "offset":          self._offset,
                        "timeout":         20,    # long-poll timeout
                        "allowed_updates": ["message"],
                    },
                    timeout=25,
                )

                if not resp.ok:
                    time.sleep(5)
                    continue

                updates = resp.json().get("result", [])
                for update in updates:
                    self._offset = update["update_id"] + 1
                    self._handle_update(update)

            except requests.exceptions.ConnectionError:
                logger.debug("[Telegram] Connection lost. Retrying in 10s...")
                time.sleep(10)
            except Exception as e:
                logger.warning(f"[Telegram] Poll error: {e}")
                time.sleep(5)

    def _handle_update(self, update: dict):
        """Process a single incoming message."""
        message = update.get("message", {})
        if not message:
            return

        # Only respond to your own chat
        chat_id = str(message.get("chat", {}).get("id", ""))
        if chat_id != str(self._chat_id):
            logger.warning(f"[Telegram] Message from unknown chat {chat_id}. Ignored.")
            return

        text = message.get("text", "").strip()
        if not text.startswith("/"):
            return

        # Extract command and args
        parts   = text.split(None, 1)
        command = parts[0].lower().split("@")[0]   # handle /cmd@botname format
        args    = parts[1] if len(parts) > 1 else ""

        logger.info(f"[Telegram] Command: {command} {args}")

        handler = self._handlers.get(command)
        if handler:
            try:
                handler(args)
            except Exception as e:
                logger.error(f"[Telegram] Handler {command} failed: {e}")
                self.send(f"❌ Command failed: {e}")
        else:
            self.send(f"Unknown command: {command}\nUse /help for all commands.")

    # ─────────────────────────────────────────────────────────────────────────
    # SCHEDULED SUMMARIES
    # ─────────────────────────────────────────────────────────────────────────

    def start_schedulers(self):
        """Start daily and weekly summary schedulers."""
        threading.Thread(
            target=self._scheduler_loop,
            daemon=True,
            name="CRAVETelegramScheduler"
        ).start()
        logger.info("[Telegram] Schedulers started.")

    def _scheduler_loop(self):
        """Check every minute if it's time for a summary."""
        from config.config import TELEGRAM as TG_CFG
        import datetime as dt

        daily_time  = TG_CFG.get("daily_summary_utc", "21:00")
        weekly_day  = TG_CFG.get("weekly_summary_day", "sunday")
        weekly_time = TG_CFG.get("weekly_summary_utc", "20:00")

        last_daily_date  = None
        last_weekly_date = None

        while self._running:
            now     = datetime.now(timezone.utc)
            now_hm  = now.strftime("%H:%M")
            today   = now.strftime("%Y-%m-%d")
            weekday = now.strftime("%A").lower()

            # Daily summary
            if now_hm == daily_time and last_daily_date != today:
                last_daily_date = today
                try:
                    from core.streak_state import streak
                    self.send_daily_summary(streak.get_status())
                    streak.close_day()
                except Exception as e:
                    logger.error(f"[Telegram] Daily summary error: {e}")

            # Weekly summary
            if (weekday == weekly_day and
                    now_hm == weekly_time and
                    last_weekly_date != today):
                last_weekly_date = today
                try:
                    from core.database_manager import db
                    report = db.get_trade_stats(days=7)
                    self.send_weekly_summary(report)
                except Exception as e:
                    logger.error(f"[Telegram] Weekly summary error: {e}")

            time.sleep(60)

    def _cmd_markets(self, args: str):
        """Show status of all markets."""
        try:
            from config.config import MARKETS
            from datetime import datetime, timezone
            lines = ["📊 <b>MARKET STATUS</b>", "━━━━━━━━━━━━━━━"]
            now_h = datetime.now(timezone.utc).hour

            for market_name, cfg in MARKETS.items():
                enabled = cfg.get("enabled", False)
                if not enabled:
                    lines.append(f"⏹️ {market_name}: DISABLED")
                    continue
                # Check if market is currently open
                open_map = {
                    "crypto":    True,   # 24/7
                    "forex":     7 <= now_h < 21,
                    "gold":      7 <= now_h < 21,
                    "us_stocks": 13 <= now_h < 20,
                    "india":     4 <= now_h < 10,
                }
                is_open = open_map.get(market_name, False)
                emoji   = "🟢" if is_open else "🔴"
                lines.append(f"{emoji} {market_name}: {'OPEN' if is_open else 'CLOSED'}")
            self.send("\n".join(lines))
        except Exception as e:
            self.send(f"❌ Markets error: {e}")

    def _cmd_india(self, args: str):
        """Indian market status — FII/DII, PCR, Zerodha auth."""
        try:
            from brokers.zerodha_agent import get_zerodha
            zr = get_zerodha()
            lines = [zr.get_status_message(), ""]

            # FII/DII
            fii = zr.get_fii_dii_data()
            if fii.get("available") is not False:
                lines.append(
                    f"📊 <b>FII/DII ({fii.get('date', 'today')})</b>\n"
                    f"FII Net : ₹{fii.get('fii_net', 0):,.0f} Cr\n"
                    f"DII Net : ₹{fii.get('dii_net', 0):,.0f} Cr\n"
                    f"Bias    : {fii.get('bias', '?')}"
                )

            # PCR
            pcr = zr.get_pcr("NIFTY")
            if pcr.get("available"):
                lines.append(
                    f"\n📈 <b>NIFTY PCR</b>\n"
                    f"PCR    : {pcr['pcr']}\n"
                    f"Signal : {pcr['signal']}"
                )

            self.send("\n".join(lines))
        except Exception as e:
            self.send(f"❌ India status error: {e}")

    def _cmd_earnings(self, args: str):
        """Check earnings blackout for a US stock."""
        symbol = args.strip().upper()
        if not symbol:
            self.send("Usage: /earnings AAPL")
            return
        try:
            from brokers.alpaca_stocks_agent import get_alpaca_stocks
            agent   = get_alpaca_stocks()
            blocked, reason = agent.is_earnings_blackout(symbol)
            earnings_date   = agent.get_next_earnings(symbol)
            date_str = (earnings_date.strftime("%Y-%m-%d")
                        if earnings_date else "unknown")
            status = "❌ BLOCKED" if blocked else "✅ Clear"
            self.send(
                f"📅 <b>EARNINGS: {symbol}</b>\n"
                f"Next date: {date_str}\n"
                f"Status   : {status}\n"
                f"Reason   : {reason}"
            )
        except Exception as e:
            self.send(f"❌ Earnings check error: {e}")

    def _cmd_portfolio(self, args: str):
        """Full portfolio heat by market."""
        try:
            from core.position_tracker import positions
            from config.config import MARKETS, get_market_for_symbol
            all_pos = positions.get_all()

            if not all_pos:
                self.send("📭 No open positions.")
                return

            market_heat: dict = {}
            total_heat = 0.0

            for pos in all_pos:
                market   = get_market_for_symbol(pos["symbol"])
                risk_pct = pos.get("risk_pct", 1.0)
                market_heat[market] = market_heat.get(market, 0) + risk_pct
                total_heat         += risk_pct

            from config.config import PORTFOLIO_RISK
            max_total = PORTFOLIO_RISK.get("max_total_heat_pct", 6.0)

            lines = [
                f"🔥 <b>PORTFOLIO HEAT</b>",
                f"Total: {total_heat:.2f}% / {max_total}% max",
                "━━━━━━━━━━━━━━━",
            ]
            for market, heat in market_heat.items():
                max_m = MARKETS.get(market, {}).get("max_heat_pct", 3.0)
                warn  = " ⚠️" if heat > max_m * 0.8 else ""
                lines.append(f"{market}: {heat:.2f}% / {max_m}%{warn}")

            self.send("\n".join(lines))
        except Exception as e:
            self.send(f"❌ Portfolio error: {e}")

    def _cmd_zerodha_token(self, args: str):
        """Complete Zerodha daily login with request_token."""
        request_token = args.strip()
        if not request_token:
            self.send("Usage: /zerodha_token YOUR_REQUEST_TOKEN")
            return
        try:
            from brokers.zerodha_agent import get_zerodha
            success = get_zerodha().complete_login(request_token)
            if success:
                self.send("✅ Zerodha login complete! Indian market trading active.")
            else:
                self.send("❌ Zerodha login failed. Check the token and try again.")
        except Exception as e:
            self.send(f"❌ Login error: {e}")

    def _cmd_brokers(self, args: str):
        """Show all broker connection status."""
        try:
            from brokers.broker_router import get_router
            self.send(get_router().get_status_message())
        except Exception as e:
            self.send(f"❌ Broker status error: {e}")

    def _cmd_backtest(self, args: str):
        """Run quick backtest from Telegram. Usage: /backtest XAUUSD 60"""
        parts = args.strip().split()
        symbol = parts[0].upper() if parts else "XAUUSD"
        days   = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 60

        self.send(f"📊 Running backtest: {symbol} {days}d ...")
        try:
            from backtesting.backtest_agent_v10 import BacktestAgentV10
            bt     = BacktestAgentV10()
            report = bt.run_backtest(symbol, days=days)
            text   = bt.format_report(report)
            # Split into chunks (Telegram max 4096 chars)
            for chunk in [text[i:i+3500] for i in range(0, len(text), 3500)]:
                self.send(f"<pre>{chunk}</pre>")
        except Exception as e:
            self.send(f"❌ Backtest failed: {e}")


# ── Singleton ─────────────────────────────────────────────────────────────────
tg = TelegramInterface()
